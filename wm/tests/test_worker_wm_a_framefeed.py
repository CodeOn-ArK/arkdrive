"""WM-A: socketpair framing, injected bind/credentials, fake X/input only."""
import copy
import hashlib
import json
import os
import selectors
import select
from pathlib import Path
import socket
import struct
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from test_worker_w1 import window
from test_worker_w2 import Harness, w
import test_worker_w3 as pixel_fixture

decode, rgb = pixel_fixture.decode, pixel_fixture.rgb
from worker_framefeed import FrameFeed, FeedClient, FRAME_COUNT, FRAME_BYTES, CLIENT_COUNT
from worker_pixels import Pixels, Shadow


class PairSocket:
    """Real AF_UNIX stream FD; sandbox permits read/write, denies recv/send."""
    def __init__(self, sock):
        self.sock = sock

    def fileno(self):
        return self.sock.fileno()

    def setblocking(self, value):
        os.set_blocking(self.fileno(), value)

    def getsockopt(self, *args):
        return struct.pack('3i', os.getpid(), os.getuid(), os.getgid())

    def send(self, data):
        return os.write(self.fileno(), data)

    def sendall(self, data):
        while data:
            data = data[self.send(data):]

    def recv(self, size):
        if not select.select([self.fileno()], [], [], 1)[0]:
            raise socket.timeout()
        return os.read(self.fileno(), size)

    def close(self):
        self.sock.close()


def start_feed(test, feed):
    # A pipe gives the selector a real FD for the injected listener. Bind and
    # SO_PEERCRED are blocked here; those kernel checks remain Raj's live gate.
    read_fd, write_fd = os.pipe()
    test.addCleanup(os.close, write_fd)
    listener = Mock()
    listener.fileno.return_value = read_fd
    listener.bind.side_effect = lambda path: Path(path).touch()
    listener.close.side_effect = lambda: os.close(read_fd)
    with patch('worker_framefeed.socket.socket', return_value=listener):
        feed.start()
    return listener


def pair_client(test, feed):
    start_feed(test, feed)
    left, right = socket.socketpair()
    client, server = PairSocket(left), PairSocket(right)
    test.addCleanup(client.close)
    feed.listener.accept.return_value = (server, None)
    feed.accept()
    return client, next(iter(feed.clients))


class FeedCase(unittest.TestCase):
    owner = pixel_fixture.PixelCase.owner
    reply_capture = pixel_fixture.PixelCase.reply_capture
    mutate = pixel_fixture.PixelCase.mutate
    damage = pixel_fixture.PixelCase.damage

    def setUp(self):
        pixel_fixture.PixelCase.setUp(self)
        self.w.selector = selectors.DefaultSelector()
        self.addCleanup(self.w.selector.close)
        self.w.cache.set('pointer', dict(x=21, y=25))
        self.w.cache.commit()
        self.feed = FrameFeed(self.w)
        self.addCleanup(self.feed.close)

    def publish(self):
        self.feed.publish()

    def test_no_settled_frame_and_new_window_return_no_frame_yet(self):
        self.assertEqual(self.feed.query({})['status'], 'no_frame_yet')
        self.publish()
        self.w.cache.upsert(11, window(xid=11))
        new_handle = self.w.cache.handle(11)
        reply = self.feed.query(dict(scope=dict(kind='window', window=new_handle)))
        self.assertEqual(reply['status'], 'no_frame_yet')
        self.assertNotIn('frame', reply)
        self.assertFalse(self.feed.ring.entries)

    def test_native_desktop_and_window_pngs_cursor_and_content_ids(self):
        self.publish()
        desktop = self.feed.query({})
        frame = desktop['frame']
        self.assertEqual(desktop['cursor'], dict(position=dict(x=21, y=25), image=None, hotspot=None))
        self.assertEqual((desktop['status'], desktop['changed']), ('frame', True))
        self.assertEqual(frame['rect'], self.p.bounds)
        self.assertEqual(frame['scale'], 1)
        path = Path(frame['path'])
        self.assertEqual(frame['frame'], 'f:' + hashlib.sha256(path.read_bytes()).hexdigest())
        width, height, rows = decode(path)
        self.assertEqual((width, height), (192, 128))
        self.assertEqual(rows[14][30:33], rgb(10, 14))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        value = self.feed.query(dict(scope=dict(kind='window', window=self.handle)))['frame']
        self.assertEqual(value['rect'], dict(x=7, y=9, w=90, h=70))
        width, height, rows = decode(value['path'])
        self.assertEqual((width, height), (90, 70))
        self.assertEqual(rows[0][:3], rgb(7, 9))
        self.assertEqual(self.feed.ring.directory.stat().st_mode & 0o777, 0o700)

    def test_unchanged_has_no_artifact_and_neither_encodes_nor_adds_ring_entry(self):
        self.publish()
        first = self.feed.query({})
        before = (self.feed.ring.used, list(self.feed.ring.entries))
        with patch.object(Shadow, 'png', side_effect=AssertionError('must not encode')):
            reply = self.feed.query(dict(since_rev=first['rev']))
        self.assertEqual(reply['status'], 'unchanged')
        self.assertFalse(reply['changed'])
        self.assertEqual(set(reply), {'session', 'generation', 'rev', 'status', 'changed'})
        self.assertEqual(before, (self.feed.ring.used, list(self.feed.ring.entries)))

    def test_queries_never_capture_and_keep_previous_immutable_settlement(self):
        self.publish()
        rev = self.feed.rev
        self.mutate(7, 9)
        self.damage(dict(x=7, y=9, w=1, h=1))
        self.w.cache.set('pointer', dict(x=50, y=51))
        self.w.cache.windows[10]['geometry'] = dict(x=30, y=30, w=70, h=60)
        self.w.cache.commit()
        self.publish()  # Dirty mmap must not be published.
        self.w.x.ax.ask.side_effect = AssertionError('feed issued X traffic')
        self.w.settle_pixels = Mock(side_effect=AssertionError('feed settled pixels'))
        reply = self.feed.query(dict(scope=dict(kind='window', window=self.handle)))
        self.assertEqual(reply['rev'], rev)
        self.assertEqual(reply['frame']['rect'], dict(x=7, y=9, w=90, h=70))
        self.assertEqual(decode(reply['frame']['path'])[2][0][:3], rgb(7, 9))
        self.assertEqual(self.feed.query({})['cursor']['position'], dict(x=21, y=25))
        self.w.x.ax.ask.assert_not_called()
        self.w.settle_pixels.assert_not_called()
        self.p.settle()  # Existing owner settlement; no capture required for mmap.
        self.publish()
        latest = self.feed.query(dict(since_rev=rev))
        self.assertTrue(latest['changed'])
        self.assertGreater(latest['rev'], rev)
        self.assertEqual(latest['cursor']['position'], dict(x=50, y=51))
        self.assertEqual(decode(latest['frame']['path'])[2][9][21:24], b'\x12\x34\x56')

    def test_pending_failed_and_uncovered_pixels_do_not_publish(self):
        for field, value in [('pending', True), ('failure', 'capture_failed')]:
            old = getattr(self.p, field)
            setattr(self.p, field, value)
            self.publish()
            self.assertIsNone(self.feed.rev)
            setattr(self.p, field, old)
        self.w.covered = lambda: False
        self.publish()
        self.assertEqual(self.feed.query({})['status'], 'no_frame_yet')

    def test_getimage_settlement_is_owned_by_existing_pixels_only(self):
        self.w.pixels = Pixels(self.w, Shadow(192, 128))
        pixels = self.w.pixels
        self.publish()
        self.assertEqual(self.feed.query({})['status'], 'no_frame_yet')
        self.w.x.ax.ask.assert_not_called()
        self.assertFalse(pixels.settle())
        self.assertTrue(pixels.pending)
        self.publish()
        self.assertEqual(self.feed.query({})['status'], 'no_frame_yet')
        self.assertEqual(self.w.x.ax.ask.call_count, 1)
        callback = self.w.x.ax.ask.call_args.args[1]
        callback(dict(depth=24, data=bytes(192*128*4)))
        self.publish()
        self.assertEqual(self.feed.query({})['status'], 'frame')
        self.assertEqual(self.w.x.ax.ask.call_count, 1)

    def test_cursor_only_revision_uses_existing_pixels(self):
        self.publish()
        snapshot, rev = self.feed.snapshot, self.feed.rev
        self.w.cache.set('pointer', dict(x=4, y=5))
        self.w.cache.commit()
        self.publish()
        self.assertIs(self.feed.snapshot, snapshot)
        reply = self.feed.query(dict(since_rev=rev))
        self.assertTrue(reply['changed'])
        self.assertEqual(reply['cursor']['position'], dict(x=4, y=5))

    def test_write_verbs_and_unknown_methods_are_refused(self):
        self.publish()
        for method in ('session.attach', 'session.detach', 'window.act', 'input.move',
                       'frames.put', 'system.info', 'unknown'):
            reply = self.feed.message(json.dumps(dict(jsonrpc='2.0', id=1, method=method)).encode())
            self.assertEqual(reply['error']['code'], -32601)
            self.assertEqual(reply['error']['data']['reason'], 'read_only')
        self.assertFalse(self.feed.ring.entries)

    def test_invalid_queries_and_generation_fencing(self):
        for params in ([], {'lease': 'x'}, {'since_rev': True}, {'since_rev': -1},
                       {'scope': {'kind': 'family'}}, {'scope': {'kind': 'session', 'window': 'x'}},
                       {'scope': {'kind': 'window', 'window': 7}}):
            reply = self.feed.message(json.dumps(dict(jsonrpc='2.0', id=1, method='frames.get', params=params)).encode())
            self.assertEqual(reply['error']['data']['reason'], 'invalid_params')
        reply = self.feed.message(json.dumps(dict(jsonrpc='2.0', id=2, method='frames.get',
                                                 params={'generation': 'old'})).encode())
        self.assertEqual(reply['error']['data']['reason'], 'stale_generation')
        self.assertEqual(self.feed.message(b'not json')['error']['code'], -32700)

    def connect(self):
        return pair_client(self, self.feed)

    def request(self, sock, client, method='frames.get', params=None):
        payload = json.dumps(dict(jsonrpc='2.0', id=1, method=method, params=params or {})).encode()
        sock.sendall(struct.pack('!I', len(payload)) + payload)
        self.feed.io(client, selectors.EVENT_READ)
        self.feed.io(client, selectors.EVENT_WRITE)
        size = struct.unpack('!I', sock.recv(4))[0]
        data = bytearray()
        while len(data) < size:
            data.extend(sock.recv(size - len(data)))
        return json.loads(data)

    def test_socketpair_framing_permissions_no_driver_peers_and_close(self):
        self.publish()
        self.w.peers = set()
        sock, client = self.connect()
        self.assertIsInstance(client, FeedClient)
        self.assertEqual(self.feed.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.request(sock, client)['result']['status'], 'frame')
        self.assertEqual(self.request(sock, client, params={'since_rev': self.feed.rev})['result']['status'], 'unchanged')
        self.assertEqual(self.request(sock, client, 'window.act')['error']['code'], -32601)
        self.assertFalse(self.w.peers)
        self.assertFalse(hasattr(client, 'retire_at'))
        self.feed.close()
        self.assertFalse(self.feed.path.exists())
        self.assertFalse(self.feed.clients or self.w.selector.get_map())

    def test_socketpair_fragmentation_and_backpressure(self):
        sock, client = self.connect()
        wire = w.frame(dict(jsonrpc='2.0', id=2, method='frames.get'))
        sock.sendall(wire[:3])
        self.feed.io(client, selectors.EVENT_READ)
        self.assertFalse(client.outgoing)
        sock.sendall(wire[3:])
        self.feed.io(client, selectors.EVENT_READ)
        self.assertEqual(self.w.selector.get_key(client.sock).events, selectors.EVENT_WRITE)
        self.assertTrue(client.outgoing)
        self.feed.io(client, selectors.EVENT_WRITE)
        self.assertEqual(self.w.selector.get_key(client.sock).events, selectors.EVENT_READ)

    def test_socket_rejects_oversize_input(self):
        sock, client = self.connect()
        sock.sendall(struct.pack('!I', 8193))
        self.feed.io(client, selectors.EVENT_READ)
        self.assertFalse(self.feed.clients)

    def test_socket_rejects_foreign_uid(self):
        start_feed(self, self.feed)
        fake = Mock()
        fake.getsockopt.return_value = struct.pack('3i', 100, os.getuid() + 1, 100)
        with patch.object(self.feed, 'listener') as listener:
            listener.accept.return_value = (fake, None)
            self.feed.accept()
        fake.close.assert_called_once()
        self.assertFalse(self.feed.clients)

    def test_connection_cap_and_credential_failure_are_feed_local(self):
        start_feed(self, self.feed)
        self.w.peers = set()
        for _ in range(CLIENT_COUNT + 1):
            left, right = socket.socketpair()
            self.addCleanup(left.close)
            self.addCleanup(right.close)
            server = PairSocket(right)
            self.feed.listener.accept.return_value = (server, None)
            self.feed.accept()
        self.assertEqual(len(self.feed.clients), CLIENT_COUNT)
        self.assertEqual(right.fileno(), -1)
        self.assertFalse(self.w.peers)
        server = Mock()
        server.getsockopt.side_effect = OSError('credentials unavailable')
        self.feed.listener.accept.return_value = (server, None)
        self.feed.accept()
        server.close.assert_called_once()
        self.assertEqual(len(self.feed.clients), CLIENT_COUNT)

    def test_ring_count_and_bytes_evict_fifo_without_driver_store_changes(self):
        frames = self.p.frames
        driver = self.p.frame(self.p.bounds, list(self.w.cache.windows.values()))
        frames.pins.add(driver['path'].rsplit('/', 1)[-1])
        before = copy.deepcopy(frames.__dict__)
        ring = self.feed.ring
        self.assertEqual((ring.count, ring.quota), (FRAME_COUNT, FRAME_BYTES))
        paths = []
        for i in range(FRAME_COUNT + 1):
            png = self.p.shadow.png(dict(x=i, y=0, w=1, h=1), 1)
            value = ring.put((i, None), png, dict(rev=i + 1))
            paths.append(value['path'])
        self.assertFalse(Path(paths[0]).exists())
        self.assertTrue(Path(paths[1]).exists())
        self.assertEqual(len(ring.entries), FRAME_COUNT)
        self.assertEqual(ring.used, sum(p.stat().st_size for p in ring.directory.glob('*.png')))
        # Re-reading the second entry must not make it newest.
        ring.put((1, None), b'ignored', {})
        ring.quota = ring.used  # Force byte pressure even with a spare count slot.
        ring.count += 10
        ring.put(('new', None), self.p.shadow.png(dict(x=0, y=1, w=2, h=1), 1), {})
        self.assertFalse(Path(paths[1]).exists())
        self.assertLessEqual(ring.used, ring.quota)
        self.assertEqual(before, frames.__dict__)
        self.assertTrue(Path(driver['path']).exists())

    def test_shared_content_survives_until_last_issuance_is_evicted(self):
        ring = self.feed.ring
        ring.count = 2
        first = ring.put((1, None), b'one', {'rev': 1})
        ring.put((2, None), b'one', {'rev': 2})
        ring.put((3, None), b'two', {'rev': 3})
        self.assertTrue(Path(first['path']).exists())
        self.assertEqual(ring.used, 6)
        ring.put((4, None), b'three', {'rev': 4})
        self.assertFalse(Path(first['path']).exists())
        self.assertEqual(ring.used, 8)

    def test_feed_disk_failure_is_local(self):
        self.publish()
        with patch.object(self.feed.ring, 'put', side_effect=OSError('disk full')):
            value = self.feed.message(w.json_bytes(dict(jsonrpc='2.0', id=1, method='frames.get')))
        self.assertEqual(value['error']['data']['reason'], 'feed_unavailable')
        self.assertNotIn('provider_degraded', json.dumps(value))


class ActingDriver(Harness):
    def test_feed_while_real_engine_is_acting_does_not_touch_quota_pins_or_lease(self):
        self.w.selector = selectors.DefaultSelector()
        self.addCleanup(self.w.selector.close)
        self.w.x = NS(ax=NS(ask=Mock(side_effect=AssertionError('unexpected X request'))))
        shadow = Shadow(640, 480)
        shadow.mode = 'mmap'  # Synthetic local shadow; no X connection.
        self.w.pixels = pixels = Pixels(self.w, shadow)
        pixels.settle()
        frame = pixels.frame(pixels.bounds, list(self.w.cache.windows.values()))
        pixels.frames.pin_observation('lease', [frame])
        pixels.frames.retain(dict(frames=[frame]))
        pixels.frames.pins.add(frame['path'].rsplit('/', 1)[-1])
        # The driver cannot admit any additional distinct frame bytes.
        pixels.frames.quota = Path(frame['path']).stat().st_size
        feed = FrameFeed(self.w)
        self.addCleanup(feed.close)
        feed.publish()
        self.submit()
        self.tick()
        self.assertIsNotNone(self.e.active)
        before = copy.deepcopy(pixels.frames.__dict__)
        lease = (self.w.lease, self.w.lease_peer, self.w.lease_until)
        lease_keys = set(self.e.lease_state)
        sock, client = pair_client(self, feed)
        sock.sendall(w.frame(dict(jsonrpc='2.0', id=1, method='frames.get')))
        feed.io(client, selectors.EVENT_READ)
        feed.io(client, selectors.EVENT_WRITE)
        size = struct.unpack('!I', sock.recv(4))[0]
        reply = json.loads(sock.recv(size))['result']
        self.assertEqual(reply['status'], 'frame')
        self.assertEqual(before, pixels.frames.__dict__)
        self.assertEqual(lease, (self.w.lease, self.w.lease_peer, self.w.lease_until))
        self.e.retire_leases()
        self.assertEqual(set(self.e.lease_state), lease_keys)
        self.w.x.ax.ask.assert_not_called()
        self.drain()
        self.assertEqual(self.result()['status'], 'success')


if __name__ == '__main__':
    unittest.main()
