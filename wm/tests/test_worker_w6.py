"""W6 headless tests: in-memory helper streams only, no X/device opens."""
import copy
import errno
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mkdevs
from input_protocol import PAGE, control_snapshot, packet
from worker_act import ActError
from worker_evdev import ChannelError, EvdevTransport, compile_keycodes
from worker_keymap import EVDEV_NAMES
import test_worker_w2 as base

GEN = '0123456789abcdef' * 2


def request(**fields):
    return dict(seq=1, generation=GEN, dev='kbd', frames=[[[1, 30, 1]], [[1, 30, 0]]], gap_us=2000, **fields)


class Helper(unittest.TestCase):
    def setUp(self):
        self.writes, self.gaps = [], []
        self.protocol = mkdevs.EventProtocol(
            {dev: lambda event, dev=dev: self.writes.append((dev, event)) for dev in ('kbd', 'ptr')},
            lambda gen: 1, lambda dev: 'busy', self.gaps.append)

    def test_frames_syn_and_gap(self):
        r = self.protocol.handle(request())
        self.assertTrue(r['ok'])
        self.assertEqual(r['frames_written'], 2)
        self.assertEqual(self.writes, [('kbd', (1, 30, 1)), ('kbd', (0, 0, 0)),
                                       ('kbd', (1, 30, 0)), ('kbd', (0, 0, 0))])
        self.assertEqual(self.gaps, [.002])

    def test_validation_is_whole_request(self):
        changes = [dict(frames=[[[1, code, 1]]]) for code in (99, 0, 249)]
        changes += [dict(frames=[[[1, 30, 2]]]), dict(dev='ptr', frames=[[[2, 2, 1]]]),
                    dict(dev='ptr', frames=[[[2, 0, 41]]]), dict(dev='ptr', frames=[[[2, 8, -41]]]),
                    dict(frames=[[[1, 30, 1]]]*65), dict(frames=[[[1, 30, 1]]*5]), dict(dev='other'),
                    dict(gap_us=-1), dict(gap_us=50001), dict(gap_us=True), dict(generation='wrong'),
                    dict(frames=[]), dict(frames=[[]]), dict(frames=[[[0, 0, 0]]]),
                    dict(frames=[[[1, 30, True]]]), dict(dev='ptr', frames=[[[1, 0x113, 1]]]),
                    dict(dev='ptr', frames=[[[1, 0x110, 2]]]), dict(frames=[[[2, 0, 1]]])]
        for seq, change in enumerate(changes, 1):
            with self.subTest(change=change):
                req = request()
                req.update(seq=seq, **change)
                # A valid prefix must also be refused, without any writes.
                if req['frames']:
                    req['frames'].insert(0, [[1, 30 if req['dev'] == 'kbd' else 0x110, 1]])
                r = self.protocol.handle(req)
                self.assertEqual(r['error'], 'rejected')
                self.assertEqual(r['frames_written'], 0)
                self.assertFalse(self.writes)
        for req in (None, [], {}, {'seq': True}, {'seq': -1}, dict(request(), extra=0)):
            self.assertEqual(self.protocol.handle(req)['error'], 'rejected')

    def test_pointer_limits_and_buttons(self):
        req = request()
        req.update(dev='ptr', gap_us=50000, frames=[[[2, c, v]] for c in (0, 1, 6, 8) for v in (-40, 40)] +
                   [[[1, c, v]] for c in (0x110, 0x111, 0x112) for v in (1, 0)])
        self.assertTrue(self.protocol.handle(req)['ok'])
        self.assertEqual(self.gaps, [.05]*13)

    def test_gate_closed_at_frame_and_epoch_fencing(self):
        for gate in (iter([1, 1, None]), iter([1, 1, 2])):
            self.writes.clear()
            self.protocol.gate = lambda gen: next(gate)
            self.protocol.high_seq = -1
            self.protocol.replies.clear()
            r = self.protocol.handle(request())
            self.assertEqual((r['error'], r['frames_written']), ('gate_closed', 1))
            self.assertEqual(self.writes, [('kbd', (1, 30, 1)), ('kbd', (0, 0, 0))])

    def test_release_exact_held_set_even_with_closed_gate(self):
        self.protocol.handle(dict(request(), frames=[[[1, 30, 1], [1, 42, 1]], [[1, 30, 0]]]))
        self.protocol.handle(dict(request(), seq=2, dev='ptr', frames=[[[1, 0x110, 1]]]))
        self.writes.clear()
        self.protocol.gate = lambda gen: None
        self.assertTrue(self.protocol.handle({'seq': 3, 'op': 'release_all'})['ok'])
        self.assertEqual(self.writes, [('kbd', (1, 42, 0)), ('kbd', (0, 0, 0)),
                                       ('ptr', (1, 0x110, 0)), ('ptr', (0, 0, 0))])
        self.writes.clear()
        self.protocol.handle({'seq': 4, 'op': 'release_all'})
        self.assertFalse(self.writes)

    def test_grab_check(self):
        self.protocol.grab = lambda dev: 'busy' if dev == 'kbd' else 'free'
        reply = self.protocol.handle({'seq': 1, 'op': 'grab_check'})
        self.assertEqual((reply['kbd'], reply['ptr']), ('busy', 'free'))

    def test_grab_lost_stops_before_frame_and_dedupes(self):
        for outcome in ('free', 'io_error', OSError(errno.EIO, 'probe error')):
            with self.subTest(outcome=outcome):
                self.setUp()
                probe = self.protocol.frame_grab = Mock(side_effect=['busy', outcome])
                req = dict(request(), frames=[[[1, 30, 1]], [[1, 30, 0]], [[1, 42, 1]]])
                r = self.protocol.handle(req)
                self.assertEqual((r['ok'], r['frames_written'], r['error']), (False, 1, 'grab_lost'))
                self.assertEqual(self.writes, [('kbd', (1, 30, 1)), ('kbd', (0, 0, 0))])
                self.assertEqual(probe.call_count, 2)
                self.assertEqual(probe.call_args_list, [unittest.mock.call('kbd')]*2)
                self.assertEqual(self.protocol.handle(req), r)
                self.assertEqual(probe.call_count, 2)
                self.assertEqual(len(self.writes), 2)

    def test_grab_held_for_maximum_paced_request(self):
        probe = self.protocol.frame_grab = Mock(return_value='busy')
        req = dict(request(), frames=[[[1, 30, i % 2]] for i in range(64)], gap_us=50000)
        r = self.protocol.handle(req)
        self.assertEqual((r['ok'], r['frames_written'], r['error']), (True, 64, None))
        self.assertEqual(len(self.writes), 128)
        self.assertEqual(probe.call_count, 64)
        self.assertEqual(self.gaps, [.05]*63)

    def test_grab_free_before_first_frame_writes_nothing(self):
        self.protocol.frame_grab = Mock(return_value='free')
        r = self.protocol.handle(request())
        self.assertEqual((r['error'], r['frames_written']), ('grab_lost', 0))
        self.assertFalse(r.get('may_have_written'))
        self.assertFalse(self.writes)
        self.protocol.frame_grab.assert_called_once_with('kbd')

    def test_gate_precedes_grab_and_grab_precedes_write_for_each_device(self):
        calls = []
        self.protocol.gate = lambda gen: calls.append('gate') or 1
        self.protocol.frame_grab = lambda dev: calls.append(('grab', dev)) or 'busy'
        self.protocol.writers['ptr'] = lambda ev: calls.append(('write', ev))
        self.protocol.handle(dict(request(), dev='ptr', frames=[[[2, 0, 1]]]*2))
        self.assertEqual(calls, ['gate'] + ['gate', ('grab', 'ptr'), ('write', (2, 0, 1)),
                                          ('write', (0, 0, 0))]*2)
        self.protocol.gate = lambda gen: None
        self.protocol.frame_grab = Mock(side_effect=AssertionError('closed gate must precede grab'))
        r = self.protocol.handle(dict(request(), seq=2))
        self.assertEqual((r['error'], r['frames_written']), ('gate_closed', 0))

    def test_release_all_bypasses_free_grab(self):
        self.protocol.handle(dict(request(), frames=[[[1, 30, 1]]]))
        self.protocol.frame_grab = Mock(return_value='free')
        self.protocol.grab = Mock(return_value='free')
        self.writes.clear()
        self.assertTrue(self.protocol.handle({'seq': 2, 'op': 'release_all'})['ok'])
        self.assertEqual(self.writes, [('kbd', (1, 30, 0)), ('kbd', (0, 0, 0))])
        self.protocol.frame_grab.assert_not_called()
        self.protocol.grab.assert_not_called()

    def test_persistent_probe_uses_one_open_and_never_ungrabs_busy_owner(self):
        with patch.object(mkdevs.os, 'open', return_value=12) as opened, patch.object(mkdevs.os, 'close') as close, patch.object(mkdevs.fcntl, 'ioctl', side_effect=OSError(errno.EBUSY, 'busy')) as ioctl:
            probe = mkdevs.FrameGrab('/assigned/node')
            self.assertEqual([probe() for _ in range(64)], ['busy']*64)
            opened.assert_called_once_with('/assigned/node', os.O_RDONLY | os.O_NONBLOCK)
            self.assertEqual(ioctl.call_args_list, [unittest.mock.call(12, 0x40044590, 1)]*64)
            close.assert_not_called()
            probe.close()
            close.assert_called_once_with(12)

    def test_persistent_probe_releases_free_grab_and_retires_on_io_error(self):
        for effects in ([0, 0], [OSError(errno.EIO, 'probe failed')], [0, OSError(errno.EIO, 'ungrab failed')]):
            with self.subTest(effects=effects), patch.object(mkdevs.os, 'open', return_value=12), patch.object(mkdevs.os, 'close') as close, patch.object(mkdevs.fcntl, 'ioctl', side_effect=effects) as ioctl:
                probe = mkdevs.FrameGrab('/assigned/node')
                if len(effects) == 2 and effects[-1] == 0:
                    self.assertEqual(probe(), 'free')
                    self.assertEqual(ioctl.call_args_list, [unittest.mock.call(12, 0x40044590, 1), unittest.mock.call(12, 0x40044590, 0)])
                else:
                    with self.assertRaises(OSError):
                        probe()
                    close.assert_called_once_with(12)
                    with self.assertRaises(OSError):
                        probe()
                    self.assertEqual(ioctl.call_count, len(effects))
                probe.close()
                close.assert_called_once_with(12)

    def test_real_grab_wrapper_outcomes(self):
        for error, expected in ((None, 'free'), (OSError(errno.EBUSY, 'busy'), 'busy')):
            with patch.object(mkdevs.os, 'open', return_value=12), patch.object(mkdevs.os, 'close') as close, patch.object(mkdevs.fcntl, 'ioctl', side_effect=error) as ioctl:
                self.assertEqual(mkdevs.node_grab('/assigned/node'), expected)
                self.assertEqual(ioctl.call_count, 2 if error is None else 1)
                close.assert_called_once_with(12)
        with patch.object(mkdevs.os, 'open', return_value=12), patch.object(mkdevs.os, 'close'), patch.object(mkdevs.fcntl, 'ioctl', side_effect=OSError(errno.EACCES, 'denied')):
            with self.assertRaises(OSError):
                mkdevs.node_grab('/assigned/node')

    def test_peer_uid_without_socket_creation(self):
        server = mkdevs.EventSocket.__new__(mkdevs.EventSocket)
        server.owner_uid = 123
        peer = Mock()
        for uid in (123, 124):
            peer.getsockopt.return_value = struct.pack('3i', 45, uid, 456)
            self.assertEqual(server.authorised(peer), uid == 123)

    def test_dedupe_changed_payload_and_generation(self):
        req = request()
        r = self.protocol.handle(req)
        before = list(self.writes)
        self.assertEqual(self.protocol.handle(req), r)
        self.assertEqual(self.protocol.handle(dict(req, gap_us=0))['error'], 'rejected')
        self.assertEqual(self.protocol.handle(dict(req, seq=2, generation='a'*32))['error'], 'rejected')
        self.assertEqual(self.writes, before)

    def test_io_prefix_remains_releasable(self):
        original = self.protocol.writers['kbd']
        def failing(ev):
            if ev == (0, 0, 0):
                raise OSError('broken')
            original(ev)
        self.protocol.writers['kbd'] = failing
        r = self.protocol.handle(request())
        self.assertEqual((r['error'], r['frames_written']), ('io_error', 0))
        self.assertTrue(r['may_have_written'])
        self.assertEqual(self.protocol.held['kbd'], {30})
        self.protocol.writers['kbd'] = original
        self.assertTrue(self.protocol.handle({'seq': 2, 'op': 'release_all'})['ok'])

    def test_control_reader_closed_missing_torn_and_generation(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2]) as tmp:
            path = Path(tmp)/'control'
            self.assertIsNone(control_snapshot(path, GEN))
            for state in (0, 1, 2, 3):
                path.write_bytes(PAGE.pack(b'ARKC', 1, bytes.fromhex(GEN), 3, state, 0, 0, bytes(16)))
                self.assertEqual(control_snapshot(path, GEN), 3 if state == 0 else None)
                self.assertIsNone(control_snapshot(path, 'f'*32))
            path.write_bytes(b'bad')
            self.assertIsNone(control_snapshot(path, GEN))

    def test_private_listener_permissions_and_cleanup_without_socket_syscalls(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2]) as tmp:
            path = Path(tmp)/'input.sock'
            path.touch(mode=0o666)
            with patch.object(mkdevs.socket, 'socket') as socket_factory, patch.object(mkdevs.threading, 'Thread'), patch.object(mkdevs.selectors, 'DefaultSelector'):
                server = mkdevs.EventSocket(path, os.getuid(), self.protocol)
                server.start()
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.stat().st_uid, os.getuid())
                socket_factory.return_value.listen.assert_called_once_with(1)
                server.close()
                self.assertFalse(path.exists())

    def test_stream_fragmentation_uid_and_one_connection(self):
        selector, listener = Mock(), Mock()
        bad, good, second = Mock(), Mock(), Mock()
        bad.getsockopt.return_value = struct.pack('3i', 1, os.getuid()+1, 1)
        for peer in (good, second):
            peer.getsockopt.return_value = struct.pack('3i', 2, os.getuid(), 1)
        listener.accept.side_effect = [(bad, None), (good, None), (second, None)]
        wire = packet(request()) + packet({'seq': 2, 'op': 'grab_check'})
        chunks = iter([wire[:2], wire[2:7], wire[7:]])
        answers, reads = bytearray(), 0
        def send(data):
            n = min(7, len(data))
            answers.extend(data[:n])
            return n
        good.send.side_effect = send
        good.recv.side_effect = lambda n: next(chunks)
        with patch.object(mkdevs.socket, 'socket', return_value=listener), patch.object(mkdevs.selectors, 'DefaultSelector', return_value=selector):
            server = mkdevs.EventSocket('/unused', os.getuid(), self.protocol)
        turns = 0
        def select(timeout):
            nonlocal turns
            turns += 1
            if turns <= 3:
                return [(NS(fileobj=listener), mkdevs.selectors.EVENT_READ)]
            if server.outgoing:
                return [(NS(fileobj=good), mkdevs.selectors.EVENT_WRITE)]
            if turns <= 6:
                return [(NS(fileobj=good), mkdevs.selectors.EVENT_READ)]
            server.stopping.set()
            return []
        selector.select.side_effect = select
        server.serve()
        bad.close.assert_called_once()
        second.close.assert_called_once()
        good.close.assert_not_called()
        replies = []
        while answers:
            size = struct.unpack('!I', answers[:4])[0]
            replies.append(json.loads(answers[4:size+4]))
            del answers[:size+4]
        self.assertEqual([r['seq'] for r in replies], [1, 2])
        self.assertTrue(all(r['ok'] for r in replies))



class MemorySocket:
    """Framed stream with fragmented sends/receives, no kernel sockets."""
    def __init__(self, handle, log, fragment=17):
        self.handle, self.log, self.fragment = handle, log, fragment
        self.incoming, self.outgoing = bytearray(), bytearray()
        self.closed = False

    def send(self, data):
        n = min(len(data), self.fragment)
        self.incoming.extend(data[:n])
        if len(self.incoming) >= 4:
            size = struct.unpack('!I', self.incoming[:4])[0]
            if len(self.incoming) == size+4:
                req = json.loads(self.incoming[4:])
                self.incoming.clear()
                self.log.append(copy.deepcopy(req))
                self.outgoing.extend(packet(self.handle(req)))
        return n

    def recv(self, size):
        if not self.outgoing:
            raise BlockingIOError()
        result = bytes(self.outgoing[:min(size, self.fragment)])
        del self.outgoing[:len(result)]
        return result

    def close(self):
        self.closed = True


class PausedSocket(MemorySocket):
    """Expose the receive/EAGAIN yield without a real socket or thread."""
    def __init__(self, handle, log):
        super().__init__(handle, log, fragment=65536)
        self.paused, self.blocked = True, False

    def send(self, data):
        if self.closed:
            raise OSError(errno.EBADF, 'closed fake socket')
        return super().send(data)

    def recv(self, size):
        if self.closed:
            raise OSError(errno.EBADF, 'closed fake socket')
        if self.paused:
            self.blocked = True
            raise BlockingIOError()
        return super().recv(size)


class Transport(unittest.TestCase):
    def setUp(self):
        self.now, self.log, self.raw = 0., [], []
        self.x = NS(raw=self.raw, raw_seq=0)
        self.w = NS(sd=Path('/unused'), generation=GEN, record={'display': 91})
        self.reply = lambda req: dict(seq=req['seq'], ok=True, frames_written=len(req.get('frames', [])), error=None)
        self.opens = 0
        def connect():
            self.opens += 1
            return MemorySocket(lambda req: self.reply(req), self.log)
        self.t = EvdevTransport(self.w, self.x, connector=connect, clock=lambda: self.now, keycodes={38: 30, 50: 42})
        self.t.sources = {'keyboard': 9, 'pointer': 10}

    def run_gen(self, gen):
        for _ in range(10000):
            try:
                next(gen)
                self.now += .001
            except StopIteration as done:
                return done.value
        self.fail('generator did not complete')

    def send(self):
        return self.run_gen(self.t.send_frames('kbd', [[[1, 30, 1]], [[1, 30, 0]]], [('key_down', 38), ('key_up', 38)], 2000))

    def emit(self, *events):
        for kind, detail, source in events:
            self.x.raw_seq += 1
            self.raw.append((self.x.raw_seq, kind, detail, source, source))

    def test_partial_packet_invalidated_before_submission_does_not_retire(self):
        pending = self.t.send_frames('kbd', [[[1, 30, 1]]], [('key_down', 38)], 2000)
        next(pending)  # MemorySocket accepts only the first 17 bytes.
        sock = self.t.sock
        self.t.dirty = True
        result = self.run_gen(pending)
        self.assertEqual((result['reason'], result['dispatch_status']), ('provider_degraded', 'not_dispatched'))
        self.assertFalse(self.log)
        self.assertTrue(sock.closed)
        self.assertFalse(self.t.inflight)
        self.assertNotEqual(self.t.health['state'], 'down')
        self.assertEqual(self.t.reconnects, 0)

    def test_stale_plan_waiting_for_stream_never_submits(self):
        self.t.inflight = True
        pending = self.t.send_frames('kbd', [[[1, 30, 1]]], [('key_down', 38)], 2000, map_epoch=0)
        next(pending)
        self.t.map_epoch = 1
        self.t.inflight = False
        result = self.run_gen(pending)
        self.assertEqual(result['reason'], 'provider_degraded')
        self.assertEqual(result['dispatch_status'], 'not_dispatched')
        self.assertFalse(self.log)
        self.assertNotEqual(self.t.health['state'], 'down')

    def test_dirty_retry_preserves_prior_submission_unknown(self):
        transport = self.t
        class LostReply(MemorySocket):
            def recv(self, size):
                transport.dirty = True
                raise OSError(errno.ECONNRESET, 'lost reply')
        self.t.connector = Mock(side_effect=[LostReply(lambda req: self.reply(req), self.log, fragment=65536),
                                             MemorySocket(lambda req: self.reply(req), self.log)])
        result = self.send()
        self.assertEqual(result['dispatch_status'], 'unknown')
        self.assertIsNone(result['accepted'])
        self.assertEqual(self.t.health['last_error'], 'helper_disconnected')
        self.assertEqual(len(self.log), 1)

    def test_seq_and_full_raw_ack(self):
        def reply(req):
            self.emit(('key_down', 38, 9), ('key_up', 38, 9))
            return dict(seq=req['seq'], ok=True, frames_written=2, error=None)
        self.reply = reply
        result = self.send()
        self.assertEqual(result['dispatch_status'], 'accepted')
        self.assertEqual(result['server_ack']['seen'], 2)
        self.assertEqual(self.log[0]['seq'], 1)
        self.assertEqual(self.log[0]['gap_us'], 2000)

    def test_short_raw_ack_is_unknown(self):
        def reply(req):
            self.emit(('key_down', 38, 9), ('key_up', 38, 5))  # wrong source
            return dict(seq=req['seq'], ok=True, frames_written=2, error=None)
        self.reply = reply
        result = self.send()
        self.assertEqual((result['dispatch_status'], result['reason']), ('unknown', 'input_unacknowledged'))
        self.assertEqual(result['server_ack']['seen'], 1)
        self.assertGreaterEqual(self.now, .152)

    def test_partial_written_count_and_release_no_replay(self):
        self.reply = lambda req: dict(seq=req['seq'], ok=req.get('op') == 'release_all',
                                      frames_written=0 if 'op' in req else 1, error=None if 'op' in req else 'io_error')
        result = self.send()
        self.assertEqual((result['dispatch_status'], result['reason'], result['written']), ('accepted', 'channel_down', 1))
        self.assertEqual([req.get('op') for req in self.log], [None, 'release_all'])
        self.assertEqual(self.t.health['state'], 'down')

    def test_grab_lost_exact_extent_release_and_no_reconnect(self):
        for written in (0, 1):
            with self.subTest(written=written):
                self.setUp()
                self.reply = lambda req: dict(seq=req['seq'], ok=req.get('op') == 'release_all',
                    frames_written=0 if 'op' in req else written, error=None if 'op' in req else 'grab_lost')
                result = self.send()
                self.assertEqual((result['dispatch_status'], result['reason'], result['written']),
                                 ('accepted' if written else 'not_dispatched', 'channel_down', written))
                self.assertEqual(result['frames_written'], written)
                self.assertEqual(result['written_prefix'], {'frames': written})
                self.assertEqual(result['error'], 'grab_lost')
                self.assertEqual([req.get('op') for req in self.log], [None, 'release_all'])
                self.assertEqual(self.t.health, {'state': 'down', 'grab': False, 'keyboard': 'untested', 'last_error': 'grab_lost'})
                self.assertEqual((self.t.reconnects, self.opens), (0, 1))
                self.assertEqual(self.send()['dispatch_status'], 'not_dispatched')
                self.assertEqual(len(self.log), 2)

    def test_refusal_before_write_and_one_reconnect(self):
        self.reply = lambda req: dict(seq=req['seq'], ok=False, frames_written=0, error='rejected')
        r = self.send()
        self.assertEqual(r['dispatch_status'], 'not_dispatched')
        self.assertEqual(r['written'], 0)
        self.assertEqual(self.opens, 2)
        self.assertEqual(self.t.health['state'], 'down')
        self.assertEqual(sum('frames' in req for req in self.log), 1)

    def test_unreachable_before_send_then_down(self):
        self.t.connector = Mock(side_effect=OSError('unreachable'))
        r = self.send()
        self.assertEqual(r['dispatch_status'], 'not_dispatched')
        self.assertEqual(self.t.connector.call_count, 2)
        self.assertEqual(self.t.health['state'], 'down')

    def test_seq_mismatch_is_never_accepted(self):
        self.reply = lambda req: dict(seq=req['seq']+1, ok=True, frames_written=2, error=None)
        result = self.send()
        self.assertEqual(result['dispatch_status'], 'unknown')  # Lost/invalid helper ack after submission.
        self.assertEqual(self.log[0], self.log[1])
        self.assertEqual(self.t.health['last_error'], 'helper_seq_mismatch')

    def test_lost_ack_reconnect_same_seq_dedupes(self):
        writes = []
        helper = mkdevs.EventProtocol({'kbd': writes.append}, lambda g: 1, lambda dev: 'busy')
        calls = 0
        def reply(req):
            nonlocal calls
            answer = helper.handle(req)
            calls += 1
            if calls == 1:
                raise OSError('lost reply')
            self.emit(('key_down', 38, 9), ('key_up', 38, 9))
            return answer
        self.reply = reply
        result = self.send()
        self.assertEqual(result['dispatch_status'], 'accepted')
        self.assertEqual(len(writes), 4)
        self.assertEqual(self.log[0], self.log[1])

    def test_plan_motion_scroll_buttons_and_remapped_key(self):
        self.t.keycodes = {200: 30}  # Deliberately no fixed offset.
        plan = self.t.plan([('key_down', 200), ('motion', 0, 101, 89), ('button_down', 2),
                            ('button_down', 4), ('button_up', 4), ('button_down', 7)], {'x': 1, 'y': 1})
        self.assertEqual(plan[0][1], [[1, 30, 1]])
        motion = [frame for dev, frame, expected in plan if expected == [('motion', 0)]]
        self.assertEqual(len(motion), 3)
        self.assertTrue(all(abs(e[2]) <= 40 for frame in motion for e in frame))
        self.assertEqual(sum(e[2] for frame in motion for e in frame if e[1] == 0), 100)
        self.assertIn(('ptr', [[1, 0x112, 1]], [('button_down', 2)]), plan)
        self.assertEqual(plan[-1][1], [[2, 6, 1]])
        self.assertEqual(plan[-2][2], [('button_down', 4), ('button_up', 4)])
        with self.assertRaises(ActError):
            self.t.plan([('key_down', 99)], {'x': 0, 'y': 0})
        self.assertEqual(EVDEV_NAMES['AC01'], 30)

    def test_missing_or_free_grab_fails_closed_without_input(self):
        for response in ({'kbd': 'free', 'ptr': 'busy'}, {'kbd': 'busy', 'ptr': 'free'}, {}):
            self.t.health.update(state='untested', last_error=None)
            self.reply = lambda req, response=response: dict(seq=req['seq'], ok=True, **response)
            self.assertFalse(self.run_gen(self.t.grab_check()))
            self.assertEqual(self.t.health['last_error'], 'no_exclusive_grab')
        self.assertFalse(any('frames' in req for req in self.log))

    def test_source_discovery_only_assigned_enabled_slaves(self):
        from Xlib.ext import xinput
        self.t.sources.clear()
        self.t.discover([{'name': 'kbd', 'use': xinput.SlaveKeyboard, 'enabled': True, 'deviceid': 9},
                         {'name': b'ptr', 'use': xinput.SlavePointer, 'enabled': True, 'deviceid': 10},
                         {'name': 'kbd', 'use': xinput.MasterKeyboard, 'enabled': True, 'deviceid': 2},
                         {'name': 'ptr', 'use': xinput.SlavePointer, 'enabled': False, 'deviceid': 12},
                         {'name': 'other', 'use': xinput.SlaveKeyboard, 'enabled': True, 'deviceid': 13}])
        self.assertEqual(self.t.sources, {'keyboard': 9, 'pointer': 10})

    def test_down_is_latched_for_generation(self):
        self.t.down('fixture_failure')
        self.assertFalse(self.run_gen(self.t.startup()))
        result = self.send()
        self.assertEqual(result['dispatch_status'], 'not_dispatched')
        self.assertFalse(self.log)

    def test_partial_request_send_failure_proves_non_dispatch(self):
        class BrokenSocket:
            def send(self, data):
                raise OSError('before any request bytes')
            def close(self):
                pass
        self.t.connector = lambda: BrokenSocket()
        self.assertEqual(self.send()['dispatch_status'], 'not_dispatched')
        self.assertEqual(self.t.reconnects, 1)

    def test_late_raw_ack_can_be_consumed_without_resending(self):
        result = self.send()
        self.assertEqual(result['dispatch_status'], 'unknown')
        self.emit(('key_down', 38, 9), ('key_up', 38, 9))
        self.t.last_ack.feed(self.t.raw)
        self.assertTrue(self.t.last_ack.complete)
        self.assertEqual(len(self.log), 1)

    def test_close_during_receive_yield_is_channel_error_and_down(self):
        sock = PausedSocket(self.reply, self.log)
        self.t.connector = Mock(side_effect=[sock, OSError(errno.ECONNRESET, 'retry disconnected')])
        pending = self.t.request(**{k: v for k, v in request().items() if k != 'seq'})
        next(pending)  # Entire request sent.
        next(pending)  # recv raises BlockingIOError and yields.
        self.assertTrue(sock.blocked)
        self.t.close()
        self.t.close()  # None is harmless, including during inflight I/O.
        with self.assertRaises(ChannelError) as caught:
            self.run_gen(pending)
        self.assertEqual((caught.exception.reason, caught.exception.submitted), ('helper_disconnected', True))
        self.assertEqual(self.t.health['last_error'], 'helper_disconnected')
        self.assertEqual(self.t.health['state'], 'down')
        self.assertFalse(self.t.inflight)
        self.assertIsNone(self.t.sock)
        self.assertEqual(self.t.connector.call_count, 2)

    def test_exchange_never_switches_to_replacement_socket_after_yield(self):
        old = PausedSocket(self.reply, self.log)
        replacement = Mock()
        self.t.sock = old
        pending = self.t.exchange(request())
        next(pending)
        next(pending)
        self.t.close()
        self.t.sock = replacement
        with self.assertRaises(ChannelError) as caught:
            next(pending)
        self.assertEqual(caught.exception.reason, 'helper_disconnected')
        replacement.send.assert_not_called()
        replacement.recv.assert_not_called()

    def test_failed_initial_connect_uses_fresh_exchange_on_retry(self):
        sock = PausedSocket(self.reply, self.log)
        self.t.connector = Mock(side_effect=[OSError('first connect failed'), sock])
        pending = self.t.request(op='grab_check')
        next(pending)
        next(pending)
        self.assertTrue(sock.blocked)
        self.assertEqual(self.t.reconnects, 1)
        self.t.down('session_keymap_changed')
        with self.assertRaises(ChannelError) as caught:
            self.run_gen(pending)
        self.assertEqual((caught.exception.reason, caught.exception.submitted), ('helper_disconnected', True))
        self.assertEqual(self.t.health['last_error'], 'session_keymap_changed')
        self.assertEqual(self.t.connector.call_count, 2)
        self.assertEqual(len(self.log), 1)

    def test_unexpected_transport_exceptions_are_structured(self):
        for phase in ('connect', 'send', 'recv', 'packet', 'request'):
            with self.subTest(phase=phase):
                self.setUp()
                sock = Mock()
                sock.send.return_value = len(packet(request()))
                sock.recv.side_effect = RuntimeError('fake receive fault')
                self.t.connector = Mock(return_value=sock)
                if phase == 'connect':
                    self.t.connector.side_effect = RuntimeError('fake connector fault')
                elif phase == 'send':
                    sock.send.side_effect = RuntimeError('fake send fault')
                elif phase == 'request':
                    self.t.seq = None
                with patch('worker_evdev.packet', side_effect=RuntimeError('fake framing fault')) if phase == 'packet' else patch('worker_evdev.packet', wraps=packet):
                    with self.assertRaises(ChannelError) as caught:
                        self.run_gen(self.t.request(**{k: v for k, v in request().items() if k != 'seq'}))
                kind = 'TypeError' if phase == 'request' else 'RuntimeError'
                self.assertEqual(caught.exception.reason, 'helper_transport_error:' + kind)
                self.assertEqual(caught.exception.submitted, phase == 'recv')
                self.assertEqual(self.t.health['last_error'], caught.exception.reason)
                self.assertEqual(self.t.health['state'], 'down')
                self.assertFalse(self.t.inflight)
                self.assertIsNone(self.t.sock)

    def test_close_fault_detaches_socket_and_is_idempotent(self):
        sock = self.t.sock = Mock()
        sock.close.side_effect = RuntimeError('fake close fault')
        self.t.close()
        self.t.close()
        sock.close.assert_called_once()
        self.assertIsNone(self.t.sock)
        self.assertEqual(self.t.health['state'], 'down')
        self.assertEqual(self.t.health['last_error'], 'helper_transport_error:RuntimeError')

    def test_grab_check_and_release_queue_behind_event_exchange(self):
        reply = lambda req: dict(self.reply(req), kbd='busy', ptr='busy')
        sock = PausedSocket(reply, self.log)
        self.t.connector = Mock(return_value=sock)
        pending = self.t.request(**{k: v for k, v in request().items() if k != 'seq'})
        next(pending)
        next(pending)
        grab, release = self.t.grab_check(), self.t.release_all()
        for _ in range(3):
            next(grab)
            next(release)
        self.assertEqual(len(self.log), 1)
        self.assertEqual(self.t.seq, 1)
        self.assertTrue(self.t.inflight)
        self.assertFalse(sock.closed)
        self.assertEqual(self.t.health['state'], 'untested')
        sock.paused = False
        self.assertTrue(self.run_gen(pending)['ok'])
        next(grab)  # grab now owns the stream and has sent its request.
        next(release)
        self.assertEqual([req.get('op') for req in self.log], [None, 'grab_check'])
        self.assertTrue(self.run_gen(grab))
        self.assertTrue(self.run_gen(release))
        self.assertEqual([req.get('op') for req in self.log], [None, 'grab_check', 'release_all'])
        self.assertEqual([req['seq'] for req in self.log], [1, 2, 3])
        self.t.connector.assert_called_once()
        self.assertFalse(self.t.inflight)

    def test_cancelling_queued_request_does_not_unlock_owner(self):
        sock = PausedSocket(self.reply, self.log)
        self.t.connector = lambda: sock
        pending = self.t.request(op='grab_check')
        next(pending)
        queued = self.t.request(op='release_all')
        next(queued)
        queued.close()
        self.assertTrue(self.t.inflight)
        self.assertEqual(len(self.log), 1)
        sock.paused = False
        self.run_gen(pending)
        self.assertFalse(self.t.inflight)


    def test_session_xkb_compilation_uses_key_names_and_releases_resources(self):
        xcb, xkb, x11 = Mock(), Mock(), Mock()
        xcb.xcb_connect.return_value = 2
        xcb.xcb_connection_has_error.return_value = 0
        x11.xkb_x11_setup_xkb_extension.return_value = 1
        xkb.xkb_context_new.return_value = 3
        x11.xkb_x11_keymap_new_from_device.return_value = 4
        xkb.xkb_keymap_min_keycode.return_value = 200
        xkb.xkb_keymap_max_keycode.return_value = 202
        xkb.xkb_keymap_key_get_name.side_effect = [b'AC01', b'LFSH', None]
        with patch('worker_evdev.C.CDLL', side_effect=[xcb, xkb, x11]):
            self.assertEqual(compile_keycodes(':91', 9), {200: 30, 201: 42})
        xcb.xcb_connect.assert_called_once_with(b':91', None)
        x11.xkb_x11_keymap_new_from_device.assert_called_once_with(3, 2, 9, 0)
        xkb.xkb_keymap_unref.assert_called_once_with(4)
        xkb.xkb_context_unref.assert_called_once_with(3)
        xcb.xcb_disconnect.assert_called_once_with(2)



class EngineW6(base.Harness):
    def setUp(self):
        super().setUp()
        self.w.generation = GEN
        self.w.record['profile'] = 'xorg'
        self.log = []
        self.ack_evdev, self.fail_frame = True, None
        self.slave_ids = {'keyboard': 9, 'pointer': 10}
        self.frame_count = 0
        self.buffers = {'kbd': [], 'ptr': []}
        mapping = {37: 29, 50: 42, 64: 56, 133: 125, 38: 30, 56: 48, 68: 60, 36: 28, 23: 15}
        reverse = self.server_keycodes = {v: k for k, v in mapping.items()}
        def writer(dev, event):
            if self.fail_frame is not None and self.frame_count >= self.fail_frame:
                raise OSError('helper killed')
            if event[0] != 0:
                self.buffers[dev].append(event)
                return
            self.frame_count += 1
            frame, self.buffers[dev] = self.buffers[dev], []
            events, dx, dy = [], 0, 0
            for typ, code, value in frame:
                if typ == 1:
                    kind = 'key' if dev == 'kbd' else 'button'
                    detail = reverse[code] if dev == 'kbd' else {0x110: 1, 0x111: 3, 0x112: 2}[code]
                    events.append((kind+('_down' if value else '_up'), detail))
                elif code in (0, 1):
                    if code == 0:
                        dx += value
                    else:
                        dy += value
                else:
                    button = {8: 4 if value > 0 else 5, 6: 7 if value > 0 else 6}[code]
                    events += [('button_down', button), ('button_up', button)]
            if dx or dy:
                events.append(('motion', 0, self.t.position['x']+dx, self.t.position['y']+dy))
            for ev in events:
                # Independent fake X server: consume the actual helper frame,
                # log application delivery, then produce the assigned raw source.
                previous = self.t.ack
                self.t.ack = False
                self.t.write(ev)
                self.t.ack = previous
                if self.ack_evdev:
                    self.t.raw_seq += 1
                    source = self.slave_ids['keyboard' if dev == 'kbd' else 'pointer']
                    self.t.raw.append((self.t.raw_seq, ev[0], ev[1], source, source))
        def gate(generation):
            c = self.w.cache.control
            return c['epoch'] if c['state'] == 'agent' else None
        self.helper = mkdevs.EventProtocol({d: lambda event, d=d: writer(d, event) for d in ('kbd', 'ptr')}, gate, lambda d: 'busy', lambda delay: setattr(self, 'now', self.now+delay))
        self.evdev = EvdevTransport(self.w, self.t,
            connector=lambda: MemorySocket(self.helper.handle, self.log, fragment=65536),
            clock=lambda: self.now, keycodes=mapping)
        self.evdev.sources = {'keyboard': 9, 'pointer': 10}
        self.evdev.devices = Mock(side_effect=lambda cb: cb(self.assigned_devices()))
        self.evdev.health.update(state='up', grab=True)
        self.e.evdev = self.evdev
        self.addCleanup(self.evdev.close)

    def assigned_devices(self):
        from Xlib.ext import xinput
        return [NS(name=name, enabled=True, use=use, deviceid=self.slave_ids[kind])
                for name, kind, use in (('kbd', 'keyboard', xinput.SlaveKeyboard),
                                        ('ptr', 'pointer', xinput.SlavePointer))]

    def params(self, steps=None, **extra):
        return super().params(steps, **dict(generation=GEN, **extra))

    def test_evdev_is_primary_and_receipt_records_route(self):
        r = self.run_action()
        self.assertEqual(r['status'], 'success')
        self.assertEqual(r['steps'][0]['route'], 'evdev')
        self.assertEqual(r['steps'][0]['attempts'][0]['written'], 1)
        self.assertEqual(self.log[0]['op'], 'grab_check')

    def test_down_channel_uses_xtest(self):
        self.evdev.down('fixture')
        r = self.run_action()
        self.assertEqual(r['steps'][0]['route'], 'xtest')
        self.assertFalse(self.log)

    def test_chord_guard_before_any_helper_traffic(self):
        for key in ('Sys_Req', 'Print'):
            self.evdev.guard({'keys': ['Alt_L', 'a']})
            with self.assertRaises(ActError) as caught:
                self.evdev.guard({'keys': ['Alt_L', key]})
            self.assertEqual(caught.exception.reason, 'invalid_request')
        r = self.run_action(self.params([self.key(intent={'kind': 'key', 'keys': ['Alt_L', 'Sys_Req']})]))
        self.assertEqual(r['reason'], 'invalid_request')
        self.assertFalse(self.log)
        self.assertFalse(self.t.log)

    def test_same_chord_on_xtest_untouched(self):
        original = self.t.keycode
        self.t.keycode = lambda name: 107 if name == 'Sys_Req' else original(name)
        r = self.run_action(self.params([self.key(intent={'kind': 'key', 'keys': ['Alt_L', 'Sys_Req'], 'route': 'xtest'})]))
        self.assertEqual(r['steps'][0]['route'], 'xtest')
        self.assertIn(('key_down', 107), self.t.log)
        self.assertFalse(self.log)

    def test_full_selftest_both_channels(self):
        self.w.cache.windows.clear()
        self.e.start_selftest()
        self.drain()
        self.assertEqual(self.evdev.health['state'], 'up')
        self.assertEqual(self.evdev.health['keyboard'], 'tested')
        self.assertEqual(self.e.channel['state'], 'ok')
        self.assertEqual(len(self.t.log), 400)
        self.assertEqual(self.t.position, {'x': 2, 'y': 2})
        self.assertEqual(sum(len(r.get('frames', [])) for r in self.log), 200)
        self.assertFalse(self.t.down)

    def test_late_attach_pointer_only_health_untested(self):
        self.e.start_selftest()
        self.drain()
        self.assertEqual(self.evdev.health['keyboard'], 'untested')
        self.assertEqual(self.evdev.health['state'], 'up')
        self.assertEqual(len(self.t.log), 200)
        self.assertTrue(all(e[0] == 'motion' for e in self.t.log))

    def test_evdev_fails_xtest_passes_still_starts(self):
        self.ack_evdev = False
        self.e.start_selftest()
        self.drain()
        self.assertEqual(self.evdev.health['state'], 'down')
        self.assertEqual(self.e.channel['state'], 'ok')
        self.assertEqual(self.evdev.health['last_error'], 'input_unacknowledged')

    def settlement_event(self, hierarchy=False):
        from Xlib import X
        from Xlib.ext import ge, xinput
        self.w.x = NS(ev=NS(display=NS(get_extension_major=lambda name: 131)))
        return (NS(type=ge.GenericEventCode, evtype=xinput.HierarchyChanged, extension=131)
                if hierarchy else NS(type=X.MappingNotify))

    def test_settlement_burst_compiles_final_hierarchy_and_runs_full_selftests(self):
        self.evdev.provided_keycodes = None
        mapping = dict(self.evdev.keycodes)
        self.evdev.health.update(state='untested', grab=False)
        self.w.cache.windows.clear()
        self.e.start_selftest()
        with patch('worker_evdev.compile_keycodes', return_value=mapping) as compile_map:
            for n in range(4):
                self.slave_ids = {'keyboard': 19+n, 'pointer': 29+n}
                self.evdev.events([self.settlement_event(), self.settlement_event(True)])
                for _ in range(20):
                    self.tick()
                self.assertFalse(self.evdev.established)
                self.assertEqual(self.evdev.health['state'], 'untested')
                compile_map.assert_not_called()
                self.evdev.devices.assert_not_called()
                self.assertFalse(self.log)
            self.drain()
            compile_map.assert_called_once_with(':91', 22)
        self.assertEqual(self.evdev.sources, {'keyboard': 22, 'pointer': 32})
        self.assertTrue(self.evdev.established)
        self.assertEqual(self.evdev.health, dict(state='up', grab=True, keyboard='tested', last_error=None))
        self.assertEqual(self.e.channel['state'], 'ok')
        self.assertEqual(sum(len(r.get('frames', [])) for r in self.log), 200)
        self.assertEqual(len(self.t.log), 400)
        self.assertEqual(self.t.test_presses, 50)
        self.assertEqual(self.t.position, {'x': 2, 'y': 2})

    def test_settlement_during_discovery_discards_snapshot_before_compile(self):
        mapping = dict(self.evdev.keycodes)
        self.evdev.provided_keycodes = None
        def devices(cb):
            snapshot = self.assigned_devices()
            if self.evdev.devices.call_count == 1:
                self.slave_ids = {'keyboard': 19, 'pointer': 20}
                self.evdev.events([self.settlement_event(True)])
            cb(snapshot)
        self.evdev.devices.side_effect = devices
        self.e.start_selftest()
        with patch('worker_evdev.compile_keycodes', return_value=mapping) as compile_map:
            self.drain()
            compile_map.assert_called_once_with(':91', 19)
        self.assertEqual(self.evdev.devices.call_count, 2)
        self.assertEqual(self.evdev.sources, self.slave_ids)
        self.assertEqual(self.evdev.health['state'], 'up')

    def test_settlement_during_compile_retries_instead_of_latching_compile_failure(self):
        mapping = dict(self.evdev.keycodes)
        self.evdev.provided_keycodes = None
        def compile_map(display, keyboard):
            if keyboard == 9:
                self.slave_ids = {'keyboard': 19, 'pointer': 20}
                self.evdev.events([self.settlement_event()])
                raise OSError('old slave disappeared during compile')
            return mapping
        self.e.start_selftest()
        with patch('worker_evdev.compile_keycodes', side_effect=compile_map) as compile_mock:
            self.drain()
            self.assertEqual([call.args for call in compile_mock.call_args_list], [(':91', 9), (':91', 19)])
        self.assertTrue(self.evdev.established)
        self.assertEqual(self.evdev.health['state'], 'up')
        self.assertIsNone(self.evdev.health['last_error'])

    def test_settlement_during_grab_check_rediscovers_and_recompiles(self):
        mapping = dict(self.evdev.keycodes)
        self.evdev.provided_keycodes = None
        sock = PausedSocket(self.helper.handle, self.log)
        self.evdev.connector = Mock(return_value=sock)
        self.e.start_selftest()
        with patch('worker_evdev.compile_keycodes', return_value=mapping) as compile_map:
            for _ in range(1000):
                self.tick()
                if sock.blocked:
                    break
            self.assertTrue(sock.blocked)
            self.assertFalse(self.evdev.established)
            self.slave_ids = {'keyboard': 19, 'pointer': 20}
            self.evdev.events([self.settlement_event(True), self.settlement_event()])
            self.assertIs(self.evdev.sock, sock)
            sock.paused = False
            self.drain()
            self.assertEqual([c.args for c in compile_map.call_args_list], [(':91', 9), (':91', 19)])
        self.assertEqual(self.evdev.devices.call_count, 2)
        self.assertEqual(sum(r.get('op') == 'grab_check' for r in self.log), 2)
        self.assertEqual(self.evdev.health['state'], 'up')
        self.evdev.connector.assert_called_once_with()

    def test_free_grab_during_settlement_can_establish_after_final_busy_probe(self):
        handle = self.helper.handle
        def reply(req):
            result = handle(req)
            if req.get('op') == 'grab_check' and self.evdev.devices.call_count == 1:
                self.evdev.events([self.settlement_event(True)])
                result['kbd'] = 'free'
            return result
        self.evdev.connector = lambda: MemorySocket(reply, self.log, fragment=65536)
        self.e.start_selftest()
        self.drain()
        self.assertEqual(self.evdev.devices.call_count, 2)
        self.assertTrue(self.evdev.established)
        self.assertEqual(self.evdev.health['state'], 'up')

    def test_discovery_failure_after_settlement_is_contained_and_honest(self):
        self.evdev.events([self.settlement_event(), self.settlement_event(True)])
        self.evdev.devices.side_effect = lambda cb: cb([])
        self.w.cache.windows.clear()
        self.e.start_selftest()
        self.drain()
        self.assert_xtest_allows_worker_ready('assigned_slaves_unavailable')
        self.assertEqual(self.evdev.sources, {})  # Cached identities cannot survive a fresh query.
        self.assertFalse(self.evdev.established)
        self.assertEqual(len(self.t.log), 200)
        self.assertFalse(any('frames' in req for req in self.log))

    def test_discovery_error_reply_and_timeout_are_contained(self):
        for value in (None, 'timeout'):
            with self.subTest(value=value):
                self.setUp()
                self.evdev.devices.side_effect = lambda cb: cb(None) if value is None else None
                self.e.start_selftest()
                self.drain()
                self.assert_xtest_allows_worker_ready('assigned_slaves_unavailable')
                self.assertFalse(self.evdev.established)

    def test_bootstrap_queries_current_session_and_contains_xi_query_error(self):
        from Xlib.ext import xinput
        for query_error in (False, True):
            with self.subTest(query_error=query_error):
                self.setUp()
                del self.evdev.devices  # Use the production XIQueryDevice bridge.
                self.t.device_list = self.assigned_devices()  # Obsolete cached ids 9/10.
                self.slave_ids = {'keyboard': 19, 'pointer': 20}
                queries = []
                def ask(cls, cb, **kw):
                    self.assertIs(cls, xinput.XIQueryDevice)
                    self.assertEqual((kw['opcode'], kw['deviceid']), (131, xinput.AllDevices))
                    queries.append(True)
                    if query_error:
                        kw['error_callback'](OSError('fake XI failure'))
                    else:
                        cb({'devices': self.assigned_devices()})
                self.w.x = NS(ax=NS(ask=ask), ev=NS(display=NS(get_extension_major=lambda _: 131)))
                self.e.start_selftest()
                self.drain()
                self.assertEqual(len(queries), 1)
                if query_error:
                    self.assert_xtest_allows_worker_ready('assigned_slaves_unavailable')
                else:
                    self.assertEqual(self.evdev.sources, self.slave_ids)
                    self.assertTrue(self.evdev.established)
                    self.assertEqual(self.evdev.health['state'], 'up')

    def test_compile_failure_after_settlement_keeps_xtest_and_cause(self):
        self.evdev.provided_keycodes = None
        self.evdev.events([self.settlement_event()])
        self.e.start_selftest()
        with patch('worker_evdev.compile_keycodes', side_effect=OSError('missing map')):
            self.drain()
        self.assert_xtest_allows_worker_ready('session_keymap_unavailable')
        self.assertFalse(self.evdev.established)

    def test_grab_failure_after_settlement_keeps_xtest_and_cause(self):
        self.evdev.events([self.settlement_event(True)])
        self.helper.grab = lambda dev: 'free'
        self.e.start_selftest()
        self.drain()
        self.assert_xtest_allows_worker_ready('no_exclusive_grab')
        self.assertFalse(self.evdev.established)
        self.assertFalse(self.evdev.health['grab'])
        self.assertFalse(any('frames' in req for req in self.log))

    def test_settlement_during_final_barrier_cannot_establish_old_snapshot(self):
        original_barrier = self.t.barrier
        barriers = []
        def barrier(cb):
            barriers.append(True)
            if len(barriers) == 2:  # After the first successful startup grab.
                self.slave_ids = {'keyboard': 19, 'pointer': 20}
                self.evdev.events([self.settlement_event(True)])
            original_barrier(cb)
        self.t.barrier = barrier
        self.e.start_selftest()
        self.drain()
        self.assertEqual(self.evdev.devices.call_count, 2)
        self.assertEqual(self.evdev.sources, self.slave_ids)
        self.assertTrue(self.evdev.established)
        self.assertEqual(self.evdev.health['state'], 'up')

    def test_endless_settlement_is_bounded_and_keeps_xtest(self):
        self.e.start_selftest()
        for _ in range(105):
            self.evdev.events([self.settlement_event()])
            for _ in range(20):
                self.tick()
        self.drain()
        self.assert_xtest_allows_worker_ready('session_settlement_timeout')
        self.assertFalse(self.evdev.established)
        self.evdev.devices.assert_not_called()

    def assigned_removal(self):
        from Xlib.ext import xinput
        event = self.settlement_event(True)
        event.data = NS(info=[NS(deviceid=self.slave_ids['keyboard'], flags=xinput.SlaveRemoved, enabled=False)])
        return event

    def test_established_assigned_hierarchy_change_retires_without_rebootstrap(self):
        from Xlib.ext import xinput
        for flags in (xinput.SlaveRemoved, xinput.DeviceDisabled):
            reason = 'assigned_device_changed'
            with self.subTest(flags=flags):
                self.setUp()
                self.e.start_selftest()
                self.drain()
                self.assertTrue(self.evdev.established)
                event = self.assigned_removal()
                event.data.info[0].flags = flags
                self.evdev.events([event])
                self.assertEqual(self.evdev.health['last_error'], reason)
                self.assertEqual(self.evdev.health['state'], 'down')
                self.assertIsNone(self.evdev.sock)
                self.e.start_selftest()
                self.drain()
                self.assert_xtest_allows_worker_ready(reason)
                self.evdev.devices.assert_called_once()

    def establish_for_refresh(self):
        self.e.start_selftest()
        self.drain()
        self.assertTrue(self.evdev.established)
        self.log.clear()
        self.evdev.devices.reset_mock()

    def finish_refresh(self):
        for _ in range(3000):
            if self.evdev.recompile_runner is None:
                return
            self.tick()
        self.fail('cache refresh did not finish')

    def test_identical_map_refresh_preserves_health_and_connection(self):
        self.establish_for_refresh()
        self.evdev.health['keyboard'] = 'tested'
        health, sock = dict(self.evdev.health), self.evdev.sock
        mapping = dict(self.evdev.keycodes)
        self.evdev.provided_keycodes = None
        self.evdev.events([self.settlement_event()])
        self.assertTrue(self.evdev.dirty)
        self.assertEqual(self.evdev.health, health)
        with patch('worker_evdev.compile_keycodes', return_value=mapping) as compile_map:
            self.finish_refresh()
        compile_map.assert_called_once_with(':91', 9)
        self.evdev.devices.assert_called_once()
        self.assertEqual(self.evdev.health, health)
        self.assertEqual(self.w.channels()['evdev']['recompiles'], 1)
        self.assertEqual(self.evdev.map_epoch, 0)
        self.assertIs(self.evdev.sock, sock)
        self.assertFalse(any('frames' in req for req in self.log))

    def test_changed_map_adopted_and_act_waits_without_stale_dispatch(self):
        self.establish_for_refresh()
        self.evdev.health['keyboard'] = 'tested'
        old = dict(self.evdev.keycodes)
        new = dict(old)
        new[38] = 31
        self.evdev.provided_keycodes = None
        callbacks = []
        self.evdev.devices.side_effect = callbacks.append
        self.evdev.events([self.settlement_event()])
        self.submit(self.params([self.move(30, 2, timeout_ms=500)]))
        with patch('worker_evdev.compile_keycodes', return_value=new):
            for _ in range(80):
                self.tick()
            self.assertEqual(len(callbacks), 1)
            self.assertEqual(self.evdev.keycodes, old)
            self.assertTrue(self.evdev.dirty)
            self.assertFalse(any('frames' in req for req in self.log))
            self.assertEqual(self.e.get(GEN, self.e.active['params']['action_id'])['status'], 'pending')
            callbacks.pop()(self.assigned_devices())
            self.drain()
        self.assertEqual(self.result()['status'], 'success')
        self.assertEqual(self.result()['steps'][0]['route'], 'evdev')
        self.assertEqual(self.evdev.keycodes, new)
        self.assertEqual(self.evdev.plan([('key_down', 38)], self.t.position)[0][1], [[1, 31, 1]])
        self.assertEqual(self.evdev.health['state'], 'up')
        self.assertEqual(self.evdev.health['keyboard'], 'untested')
        self.assertEqual(self.evdev.recompiles, 1)
        # A subsequent verified keyboard step on the adopted table promotes it.
        self.server_keycodes[31] = 38
        self.t.on_write = lambda event: self.later() if event == ('key_up', 38) else None
        result = self.run_action(self.params([self.key(intent={'kind': 'key', 'keys': ['a']}, timeout_ms=500)],
                                             action_id='keyboard', base_rev=self.w.cache.rev))
        self.assertEqual(result['status'], 'success')
        self.assertEqual(self.evdev.health['keyboard'], 'tested')
        keyboard = [req for req in self.log if req.get('dev') == 'kbd']
        self.assertEqual(keyboard[-1]['frames'], [[[1, 31, 1]], [[1, 31, 0]]])

    def test_refresh_wait_respects_step_deadline_without_dispatch_or_down(self):
        self.establish_for_refresh()
        callbacks = []
        self.evdev.devices.side_effect = callbacks.append
        self.evdev.events([self.settlement_event()])
        result = self.run_action(self.params([self.move(30, 2, timeout_ms=50)]))
        self.assertEqual(result['reason'], 'timeout')
        self.assertEqual(result['dispatch_status'], 'not_dispatched')
        self.assertEqual(self.evdev.health['state'], 'up')
        self.assertFalse(any('frames' in req for req in self.log))
        self.evdev.devices.side_effect = lambda cb: cb(self.assigned_devices())
        for cb in callbacks:
            cb(self.assigned_devices())
        self.finish_refresh()

    def test_unrelated_hierarchy_and_mapping_pointer_do_nothing(self):
        from Xlib import X
        from Xlib.ext import xinput
        self.establish_for_refresh()
        event = self.settlement_event(True)
        event.data = {'flags': xinput.SlaveRemoved, 'info': [
            {'deviceid': 99, 'flags': xinput.SlaveRemoved, 'enabled': 0},
            {'deviceid': 9, 'flags': 0, 'enabled': 1}]}
        self.evdev.events([event, NS(type=X.MappingNotify, request=X.MappingPointer)])
        for _ in range(100):
            self.tick()
        self.assertEqual(self.evdev.health['state'], 'up')
        self.assertEqual(self.evdev.recompiles, 0)
        self.assertEqual(self.evdev.revision, 0)
        self.assertIsNone(self.evdev.recompile_runner)
        self.evdev.devices.assert_not_called()
        self.assertFalse(self.log)

    def test_assigned_disable_from_wire_dict_retires(self):
        from Xlib.ext import xinput
        self.establish_for_refresh()
        event = self.settlement_event(True)
        event.data = {'info': [{'deviceid': 10, 'flags': xinput.DeviceDisabled, 'enabled': 0}]}
        self.evdev.events([event])
        self.assertEqual(self.evdev.health['last_error'], 'assigned_device_changed')
        self.assertIsNone(self.evdev.sock)
        self.evdev.devices.assert_not_called()

    def test_refresh_replacement_snapshot_retires(self):
        self.establish_for_refresh()
        self.slave_ids['keyboard'] = 19
        self.evdev.events([self.settlement_event()])
        self.finish_refresh()
        self.assertEqual(self.evdev.health['last_error'], 'assigned_device_changed')
        self.assertEqual(self.evdev.sources['keyboard'], 9)

    def test_refresh_failures_keep_honest_causes(self):
        for failure in ('discovery', 'compile', 'grab'):
            with self.subTest(failure=failure):
                self.setUp()
                self.establish_for_refresh()
                self.evdev.provided_keycodes = None
                if failure == 'discovery':
                    self.evdev.devices.side_effect = lambda cb: cb(None)
                if failure == 'grab':
                    self.helper.grab = lambda dev: 'free'
                mapping = dict(self.evdev.keycodes)
                self.evdev.events([self.settlement_event()])
                with patch('worker_evdev.compile_keycodes', side_effect=OSError('map') if failure == 'compile' else None,
                           return_value=mapping):
                    self.finish_refresh()
                self.assertEqual(self.evdev.health['last_error'], {
                    'discovery': 'assigned_slaves_unavailable', 'compile': 'session_keymap_unavailable',
                    'grab': 'no_exclusive_grab'}[failure])
                self.assertEqual(self.evdev.recompiles, 0)

    def test_refresh_notification_during_final_barrier_discards_candidate(self):
        self.establish_for_refresh()
        original = self.t.barrier
        barriers = []
        def barrier(cb):
            barriers.append(True)
            if len(barriers) == 2:
                self.evdev.events([self.settlement_event()])
            original(cb)
        self.t.barrier = barrier
        self.evdev.provided_keycodes = None
        mapping = dict(self.evdev.keycodes)
        self.evdev.events([self.settlement_event()])
        with patch('worker_evdev.compile_keycodes', side_effect=[dict(mapping), mapping]) as compile_map:
            self.finish_refresh()
        self.assertEqual(compile_map.call_count, 2)
        self.assertEqual(self.evdev.recompiles, 1)
        self.assertEqual(self.evdev.map_epoch, 0)
        self.assertEqual(self.evdev.health['state'], 'up')

    def test_map_change_between_macro_groups_does_not_send_stale_suffix(self):
        self.establish_for_refresh()
        mapping = dict(self.evdev.keycodes)
        mapping[38] = 31
        self.evdev.provided_keycodes = None
        handle = self.helper.handle
        def reply(req):
            result = handle(req)
            if 'frames' in req and not self.evdev.dirty:
                self.evdev.events([self.settlement_event()])
            return result
        self.evdev.connector = lambda: MemorySocket(reply, self.log, fragment=65536)
        self.evdev.close()
        self.e.route = 'evdev'
        test = self.t.test_window(True)
        self.addCleanup(self.t.end_test, test, self.w.cache.focus)
        r = dict(id='manual', attempts=[], rev={'dispatched': None}, dispatch_status='not_dispatched')
        runner = self.e.send_evdev({}, r, [('key_down', 38), ('key_up', 38), ('motion', 0, 30, 2)], diagnostic=True)
        with patch('worker_evdev.compile_keycodes', return_value=mapping):
            with self.assertRaises(ActError) as caught:
                for _ in range(3000):
                    self.evdev.tick()
                    next(runner)
                    self.now += .001
        self.assertEqual(caught.exception.reason, 'provider_degraded', (self.evdev.health, self.log))
        self.assertEqual(sum('frames' in req for req in self.log), 1)
        self.assertEqual(self.evdev.health['state'], 'up')
        self.assertEqual(self.evdev.health['keyboard'], 'untested')

    def test_old_verified_keyboard_step_cannot_promote_adopted_map(self):
        self.establish_for_refresh()
        self.evdev.health['keyboard'] = 'tested'
        mapping = dict(self.evdev.keycodes)
        mapping[38] = 31
        self.evdev.provided_keycodes = None
        def changed(event):
            if event == ('key_up', 68):
                self.evdev.events([self.settlement_event()])
        self.t.on_write = changed
        self.submit(self.params([self.key(timeout_ms=500)]))
        with patch('worker_evdev.compile_keycodes', return_value=mapping):
            for _ in range(100):
                self.tick()
            self.assertEqual(self.evdev.map_epoch, 1)
            self.later()
            self.drain()
        self.assertEqual(self.result()['status'], 'success')
        self.assertEqual(self.evdev.health['keyboard'], 'untested')

    def test_shutdown_drains_refresh_grab_before_release(self):
        self.establish_for_refresh()
        sock = PausedSocket(self.helper.handle, self.log)
        self.evdev.close()
        self.evdev.connector = lambda: sock
        self.evdev.events([self.settlement_event()])
        for _ in range(100):
            self.tick()
            if sock.blocked:
                break
        self.assertTrue(self.evdev.refresh_inflight)
        self.e.holds['evdev']['key'].add(38)
        self.assertFalse(self.e.stopping_tick())
        self.assertEqual(len(self.log), 1)
        sock.paused = False
        for _ in range(100):
            if self.e.stopping_tick():
                break
            self.now += .001
        else:
            self.fail('shutdown hung on cache refresh')
        self.assertEqual([req.get('op') for req in self.log], ['grab_check', 'release_all'])
        self.assertIsNone(self.evdev.recompile_runner)
        self.assertFalse(self.evdev.inflight)

    def test_shutdown_cancels_refresh_waiting_on_discovery(self):
        self.establish_for_refresh()
        self.evdev.devices.side_effect = lambda cb: None
        self.evdev.events([self.settlement_event()])
        for _ in range(60):
            self.tick()
        self.evdev.devices.assert_called_once()
        self.assertTrue(self.e.stopping_tick())
        self.assertIsNone(self.evdev.recompile_runner)
        self.assertFalse(self.log)

    def test_changed_map_during_diagnostic_restarts_private_test_without_retirement(self):
        self.w.cache.windows.clear()
        mapping = dict(self.evdev.keycodes)
        mapping[38] = 31  # Shift's diagnostic frames remain valid on the fake server.
        self.evdev.provided_keycodes = None
        test_window = self.t.test_window
        windows = []
        def window(keyboard):
            windows.append(True)
            if len(windows) == 1:
                self.evdev.events([self.settlement_event()])
            return test_window(keyboard)
        self.t.test_window = window
        self.e.start_selftest()
        # Establish the original map, then compile the changed one.
        with patch('worker_evdev.compile_keycodes', side_effect=[dict(self.evdev.keycodes), mapping]):
            self.drain()
        self.assertEqual(self.evdev.recompiles, 1)
        self.assertEqual(self.evdev.map_epoch, 1)
        self.assertEqual(self.evdev.health, dict(state='up', grab=True, keyboard='tested', last_error=None))
        self.assertEqual(len(windows), 3)  # Cancelled evdev, restarted evdev, XTEST.
        self.assertEqual(sum(len(req.get('frames', [])) for req in self.log), 200)

    def test_continuous_post_establishment_settlement_is_bounded(self):
        self.establish_for_refresh()
        for _ in range(105):
            self.evdev.events([self.settlement_event()])
            for _ in range(20):
                self.tick()
        self.finish_refresh()
        self.assertEqual(self.evdev.health['last_error'], 'session_settlement_timeout')
        self.assertEqual(self.evdev.health['state'], 'down')
        self.assertEqual(self.evdev.recompiles, 0)
        self.evdev.devices.assert_not_called()
        self.assertFalse(self.log)

    def test_a2_two_full_map_broadcasts_140ms_apart_during_selftest(self):
        from Xlib import X
        self.w.cache.windows.clear()
        sock = PausedSocket(self.helper.handle, self.log)
        recv = sock.recv
        sock.recv = lambda size: recv(size) if 'frames' in self.log[-1] else MemorySocket.recv(sock, size)
        self.evdev.connector = lambda: sock
        # Keep helper gap timing independent of the fake owner loop's clock.
        self.helper.sleep = lambda _: None
        notifications = []
        test_window = self.t.test_window
        def window(keyboard):
            if notifications:
                return test_window(keyboard)
            self.assertTrue(self.evdev.established)
            self.evdev.events([NS(type=X.MappingNotify, request=X.MappingKeyboard, first_keycode=8, count=248)])
            notifications.append(self.now)
            return test_window(keyboard)
        self.t.test_window = window
        self.e.start_selftest()
        for _ in range(3000):
            self.tick()
            if len(notifications) == 1 and self.now >= notifications[0] + .140:
                self.assertIsNotNone(self.e.selftest)
                self.assertEqual(self.evdev.recompiles, 1)
                self.evdev.events([NS(type=X.MappingNotify, request=X.MappingModifier, first_keycode=0, count=0)])
                notifications.append(self.now)
                sock.paused = False
            if len(notifications) == 2 and self.e.selftest is None:
                break
        self.assertEqual(len(notifications), 2)
        self.assertAlmostEqual(notifications[1] - notifications[0], .140, places=3)
        self.assertEqual(self.evdev.recompiles, 2)
        self.assertEqual(self.evdev.health, dict(state='up', grab=True, keyboard='tested', last_error=None))
        self.assertEqual(self.e.channel['state'], 'ok')
        self.assertEqual(sum(len(req.get('frames', [])) for req in self.log), 200)
        self.assertEqual(len(self.t.log), 400)
        self.assertEqual(self.t.test_presses, 50)
        self.assertEqual(self.evdev.map_epoch, 0)

    def assert_xtest_allows_worker_ready(self, reason):
        self.assertEqual(self.evdev.health['state'], 'down')
        self.assertEqual(self.evdev.health['last_error'], reason)
        self.assertEqual(self.e.channel['state'], 'ok')
        self.assertIsNone(self.e.selftest)
        # Exercise the production channel_test -> ready transition, without
        # constructing an X connection or publishing any real worker endpoint.
        state = base.w.XState.__new__(base.w.XState)
        state.w, state.phase = self.w, 'channel_test'
        self.w.ready = Mock()
        state.advance()
        self.assertEqual(state.phase, 'ready')
        self.w.ready.assert_called_once_with()

    def test_startup_assigned_removal_during_evdev_receive_continues_xtest(self):
        from Xlib import X
        # The real worker ingests X events between successive Engine.tick calls.
        self.w.x = NS(ev=NS(display=NS(get_extension_major=lambda name: 131)))
        sock = PausedSocket(self.helper.handle, self.log)
        # Permit startup grab_check, pause only once the first event request
        # has reached recv/EAGAIN. Cleanup can open a fresh stream afterwards.
        original_recv = sock.recv
        sock.recv = lambda size: original_recv(size) if 'frames' in self.log[-1] else MemorySocket.recv(sock, size)
        self.evdev.connector = Mock(side_effect=[sock, MemorySocket(self.helper.handle, self.log, fragment=65536)])
        self.w.cache.windows.clear()
        self.e.start_selftest()
        for _ in range(1000):
            self.tick()
            if sock.blocked:
                break
        self.assertTrue(sock.blocked)
        self.assertTrue(self.evdev.inflight)
        self.assertEqual(self.log[0]['op'], 'grab_check')
        self.evdev.events([self.assigned_removal()])
        self.assertIsNone(self.evdev.sock)
        self.drain()
        self.assert_xtest_allows_worker_ready('assigned_device_changed')
        self.assertFalse(self.evdev.inflight)
        self.assertEqual(sum('frames' in req for req in self.log), 1)
        self.assertEqual(self.t.test_presses, 50)
        self.assertEqual(self.t.position, {'x': 2, 'y': 2})
        self.assertIn('release_all', [req.get('op') for req in self.log])

    def test_startup_hierarchy_invalidation_after_establishment_continues_xtest(self):
        from Xlib.ext import ge, xinput
        self.w.x = NS(ev=NS(display=NS(get_extension_major=lambda name: 131)))
        sock = PausedSocket(self.helper.handle, self.log)
        original_recv = sock.recv
        sock.recv = lambda size: original_recv(size) if 'frames' in self.log[-1] else MemorySocket.recv(sock, size)
        self.evdev.connector = Mock(return_value=sock)
        self.e.start_selftest()
        for _ in range(1000):
            self.tick()
            if sock.blocked:
                break
        self.assertTrue(sock.blocked)
        self.assertTrue(self.evdev.established)
        self.assertTrue(self.evdev.inflight)
        self.evdev.events([self.assigned_removal()])
        self.drain()
        self.assert_xtest_allows_worker_ready('assigned_device_changed')
        self.evdev.connector.assert_called_once()  # Invalidation never reconnects a retired channel.
        self.assertEqual(sum('frames' in req for req in self.log), 1)

    def test_unexpected_event_transport_fault_preserves_cause_and_runs_xtest(self):
        self.w.cache.windows.clear()
        self.evdev.connector = self.event_fault_socket
        self.e.start_selftest()
        self.drain()
        self.assert_xtest_allows_worker_ready('helper_transport_error:RuntimeError')
        self.assertEqual(self.t.test_presses, 50)
        self.assertEqual(len(self.t.log), 200)  # The complete XTEST diagnostic.
        self.assertFalse(self.evdev.inflight)
        self.assertEqual(self.evdev.reconnects, 1)

    def test_unexpected_connector_fault_still_completes_startup(self):
        self.evdev.connector = Mock(side_effect=RuntimeError('fake connector failure'))
        self.e.start_selftest()
        self.drain()
        self.assert_xtest_allows_worker_ready('helper_transport_error:RuntimeError')
        self.assertEqual(len(self.t.log), 100)
        self.assertEqual(self.evdev.connector.call_count, 2)

    def event_fault_socket(self):
        sock = MemorySocket(self.helper.handle, self.log, fragment=65536)
        send = sock.send
        def fail_events(data):
            if 'frames' in json.loads(data[4:]):
                raise RuntimeError('fake batch send failure')
            return send(data)
        sock.send = fail_events
        return sock

    def test_unexpected_evdev_zero_write_fault_advances_to_xtest(self):
        self.evdev.connector = self.event_fault_socket
        r = self.run_action(self.params([self.move(100, 2), self.key()]))
        first = r['steps'][0]
        self.assertEqual((first['reason'], first['dispatch_status'], first['written']),
                         (None, 'accepted', 0))
        self.assertEqual([v['route'] for v in first['attempts']], ['evdev', 'xtest'])
        self.assertEqual(first['attempts'][0]['error'], 'helper_transport_error:RuntimeError')
        self.assertEqual(first['effect']['status'], 'verified')
        self.assertEqual(self.evdev.health['last_error'], 'helper_transport_error:RuntimeError')
        self.assertEqual(self.evdev.health['state'], 'down')

    def test_unexpected_reply_fault_mid_batch_is_unknown_without_duplicate_input(self):
        def connect():
            sock = MemorySocket(self.helper.handle, self.log, fragment=65536)
            recv = sock.recv
            def fail_event_reply(size):
                if 'frames' in self.log[-1]:
                    raise RuntimeError('fake event receive failure')
                return recv(size)
            sock.recv = fail_event_reply
            return sock
        self.evdev.connector = connect
        r = self.run_action(self.params([self.move(100, 2), self.key()]))
        first = r['steps'][0]
        self.assertEqual((first['reason'], first['dispatch_status']), ('channel_down', 'unknown'))
        self.assertEqual(first['attempts'][0]['error'], 'helper_transport_error:RuntimeError')
        self.assertEqual(r['steps'][1]['reason'], 'not_reached')
        frames = [req for req in self.log if 'frames' in req]
        self.assertEqual(len(frames), 2)
        self.assertEqual(frames[0], frames[1])
        self.assertEqual(self.frame_count, 3)  # Retried reply, each actual frame written only once.
        self.assertEqual(self.evdev.health['state'], 'down')
        self.assertEqual(r['advice'], 'do_not_retry')

    def test_grab_fail_routes_to_xtest_without_evdev_frames(self):
        self.helper.grab = lambda d: 'free'
        r = self.run_action()
        self.assertEqual(r['status'], 'success')
        self.assertEqual(r['steps'][0]['route'], 'xtest')
        self.assertEqual(self.evdev.health['last_error'], 'no_exclusive_grab')
        self.assertFalse(any('frames' in req for req in self.log))

    def test_partial_receipt_written_prefix_no_retry_and_no_later_step(self):
        # motion splits into three frames; kill before the second frame.
        self.fail_frame = 1
        r = self.run_action(self.params([self.move(100, 2), self.key()]))
        s = r['steps'][0]
        self.assertEqual(s['reason'], 'channel_down')
        self.assertEqual(s['dispatch_status'], 'accepted')
        self.assertEqual(s['written'], 1)
        self.assertEqual(s['attempts'][0]['frames_written'], 1)
        self.assertEqual(self.evdev.health['state'], 'down')
        self.assertEqual(sum('frames' in req for req in self.log), 1)
        self.assertTrue(any(req.get('op') == 'release_all' for req in self.log))
        self.assertEqual(r['steps'][1]['reason'], 'not_reached')

    def test_system_info_health_fields(self):
        health = self.w.channels()['evdev']
        self.assertEqual(set(health), {'state', 'grab', 'keyboard', 'last_error', 'recompiles'})
        self.assertEqual(health['state'], 'up')

    def check_grab_lost_batch(self, written):
        self.helper.frame_grab = Mock(side_effect=['busy']*written + ['free'])
        r = self.run_action(self.params([self.move(100, 2), self.key()]))
        step = r['steps'][0]
        self.assertEqual((step['reason'], step['dispatch_status'], step['written']),
                         ('channel_down' if written else None, 'accepted', written))
        self.assertEqual(step['attempts'][0]['frames_written'], written)
        self.assertEqual(step['attempts'][0]['error'], 'grab_lost')
        if written:
            self.assertEqual(r['steps'][1]['reason'], 'not_reached')
        else:
            self.assertEqual([v['route'] for v in step['attempts']], ['evdev', 'xtest'])
            self.assertEqual(step['effect']['status'], 'verified')
        self.assertEqual(self.w.channels()['evdev']['last_error'], 'grab_lost')
        self.assertEqual(self.evdev.health['state'], 'down')
        self.assertFalse(self.evdev.health['grab'])
        self.assertEqual(sum('frames' in req for req in self.log), 1)
        self.assertIn('release_all', [req.get('op') for req in self.log])
        self.assertEqual(self.evdev.reconnects, 0)
        self.assertEqual(self.w.counters['retries'], 0)
        if written:
            self.assertEqual(r['advice'], 'do_not_retry')

    def test_grab_lost_partial_batch_never_replays(self):
        self.check_grab_lost_batch(1)

    def test_grab_lost_zero_batch_advances_to_xtest(self):
        self.check_grab_lost_batch(0)

    def application_release(self):
        self.t.on_write = lambda event: self.later() if event[0] == 'button_up' else None

    def test_click_chord_uses_pointer_and_keyboard_frames(self):
        self.application_release()
        step = self.key(intent={'kind': 'click', 'at': {'point': {'space': 'desktop', 'x': 20, 'y': 20}},
                                'modifiers': ['Control_L'], 'button': 'left', 'count': 1})
        r = self.run_action(self.params([step]))
        self.assertEqual(r['status'], 'success')
        self.assertEqual(r['steps'][0]['route'], 'evdev')
        self.assertIn(('button_down', 1), self.t.log)
        self.assertFalse(self.t.down | self.t.buttons)
        self.assertEqual({req['dev'] for req in self.log if 'dev' in req}, {'kbd', 'ptr'})

    def test_scroll_emits_wheel_frame_and_raw_button_pair(self):
        self.application_release()
        step = self.key(intent={'kind': 'scroll', 'dy': -1, 'dx': 0, 'at': {'point': {'space': 'desktop', 'x': 20, 'y': 20}}})
        r = self.run_action(self.params([step]))
        self.assertEqual(r['status'], 'success')
        self.assertEqual(r['steps'][0]['route'], 'evdev')
        self.assertTrue(any(req.get('frames') == [[[2, 8, 1]]] for req in self.log))
        self.assertEqual(r['steps'][0]['attempts'][-1]['input_groups'][-1]['server_ack']['seen'], 2)

    def test_drag_frames_release_and_reach_endpoint(self):
        self.application_release()
        step = self.key(intent={'kind': 'drag', 'from': {'point': {'space': 'desktop', 'x': 20, 'y': 20}},
                                'to': {'point': {'space': 'desktop', 'x': 100, 'y': 100}}, 'duration_ms': 50}, timeout_ms=500)
        r = self.run_action(self.params([step]))
        self.assertEqual(r['status'], 'success')
        self.assertEqual(self.t.position, {'x': 100, 'y': 100})
        self.assertFalse(self.t.buttons)
        self.assertTrue(all(req.get('gap_us') == 0 for req in self.log if req.get('dev') == 'ptr'))

    def test_text_pacing_and_first_verified_keyboard_marks_tested(self):
        self.t.on_write = lambda event: self.later() if event == ('key_up', 38) else None
        step = self.key(intent={'kind': 'text', 'text': 'A', 'replace': False})
        r = self.run_action(self.params([step]))
        self.assertEqual(r['status'], 'success')
        self.assertEqual(self.evdev.health['keyboard'], 'tested')
        self.assertTrue(all(req['gap_us'] == 2000 for req in self.log if req.get('dev') == 'kbd'))
        self.assertEqual(self.t.log, [('key_down', 50), ('key_down', 38), ('key_up', 38), ('key_up', 50)])

    def test_gate_closes_with_shift_held_release_is_confirmed(self):
        self.t.on_write = lambda event: self.w.cache.control.update(state='paused', epoch=2) if event == ('key_down', 50) else None
        step = self.key(intent={'kind': 'text', 'text': 'A', 'replace': False})
        r = self.run_action(self.params([step]))
        self.assertEqual(r['steps'][0]['dispatch_status'], 'accepted')
        self.assertEqual(r['steps'][0]['written'], 1)
        self.assertEqual(self.t.log, [('key_down', 50), ('key_up', 50)])
        self.assertTrue(r['steps'][0]['cleanup']['confirmed'])
        self.assertEqual(self.evdev.health['state'], 'down')

    def test_all_channels_failing_does_not_start(self):
        self.ack_evdev = self.t.ack = False
        self.e.start_selftest()
        with self.assertRaisesRegex(RuntimeError, 'all_input_channels_down'):
            self.drain()

    def test_quiesce_evdev_closes_only_agent_gate(self):
        path = self.w.sd/'control'
        for state in (0, 1, 2, 3):
            path.write_bytes(PAGE.pack(b'ARKC', 1, bytes.fromhex(GEN), 4, state, 0, 0, bytes(16)))
            self.w.quiesce_evdev()
            value = base.w.decode_control(path.read_bytes(), GEN)
            self.assertEqual(value['state'], 1 if state == 0 else state)
            self.assertEqual(value['epoch'], 4)



if __name__ == '__main__':
    unittest.main()
