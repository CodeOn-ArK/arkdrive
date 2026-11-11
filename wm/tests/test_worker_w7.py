#!/usr/bin/python3
"""Headless W7: W3 shadow frames, real ctypes child, deterministic fault IPC.

No X, D-Bus, session or input device is opened. Cairo renders fx_canvas's
declared drawing commands onto an image surface. Live gates remain Raj's.
"""
import copy
import ctypes
from pathlib import Path
import re
import subprocess
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

import cairo

from test_worker_w4 import ProviderHarness, FakeChild, FakeWire
from worker_act import ActError, OCRPending, predicate_schema
from worker_pixels import Pixels, Shadow
from worker_ocr import Backend, Provider, merge_lines, overlaps

STRINGS = (('ArkDrive canvas', 24, 52), ('Alpha 123', 24, 112),
           ('Bravo 456', 260, 172), ('Target: W0', 24, 232))
ORIGIN = (31, 27)


def frame(strings=STRINGS):
    surface = cairo.ImageSurface(cairo.FORMAT_RGB24, 640, 480)
    context = cairo.Context(surface)
    context.set_source_rgb(1, 1, 1)
    context.paint()
    context.translate(*ORIGIN)
    context.set_source_rgb(0, 0, 0)
    context.select_font_face('Sans')
    context.set_font_size(24)
    boxes = []
    for text, x, y in strings:
        xb, yb, width, height, _, _ = context.text_extents(text)
        boxes.append([ORIGIN[0]+x+xb, ORIGIN[1]+y+yb, width, height])
        context.move_to(x, y)
        context.show_text(text)
    surface.flush()
    return bytes(surface.get_data()), boxes


def line(text='Alpha 123', x=55, y=121, w=111, h=24, conf=95):
    return dict(text=text, box=[x, y, w, h], conf=conf)


class OCRHarness(ProviderHarness):
    def setUp(self):
        super().setUp()
        (self.path / 'logs').mkdir()
        self.shadow = Shadow(640, 480)
        self.addCleanup(self.shadow.close)
        # Synthetic captured bytes use the same RGB row path as W3's mmap.
        self.shadow.mode = 'mmap'
        self.w.pixels = Pixels(self.w, self.shadow)
        win = dict(self.w.cache.windows[10], geometry=dict(x=31, y=27, w=560, h=280))
        self.w.cache.upsert(10, win)
        self.paint()
        self.w.cache.baseline()
        self.ocr_children = []
        def factory():
            wire, child = FakeWire(), FakeChild()
            wire.incoming.append(dict(event='ready'))
            self.ocr_children.append((wire, child))
            return wire, child
        self.ocr_factory = factory
        self.w.ocr = self.o = Provider(self.w, factory, clock=lambda: self.now)
        self.addCleanup(self.o.close)
        self.o.tick()

    def paint(self, strings=STRINGS):
        data, boxes = frame(strings)
        self.shadow.update(self.w.pixels.bounds, data)
        self.w.pixels.mark([self.w.pixels.bounds])
        self.w.pixels.settle()
        return boxes

    def answer_ocr(self, lines):
        self.o.wire.incoming.append(dict(id=self.o.flight['id'], lines=copy.deepcopy(lines)))
        self.o.tick()

    def warm(self, lines):
        self.assertIsNone(self.o.read(self.handle))
        self.answer_ocr(lines)
        return self.o.read(self.handle)

    def tick(self):
        self.o.tick()
        super().tick()

    def degrade_tree(self):
        app = self.p.app(self.p.window(self.handle))
        self.p.apps[app].degrade(self.now, 'application_unreachable')


class CacheAndIncremental(OCRHarness):
    def test_unchanged_pixels_no_child_request(self):
        self.warm([line()])
        count = self.o.requests
        for _ in range(20):
            self.assertEqual(self.o.read(self.handle), [line()])
            self.o.tick()
        self.assertEqual(self.o.requests, count)

    def test_damage_but_identical_no_child_request(self):
        self.warm([line()])
        self.w.pixels.mark([dict(x=55, y=121, w=20, h=10)])
        self.w.pixels.settle()
        self.assertEqual(self.o.read(self.handle), [line()])
        self.assertEqual(self.o.requests, 1)

    def test_return_to_cached_content_sends_no_request(self):
        self.warm([line()])
        self.paint((('Changed label', 24, 112),))
        self.o.read(self.handle)
        self.answer_ocr([line('Changed label')])
        self.paint()
        self.assertEqual(self.o.read(self.handle), [line()])
        self.assertEqual(self.o.requests, 2)

    def test_slab_is_full_window_width_plus_24(self):
        self.warm([line()])
        # One changed source pixel makes the independently known damage box.
        self.shadow.update(dict(x=88, y=131, w=1, h=1), bytes((0, 0, 0, 0)))
        self.w.pixels.mark([dict(x=88, y=131, w=1, h=1)])
        self.w.pixels.settle()
        self.o.read(self.handle)
        self.assertEqual(self.o.flight['slab'], dict(x=31, y=107, w=560, h=49))
        self.assertEqual(len(self.o.flight['rgb']), 560*49*3)

    def test_stale_flight_is_never_current_evidence(self):
        self.o.read(self.handle)
        self.paint((('Changed label', 24, 112),))
        self.answer_ocr([line('old')])
        self.assertIsNone(self.o.read(self.handle))
        self.assertEqual(self.o.requests, 2)

    def test_cache_lru_is_256(self):
        for i in range(260):
            Provider.remember(self.o.cache, i, i)
        self.assertEqual(len(self.o.cache), 256)
        self.assertNotIn(3, self.o.cache)
        self.assertIn(4, self.o.cache)

    def test_boundary_fragments_preserve_cached_lines_outside_damage(self):
        old = [line('top', y=45, h=15), line('old', y=75, h=15), line('bottom', y=100, h=20)]
        fresh = [line('top fragment', y=50, h=10), line('new', y=75, h=15), line('bottom fragment', y=100, h=10)]
        result = merge_lines(old, fresh, dict(x=0, y=50, w=560, h=60), dict(x=0, y=74, w=560, h=12))
        self.assertEqual(result, [old[0], fresh[1], old[2]])

    def test_line_touching_damage_is_replaced(self):
        old = [line('old', y=50, h=20)]
        new = [line('new', y=50, h=20)]
        self.assertEqual(merge_lines(old, new, dict(x=0, y=50, w=560, h=50),
                                     dict(x=0, y=60, w=560, h=10)), new)

    def test_window_move_changes_desktop_boxes_and_cache_key(self):
        self.warm([line()])
        win = self.w.cache.windows[10]
        self.w.cache.upsert(10, dict(win, geometry=dict(x=40, y=30, w=560, h=280)))
        self.assertIsNone(self.o.read(self.handle))
        self.assertEqual(self.o.requests, 2)


class PredicatesAndObservations(OCRHarness):
    def pending_live_shape(self, app_state, snapshot=False):
        # Establish another app before registering the target as never-published.
        self.later()
        healthy = self.w.cache.handle(11)
        self.w.x.window_pids[11] = 202
        self.w.cache.set('focus', healthy)
        self.w.cache.commit()
        self.p.tick()
        self.answer(self.raw, task=next(t for t in self.p.pending.values() if t['window'] == healthy))
        if not snapshot:
            self.p.cache.prune(self.handle)
            self.p.probes.pop(self.handle, None)
            self.p.layouts.pop(self.handle, None)
        self.p.first_map[self.handle] = self.now
        providers = self.w.providers()
        app = providers['atspi']['apps']['101']
        # Raj's system.info payload: no measured breach or completed snapshot.
        app.update(state=app_state, reason=None, breaches=0, next_probe_ms=0)
        app['window_status'][self.handle].update(state='pending', reason=None,
                                               breaches=0, next_probe_ms=0)
        # Only the Openbox pseudo-window may account for aggregate degradation.
        providers['atspi']['apps']['Openbox'] = dict(pid=None, state='degraded', windows=[], window_status={})
        providers['atspi']['state'] = 'degraded'
        return healthy, providers

    def check_live_shape_delivery(self, app_state, snapshot=False):
        healthy, providers = self.pending_live_shape(app_state, snapshot)
        self.now += 2.0
        original_status, original_readiness = self.p.status, self.p.readiness
        def status(h):
            return ('usable' if snapshot else 'pending') if h == self.handle else original_status(h)
        def readiness(h):
            return 'pending' if h == self.handle else original_readiness(h)
        with patch.object(self.w, 'providers', return_value=providers), \
                patch.object(self.p, 'status', side_effect=status), \
                patch.object(self.p, 'readiness', side_effect=readiness):
            for _ in range(5):
                params = dict(channels=['auto'], since_rev=self.w.cache.rev)
                result = self.w.observation(params)
                self.w.observed(self.peer, params, result)
                self.assertNotIn('ocr', result)
                self.assertEqual(result['providers']['atspi']['apps']['101']['state'], app_state)
            params = dict(scope=dict(kind='window', window=self.handle),
                          channels=['auto'], since_rev=self.w.cache.rev)
            # Exercise the RPC fast path itself, not just the decision helper.
            self.peer.waits = []
            self.w.observe(self.peer, 77, params)
            self.assertTrue(any(p['id'] == 77 for p in self.peer.waits))
            self.assertEqual(self.o.requests, 1)
            lines = [line('Add to list'), line('Clicks: 0', y=170), line('Item 1', y=210)]
            self.answer_ocr(lines)
            result = self.w.observation(params, wait_ocr=True)
            self.assertFalse(result['unchanged'])
            self.assertEqual(result['ocr']['lines'], lines)
            self.w.observed(self.peer, params, result)
            with patch.object(self.o, 'read', side_effect=AssertionError('already delivered / healthy OCR')):
                for _ in range(3):
                    result = self.w.observation(params)
                    self.assertTrue(result['unchanged'])
                    self.w.observed(self.peer, params, result)
                self.assertTrue(self.w.observation(dict(scope=dict(kind='window', window=healthy),
                                                       since_rev=self.w.cache.rev))['unchanged'])
            self.assertEqual(self.o.requests, 1)

    def test_live_pending_never_published_after_session_observes(self):
        self.check_live_shape_delivery('pending')

    def test_live_app_degraded_with_pending_window_after_session_observes(self):
        self.check_live_shape_delivery('degraded')

    def test_app_degraded_overrides_even_a_usable_publication(self):
        self.check_live_shape_delivery('degraded', snapshot=True)

    def test_pending_map_grace_is_two_seconds_even_on_a_changed_observe(self):
        _, providers = self.pending_live_shape('pending')
        # Observation can run before the provider has recorded this map.
        mapped_at = self.p.first_map.pop(self.handle)
        with patch.object(self.w, 'providers', return_value=providers), \
                patch.object(self.p, 'status', return_value='pending'), \
                patch.object(self.p, 'readiness', return_value='pending'):
            params = dict(scope=dict(kind='window', window=self.handle), channels=['auto'])
            for age in (0, 1.999):
                self.now = mapped_at+age
                with patch.object(self.o, 'read', side_effect=AssertionError('map grace OCR')):
                    self.assertNotIn('ocr', self.w.observation(params))
                    self.assertTrue(self.w.observation(dict(params, since_rev=self.w.cache.rev))['unchanged'])
            self.assertEqual(self.p.first_map[self.handle], mapped_at)
            self.now = mapped_at+2.0
            with self.assertRaises(OCRPending):
                self.w.observation(dict(params, since_rev=self.w.cache.rev), wait_ocr=True)

    def test_window_health_degradation_overrides_usable_publication(self):
        # Contract defence for a split publication/health view. The real
        # provider sequence below currently reports degraded in BOTH views;
        # this test does not claim that it reproduces Raj's live failure.
        self.degrade_tree()
        providers = self.w.providers()
        with patch.object(self.w, 'providers', return_value=providers), \
                patch.object(self.p, 'status', return_value='usable'), \
                patch.object(self.p, 'readiness', return_value='healthy'):
            params = dict(scope=dict(kind='window', window=self.handle),
                          channels=['auto'], since_rev=self.w.cache.rev)
            with self.assertRaises(OCRPending):
                self.w.observation(params, wait_ocr=True)
            self.answer_ocr([line()])
            result = self.w.observation(params, wait_ocr=True)
            self.assertFalse(result['unchanged'])
            self.assertEqual(result['ocr']['lines'], [line()])
            self.w.observed(self.peer, params, result)
            with patch.object(self.o, 'read', side_effect=AssertionError('already served')):
                self.assertTrue(self.w.observation(params)['unchanged'])

    def test_never_published_hang_after_session_health_polling(self):
        # Rebuild the first window as a mapped but never-published app. The
        # second app is healthy and focused throughout the session polls.
        self.p.cache.prune(self.handle)
        self.p.probes.pop(self.handle)
        self.p.layouts.pop(self.handle)
        self.later()
        healthy = self.w.cache.handle(11)
        self.w.x.window_pids[11] = 202
        self.w.cache.set('focus', healthy)
        self.w.cache.commit()
        self.p.tick()
        hung_task = next(t for t in self.p.pending.values() if t['window'] == self.handle)
        healthy_task = next(t for t in self.p.pending.values() if t['window'] == healthy)
        self.answer(self.raw, task=healthy_task)
        self.assertNotIn(self.handle, self.p.cache.windows)
        self.assertEqual(self.p.status(self.handle), 'pending')
        self.assertEqual(self.p.readiness(self.handle), 'healthy')
        for _ in range(3):
            params = dict(channels=['auto'], since_rev=self.w.cache.rev)
            result = self.w.observation(params)
            self.w.observed(self.peer, params, result)
            self.assertNotIn('ocr', result)
        # worker_atspi translates a failed binding call into this error. Its
        # measured round trip exceeds 1 s, outside the 2 s map grace period.
        self.now += 3.1
        self.answer(error='application_unreachable', task=hung_task,
                    max_call_ms=1100, breach=True)
        for _ in range(3):
            params = dict(channels=['auto'], since_rev=self.w.cache.rev)
            result = self.w.observation(params)
            self.w.observed(self.peer, params, result)
            app = result['providers']['atspi']['apps']['101']
            self.assertEqual(app['state'], 'degraded')
            self.assertEqual(app['window_status'][self.handle]['state'], 'degraded')
            self.assertEqual(app['reason'], 'application_unreachable')
            self.assertNotIn('ocr', result)
        self.assertNotIn(self.handle, self.p.probes)
        self.assertEqual(self.p.status(self.handle), 'degraded')
        self.assertEqual(self.p.readiness(self.handle), 'degraded')
        params = dict(scope=dict(kind='window', window=self.handle),
                      channels=['auto'], since_rev=self.w.cache.rev)
        with self.assertRaises(OCRPending):
            self.w.observation(params, wait_ocr=True)
        lines = [line('Add to list'), line('Clicks: 0', y=170), line('Alpha', y=210)]
        self.answer_ocr(lines)
        result = self.w.observation(params, wait_ocr=True)
        self.assertEqual(result['tree']['status'], 'degraded')
        self.assertEqual(result['ocr']['lines'], lines)
        self.w.observed(self.peer, params, result)
        with patch.object(self.o, 'read', side_effect=AssertionError('unchanged OCR')):
            self.assertTrue(self.w.observation(params)['unchanged'])
            healthy_params = dict(scope=dict(kind='window', window=healthy),
                                  channels=['auto'], since_rev=self.w.cache.rev)
            self.assertTrue(self.w.observation(healthy_params)['unchanged'])

    def test_presence_absence_regex_and_exact_legacy_kind(self):
        self.warm([line('Alpha 123')])
        for p, expected in [(dict(kind='ocr_text_present', text='Alpha'), True),
                            (dict(kind='ocr_text_absent', text='Other'), True),
                            (dict(kind='ocr_text_present', re=r'Alpha \d+'), True),
                            (dict(kind='ocr_text_absent', re=r'Alpha \d+'), False),
                            (dict(kind='ocr_text', text='Alpha 123'), True),
                            (dict(kind='ocr_text', text='Alpha'), False)]:
            with self.subTest(p=p):
                predicate_schema(p)
                self.assertEqual(self.e.evaluate(p), expected)

    def test_legacy_crop_dimensions_and_mode_use_existing_pixel_transform(self):
        p = dict(kind='ocr_text', text='Alpha 123', crop=dict(space='window:'+self.handle,
                 at_rev=self.w.cache.rev, x=0, y=70, width=560, height=80, mode='line'))
        self.assertIsNone(self.e.evaluate(p))
        self.assertEqual(self.o.flight['rect'], dict(x=31, y=97, w=560, h=80))
        self.assertEqual(self.o.flight['mode'], 'line')
        self.answer_ocr([line()])
        self.assertTrue(self.e.evaluate(p))
        self.assertEqual(self.e.evidence(p)[0]['kind'], 'ocr')

    def test_degraded_node_presence_receipt_records_visual_fallback(self):
        self.warm([line('Open')])
        self.degrade_tree()
        p = dict(kind='node_present', match=dict(name='Open'), window=self.handle)
        receipt = self.run_action(self.params([dict(intent=dict(kind='wait'), expect=p)]))
        effect = receipt['steps'][0]['effect']
        self.assertEqual(effect['status'], 'verified')
        self.assertEqual(effect['fallback_from'], 'tree')
        self.assertEqual(effect['predicate_class'], 'ocr')
        self.assertEqual(effect['predicate']['kind'], 'ocr_text_present')
        self.assertEqual(effect['assurance'], 'visual_state')
        self.assertEqual(effect['evidence'][0]['kind'], 'ocr')
        self.assertEqual(effect['evidence'][0]['rev'], receipt['steps'][0]['rev']['verified'])

    def test_degraded_ref_text_equals_unknown_never_ocr(self):
        ref = self.refs()['entry']
        self.warm([line('ab')])
        self.degrade_tree()
        p = dict(kind='text_equals', ref=ref, value='ab', window=self.handle)
        receipt = self.run_action(self.params([dict(intent=dict(kind='wait'), expect=p)]))
        effect = receipt['steps'][0]['effect']
        self.assertEqual(effect['status'], 'unknown')
        self.assertEqual(receipt['steps'][0]['reason'], 'provider_degraded')
        self.assertNotIn('fallback_from', effect)
        self.assertEqual(effect['assurance'], 'none')

    def test_no_fallback_for_role_ref_or_container_constraints(self):
        self.degrade_tree()
        for p in [dict(kind='node_present', ref='n1'),
                  dict(kind='node_present', match=dict(name='Open', role='push_button')),
                  dict(kind='node_present', name='Open', within='n1'),
                  dict(kind='node_present', name='Open', container='n1')]:
            self.assertEqual(self.e.effective_predicate(p), p)

    def test_degraded_name_absence_maps_only_to_visual_absence(self):
        self.warm([line('Other')])
        self.degrade_tree()
        predicate = dict(kind='node_absent', name='Open', window=self.handle)
        self.assertTrue(self.e.evaluate(predicate))
        effect = self.e.evaluated_effect('verified', predicate)
        self.assertEqual(effect['predicate']['kind'], 'ocr_text_absent')
        self.assertEqual(effect['assurance'], 'visual_state')

    def test_tree_usable_skips_ocr_auto(self):
        result = self.w.observation({})
        self.assertNotIn('ocr', result)
        self.assertEqual(self.o.requests, 0)
        self.assertNotIn('frames', result)

    def test_explicit_ocr_on_usable_tree(self):
        self.warm([line()])
        result = self.w.observation(dict(channels=['ocr']))
        self.assertEqual(result['ocr']['lines'], [line()])
        self.assertEqual(result['channels_used'], ['state', 'ocr'])
        self.assertNotIn('frames', result)

    def test_blind_escalation_exactly_one_frame(self):
        self.warm([line()])
        self.degrade_tree()
        result = self.w.observation(dict(channels=['auto']))
        self.assertEqual(result['ocr']['lines'], [line()])
        self.assertEqual(len(result['frames']), 1)
        self.assertEqual(result['channels_used'], ['state', 'ocr', 'pixels'])

    def test_three_lines_skip_frame_even_degraded_tree(self):
        self.warm([line(str(i), y=50+i*40) for i in range(3)])
        self.degrade_tree()
        result = self.w.observation({})
        self.assertNotIn('frames', result)
        self.assertEqual(result['channels_used'], ['state', 'ocr'])

    def test_explicit_pixels_plus_blind_still_one_frame(self):
        self.warm([])
        self.degrade_tree()
        result = self.w.observation(dict(channels=['tree', 'ocr', 'pixels']))
        self.assertEqual(len(result['frames']), 1)
        self.assertEqual(result['channels_used'].count('pixels'), 1)

    def test_empty_and_down_tree_both_use_ocr(self):
        self.warm([line(str(i), y=50+i*40) for i in range(3)])
        for status in ('empty', 'down'):
            with self.subTest(status=status), patch.object(self.p, 'status', return_value=status):
                self.assertIn('ocr', self.w.observation({}))

    def test_unchanged_does_not_read_ocr(self):
        with patch.object(self.o, 'read', side_effect=AssertionError('unchanged read')):
            result = self.w.observation(dict(since_rev=self.w.cache.rev))
        self.assertTrue(result['unchanged'])

    def test_degradation_after_repeated_session_observes_still_serves_window_ocr(self):
        since = self.w.cache.rev
        for _ in range(5):
            result = self.w.observation(dict(since_rev=since))
            self.w.observed(self.peer, {}, result)
            self.assertTrue(result['unchanged'])
        self.degrade_tree()
        params = dict(scope=dict(kind='window', handle=self.handle), since_rev=since)
        with self.assertRaises(OCRPending):
            self.w.observation(params, wait_ocr=True)
        self.answer_ocr([line('Add to list'), line('Clicks: 0', y=170), line('Item 1', y=210)])
        result = self.w.observation(params, wait_ocr=True)
        self.assertFalse(result['unchanged'])
        self.assertEqual(result['channels_used'], ['state', 'ocr'])
        self.assertEqual(len(result['ocr']['lines']), 3)
        self.w.observed(self.peer, params, result)
        with patch.object(self.o, 'read', side_effect=AssertionError('already served')):
            self.assertTrue(self.w.observation(params)['unchanged'])
        # A healthy interval and a second degradation require fresh delivery,
        # even when all source pixels stayed identical throughout.
        self.p.apps[self.p.app(self.p.window(self.handle))].state = 'healthy'
        self.w.observed(self.peer, params, self.w.observation(params))
        self.degrade_tree()
        self.assertIn('ocr', self.w.observation(params))

    def test_healthy_window_stays_on_fast_path_with_degraded_neighbor(self):
        with patch.object(self.w, 'providers', return_value=dict(atspi=dict(state='degraded'), ocr=self.o.health())), \
                patch.object(self.o, 'read', side_effect=AssertionError('healthy OCR')):
            for _ in range(5):
                result = self.w.observation(dict(scope=dict(kind='window', handle=self.handle),
                                                 since_rev=self.w.cache.rev))
                self.assertTrue(result['unchanged'])
        self.assertEqual(self.o.requests, 0)

    def test_degraded_publication_with_pending_retry_still_bypasses_unchanged(self):
        self.warm([line()])
        with patch.object(self.p, 'status', return_value='degraded'), \
                patch.object(self.p, 'readiness', return_value='pending'):
            result = self.w.observation(dict(since_rev=self.w.cache.rev,
                scope=dict(kind='window', handle=self.handle)))
        self.assertFalse(result['unchanged'])
        self.assertEqual(result['ocr']['lines'], [line()])

    def test_symbol_only_lines_do_not_suppress_blind_frame(self):
        self.warm([line('Add to list'), line('(|', y=160), line('|', y=200)])
        self.degrade_tree()
        result = self.w.observation({})
        self.assertEqual(result['ocr']['lines'], [line('Add to list')])
        self.assertEqual(len(result['frames']), 1)

    def test_short_observation_deadline_does_not_wait_for_region_budget(self):
        self.degrade_tree()
        with self.assertRaises(OCRPending):
            self.w.observation({}, wait_ocr=True)
        started = time.monotonic()
        result = self.w.observation({}, wait_ocr=False)
        self.assertLess(time.monotonic()-started, .1)
        self.assertEqual(result['providers']['ocr']['state'], 'degraded')
        self.assertEqual(result['providers']['ocr']['reason'], 'observation_deadline')
        self.assertEqual(len(result['frames']), 1)

    def test_provider_health_in_observe_and_system_info_source(self):
        self.assertEqual(self.w.providers()['ocr'], self.o.health())
        self.assertEqual(self.w.observation({})['providers']['ocr'], self.o.health())
        self.w.stopping = False
        with patch.object(self.w, 'versions', return_value={}):
            self.assertEqual(self.w.health()['providers']['ocr'], self.o.health())

    def test_pending_observe_returns_at_deadline_while_child_is_wedged(self):
        self.degrade_tree()
        self.peer.waits = [dict(kind='observe', id=7, params=dict(lease='lease'), deadline=0)]
        self.w.peers = [self.peer]
        self.w.pending()
        self.assertFalse(self.peer.waits)
        self.assertIsNotNone(self.o.flight)
        self.assertEqual(self.result()['providers']['ocr']['reason'], 'observation_deadline')
        self.assertEqual(len(self.result()['frames']), 1)

    def test_wait_cold_ocr_is_pending_then_returns_evidence(self):
        params = dict(lease='lease', expect=dict(kind='ocr_text_present', text='Open'))
        self.peer.waits = []
        self.w.peers = [self.peer]
        self.w.wait(self.peer, 8, params)
        self.assertTrue(self.peer.waits)
        self.assertFalse(self.replies)
        self.answer_ocr([line('Open')])
        self.w.pending()
        self.assertFalse(self.peer.waits)
        self.assertTrue(self.result()['satisfied'])
        self.assertEqual(self.result()['evidence'][0]['kind'], 'ocr')

    def test_mutation_fallback_waits_for_new_pixels_and_retains_assurance(self):
        self.warm([line('Before')])
        self.degrade_tree()
        def on_input(event):
            if event[0] == 'key_down':
                self.paint((('Open', 24, 112),))
                self.o.read(self.handle)
                self.answer_ocr([line('Open')])
        self.t.on_write = on_input
        p = dict(kind='node_present', name='Open', window=self.handle)
        receipt = self.run_action(self.params([self.key(expect=p)]))
        self.assertTrue(any(v[0] == 'key_down' for v in self.t.log))
        self.assertEqual(receipt['status'], 'success')
        self.assertEqual(receipt['effect']['fallback_from'], 'tree')
        self.assertEqual(receipt['effect']['assurance'], 'visual_state')

    def test_coverage_is_current_only_after_source_is_read(self):
        self.assertFalse(self.o.coverage()['complete'])
        self.warm([line()])
        self.assertTrue(self.o.coverage()['complete'])
        self.paint((('Changed', 24, 112),))
        self.assertFalse(self.o.coverage()['complete'])

    def test_healthy_name_match_keeps_tree_assurance(self):
        p = dict(kind='node_present', match=dict(name='Add to list'), within=self.handle)
        self.assertTrue(self.e.evaluate(p))
        self.assertEqual(self.e.effective_predicate(p), p)
        self.assertEqual(self.e.evidence(p)[0]['provider'], 'atspi')

    def test_adapter_retains_fallback_assurance_fields(self):
        import worker_adapter
        effect = dict(status='verified', predicate_class='ocr', expect_source='explicit',
                      assurance='visual_state', fallback_from='tree')
        projected = worker_adapter.render_receipt(dict(steps=[dict(id='1', effect=effect)]))
        self.assertEqual(projected['steps'][0]['effect'], effect)


class TextTargets(OCRHarness):
    def test_tree_exact_name_precedes_ocr(self):
        with patch.object(self.o, 'read', side_effect=AssertionError('tree match already found')):
            point = self.e.point(dict(text='Add to list', within=self.handle))
        self.assertEqual(point, dict(x=150, y=125))

    def test_ocr_contains_and_nth(self):
        self.warm([line('Open first', y=60), line('Open second', y=120)])
        self.assertEqual(self.e.point(dict(text='Open', nth=1, within=self.handle)), dict(x=110, y=132))

    def test_ambiguous_returns_up_to_five_candidates(self):
        self.warm([line('Open '+str(i), y=40+i*30) for i in range(7)])
        with self.assertRaises(ActError) as cm:
            self.e.point(dict(text='Open', within=self.handle))
        self.assertEqual(cm.exception.reason, 'target_not_found')
        self.assertEqual(len(cm.exception.detail['candidates']), 5)

    def test_unmatched_and_nth_out_of_range_never_guess(self):
        self.warm([line('Open')])
        for target in [dict(text='Other'), dict(text='Open', nth=1)]:
            with self.assertRaises(ActError) as cm:
                self.e.point(dict(target, within=self.handle))
            self.assertEqual(cm.exception.reason, 'target_not_found')

    def test_pending_text_target_waits_in_admission(self):
        with self.assertRaises(OCRPending):
            self.e.point(dict(text='Open', within=self.handle))
        self.answer_ocr([line('Open')])
        self.assertEqual(self.e.point(dict(text='Open', within=self.handle)), dict(x=110, y=133))

    def test_text_target_is_rechecked_before_dispatch(self):
        self.warm([line('Open')])
        step = dict(target=self.handle, require={}, intent=dict(kind='click', at=dict(text='Open')))
        handle, point = self.e.resolve(step, self.w.cache.rev)
        with patch.object(self.e, 'text_point', return_value=dict(x=point['x']+10, y=point['y'])):
            with self.assertRaises(ActError) as cm:
                self.e.guards({}, step, self.w.cache.rev, handle, point)
        self.assertEqual(cm.exception.reason, 'stale_rev')


class Breakers(OCRHarness):
    def test_wedge_1000ms_restart_once_then_down(self):
        self.o.read(self.handle)
        first = self.ocr_children[0][1]
        self.now += .999
        self.o.tick()
        self.assertEqual(self.o.requests, 1)
        self.now += .001
        self.o.tick()
        self.assertEqual(first.code, -9)
        self.assertEqual(len(self.ocr_children), 2)
        self.o.tick()  # new child's ready retries the same region
        self.assertEqual(self.o.requests, 2)
        self.now += 1
        self.o.tick()
        self.assertEqual(len(self.ocr_children), 2)
        self.assertEqual(self.o.state, 'down')
        self.assertEqual(self.o.health()['reason'], 'timeout')
        self.assertEqual(self.o.health()['breaches'], 2)
        self.assertEqual(self.o.health()['next_probe_ms'], 1000)

    def test_crash_retries_once_then_down_without_blocking(self):
        self.o.read(self.handle)
        self.o.child.code = 7
        self.o.tick()
        self.o.tick()
        self.o.child.code = 7
        self.o.tick()
        self.assertEqual(self.o.state, 'down')
        self.assertEqual(self.o.requests, 2)
        self.assertEqual(self.o.reason, 'provider_crashed')

    def test_backoff_probe_and_recovery(self):
        self.o.fail('timeout')
        self.o.tick()
        self.o.fail('timeout')
        self.now += .999
        self.o.tick()
        self.assertEqual(len(self.ocr_children), 2)
        self.now += .001
        self.o.tick()
        self.assertEqual(len(self.ocr_children), 3)
        self.o.tick()
        self.o.read(self.handle)
        self.answer_ocr([line()])
        self.assertEqual(self.o.health()['state'], 'healthy')

    def test_failures_more_than_60_seconds_apart_allow_restart(self):
        self.o.fail('provider_crashed')
        self.o.tick()
        self.now += 61
        self.o.fail('provider_crashed')
        self.assertEqual(self.o.state, 'degraded')
        self.assertEqual(len(self.ocr_children), 3)

    def test_late_reply_still_breaches(self):
        self.o.read(self.handle)
        self.now += 1
        self.answer_ocr([line()])
        self.assertEqual(self.o.breaches, 1)
        self.assertEqual(len(self.o.cache), 0)

    def test_invalid_box_is_provider_failure_and_retries_read(self):
        self.o.read(self.handle)
        self.answer_ocr([line(x=-10)])
        self.assertEqual(self.o.breaches, 1)
        self.o.tick()
        self.assertEqual(self.o.requests, 2)
        self.assertIsNotNone(self.o.flight)


class BackendContracts(unittest.TestCase):
    def test_symbol_only_lines_are_removed_but_unicode_text_is_retained(self):
        header = 'level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n'
        texts = ('(|', '|', '...', '—', 'é', '３', 'Clicks: 0', '©')
        tsv = header + '\n'.join(f'5\t1\t1\t1\t{i}\t1\t0\t{i*20}\t20\t10\t95\t{text}'
                                  for i, text in enumerate(texts))
        self.assertEqual([l['text'] for l in Backend.lines(tsv, dict(x=0, y=0))],
                         ['é', '３', 'Clicks: 0'])

    def test_low_confidence_words_are_removed_before_line_boxes(self):
        header = 'level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n'
        rows = ['5\t1\t1\t1\t1\t1\t10\t20\t30\t12\t59\tNoise',
                '5\t1\t1\t1\t1\t2\t50\t20\t40\t12\t60\tGood']
        self.assertEqual(Backend.lines(header+'\n'.join(rows), dict(x=31, y=27)),
                         [line('Good', x=56, y=37, w=20, h=6, conf=60)])

    def test_library_loaded_once_for_repeated_regions(self):
        rgb = b'\xff' * (200*80*3)
        with patch('worker_ocr.C.CDLL', wraps=ctypes.CDLL) as loader:
            backend = Backend()
            try:
                for _ in range(3):
                    self.assertEqual(backend.read(rgb, dict(x=10, y=20, w=200, h=80)), [])
            finally:
                backend.close()
            loader.assert_called_once_with('libtesseract.so.5')

    def test_exact_normalization_does_not_fold_case_or_spaces(self):
        # Exercise exact NFC/CRLF semantics through the real evaluator seam.
        owner = NS(cache=NS(focus='w'), pixels=True)
        provider = Provider.__new__(Provider)
        provider.w = owner
        provider.region = lambda p: ('w', {})
        provider.read = lambda *args: [line('Cafe\u0301')]
        self.assertTrue(provider.evaluate(dict(kind='ocr_text', text='Café')))
        self.assertFalse(provider.evaluate(dict(kind='ocr_text', text='café')))
        self.assertFalse(provider.evaluate(dict(kind='ocr_text', text='Café ')))

    def test_bad_ocr_regex_is_rejected(self):
        for p in [dict(kind='ocr_text_present', re='['), dict(kind='ocr_text_absent', re='a', text='a')]:
            with self.assertRaises(ActError):
                predicate_schema(p)


class RealChild(OCRHarness):
    def real_child(self, traced=False):
        # Keep fake cleanup registered, and give the real provider its own.
        if traced:
            harness = self
            class TracedProvider(Provider):
                def spawn(self):
                    original = subprocess.Popen
                    def popen(args, **kw):
                        harness.trace_read_fd = int(args[2])
                        return original(['strace', '-o', str(harness.path/'ocr.strace'),
                                         '-e', 'trace=read,execve,openat'] + args, **kw)
                    with patch('worker_ocr.subprocess.Popen', side_effect=popen):
                        return super().spawn()
            real = TracedProvider(self.w)
        else:
            real = Provider(self.w)
        self.addCleanup(real.close)
        self.w.ocr = real
        return real

    def read_real(self, provider):
        until = time.monotonic()+3
        while time.monotonic() < until:
            provider.tick()
            result = provider.read(self.handle)
            if result is not None:
                return result
            time.sleep(.001)
        self.fail('real child did not answer: '+str(provider.health()))

    def test_rendered_fx_canvas_boxes_within_four_pixels(self):
        provider = self.real_child()
        result = self.read_real(provider)
        _, boxes = frame()
        self.assertEqual(len(result), 4)
        for actual, expected in zip(result, boxes):
            for a, e in zip(actual['box'], expected):
                self.assertLessEqual(abs(a-e), 4)
        self.assertEqual([l['text'] for l in result[:3]], [v[0] for v in STRINGS[:3]])

    def test_fx_canvas_every_string_byte_exact_B6(self):
        self.assertEqual([l['text'] for l in self.read_real(self.real_child())], [v[0] for v in STRINGS])

    def test_200_partial_repaints_equal_fresh_full_read(self):
        edge = ('Edge text', 300, 150)
        self.paint((*STRINGS, edge))
        provider = self.real_child()
        self.read_real(provider)
        backend = Backend()
        self.addCleanup(backend.close)
        boundary_cases = 0
        for i in range(200):
            # Unique pixels avoid turning the sequence into alternating cache
            # hits. All changed content stays in one line's horizontal slab.
            strings = (STRINGS[0], (f'Alpha {100+i}', 24, 112), *STRINGS[2:], edge)
            self.paint(strings)
            provider.read(self.handle)
            if provider.flight:
                task = provider.flight
                for prior in task['previous']['lines']:
                    y, height = prior['box'][1], prior['box'][3]
                    if (overlaps(prior, task['slab']) and not overlaps(prior, task['damage']) and
                            (y <= task['slab']['y'] or y+height >= task['slab']['y']+task['slab']['h'])):
                        boundary_cases += 1
            incremental = self.read_real(provider)
            rect = self.w.cache.windows[10]['geometry']
            rgb = b''.join(self.shadow.row(rect['x'], y, rect['w']) for y in range(rect['y'], rect['y']+rect['h']))
            full = backend.read(rgb, rect)
            self.assertEqual(incremental, full, 'partial repaint '+str(i))
        self.assertGreater(provider.requests, 190)
        self.assertGreater(boundary_cases, 100)

    def test_real_child_unchanged_read_request_count(self):
        provider = self.real_child()
        result = self.read_real(provider)
        count = provider.requests
        for _ in range(10):
            self.assertEqual(provider.read(self.handle), result)
            provider.tick()
        self.assertEqual(count, 1)
        self.assertEqual(provider.requests, count)
        self.assertEqual(provider.child_requests, 1)

    def test_strace_unchanged_sends_no_child_request_and_no_cli(self):
        probe = subprocess.run(['strace', '-o', str(self.path/'ptrace-probe'), '/usr/bin/true'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if probe.returncode:
            self.skipTest('NOT RUN: sandbox denies ptrace; Raj must run the strace B6 gate')
        provider = self.real_child(traced=True)
        result = self.read_real(provider)
        trace = self.path/'ocr.strace'
        before = trace.read_text()
        requests = provider.requests
        for _ in range(5):
            self.assertEqual(provider.read(self.handle), result)
            provider.tick()
        after = trace.read_text()
        self.assertEqual(provider.requests, requests)
        pattern = r'read\('+str(self.trace_read_fd)+r',.*'
        self.assertGreaterEqual(len(re.findall(pattern, before)), 2)
        self.assertEqual(re.findall(pattern, before), re.findall(pattern, after))
        self.assertIn('libtesseract.so.5', after)
        self.assertNotRegex(after, r'execve\("[^"\n]*/tesseract"')


if __name__ == '__main__':
    unittest.main()
