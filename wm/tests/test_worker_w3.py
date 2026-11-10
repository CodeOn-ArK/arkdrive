#!/usr/bin/python3
"""Headless W3 contracts: synthetic XWD, deferred fake X and real PNG files.

No display, device, external process or provider is opened. PNG assertions use
an independent zlib decoder and RGB oracle, not the worker's encoder/reader.
"""
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch
import zlib
from Xlib.protocol import display as xdisplay, event as xevent
from Xlib.xobject.drawable import Window

from test_worker_w1 import w, GEN, window
from test_worker_w2 import Harness
from worker_act import ActError
from worker_pixels import Pixels, Shadow, Frames, intersect


class WireX:
    """Fake transport, real python-xlib extension registration and wire decoder.

    Display.__init__ would open a socket. Supply only the parser/queue state
    here; pending_events feeds server bytes instead of reading a real socket.
    In particular, do not instantiate DamageNotify to stand in for decoding:
    python-xlib registers a *clone*, not a subclass of that imported class.
    """
    def __init__(self, damage_code=90):
        self.connection = w.display.Display.__new__(w.display.Display)
        self.parser = self.connection.display = xdisplay.Display.__new__(xdisplay.Display)
        self.connection.extension_event = NS()
        self.parser.event_classes = {w.X.MappingNotify: xevent.MappingNotify}
        self.connection.extension_add_event(damage_code, w.damage.DamageNotify)
        self.parser.resource_classes = {}
        self.parser.event_queue, self.parser.sent_requests = [], []
        self.parser.request_serial = 1
        self.requests = []
        self.parser.send_request = self.send_request
        self.parser.data_recv, self.parser.recv_packet_len = b'', 0
        self.parser.event_waiting = self.parser.request_waiting = False
        self.parser.socket_error = None
        for name in ('event_queue_read_lock', 'event_queue_write_lock',
                     'send_recv_lock', 'socket_error_lock'):
            setattr(self.parser, name, threading.Lock())
        self.packets = []
        self.connection.pending_events = self.pending_events

    def send_request(self, req, wait):
        req._serial = self.parser.request_serial
        self.parser.request_serial += 1
        self.requests.append(req)
        if wait:
            self.parser.sent_requests.append(req)

    def pending_events(self):
        if self.packets:
            self.parser.data_recv = bytes(self.parser.data_recv) + self.packets.pop(0)
            self.parser.parse_response(None)
        return len(self.parser.event_queue)

    def damage(self, rect, sequence=1, more=False):
        return struct.pack('=BBHIIIhhHHhhHH',
            self.connection.extension_event.DamageNotify, 0x80 if more else 0,
            sequence, 1, 2, sequence, rect['x'], rect['y'], rect['w'], rect['h'],
            0, 0, 192, 128)

    def feed(self, wire, chunk=4096):
        self.packets.extend(wire[i:i+chunk] for i in range(0, len(wire), chunk))

    @staticmethod
    def unrelated():
        return xevent.MappingNotify(sequence_number=1, request=0,
                                    first_keycode=8, count=1)._binary


def rgb(x, y):
    return bytes(((x*3+y) % 256, (x+y*7) % 256, (x*11+y*13) % 256))


def synthetic(path, width, height, header_size=160, ncolors=256, padding=0, order=0):
    """Construct XWD independently, including non-default name/colormap sizes."""
    stride = width*4 + padding
    header = [header_size, 7, 2, 24, width, height, 0, order, 32, 0, 32, 32,
              stride, 4, 0xff0000, 0xff00, 0xff, 8, 256, ncolors,
              width, height, 0, 0, 0]
    offset = header_size + ncolors*12
    data = bytearray(offset + stride*height)
    data[:100] = struct.pack('>25I', *header)
    for y in range(height):
        row = bytearray(width*4)
        for x in range(width):
            r, g, b = rgb(x, y)
            row[x*4:x*4+4] = bytes((b, g, r, 0)) if order == 0 else bytes((0, r, g, b))
        data[offset+y*stride:offset+y*stride+len(row)] = row
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(data)
    return offset, stride


def decode(path):
    data = Path(path).read_bytes()
    assert data[:8] == b'\x89PNG\r\n\x1a\n'
    pos, compressed = 8, bytearray()
    while pos < len(data):
        length = struct.unpack_from('>I', data, pos)[0]
        kind, payload = data[pos+4:pos+8], data[pos+8:pos+8+length]
        assert zlib.crc32(kind+payload) == struct.unpack_from('>I', data, pos+8+length)[0]
        if kind == b'IHDR':
            width, height, *fields = struct.unpack('>2I5B', payload)
            assert fields == [8, 2, 0, 0, 0]
        if kind == b'IDAT':
            compressed.extend(payload)
        pos += length+12
    raw = zlib.decompress(compressed)
    assert len(raw) == height*(width*3+1)
    rows = []
    for y in range(height):
        start = y*(width*3+1)
        assert raw[start] == 0
        rows.append(raw[start+1:start+1+width*3])
    return width, height, rows


class PixelCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='worker-w3-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.owner(192, 128)

    def owner(self, width, height, mmap=True, **kw):
        path = self.path / 'fb/Xvfb_screen0'
        self.offset, self.stride = synthetic(path, width, height, **kw)
        owner = self.w = w.Worker.__new__(w.Worker)
        owner.cache = w.StateCache('s1', GEN)
        owner.sid, owner.generation = 's1', GEN
        owner.sd = owner.state = self.path
        owner.screen = dict(w=width, h=height)
        owner.record = dict(profile='xvfb' if mmap else 'xorg', display=91)
        owner.covered = lambda: True
        owner.counters = dict(resync_drift=0)
        owner.x = NS(ax=NS(ask=Mock(), pending=[]), root=1, refreshing=set(), uncovered=set(), initialised=True)
        owner.cache.upsert(10, window(geometry=dict(x=7, y=9, w=90, h=70)))
        owner.cache.set('focus', owner.cache.handle(10))
        owner.cache.baseline()
        shadow = Shadow(width, height, path if mmap else None)
        self.addCleanup(shadow.close)
        owner.pixels = self.p = Pixels(owner, shadow)
        self.handle = owner.cache.handle(10)
        if mmap:
            self.assertTrue(self.p.settle())
        else:
            self.assertFalse(self.p.settle())
            self.reply_capture()
            self.assertTrue(self.p.settle())
        owner.cache.baseline()
        return owner

    def reply_capture(self):
        cls, callback = self.w.x.ax.ask.call_args.args
        kw = self.w.x.ax.ask.call_args.kwargs
        self.assertIs(cls, w.request.GetImage)
        data = b''.join(bytes((rgb(x, y)[2], rgb(x, y)[1], rgb(x, y)[0], 0))
                        for y in range(kw['y'], kw['y']+kw['height'])
                        for x in range(kw['x'], kw['x']+kw['width']))
        callback(dict(depth=24, data=data))

    def mutate(self, x, y, color=b'\x12\x34\x56'):
        with (self.path / 'fb/Xvfb_screen0').open('r+b') as stream:
            stream.seek(self.offset+y*self.stride+x*4)
            stream.write(bytes((color[2], color[1], color[0], 0)))

    def damage(self, *rects):
        self.p.mark(rects)
        self.w.cache.commit(True)

    def observe(self, **params):
        return self.w.observation(params)

    def wire_owner(self, code=90):
        wire = WireX(code)
        xs = w.XState.__new__(w.XState)
        xs.w, xs.root, xs.ev = self.w, Window(wire.parser, 1), wire.connection
        xs.derive = Mock()
        xs.ax = w.AsyncX(wire.connection, [])
        xs.inp_ax = NS(pump=Mock())
        xs.initialised, xs.refreshing, xs.uncovered = True, set(), set()
        self.w.x, self.w.x_errors = xs, []
        self.w.poll_control = Mock()
        self.w.peers = [NS(waits=[])]
        self.w.respond, self.w.require_lease = Mock(), Mock()
        return wire

    def paint(self, n):
        color = bytes((n % 256, (n*3) % 256, (n*7) % 256, 0))
        with (self.path / 'fb/Xvfb_screen0').open('r+b') as stream:
            for y in range(self.p.shadow.h):
                stream.seek(self.offset + y*self.stride)
                stream.write(color*self.p.shadow.w)

    def test_registered_wire_damage_marks_tiles_without_hashing(self):
        for code in (90, 105):
            with self.subTest(code=code):
                wire = self.wire_owner(code)
                wire.feed(wire.damage(dict(x=63, y=63, w=2, h=2)))
                with patch.object(self.p.shadow, 'digest', side_effect=AssertionError('hash')):
                    self.w.ingest()
                registered = wire.parser.event_classes[code]
                self.assertFalse(issubclass(registered, w.damage.DamageNotify))
                self.assertEqual(set(self.p.dirty), {0, 1, 3, 4})
                self.assertFalse(wire.packets)
                self.p.settle()

    def test_sustained_wire_damage_next_observe_at_80ms(self):
        wire = self.wire_owner()
        # More than two transport reads of other traffic precede every paint.
        # No watchdog runs during these eight simulated seconds of painting.
        for n in range(1, 101):
            since = self.w.cache.rev
            self.paint(n)
            wire.feed(wire.unrelated()*300 + b''.join(
                wire.damage(r, n, more=i < len(self.p.rects)-1)
                for i, r in enumerate(self.p.rects)))
            with patch.object(w.time, 'monotonic', return_value=100+n*.080):
                self.w.ingest()
                self.w.observe(self.w.peers[0], n, dict(since_rev=since))
            result = self.w.respond.call_args.args[2]
            self.assertFalse(result['unchanged'], n)
            self.assertEqual(result['pixels_changed']['tiles'], list(range(6)), n)
            self.assertFalse(self.p.dirty)
            self.assertFalse(self.w.peers[0].waits)
        self.assertEqual(self.w.counters['resync_drift'], 0)

    def test_wire_backlog_surfaces_after_paint_stops_without_watchdog(self):
        wire = self.wire_owner()
        since = self.w.cache.rev
        # The owner falls behind while 100 paints accumulate in its transport.
        for n in range(1, 101):
            self.paint(n)
            wire.feed(wire.unrelated()*40 + wire.damage(self.p.bounds, n))
        self.w.ingest()
        self.w.observe(self.w.peers[0], 1, dict(since_rev=since))
        result = self.w.respond.call_args.args[2]
        self.assertFalse(result['unchanged'])
        self.assertEqual(result['pixels_changed']['tiles'], list(range(6)))
        self.assertFalse(wire.packets)
        self.assertFalse(self.w.peers[0].waits)
        with patch.object(self.p.shadow, 'digest', side_effect=AssertionError('idle hash')):
            self.assertTrue(self.observe(since_rev=result['rev'])['unchanged'])
        self.assertEqual(wire.requests, [])

    def test_registered_wire_damage_settles_getimage_before_observe(self):
        self.owner(192, 128, mmap=False)
        wire = self.wire_owner()
        self.paint(42)
        wire.feed(wire.damage(self.p.bounds))
        self.w.ingest()
        self.w.observe(self.w.peers[0], 1, dict(since_rev=1))
        self.w.respond.assert_not_called()
        self.assertTrue(self.p.pending)
        # Complete the deferred capture from independent fake server pixels.
        req = wire.requests[-1]
        data = (self.path / 'fb/Xvfb_screen0').read_bytes()[self.offset:]
        wire.feed(struct.pack('=BBHII20x', 1, 24, req._serial, len(data)//4, 0) + data)
        # Reply-only reads can end one pump before the complete image arrives.
        while wire.packets:
            self.w.ingest()
        self.w.pending()
        result = self.w.respond.call_args.args[2]
        self.assertFalse(result['unchanged'])
        self.assertEqual(result['pixels_changed']['tiles'], list(range(6)))
        self.assertFalse(self.p.dirty)

    def test_tile_counts_and_edge_sizes(self):
        for width, height, count in ((1920, 1080, 510), (1280, 800, 260)):
            with self.subTest(width=width):
                owner = self.owner(width, height)
                self.assertEqual(len(owner.pixels.rects), count)
                self.assertEqual(sum(r['w']*r['h'] for r in owner.pixels.rects), width*height)

    def test_damage_marks_intersections_once_without_hashing(self):
        with patch.object(self.p.shadow, 'digest', wraps=self.p.shadow.digest) as digest:
            self.damage(dict(x=63, y=63, w=2, h=2), dict(x=64, y=64, w=1, h=1))
            self.assertEqual(set(self.p.dirty), {0, 1, 3, 4})
            self.assertTrue(all(stamp[0] == self.w.cache.rev for stamp in self.p.dirty.values()))
            digest.assert_not_called()
            self.w.x.ax.ask.assert_not_called()

    def test_damage_clips_to_desktop_and_ignores_empty(self):
        self.damage(dict(x=-2, y=-2, w=3, h=3), dict(x=192, y=0, w=8, h=8), dict(x=3, y=4, w=0, h=2))
        self.assertEqual(set(self.p.dirty), {0})

    def test_damage_identical_cleans_without_pixel_facet_bump(self):
        before = dict(self.w.cache.facets)
        self.damage(dict(x=0, y=0, w=100, h=75))
        rev = self.w.cache.rev
        result = self.observe(since_rev=1, channels=['pixels'])
        self.assertTrue(result['unchanged'])
        self.assertNotIn('frames', result)
        self.assertNotIn('pixels_changed', result)
        self.assertEqual(self.w.cache.facets, before)
        self.assertEqual(self.w.cache.rev, rev)
        self.assertFalse(self.p.dirty)

    def test_real_change_stamps_exactly_affected_tiles_and_bbox(self):
        self.mutate(65, 66)
        self.damage(dict(x=0, y=0, w=192, h=128))
        result = self.observe(scope=dict(kind='window', window=self.handle), since_rev=1)
        changed = [t for t in range(6) if self.w.cache.facets[self.p.facet(t)] > 1]
        self.assertEqual(changed, [4])
        self.assertFalse(result['unchanged'])
        delta = result['pixels_changed']
        self.assertEqual(delta['tiles'], [4])
        self.assertEqual(delta['bbox'], dict(x=64, y=64, w=33, h=15))
        self.assertEqual(delta['windows'][0]['window'], self.handle)
        self.assertNotIn('frames', result)

    def test_1000_idle_pixel_observes_do_no_hash_or_x_traffic(self):
        with patch.object(self.p.shadow, 'digest', side_effect=AssertionError('hash on fast path')), \
             patch.object(self.p.shadow, 'png', side_effect=AssertionError('frame on fast path')):
            for _ in range(1000):
                self.assertTrue(self.observe(since_rev=1, channels=['state', 'pixels'])['unchanged'])
        self.w.x.ax.ask.assert_not_called()

    def test_settle_outside_scope_before_reporting_rev(self):
        self.mutate(180, 120)
        self.damage(dict(x=180, y=120, w=1, h=1))
        result = self.observe(scope=dict(kind='window', window=self.handle), since_rev=1)
        self.assertTrue(result['unchanged'])
        self.assertFalse(self.p.dirty)
        self.assertGreater(self.w.cache.facets['pixels[5]'], 1)
        self.assertEqual(result['rev'], self.w.cache.rev)

    def test_wait_result_settles_all_pixels(self):
        self.mutate(180, 120)
        self.damage(dict(x=180, y=120, w=1, h=1))
        result = self.w.wait_result(dict(since_rev=1), True)
        self.assertFalse(self.p.dirty)
        self.assertEqual(result['rev'], self.w.cache.facets['pixels[5]'])
        self.assertFalse(result['state_delta']['unchanged'])

    def test_xwd_header_size_colormap_and_stride_authoritative(self):
        self.owner(19, 13, header_size=213, ncolors=3, padding=12)
        result = self.observe(channels=['pixels'], region=dict(space='desktop', x=2, y=3, w=13, h=7), scale=1)
        width, height, rows = decode(result['frames'][0]['path'])
        self.assertEqual((width, height), (13, 7))
        self.assertEqual(rows, [b''.join(rgb(x, y) for x in range(2, 15)) for y in range(3, 10)])

    def test_xwd_big_endian_pixels(self):
        self.owner(19, 13, order=1)
        result = self.observe(channels=['pixels'], scale=1)
        self.assertEqual(decode(result['frames'][0]['path'])[2], [b''.join(rgb(x,y) for x in range(19)) for y in range(13)])

    def test_invalid_xwd_fails_closed(self):
        path = self.path / 'bad'
        for data in (b'bad', struct.pack('>25I', *([0]*25))):
            path.write_bytes(data)
            with self.assertRaises((ValueError, struct.error)):
                Shadow(192, 128, path)

    def test_crop_scale_one_is_pixel_exact(self):
        result = self.observe(scope=dict(kind='window', window=self.handle), channels=['pixels'], scale=1)
        self.assertEqual(len(result['frames']), 1)
        frame = result['frames'][0]
        self.assertEqual(frame['rect'], dict(x=7, y=9, w=90, h=70))
        self.assertEqual(decode(frame['path'])[2], [b''.join(rgb(x,y) for x in range(7,97)) for y in range(9,79)])
        self.assertEqual(frame['scale'], 1)
        self.assertEqual(frame['space'], 'desktop')

    def test_downscale_nearest_neighbor_pixels_and_long_side(self):
        self.owner(1280, 800)
        frame = self.observe(channels=['pixels'])['frames'][0]
        width, height, rows = decode(frame['path'])
        self.assertEqual((width, height), (1024, 640))
        self.assertEqual(frame['scale'], 0.8)
        self.assertEqual(rows, [b''.join(rgb(int(x/.8),int(y/.8)) for x in range(width)) for y in range(height)])

    def test_scale_one_oversized_and_nonfinite_rejected(self):
        for scale in (0, -1, float('nan'), float('inf'), True, 2):
            with self.subTest(scale=scale), self.assertRaises(ActError):
                self.observe(channels=['pixels'], scale=scale)
        self.owner(1280,800)
        with self.assertRaises(ActError):
            self.observe(channels=['pixels'], scale=1)

    def test_content_addressing_and_transform_identity(self):
        a = self.observe(channels=['pixels'])['frames'][0]
        b = self.observe(channels=['pixels'])['frames'][0]
        self.assertEqual(a, b)
        path = Path(a['path'])
        self.assertEqual(path.stem, hashlib.sha256(path.read_bytes()).hexdigest())
        # Same content with a different offset must never reuse its transform.
        store = self.p.frames
        meta = dict(store.issued[a['frame']])
        meta = {k:v for k,v in meta.items() if k not in ('frame', 'path')}
        meta['rect'] = dict(meta['rect'], x=1)
        c = store.put(path.read_bytes(), meta)
        self.assertEqual(c['path'], a['path'])
        self.assertNotEqual(c['frame'], a['frame'])

    def test_frame_point_conversion_exact_and_geometry_stale(self):
        frame = self.observe(scope=dict(kind='window', window=self.handle), channels=['pixels'], scale=.5)['frames'][0]
        self.assertEqual(self.p.desktop_point(dict(space='frame:'+frame['frame'], x=13, y=11)), (33,31))
        win = self.w.cache.windows[10]
        self.w.cache.upsert(10, dict(win, geometry=dict(win['geometry'], x=8)))
        self.w.cache.commit()
        with self.assertRaisesRegex(ActError, 'stale_rev'):
            self.p.desktop_point(dict(space='frame:'+frame['frame'], x=13, y=11))

    def test_window_space_uses_observation_rev(self):
        self.assertEqual(self.p.desktop_point(dict(space='window:'+self.handle, x=4, y=5), 1), (11,14))
        win = self.w.cache.windows[10]
        self.w.cache.upsert(10, dict(win, geometry=dict(win['geometry'], y=10)))
        self.w.cache.commit()
        with self.assertRaisesRegex(ActError, 'stale_rev'):
            self.p.desktop_point(dict(space='window:'+self.handle, x=4,y=5), 1)

    def test_unknown_or_outside_frame_rejected(self):
        with self.assertRaisesRegex(ActError, 'transform_stale'):
            self.p.desktop_point(dict(space='frame:absent',x=0,y=0))
        f = self.observe(channels=['pixels'])['frames'][0]
        with self.assertRaises(ActError):
            self.p.desktop_point(dict(space='frame:'+f['frame'],x=192,y=0))

    def test_getimage_dirty_bounding_box_and_full_threshold(self):
        self.owner(192,128,mmap=False)
        self.w.x.ax.ask.reset_mock()
        self.damage(dict(x=66,y=2,w=1,h=1))
        self.assertFalse(self.p.settle())
        kw = self.w.x.ax.ask.call_args.kwargs
        self.assertEqual((kw['x'],kw['y'],kw['width'],kw['height']), (64,0,64,64))
        self.reply_capture()
        self.assertTrue(self.p.settle())
        self.w.x.ax.ask.reset_mock()
        self.damage(dict(x=0,y=0,w=192,h=65))
        self.assertFalse(self.p.settle())
        kw = self.w.x.ax.ask.call_args.kwargs
        self.assertEqual((kw['x'],kw['y'],kw['width'],kw['height']), (0,0,192,128))

    def test_getimage_damage_arriving_in_flight_stays_dirty(self):
        self.owner(192,128,mmap=False)
        rect = dict(x=64,y=0,w=1,h=1)
        self.damage(rect)
        self.assertFalse(self.p.settle())
        self.damage(rect)
        self.reply_capture()
        self.assertEqual(set(self.p.dirty), {1})
        self.assertFalse(self.p.settle())
        self.reply_capture()
        self.assertTrue(self.p.settle())

    def test_getimage_clean_fast_path_and_pixel_exact_frame(self):
        self.owner(192,128,mmap=False)
        self.w.x.ax.ask.reset_mock()
        with patch.object(self.p.shadow, 'digest', side_effect=AssertionError('hash')):
            self.assertTrue(self.observe(since_rev=1,channels=['pixels'])['unchanged'])
        self.w.x.ax.ask.assert_not_called()
        frame = self.observe(channels=['pixels'])['frames'][0]
        self.assertEqual(decode(frame['path'])[2], [b''.join(rgb(x,y) for x in range(192)) for y in range(128)])

    def test_watchdog_detects_missed_pixel_damage(self):
        self.w.x.reconcile = Mock()
        self.mutate(10,10)
        self.w.watchdog_start()
        self.w.watchdog_finish()
        self.assertEqual(self.w.counters['resync_drift'], 1)
        self.assertGreater(self.w.cache.facets['pixels[0]'], 1)
        self.assertFalse(self.observe(since_rev=1)['unchanged'])
        self.w.watchdog_start()
        self.w.watchdog_finish()
        self.assertEqual(self.w.counters['resync_drift'], 1)

    def test_watchdog_detects_missed_state_change(self):
        self.w.x.reconcile = Mock()
        self.w.watchdog_start()
        self.w.cache.upsert(10,dict(self.w.cache.windows[10],title='missed title'))
        self.w.cache.commit()
        self.w.watchdog_finish()
        self.assertEqual(self.w.counters['resync_drift'],1)

    def test_damage_event_batch_only_marks_lazy_tiles(self):
        xs = w.XState.__new__(w.XState)
        xs.w, xs.root = self.w, NS(id=1)
        xs.ev = NS(extension_event=NS(DamageNotify=90))
        xs.derive = Mock()
        event = w.damage.DamageNotify(type=90, level=0, sequence_number=1,
                drawable=1, damage=2, timestamp=1,
                area=dict(x=63,y=63,width=2,height=2),
                drawable_geometry=dict(x=0,y=0,width=192,height=128))
        event = w.damage.DamageNotify(binarydata=event._binary,
            display=NS(get_resource_class=lambda name: lambda display, resource_id: resource_id))
        with patch.object(self.p.shadow,'digest',side_effect=AssertionError('hash')):
            xs.events([event,event])
        self.assertEqual(set(self.p.dirty), {0,1,3,4})
        self.assertEqual(self.p.serial, 2)  # initial mark + coalesced batch

    def test_ignore_regions_exclude_only_fully_covered_tiles(self):
        self.mutate(10,10)
        self.damage(dict(x=0,y=0,w=64,h=64))
        self.assertTrue(self.observe(since_rev=1,ignore_regions=[dict(x=0,y=0,w=64,h=64)])['unchanged'])
        self.assertFalse(self.observe(since_rev=1,ignore_regions=[dict(x=0,y=0,w=63,h=64)])['unchanged'])

    def test_1920_default_downscale_exact_optimized_columns(self):
        self.owner(1920, 1080)
        f = self.observe(channels=['pixels'])['frames'][0]
        width, height, rows = decode(f['path'])
        self.assertEqual((width,height),(1024,576))
        scale = 1024/1920
        self.assertEqual(rows,[b''.join(rgb(int(x/scale),int(y/scale)) for x in range(width)) for y in range(height)])

    def test_unusual_downscale_exact_general_mapping(self):
        f = self.observe(channels=['pixels'],scale=.371)['frames'][0]
        width,height,rows = decode(f['path'])
        self.assertEqual(rows,[b''.join(rgb(int(x/.371),int(y/.371)) for x in range(width)) for y in range(height)])

    def test_pending_xorg_observe_waits_for_refresh_before_reply(self):
        self.owner(192,128,mmap=False)
        self.damage(dict(x=66,y=2,w=1,h=1))
        self.w.require_lease = Mock()
        peer = NS(waits=[])
        self.w.peers = [peer]
        self.w.respond = Mock()
        self.w.observe(peer,7,dict(since_rev=1,channels=['pixels']))
        self.w.respond.assert_not_called()
        self.assertEqual(len(peer.waits),1)
        self.reply_capture()
        self.w.pending()
        result = self.w.respond.call_args.args[2]
        self.assertTrue(result['unchanged'])
        self.assertEqual(result['rev'],self.w.cache.rev)
        self.assertFalse(self.p.dirty)
        self.assertEqual(peer.waits,[])

    def test_damage_subscription_waits_for_version_replies(self):
        xs = w.XState.__new__(w.XState)
        xs.phase = 'versions'
        xs.w = self.w
        xs.w.timings = {}
        xs.w.elapsed = lambda: 0
        xs.ax = NS(pending=[object()],ask=Mock())
        xs.inp_ax = NS(pending=[])
        xs.root = NS(damage_create=Mock(return_value=9))
        xs.atoms = {'WM_S0':1,'_NET_SUPPORTING_WM_CHECK':2}
        xs.advance()
        xs.root.damage_create.assert_not_called()
        xs.ax.pending.clear()
        xs.advance()
        xs.root.damage_create.assert_called_once_with(w.damage.DamageReportRawRectangles)
        self.assertEqual(xs.damage_id,9)

    def test_known_damage_is_settled_before_watchdog_baseline(self):
        self.w.x.reconcile = Mock()
        self.mutate(10,10)
        self.damage(dict(x=10,y=10,w=1,h=1))
        self.w.watchdog_start()
        self.w.watchdog_finish()
        self.assertEqual(self.w.counters['resync_drift'],0)
        self.assertGreater(self.w.cache.facets['pixels[0]'],1)


class FrameQuotaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='worker-w3-quota-')
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.store = Frames(self.path/'frames', quota=12)
        self.meta = dict(rev=1,rect=dict(x=0,y=0,w=1,h=1),scale=1,space='desktop',geometry={})

    def put(self, n):
        return self.store.put(bytes([n])*4, self.meta)

    def test_oldest_evicted_receipt_reference_protected(self):
        a,b,c = self.put(1),self.put(2),self.put(3)
        self.store.retain(dict(frame=a['frame'],path=a['path']))
        d = self.put(4)
        self.assertTrue(Path(a['path']).exists())
        self.assertFalse(Path(b['path']).exists())
        self.assertTrue(Path(c['path']).exists())
        self.assertTrue(Path(d['path']).exists())

    def test_protection_expires_after_fifty_receipts(self):
        a = self.put(1)
        self.store.retain(a)
        for _ in range(49):
            self.store.retain({})
        self.assertIn(Path(a['path']).name,set().union(*self.store.receipts))
        self.store.retain({})
        self.put(2); self.put(3); self.put(4)
        self.assertFalse(Path(a['path']).exists())
        self.assertEqual(len(self.store.receipts), 50)

    def test_hard_quota_never_evicts_protected_frames(self):
        for n in (1,2,3):
            self.store.retain(self.put(n))
        with self.assertRaisesRegex(ActError,'provider_degraded'):
            self.put(4)
        self.assertEqual(len(list(self.store.directory.glob('*.png'))), 3)
        self.assertEqual(sum(p.stat().st_size for p in self.store.directory.glob('*.png')), 12)

    def test_restored_receipts_protect_files_across_restart(self):
        a = self.put(1)
        (self.path/'acts').mkdir()
        (self.path/'acts'/'g.jsonl').write_text(json.dumps(dict(type='receipt',session='s1',ts='2026',receipt=a))+'\n')
        store = Frames(self.store.directory, quota=4)
        store.restore(self.path,'s1')
        with self.assertRaises(ActError):
            store.put(b'xxxx',self.meta)
        self.assertTrue(Path(a['path']).exists())


# Reuse W2's independent transport/journal harness without inheriting its test
# classes: the harness itself has helpers only. Actions still use fake X input.
class PixelActionTests(Harness):
    def setUp(self):
        super().setUp()
        self.fb = self.path/'fb/Xvfb_screen0'
        self.offset, self.stride = synthetic(self.fb,640,480)
        shadow = Shadow(640,480,self.fb)
        self.addCleanup(shadow.close)
        self.w.pixels = self.p = Pixels(self.w,shadow)
        self.assertTrue(self.p.settle())
        self.w.cache.baseline()

    def run_action(self, steps, **extra):
        return super().run_action(self.params(steps=steps, **extra))

    def test_not_observed_receipt_has_target_region_frame(self):
        r = self.run_action([self.key(expect=dict(kind='window_present',match=dict(title='Never appears')))])
        self.assertEqual(r['effect']['status'],'not_observed')
        evidence = [v for v in r['steps'][0]['effect']['evidence'] if v['type']=='frame']
        self.assertEqual(len(evidence),1)
        f = evidence[0]
        self.assertTrue(Path(f['path']).exists())
        self.assertEqual(f['rect'],dict(x=0,y=0,w=500,h=400))
        self.assertEqual(decode(f['path'])[2], [b''.join(rgb(x,y) for x in range(500)) for y in range(400)])
        self.assertEqual(f['rev'],r['rev']['final'])
        self.assertEqual(len(self.p.frames.receipts),1)
        self.assertEqual(self.rows()[-1]['receipt'],r)

    def test_frame_space_click_converts_exactly(self):
        f = self.p.frame(dict(x=10,y=20,w=200,h=100),[self.w.cache.windows[10]],.5)
        r = self.run_action([dict(intent=dict(kind='click',at=dict(space='frame:'+f['frame'],x=11,y=9)),
                                  timeout_ms=50, expect=dict(kind='window_present',match=dict(title='Absent')))])
        self.assertEqual(r['dispatch_status'],'accepted')
        self.assertEqual(self.t.position,dict(x=32,y=38))

    def test_stale_frame_geometry_rejects_before_input(self):
        f = self.p.frame(dict(x=0,y=0,w=200,h=100),[self.w.cache.windows[10]],1)
        win = self.w.cache.windows[10]
        self.w.cache.upsert(10,dict(win,geometry=dict(win['geometry'],x=1)))
        self.w.cache.commit()
        r = self.run_action([dict(intent=dict(kind='click',at=dict(space='frame:'+f['frame'],x=20,y=20)),timeout_ms=50)])
        self.assertEqual(r['reason'],'stale_rev')
        self.assertFalse(r['dispatched'])
        self.assertEqual(self.t.log,[])

    def test_pixel_dependency_rejects_real_changes_allow_stale_optout(self):
        with self.fb.open('r+b') as stream:
            stream.seek(self.offset+20*self.stride+20*4)
            stream.write(b'\x01\x02\x03\0')
        self.p.mark([dict(x=20,y=20,w=1,h=1)])
        self.w.cache.commit(True)
        r = self.run_action([self.move()])
        self.assertEqual(r['reason'],'stale_rev')
        self.assertFalse(r['dispatched'])
        self.assertFalse(self.p.dirty)
        # New action ID after a pre-dispatch failure is still admissible.
        r = self.run_action([self.move(allow_stale_pixels=True)],action_id='b')
        self.assertEqual(r['status'],'success')

    def test_receipt_settles_damage_outside_target(self):
        original = self.t.write
        def write(event):
            with self.fb.open('r+b') as stream:
                stream.seek(self.offset+470*self.stride+630*4)
                stream.write(b'\x01\x02\x03\0')
            self.p.mark([dict(x=630,y=470,w=1,h=1)])
            self.w.cache.commit(True)
            original(event)
        self.t.write = write
        r = self.run_action([self.move()])
        self.assertFalse(self.p.dirty)
        self.assertGreater(self.w.cache.facets['pixels[79]'],1)
        self.assertEqual(r['state_delta']['rev'],r['rev']['final'])

    def test_unknown_receipt_also_carries_target_frame(self):
        r = self.run_action([self.key(expect=dict(kind='text_equals',value='later'))])
        self.assertEqual(r['effect']['status'],'unknown')
        frames = [v for v in r['effect']['evidence'] if v['type']=='frame']
        self.assertEqual(len(frames),1)
        self.assertEqual(decode(frames[0]['path'])[:2],(500,400))

    def test_contradicted_receipt_also_carries_target_frame(self):
        def unexpected(event):
            self.t.on_write = None
            self.later()
            self.w.cache.upsert(11,dict(self.w.cache.windows[11],title='Unrelated'))
            self.w.cache.commit()
        self.t.on_write = unexpected
        r = self.run_action([self.key()])
        self.assertEqual(r['effect']['status'],'contradicted')
        frames = [v for v in r['effect']['evidence'] if v['type']=='frame']
        self.assertEqual(len(frames),1)
        self.assertEqual(decode(frames[0]['path'])[:2],(500,400))


if __name__ == '__main__':
    unittest.main()
