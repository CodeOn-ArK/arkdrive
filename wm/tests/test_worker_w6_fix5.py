"""W6 helper-death recovery: fake devices/processes and framed memory streams.

No live sessions, X connections, uinput nodes or privileged operations.
"""
import copy
import errno
import mmap
import os
from pathlib import Path
import signal
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

import test_worker_w2 as base
import test_worker_w6 as w6
from test_worker_w6_fix1 import sessiond
from input_protocol import PAGE, control_snapshot
import mkdevs
from worker_evdev import EvdevTransport

GEN = w6.GEN
OLD, NEW = '1' * 32, '2' * 32


class Recovery(base.Harness):
    def setUp(self):
        super().setUp()
        self.w.record['profile'] = 'xorg'
        self.pin = mmap.mmap(-1, 32)
        self.addCleanup(self.pin.close)
        self.log, self.device_events, self.confirmations = [], [], []
        self.fixture_text = ''
        self.device_down = {'kbd': set(), 'ptr': set()}
        self.buffers = {'kbd': [], 'ptr': []}
        self.reverse = {30: 38, 42: 50, 48: 56, 60: 68}
        self.died, self.gone, self.respawn_at = False, False, 0
        self.respawn_delay = .06
        self.stale_metadata = False
        self.helper = self.new_helper(OLD)
        self.evdev = EvdevTransport(self.w, self.t, connector=self.connect,
            clock=lambda: self.now, keycodes={v: k for k, v in self.reverse.items()})
        self.evdev.health.update(state='up', grab=True)
        self.evdev.sources = {'keyboard': 9, 'pointer': 10}
        self.e.evdev = self.evdev
        self.addCleanup(self.evdev.close)
        original = self.t.keymap
        def query(cb):
            self.confirmations.append((set(self.t.down), copy.deepcopy(self.device_down)))
            original(cb)
        self.t.keymap = query

    def writer(self, dev, event):
        self.device_events.append((dev, event))
        typ, code, value = event
        if typ == 1:
            if value:
                self.device_down[dev].add(code)
            else:
                self.device_down[dev].discard(code)
            self.buffers[dev].append(event)
        if typ == 0:
            for _, code, value in self.buffers[dev]:
                kind = 'key' if dev == 'kbd' else 'button'
                detail = self.reverse[code] if dev == 'kbd' else {0x110: 1, 0x112: 2, 0x111: 3}[code]
                ev = (kind + ('_down' if value else '_up'), detail)
                self.t.write(ev)
                if ev[0] == 'key_down' and detail in (38, 56):
                    char = 'a' if detail == 38 else 'b'
                    self.fixture_text += char.upper() if 50 in self.t.down else char
                self.t.raw_seq += 1
                source = 9 if dev == 'kbd' else 10
                self.t.raw.append((self.t.raw_seq, *ev, source, source))
            self.buffers[dev].clear()

    def new_helper(self, instance):
        return mkdevs.EventProtocol({d: lambda ev, d=d: self.writer(d, ev) for d in ('kbd', 'ptr')},
            lambda gen: self.w.cache.control['epoch'] if self.w.cache.control['state'] == 'agent' else None,
            lambda dev: 'busy', lambda delay: None, generation_state=self.pin, instance=instance)

    def connect(self):
        if self.died:
            if self.gone or self.now < self.respawn_at:
                raise OSError(errno.ECONNREFUSED, 'helper not ready')
            if self.helper.instance == OLD:
                self.helper = self.new_helper(NEW)
        helper = self.helper
        self.evdev.instance = OLD if self.stale_metadata else helper.instance  # Atomic metadata seam.
        lost = [False]
        def handle(req):
            if 'frames' in req and not self.died:
                # Kill with shift and the first letter down. The dead child's held
                # set/cache are lost; only the pin and kernel device survive.
                prefix = dict(req, frames=req['frames'][:2])
                reply = helper.handle(prefix)
                self.assertTrue(reply['ok'])
                self.died, lost[0] = True, True
                self.respawn_at = self.now + self.respawn_delay
                return reply  # No reply reaches the worker.
            return helper.handle(req)
        sock = w6.MemorySocket(handle, self.log, fragment=65536)
        recv = sock.recv
        def receive(size):
            if lost[0]:
                raise OSError(errno.ECONNRESET, 'helper killed')
            return recv(size)
        sock.recv = receive
        return sock

    def killed_text(self):
        return self.run_action(self.params([self.key(intent={'kind': 'text', 'text': 'Aab', 'replace': False})]))

    def reconcile(self):
        self.later()
        self.w.observed(self.peer, {}, {'full': True, 'settled': True, 'rev': self.w.cache.rev})
        self.assertFalse(self.e.pending_effects)

    def test_mid_text_respawn_releases_and_next_xtest_act_succeeds(self):
        r = self.killed_text()
        self.assertEqual(r['steps'][0]['reason'], 'channel_down')
        self.assertEqual(r['steps'][0]['dispatch_status'], 'unknown')
        self.assertTrue(r['steps'][0]['cleanup']['confirmed'])
        self.assertIsNone(self.e.fence_reason)
        self.assertEqual(self.helper.instance, NEW)
        self.assertEqual(self.helper.generation, GEN)
        self.assertFalse(self.t.down | self.t.buttons)
        self.assertFalse(any(self.device_down.values()))
        self.assertTrue(self.confirmations)
        self.assertTrue(all(not keys and not any(devices.values()) for keys, devices in self.confirmations))
        self.assertEqual([ev for ev in self.t.log if ev[0].startswith('key')],
                         [('key_down', 50), ('key_down', 38), ('key_up', 38), ('key_up', 50), ('key_up', 56)])
        self.assertEqual(self.fixture_text, 'A')  # Strict prefix of intended Aab, no duplicates.
        self.assertEqual(r['steps'][0]['channel_probe'][0]['route'], 'xtest')
        self.assertEqual(self.w.channels()['evdev']['state'], 'down')
        self.assertNotIn('fenced', self.w.channels()['evdev'])
        self.assertNotIn('release_pending', self.w.channels()['evdev'])
        self.reconcile()
        following = self.run_action(self.params([self.move(30, 30)], action_id='following', base_rev=self.w.cache.rev))
        self.assertEqual(following['status'], 'success')
        self.assertEqual(following['steps'][0]['route'], 'xtest')

    def test_no_act_replay_and_fresh_release_sequence_across_respawn(self):
        self.killed_text()
        frames = [req for req in self.log if 'frames' in req]
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]['instance'], OLD)
        fresh = [req for req in self.log if req.get('instance') == NEW]
        self.assertEqual(len(fresh), 1)
        self.assertEqual((fresh[0]['op'], fresh[0]['seq']), ('release_held', 1))
        self.assertEqual(fresh[0]['holds'], {'kbd': [30, 42, 48], 'ptr': []})
        self.assertFalse(any('frames' in req for req in fresh))
        rows = [v for v in self.rows() if v['type'] == 'release' and v.get('route') == 'evdev']
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]['confirmed'])
        self.assertEqual(rows[0]['helper_instance'], NEW)

    def test_immediate_respawn_on_act_reconnect_refuses_replay_and_resets_cleanup_sequence(self):
        self.respawn_delay = 0
        self.test_no_act_replay_and_fresh_release_sequence_across_respawn()

    def test_stale_metadata_new_socket_refuses_old_act_token_without_any_writes(self):
        # Metadata may lag socket replacement. Even if the client cannot yet
        # see the new incarnation, the server must refuse the old token.
        self.respawn_delay = 0
        self.stale_metadata = True
        r = self.killed_text()
        self.assertEqual(self.e.fence_reason, 'F1:release_unconfirmed')
        self.assertFalse(r['steps'][0]['cleanup']['confirmed'])
        self.assertEqual(self.t.log, [('key_down', 50), ('key_down', 38)])
        self.assertEqual(self.helper.high_seq, -1)
        self.assertTrue(all(req.get('instance') == OLD for req in self.log))

    def test_device_gone_latches_f1_visible_and_blocks_following_input(self):
        self.gone = True
        r = self.killed_text()
        self.assertEqual(self.e.fence_reason, 'F1:release_unconfirmed')
        self.assertTrue(r['needs_human'])
        self.assertFalse(r['steps'][0]['cleanup']['confirmed'])
        self.assertEqual(self.w.channels()['evdev']['fenced'], 'F1:release_unconfirmed')
        self.assertEqual(self.t.down, {38, 50})
        self.assertEqual(self.device_down['kbd'], {30, 42})
        before = list(self.t.log)
        self.reconcile()
        blocked = self.run_action(self.params(action_id='blocked', base_rev=self.w.cache.rev))
        self.assertEqual(blocked['reason'], 'worker_fault')
        self.assertIsNone(blocked['journal_seq'])
        self.assertEqual(self.t.log, before)
        rows = [v for v in self.rows() if v['type'] == 'release' and v.get('route') == 'evdev']
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]['confirmed'])

    def test_successful_write_without_observation_confirmation_keeps_f1_and_operator_clear(self):
        self.t.release_ack = False  # Device consumes ups; X query still reports down.
        self.killed_text()
        self.assertEqual(self.e.fence_reason, 'F1:release_unconfirmed')
        self.assertFalse(any(self.device_down.values()))
        self.assertEqual(self.t.down, {38, 50})
        old = self.w.cache.control
        current = dict(state='agent', epoch=2, flags=1)
        self.e.control_changed(old, current)
        self.assertEqual(self.e.fence_reason, 'F1:release_unconfirmed')
        # Separate release attempt after the operator restores observation.
        self.t.release_ack = True
        self.w.cache.control = current
        runner = self.e.release_evdev({'params': {'action_id': 'operator_release'}}, {})
        self.run_generator(runner)
        self.assertFalse(self.t.down)
        self.assertTrue(self.confirmations[-1][0] == set())
        self.assertEqual(self.e.fence_reason, 'F1:release_unconfirmed')
        self.e.control_changed(current, dict(state='agent', epoch=3, flags=1))
        self.assertIsNone(self.e.fence_reason)
        self.assertNotIn('fenced', self.w.channels()['evdev'])
        self.assertTrue(any(row['type'] == 'fence_clear' for row in self.rows()))

    def run_generator(self, runner):
        for _ in range(10000):
            try:
                next(runner)
                self.now += .001
            except StopIteration as done:
                return done.value
        self.fail('cleanup did not finish')

    def test_unconfirmed_release_blocks_startup_xtest_diagnostic(self):
        self.gone = True
        self.killed_text()
        before = list(self.t.log)
        self.run_generator(self.e.channel_selftest())
        self.assertEqual(self.t.log, before)
        self.assertEqual(self.e.channel['state'], 'down')
        # A nominally available transport cannot bypass F1 through the evdev
        # diagnostic's special admission path either.
        self.evdev.health['state'] = 'up'
        runner = self.e.send_evdev({'params': {'action_id': 'blocked_diagnostic'}},
            {'id': 'selftest', 'attempts': [], 'rev': {'dispatched': None}}, [('key_down', 38)], diagnostic=True)
        with self.assertRaises(base.ActError):
            self.run_generator(runner)
        self.assertEqual(self.t.log, before)

    def test_health_exposes_release_pending_while_device_keys_are_down(self):
        self.submit(self.params([self.key(intent={'kind': 'text', 'text': 'Aab', 'replace': False})]))
        for _ in range(1000):
            self.tick()
            if self.w.channels()['evdev'].get('release_pending'):
                break
        self.assertTrue(self.w.channels()['evdev'].get('release_pending'))
        self.assertEqual(self.device_down['kbd'], {30, 42})
        self.assertEqual(self.t.down, {38, 50})
        self.drain()
        self.assertIsNone(self.e.fence_reason)

    def test_other_worker_generation_zero_write_uses_xtest_without_phantom_holds_or_f1(self):
        self.helper.generation = 'f' * 32
        self.evdev.instance = OLD
        self.evdev.connector = lambda: w6.MemorySocket(self.helper.handle, self.log, fragment=65536)
        r = self.killed_text()
        self.assertEqual([a['route'] for a in r['steps'][0]['attempts']], ['evdev', 'xtest'])
        self.assertEqual(r['steps'][0]['attempts'][0]['dispatch_status'], 'not_dispatched')
        self.assertEqual(r['steps'][0]['dispatch_status'], 'accepted')
        self.assertTrue(self.t.log)
        self.assertIsNone(self.e.fence_reason)
        self.assertFalse(any(self.e.holds['evdev'].values()))
        self.assertFalse(any(self.evdev.held_codes.values()))
        self.reconcile()
        following = self.run_action(self.params([self.move(30, 30)], action_id='following', base_rev=self.w.cache.rev))
        self.assertEqual(following['status'], 'success')
        self.assertEqual(following['steps'][0]['route'], 'xtest')


class Protocol(unittest.TestCase):
    def test_shared_pin_survives_child_death_but_dedupe_does_not(self):
        with mmap.mmap(-1, 32) as pin:
            writes = []
            def helper(instance):
                return mkdevs.EventProtocol({'kbd': writes.append, 'ptr': writes.append}, lambda gen: 1,
                    lambda dev: 'busy', generation_state=pin, instance=instance)
            first = helper(OLD)
            req = dict(w6.request(), instance=OLD)
            self.assertTrue(first.handle(req)['ok'])
            second = helper(NEW)
            self.assertEqual(second.generation, GEN)
            self.assertEqual((second.high_seq, second.replies), (-1, {}))
            count = len(writes)
            self.assertEqual(second.handle(req)['error'], 'helper_restarted')
            self.assertEqual(second.high_seq, -1)  # Old token cannot poison the new sequence space.
            other = dict(req, instance=NEW, generation='f' * 32)
            self.assertFalse(second.handle(other)['ok'])
            release = dict(seq=2, instance=NEW, op='release_held', generation=GEN, holds={'kbd': [30], 'ptr': []})
            self.assertTrue(second.handle(release)['ok'])
            self.assertEqual(writes[count:], [(1, 30, 0), (0, 0, 0)])
            self.assertTrue(second.handle(release)['ok'])
            self.assertEqual(len(writes), count + 2)  # Same-incarnation reply dedupe still holds.
            self.assertFalse(second.handle(dict(release, holds={'kbd': [42], 'ptr': []}))['ok'])

    def test_recovery_is_generation_pinned_release_only_and_gate_independent(self):
        writes = []
        helper = mkdevs.EventProtocol({'kbd': writes.append, 'ptr': writes.append}, lambda gen: None,
            Mock(side_effect=AssertionError('cleanup must not probe grab')), instance=NEW)
        helper.generation = GEN
        release = dict(seq=0, instance=NEW, op='release_held', generation=GEN, holds={'kbd': [30], 'ptr': [0x110]})
        self.assertTrue(helper.handle(release)['ok'])
        self.assertEqual(writes, [(1, 30, 0), (0, 0, 0), (1, 0x110, 0), (0, 0, 0)])
        count = len(writes)
        invalid = [dict(generation='f' * 32), dict(holds={'kbd': [99], 'ptr': []}),
                   dict(holds={'kbd': [True], 'ptr': []}), dict(holds={'kbd': [30], 'ptr': [1]}),
                   dict(holds={'kbd': [[1, 30, 1]], 'ptr': []}), dict(extra='bad')]
        for seq, fields in enumerate(invalid, 1):
            self.assertFalse(helper.handle(dict(release, **dict(seq=seq, **fields)))['ok'])
        self.assertEqual(len(writes), count)


class Supervision(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='.w6-supervisor-', dir=Path(__file__).resolve().parents[2])
        self.addCleanup(temp.cleanup)
        self.sd = Path(temp.name)
        self.nodes = {kind: str(self.sd / kind) for kind in ('kbd', 'ptr')}
        for node in self.nodes.values():
            Path(node).touch()
        args = NS(devices_json=str(self.sd / 'devices.json'), event_socket=str(self.sd / 'input.sock'),
                  fifo=str(self.sd / 'cmd.fifo'), control=str(self.sd / 'control'), owner_uid=os.getuid())
        self.s = mkdevs.HelperSupervisor(args, self.nodes, [71, 72])
        self.addCleanup(self.s.generation.close)

    def test_respawn_reuses_devices_fds_and_pin_with_fresh_child(self):
        self.s.generation[:] = GEN.encode()
        with patch.object(mkdevs.os, 'fork', side_effect=[101, 102]) as fork, \
                patch.object(mkdevs, 'make_dev') as create, patch.object(mkdevs, 'destroy') as destroy:
            self.assertTrue(self.s.start_child())
            self.assertEqual(self.s.child, 101)
            self.s.child = None  # run() does this only after waitpid confirms exit.
            self.assertTrue(self.s.start_child())
            self.assertEqual(self.s.child, 102)
            self.assertEqual(self.s.fds, [71, 72])
            self.assertEqual(self.s.generation[:], GEN.encode())
            self.assertEqual((self.sd / 'devices.json.pid').read_text().strip(), '102')
            self.assertEqual(fork.call_count, 2)
            create.assert_not_called()
            destroy.assert_not_called()

    def test_stopping_or_missing_device_never_forks(self):
        with patch.object(mkdevs.os, 'fork') as fork:
            (self.sd / 'helper.stopping').touch()
            self.assertFalse(self.s.start_child())
            (self.sd / 'helper.stopping').unlink()
            Path(self.nodes['kbd']).unlink()
            self.assertFalse(self.s.start_child())
            Path(self.nodes['kbd']).touch()
            self.s.stopping = True
            self.assertFalse(self.s.start_child())
            fork.assert_not_called()

    def test_loop_reaps_dead_child_before_respawn_and_reaps_replacement_on_quit(self):
        self.s.child = 101
        with patch.object(mkdevs.signal, 'signal'), \
                patch.object(mkdevs.os, 'open', return_value=99999), \
                patch.object(mkdevs.os, 'read', side_effect=[BlockingIOError(), b'quit\n']), \
                patch.object(mkdevs.os, 'close'), patch.object(mkdevs.time, 'sleep'), \
                patch.object(mkdevs.os, 'waitpid', side_effect=[(101, 9), (102, 0)]) as wait, \
                patch.object(mkdevs.os, 'fork', return_value=102) as fork, \
                patch.object(mkdevs.os, 'kill') as kill, patch.object(mkdevs, 'destroy') as destroy:
            self.s.run()
        self.assertEqual(wait.call_args_list, [unittest.mock.call(101, os.WNOHANG), unittest.mock.call(102, 0)])
        fork.assert_called_once_with()
        kill.assert_called_once_with(102, signal.SIGKILL)
        destroy.assert_not_called()  # Device destruction belongs to parent main's final teardown.
        self.assertIsNone(self.s.child)

    def test_loop_observes_stopping_before_respawning_even_if_child_died(self):
        self.s.child = 101
        (self.sd / 'helper.stopping').touch()
        with patch.object(mkdevs.signal, 'signal'), \
                patch.object(mkdevs.os, 'open', return_value=99999), \
                patch.object(mkdevs.os, 'read', side_effect=BlockingIOError()), \
                patch.object(mkdevs.os, 'close'), \
                patch.object(mkdevs.os, 'waitpid', return_value=(101, 9)) as wait, \
                patch.object(mkdevs.os, 'fork') as fork, \
                patch.object(mkdevs.os, 'kill', side_effect=ProcessLookupError()):
            self.s.run()
        fork.assert_not_called()
        wait.assert_called_once_with(101, 0)

    def test_main_retains_uinput_fds_until_supervised_children_are_drained(self):
        order = []
        runner = Mock()
        runner.run.side_effect = lambda: order.append('children_drained')
        args = ['mkdevs', '--fifo', str(self.sd / 'cmd.fifo'), '--devices-json', str(self.sd / 'devices.json'),
                '--name-prefix', 'ArkDrive test', '--event-socket', str(self.sd / 'input.sock'),
                '--control', str(self.sd / 'control'), '--owner-uid', str(os.getuid()), '--supervise']
        with patch.object(sys, 'argv', args), \
                patch.object(mkdevs, 'make_dev', side_effect=[(71, self.nodes['kbd']), (72, self.nodes['ptr'])]) as create, \
                patch.object(mkdevs, 'HelperSupervisor', return_value=runner) as supervise, \
                patch.object(mkdevs, 'destroy', side_effect=lambda fd: order.append(('destroy', fd))), \
                patch.object(mkdevs, 'EventSocket') as socket:
            self.assertEqual(mkdevs.main(), 0)
        self.assertEqual(create.call_count, 2)
        self.assertEqual(supervise.call_args.args[1:], (self.nodes, [71, 72]))
        self.assertEqual(order, ['children_drained', ('destroy', 71), ('destroy', 72)])
        socket.assert_not_called()

    def test_supervised_teardown_stops_respawn_then_waits_for_current_child_before_x(self):
        (self.sd / 'control').write_bytes(PAGE.pack(b'ARKC', 1, bytes.fromhex(GEN), 7, 0, 0, 0, bytes(16)))
        (self.sd / 'devices.json.pid').write_text('202\n')
        record = dict(session='s1', session_dir=str(self.sd), profile='xorg', display=None,
                      pid_x=200, mkdevs_pid=201, mkdevs_supervisor_pid=203)
        living, order = {200, 202, 203}, []
        def wait(pids, seconds):
            if pids == [203]:
                self.assertIsNone(control_snapshot(self.sd / 'control', GEN))
                self.assertTrue((self.sd / 'helper.stopping').exists())
                with patch.object(mkdevs.os, 'fork') as fork:
                    self.assertFalse(self.s.start_child())
                    fork.assert_not_called()
                order.append('supervisor_child_exit')
                living.difference_update({202, 203})
        def kill(pid, sig):
            if pid == 200:
                self.assertFalse(living & {202, 203})
                order.append('x_signal')
                living.discard(pid)
        original_open = os.open
        def open_fifo(path, *args, **kwargs):
            if Path(path) == self.sd / 'cmd.fifo':
                raise OSError(errno.ENXIO, 'no fifo')
            return original_open(path, *args, **kwargs)
        with patch.object(sessiond, 'alive', side_effect=lambda pid: pid in living), \
                patch.object(sessiond, 'wait_exit', side_effect=wait), \
                patch.object(sessiond.os, 'open', side_effect=open_fifo), \
                patch.object(sessiond.os, 'kill', side_effect=kill), \
                patch.object(sessiond, 'marked_pids', return_value=[]), \
                patch.object(sessiond.time, 'sleep'), patch.object(sessiond, 'execute'), \
                patch.object(sessiond, 'save_registry'):
            sessiond.teardown(record, {'s1': record})
        self.assertEqual(record['mkdevs_pid'], 202)
        self.assertEqual(order, ['supervisor_child_exit', 'x_signal'])

    def test_x_signal_guard_checks_respawned_child_not_stale_registry_pid(self):
        (self.sd / 'devices.json.pid').write_text('202\n')
        record = dict(session_dir=str(self.sd), profile='xorg', pid_x=200,
                      mkdevs_pid=201, mkdevs_supervisor_pid=203)
        with patch.object(sessiond, 'alive', side_effect=lambda pid: pid == 202), \
                patch.object(sessiond.os, 'kill') as kill:
            with self.assertRaises(sessiond.UnsafeTeardown):
                sessiond.send_signal(record, 200, signal.SIGTERM)
        kill.assert_not_called()
        self.assertEqual(record['mkdevs_pid'], 202)


if __name__ == '__main__':
    unittest.main()
