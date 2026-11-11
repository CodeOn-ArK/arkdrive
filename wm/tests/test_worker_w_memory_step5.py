"""Step 5 bounded backlog, streaming recovery and verified cleanup; no display."""
import errno
from contextlib import nullcontext
import gc
import json
import os
from pathlib import Path
import queue
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch
import weakref

from test_worker_w2 import Harness, w, GEN
import worker_atspi as atspi
from worker_housekeeping import cap_logs, reclaim_scratch, matches_instance
from test_worker_w6_fix1 import sessiond
import test_worker_w4 as tree_h


class ChildQueue(unittest.TestCase):
    def test_high_rate_flood_coalesces_then_overflows_to_global(self):
        for rate in (1000, 10000):
            backend, results = atspi.Backend(None), atspi.Results()
            backend.bus_apps[':1'] = 'app'
            source = NS(app=NS(bus_name=':1'), path='/button')
            backend.remember_window('w', ':1:/button')
            event = NS(source=source, type='object:text-changed:insert')
            with patch.object(atspi.time, 'monotonic', return_value=1):
                for _ in range(rate):
                    backend.changed(event, results)
                self.assertEqual(results.qsize(), 1)
                self.assertEqual(backend.window_versions['w'], rate)
                for i in range(rate):
                    source.path = f'/button{i}'
                    backend.remember_window(f'w{i}', f':1:/button{i}')
                    backend.changed(event, results)
                    self.assertLessEqual(results.qsize(), atspi.EVENT_LIMIT)
                    self.assertLessEqual(results.bytes, atspi.EVENT_BYTES)
            self.assertEqual(results.get_nowait(), dict(event='changed', global_invalidation=True))
            self.assertEqual(results.qsize(), 0)

    def test_byte_overflow_and_distinct_removals_are_global(self):
        for rows in ([dict(event='changed', app='a', removed='x'*atspi.EVENT_BYTES)],
                     [dict(event='changed', app='a', removed=p) for p in ('one', 'two')]):
            results = atspi.Results()
            for row in rows:
                results.put(row)
            self.assertEqual(results.get_nowait(), dict(event='changed', global_invalidation=True, removed_all=True))

    def test_job_barrier_survives_subsequent_flood(self):
        results = atspi.Results()
        results.put(dict(event='changed', app='before'))
        result = dict(id=1, result={'ok': True})
        results.put(result)
        for i in range(10000):
            results.put(dict(event='changed', app=str(i)))
        self.assertLessEqual(results.qsize(), atspi.EVENT_LIMIT+2)
        self.assertLessEqual(results.bytes+results.job_bytes, atspi.EVENT_BYTES+atspi.MAX_IPC+128)
        self.assertEqual(results.get_nowait()['app'], 'before')
        self.assertEqual(results.get_nowait(), result)
        self.assertEqual(results.get_nowait(), dict(event='changed', global_invalidation=True))
        with self.assertRaises(queue.Empty):
            results.get_nowait()

    def test_results_cannot_accumulate_multiple_job_payloads(self):
        results = atspi.Results()
        results.put(dict(id=1, result='x'))
        with self.assertRaises(ValueError):
            results.put(dict(id=2, result='y'))
        self.assertEqual(results.qsize(), 1)


class RemovalOverflow(tree_h.ProviderHarness):
    def test_overflow_removals_do_not_reuse_old_refs(self):
        refs = self.refs()
        results = atspi.Results()
        for path in ('first', 'second'):
            results.put(dict(event='changed', app='101', window=self.handle, removed=path))
        self.wire.incoming.append(results.get_nowait())
        self.p.tick()
        self.now += .041
        self.p.tick()
        self.answer(self.raw)
        self.assertFalse(set(refs.values()) & set(self.refs().values()))
        self.assertEqual(self.p.apps['101'].breaches, 0)


class PeerRetirement(Harness):
    def setUp(self):
        super().setUp()
        self.peer = w.Peer(Mock())
        self.w.peers = {self.peer}
        self.w.selector = Mock()
        self.w.lease_peer = self.peer
        self.e.attach('lease', self.peer, {'acts': 2})
        self.e.lease_state['lease'].update(observed=1)

    def test_grace_preserves_state_and_queued_acts_finish_before_retirement(self):
        peer = weakref.ref(self.peer)
        self.submit(self.params(action_id='a', deadline_ms=10000))
        self.submit(self.params(action_id='b', deadline_ms=10000), ident=2)
        state = self.e.lease_state['lease']
        state.update(suspended=True, done=True)
        self.peer.incoming.extend(b'x'*100000)
        self.peer.outgoing.extend(b'y'*100000)
        self.peer.waits.append({'kind': 'observe'})
        with patch.object(w.time, 'monotonic', side_effect=lambda: self.now):
            self.w.disconnect(self.peer)
            self.assertFalse(self.peer.incoming or self.peer.outgoing or self.peer.waits)
            self.now += 4.999
            self.w.expire_lease()
            self.assertEqual(self.w.lease, 'lease')
            self.now += .001
            self.w.expire_lease()
            self.assertIsNone(self.w.lease)
            self.assertIs(self.e.lease_state['lease'], state)
            self.assertTrue(state['done'] and state['suspended'])
            self.assertEqual(state['used'], 2)
            self.drain()
            self.e.retire_leases()
        self.assertNotIn('lease', self.e.lease_state)
        receipts = [r['receipt'] for r in self.rows() if r['type'] == 'receipt']
        self.assertEqual([r['action_id'] for r in receipts], ['a', 'b'])
        self.assertTrue(all(r['reason'] == 'preempted' for r in receipts))
        self.assertFalse(self.t.log)
        del self.peer, state
        gc.collect()
        self.assertIsNone(peer())

    def test_completed_peer_waits_for_grace_and_live_leases_stay(self):
        with patch.object(w.time, 'monotonic', side_effect=lambda: self.now):
            self.w.disconnect(self.peer)
            self.e.retire_leases()
            self.assertIn('lease', self.e.lease_state)
            self.now += 5
            self.w.expire_lease()
            self.assertFalse(self.e.lease_state)
        fresh = w.Peer(Mock())
        self.w.lease, self.w.lease_peer = 'fresh', fresh
        self.e.attach('fresh', fresh, {})
        self.e.retire_leases()
        self.assertIs(self.e.lease_state['fresh']['peer'], fresh)
        self.assertIsNot(fresh, self.peer)

    def test_retained_unresolved_action_does_not_keep_peer_buffers(self):
        self.run_action(self.params([self.key()]))
        self.assertTrue(self.e.actions)
        with patch.object(w.time, 'monotonic', side_effect=lambda: self.now):
            self.w.disconnect(self.peer)
            self.now += 5
            self.w.expire_lease()
        self.assertFalse(self.e.lease_state)
        self.assertTrue(self.e.pending_effects)
        self.assertTrue(all(a['peer'] is None for a in self.e.actions.values()))


    def test_receipt_peer_retires_even_when_lease_released_before_disconnect(self):
        self.run_action(self.params([self.key()]))
        self.w.release_lease()
        self.e.retire_leases()
        self.assertFalse(self.e.lease_state)
        with patch.object(w.time, 'monotonic', side_effect=lambda: self.now):
            self.w.disconnect(self.peer)
            self.now += 5
            self.e.retire_leases()
        self.assertTrue(all(a['peer'] is None for a in self.e.actions.values()))


class XBounds(unittest.TestCase):
    def test_shared_ceiling_counts_requests_detached_during_pump(self):
        budget = w.XBudget()
        budget.limit = 4
        connection = Mock()
        connection.pending_events.return_value = False
        first, second = w.AsyncX(connection, [], budget), w.AsyncX(connection, [], budget)
        requests = []
        def factory(**kw):
            req = NS(_data=None, _error=None)
            requests.append(req)
            return req
        for channel in (first, second, first, second):
            self.assertTrue(channel.ask(factory, lambda data: None))
        self.assertFalse(second.ask(factory, lambda data: None))
        self.assertEqual(len(requests), 4)
        self.assertEqual(budget.count, 4)
        # Callback enqueues against the same budget while the other requests
        # are held in pump's local list rather than channel.pending.
        first.pending[0] = (requests[0], lambda data: first.ask(factory, lambda d: None), False, None)
        requests[0]._data = {}
        first.pump()
        self.assertEqual(budget.count, 4)
        self.assertEqual(len(requests), 5)
        self.assertTrue(budget.overloaded)

    def test_full_x_budget_rejects_observe_without_adding_wait(self):
        owner = w.Worker.__new__(w.Worker)
        owner.x = NS(budget=w.XBudget())
        owner.x.budget.count = owner.x.budget.limit
        peer = w.Peer(Mock())
        replies = []
        owner.respond = lambda *args, **kw: replies.append(kw.get('exc'))
        owner.message(peer, json.dumps(dict(jsonrpc='2.0', id=1, method='window.observe', params={})).encode())
        self.assertEqual(replies[0].reason, 'queue_full')
        self.assertFalse(peer.waits)


    def test_rejected_refresh_does_not_accumulate_window_work(self):
        x = w.XState.__new__(w.XState)
        x.root, x.refreshing, x.ignored = NS(id=1), set(), set()
        x.budget = w.XBudget()
        x.budget.count = x.budget.limit
        for wid in range(2, 10002):
            x.refresh(wid)
        self.assertFalse(x.refreshing)
        self.assertTrue(x.budget.overloaded)

    def test_pressure_drain_forces_resync_and_cannot_claim_coverage(self):
        owner = w.Worker.__new__(w.Worker)
        budget = w.XBudget()
        budget.overloaded, budget.count = True, 1
        owner.x = NS(budget=budget, ax=NS(pump=lambda: [], pending=[]),
                     inp_ax=NS(pump=lambda: []), events=Mock(), refreshing={10},
                     again={10}, initialised=True, uncovered=set())
        owner.x_errors = []
        owner.poll_control = Mock()
        owner.cache = w.StateCache('s1', GEN)
        owner.prune_history = Mock()
        owner.log_due = float('inf')
        owner.watchdog = float('inf')
        owner.resync_baseline = ('old',)
        owner.ingest()
        self.assertFalse(owner.covered())
        budget.count = 0
        owner.ingest()
        self.assertFalse(budget.overloaded or owner.x.refreshing or owner.x.again)
        self.assertEqual(owner.watchdog, 0)
        self.assertIsNone(owner.resync_baseline)
        self.assertFalse(owner.covered())


class XActBound(Harness):
    def test_full_x_budget_uses_existing_queue_full_receipt_without_intent(self):
        self.w.x = NS(budget=w.XBudget())
        self.w.x.budget.count = self.w.x.budget.limit
        self.submit()
        self.assertEqual(self.result()['reason'], 'queue_full')
        self.assertFalse(self.rows() or self.e.queue or self.t.log)


class Recovery(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='memory-step5-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.journal = w.Journal(self.root / (GEN+'.jsonl'), 's1', GEN)
        self.addCleanup(self.journal.close)
        self.old = 'a'*32
        self.ts = '2026-01-01T00:00:00Z'
        self.now = 1800000000

    def row(self, kind, gen=None, **fields):
        return dict(ts=self.ts, type=kind, session='s1', generation=gen or self.old, **fields)

    def write(self, gen, rows):
        path = self.root / (gen+'.jsonl')
        with path.open('w') as stream:
            for row in rows:
                stream.write(json.dumps(row)+'\n')
        os.utime(path, (1, 1))
        return path

    def resolved(self, gen=None, n=1, pending=False, closed=True):
        gen = gen or self.old
        for i in range(n):
            action_id = str(i)
            yield self.row('intent', gen, action_id=action_id, request={'steps': []})
            effects = [dict(action_id=action_id, step_id='s', delivery_quiescent=False)] if pending else []
            yield self.row('receipt', gen, action_id=action_id, receipt=dict(pending_effects=effects))
        if closed:
            yield self.row('generation_closed', gen)

    def unpin_old(self):
        # 50 newer receipts from a newer generation, not evidence erasure.
        rows = list(self.resolved(GEN, n=50, closed=False))
        for row in rows:
            row['ts'] = '2026-02-01T00:00:00Z'
        self.write(GEN, rows)

    def test_large_generation_streams_and_drops_resolved_payloads(self):
        path = self.write(self.old, self.resolved(n=50000))
        self.assertGreater(path.stat().st_size, 10*1024*1024)
        self.assertEqual(self.journal.unresolved(), [])
        # Production recovery updates this proxy for every row, including
        # receipt processing, rather than inspecting only its final result.
        self.assertEqual(self.journal.recovery_peak_actions, 1)

    def test_closed_reconciled_old_archive_deletes_but_recent_does_not(self):
        old = self.write(self.old, self.resolved())
        self.unpin_old()
        self.assertEqual(self.journal.prune_history(now=self.now), [self.old])
        self.assertFalse(old.exists())
        old = self.write(self.old, self.resolved())
        self.assertEqual(self.journal.prune_history(now=1767225600+89*86400), [])
        self.assertTrue(old.exists())

    def test_unclean_unreconciled_and_missing_receipt_are_never_deleted(self):
        for rows in (list(self.resolved(closed=False)), list(self.resolved(pending=True)),
                     [self.row('intent', action_id='x', request={'steps': []}), self.row('generation_closed')]):
            path = self.write(self.old, rows)
            self.unpin_old()
            self.assertEqual(self.journal.prune_history(now=self.now), [])
            self.assertTrue(path.exists())

    def test_cross_generation_ack_and_order_are_streamed_and_durable(self):
        path = self.write(self.old, self.resolved(pending=True))
        self.unpin_old()
        ack = self.row('reconciled', GEN, action_id='0', action_generation=self.old, rev=2)
        with self.journal.path.open('a') as stream:
            stream.write(json.dumps(ack)+'\n')
        # Force acknowledgement file before the source in mtime order.
        os.utime(self.journal.path, (0, 0))
        self.assertEqual(self.journal.unresolved(), [])
        self.assertEqual(self.journal.prune_history(now=self.now), [self.old])
        self.assertFalse(path.exists())

    def test_ack_archive_is_preserved_when_unclean_source_must_remain(self):
        self.write(self.old, self.resolved(pending=True, closed=False))
        gen = 'b'*32
        self.write(gen, [self.row('reconciled', gen, action_id='0', action_generation=self.old),
                         self.row('generation_closed', gen)])
        self.unpin_old()
        self.assertEqual(self.journal.unresolved(), [])
        self.assertEqual(self.journal.prune_history(now=self.now), [])
        self.assertTrue((self.root / (gen+'.jsonl')).exists())
        self.assertEqual(self.journal.unresolved(), [])

    def test_last_fifty_receipts_protect_archive_and_corruption_blocks_cleanup(self):
        path = self.write(self.old, self.resolved())
        self.assertEqual(self.journal.prune_history(now=self.now), [])
        self.unpin_old()
        with path.open('ab') as stream:
            stream.write(b'{broken')
        with self.assertRaises(ValueError):
            self.journal.prune_history(now=self.now)
        self.assertTrue(path.exists())

    def test_new_instance_does_not_recover_prior_incarnations_effects(self):
        self.write(self.old, self.resolved(pending=True, closed=False))
        journal = w.Journal(self.root / ('c'*32+'.jsonl'), 's1', 'c'*32, instance='new')
        self.addCleanup(journal.close)
        self.assertEqual(journal.unresolved(), [])
        journal.append('worker_start')
        self.assertEqual(json.loads(journal.path.read_text())['instance'], 'new')


class CleanClosure(unittest.TestCase):
    def owner(self):
        owner = w.Worker.__new__(w.Worker)
        owner.openbox = None
        owner.stop_deadline = 1
        owner.stop_reason = 'requested'
        owner.journal = Mock()
        owner.socket_bound = False
        owner.published = True
        owner.sd = Path(__file__).resolve().parent / '.tmp/no-such-step5-cleanup'
        owner.control_page = Mock()
        owner.control_file = Mock()
        owner.act = NS(fence_reason=None, active=None, queue=[], actions={}, evdev=None)
        owner.x = Mock()
        owner.selector = Mock()
        owner.wake_r, owner.wake_w = 123, 124
        owner.ready_fd = None
        return owner

    def test_clean_marker_is_after_resource_cleanup_and_fsynced_before_close(self):
        owner = self.owner()
        order = []
        owner.x.close.side_effect = lambda: order.append('x_closed')
        owner.journal.append.side_effect = lambda kind, **kw: order.append(kind)
        owner.journal.close.side_effect = lambda: order.append('journal_closed')
        with patch.object(w.signal, 'set_wakeup_fd'), patch.object(w.os, 'close'):
            owner.cleanup(None)
        self.assertEqual(order, ['worker_stop', 'x_closed', 'generation_closed', 'journal_closed'])

    def test_failure_busy_or_fenced_cleanup_does_not_mark_closed(self):
        for kind in ('failure', 'busy', 'fenced', 'uncertain', 'cleanup_error'):
            owner = self.owner()
            if kind == 'busy':
                owner.act.queue.append({})
            elif kind == 'fenced':
                owner.act.fence_reason = 'F3:journal_unavailable'
            elif kind == 'uncertain':
                owner.act.actions['a'] = {'final': False}
            elif kind == 'cleanup_error':
                owner.x.close.side_effect = OSError('cleanup failed')
            with patch.object(w.signal, 'set_wakeup_fd'), patch.object(w.os, 'close'):
                if kind == 'cleanup_error':
                    with self.assertRaises(OSError):
                        owner.cleanup(None)
                else:
                    owner.cleanup('fault' if kind == 'failure' else None)
            self.assertEqual([call.args[0] for call in owner.journal.append.call_args_list], ['worker_stop'])
            owner.journal.close.assert_called_once()


class Housekeeping(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='memory-step5-clean-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / 'logs').mkdir()

    def test_log_caps_keep_append_inode_and_never_touch_journal(self):
        path = self.root / 'logs/worker.log'
        journal = self.root / 'journal.jsonl'
        journal.write_bytes(b'journal')
        with path.open('ab') as stream:
            stream.write(b'x'*1025)
            stream.flush()
            inode = path.stat().st_ino
            cap_logs(self.root, limit=1024)
            self.assertEqual(path.stat().st_size, 0)
            stream.write(b'after')
            stream.flush()
        self.assertEqual(path.stat().st_ino, inode)
        self.assertEqual(path.read_bytes(), b'after')
        self.assertEqual(journal.read_bytes(), b'journal')
        with patch('worker_housekeeping.os.ftruncate', side_effect=OSError(errno.ENOSPC, 'full')):
            cap_logs(self.root, limit=1)  # Diagnostics cannot throw over F3.

    def test_scratch_requires_matching_instance_and_preserves_home_and_evidence(self):
        record = dict(session_dir=str(self.root), instance='new')
        (self.root / '.instance').write_text('old\n')
        (self.root / 'fb').mkdir()
        (self.root / 'runtime').mkdir()
        (self.root / 'home').mkdir()
        (self.root / 'home/work').write_text('user work')
        (self.root / 'driver/frames').mkdir(parents=True)
        (self.root / 'driver/frames/evidence.png').write_bytes(b'evidence')
        (self.root / 'driver/act.sock').touch()
        reclaim_scratch(record)
        self.assertTrue((self.root / 'fb').exists())
        (self.root / '.instance').write_text('new\n')
        self.assertTrue(matches_instance(self.root, 'new'))
        reclaim_scratch(record)
        self.assertFalse((self.root / 'fb').exists())
        self.assertFalse((self.root / 'runtime').exists())
        self.assertFalse((self.root / 'logs').exists())
        self.assertFalse((self.root / 'driver/act.sock').exists())
        self.assertEqual((self.root / 'home/work').read_text(), 'user work')
        self.assertEqual((self.root / 'driver/frames/evidence.png').read_bytes(), b'evidence')

    def test_teardown_survivors_keep_registry_and_all_scratch(self):
        record = dict(session='s1', session_dir=str(self.root), profile='xvfb', pid_x=123, display=None)
        records = {'s1': record}
        with patch.object(sessiond, 'alive', return_value=True), \
                patch.object(sessiond, 'marked_pids', return_value=[123]), \
                patch.object(sessiond, 'send_signal'), patch.object(sessiond, 'wait_exit'), \
                patch.object(sessiond.time, 'sleep'), patch.object(sessiond, 'save_registry') as save, \
                patch.object(sessiond, 'reclaim_scratch') as reclaim:
            with self.assertRaises(sessiond.UnsafeTeardown):
                sessiond.teardown(record, records)
        save.assert_not_called()
        reclaim.assert_not_called()
        self.assertIn('s1', records)


    def test_successful_teardown_reclaims_only_after_death_proof(self):
        instance = 'a'*32
        (self.root / '.instance').write_text(instance)
        (self.root / 'fb').mkdir()
        record = dict(session='s1', session_dir=str(self.root), instance=instance,
                      profile='xvfb', pid_x=123, display=None)
        records = {'s1': record}
        def saved(current):
            self.assertFalse((self.root / 'fb').exists())
            self.assertEqual(current, {})
        with patch.object(sessiond, 'alive', return_value=False), \
                patch.object(sessiond, 'marked_pids', return_value=[]), \
                patch.object(sessiond, 'send_signal'), patch.object(sessiond, 'wait_exit'), \
                patch.object(sessiond.time, 'sleep'), patch.object(sessiond, 'save_registry', side_effect=saved):
            sessiond.teardown(record, records)
        self.assertFalse(records)

    def test_recycled_session_id_gets_new_scratch_and_process_identity(self):
        records = {}
        old = self.root / 'run/s1'
        old.mkdir(parents=True)
        (old / 'control').write_text('old incarnation')
        with patch.object(sessiond, 'STATE', self.root), \
                patch.object(sessiond, 'locked_registry', side_effect=lambda: nullcontext(records)), \
                patch.object(sessiond.os.path, 'lexists', return_value=False), \
                patch.object(sessiond, 'spawn', return_value=NS(pid=123)), \
                patch.object(sessiond.subprocess, 'Popen', return_value=NS(pid=124)), \
                patch.object(sessiond, 'wait_display'), patch.object(sessiond.time, 'sleep'), \
                patch.object(sessiond, 'save_registry'), patch.object(sessiond, 'journal'), \
                patch.object(sessiond, 'execute', side_effect=lambda r, cmd, **kw:
                             'unix:path=bus\n125\n' if cmd[0] == 'dbus-daemon' else "('unix:path=atspi',)"):
            self.assertEqual(sessiond.start_session('xvfb')[1], 0)
            first = dict(records['s1'])
            records.clear()  # Simulates an already verified stop's registry.
            self.assertEqual(sessiond.start_session('xvfb')[1], 0)
            second = records['s1']
        self.assertNotEqual(first['instance'], second['instance'])
        self.assertNotEqual(first['session_dir'], second['session_dir'])
        self.assertFalse((Path(second['session_dir']) / 'control').exists())
        self.assertEqual((old / 'control').read_text(), 'old incarnation')
        self.assertEqual(sessiond.session_env(second)['ARK_SESSION_INSTANCE'], second['instance'])
        self.assertTrue(matches_instance(second['session_dir'], second['instance']))


if __name__ == '__main__':
    unittest.main()
