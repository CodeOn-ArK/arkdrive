#!/usr/bin/python3
"""F8 invalidation signals: fake accessibles/IPC, explicit time, no live bus."""
import io
import queue
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

import test_worker_w4 as h
import worker_atspi as atspi


class Signals(unittest.TestCase):
    def setUp(self):
        h.BackendContracts.setUp(self)
        self.b.snapshot(self.task)
        self.results = queue.Queue()
        self.now = 100.0
        self.clock = patch('worker_atspi.time.monotonic', side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def rows(self):
        rows = []
        while not self.results.empty():
            rows.append(self.results.get_nowait())
        return rows

    def assert_global(self):
        self.assertEqual(self.rows(), [])
        self.now += .041
        self.b.flush_events(self.results)
        self.assertEqual(self.rows(), [dict(event='changed', global_invalidation=True)])
        self.assertEqual(self.b.versions, {})

    def test_missing_or_unknown_source_bus_retains_invalidation_without_ancestry(self):
        for app in (None, NS(bus_name=None), NS(bus_name=':unmatched')):
            with self.subTest(app=app):
                source = NS(app=app, parent=NS(app=None, parent=self.obj))
                before = self.b.global_version
                self.b.changed(NS(source=source, type='object:text-changed:insert'), self.results)
                self.assertEqual(self.b.global_version, before + 1)
                self.assert_global()
                self.assertEqual(self.b.event_drops, 0)

    def test_application_bus_on_ancestor_uses_global_fallback(self):
        source = NS(app=None, parent=NS(app=None, bus_name=':1.42'))
        self.b.changed(NS(source=source, type='object:state-changed:focused'), self.results)
        self.assert_global()
        self.assertEqual(self.b.event_drops, 0)

    def test_broken_app_accessor_retains_global_invalidation(self):
        class Source:
            parent = self.obj

            @property
            def app(self):
                raise RuntimeError('unavailable app metadata')
        self.b.changed(NS(source=Source(), type='object:text-changed'), self.results)
        self.assert_global()
        self.assertEqual(self.b.event_drops, 0)

    def test_unresolved_flood_has_one_global_signal_per_fixed_debounce_window(self):
        for batch in range(2):
            before = self.b.global_version
            for i in range(1000):
                self.b.changed(NS(source=NS(app=None), type='object:text-changed'), self.results)
            self.assertEqual(self.b.event_drops, 0)
            self.assertEqual(self.b.global_version, before + 1000)
            self.b.flush_events(self.results)
            self.assertEqual(self.rows(), [])
            # Further events must not slide the first event's flush deadline.
            self.now += .039
            self.b.changed(NS(source=NS(app=None), type='object:visible-data-changed'), self.results)
            self.now += .002
            self.b.flush_events(self.results)
            self.assertEqual(self.rows(), [dict(event='changed', global_invalidation=True)])
            self.b.flush_events(self.results)
            self.assertEqual(self.rows(), [])
            self.assertEqual(self.b.global_version, before + 1001)

    def test_parent_cycles_and_unbounded_chains_are_never_walked(self):
        calls = []
        class Endless:
            app = None

            @property
            def parent(self):
                calls.append(True)
                if len(calls) > 128:
                    raise AssertionError('ancestry traversal must be bounded')
                return Endless()
        cycle = NS(app=None)
        cycle.parent = cycle
        for source in (cycle, Endless()):
            self.b.changed(NS(source=source, type='object:text-changed'), self.results)
        self.assertEqual(self.b.event_drops, 0)
        self.assertEqual(calls, [])
        self.assert_global()

    def test_global_event_overlapping_snapshot_marks_it_dirty(self):
        original = self.obj.getState
        def changed():
            self.b.changed(NS(source=NS(app=None), type='object:text-changed'), self.results)
            return original()
        self.obj.getState = changed
        self.assertTrue(self.b.snapshot(self.task)['dirty'])
        self.assertEqual(self.b.event_drops, 0)

    def test_removed_child_without_local_bus_retains_global_invalidation(self):
        removed = NS(app=None, parent=self.obj, path='/gone')
        with patch('sys.stderr', new_callable=io.StringIO) as log:
            self.b.changed(NS(source=NS(app=None, parent=self.obj),
                              type='object:children-changed:remove', any_data=removed), self.results)
        self.assert_global()
        self.assertEqual(self.b.event_drops, 1)  # Missing removal identity, invalidation retained.
        self.assertIn('object:children-changed:remove', log.getvalue())

    def test_removed_child_can_attribute_an_unresolved_source(self):
        self.b.changed(NS(source=NS(app=None), type='object:children-changed:remove',
                          any_data=self.obj), self.results)
        self.assertEqual(self.rows(), [dict(event='changed', app='42', removed=':1.42:/button')])
        self.assertEqual(self.b.versions.get('42'), 1)
        self.assertEqual(self.b.event_drops, 0)

    def test_unresolved_removed_child_preserves_source_signal_and_global_fallback(self):
        with patch('sys.stderr', new_callable=io.StringIO) as log:
            for _ in range(1000):
                self.b.changed(NS(source=self.obj, type='object:children-changed:remove',
                                  any_data=NS(app=None, path='/gone')), self.results)
        self.assertEqual(len(self.rows()), 1000)  # Normal attributed signals are unchanged.
        self.assertIn('object:children-changed:remove', log.getvalue())
        self.now += .041
        self.b.flush_events(self.results)
        self.assertEqual(self.rows(), [dict(event='changed', global_invalidation=True)])

    def test_listener_fault_still_emits_global_invalidation(self):
        with patch.object(self.b, '_changed', side_effect=RuntimeError('logic fault')), \
             patch('sys.stderr', new_callable=io.StringIO) as log:
            self.b.changed(NS(source=self.obj, type='object:text-changed'), self.results)
        self.assertIn('worker_fault', log.getvalue())
        self.assertIn('object:text-changed', log.getvalue())
        self.now += .041
        self.b.flush_events(self.results)
        self.assertEqual(self.rows(), [dict(event='changed', global_invalidation=True)])

    def test_removed_child_with_different_known_app_invalidates_both_apps(self):
        self.b.bus_apps[':1.other'] = 'other'
        removed = NS(app=NS(bus_name=':1.other'), path='/gone')
        self.b.changed(NS(source=self.obj, type='object:children-changed:remove',
                          any_data=removed), self.results)
        self.assertCountEqual(self.rows(), [dict(event='changed', app='42'),
                              dict(event='changed', app='other', removed=':1.other:/gone')])
        self.assertEqual(self.b.versions, {'42': 1, 'other': 1})
        self.assertEqual(self.b.event_drops, 0)

    def test_source_less_drop_logs_type_reason_and_retains_rate_limit(self):
        with patch('sys.stderr', new_callable=io.StringIO) as log:
            for _ in range(1000):
                self.b.changed(NS(source=None, type='object:text-caret-moved'), self.results)
            self.assertEqual(len(log.getvalue().splitlines()), 1)
            self.assertIn('object:text-caret-moved', log.getvalue())
            self.assertIn('no source', log.getvalue())
            self.now += 5.001
            self.b.changed(NS(source=None, type='window:activate'), self.results)
            self.assertIn('window:activate', log.getvalue())
            self.assertIn('total=1001', log.getvalue())
        self.assertEqual(self.b.event_drops, 1001)
        self.assertEqual(self.rows(), [])

    def test_registered_callback_skips_ancestry_and_pump_flushes_global_signal(self):
        wire, callbacks, listeners, entries = h.FakeWire(), [], [], []
        obj = self.obj
        class Source:
            app = None

            @property
            def parent(self):
                entries.append('parent')
                return obj
        def run():
            listeners[0](NS(source=Source(), type='object:text-changed'))
            listeners[0](NS(source=NS(app=None), type='object:text-changed'))
            listeners[0](NS(source=obj, type='object:text-changed'))
            self.now += .041
            callbacks[0]()
        api = NS(Registry=NS(registerEventListener=lambda cb, *args: listeners.append(cb)), setTimeout=Mock())
        glib = NS(MainLoop=lambda: NS(run=run), timeout_add=lambda ms, cb: callbacks.append(cb))
        with patch.dict('sys.modules', {'gi': NS(require_version=Mock()), 'pyatspi': api,
                                      'gi.repository': NS(GLib=glib)}), \
             patch.dict('os.environ', AT_SPI_BUS_ADDRESS='fake', DBUS_SESSION_BUS_ADDRESS='fake'), \
             patch('worker_atspi.socket.socket'), patch('worker_atspi.Wire', return_value=wire), \
             patch('worker_atspi.Backend', return_value=self.b), \
             patch('worker_atspi.threading.Thread', side_effect=AssertionError('no binding threads')):
            atspi.main(123)
        self.assertEqual(entries, [])
        self.assertIn(dict(event='changed', app='42'), wire.sent)
        self.assertIn(dict(event='changed', global_invalidation=True), wire.sent)


class Verification(h.ProviderHarness):
    def setUp(self):
        super().setUp()
        self.b, self.events = atspi.Backend(h.fake_atspi()), queue.Queue()
        self.b.bus_apps[':1.101'] = '101'
        self.clock = patch('worker_atspi.time.monotonic', side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def tick(self):
        self.b.flush_events(self.events)
        while not self.events.empty():
            self.wire.incoming.append(self.events.get_nowait())
        super().tick()

    def test_global_flood_rebuilds_both_matched_apps_once_and_observe_is_current(self):
        self.later()
        other = self.w.cache.handle(11)
        self.w.x.window_pids[11] = 202
        self.p.tick()
        self.answer(h.fixture(), task=next(t for t in self.p.pending.values() if t['window'] == other))
        self.b.bus_apps[':1.202'] = '202'
        before = len(self.wire.sent)
        for _ in range(1000):
            self.b.changed(NS(source=NS(app=NS(bus_name=':unknown')), type='object:text-changed'), self.events)
        self.now += .041
        self.tick()
        due = self.p.dirty_due[self.handle]
        self.assertEqual(self.p.serials['101'], 1)
        self.assertEqual(self.p.serials['202'], 1)
        self.assertEqual(self.p.dirty_due[other], due)
        self.assertEqual(len(self.wire.sent), before)
        self.now = due + .001
        self.tick()
        self.assertEqual(len(self.wire.sent), before + 2)
        self.raw['children'][0]['text'] = 'new text'
        for task in list(self.p.pending.values()):
            self.answer(self.raw, task=task)
        for _ in range(100):
            self.tick()
        self.assertEqual(len(self.wire.sent), before + 2)
        self.assertFalse(self.p.dirty)
        observed = self.w.observation(dict(channels=['tree'], scope=dict(kind='window', window=self.handle)))
        self.assertIn('text="new text"', '\n'.join(observed['tree']['lines']))

    def test_later_global_signal_overlapping_flight_preserves_followup_rebuild(self):
        event = NS(source=NS(app=None), type='object:text-changed')
        self.b.changed(event, self.events)
        self.now += .041
        self.tick()
        self.now += .041
        self.tick()
        task = next(iter(self.p.pending.values()))
        self.b.changed(event, self.events)
        self.now += .041
        self.tick()
        self.assertEqual(self.p.serials['101'], 2)
        self.answer(self.raw, task=task)
        self.assertIn(self.handle, self.p.dirty)
        self.assertFalse(self.p.pending)
        self.now += .041
        self.tick()
        self.assertEqual(len(self.p.pending), 1)
        self.raw['children'][0]['text'] = 'latest'
        self.answer(self.raw)
        self.assertNotIn(self.handle, self.p.dirty)
        for _ in range(80):
            self.tick()
        self.assertFalse(self.p.pending)

    def test_app_less_removal_schedules_global_rebuild_and_removed_ref_is_retired(self):
        parent = NS(app=NS(bus_name=':1.101'))
        ref = self.refs()['entry']
        with patch('sys.stderr', new_callable=io.StringIO):
            self.b.changed(NS(source=NS(app=None, parent=parent),
                              type='object:children-changed:remove',
                              any_data=NS(app=None, parent=parent, path='/entry')), self.events)
        self.now += .041
        self.tick()
        self.assertEqual(self.b.global_version, 1)
        self.assertIn(self.handle, self.p.dirty)
        self.now += .041
        self.tick()
        self.assertEqual(len(self.p.pending), 1)
        self.assertEqual(next(iter(self.p.pending.values()))['op'], 'snapshot')
        self.raw['children'] = [n for n in self.raw['children'] if n['key'] != 'entry']
        self.answer(self.raw)
        self.assertNotIn(ref, self.p.cache.windows[self.handle]['nodes'])

    def text_verifies(self, known_parent):
        landed = []
        parent = NS(app=NS(bus_name=':1.101')) if known_parent else None
        def effect():
            if not landed and self.t.log.count(('key_down', 38)) >= 2:
                landed.append(self.now)
                self.raw['children'][0]['text'] = 'a'
                for _ in range(1000):
                    self.b.changed(NS(source=NS(app=None, parent=parent),
                                      type='object:text-changed:insert'), self.events)
        self.t.hook = effect
        step = dict(target=dict(window=self.handle, ref=self.refs()['entry']),
                    intent=dict(kind='text', text='a', replace=True), timeout_ms=350)
        self.submit(self.params([step]))
        builds = 0
        for _ in range(600):
            self.tick()
            for task in list(self.p.pending.values()):
                if task['op'] == 'snapshot':
                    builds += 1
                    self.answer(self.raw, task=task)
            if self.replies:
                break
        self.assertTrue(landed)
        result = self.result()
        self.assertEqual(result['status'], 'success', result)
        self.assertEqual(result['steps'][0]['effect']['status'], 'verified')
        self.assertEqual(result['steps'][0]['expect_source'], 'default')
        self.assertLess(self.now - landed[0], .35)
        self.assertEqual(builds, 1)
        self.assertEqual(self.b.event_drops, 0)
        self.assertEqual(self.p.apps['101'].breaches, 0)

    def test_computed_text_verifies_with_only_app_less_events_despite_known_parent(self):
        self.text_verifies(known_parent=True)

    def test_computed_text_verifies_with_only_global_fallback_events(self):
        self.text_verifies(known_parent=False)


if __name__ == '__main__':
    unittest.main()
