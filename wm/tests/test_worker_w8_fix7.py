"""Control-page preemption with asynchronous prefix acknowledgments, no X server."""
import copy
from collections import Counter
import unittest
from unittest.mock import patch

from test_worker_w2 import Harness, w, GEN
import test_worker_w6 as physical_tests


class DispatchPreemption(Harness):
    def setUp(self):
        super().setUp()
        # Use the real control reader/epoch transition, rather than changing
        # cached state from the transport callback.
        self.control = self.path / 'control'
        self.control.write_bytes(w.encode_control(GEN))
        self.w.control_file = self.control.open('rb')
        self.addCleanup(self.w.control_file.close)
        info = self.control.stat()
        self.w.control_identity = info.st_dev, info.st_ino
        self.w.control_raw = self.control.read_bytes()
        self.w.control_due = 0
        self.w.poll_control = lambda force=False: w.Worker.poll_control(self.w, force)
        self.prefix = []
        self.text = ''

    def transition(self, state, epoch):
        self.control.write_bytes(w.encode_control(GEN, epoch=epoch, state=w.STATES[state]))
        self.w.poll_control(force=True)

    def interrupted_text(self, *, delayed_ack=True, confirmed=True, prefix_ack=True,
                         racing_read=False):
        self.t.ack = not delayed_ack
        self.t.release_ack = confirmed
        original_write, original_flush = self.t.write, self.t.flush
        paused = False
        delivered = False
        raced = False
        pread = w.os.pread

        def read(fd, size, offset):
            nonlocal raced
            if paused and racing_read and not raced:
                raced = True
                return w.encode_control(GEN)  # Pause lands between the reads.
            return pread(fd, size, offset)

        def write(event):
            nonlocal paused
            # The independent fixture records a character on its key press.
            if not paused:
                self.prefix.append(event)
                if event == ('key_down', 38):
                    self.text += 'A'
            original_write(event)
            if len(self.prefix) == 6 and not paused:
                paused = True
                self.control.write_bytes(w.encode_control(GEN, epoch=2, state=w.STATES['paused']))

        def flush():
            nonlocal delivered
            # Prefix XI2 events arrive on the other connection only during
            # cleanup. Release acknowledgments follow them in server order.
            if delayed_ack and any(ev[0] == 'key_up' for ev in self.t.buffer) and len(self.t.log) > len(self.prefix):
                if not delivered and prefix_ack:
                    for event in self.prefix:
                        self.t.emit(event)
                    delivered = True
                if confirmed:
                    for event in self.t.buffer:
                        self.t.emit(event)
            original_flush()

        self.t.write, self.t.flush = write, flush
        step = self.key(intent=dict(kind='text', text='A' * 64, via='keys'))
        with patch.object(w.os, 'pread', side_effect=read):
            result = self.run_action(self.params([step, self.move(300)]))
        self.t.write, self.t.flush = original_write, original_flush
        self.t.ack = True
        return result

    def assert_preempted(self, result):
        step, suffix = result['steps']
        self.assertEqual(result['reason'], 'preempted')
        self.assertEqual(step['reason'], 'preempted')
        self.assertEqual(step['dispatch_status'], 'accepted')
        self.assertNotEqual(result['status'], 'unknown')
        attempt, = step['attempts']
        self.assertEqual(attempt['written'], len(self.prefix))
        self.assertEqual(attempt['written_prefix'], {'events': len(self.prefix)})
        self.assertLess(attempt['written'], attempt['expected'])
        self.assertEqual(attempt['server_ack']['seen'], len(self.prefix))
        self.assertEqual(attempt['server_ack']['expected'], len(self.prefix))
        self.assertEqual(suffix['reason'], 'not_reached')
        self.assertFalse(suffix['attempts'])
        self.assertTrue(step['cleanup']['confirmed'])
        self.assertFalse(step['cleanup']['unreleased'])
        self.assertFalse(self.t.down)
        balance = Counter()
        for kind, code, *rest in self.t.log:
            if kind in ('key_down', 'key_up'):
                balance[code] += 1 if kind == 'key_down' else -1
        self.assertTrue(all(value == 0 for value in balance.values()))
        self.assertTrue(self.text)
        self.assertTrue(('A' * 64).startswith(self.text))
        self.assertLess(len(self.text), 64)
        self.assertFalse(any(event[0] == 'motion' for event in self.t.log))
        self.assertIsNone(self.e.fence_reason)
        self.assertFalse(any(row['type'] == 'fence' for row in self.rows()))
        self.assertEqual(self.e.unknown_streak, 0)
        self.assertFalse(result['needs_human'])
        self.assertEqual(self.e.get(GEN, 'a')['receipt'], result)

    def test_pause_mid_send_delayed_prefix_ack_confirmed_cleanup_no_f2(self):
        # A prior unknown must not turn a known, worker-stopped prefix into F2.
        self.e.unknown_streak = 1
        self.assert_preempted(self.interrupted_text())

    def test_pause_mid_send_immediate_ack_keeps_partial_accounting(self):
        self.assert_preempted(self.interrupted_text(delayed_ack=False))

    def test_pause_between_control_reads_is_preemption_not_worker_fault_f4(self):
        self.assert_preempted(self.interrupted_text(racing_read=True))

    def test_malformed_control_mid_send_still_latches_f4(self):
        self.t.on_write = lambda event: self.control.write_bytes(b'bad') if len(self.t.log) == 1 else None
        result = self.run_action(self.params([self.key(intent=dict(kind='text', text='A' * 64, via='keys'))]))
        self.assertEqual(result['reason'], 'worker_fault')
        self.assertEqual(self.e.fence_reason, 'F4:control_unavailable')
        self.assertTrue(result['steps'][0]['cleanup']['confirmed'])

    def test_preempted_prefix_does_not_count_as_complete_route_rejection_miss(self):
        context = self.e.route_context
        keys = []
        def prior_miss(step, handle):
            key, routes = context(step, handle)
            keys.append(key)
            self.e.routes.entries[key]['states']['xtest']['misses'] = 1
            return key, routes
        self.e.route_context = prior_miss
        result = self.interrupted_text(racing_read=True)
        self.e.route_context = context
        self.assert_preempted(result)
        self.assertEqual(self.e.routes.state(keys[0], 'xtest'), 'ok')
        self.assertEqual(self.e.routes.entries[keys[0]]['states']['xtest']['misses'], 0)

    def test_resume_attach_full_observe_then_followup_act_succeeds(self):
        self.e.unknown_streak = 1
        result = self.interrupted_text(racing_read=True)
        self.assert_preempted(result)
        original = copy.deepcopy(result)
        self.assertIsNone(self.w.lease)
        self.transition('agent', 2)  # Pause already advanced epoch; no clear bit.
        self.w.lease = 'fresh'
        self.w.lease_peer = self.peer
        self.e.attach('fresh', self.peer, {})
        self.assertEqual(self.e.reconciliation_reason(), 'awaiting_reconciliation')
        self.w.cache.commit()
        self.e.observed(self.peer, {'lease': 'fresh', 'scope': {'kind': 'session'}},
                        {'full': True, 'settled': True, 'rev': self.w.cache.rev})
        self.assertFalse(self.e.pending_effects)
        self.assertIsNone(self.e.fence_reason)
        self.t.on_write = lambda event: self.later() if event == ('key_up', 38) else None
        followup = self.run_action(self.params([
            self.key(intent=dict(kind='text', text='a', via='keys'))],
            action_id='followup', lease='fresh', control_epoch=2, base_rev=self.w.cache.rev))
        self.assertEqual(followup['status'], 'success')
        self.assertIsNone(followup['reason'])
        self.assertEqual(self.e.get(GEN, 'a')['receipt'], original)
        self.assertFalse(any(row['type'] == 'fence_clear' for row in self.rows()))

    def test_pause_during_wait_keeps_clean_preemption(self):
        self.submit(self.params([dict(intent={'kind': 'wait'}, timeout_ms=50,
                                     expect={'kind': 'window_present', 'match': {'title': 'Never'}}),
                                 self.move(300)]))
        self.tick()
        self.assertEqual(self.e.active['receipt']['phase'], 'verify')
        self.transition('paused', 2)
        self.drain()
        result = self.result()
        self.assertEqual((result['status'], result['reason']), ('failed', 'preempted'))
        self.assertEqual(result['steps'][1]['reason'], 'not_reached')
        self.assertFalse(self.t.log)
        self.assertIsNone(self.e.fence_reason)

    def test_pause_with_real_unconfirmed_release_still_latches_f1(self):
        result = self.interrupted_text(delayed_ack=False, confirmed=False)
        self.assertEqual(self.e.fence_reason, 'F1:release_unconfirmed')
        self.assertFalse(result['steps'][0]['cleanup']['confirmed'])
        self.assertTrue(result['needs_human'])

    def test_real_channel_failure_preserves_unknown_extent(self):
        def broken(event):
            if event[0] == 'key_down':
                raise OSError('genuine channel failure after write')
        self.t.on_write = broken
        result = self.run_action(self.params([self.key(), self.move(300)]))
        self.assertEqual(result['reason'], 'channel_down')
        self.assertEqual(result['dispatch_status'], 'unknown')
        self.assertEqual(result['steps'][0]['attempts'][0]['error'], 'genuine channel failure after write')
        self.assertEqual(result['steps'][1]['reason'], 'not_reached')
        self.assertTrue(result['steps'][0]['cleanup']['confirmed'])

    def test_missing_prefix_evidence_remains_unknown_despite_confirmed_release(self):
        result = self.interrupted_text(prefix_ack=False)
        self.assertEqual(result['reason'], 'preempted')
        self.assertEqual(result['dispatch_status'], 'unknown')
        self.assertTrue(result['steps'][0]['cleanup']['confirmed'])
        self.assertTrue(result['pending_effects'])


class HelperPreemption(unittest.TestCase):
    def setUp(self):
        self.h = physical_tests.EngineW6()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def pause_text(self):
        h = self.h
        def pause(event):
            if len(h.t.log) == 6:
                previous = h.w.cache.control
                h.w.cache.control = {'state': 'paused', 'epoch': 2}
                h.e.control_changed(previous, h.w.cache.control)
        h.t.on_write = pause
        return h.run_action(h.params([
            h.key(intent=dict(kind='text', text='A' * 64, via='keys'), timeout_ms=500),
            h.move(300)]))

    def test_helper_gate_closed_with_written_prefix_is_preempted_and_stays_up(self):
        result = self.pause_text()
        h = self.h
        step, suffix = result['steps']
        self.assertEqual(result['reason'], 'preempted')
        self.assertEqual(step['reason'], 'preempted')
        self.assertEqual(step['dispatch_status'], 'accepted')
        self.assertEqual(step['attempts'][0]['frames_written'], 6)
        self.assertEqual(step['attempts'][0]['written_prefix'], {'frames': 6})
        self.assertEqual(step['attempts'][0]['error'], 'gate_closed')
        self.assertTrue(step['cleanup']['confirmed'])
        self.assertFalse(h.t.down)
        self.assertFalse(any(h.e.holds['evdev'].values()))
        self.assertEqual(suffix['reason'], 'not_reached')
        self.assertIsNone(h.e.fence_reason)
        self.assertEqual(h.evdev.health['state'], 'up')
        self.assertEqual(h.evdev.reconnects, 0)
        self.assertEqual(sum('frames' in req for req in h.log), 1)

    def test_helper_pause_resume_observe_followup_uses_same_channel(self):
        self.pause_text()
        h = self.h
        previous = h.w.cache.control
        h.w.cache.control = {'state': 'agent', 'epoch': 2}
        h.e.control_changed(previous, h.w.cache.control)
        h.w.lease, h.w.lease_peer = 'fresh', h.peer
        h.e.attach('fresh', h.peer, {})
        h.w.cache.commit()
        h.e.observed(h.peer, {'lease': 'fresh'},
                     {'full': True, 'settled': True, 'rev': h.w.cache.rev})
        h.t.on_write = None
        result = h.run_action(h.params(action_id='followup', lease='fresh',
                                       control_epoch=2, base_rev=h.w.cache.rev))
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['steps'][0]['route'], 'evdev')
        self.assertIsNone(h.e.fence_reason)

    def test_helper_pause_cannot_hide_unconfirmed_release(self):
        h = self.h
        h.t.keymap = lambda cb: cb({50, 38})
        result = self.pause_text()
        self.assertEqual(h.e.fence_reason, 'F1:release_unconfirmed')
        self.assertFalse(result['steps'][0]['cleanup']['confirmed'])
        self.assertTrue(result['needs_human'])
