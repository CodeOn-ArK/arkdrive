#!/usr/bin/python3
"""Retry readiness and serialized-child supervision; fake IPC/time only."""
import copy
import unittest

import test_worker_w4 as h


class RetryReadiness(h.ProviderHarness):
    def gap(self, grace=False):
        # Real single measured breach with a closed breaker. Map grace can
        # leave breaches at zero while the unavailable retry is still present.
        self.p.first_map[self.handle] = self.now if grace else self.now - 3
        task = self.p.request('snapshot', self.handle)
        self.answer(self.raw, task=task, max_call_ms=210, breach=True)
        self.assertEqual(self.p.apps['101'].state, 'healthy')
        self.assertIn(self.handle, self.p.unavailable)
        self.assertFalse(self.p.pending)

    def step(self, timeout=1500, ref=False):
        return dict(target=dict(window=self.handle, ref=self.refs()['button']) if ref else self.handle,
                    intent=dict(kind='key', keys=['F2']), timeout_ms=timeout,
                    expect=dict(kind='node_present', window=self.handle,
                                selector=dict(provider='atspi', role='tree_table')))

    def advance(self, seconds):
        end = self.now + seconds
        while self.now < end:
            self.tick()

    def install_effect(self):
        def effect():
            if ('key_down', 68) in self.t.log and len(self.raw['children']) == 2:
                self.raw['children'].append(h.node('details', 'tree_table', 'Details'))
                self.p.cache.update(self.handle, self.raw, self.w.cache.rev + 1)
                self.p.bump([self.handle])
        self.t.hook = effect

    def test_selector_expect_waits_through_gap_and_retry_then_dispatches_once(self):
        self.gap()
        self.install_effect()
        self.submit(self.params([self.step()]))
        self.advance(.9)
        self.assertFalse(self.replies, 'retry gap was rejected at validation')
        self.assertFalse(self.t.log)
        self.advance(.12)
        self.assertEqual(len(self.p.pending), 1)
        self.assertFalse(self.replies)
        self.answer(self.raw, max_call_ms=10, breach=False)
        self.drain()
        self.assertEqual(self.result()['status'], 'success', self.result())
        self.assertEqual(self.t.log.count(('key_down', 68)), 1)
        self.assertEqual(self.p.apps['101'].breaches, 0)

    def test_ref_admission_read_waits_for_retry_snapshot(self):
        self.gap()
        self.install_effect()
        self.submit(self.params([self.step(ref=True)]))
        self.advance(1.02)
        self.assertFalse(self.replies)
        self.assertFalse(self.t.log)
        self.answer(self.raw, max_call_ms=10, breach=False)
        self.drain()
        self.assertEqual(self.result()['status'], 'success', self.result())

    def test_inflight_retry_without_remaining_due_marker_is_undecided(self):
        self.gap()
        self.now += 1.01
        self.p.tick()
        self.p.dirty_due.pop(self.handle)
        self.install_effect()
        self.submit(self.params([self.step(ref=True)]))
        self.advance(.1)
        self.assertFalse(self.replies)
        self.assertFalse(self.t.log)
        self.answer(self.raw, max_call_ms=10, breach=False)
        self.drain()
        self.assertEqual(self.result()['status'], 'success', self.result())

    def test_retry_gap_expires_at_shared_step_budget_without_input(self):
        self.gap()
        started = self.now
        result = self.run_action(self.params([self.step(timeout=300)]))
        self.assertGreaterEqual(self.now - started, .3)
        self.assertLess(self.now - started, .35)
        self.assertEqual((result['phase'], result['reason']), ('validate', 'provider_degraded'))
        self.assertFalse(self.t.log)

    def test_breaker_open_rejects_promptly_despite_scheduled_retry(self):
        self.gap()
        self.p.apps['101'].fail(self.now, .21)
        started = self.now
        result = self.run_action(self.params([self.step()]))
        self.assertLess(self.now - started, .05)
        self.assertEqual(result['reason'], 'provider_degraded')
        self.assertFalse(self.t.log)

    def test_child_down_without_recovery_rejects_promptly(self):
        self.gap()
        self.p.down()
        self.p.restart_at = float('inf')
        started = self.now
        result = self.run_action(self.params([self.step()]))
        self.assertLess(self.now - started, .05)
        self.assertEqual(result['reason'], 'provider_degraded')
        self.assertFalse(self.t.log)

    def test_unavailable_without_retry_is_genuine_degradation(self):
        self.p.unavailable.add(self.handle)
        started = self.now
        result = self.run_action(self.params([self.step()]))
        self.assertLess(self.now - started, .05)
        self.assertEqual(result['reason'], 'provider_degraded')

    def test_breaker_open_while_waiting_stops_promptly(self):
        self.gap()
        self.submit(self.params([self.step()]))
        self.advance(.1)
        self.assertFalse(self.replies)
        self.p.apps['101'].degrade(self.now, 'application_unreachable')
        started = self.now
        self.drain()
        self.assertLess(self.now - started, .05)
        self.assertEqual(self.result()['reason'], 'provider_degraded')

    def test_gap_wait_honors_preemption(self):
        self.gap()
        self.submit(self.params([self.step()]))
        self.advance(.1)
        self.assertFalse(self.replies)
        self.w.cache.control['epoch'] += 1
        self.drain()
        self.assertEqual(self.result()['reason'], 'preempted')
        self.assertFalse(self.t.log)

    def test_verification_waits_through_retry_gap_without_replaying_input(self):
        fired = []
        def effect():
            if ('key_down', 68) in self.t.log and not fired:
                fired.append(self.now)
                self.gap()
        self.t.hook = effect
        self.submit(self.params([self.step()]))
        self.advance(.2)
        self.assertTrue(fired)
        self.assertFalse(self.replies, 'retry gap was rejected during verification')
        self.advance(.9)
        raw = copy.deepcopy(self.raw)
        raw['children'].append(h.node('details', 'tree_table', 'Details'))
        self.answer(raw, max_call_ms=10, breach=False)
        self.drain()
        self.assertEqual(self.result()['status'], 'success', self.result())
        self.assertEqual(self.t.log.count(('key_down', 68)), 1)

    def test_verification_retry_gap_exhausts_step_budget_as_unknown(self):
        fired = []
        def effect():
            if ('key_down', 68) in self.t.log and not fired:
                fired.append(self.now)
                self.gap()
        self.t.hook = effect
        started = self.now
        result = self.run_action(self.params([self.step(timeout=300)]))
        self.assertTrue(fired)
        self.assertGreaterEqual(self.now - started, .3)
        self.assertLess(self.now - started, .4)
        self.assertEqual((result['phase'], result['reason']), ('verify', 'provider_degraded'))
        self.assertEqual(result['steps'][0]['effect']['status'], 'unknown')
        self.assertEqual(self.t.log.count(('key_down', 68)), 1)

    def test_hung_app_breach_still_rejects_promptly_with_retry_inflight(self):
        self.gap()
        self.now += 1.01
        self.p.call_progress = True
        self.p.tick()
        task = next(iter(self.p.pending.values()))
        self.now += 2.01
        self.wire.incoming.append(dict(event='progress', id=task['id'],
                                       active_call_ms=2010, max_call_ms=10, breach=True))
        self.p.tick()
        self.assertEqual(self.p.apps['101'].state, 'degraded')
        started = self.now
        result = self.run_action(self.params([self.step()]))
        self.assertLess(self.now - started, .05)
        self.assertEqual(result['reason'], 'provider_degraded')
        self.assertFalse(self.t.log)

    def test_observe_payload_exposes_gap_and_inflight_retry_per_window(self):
        self.gap(grace=True)
        # Same app has a healthy second window: aggregation must retain the
        # affected window's status regardless of iteration order.
        self.later()
        other = self.w.cache.handle(11)
        self.w.x.window_pids[11] = 101
        self.p.cache.update(other, self.raw, self.w.cache.rev)
        self.p.probes[other] = 'usable'
        self.p.layouts[other] = self.w.cache.facets.get('geometry['+other+']', 1)
        for _ in range(11):
            result = self.w.observation(dict(channels=['tree'], since_rev=self.w.cache.rev,
                                            scope=dict(kind='window', window=self.handle)))
            app = result['providers']['atspi']['apps']['101']
            self.assertEqual(app['breaches'], 0)
            self.assertEqual(app['state'], 'pending')
            self.assertEqual(app['window_status'][self.handle]['state'], 'pending')
            self.assertEqual(app['window_status'][other]['state'], 'healthy')
            # The existing tree-page status describes usability; the providers
            # payload supplies the per-window admission/readiness state.
            self.assertEqual(result['tree']['status'], 'degraded')
            self.now += .1
            self.p.tick()
        self.answer(self.raw, max_call_ms=10, breach=False)
        app = self.p.health()['atspi']['apps']['101']
        self.assertEqual(app['state'], 'healthy')
        self.assertEqual(app['window_status'][self.handle]['state'], 'healthy')

    def test_unmatched_beyond_map_allowance_is_prompt_degradation(self):
        self.p.first_map[self.handle] = self.now - 31
        task = self.p.request('snapshot', self.handle)
        self.answer(dict(key='unmatched', role='frame', showing=False, visible=False,
                         status='unmatched'), task=task)
        started = self.now
        result = self.run_action(self.params([self.step()]))
        self.assertLess(self.now - started, .05)
        self.assertEqual(result['reason'], 'provider_degraded')
        self.assertFalse(self.t.log)


class SerializedHeartbeat(h.ProviderHarness):
    def flight(self):
        # This capability is advertised by the single-owner child. Legacy
        # children keep their existing one-second heartbeat behavior.
        self.wire.incoming.append(dict(event='ready', call_progress=True, serialized_jobs=True))
        self.p.tick()
        return self.p.request('snapshot', self.handle)

    def test_busy_owner_can_complete_after_one_second_ping_budget(self):
        task = self.flight()
        self.now = task['started'] + 2.01
        self.p.tick()
        self.assertIsNotNone(self.p.ping)
        self.now += 1.01
        self.p.tick()
        self.assertIsNone(self.child.poll(), 'busy GLib owner was restarted by its queued ping')
        self.answer(self.raw, task=task, max_call_ms=10, breach=False)
        self.now += .01
        self.wire.incoming.append(dict(event='pong'))
        self.p.tick()
        self.assertTrue(self.p.healthy(self.handle))
        self.assertEqual(self.p.apps['101'].breaches, 0)

    def test_serialized_wedged_job_still_restarts_at_five_seconds(self):
        task = self.flight()
        self.now = task['started'] + 2.01
        self.p.tick()
        self.now = task['started'] + 5.001
        self.p.tick()
        self.assertEqual(self.child.poll(), -9)
        self.assertEqual(task['error'], 'provider_degraded')
        self.assertEqual(self.p.apps['101'].breaches, 0)

    def test_unanswered_ping_after_job_drains_still_restarts(self):
        task = self.flight()
        self.now = task['started'] + 2.01
        self.p.tick()
        self.now += .5
        self.answer(self.raw, task=task, max_call_ms=10, breach=False)
        self.now += 1.001
        self.p.tick()
        self.assertEqual(self.child.poll(), -9)


if __name__ == '__main__':
    unittest.main()
