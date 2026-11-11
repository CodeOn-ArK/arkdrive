#!/usr/bin/python3
"""Per-window freshness with fake transports/accessibles and explicit time only."""
import copy
import queue
from types import SimpleNamespace as NS
import unittest

import test_worker_w4 as h
from test_worker_w4_fix7 import GuardBackend


class WindowFreshness(h.ProviderHarness):
    def setUp(self):
        super().setUp()
        self.dialog, self.dialog_raw = h.Dialogs.dialog(self)
        self.p.tick()
        self.answer(self.dialog_raw)
        self.entry = next(r for r, n in self.p.cache.windows[self.dialog]['nodes'].items()
                          if n['key'] == 'rename-entry')
        self.p.consumers.clear()

    def event(self, window=None, **fields):
        row = dict(event='changed', app='101', **fields)
        if window is not None:
            row['window'] = window
        self.wire.incoming.append(row)
        self.p.tick()

    def predicate(self, value='Folder'):
        return dict(kind='text_equals', window=self.dialog, ref=self.entry, value=value)

    def test_sibling_events_during_dialog_flight_install_clean_and_verify_immediately(self):
        task = self.p.request('snapshot', self.dialog)
        self.assertIsNone(self.p.request('snapshot', self.handle))  # one flight per app
        for _ in range(100):
            self.event(self.handle)
        self.assertNotIn(self.dialog, self.p.dirty)
        self.dialog_raw['children'][0]['text'] = 'Renamed target'
        self.answer(self.dialog_raw, task=task)
        self.assertNotIn(self.dialog, self.p.dirty)
        self.assertTrue(self.p.evaluate(self.predicate('Renamed target')))
        self.assertIn(self.handle, self.p.dirty)
        self.assertEqual(self.p.apps['101'].breaches, 0)

    def test_own_event_during_flight_stays_undecided_until_followup(self):
        task = self.p.request('snapshot', self.dialog)
        self.event(self.dialog)
        self.assertNotIn(self.handle, self.p.dirty)
        self.answer(self.dialog_raw, task=task)
        self.assertIn(self.dialog, self.p.dirty)
        self.assertIsNone(self.p.evaluate(self.predicate()))
        self.now += .041
        self.p.tick()
        self.answer(self.dialog_raw)
        self.assertTrue(self.p.evaluate(self.predicate()))

    def test_app_only_event_dirties_both_windows_and_overlapping_reply(self):
        task = self.p.request('snapshot', self.dialog)
        self.event()
        self.assertTrue({self.handle, self.dialog} <= self.p.dirty)
        self.answer(self.dialog_raw, task=task)
        self.assertIn(self.dialog, self.p.dirty)
        self.assertIsNone(self.p.evaluate(self.predicate()))

    def test_global_event_dirties_other_app_too_and_overlapping_reply(self):
        self.w.cache.upsert(12, dict(self.w.cache.windows[10], xid=12, handle='other-app'))
        other = self.w.cache.handle(12)
        self.w.x.window_pids[12] = 202
        self.p.tick()
        self.answer(h.fixture())
        task = self.p.request('snapshot', self.dialog)
        self.event(global_invalidation=True)
        self.assertTrue({self.handle, self.dialog, other} <= self.p.dirty)
        self.answer(self.dialog_raw, task=task)
        self.assertIsNone(self.p.evaluate(self.predicate()))

    def test_child_reported_overlap_keeps_target_dirty_without_worker_event(self):
        task = self.p.request('snapshot', self.dialog)
        self.answer(dict(self.dialog_raw, dirty=True), task=task)
        self.assertIn(self.dialog, self.p.dirty)
        self.assertIsNone(self.p.evaluate(self.predicate()))

    def inventory_query(self):
        self.p.raw[self.dialog]['rendered_only'] = True
        root = next(r for r, n in self.p.cache.windows[self.dialog]['nodes'].items()
                    if n['key'] == 'dialog')
        return dict(kind='item_present', window=self.dialog, container=root,
                    name='Rename', role='push_button')

    def inventory(self):
        return dict(items=copy.deepcopy(self.p.cache.windows[self.dialog]['enumeration']))

    def test_sibling_event_during_inventory_flight_preserves_dialog_inventory(self):
        query = self.inventory_query()
        self.assertIsNone(self.p.evaluate(query))
        task = next(iter(self.p.pending.values()))
        self.assertEqual(task['op'], 'enumerate')
        self.event(self.handle)
        self.answer(self.inventory(), task=task)
        self.assertTrue(self.p.evaluate(query))

    def test_own_event_during_inventory_flight_requires_new_inventory_after_snapshot(self):
        query = self.inventory_query()
        self.assertIsNone(self.p.evaluate(query))
        task = next(iter(self.p.pending.values()))
        self.event(self.dialog)
        self.answer(self.inventory(), task=task)
        self.now += .041
        self.p.tick()
        self.answer(dict(self.dialog_raw, rendered_only=True))
        self.assertIsNone(self.p.evaluate(query))
        self.assertEqual(next(iter(self.p.pending.values()))['op'], 'enumerate')
        self.answer(self.inventory())
        self.assertTrue(self.p.evaluate(query))

    def test_child_dirty_inventory_is_not_used_as_verification(self):
        query = self.inventory_query()
        self.assertIsNone(self.p.evaluate(query))
        self.answer(dict(self.inventory(), dirty=True))
        self.assertIsNone(self.p.evaluate(query))

    def test_name_step_verifies_with_continuously_rebuilding_600_node_sibling(self):
        self.dialog_raw['children'].extend(h.node('label'+str(i), 'label', 'Label '+str(i))
                                          for i in range(4))
        main = h.node(children=[h.node('item'+str(i), 'icon', 'Item '+str(i))
                                for i in range(600)])
        self.p.cache.update(self.handle, main, self.w.cache.rev)
        self.assertTrue(self.p.classify(self.dialog, 'rename', initial='Folder', remember=True))
        landed, builds = [], []
        def effect():
            if not landed and self.t.log.count(('key_down', 38)) >= 3:
                landed.append(self.now)
                self.dialog_raw['children'][0]['text'] = 'aa'
                self.event(self.handle)
                self.event(self.dialog)
        self.t.hook = effect
        step = dict(id='name', target=dict(window=self.dialog, ref=self.entry),
                    require=dict(modal_role='rename'),
                    intent=dict(kind='text', text='aa', replace=True), timeout_ms=3500)
        self.e.dialog_initial = 'Folder'
        self.submit(self.params([step], base_rev=self.w.cache.rev))
        for _ in range(4000):
            if landed:
                self.event(self.handle)
            self.tick()
            for task in list(self.p.pending.values()):
                # Main walks take 120 ms; the eight-node dialog takes 8 ms.
                delay = .120 if task['window'] == self.handle else .008
                if self.now - task['started'] >= delay:
                    builds.append(task['window'])
                    self.answer(main if task['window'] == self.handle else self.dialog_raw,
                                task=task, max_call_ms=10, breach=False)
            if self.replies:
                break
        self.assertTrue(landed, 'physical input must land before semantic verification')
        result = self.result()
        self.assertEqual(result['status'], 'success', result)
        self.assertEqual(result['steps'][0]['effect']['status'], 'verified')
        self.assertEqual(result['steps'][0]['expect_source'], 'default')
        self.assertLess(self.now - landed[0], 3.5)
        self.assertIn(self.handle, builds)
        self.assertIn(self.dialog, builds)
        self.assertEqual(self.p.apps['101'].breaches, 0)


class ChildWindowFreshness(unittest.TestCase):
    def setUp(self):
        states = ['showing', 'visible', 'enabled', 'sensitive']
        self.a = h.FakeAccessible('/a-entry', role='text', states=states)
        self.b = h.FakeAccessible('/b-entry', role='text', states=states)
        self.a.text, self.b.text = 'Main', 'Folder'
        self.frames = [h.FakeAccessible('/a-frame', children=[self.a], states=states,
                                         bounds=(0, 0, 600, 400)),
                       h.FakeAccessible('/b-frame', children=[self.b], states=states,
                                         bounds=(100, 100, 100, 50))]
        app = h.FakeAccessible('/app', children=self.frames)
        self.backend = GuardBackend(h.fake_atspi([app]))
        self.tasks = [dict(id=i+1, op='snapshot', app='42', window=window, pid=42,
                           rect=dict(zip(('x', 'y', 'w', 'h'), frame.bounds)),
                           window_events=True)
                      for i, (window, frame) in enumerate(zip(('A', 'B'), self.frames))]
        self.results = queue.Queue()
        for task in self.tasks:
            self.backend.snapshot(task)

    def event(self, source, **fields):
        self.backend.changed(NS(source=source, type='object:text-changed', **fields), self.results)

    def rows(self):
        rows = []
        while not self.results.empty():
            rows.append(self.results.get_nowait())
        return rows

    def test_cached_identity_attributes_window_without_callback_binding_calls(self):
        self.event(NS(app=self.a.app, path=self.a.path))  # a fresh proxy, same identity
        self.assertEqual(self.rows(), [dict(event='changed', app='42', window='A')])
        self.assertEqual(self.backend.forbidden, [])

    def test_sibling_events_inside_dialog_walk_do_not_mark_result_dirty(self):
        original = self.b.getState
        def state():
            for _ in range(100):
                self.event(self.a)
            return original()
        self.b.getState = state
        self.assertFalse(self.backend.snapshot(self.tasks[1])['dirty'])
        self.assertTrue(all(r.get('window') == 'A' for r in self.rows()))
        self.assertEqual(self.backend.forbidden, [])

    def test_own_event_inside_walk_keeps_result_dirty(self):
        original = self.b.getState
        def state():
            self.event(self.b)
            return original()
        self.b.getState = state
        self.assertTrue(self.backend.snapshot(self.tasks[1])['dirty'])
        self.assertEqual(self.rows(), [dict(event='changed', app='42', window='B')])

    def test_unknown_object_with_known_app_retains_app_only_fallback(self):
        self.event(NS(app=self.a.app, path='/not-walked'))
        self.assertEqual(self.rows(), [dict(event='changed', app='42')])

    def test_event_during_frame_matching_is_not_hidden_by_late_version_capture(self):
        original = self.frames[1].queryComponent
        def component():
            self.event(self.b)
            return original()
        self.frames[1].queryComponent = component
        self.assertTrue(self.backend.snapshot(self.tasks[1])['dirty'])

    def test_removed_objects_in_two_windows_invalidate_both_and_retire_identity(self):
        self.backend.changed(NS(source=self.a, type='object:children-changed:remove',
                                any_data=self.b), self.results)
        self.assertCountEqual(self.rows(), [dict(event='changed', app='42', window='A'),
                              dict(event='changed', app='42', window='B', removed=':1.42:/b-entry')])
        self.assertEqual(self.backend.forbidden, [])

    def test_replaced_window_mapping_drops_old_identity(self):
        self.frames[1].children = [h.FakeAccessible('/replacement', states=['showing', 'visible'])]
        self.backend.snapshot(self.tasks[1])
        self.event(self.b)
        self.assertEqual(self.rows(), [dict(event='changed', app='42')])

    def test_ambiguous_cached_window_identity_uses_app_fallback(self):
        self.frames[1].children.append(self.a)
        self.backend.snapshot(self.tasks[1])
        self.event(self.a)
        self.assertEqual(self.rows(), [dict(event='changed', app='42')])

    def test_first_walk_can_attribute_own_reentrant_event(self):
        self.backend = GuardBackend(self.backend.a)
        original = self.b.getState
        def state():
            self.event(self.b)
            return original()
        self.b.getState = state
        self.assertTrue(self.backend.snapshot(self.tasks[1])['dirty'])
        self.assertEqual(self.rows(), [dict(event='changed', app='42', window='B')])
        self.assertEqual(self.backend.forbidden, [])

    def test_inventory_overlap_uses_own_window_counter(self):
        original = self.b.clearCache
        for source, dirty in ((self.a, False), (self.b, True)):
            with self.subTest(source=source.path):
                def clear():
                    self.event(source)
                    return original()
                self.b.clearCache = clear
                raw = self.backend.enumerate(dict(self.tasks[1], key=':1.42:/b-frame'))
                self.assertEqual(raw.get('dirty'), dirty)
        self.assertEqual(self.backend.forbidden, [])

    def test_global_overlap_during_inventory_is_reported_dirty(self):
        original = self.b.clearCache
        def clear():
            self.event(NS(app=None))
            return original()
        self.b.clearCache = clear
        raw = self.backend.enumerate(dict(self.tasks[1], key=':1.42:/b-frame'))
        self.assertTrue(raw.get('dirty'))
        self.assertTrue(self.results.empty())  # coalesced notification has not flushed


if __name__ == '__main__':
    unittest.main()
