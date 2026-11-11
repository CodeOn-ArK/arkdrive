#!/usr/bin/python3
"""F7 admission regressions: completed snapshots, fake IPC and explicit time."""
import unittest

import test_worker_w4 as h


class Admission(h.ProviderHarness):
    def step(self, **extra):
        return dict(target=dict(window=self.handle, ref=self.refs()['button']),
                    intent=dict(kind='click'), timeout_ms=300,
                    expect=dict(kind='node_state', ref=self.refs()['button'], state='selected'), **extra)

    def refresh(self, absent=False):
        self.p.dirty.add(self.handle)
        if absent:
            self.p.cache.windows.pop(self.handle)
            self.p.probes.pop(self.handle, None)
        return self.p.request('snapshot', self.handle)

    def click_effect(self):
        def effect():
            if ('button_down', 1) in self.t.log:
                self.raw['children'][1]['states'] = ['selected']
                self.p.cache.update(self.handle, self.raw, self.w.cache.rev + 1)
                self.p.dirty.discard(self.handle)
                self.p.bump([self.handle])
        self.t.hook = effect

    def test_ref_act_with_healthy_inflight_large_tree_completes(self):
        self.raw['children'].extend(h.node('extra'+str(i), name=str(i)) for i in range(600))
        self.p.cache.update(self.handle, self.raw, 1)
        step = self.step()
        task = self.refresh()
        self.click_effect()
        self.submit(self.params([step]))
        for _ in range(80):
            self.tick()
        self.assertFalse(self.replies, 'healthy refresh was rejected at admission')
        self.assertEqual(self.p.health()['atspi']['apps']['101']['state'], 'healthy')
        self.assertEqual(self.p.apps['101'].breaches, 0)
        self.answer(self.raw, task=task)
        self.drain()
        self.assertEqual(self.result()['status'], 'success', self.result())
        self.assertIn(('button_down', 1), self.t.log)

    def test_no_completed_snapshot_waits_for_refresh_then_admits(self):
        step = self.step()
        # A first walk has not issued refs: use a physical target and a tree
        # expectation. Existing selector revision guards still apply to targets.
        step['target'] = dict(window=self.handle, point=dict(x=150, y=125))
        step['expect'] = dict(kind='node_state', window=self.handle,
                              selector=dict(provider='atspi', role='push_button'), state='selected')
        task = self.refresh(absent=True)
        self.click_effect()
        self.submit(self.params([step]))
        for _ in range(80):
            self.tick()
        self.assertFalse(self.replies)
        self.assertFalse(self.t.log)
        self.answer(self.raw, task=task)
        self.drain()
        self.assertEqual(self.result()['status'], 'success', self.result())

    def test_no_completed_snapshot_times_out_at_validate(self):
        step = self.step()
        self.refresh(absent=True)
        started = self.now
        r = self.run_action(self.params([step]))
        self.assertGreaterEqual(self.now - started, .3)
        self.assertLess(self.now - started, .35)
        self.assertEqual((r['phase'], r['reason']), ('validate', 'provider_degraded'))
        self.assertEqual(r['steps'][0]['effect']['status'], 'not_observed')
        self.assertFalse(self.t.log)

    def test_real_degradation_rejects_promptly_even_with_completed_snapshot(self):
        for failure in ('breaker', 'unreachable', 'child_down'):
            with self.subTest(failure=failure):
                step = self.step()
                if failure == 'unreachable':
                    self.p.unavailable.add(self.handle)
                elif failure == 'breaker':
                    self.p.apps['101'].degrade(self.now, 'application_unreachable')
                else:
                    self.p.ready = False
                started = self.now
                r = self.run_action(self.params([step], action_id=failure))
                self.assertLess(self.now - started, .05)
                self.assertEqual((r['phase'], r['reason']), ('validate', 'provider_degraded'))
                self.assertFalse(self.t.log)
                self.p.apps['101'].success()
                self.p.unavailable.discard(self.handle)
                self.p.ready = True

    def test_small_completed_tree_still_admits(self):
        self.click_effect()
        r = self.run_action(self.params([self.step()]))
        self.assertEqual(r['status'], 'success', r)

    def test_dirty_completed_identity_resolution_needs_no_fresh_walk(self):
        ref = self.refs()['button']
        for in_flight in (False, True):
            with self.subTest(in_flight=in_flight):
                if in_flight:
                    self.refresh()
                else:
                    self.p.dirty.add(self.handle)
                    self.p.dirty_due[self.handle] = self.now + 1
                s = self.e.normalise(self.params([self.step()]))[0]['steps'][0]
                self.assertEqual(self.e.resolve(s, 1)[0], self.handle)
                self.e.guards({}, s, 1, self.handle, dict(x=150, y=125))
                self.assertEqual(s['_node']['ref'], ref)

    def test_dirty_identity_still_rejects_changed_name_and_disabled_target(self):
        for failure, reason in (('name', 'stale_rev'), ('disabled', 'precondition_failed')):
            with self.subTest(failure=failure):
                self.raw['children'][1]['name'] = 'Changed' if failure == 'name' else 'Add to list'
                self.raw['children'][1]['states'] = ['disabled'] if failure == 'disabled' else []
                self.p.cache.update(self.handle, self.raw, 1)
                self.p.dirty.add(self.handle)
                self.p.dirty_due[self.handle] = self.now + 1
                r = self.run_action(self.params([self.step()], action_id=failure))
                self.assertEqual((r['phase'], r['reason']), ('validate', reason))
                self.assertFalse(self.t.log)

    def test_requires_use_completed_snapshot_while_refresh_inflight(self):
        self.raw['children'][0]['states'].append('selected')
        self.p.cache.update(self.handle, self.raw, 1)
        ref = self.refs()['entry']
        step = self.step(require=dict(selected_ref=ref, editable_ref=ref,
                                      text_equals=dict(ref=ref, value='ab')))
        task = self.refresh()
        self.click_effect()
        self.submit(self.params([step]))
        for _ in range(80):
            self.tick()
        self.assertFalse(self.replies)
        self.answer(self.raw, task=task)
        self.drain()
        self.assertEqual(self.result()['status'], 'success', self.result())

    def test_missing_require_snapshot_waits_instead_of_precondition_failure(self):
        ref = self.refs()['entry']
        step = dict(intent=dict(kind='key', keys=['F2']), require=dict(editable_ref=ref,
                    text_equals=dict(ref=ref, value='ab')), timeout_ms=300,
                    expect=dict(kind='text_equals', ref=ref, value='new'))
        self.refresh(absent=True)
        started = self.now
        self.submit(self.params([step]))
        for _ in range(80):
            self.tick()
        self.assertFalse(self.replies)
        self.drain()
        self.assertGreaterEqual(self.now-started, .3)
        self.assertEqual((self.result()['phase'], self.result()['reason']), ('validate', 'provider_degraded'))
        self.assertFalse(self.t.log)

    def test_undecided_expectation_rechecks_real_health_promptly(self):
        step = dict(intent=dict(kind='key', keys=['F2']), timeout_ms=300,
                    expect=dict(kind='node_state', ref=self.refs()['button'], state='selected'))
        self.refresh(absent=True)
        self.submit(self.params([step]))
        for _ in range(40):
            self.tick()
        self.assertFalse(self.replies)
        self.p.unavailable.add(self.handle)
        started = self.now
        self.drain()
        self.assertLess(self.now-started, .05)
        self.assertEqual(self.result()['reason'], 'provider_degraded')
        self.assertFalse(self.t.log)

    def test_wait_for_first_snapshot_honors_preemption(self):
        step = self.step()
        self.refresh(absent=True)
        self.submit(self.params([step]))
        for _ in range(40):
            self.tick()
        self.assertFalse(self.replies)
        self.w.cache.control['epoch'] = 2
        self.drain()
        self.assertEqual(self.result()['reason'], 'preempted')
        self.assertFalse(self.t.log)

    def test_admission_and_verification_share_one_step_timeout(self):
        step = self.step()
        task = self.refresh()
        started = self.now
        self.submit(self.params([step]))
        for _ in range(220):
            self.tick()
        self.assertFalse(self.replies)
        self.assertFalse(self.t.log)
        self.answer(self.raw, task=task)
        self.drain()
        self.assertIn(('button_down', 1), self.t.log)
        self.assertEqual(self.result()['reason'], 'timeout')
        self.assertGreaterEqual(self.now-started, .3)
        self.assertLess(self.now-started, .38)


class DialogAdmission(h.ProviderHarness):
    def dialog(self):
        return h.Dialogs.dialog(self)

    def test_completed_dirty_modal_require_and_binding_export(self):
        handle, raw = self.dialog()
        self.e.dialog_initial = 'Folder'
        self.p.classify(handle, 'rename', initial='Folder', remember=True)
        self.p.dirty.add(handle)
        self.p.request('snapshot', handle)
        p = dict(kind='dialog_present', parent=self.handle, role='rename',
                 bind=dict(dialog='dialog', entry=dict(role='text')))
        exports = self.e.exports(p)
        self.assertEqual(exports['entry']['window'], handle)
        s = self.e.bound(dict(target=dict(from_step='dialog', name='entry'),
                             intent=dict(kind='key', keys=['F2']), require=dict(modal_role='rename')),
                         dict(dialog=exports))
        target, point = self.e.resolve(s, self.w.cache.rev)
        self.e.guards({}, s, self.w.cache.rev, target, point)

    def test_modal_require_without_snapshot_waits_to_deadline(self):
        handle, raw = self.dialog()
        self.p.cache.windows.pop(handle)
        self.p.probes.pop(handle, None)
        self.p.dirty.add(handle)
        step = dict(target=handle, intent=dict(kind='key', keys=['F2']),
                    require=dict(modal_role='rename'), timeout_ms=200,
                    expect=dict(kind='text_equals', window=handle, selector=dict(provider='atspi', role='text'), value='new'))
        started = self.now
        r = self.run_action(self.params([step], base_rev=self.w.cache.rev))
        self.assertGreaterEqual(self.now-started, .2)
        self.assertEqual((r['phase'], r['reason']), ('validate', 'provider_degraded'))
        self.assertFalse(self.t.log)

    def test_dialog_expectation_waits_through_healthy_refresh(self):
        handle, raw = self.dialog()
        self.p.classify(handle, 'rename', initial='Folder', remember=True)
        self.p.dirty.add(handle)
        task = self.p.request('snapshot', handle)
        def effect():
            if ('key_down', 36) in self.t.log:
                self.w.cache.remove(11)
                self.w.cache.set('focus', self.handle)
                self.w.cache.set('modal', [])
                self.w.cache.commit()
        self.t.hook = effect
        step = dict(target=handle, require=dict(modal_role='rename'),
                    intent=dict(kind='key', keys=['Return']), timeout_ms=300,
                    expect=dict(kind='dialog_absent', handle=handle, role='rename'))
        self.submit(self.params([step], base_rev=self.w.cache.rev))
        for _ in range(80):
            self.tick()
        self.assertFalse(self.replies)
        self.assertFalse(self.t.log)
        self.answer(raw, task=task)
        self.drain()
        self.assertEqual(self.result()['status'], 'success', self.result())


if __name__ == '__main__':
    unittest.main()
