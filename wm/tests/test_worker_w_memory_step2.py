"""Window retirement contracts, real provider/child code with fake GLib and X.

Never opens a display, session bus, listener, or input device.
"""
import copy
import queue
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

import test_worker_w4 as h
import test_worker_w4_fix3 as child_h
import worker_atspi as atspi


class ChildForget(unittest.TestCase):
    child = child_h.ChildPlumbing.child
    reply = child_h.ChildPlumbing.reply

    def setUp(self):
        h.BackendContracts.setUp(self)
        self.task['window_events'] = True

    def test_idempotent_forget_retires_objects_epochs_and_reverse_memberships(self):
        baseline = self.b.memory_counters()
        self.b.snapshot(self.task)
        self.b.versions['42'] = 8
        self.b.window_versions['w'] = 3
        self.assertEqual(self.b.memory_counters()['objects'], 2)
        self.assertEqual(self.b.forget_window(self.task), dict(forgotten=True))
        self.assertEqual(self.b.memory_counters(), baseline)
        self.assertEqual(self.b.forget_window(self.task), dict(forgotten=True))
        self.assertEqual(self.b.memory_counters(), baseline)
        self.assertIsNone(self.b.event_owner(self.obj))

    def test_sibling_and_shared_identity_memberships_survive(self):
        self.b.snapshot(self.task)
        self.b.snapshot(dict(self.task, window='sibling'))
        self.b.versions['42'] = 8
        self.b.window_versions.update(w=3, sibling=7)
        sibling = self.b.objects['sibling']
        self.b.forget_window(self.task)
        self.assertIs(self.b.objects['sibling'], sibling)
        self.assertEqual(self.b.versions, {'42': 8})
        self.assertEqual(self.b.window_versions, {'sibling': 7})
        self.assertEqual(self.b.bus_apps, {':1.42': '42'})
        self.assertEqual(self.b.bus_windows, {':1.42': {'sibling'}})
        self.assertTrue(all(windows == {'sibling'} for windows in self.b.event_windows.values()))
        self.assertEqual(self.b.event_window(self.obj, self.b.event_owner(self.obj)), 'sibling')
        self.b.forget_window(dict(self.task, window='sibling'))
        self.assertTrue(all(v == 0 for v in self.b.memory_counters().values()))

    def test_failed_walk_partial_memberships_are_forgotten(self):
        def fail():
            raise RuntimeError('binding failed halfway through walk')
        self.obj.getState = fail
        with self.assertRaises(RuntimeError):
            self.b.snapshot(self.task)
        self.assertTrue(self.b.window_keys)
        self.assertTrue(self.b.bus_apps)
        self.b.forget_window(self.task)
        self.assertTrue(all(v == 0 for v in self.b.memory_counters().values()))

    def test_enumeration_only_memberships_are_forgotten(self):
        self.b.snapshot(self.task)
        hidden = h.FakeAccessible('/offscreen', name='Hidden')
        self.obj.children.append(hidden)
        self.b.enumerate(dict(self.task, key=':1.42:/button'))
        self.assertIn(':1.42:/offscreen', self.b.event_windows)
        self.b.forget_window(self.task)
        self.assertTrue(all(v == 0 for v in self.b.memory_counters().values()))

    def test_200_distinct_apps_return_child_maps_to_baseline(self):
        baseline = self.b.memory_counters()
        for i in range(200):
            task = dict(self.task, window=f'w{i}', app=str(i))
            self.b.snapshot(task)
            self.b.forget_window(task)
            self.assertEqual(self.b.memory_counters(), baseline)

    def test_window_app_identity_change_does_not_leak_old_app_membership(self):
        self.b.snapshot(self.task)
        self.b.versions['42'] = 8
        changed = dict(self.task, app='resolved-pid')
        self.b.snapshot(changed)
        self.assertEqual(self.b.app_windows, {'resolved-pid': {'w'}})
        self.assertNotIn('42', self.b.versions)
        self.b.forget_window(changed)
        self.assertTrue(all(v == 0 for v in self.b.memory_counters().values()))

    def test_forget_queues_behind_walk_and_mutation_and_flushes_events_before_results(self):
        api = self.b.a
        api.setTimeout = Mock()
        api.Registry.registerEventListener = Mock()
        entered, release = threading.Event(), threading.Event()
        original = self.obj.getState
        def blocked():
            entered.set()
            if not release.wait(2):
                raise RuntimeError('test failed to release binding')
            # The debounce deadline is still ahead when the job returns.
            self.b.invalidate_global()
            return original()
        self.obj.getState = blocked
        with patch('worker_atspi.Backend', return_value=self.b), self.child(api) as (loop, wire):
            try:
                wire.incoming.put(dict(self.task, id=1))
                self.assertTrue(entered.wait(2))
                wire.incoming.put(dict(self.task, id=2, op='forget_window'))
                self.assertTrue(wire.outgoing.empty(), 'forget must wait for owner')
                release.set()
                rows = [wire.outgoing.get(timeout=2) for _ in range(3)]
                self.assertEqual(rows[0], dict(event='changed', global_invalidation=True))
                self.assertEqual([r['id'] for r in rows[1:]], [1, 2])
                self.assertTrue(rows[1]['result']['dirty'])
                self.assertEqual(rows[1]['memory']['objects'], 2)
                self.assertTrue(all(v == 0 for v in rows[2]['memory'].values()))
                wire.incoming.put(dict(self.task, id=3, op='forget_window'))
                again = self.reply(wire, [3])[3]
                self.assertEqual(again['result'], rows[2]['result'])
                self.assertEqual(again['memory'], rows[2]['memory'])

                # Forget behind an already queued mutation, same app and pump.
                self.obj.getState = original
                wire.incoming.put(dict(self.task, id=4))
                self.assertNotIn('error', self.reply(wire, [4])[4])
                def action():
                    def invoke(index):
                        self.b.invalidate_global()
                        return True
                    return NS(nActions=1, getName=lambda i: 'press', doAction=invoke)
                self.obj.queryAction = action
                with patch('worker_atspi.check_authority'):
                    wire.incoming.put(dict(self.task, id=5, op='invoke', key=':1.42:/button'))
                    wire.incoming.put(dict(self.task, id=6, op='forget_window'))
                    rows = [wire.outgoing.get(timeout=2) for _ in range(3)]
                self.assertEqual(rows[0], dict(event='changed', global_invalidation=True))
                self.assertEqual([r['id'] for r in rows[1:]], [5, 6])
                self.assertTrue(rows[1]['result']['accepted'])
                self.assertTrue(all(v == 0 for v in rows[2]['memory'].values()))
            finally:
                release.set()


class ParentPruning(h.ProviderHarness):
    def close(self, xid=10):
        self.w.cache.remove(xid)
        self.w.cache.commit()
        self.p.tick()

    def forget(self):
        return next(t for t in self.p.pending.values() if t['op'] == 'forget_window')

    def test_close_prunes_all_leases_and_late_cursor_delivery_cannot_restore_entries(self):
        self.later()
        sibling = self.w.cache.handle(11)
        self.e.attach('second-lease', NS(), {})
        for state in self.e.lease_state.values():
            for field in ('trees', 'tree_pages', 'ocr_trees'):
                state[field] = {self.handle: ('closed',), sibling: ('live',)}
        preserved = copy.deepcopy(self.e.lease_state)
        self.close()
        for token, state in self.e.lease_state.items():
            for field in ('trees', 'tree_pages', 'ocr_trees'):
                self.assertEqual(state[field], {sibling: ('live',)})
            for field in ('used', 'misses', 'suspended', 'observed'):
                self.assertEqual(state[field], preserved[token][field])
        self.e.observed(self.peer, {}, dict(tree=dict(window=self.handle, rev=5, full=True)))
        self.assertNotIn(self.handle, self.e.lease_state['lease']['trees'])

    def test_lease_pruning_works_without_tree_provider(self):
        self.w.tree = None
        state = self.e.lease_state['lease']
        state['ocr_trees'] = {self.handle: ('fallback',)}
        self.w.cache.windows[10]['mapped'] = False
        self.e.tick()
        self.assertEqual(state['ocr_trees'], {})

    def test_late_snapshot_never_republishes_and_app_retires_after_forget_ack(self):
        task = self.p.request('snapshot', self.handle)
        self.p.serials['101'] = 9
        self.p.samples['101'] = self.now
        self.close()
        forgotten = self.forget()
        self.assertGreater(forgotten['id'], task['id'])
        self.assertIn('101', self.p.apps, 'flight still owns app state')
        rev = self.w.cache.rev
        self.answer(h.fixture(), task=task)
        self.assertTrue(task['done'])
        self.assertIsNotNone(task['result'], 'act waiter retains its completed task')
        self.assertNotIn(self.handle, self.p.cache.windows)
        self.assertNotIn(self.handle, self.p.raw)
        self.assertNotIn(self.handle, self.p.probes)
        self.assertEqual(self.w.cache.rev, rev, 'late result cannot publish a tree revision')
        counts = atspi.Backend(h.fake_atspi()).memory_counters()
        self.answer(dict(forgotten=True), task=forgotten, memory=counts)
        self.assertEqual(self.w.memory_counters()['atspi_child'], counts)
        self.assertEqual(self.w.memory_counters()['atspi_apps'], 0)
        for mapping in (self.p.apps, self.p.samples, self.p.serials):
            self.assertEqual(mapping, {})
        # A late app-wide notification must not recreate a retired epoch.
        self.wire.incoming.append(dict(event='changed', app='101'))
        self.p.tick()
        self.assertEqual(self.p.serials, {})
        before = len(self.wire.sent)
        self.p.tick()
        self.assertEqual(len(self.wire.sent), before, 'forget is not reissued after ack')

    def test_sibling_inflight_job_and_breaker_are_unchanged(self):
        self.later()
        self.w.x.window_pids[11] = 101
        sibling = self.w.cache.handle(11)
        task = self.p.request('snapshot', sibling)
        breaker = self.p.apps['101']
        self.close()
        forgotten = self.forget()
        self.assertFalse(task['done'])
        self.answer(h.fixture(), task=task)
        snap = self.p.cache.windows[sibling]
        self.answer(dict(forgotten=True), task=forgotten)
        self.assertIs(self.p.apps['101'], breaker)
        self.assertEqual(breaker.breaches, 0)
        self.assertIs(self.p.cache.windows[sibling], snap)
        self.assertEqual(self.p.status(sibling), 'usable')
        self.assertEqual(self.p.resolve(sibling, next(iter(snap['nodes'])))['key'], 'root')

    def test_unmap_remap_cannot_publish_the_pre_unmap_flight(self):
        old = self.p.request('snapshot', self.handle)
        self.w.cache.windows[10]['mapped'] = False
        self.p.tick()
        forgotten = self.forget()
        self.w.cache.windows[10]['mapped'] = True
        self.answer(dict(h.fixture(), name='obsolete'), task=old)
        self.assertNotIn(self.handle, self.p.cache.windows)
        self.assertNotIn(self.handle, self.p.raw)
        self.assertTrue(self.p.observe_probe_ready(old))
        self.answer(dict(forgotten=True), task=forgotten)
        fresh = next(t for t in self.p.pending.values() if t['op'] == 'snapshot')
        self.assertGreater(fresh['id'], forgotten['id'])
        self.answer(h.fixture(), task=fresh)
        self.assertEqual(self.p.status(self.handle), 'usable')
        self.assertEqual(self.p.cache.windows[self.handle]['nodes'][self.refs()['root']]['name'], 'Fixture')

    def test_late_enumeration_or_error_cannot_restore_closed_window_state(self):
        for op, result, error in (
                ('enumerate', dict(items=[dict(key='root')]), None),
                ('probe', None, 'application_unreachable')):
            with self.subTest(op=op):
                # Each case uses a separate live window, keeping the fixture
                # app alive so health changes would be observable.
                self.later()
                sibling = self.w.cache.handle(11)
                self.w.x.window_pids[11] = 101
                task = self.p.request(op, sibling, key='root')
                self.close(11)
                forgotten = self.forget()
                breaker = copy.copy(self.p.apps['101'])
                self.answer(result, error=error, task=task)
                self.assertNotIn(sibling, self.p.unavailable)
                self.assertNotIn(sibling, self.p.dirty)
                self.assertFalse(any(k[0] == sibling for k in self.p.inventories))
                self.assertEqual(vars(self.p.apps['101']), vars(breaker))
                self.answer(dict(forgotten=True), task=forgotten)
                self.now += 2
                self.assertTrue(self.p.observe_probe_ready(task))
                self.assertNotIn(sibling, self.p.unavailable)
                self.assertNotIn(sibling, self.p.dirty)

    def test_forget_ack_is_not_an_app_liveness_probe(self):
        self.later()
        self.w.x.window_pids[11] = 101
        sibling = self.w.cache.handle(11)
        task = self.p.request('snapshot', sibling)
        self.answer(h.fixture(), task=task)
        self.close(11)
        forgotten = self.forget()
        self.now += 3
        self.p.first_map[self.handle] = self.now - 3
        self.assertIsNone(self.p.observe_probe(self.handle))
        self.assertFalse(forgotten['done'])
        self.assertEqual(self.p.apps['101'].breaches, 0)

    def test_inflight_act_on_live_window_survives_sibling_close(self):
        self.later()
        self.w.x.window_pids[11] = 101
        sibling = self.w.cache.handle(11)
        self.p.tick()
        self.answer(h.fixture())
        # Dispatch into the fake X acknowledgement wait, then retire sibling.
        self.t.ack = False
        self.submit(self.params([self.move()], base_rev=self.w.cache.rev))
        for _ in range(100):
            self.tick()
            if self.t.log:
                break
        self.assertIsNotNone(self.e.active, self.replies)
        self.assertTrue(self.t.log)
        action = self.e.active
        self.close(11)
        self.assertIs(self.e.active, action)
        self.assertNotIn(sibling, self.p.cache.windows)
        self.answer(dict(forgotten=True), task=self.forget())
        self.t.ack = True
        self.t.emit(('motion', 0, 20, 20))  # delayed acknowledgement of original write
        self.drain()
        self.assertEqual(self.result()['status'], 'success', self.result())

    def test_forget_retry_and_child_restart_discard_retirement_state(self):
        self.close()
        forgotten = self.forget()
        self.answer(error='worker_fault', task=forgotten)
        retry = self.forget()
        self.assertGreater(retry['id'], forgotten['id'])
        self.child.code = -1
        self.p.tick()
        self.assertTrue(retry['done'])
        self.assertIsNone(self.p.child_memory)
        self.assertFalse(self.p.forgetting)
        self.p.tick()
        self.assertFalse(self.p.apps)

    def test_closed_target_act_keeps_target_not_found(self):
        self.close()
        result = self.run_action(self.params([dict(target=self.handle,
            intent=dict(kind='key', keys=['F2']),
            expect=dict(kind='window_present', window=self.handle))]))
        self.assertEqual(result['reason'], 'target_not_found')
        self.assertEqual(self.t.log, [])

    def test_200_open_close_cycles_return_parent_and_real_backend_maps_to_baseline(self):
        # Real parent request/close/ack path, real Backend snapshot and forget;
        # only the GLib transport and accessible hierarchy are fakes.
        backend = atspi.Backend(h.fake_atspi())
        self.close()
        self.answer(dict(forgotten=True), task=self.forget())
        baseline = self.w.memory_counters()['atspi_apps']
        child_baseline = backend.memory_counters()
        template = self.w.cache.tombstones[self.handle]
        states = ['showing', 'visible', 'enabled', 'sensitive']
        for i in range(200):
            xid, pid = 20+i, 500+i
            frame = h.FakeAccessible('/frame', states=states,
                bounds=tuple(template['geometry'][k] for k in ('x', 'y', 'w', 'h')))
            frame.app.bus_name = f':1.{pid}'
            app = h.FakeAccessible('/app', children=[frame])
            app.pid = pid
            backend.a.Registry.getDesktop = lambda index, app=app: [app]
            self.w.cache.upsert(xid, dict(template, xid=xid))
            self.w.x.window_pids[xid] = pid
            self.w.cache.commit()
            self.p.tick()
            task = next(t for t in self.p.pending.values() if t['op'] == 'snapshot')
            request = next(row for row in reversed(self.wire.sent) if row.get('id') == task['id'])
            raw = backend.snapshot(request)
            self.assertNotEqual(raw.get('status'), 'unmatched')
            counts = backend.memory_counters()
            self.assertEqual((counts['windows'], counts['objects'], counts['bus_apps']), (1, 1, 1))
            self.answer(raw, task=task, memory=counts)
            self.close(xid)
            forgotten = self.forget()
            self.answer(backend.forget_window(forgotten), task=forgotten, memory=backend.memory_counters())
            self.assertEqual(self.w.memory_counters()['atspi_apps'], baseline)
            self.assertEqual(self.w.memory_counters()['atspi_child'], child_baseline)
        self.assertEqual(self.p.window_apps, {})
        self.assertEqual(self.p.pending, {})


class SnapshotRetention(unittest.TestCase):
    def test_rendered_inventory_is_absent_even_on_unchanged_snapshot(self):
        cache = h.TreeCache()
        raw = h.fixture()
        cache.update('w', raw, 1)
        self.assertIn('enumeration', cache.windows['w'])
        self.assertFalse(cache.update('w', dict(raw, rendered_only=True), 2))
        self.assertNotIn('enumeration', cache.windows['w'])
        cache.update('w', dict(h.node(), rendered_only=True), 3)
        self.assertNotIn('enumeration', cache.windows['w'])
        self.assertEqual(set(cache.windows['w']['previous']), {'refs', 'lines', 'rev'})
        # Complete fixtures still provide semantic item inventories.
        self.assertFalse(cache.update('w', h.node(), 4))
        self.assertEqual(cache.windows['w']['enumeration'][0]['key'], 'root')

    def test_slim_predecessor_keeps_delta_and_cursor_after_prune(self):
        cache = h.TreeCache()
        raw = h.node(children=[h.node(str(i), 'push_button', str(i)) for i in range(205)])
        cache.update('w', raw, 1)
        first = cache.page('w')
        frozen = cache.windows['w']
        expected = frozen['lines'][200:]
        raw['children'][0]['name'] = 'changed'
        raw['children'].pop()
        cache.update('w', raw, 2)
        previous = cache.windows['w']['previous']
        self.assertEqual(set(previous), {'refs', 'lines', 'rev'})
        delta = cache.page('w', since=1)['delta']
        self.assertEqual(len(delta['changed']), 1)
        self.assertEqual(delta['removed'], [list(frozen['nodes'])[-1]])
        cache.prune('w')
        continuation = cache.page('w', cursor=first['cursor'])
        self.assertEqual(continuation['lines'], expected)
        self.assertEqual(continuation['rev'], 1)
        self.assertFalse(continuation['truncated'])
        self.assertEqual(cache.identities, {})
        self.assertEqual(cache.ref_windows, {})


if __name__ == '__main__':
    unittest.main()
