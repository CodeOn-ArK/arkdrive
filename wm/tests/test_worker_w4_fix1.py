#!/usr/bin/python3
"""W4 fix round 1 regressions: deterministic fakes only; no X or bus."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

import test_worker_w4 as h
import worker_atspi as atspi
from worker_act import ActError, predicate_schema
from worker_tree import TreeCache


class BackendFixes(unittest.TestCase):
    def setUp(self):
        h.BackendContracts.setUp(self)

    def test_m1_live_role_vocabulary_passes_through_snapshot(self):
        roles = ['button', 'list box', 'list item', 'text', 'frame', 'filler', 'label']
        frame = self.b.a.Registry.getDesktop(0)[0].children[0]
        frame.children = [h.FakeAccessible('/'+str(i), role=r, states=self.obj.states)
                          for i, r in enumerate(roles)]
        raw = self.b.snapshot(self.task)
        self.assertEqual([n['role'] for n in raw['children']],
                         ['push_button', 'list_box', 'list_item', 'text', 'frame', 'filler', 'label'])
        cache = TreeCache()
        cache.update('w', raw)
        self.assertIn('push_button', cache.page('w')['lines'][1])
        self.assertNotIn('text', raw['children'][3])  # missing Text interface is harmless

    def test_m2_nested_dialog_scores_its_own_frame(self):
        frames = [dict(pid=42, bounds=[0, 0, 800, 600], name='Main'),
                  dict(pid=42, bounds=[200, 200, 300, 200], name='Rename')]
        rect = dict(x=200, y=200, w=300, h=200)
        self.assertIs(atspi.match_frame(frames, 42, rect, 'Rename'), frames[1])
        duplicate = dict(frames[1], name='Other')
        self.assertIs(atspi.match_frame([duplicate, frames[1]], 42, rect, 'Rename'), frames[1])
        self.assertIsNone(atspi.match_frame([frames[0]], 42, rect))

    def test_m2_known_pid_unmatched_never_borrows_bus_identity(self):
        raw = self.b.snapshot(dict(self.task, pid=999))
        self.assertEqual(raw['status'], 'unmatched')
        self.assertFalse(self.b.bus_apps)
        self.assertFalse(self.b.objects)

    def test_m3_many_short_calls_over_240ms_succeed_one_300ms_call_breaches(self):
        frame = self.b.a.Registry.getDesktop(0)[0].children[0]
        frame.children = [h.FakeAccessible('/'+str(i), role='button', states=self.obj.states)
                          for i in range(40)]
        now = [0.0]
        for child in frame.children:
            def small_call():
                now[0] += .01
            child.clearCache = small_call
        with patch('worker_atspi.time.monotonic', side_effect=lambda: now[0]):
            raw = self.b.snapshot(self.task)
        self.assertEqual(len(raw['children']), 40)
        self.assertGreater(raw['elapsed_ms'], 240)
        self.assertLess(raw['max_call_ms'], 200)
        self.assertFalse(raw['breach'])
        frame.children = [self.obj]
        def slow_call():
            now[0] += .3
        self.obj.clearCache = slow_call
        with patch('worker_atspi.time.monotonic', side_effect=lambda: now[0]):
            raw = self.b.snapshot(self.task)
        self.assertTrue(raw['breach'])
        self.assertAlmostEqual(raw['max_call_ms'], 300)

    def test_m3_render_walk_skips_hidden_descendants_inventory_walk_is_on_demand(self):
        hidden = h.FakeAccessible('/hidden', role='list box', states=['enabled'])
        leaf = h.FakeAccessible('/offscreen', role='list item', name='Offscreen')
        leaf.clearCache = Mock()
        hidden.children = [leaf]
        frame = self.b.a.Registry.getDesktop(0)[0].children[0]
        frame.children.append(hidden)
        raw = self.b.snapshot(self.task)
        self.assertEqual(raw['children'][1]['children'], [])
        leaf.clearCache.assert_not_called()
        inventory = self.b.enumerate(dict(self.task, key=':1.42:/frame'))
        self.assertIn('Offscreen', [n['name'] for n in inventory['items']])
        leaf.clearCache.assert_called_once()

    def test_m3_walk_has_separate_two_second_cooperative_budget(self):
        frame = self.b.a.Registry.getDesktop(0)[0].children[0]
        frame.children = [h.FakeAccessible('/'+str(i), states=self.obj.states) for i in range(30)]
        now = [0.0]
        for child in frame.children:
            child.clearCache = lambda: now.__setitem__(0, now[0]+.1)
        with patch('worker_atspi.time.monotonic', side_effect=lambda: now[0]), self.assertRaisesRegex(ValueError, 'tree_internal_budget'):
            self.b.snapshot(self.task)

    def child_setup(self):
        registry = Mock()
        api = NS(Registry=registry, setTimeout=Mock())
        glib = NS(MainLoop=lambda: NS(run=lambda: None), timeout_add=Mock())
        with patch.dict('sys.modules', {'gi': NS(require_version=Mock()), 'pyatspi': api,
                                        'gi.repository': NS(GLib=glib)}), \
             patch.dict('os.environ', AT_SPI_BUS_ADDRESS='fake', DBUS_SESSION_BUS_ADDRESS='fake'), \
             patch('worker_atspi.socket.socket'), patch('worker_atspi.Wire', return_value=h.FakeWire()):
            atspi.main(123)
        return api

    def test_m9_libatspi_timeouts_keep_per_call_and_allow_startup(self):
        self.child_setup().setTimeout.assert_called_once_with(250, 2000)

    def test_m11_additional_listener_classes_are_registered(self):
        api = self.child_setup()
        registered = api.Registry.registerEventListener.call_args.args[1:]
        for event in ('visible-data-changed', 'active-descendant-changed', 'row-inserted',
                      'row-deleted', 'column-inserted', 'column-deleted', 'model-changed'):
            self.assertIn('object:'+event, registered)

    def test_s2_text_interface_error_does_not_fail_snapshot(self):
        self.obj.role = 'text'
        self.obj.queryText = Mock(side_effect=RuntimeError('interface unavailable'))
        raw = self.b.snapshot(self.task)
        self.assertNotIn('text', raw['children'][0])
        self.assertEqual(len(raw['children']), 1)

    def test_s3_disabled_invoke_is_precondition_failed_with_no_action(self):
        self.b.snapshot(self.task)
        self.obj.states.discard('enabled')
        with self.assertRaisesRegex(ValueError, 'precondition_failed'):
            self.b.invoke(dict(self.task, key=':1.42:/button'))
        self.assertFalse(self.obj.calls)

    def test_m7_child_invoke_checks_identity_instead_of_unrelated_app_event(self):
        self.b.snapshot(self.task)
        self.b.versions['42'] = 10
        task = dict(self.task, key=':1.42:/button', role='push_button', name='Create Folder')
        with patch('worker_atspi.check_authority'):
            self.assertTrue(self.b.invoke(task)['accepted'])
        self.obj.name = 'Changed'
        with self.assertRaisesRegex(ValueError, 'stale_rev'):
            self.b.invoke(task)


class WorkerFixes(h.ProviderHarness):
    def test_m3_child_measurements_not_tick_cadence_drive_breaker(self):
        self.now += 2.1
        task = self.p.request('snapshot', self.handle)
        self.now += .8
        self.answer(self.raw, task=task, elapsed_ms=400, max_call_ms=10, breach=False)
        self.assertFalse(task['breached'])
        self.assertEqual(self.p.apps['101'].breaches, 0)
        self.assertEqual(self.p.probes[self.handle], 'usable')
        task = self.p.request('snapshot', self.handle)
        self.answer(self.raw, task=task, elapsed_ms=300, max_call_ms=300, breach=True)
        self.assertTrue(task['breached'])
        self.assertEqual(self.p.apps['101'].breaches, 1)
        self.assertFalse(self.p.healthy(self.handle))

    def test_m3_deterministic_budget_failure_preserves_reason_and_retries_snapshot(self):
        self.now += 2.1
        task = self.p.request('snapshot', self.handle)
        self.answer(error='tree_text_budget', task=task)
        b = self.p.apps['101']
        self.assertEqual((b.state, b.reason, b.breaches), ('degraded', 'tree_text_budget', 0))
        self.assertEqual(self.p.health()['atspi']['apps']['101']['reason'], 'tree_text_budget')
        self.assertFalse(self.p.pending)
        self.now = b.due + .001
        self.p.tick()
        task = next(iter(self.p.pending.values()))
        self.assertEqual(task['op'], 'snapshot')
        self.answer(self.raw, task=task)
        self.assertEqual(b.state, 'healthy')

    def test_m4_restart_after_publish_does_not_hold_worker_loop(self):
        self.p.down()
        self.now = self.p.restart_at
        self.p.start()
        self.assertFalse(self.p.ready)
        self.w.published, self.w.stopping = True, False
        self.w.selector = Mock()
        self.w.selector.select.side_effect = RuntimeError('held fake loop')
        self.w.ingest, self.w.pending = Mock(), Mock()
        self.w.openbox = None
        self.w.watchdog_finish, self.w.expire_lease, self.w.cleanup = Mock(), Mock(), Mock()
        self.e.tick = Mock(side_effect=RuntimeError('end fake loop'))
        x = Mock(initialised=False)
        with patch.object(h.w, 'TreeProvider', return_value=self.p), \
             patch.object(h.w, 'XState', return_value=x), patch.object(h.w.traceback, 'print_exc'):
            self.w.run()
        x.advance.assert_called_once()
        self.w.pending.assert_called_once()
        self.e.tick.assert_called_once()
        self.assertEqual(self.w.observation(dict(channels=['tree']))['tree']['status'], 'degraded')

    def test_m5_incremental_observe_neither_probes_nor_parks_on_provider(self):
        self.now += 10
        before = len(self.wire.sent)
        self.peer.waits = []
        params = dict(channels=['auto'], lease='lease', since_rev=self.w.cache.rev)
        with patch('worker_tree.Provider.busy', side_effect=AssertionError('provider busy path')):
            self.w.observe(self.peer, 5, params)
        self.assertFalse(self.peer.waits)
        self.assertTrue(self.result()['unchanged'])
        self.assertEqual(len(self.wire.sent), before)
        self.assertFalse(self.p.busy(params))

    def test_m5_due_health_probe_is_nonblocking_and_requires_a_live_consumer(self):
        self.now += 1.1
        self.p.tick()
        self.assertFalse(self.p.pending)
        self.w.observation(dict(channels=['tree']))
        task = next(iter(self.p.pending.values()))
        self.assertEqual(task['op'], 'probe')
        self.assertFalse(self.p.busy(dict(channels=['tree'])))
        self.answer(dict(name='Fixture'), task=task)
        self.w.lease = None
        self.now += 1.1
        self.p.tick()
        self.assertFalse(self.p.pending)

    def test_m6_no_change_event_keeps_rev_and_cursor_page_two(self):
        raw = h.node(children=[h.node(str(i), name=str(i)) for i in range(220)])
        self.change(raw)
        first = self.p.cache.page(self.handle)
        snap = self.p.cache.windows[self.handle]
        rev, facet, global_rev = snap['rev'], self.w.cache.facets['tree['+self.handle+']'], self.w.cache.rev
        self.change(raw)
        self.assertEqual(self.w.cache.rev, global_rev)
        self.assertEqual(self.p.cache.windows[self.handle]['rev'], rev)
        self.assertEqual(self.w.cache.facets['tree['+self.handle+']'], facet)
        self.assertEqual(len(self.p.cache.page(self.handle, cursor=first['cursor'])['lines']), 21)

    def test_m6_overlapping_event_accepts_snapshot_and_debounces_one_followup(self):
        old_rev = self.w.cache.rev
        self.p.dirty.add(self.handle)
        task = self.p.request('snapshot', self.handle)
        raw = copy.deepcopy(self.raw)
        raw['children'][1]['name'] = 'Changed'
        self.wire.incoming.extend([dict(event='changed', app='101'), dict(id=task['id'], result=raw)])
        self.p.tick()
        self.assertIn('Changed', self.p.cache.windows[self.handle]['lines'][-1])
        self.assertGreater(self.w.cache.rev, old_rev)
        self.assertIn(self.handle, self.p.dirty)
        self.assertFalse(self.p.pending)
        self.now += .039
        self.p.tick()
        self.assertFalse(self.p.pending)
        self.now += .002
        self.p.tick()
        self.assertEqual(len(self.p.pending), 1)
        self.answer(raw)
        self.assertNotIn(self.handle, self.p.dirty)
        self.p.tick()
        self.assertFalse(self.p.pending)

    def test_m6_cursor_pins_old_lines_for_60_seconds_after_changed_snapshot(self):
        raw = h.node(children=[h.node(str(i)) for i in range(220)])
        self.p.cache.update(self.handle, raw, 7)
        with patch('worker_tree.time.monotonic', return_value=10):
            first = self.p.cache.page(self.handle)
        self.p.cache.update(self.handle, self.raw, 8)
        with patch('worker_tree.time.monotonic', return_value=69):
            rest = self.p.cache.page(self.handle, cursor=first['cursor'])
        self.assertEqual((len(rest['lines']), rest['rev']), (21, 7))
        with patch('worker_tree.time.monotonic', return_value=70), self.assertRaises(ActError):
            self.p.cache.page(self.handle, cursor=first['cursor'])

    def test_m6_cursor_is_served_by_rpc_while_current_tree_is_dirty(self):
        self.change(h.node(children=[h.node(str(i)) for i in range(220)]))
        params = dict(channels=['tree'], scope=dict(kind='window', window=self.handle))
        first = self.w.observation(params)['tree']
        self.p.dirty.add(self.handle)
        self.assertEqual(len(self.w.observation(dict(params, cursor=first['cursor']))['tree']['lines']), 21)
        self.w.cache.remove(10)
        self.w.cache.commit()
        self.p.tick()
        self.assertEqual(len(self.w.observation(dict(params, cursor=first['cursor']))['tree']['lines']), 21)

    def test_m7_ref_valid_after_unrelated_same_app_event_and_real_change(self):
        ref, base = self.refs()['button'], self.w.cache.rev
        raw = copy.deepcopy(self.raw)
        raw['children'][0]['text'] = 'unrelated text update'
        self.change(raw)
        self.assertGreater(self.w.cache.facets['tree['+self.handle+']'], base)
        self.assertEqual(self.p.resolve(self.handle, ref, base)['key'], 'button')
        self.p.requirements(dict(ref=ref), self.handle, base)
        raw['children'][1]['name'] = 'Different button'
        self.change(raw)
        with self.assertRaises(ActError) as cm:
            self.p.resolve(self.handle, ref, base)
        self.assertEqual(cm.exception.detail, 'tree_identity')

    def test_m8_unmatched_window_recovers_without_geometry_or_bus_event(self):
        task = self.p.request('snapshot', self.handle)
        self.answer(dict(key='unmatched', role='frame', showing=False, visible=False, status='unmatched'), task=task)
        self.assertEqual(self.p.status(self.handle), 'unmatched')
        self.assertEqual(self.w.observation(dict(channels=['tree']))['tree']['status'], 'unmatched')
        self.assertFalse(self.p.pending)
        self.now += 1.001
        self.p.tick()
        self.assertEqual(next(iter(self.p.pending.values()))['op'], 'snapshot')
        self.answer(self.raw)
        self.assertEqual(self.p.status(self.handle), 'usable')

    def test_m9_ready_allowance_is_five_seconds(self):
        self.p.down()
        self.p.start()
        self.wire, self.child = self.children[-1]
        self.wire.incoming.clear()
        self.now += 4.999
        self.p.tick()
        self.assertIsNone(self.child.poll())
        self.now += .002
        self.p.tick()
        self.assertEqual(self.child.poll(), -9)

    def test_m9_first_two_seconds_do_not_count_breaches(self):
        task = self.p.request('snapshot', self.handle)
        self.answer(self.raw, task=task, breach=True, max_call_ms=300)
        self.assertEqual(self.p.apps['101'].breaches, 0)
        self.now += 2.001
        task = self.p.request('snapshot', self.handle)
        self.answer(self.raw, task=task, breach=True, max_call_ms=300)
        self.assertEqual(self.p.apps['101'].breaches, 1)

    def test_m10_flight_older_than_five_seconds_restarts_even_with_heartbeat(self):
        task = self.p.request('snapshot', self.handle)
        for age in (2.01, 2.5, 3, 3.5, 4, 4.5, 4.99):
            self.now = task['started'] + age
            self.wire.incoming.append(dict(event='pong'))
            self.p.tick()
            self.assertIsNone(self.child.poll())
        self.now = task['started'] + 5.001
        self.wire.incoming.append(dict(event='pong'))
        self.p.tick()
        self.assertEqual(self.child.poll(), -9)
        self.assertIsNone(self.p.wire)
        self.assertTrue(task['done'])

    def test_m12_undecided_before_dispatch_timeout_sends_no_input(self):
        s = self.key(expect=dict(kind='text_equals', ref=self.refs()['entry'], value='later'))
        self.p.dirty.add(self.handle)
        r = self.run_action(self.params([s, self.key()]))
        self.assertEqual(r['reason'], 'provider_degraded')
        self.assertEqual(r['steps'][0]['dispatch_status'], 'not_dispatched')
        self.assertEqual(r['steps'][1]['disposition'], 'unexecuted')
        self.assertFalse(self.t.log)

    def test_m12_undecided_then_true_rejects_already_satisfied(self):
        s = self.key(expect=dict(kind='text_equals', ref=self.refs()['entry'], value='ab'))
        self.p.dirty.add(self.handle)
        self.submit(self.params([s]))
        for _ in range(5):
            self.tick()
        self.assertFalse(self.t.log)
        self.answer(self.raw)
        self.drain()
        self.assertEqual(self.result()['reason'], 'invalid_expectation')
        self.assertEqual(self.result()['steps'][0]['detail'], 'precondition_already_satisfied')
        self.assertFalse(self.t.log)

    def test_m12_undecided_then_false_allows_dispatch(self):
        s = self.key(expect=dict(kind='text_equals', ref=self.refs()['entry'], value='later'))
        self.p.dirty.add(self.handle)
        self.submit(self.params([s]))
        for _ in range(5):
            self.tick()
        self.assertFalse(self.t.log)
        self.answer(self.raw)
        self.drain()
        self.assertTrue(any(e[0] == 'key_down' for e in self.t.log))

    def test_m13_node_state_rejects_unknown_or_nonboolean(self):
        for state, value in [('bogus', False), ('showing', False), ('enabled', 1), ('checked', 'false')]:
            with self.subTest(state=state, value=value), self.assertRaises(ActError) as cm:
                predicate_schema(dict(kind='node_state', ref=self.refs()['button'], state=state, value=value))
            self.assertEqual(cm.exception.reason, 'invalid_expectation')
        for state in ('selected', 'checked', 'expanded', 'focused', 'enabled', 'editable', 'disabled'):
            predicate_schema(dict(kind='node_state', ref=self.refs()['button'], state=state))

    def test_m14_health_and_observation_have_honest_status_and_app_fields(self):
        self.assertEqual(self.w.observation(dict(channels=['tree']))['tree']['status'], 'usable')
        app = self.p.health()['atspi']['apps']['101']
        self.assertEqual(app['pid'], 101)
        self.assertTrue({'pid', 'state', 'reason', 'breaches', 'next_probe_ms'} <= app.keys())
        self.assertNotEqual(self.w.channels()['atspi'].get('reason'), 'not_implemented')
        self.assertNotEqual(self.w.coverage()['tree'].get('reason'), 'not_implemented')
        self.assertEqual(self.w.coverage()['ocr']['reason'], 'not_implemented')
        self.p.down()
        self.assertEqual(self.w.observation(dict(channels=['tree']))['tree']['status'], 'degraded')

    def test_m14_single_unavailable_window_cannot_be_hidden_by_healthy_sibling(self):
        self.later()
        self.w.x.window_pids[11] = 101
        self.p.unavailable.add(self.handle)
        self.p.dirty_due[self.handle] = self.now + 1
        app = self.p.health()['atspi']['apps']['101']
        self.assertEqual(app['state'], 'degraded')
        self.assertEqual(app['reason'], 'application_unreachable')
        self.assertEqual(app['next_probe_ms'], 1000)
        self.assertEqual(len(app['windows']), 2)

    def test_m15_only_same_lease_served_baseline_gets_delta(self):
        full = self.w.observation(dict(channels=['tree']))
        self.w.observed(self.peer, dict(channels=['tree']), full)
        base = self.w.cache.rev
        raw = copy.deepcopy(self.raw)
        raw['children'][1]['name'] = 'Changed'
        self.change(raw)
        self.assertIn('delta', self.w.observation(dict(channels=['tree'], since_rev=base))['tree'])
        self.e.attach('other', self.peer, {})
        self.w.lease = 'other'
        self.assertTrue(self.w.observation(dict(channels=['tree'], since_rev=base))['tree']['full'])
        self.assertNotIn('delta', self.w.observation(dict(channels=['tree'], since_rev=base))['tree'])

    def test_m15_unread_continuation_is_not_a_delta_baseline(self):
        raw = h.node(children=[h.node(str(i)) for i in range(220)])
        self.change(raw)
        result = self.w.observation(dict(channels=['tree']))
        self.w.observed(self.peer, dict(channels=['tree']), result)
        self.assertNotIn(self.handle, self.e.lease_state['lease'].get('trees', {}))
        cursor = result['tree']['cursor']
        tail = self.w.observation(dict(channels=['tree'], cursor=cursor))
        self.w.observed(self.peer, dict(channels=['tree'], cursor=cursor), tail)
        self.assertEqual(self.e.lease_state['lease']['trees'][self.handle], tail['tree']['rev'])

    def test_m16_observe_budget_limits_nodes_and_serialized_bytes(self):
        raw = h.node(children=[h.node(str(i), name='x'*100) for i in range(230)])
        self.change(raw)
        tree = self.w.observation(dict(channels=['tree'], budget=dict(nodes=3, bytes=1000)))['tree']
        self.assertEqual(len(tree['lines']), 3)
        self.assertLessEqual(len(json.dumps(tree, ensure_ascii=False, separators=(',', ':')).encode()), 1000)
        self.assertTrue(tree['truncated'])
        maximum = self.p.cache.page(self.handle, budget=dict(nodes=500, bytes=100000))
        self.assertLessEqual(len(maximum['lines']), 200)
        self.assertLessEqual(len(json.dumps(maximum, separators=(',', ':')).encode()), 16384)
        with self.assertRaises(ActError):
            self.p.cache.page(self.handle, budget=dict(nodes=0))

    def test_m16_large_delta_falls_back_to_budgeted_full_continuation(self):
        raw = h.node(children=[h.node(str(i)) for i in range(220)])
        self.p.cache.update(self.handle, raw, 3)
        page = self.p.cache.page(self.handle, since=1)
        self.assertEqual(len(page['lines']), 200)
        self.assertTrue(page['truncated'])
        self.assertEqual(len(self.p.cache.page(self.handle, cursor=page['cursor'])['lines']), 21)

    def test_m17_ref_absence_after_unmap_survives_pruning_but_not_restart(self):
        ref = self.refs()['entry']
        p = dict(kind='node_absent', ref=ref, window=self.handle)
        self.w.cache.remove(10)
        self.w.cache.set('focus', None)
        self.w.cache.commit()
        self.assertTrue(self.p.evaluate(p))
        self.p.tick()
        self.assertNotIn(ref, self.p.cache.ref_windows)
        self.assertTrue(self.p.evaluate(p))
        self.p.cache.restart()
        self.assertIsNot(self.p.evaluate(p), True)

    def test_m18_dump_rpc_writes_latest_raw_with_timing_and_cli_prints_path(self):
        task = self.p.request('snapshot', self.handle)
        self.answer(self.raw, task=task, elapsed_ms=350, max_call_ms=15, breach=False)
        msg = dict(jsonrpc='2.0', id=7, method='debug.tree_dump', params=dict(window=self.handle))
        self.w.message(self.peer, json.dumps(msg).encode())
        path = Path(self.result()['path'])
        self.assertEqual(path.parent, self.w.sd)
        self.assertEqual(path.name, 'tree-dump-'+self.handle.replace('/', '_')+'.json')
        raw = json.loads(path.read_text())
        self.assertEqual((raw['elapsed_ms'], raw['max_call_ms'], raw['breach']), (350, 15, False))
        self.assertEqual(raw['children'][0]['text'], 'ab')
        with patch.object(h.w.sys, 'argv', ['ark-worker', 'tree-dump', '--session', 's1', '--window', self.handle]), \
             patch.object(h.w, 'command', return_value=({'path': str(path)}, 0)), patch('builtins.print') as output:
            self.assertEqual(h.w.main(), 0)
        output.assert_called_once_with(str(path))

    def test_s1_ref_click_clips_to_window_and_rejects_empty_intersection(self):
        ref = self.refs()['button']
        node = self.p.cache.windows[self.handle]['nodes'][ref]
        node['bounds'] = [450, 100, 100, 100]
        step = dict(target=dict(window=self.handle, ref=ref), intent=dict(kind='click'), require={})
        self.assertEqual(self.e.resolve(step, self.w.cache.rev)[1], dict(x=475, y=150))
        node['bounds'] = [510, 100, 100, 100]
        with self.assertRaises(ActError) as cm:
            self.e.resolve(step, self.w.cache.rev)
        self.assertEqual(cm.exception.reason, 'target_not_found')

    def test_s4_tick_exception_is_logged_before_restart(self):
        self.wire.pump = Mock(side_effect=KeyError('logic defect sentinel'))
        self.p.tick()
        log = self.w.sd / 'logs/atspi-provider.log'
        self.assertIn('logic defect sentinel', log.read_text())
        self.assertEqual(self.child.poll(), -9)

    def test_s5_dead_window_bookkeeping_is_pruned(self):
        self.p.modals[self.handle] = {'entry': self.refs()['entry']}
        self.p.unavailable.add(self.handle)
        self.w.cache.remove(10)
        self.w.cache.commit()
        self.p.tick()
        for mapping in (self.p.modals, self.p.layouts, self.p.unavailable,
                        self.p.probes, self.p.raw, self.p.first_map):
            self.assertNotIn(self.handle, mapping)
        self.assertNotIn(self.handle, self.p.cache.ref_windows.values())

    def test_m3_item_predicates_request_complete_inventory_only_on_demand(self):
        self.p.raw[self.handle]['rendered_only'] = True
        predicate = dict(kind='item_absent', container=self.refs()['root'], name='Offscreen')
        self.assertIsNone(self.p.evaluate(predicate))
        task = next(iter(self.p.pending.values()))
        self.assertEqual(task['op'], 'enumerate')
        items = [dict(key='root', parent=None, role='frame', name='Fixture'),
                 dict(key='offscreen', parent='root', role='list_item', name='Offscreen')]
        self.answer(dict(items=items), task=task)
        self.assertIs(self.p.evaluate(predicate), False)
        self.assertTrue(self.p.ready)
        self.assertFalse(self.p.pending)


if __name__ == '__main__':
    unittest.main()
