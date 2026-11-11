"""Step 4 frame bounds and lease lifetime; fake transports, no display."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from test_worker_w2 import Harness, w
from worker_act import ActError
from worker_pixels import Frames, Pixels, Shadow, FRAME_LIMIT, IDENTITY_LIMIT, QUOTA


META = dict(rev=1, rect=dict(x=10, y=20, w=100, h=80), scale=.5,
            space='desktop', geometry={})


class FrameBounds(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='memory-step4-')
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)

    def test_default_caps(self):
        store = Frames(self.path)
        self.assertEqual((store.file_limit, store.identity_limit, store.quota),
                         (4096, 4096, 200 * 1024 * 1024))
        self.assertEqual((FRAME_LIMIT, IDENTITY_LIMIT, QUOTA),
                         (store.file_limit, store.identity_limit, store.quota))

    def test_file_sequence_survives_identity_eviction_and_shared_reissue(self):
        store = Frames(self.path, file_limit=3, identity_limit=1)
        payloads = sorted((bytes([n]) for n in range(1, 6)),
                          key=lambda b: hashlib.sha256(b).hexdigest(), reverse=True)
        a, b, c = [store.put(data, META) for data in payloads[:3]]
        for frame in (a, b, c):
            os.utime(frame['path'], ns=(1, 1))
        self.assertNotIn(a['frame'], store.issued)
        again = store.put(payloads[0], dict(META, rev=2))
        self.assertEqual(a['path'], again['path'])
        self.assertEqual(len(store.file_sequence), 3)
        d = store.put(payloads[3], META)
        self.assertFalse(Path(a['path']).exists())
        self.assertEqual(set(store.directory.glob('*.png')), {Path(f['path']) for f in (b, c, d)})
        e = store.put(payloads[4], META)
        self.assertFalse(Path(b['path']).exists())
        self.assertEqual(set(store.directory.glob('*.png')), {Path(f['path']) for f in (c, d, e)})
        self.assertEqual(len(store.file_sequence), 3)
        self.assertEqual(len(store.issued), 1)

    def test_restored_sequence_survives_identity_churn_and_mtime_changes(self):
        old = Frames(self.path)
        a, b = [old.put(bytes([n]), META) for n in (1, 2)]
        os.utime(a['path'], ns=(20, 20))
        os.utime(b['path'], ns=(10, 10))
        store = Frames(self.path, file_limit=3, identity_limit=1)
        store.restore(self.path, 's1')
        # Reissuing a restored file must not turn it into a new file issue.
        store.put(bytes([2]), dict(META, rev=2))
        c = store.put(bytes([3]), META)
        os.utime(b['path'], ns=(30, 30))
        os.utime(c['path'], ns=(1, 1))
        store.put(bytes([4]), META)
        self.assertFalse(Path(b['path']).exists())
        self.assertTrue(Path(a['path']).exists())
        self.assertTrue(Path(c['path']).exists())
        store.put(bytes([5]), META)
        self.assertFalse(Path(a['path']).exists())

    def test_count_cap_protects_exactly_last_fifty_receipts(self):
        store = Frames(self.path, file_limit=3, identity_limit=1)
        a, b, c = [store.put(bytes([n]), META) for n in (1, 2, 3)]
        store.retain(dict(steps=[dict(effect=dict(evidence=[a]))]))
        for _ in range(49):
            store.retain({})
        d = store.put(b'4', META)
        self.assertTrue(Path(a['path']).exists())
        self.assertFalse(Path(b['path']).exists())
        store.retain({})
        e = store.put(b'5', META)
        self.assertFalse(Path(a['path']).exists())
        self.assertEqual(set(store.directory.glob('*.png')), {Path(f['path']) for f in (c, d, e)})
        self.assertEqual(len(store.receipts), 50)

    def test_count_cap_fails_closed_when_all_files_protected(self):
        store = Frames(self.path, file_limit=2)
        a, b = [store.put(bytes([n]), META) for n in (1, 2)]
        store.retain(dict(evidence=[a, b]))
        with self.assertRaises(ActError) as caught:
            store.put(b'3', META)
        self.assertEqual((caught.exception.reason, caught.exception.detail),
                         ('provider_degraded', 'frame_quota'))
        self.assertEqual(set(store.directory.glob('*.png')), {Path(f['path']) for f in (a, b)})

    def test_startup_count_cap_loads_receipt_protection_before_eviction(self):
        store = Frames(self.path / 'frames')
        a, b, c = [store.put(bytes([n]), META) for n in (1, 2, 3)]
        for n, frame in enumerate((a, b, c)):
            os.utime(frame['path'], ns=(n+1, n+1))
        (self.path / 'acts').mkdir()
        (self.path / 'acts/g.jsonl').write_text(json.dumps(dict(
            type='receipt', session='s1', ts='2026', receipt=a)) + '\n')
        restored = Frames(store.directory, file_limit=2)
        restored.restore(self.path, 's1')
        self.assertTrue(Path(a['path']).exists())
        self.assertFalse(Path(b['path']).exists())
        self.assertTrue(Path(c['path']).exists())
        self.assertEqual(len(restored.file_sequence), 2)

    def test_production_file_limit_on_restart_and_next_issue(self):
        for n in range(FRAME_LIMIT + 1):
            path = self.path / f'{n:064x}.png'
            path.write_bytes(b'x')
            os.utime(path, ns=(n+1, n+1))
        store = Frames(self.path)
        store.restore(self.path, 's1')
        self.assertFalse((self.path / f'{0:064x}.png').exists())
        store.put(b'next', META)
        self.assertFalse((self.path / f'{1:064x}.png').exists())
        self.assertEqual(len(list(self.path.glob('*.png'))), FRAME_LIMIT)
        self.assertEqual(len(store.file_sequence), FRAME_LIMIT)


class PixelFrameLifetime(Harness):
    def setUp(self):
        super().setUp()
        self.w.pixels = self.p = Pixels(self.w, Shadow(640, 480))
        # A settled, synthetic framebuffer; no X capture or connection.
        self.p.dirty.clear()

    def observation(self, n):
        self.p.shadow.data[0:3] = bytes([n, n, n])
        params = dict(channels=['pixels'], lease=self.w.lease)
        result = self.w.observation(params)
        self.w.deliver_observation(self.peer, n, params, result)
        return result['frames'][0]

    def test_evicted_identity_returns_transform_stale_on_all_point_paths(self):
        store = self.p.frames
        store.identity_limit = 1
        a = store.put(b'shared', META)
        b = store.put(b'shared', dict(META, rect=dict(META['rect'], x=100)))
        self.assertTrue(Path(a['path']).exists())
        raw = dict(space='frame:' + a['frame'], x=3, y=4)
        for call in (lambda: self.p.desktop_point(raw), lambda: self.e.point(raw),
                     lambda: self.p.region(dict(region=dict(raw, w=10, h=10)))):
            with self.subTest(call=call), self.assertRaisesRegex(ActError, 'transform_stale'):
                call()
        fresh = dict(raw, space='frame:' + b['frame'])
        self.assertEqual(self.p.desktop_point(fresh), (106, 28))
        self.assertEqual(self.e.point(fresh), dict(x=106, y=28))
        receipt = self.run_action(self.params(steps=[dict(intent=dict(kind='move', at=raw), timeout_ms=50)]))
        self.assertEqual(receipt['reason'], 'transform_stale')
        self.assertFalse(receipt['dispatched'])
        self.assertEqual(self.t.log, [])

    def test_last_two_delivered_observations_survive_byte_quota_eviction(self):
        a, b, c = [self.observation(n) for n in (1, 2, 3)]
        # Receipt processing clears its temporary pins, never lease pins.
        self.p.receipt_frames(dict(steps=[]))
        self.w.deliver_observation(self.peer, 4, {}, dict(unchanged=True))
        self.p.frames.quota = sum(Path(f['path']).stat().st_size for f in (b, c)) + 4
        d = self.p.frames.put(b'next', META)
        self.assertFalse(Path(a['path']).exists())
        self.assertEqual(set(self.p.frames.directory.glob('*.png')),
                         {Path(f['path']) for f in (b, c, d)})
        self.assertEqual(list(self.p.frames.observations['lease']),
                         [Path(f['path']).name for f in (b, c)])

    def test_observation_pin_rollover_and_lease_release_paths(self):
        for release in ('done', 'expiry', 'epoch'):
            with self.subTest(release=release):
                self.w.lease, self.w.lease_peer, self.w.lease_until = 'lease', self.peer, None
                a, b, c = [self.observation(n) for n in (1, 2, 3)]
                if release == 'done':
                    self.w.message(self.peer, w.json_bytes(dict(jsonrpc='2.0', id=10,
                        method='session.done', params=dict(lease='lease', status='success', summary='done'))))
                elif release == 'expiry':
                    self.w.lease_peer, self.w.lease_until = None, 105
                    with patch.object(w.time, 'monotonic', return_value=104):
                        self.w.expire_lease()
                    self.assertIn('lease', self.p.frames.observations)
                    with patch.object(w.time, 'monotonic', return_value=105):
                        self.w.expire_lease()
                else:
                    self.e.control_changed(dict(state='agent', epoch=1), dict(state='paused', epoch=2))
                self.assertFalse(self.p.frames.observations)
                self.assertIsNone(self.w.lease)
                self.p.frames.evict(extra=QUOTA)
                self.assertFalse(any(Path(f['path']).exists() for f in (a, b, c)))

    def test_only_deliverable_observations_for_current_lease_are_pinned(self):
        frame = self.p.frames.put(b'frame', META)
        self.w.observed(NS(), {}, dict(frames=[frame]))
        self.w.observed(self.peer, dict(lease='other'), dict(frames=[frame]))
        self.assertFalse(self.p.frames.observations)
        self.w.respond = Mock()
        self.w.deliver_observation(self.peer, 1, {}, dict(frames=[frame], huge='x' * w.MAX_FRAME))
        self.assertFalse(self.p.frames.observations)
        self.assertEqual(self.w.respond.call_args.kwargs['exc'].reason, 'response_too_large')

    def test_worker_pixel_startup_cleans_tmp_orphans_only(self):
        directory = self.p.frames.directory
        frame = self.p.frames.put(b'keep', META)
        for name in ('interrupted.tmp', '.hidden.tmp'):
            (directory / name).write_bytes(b'incomplete')
        (directory / 'keep.txt').write_bytes(b'keep')
        (directory / 'keep.tmp').mkdir()
        pixels = Pixels(self.w, Shadow(640, 480))
        self.assertTrue(Path(frame['path']).exists())
        self.assertTrue((directory / 'keep.txt').exists())
        self.assertTrue((directory / 'keep.tmp').is_dir())
        self.assertFalse((directory / 'interrupted.tmp').exists())
        self.assertFalse((directory / '.hidden.tmp').exists())
        self.assertFalse(pixels.frames.issued)


if __name__ == '__main__':
    unittest.main()
