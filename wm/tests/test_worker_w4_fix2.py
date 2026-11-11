#!/usr/bin/python3
"""Live-failure regressions using fakes and deterministic time; no X or bus."""
import copy
import io
import json
import queue
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

import test_worker_w4 as h
import worker_atspi as atspi


class ListenerFixes(unittest.TestCase):
    def setUp(self):
        h.BackendContracts.setUp(self)
        self.b.snapshot(self.task)
        self.results = queue.Queue()

    def test_f1_nullable_sources_are_counted_logged_and_do_not_poison_next_event(self):
        with patch('worker_atspi.time.monotonic', return_value=100), patch('sys.stderr', new_callable=io.StringIO) as log:
            for source in (None, NS(app=None)) * 20:
                self.b.changed(NS(source=source), self.results)
            # F8: only source-less events drop; source-without-app events now
            # retain a coalesced conservative invalidation.
            self.assertEqual(self.b.event_drops, 20)
            self.assertEqual(self.b.global_version, 20)
            self.assertEqual(len(log.getvalue().splitlines()), 1)
            self.assertIn('unattributed', log.getvalue())
        self.assertTrue(self.results.empty())
        self.assertEqual(self.b.versions, {})
        with patch('worker_atspi.time.monotonic', return_value=100.041):
            self.b.flush_events(self.results)
        self.assertEqual(self.results.get_nowait(), dict(event='changed', global_invalidation=True))
        self.b.changed(NS(source=self.obj, type='object:text-changed'), self.results)
        self.assertEqual(self.results.get_nowait(), dict(event='changed', app='42'))
        self.assertEqual(self.b.versions['42'], 1)

    def test_f1_bad_removed_identity_keeps_invalidation_and_logs_drop(self):
        with patch('sys.stderr', new_callable=io.StringIO):
            self.b.changed(NS(source=self.obj, type='object:children-changed:remove',
                              any_data=NS(path='/gone', app=None)), self.results)
        self.assertEqual(self.results.get_nowait(), dict(event='changed', app='42'))
        self.assertEqual(self.b.event_drops, 1)

    def test_f1_registered_callback_absorbs_unexpected_errors(self):
        registry = Mock()
        glib = NS(MainLoop=lambda: NS(run=lambda: None), timeout_add=Mock())
        with patch.dict('sys.modules', {'gi': NS(require_version=Mock()),
                    'pyatspi': NS(Registry=registry, setTimeout=Mock()), 'gi.repository': NS(GLib=glib)}), \
             patch.dict('os.environ', AT_SPI_BUS_ADDRESS='fake', DBUS_SESSION_BUS_ADDRESS='fake'), \
             patch('worker_atspi.socket.socket'), patch('worker_atspi.Wire', return_value=h.FakeWire()), \
             patch.object(atspi.Backend, '_changed', side_effect=RuntimeError('listener defect')), \
             patch('sys.stderr', new_callable=io.StringIO) as log:
            atspi.main(123)
            registry.registerEventListener.call_args.args[0](NS())
            self.assertIn('worker_fault', log.getvalue())

    def test_f1_nullable_and_raising_event_metadata_never_escapes(self):
        class Broken:
            @property
            def app(self):
                raise RuntimeError('bad proxy')
        with patch('sys.stderr', new_callable=io.StringIO):
            for event in (None, NS(source=NS(app=NS(bus_name=None))), NS(source=Broken())):
                self.b.changed(event, self.results)
        self.assertEqual(self.b.event_drops, 1)  # F8 retains both source-bearing events.
        self.assertEqual(self.b.global_version, 2)
        self.assertTrue(self.results.empty())
        self.b.changed(NS(source=self.obj, type='object:state-changed'), self.results)
        self.assertEqual(self.results.get_nowait()['app'], '42')

    def test_f4_child_distinguishes_internal_faults_from_application_call_failure(self):
        for inside_call, exception, reason in (
                (False, RuntimeError('internal sentinel'), 'worker_fault'),
                (False, ValueError('bad bookkeeping'), 'worker_fault'),
                (True, RuntimeError('bus call failed'), 'application_unreachable')):
            with self.subTest(inside_call=inside_call, exception=exception):
                wire = h.FakeWire()
                wire.incoming = [dict(self.task, id=9, op='snapshot')]
                def run_job(task):
                    if inside_call:
                        return self.b.call(Mock(side_effect=exception))
                    raise exception
                callbacks = []
                glib = NS(MainLoop=lambda: NS(run=lambda: callbacks[0]()),
                          timeout_add=lambda ms, fn: callbacks.append(fn))
                def thread(**kw):
                    return NS(start=lambda: kw['target'](*kw['args']))
                with patch.dict('sys.modules', {'gi': NS(require_version=Mock()),
                        'pyatspi': NS(Registry=Mock(), setTimeout=Mock()), 'gi.repository': NS(GLib=glib)}), \
                     patch.dict('os.environ', AT_SPI_BUS_ADDRESS='fake', DBUS_SESSION_BUS_ADDRESS='fake'), \
                     patch('worker_atspi.socket.socket'), patch('worker_atspi.Wire', return_value=wire), \
                     patch('worker_atspi.Backend', return_value=self.b), \
                     patch.object(self.b, 'snapshot', side_effect=run_job), \
                     patch('worker_atspi.threading.Thread', side_effect=thread), \
                     patch('sys.stderr', new_callable=io.StringIO) as log:
                    atspi.main(123)
                reply = next(row for row in wire.sent if row.get('id') == 9)
                self.assertEqual(reply['error'], reason)
                self.assertFalse(reply['breach'])
                if reason == 'worker_fault':
                    self.assertIn(str(exception), log.getvalue())

    def test_f4_progress_measures_active_rpc_and_completed_maximum(self):
        self.b.local.job = 7
        self.b.begin()
        now = [10.0]
        def slow():
            now[0] += .3
            self.assertAlmostEqual(self.b.progress(7)['active_call_ms'], 300)
            self.assertTrue(self.b.progress(7)['breach'])
        with patch('worker_atspi.time.monotonic', side_effect=lambda: now[0]):
            self.b.call(slow)
            progress = self.b.progress(7)
        self.assertEqual(progress['active_call_ms'], 0)
        self.assertAlmostEqual(progress['max_call_ms'], 300)
        self.assertTrue(progress['breach'])


class ProviderFixes(h.ProviderHarness):
    def observe(self):
        return self.w.observation(dict(channels=['tree'], scope=dict(kind='window', window=self.handle)))['tree']

    def test_f2_first_completed_snapshot_is_served_through_continuous_events(self):
        self.p.cache.restart()
        self.p.probes.clear()
        self.p.dirty.add(self.handle)
        task = self.p.request('snapshot', self.handle)
        for i in range(5):
            self.assertIsNotNone(task)
            raw = copy.deepcopy(self.raw)
            raw['children'][1]['name'] = 'Completion '+str(i)
            self.wire.incoming.append(dict(event='changed', app='101'))
            self.answer(dict(raw, dirty=True), task=task, max_call_ms=10, breach=False)
            tree = self.observe()
            self.assertEqual(tree['status'], 'usable')
            self.assertTrue(tree['dirty'])
            self.assertIn('Completion '+str(i), tree['lines'][-1])
            self.assertIn(self.handle, self.p.dirty)
            # Continuous events do not move the already scheduled deadline.
            due = self.p.dirty_due[self.handle]
            for _ in range(3):
                self.now += .01
                self.wire.incoming.append(dict(event='changed', app='101'))
                self.p.tick()
                self.assertEqual(self.p.dirty_due[self.handle], due)
            self.now = due + .001
            self.p.tick()
            self.assertEqual(len(self.p.pending), 1)
            task = next(iter(self.p.pending.values()))
        self.assertEqual(self.p.apps['101'].breaches, 0)
        self.assertEqual(self.p.health()['atspi']['apps']['101']['state'], 'healthy')

    def test_f2_auto_observe_serves_dirty_snapshot_and_counts_metadata_in_budget(self):
        self.p.dirty.add(self.handle)
        self.assertIn('tree', self.w.observation(dict(channels=['auto']))['channels_used'])
        clean = self.p.cache.page(self.handle)
        size = len(json.dumps(clean, ensure_ascii=False, separators=(',', ':')).encode()) + 1
        tree = self.w.observation(dict(channels=['tree'], budget=dict(bytes=size)))['tree']
        self.assertLessEqual(len(json.dumps(tree, ensure_ascii=False, separators=(',', ':')).encode()), size)
        self.assertTrue(tree['dirty'])

    def text_step(self):
        return dict(target=dict(window=self.handle, ref=self.refs()['entry']),
                    intent=dict(kind='text', text='a', replace=True), timeout_ms=300)

    def start_effect(self, degraded=False):
        landed = []
        def effect():
            if not landed and len([ev for ev in self.t.log if ev == ('key_down', 38)]) >= 2:
                landed.append(self.now)
                self.raw['children'][0]['text'] = 'a'
                self.wire.incoming.append(dict(event='changed', app='101'))
                self.p.tick()
                if degraded:
                    self.p.apps['101'].degrade(self.now, 'application_unreachable')
        self.t.hook = effect
        self.submit(self.params([self.text_step()]))
        for _ in range(30):
            self.tick()
        self.assertTrue(landed)
        return landed[0]

    def test_f3_computed_text_waits_through_debounce_and_inflight_rebuild(self):
        self.start_effect()
        self.assertFalse(self.replies, 'dirty debounce gap ended verification early')
        for _ in range(80):
            self.tick()
        self.assertFalse(self.replies, 'in-flight rebuild ended verification early')
        self.answer(self.raw)
        self.drain()
        r = self.result()
        self.assertEqual(r['status'], 'success')
        self.assertEqual(r['steps'][0]['effect']['status'], 'verified')
        self.assertEqual(r['steps'][0]['expect_source'], 'default')
        self.assertGreater(r['steps'][0]['timing_ms']['verify'], 50)

    def test_f3_healthy_dirty_tree_waits_until_step_deadline(self):
        landed = self.start_effect()
        self.drain()
        self.assertGreaterEqual(self.now - landed, .25)
        self.assertEqual(self.result()['reason'], 'provider_degraded')
        self.assertEqual(self.result()['steps'][0]['effect']['status'], 'unknown')

    def test_f3_genuine_app_degradation_concludes_without_waiting_for_rebuild(self):
        landed = self.start_effect(degraded=True)
        self.drain()
        self.assertLess(self.now - landed, .1)
        self.assertEqual(self.result()['reason'], 'provider_degraded')

    def test_f3_invoke_waits_for_effect_rebuild(self):
        step = dict(target=dict(window=self.handle, ref=self.refs()['button']),
                    intent=dict(kind='invoke'), timeout_ms=300,
                    expect=dict(kind='text_equals', ref=self.refs()['entry'], value='a'))
        self.submit(self.params([step]))
        self.tick()
        task = next(t for t in self.p.pending.values() if t['op'] == 'invoke')
        self.raw['children'][0]['text'] = 'a'
        self.wire.incoming.append(dict(event='changed', app='101'))
        self.answer(dict(accepted=True), task=task)
        for _ in range(80):
            self.tick()
        self.assertFalse(self.replies)
        self.answer(self.raw)
        self.drain()
        self.assertEqual(self.result()['status'], 'success')
        self.assertFalse(self.t.log)

    def test_f3_ref_identity_waits_after_position_generates_events(self):
        entry = self.refs()['entry']
        step = dict(target=dict(window=self.handle, ref=self.refs()['button']),
                    intent=dict(kind='click'), expect=dict(kind='text_equals', ref=entry, value='a'), timeout_ms=300)
        moved = []
        def effect():
            if not moved and any(ev[0] == 'motion' for ev in self.t.log):
                moved.append(True)
                self.wire.incoming.append(dict(event='changed', app='101'))
                self.p.tick()
            if ('button_down', 1) in self.t.log and self.raw['children'][0]['text'] != 'a':
                self.raw['children'][0]['text'] = 'a'
                self.change(self.raw)
        self.t.hook = effect
        self.submit(self.params([step]))
        for _ in range(80):
            self.tick()
        self.assertFalse(self.replies)
        self.answer(self.raw)
        self.drain()
        self.assertEqual(self.result()['status'], 'success')

    def test_f3_ref_recheck_timeout_waits_without_sending_the_click(self):
        step = dict(target=dict(window=self.handle, ref=self.refs()['button']),
                    intent=dict(kind='click'), timeout_ms=200,
                    expect=dict(kind='text_equals', ref=self.refs()['entry'], value='a'))
        moved = []
        def effect():
            if not moved and any(ev[0] == 'motion' for ev in self.t.log):
                moved.append(self.now)
                self.wire.incoming.append(dict(event='changed', app='101'))
                self.p.tick()
        self.t.hook = effect
        r = self.run_action(self.params([step]))
        self.assertGreaterEqual(self.now - moved[0], .2)
        self.assertEqual(r['reason'], 'provider_degraded')
        self.assertFalse(any(ev[0] == 'button_down' for ev in self.t.log))

    def test_f4_provider_work_without_slow_calls_never_charges_app_breaker(self):
        self.now += 2.1
        task = self.p.request('snapshot', self.handle)
        self.now += 2.1
        self.wire.incoming.append(dict(event='progress', id=task['id'], max_call_ms=10,
                                       active_call_ms=0, breach=False))
        self.p.tick()
        b = self.p.apps['101']
        self.assertEqual((b.state, b.reason, b.breaches), ('healthy', None, 0))
        self.answer(dict(self.raw, dirty=True), task=task, max_call_ms=10, breach=False)
        self.assertFalse(task['breached'])
        self.assertEqual(self.observe()['status'], 'usable')

    def test_f4_measured_slow_call_still_charges_app_breaker(self):
        self.now += 2.1
        task = self.p.request('snapshot', self.handle)
        self.now += 2.1
        self.wire.incoming.append(dict(event='progress', id=task['id'], max_call_ms=10,
                                       active_call_ms=2100, breach=True))
        self.p.tick()
        self.assertEqual(self.p.apps['101'].reason, 'application_unreachable')
        self.assertEqual(self.p.apps['101'].breaches, 1)

    def test_f4_missing_progress_from_current_child_is_not_app_unreachability(self):
        self.p.call_progress = True
        self.now += 2.1
        self.p.request('snapshot', self.handle)
        self.now += 2.1
        self.p.tick()
        self.assertEqual(self.p.apps['101'].breaches, 0)
        self.assertIsNone(self.p.apps['101'].reason)
        self.now += 1.001
        self.p.tick()
        health = self.p.health()['atspi']['apps']['101']
        self.assertEqual(health['reason'], 'provider_restarting')
        self.assertEqual(health['breaches'], 0)

    def test_f4_internal_failure_reports_worker_fault_without_breaches(self):
        self.now += 2.1
        task = self.p.request('snapshot', self.handle)
        self.answer(error='worker_fault', task=task, max_call_ms=10, breach=False)
        app = self.p.health()['atspi']['apps']['101']
        self.assertEqual((app['state'], app['reason'], app['breaches']), ('degraded', 'worker_fault', 0))


if __name__ == '__main__':
    unittest.main()
