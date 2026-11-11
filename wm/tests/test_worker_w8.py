"""W8 headless contracts: real engine/table/provider, fake X selection oracle."""
import ast
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from test_worker_w2 import Harness, Engine, GEN, ActError, DeferredMetadata
import test_worker_w4 as tree_tests
from worker_routes import RouteTable, ladder, KEY_FIELDS
from worker_paste import Clipboard
from worker_atspi import Backend
from worker_pixels import Pixels, Shadow
from Xlib import X, Xatom


class Routing(unittest.TestCase):
    def test_table_all_operations_and_backends(self):
        for backend in ('xvfb', 'xorg'):
            physical = ['evdev', 'xtest'] if backend == 'xorg' else ['xtest']
            for kind in ('key', 'move', 'drag', 'scroll'):
                with self.subTest(backend=backend, kind=kind):
                    self.assertEqual(ladder(backend, kind), physical)
            self.assertEqual(ladder(backend, 'text', editable=True), physical+['paste', 'atspi'])
            self.assertEqual(ladder(backend, 'text'), physical+['paste'])
            self.assertEqual(ladder(backend, 'click', action=True), physical+['atspi'])
            self.assertEqual(ladder(backend, 'click'), physical)
            self.assertEqual(ladder(backend, 'invoke'), ['atspi'])

    def test_text_first_rung_threshold_mapping_and_via(self):
        for backend in ('xvfb', 'xorg'):
            for via in ('auto', 'paste', 'keys'):
                for length in (0, 200, 201, 2000):
                    for mapped in (True, False):
                        routes = ladder(backend, 'text', via, mapped, length, True)
                        first_keys = via == 'keys' or via == 'auto' and mapped and length <= 200
                        expected = ('evdev' if backend == 'xorg' else 'xtest') if first_keys else 'paste'
                        self.assertEqual(routes[0], expected)
                        self.assertEqual('paste' in routes, via != 'keys')
                        self.assertEqual('atspi' in routes, via != 'keys')


class LearnedTable(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'route-table.json'
        self.t = RouteTable(self.path, GEN)
        self.identity = dict(zip(KEY_FIELDS, ('profile', 'build', 'gtk', 'entry', 'text', 'keyboard')))
        self.key = self.t.entry(self.identity, ['xtest', 'paste', 'atspi'])

    def miss(self, **changes):
        values = dict(verified=False, acknowledged=True, focused_before=True, focused_after=True,
                      no_change=True, effect='not_observed')
        values.update(changes)
        return self.t.result(self.key, 'xtest', **values)

    def test_exact_two_and_persistent_flip_new_worker(self):
        self.assertFalse(self.miss())
        self.assertFalse(self.path.exists())
        self.assertTrue(self.miss())
        fresh = RouteTable(self.path, 'new')
        key = fresh.entry(self.identity, ['xtest', 'paste', 'atspi'])
        self.assertEqual(fresh.next(key, ['xtest', 'paste', 'atspi']), 'paste')
        self.assertEqual(fresh.entries[key]['states']['xtest']['misses'], 0)

    def test_each_required_condition_independently_false_breaks_streak(self):
        for field, value in [('acknowledged', False), ('focused_before', False), ('focused_after', False),
                             ('no_change', False), ('effect', 'unknown'), ('effect', 'contradicted')]:
            with self.subTest(field=field, value=value):
                self.t.entries[self.key]['states']['xtest']['misses'] = 0
                self.assertFalse(self.miss())
                self.assertFalse(self.miss(**{field: value}))
                self.assertFalse(self.miss())
                self.assertEqual(self.t.state(self.key, 'xtest'), 'ok')

    def test_non_xtest_does_not_infer_rejection(self):
        for route in ('evdev', 'paste', 'atspi'):
            for _ in range(3):
                self.assertFalse(self.t.result(self.key, route, verified=False, acknowledged=True,
                    focused_before=True, focused_after=True, no_change=True, effect='not_observed'))
            self.assertEqual(self.t.state(self.key, route), 'ok')

    def test_verified_resets_misses_and_recovers_suspect(self):
        self.miss()
        self.t.flip(self.key, 'xtest', 'suspect')
        self.assertFalse(self.miss(verified=True, effect='verified'))
        self.assertEqual(self.t.state(self.key, 'xtest'), 'ok')
        self.assertFalse(self.miss())
        self.assertTrue(self.miss())

    def test_persists_all_flips_and_recovery(self):
        for state in ('suspect', 'down', 'rejected', 'ok'):
            self.t.flip(self.key, 'xtest', state)
            self.assertEqual(RouteTable(self.path, 'new').state(self.key, 'xtest'), state)

    def test_missing_corrupt_and_malformed_are_fresh(self):
        for data in ('{', 'null', '{}', '[]', '{"version":1,"entries":null}',
                     '{"version":1,"entries":{"oops":{}}}'):
            self.path.write_text(data)
            fresh = RouteTable(self.path, 'new')
            self.assertEqual(fresh.entries, {})
            key = fresh.entry(self.identity, ['xtest'])
            fresh.flip(key, 'xtest', 'rejected')
            self.assertEqual(RouteTable(self.path, 'new').state(key, 'xtest'), 'rejected')

    def test_shared_writers_merge_entries(self):
        other = RouteTable(self.path, 'other')
        second = other.entry(dict(self.identity, app_build='other'), ['xtest'])
        self.t.flip(self.key, 'xtest', 'rejected')
        other.flip(second, 'xtest', 'down')
        self.assertEqual(len(RouteTable(self.path, 'new').entries), 2)

    def test_same_entry_writers_merge_distinct_route_flips(self):
        other = RouteTable(self.path, 'other')
        other.entry(self.identity, ['xtest', 'paste', 'atspi'])
        self.t.flip(self.key, 'xtest', 'rejected')
        other.flip(self.key, 'paste', 'down')
        fresh = RouteTable(self.path, 'new')
        self.assertEqual(fresh.state(self.key, 'xtest'), 'rejected')
        self.assertEqual(fresh.state(self.key, 'paste'), 'down')
        self.assertEqual(fresh.state(self.key, 'atspi'), 'ok')


class EngineRoutes(Harness):
    def repeat(self, step, ident):
        # Fresh read-only reconciliation is needed between unobserved actions.
        self.e.pending_effects.clear()
        self.e.observed(self.peer, {}, {'full': True, 'settled': True, 'rev': self.w.cache.rev})
        return self.run_action(self.params([step], action_id=ident, base_rev=self.w.cache.rev))

    def test_second_miss_receipt_flip_then_xvfb_undispatched_escalation(self):
        first = self.repeat(self.key(), 'one')['steps'][0]
        self.assertNotIn('route_change', first)
        second = self.repeat(self.key(), 'two')['steps'][0]
        self.assertEqual(second['route_change'], {'from': 'xtest', 'to': None,
                         'reason': 'synthetic_input_rejected_suspect'})
        before = len(self.t.log)
        third = self.repeat(self.key(), 'three')['steps'][0]
        self.assertEqual((third['dispatch_status'], third['reason'], third['detail'], third['escalate']),
                         ('not_dispatched', 'backend_unsupported', 'xtest_rejected', 'relaunch_on_xorg'))
        self.assertEqual(len(self.t.log), before)

    def test_new_engine_loads_learned_rejection(self):
        self.repeat(self.key(), 'one')
        self.repeat(self.key(), 'two')
        self.e = self.w.act = Engine(self.w, self.t, clock=lambda: self.now)
        self.e.channel = dict(state='ok', keyboard='ok', pointer='ok')
        self.e.attach('lease', self.peer, {})
        r = self.repeat(self.key(), 'three')['steps'][0]
        self.assertEqual(r['detail'], 'xtest_rejected')
        self.assertFalse(r['attempts'])

    def test_profile_classification_xorg_evdev_down_escalates(self):
        self.w.record['profile'] = 'xorg'
        self.w.profile_data['applications'] = {'Fixture': {'synthetic_input': 'rejected'}}
        r = self.run_action(self.params([self.key()]))['steps'][0]
        self.assertEqual(r['escalate'], 'restart_session')
        self.assertEqual(r['dispatch_status'], 'not_dispatched')
        self.assertFalse(self.t.log)

    def test_unknown_never_advances_and_successful_probe_leaves_suspect(self):
        self.w.record['profile'] = 'xorg'
        # xtest is used after the proven absent evdev channel.
        self.t.ack = False
        def release_ack(event):
            if event[0] == 'motion':
                self.t.emit(event)
        self.t.on_write = release_ack
        r = self.run_action(self.params([self.key()]))['steps'][0]
        self.assertEqual(r['dispatch_status'], 'unknown')
        self.assertEqual([v['route'] for v in r['attempts']], ['evdev', 'xtest'])
        key = next(iter(self.e.routes.entries))
        self.assertEqual(self.e.routes.state(key, 'xtest'), 'suspect')
        self.assertEqual(len([ev for ev in self.t.log if ev[0] == 'key_down']), 1)
        self.assertTrue(r['channel_probe'])

    def test_failed_unknown_probe_marks_down(self):
        self.t.ack = False
        r = self.run_action(self.params([self.key()]))['steps'][0]
        key = next(iter(self.e.routes.entries))
        self.assertEqual(self.e.routes.state(key, 'xtest'), 'down')
        self.assertEqual(r['dispatch_status'], 'unknown')

    def test_acknowledged_no_effect_never_advances(self):
        step = self.key(intent=dict(kind='text', text='aa', via='auto'))
        r = self.run_action(self.params([step]))['steps'][0]
        self.assertEqual([v['route'] for v in r['attempts']], ['xtest'])
        self.assertEqual(r['effect']['status'], 'not_observed')
        self.assertEqual(self.e.clipboard.health['state'], 'unavailable')

    def test_three_attempt_limit_and_no_fourth_route(self):
        self.w.record['profile'] = 'xorg'
        def fail(a, s, r, handle, point, route):
            if False:
                yield
            raise ActError('channel_down')
        self.e.dispatch_rung = fail
        self.e.route_context = lambda s, h: (self.e.routes.entry(dict(zip(KEY_FIELDS,
            ('p', 'a', 't', 's', 'text', 'keyboard'))), ['evdev', 'xtest', 'paste', 'atspi']),
            ['evdev', 'xtest', 'paste', 'atspi'])
        r = self.run_action(self.params([self.key(intent=dict(kind='text', text='a'))]))['steps'][0]
        self.assertEqual([v['route'] for v in r['attempts']], ['evdev', 'xtest', 'paste'])
        self.assertEqual(r['dispatch_status'], 'not_dispatched')

    def test_family_facet_change_blocks_inference_even_if_value_restored(self):
        def change():
            win = self.w.cache.windows[10]
            self.w.cache.upsert(10, dict(win, title='reaction'))
            self.w.cache.commit()
            self.w.cache.upsert(10, win)
            self.w.cache.commit()
        self.t.hook = change
        self.repeat(self.key(), 'one')
        self.repeat(self.key(), 'two')
        self.assertTrue(all(e['states']['xtest']['state'] == 'ok' for e in self.e.routes.entries.values()))

    def test_verified_step_resets_engine_streak(self):
        self.repeat(self.key(), 'one')
        self.t.on_write = lambda ev: self.later() if ev[0] == 'key_up' else None
        good = self.repeat(self.key(), 'two')['steps'][0]
        self.assertEqual(good['effect']['status'], 'verified')
        key = next(iter(self.e.routes.entries))
        self.assertEqual(self.e.routes.entries[key]['states']['xtest']['misses'], 0)

    def test_verified_unknown_delivery_stays_suspect_until_next_step(self):
        self.t.ack = False
        def effect_and_probe(ev):
            if ev[0] == 'key_up':
                self.later()
            if ev[0] == 'motion':
                self.t.emit(ev)
        self.t.on_write = effect_and_probe
        r = self.run_action(self.params([self.key()]))['steps'][0]
        self.assertEqual(r['dispatch_status'], 'unknown')
        self.assertEqual(r['effect']['status'], 'verified')
        key = next(iter(self.e.routes.entries))
        self.assertEqual(self.e.routes.state(key, 'xtest'), 'suspect')


class SelectionServer:
    """Independent oracle for X ownership/properties/notifications and gate order."""
    def __init__(self, worker):
        self.log, self.properties, self.notifications = [], [], []
        self.owner = 0
        self.window = NS(id=100, set_selection_owner=self.own)
        self.requestor = NS(id=11, change_property=lambda *args: self.properties.append(args),
                            send_event=lambda msg, **kw: self.notifications.append(msg))
        self.x = NS(root=NS(create_window=self.create), atoms=dict(CLIPBOARD=1, UTF8_STRING=2, STRING=3, TARGETS=4),
                    ignored=set(), ax=NS(ask=self.ask), ev=NS(display=NS(info=NS(resource_id_mask=255)),
                                                          create_resource_object=lambda *args: self.requestor))
        worker.x = self.x

    def create(self, *args, **kwargs):
        self.log.append('create')
        return self.window

    def own(self, *args, **kwargs):
        self.log.append('ownership')
        self.owner = self.window.id

    def ask(self, request, callback, **kwargs):
        callback(dict(owner=self.owner))

    def event(self, target):
        return NS(type=X.SelectionRequest, owner=100, selection=1, target=target, property=9,
                  requestor=11, time=123)


def consume(gen):
    for _ in gen:
        pass


class PasteHarness(Harness):
    def setUp(self):
        super().setUp()
        self.server = SelectionServer(self.w)
        keycode = self.t.keycode
        self.t.keycode = lambda name: 55 if name == 'v' else keycode(name)

class Paste(PasteHarness):
    def test_owner_serves_targets_utf8_string_and_string(self):
        evidence = {}
        consume(self.e.clipboard.own('é😀', self.handle, lambda: self.server.log.append('gate'), evidence))
        self.assertEqual(self.server.log, ['gate', 'create', 'gate', 'ownership'])
        self.assertEqual(self.e.clipboard.health, dict(state='ok'))
        self.assertTrue(evidence['clipboard_overwritten'])
        self.assertFalse(evidence['selection_requested'])
        for target in (4, 2, 3, 99):
            self.e.clipboard.events([self.server.event(target)])
        self.assertTrue(evidence['selection_requested'])
        self.assertEqual(self.server.properties[0], (9, Xatom.ATOM, 32, [2, 3, 4]))
        self.assertEqual(self.server.properties[1], (9, 2, 8, 'é😀'.encode()))
        self.assertEqual(self.server.properties[2], (9, 3, 8, b'\xe9?'))
        self.assertEqual(self.server.notifications[-1].property, X.NONE)

    def test_second_paste_reuses_owner_and_overwrites(self):
        clip = self.e.clipboard
        consume(clip.own('one', self.handle, lambda: None, {}))
        consume(clip.own('two', self.handle, lambda: None, {}))
        self.assertEqual(self.server.log.count('create'), 1)
        self.assertEqual(clip.text, 'two')

    def test_gate_closed_before_ownership_no_write(self):
        def closed():
            raise ActError('preempted')
        with self.assertRaises(ActError):
            consume(self.e.clipboard.own('one', self.handle, closed, {}))
        self.assertFalse(self.server.log)

    def test_ownership_failure_health(self):
        self.server.x.ax.ask = lambda req, callback, **kw: callback(dict(owner=42))
        with self.assertRaises(ActError):
            consume(self.e.clipboard.own('one', self.handle, lambda: None, {}))
        self.assertEqual(self.e.clipboard.health, dict(state='down', reason='clipboard_ownership_failed'))

    def test_paste_attempt_receipt_request_and_select_all_then_ctrl_v(self):
        def requested(ev):
            if ev == ('key_down', 55):
                self.e.clipboard.events([self.server.event(2)])
                self.later()
        self.t.on_write = requested
        step = self.key(intent=dict(kind='text', text='😀', replace=True, via='paste'))
        result = self.run_action(self.params([step]))
        r = result['steps'][0]
        self.assertEqual(r['route'], 'paste')
        self.assertEqual(r['effect']['status'], 'verified')
        self.assertEqual([v for v in self.t.log if v[0] == 'key_down'],
                         [('key_down', 37), ('key_down', 38), ('key_down', 37), ('key_down', 55)])
        self.assertTrue(r['attempts'][0]['clipboard_overwritten'])
        self.assertTrue(r['attempts'][0]['selection_requested'])

    def test_unrequested_selection_is_ordinary_not_observed(self):
        r = self.run_action(self.params([self.key(intent=dict(kind='text', text='😀'))]))['steps'][0]
        self.assertEqual(r['effect']['status'], 'not_observed')
        self.assertFalse(r['attempts'][0]['selection_requested'])
        self.assertTrue(r['attempts'][0]['clipboard_overwritten'])
        self.assertEqual(self.e.clipboard.text, '😀')

    def test_auto_long_text_starts_paste(self):
        r = self.run_action(self.params([self.key(intent=dict(kind='text', text='a'*201))]))['steps'][0]
        self.assertEqual(r['attempts'][0]['route'], 'paste')


class Semantic(tree_tests.ProviderHarness):
    def wait_task(self, op):
        for _ in range(100):
            self.tick()
            tasks = [t for t in self.p.pending.values() if t['op'] == op]
            if tasks:
                return tasks[0]
        self.fail('provider request not sent: '+op)

    def test_set_text_rung_child_request_and_existing_expect(self):
        # A rejected keys rung and an unavailable paste channel reach set-text.
        self.w.profile_data['applications'] = {'Fixture': {'synthetic_input': 'rejected'}}
        self.e.clipboard.own = Mock(side_effect=ActError('channel_down'))
        step = dict(target=dict(window=self.handle, ref=self.refs()['entry']),
                    intent=dict(kind='text', text='😀', replace=True), timeout_ms=300)
        self.submit(self.params([step]))
        task = self.wait_task('set_text')
        request = self.wire.sent[-1]
        self.assertEqual((request['key'], request['text'], request['generation'], request['epoch']),
                         ('entry', '😀', GEN, 1))
        self.answer(dict(accepted=True), task=task)
        self.raw['children'][0]['text'] = '😀'
        self.change(self.raw)
        self.drain()
        r = self.result()['steps'][0]
        self.assertEqual(r['route'], 'atspi')
        self.assertEqual(r['effect']['status'], 'verified')
        self.assertEqual([a['route'] for a in r['attempts']], ['paste', 'atspi'])
        self.assertFalse(self.t.log)

    def test_click_rung_uses_invoke_child_request(self):
        self.raw['children'][1]['actions'] = ['click']
        self.change(self.raw)
        self.w.profile_data['applications'] = {'Fixture': {'synthetic_input': 'rejected'}}
        step = dict(target=dict(window=self.handle, ref=self.refs()['button']), intent=dict(kind='click'),
                    expect=dict(kind='text_equals', ref=self.refs()['entry'], value='done'), timeout_ms=300)
        self.submit(self.params([step], base_rev=self.w.cache.rev))
        task = self.wait_task('invoke')
        self.assertEqual(self.wire.sent[-1]['key'], 'button')
        self.answer(dict(accepted=True), task=task)
        self.raw['children'][0]['text'] = 'done'
        self.change(self.raw)
        self.drain()
        self.assertEqual(self.result()['steps'][0]['route'], 'atspi')
        self.assertEqual(self.result()['status'], 'success')
        self.assertFalse(self.t.log)

    def test_already_degraded_provider_not_dispatched(self):
        step = dict(target=dict(window=self.handle, ref=self.refs()['entry']),
                    intent=dict(kind='text', text='😀'), timeout_ms=100)
        self.w.profile_data['applications'] = {'Fixture': {'synthetic_input': 'rejected'}}
        self.e.clipboard.own = Mock(side_effect=ActError('channel_down'))
        request = self.p.request
        self.p.request = lambda op, h, **kw: None if op == 'set_text' else request(op, h, **kw)
        r = self.run_action(self.params([step]))['steps'][0]
        self.assertEqual(r['dispatch_status'], 'not_dispatched')
        self.assertEqual(r['reason'], 'provider_degraded')

    def test_sent_provider_timeout_unknown_no_advance(self):
        self.w.profile_data['applications'] = {'Fixture': {'synthetic_input': 'rejected'}}
        self.e.clipboard.own = Mock(side_effect=ActError('channel_down'))
        step = dict(target=dict(window=self.handle, ref=self.refs()['entry']),
                    intent=dict(kind='text', text='😀'), timeout_ms=100)
        self.submit(self.params([step]))
        task = self.wait_task('set_text')
        task['breached'] = True
        self.drain()
        r = self.result()['steps'][0]
        self.assertEqual(r['dispatch_status'], 'unknown')
        self.assertEqual(r['attempts'][-1]['route'], 'atspi')

    def test_focused_editable_node_can_supply_window_only_text_fallback(self):
        self.w.profile_data['applications'] = {'Fixture': {'synthetic_input': 'rejected'}}
        step = dict(target=self.handle, intent=dict(kind='text', text='😀'), timeout_ms=300,
                    expect=dict(kind='text_equals', ref=self.refs()['entry'], value='😀'))
        self.submit(self.params([step]))
        task = self.wait_task('set_text')
        self.assertTrue(self.wire.sent[-1]['focused_required'])
        self.answer(dict(accepted=True), task=task)
        self.raw['children'][0]['text'] = '😀'
        self.change(self.raw)
        self.drain()
        self.assertEqual(self.result()['status'], 'success')


class PostDispatchVerification(tree_tests.ProviderHarness):
    """Real click engine/provider, explicit snapshot sequence and fake clock."""
    def setUp(self):
        super().setUp()
        self.raw['children'][0]['states'] = ['editable']
        self.p.cache.update(self.handle, self.raw, 1)
        self.samples = []

    def predicate(self):
        return dict(kind='node_state', window=self.handle, ref=self.refs()['entry'],
                    state='focused', value=True)

    def click(self, expect=None):
        return dict(target=dict(window=self.handle, ref=self.refs()['entry']),
                    intent=dict(kind='click'), expect=expect or self.predicate(), timeout_ms=300)

    def publish(self, raw):
        self.assertTrue(self.p.cache.update(self.handle, raw, self.w.cache.rev + 1))
        self.p.bump([self.handle])

    def sample_then(self, update):
        def refresh():
            self.samples.append(self.e.verification_verdict(
                self.step_expect, False, self.e.active['receipt']['steps'][0]['rev']['dispatched']))
            if len(self.samples) == 2:
                update()
        self.t.refresh_hook = refresh

    def focused_after_two_old_samples(self):
        self.step_expect = self.predicate()
        def focus():
            self.raw['children'][0]['states'].append('focused')
            self.publish(self.raw)
        self.sample_then(focus)
        return self.run_action(self.params([self.click()]))

    def test_click_old_false_then_new_true_verifies(self):
        result = self.focused_after_two_old_samples()
        self.assertEqual((result['status'], result['effect']['status']), ('success', 'verified'), result)
        self.assertEqual(self.samples, [(False, False), (False, False)])
        self.assertGreater(self.w.cache.facets['tree['+self.handle+']'], result['rev']['dispatched'])

    def window_focus_transition(self, destination):
        # A window/point click lets the click itself focus the client. Ref
        # clicks explicitly focus it before dispatch and would hide this bug.
        cache = self.w.cache
        cache.upsert(11, dict(cache.windows[10], xid=11, family='x:11', title='Old focus'))
        old = cache.handle(11)
        cache.set('focus', old)
        if destination == 'target':
            new = self.handle
        else:
            family = cache.windows[10]['family'] if destination == 'family' else 'x:12'
            cache.upsert(12, dict(cache.windows[10], xid=12, family=family,
                                 title='New focus', type='dialog' if destination == 'family' else 'normal',
                                 transient_for=10 if destination == 'family' else None))
            new = cache.handle(12)
        cache.baseline()
        expect = self.predicate()
        facets = []
        def refresh():
            cache.set('focus', new)
            if not facets:
                self.raw['children'][1]['name'] = 'Newer but still unfocused'
            else:
                self.raw['children'][0]['states'].append('focused')
            self.publish(self.raw)
            dispatched = self.e.active['receipt']['steps'][0]['rev']['dispatched']
            facets.append(cache.facets['tree['+self.handle+']'])
            self.samples.append(self.e.verification_verdict(expect, False, dispatched))
        self.t.refresh_hook = refresh
        click = dict(self.click(), target=self.handle,
                     intent=dict(kind='click', at=dict(point=dict(space='desktop', x=20, y=20))))
        result = self.run_action(self.params([click]))
        step = result['steps'][0]
        self.assertEqual(step['target_resolved'], self.handle)
        self.assertEqual((step['focus']['before'], step['focus']['after']), (old, new))
        self.assertNotIn('confirmed', step['focus'])
        self.assertTrue(all(facet > step['rev']['dispatched'] for facet in facets))
        return result

    def test_unfocused_window_click_tree_expect_target_focus_verifies(self):
        result = self.window_focus_transition('target')
        self.assertEqual((result['status'], result['effect']['status']), ('success', 'verified'), result)
        self.assertEqual(self.samples, [(False, False), (True, False)])

    def test_unfocused_window_click_tree_expect_foreign_focus_contradicts(self):
        result = self.window_focus_transition('foreign')
        self.assertEqual((result['effect']['status'], result['reason']),
                         ('contradicted', 'effect_contradicted'), result)
        self.assertEqual(self.samples, [(False, False)])
        self.assertLess(result['steps'][0]['timing_ms']['total'], 300)

    def test_unfocused_window_click_tree_expect_target_family_focus_verifies(self):
        result = self.window_focus_transition('family')
        self.assertEqual((result['status'], result['effect']['status']), ('success', 'verified'), result)
        self.assertEqual(self.samples, [(False, False), (True, False)])

    def test_transition_new_modal_target_itself_is_explained(self):
        r = dict(target=self.handle, focus=dict(before=self.handle), modal=dict(before=[]))
        self.w.cache.set('modal', [self.handle])
        self.assertIs(self.e.transition_expected(r, self.predicate()), True)

    def test_transition_new_modal_family_member_is_not_explained(self):
        # Focus within the target family is permitted; a new modal in that
        # family still needs its own STATE expectation on the opening step.
        r = dict(target=self.handle, focus=dict(before=self.handle), modal=dict(before=[]))
        self.w.cache.upsert(11, dict(self.w.cache.windows[10], xid=11, type='dialog',
                                    title='Unexpected', transient_for=10, is_modal=True))
        h = self.w.cache.handle(11)
        self.w.cache.set('focus', h)
        self.w.cache.set('modal', [h])
        self.assertIs(self.e.transition_expected(r, self.predicate()), False)

    def old_holder_then_target_focuses(self):
        self.raw['children'][1]['states'] = ['focused']
        self.p.cache.update(self.handle, self.raw, 1)
        self.step_expect = self.predicate()
        def focus():
            self.raw['children'][1]['states'] = []
            self.raw['children'][0]['states'].append('focused')
            self.publish(self.raw)
        self.sample_then(focus)
        return self.run_action(self.params([self.click()]))

    def test_old_other_holder_then_new_target_focus_verifies(self):
        result = self.old_holder_then_target_focuses()
        self.assertEqual(result['effect']['status'], 'verified', result)
        self.assertEqual(self.samples, [(False, False), (False, False)])

    def test_freshness_negative_control_old_other_holder_is_premature_contradiction(self):
        # Round 2: old target-state absence is no longer contrary even if the
        # gate is bypassed. Use a positive old holder to pin the freshness gate.
        with patch.object(self.p, 'effect_fresh', return_value=True):
            result = self.old_holder_then_target_focuses()
        self.assertEqual((result['effect']['status'], result['reason']),
                         ('contradicted', 'effect_contradicted'))
        self.assertFalse(self.samples)

    def test_new_snapshot_other_node_focused_still_contradicts(self):
        self.step_expect = self.predicate()
        def other_focus():
            self.raw['children'][1]['states'] = ['focused']
            self.publish(self.raw)
        self.sample_then(other_focus)
        result = self.run_action(self.params([self.click()]))
        self.assertEqual((result['effect']['status'], result['reason']),
                         ('contradicted', 'effect_contradicted'), result)
        self.assertEqual(self.samples, [(False, False), (False, False)])
        self.assertGreater(self.w.cache.facets['tree['+self.handle+']'], result['rev']['dispatched'])
        self.assertLess(result['steps'][0]['timing_ms']['total'], 300)

    def test_new_snapshot_target_state_absent_until_budget_is_not_observed(self):
        # Replaces round 1's "definitively false" contradiction assertion:
        # a newer snapshot can still contain a pre-effect state bit.
        self.step_expect = self.predicate()
        started = self.now
        def changed():
            self.raw['children'][1]['name'] = 'Changed after dispatch'
            self.publish(self.raw)
        self.sample_then(changed)
        result = self.run_action(self.params([self.click()]))
        self.assertEqual((result['effect']['status'], result['reason']),
                         ('not_observed', 'timeout'), result)
        self.assertGreaterEqual(self.now - started, .3)
        self.assertGreaterEqual(result['steps'][0]['timing_ms']['total'], 300)
        self.assertGreater(len(self.samples), 2)
        self.assertTrue(all(sample == (False, False) for sample in self.samples))
        self.assertGreater(self.w.cache.facets['tree['+self.handle+']'], result['rev']['dispatched'])

    def test_new_snapshot_state_absent_then_present_verifies(self):
        # Live shape: both deciding trees are post-dispatch. The first is
        # genuinely newer but still lacks the asynchronously propagated bit.
        expect = self.predicate()
        facets = []
        def refresh():
            if not facets:
                self.raw['children'][1]['name'] = 'First post-dispatch snapshot'
            else:
                self.raw['children'][0]['states'].append('focused')
            self.publish(self.raw)
            dispatched = self.e.active['receipt']['steps'][0]['rev']['dispatched']
            facets.append(self.w.cache.facets['tree['+self.handle+']'])
            self.samples.append(self.e.verification_verdict(expect, False, dispatched))
        self.t.refresh_hook = refresh
        result = self.run_action(self.params([dict(self.click(), timeout_ms=4000)]))
        self.assertEqual((result['status'], result['effect']['status']), ('success', 'verified'), result)
        self.assertEqual(self.samples, [(False, False), (True, False)])
        self.assertTrue(all(facet > result['rev']['dispatched'] for facet in facets))
        self.assertGreater(facets[1], facets[0])
        self.assertLess(result['steps'][0]['timing_ms']['total'], 4000)

    def test_new_snapshot_missing_target_other_node_focused_contradicts(self):
        self.step_expect = self.predicate()
        def other_focus():
            self.raw['children'].pop(0)
            self.raw['children'][0]['states'] = ['focused']
            self.publish(self.raw)
        self.sample_then(other_focus)
        result = self.run_action(self.params([self.click()]))
        self.assertEqual((result['effect']['status'], result['reason']),
                         ('contradicted', 'effect_contradicted'), result)

    def test_new_snapshot_missing_target_alone_is_not_positive_contradiction(self):
        self.step_expect = self.predicate()
        def remove():
            self.raw['children'].pop(0)
            self.publish(self.raw)
        self.sample_then(remove)
        result = self.run_action(self.params([self.click()]))
        self.assertEqual((result['effect']['status'], result['reason']), ('not_observed', 'timeout'), result)

    def test_no_new_target_snapshot_times_out_despite_other_facets_advancing(self):
        facet = 'tree['+self.handle+']'
        initial = self.w.cache.facets[facet]
        self.t.refresh_hook = lambda: self.p.bump(['unrelated-window'])
        started = self.now
        result = self.run_action(self.params([self.click()]))
        self.assertEqual((result['effect']['status'], result['reason']), ('not_observed', 'timeout'), result)
        self.assertGreaterEqual(self.now - started, .3)
        self.assertEqual(self.w.cache.facets[facet], initial)
        self.assertGreater(self.w.cache.rev, result['rev']['dispatched'])

    def test_text_equals_old_false_then_new_true_verifies(self):
        self.step_expect = dict(kind='text_equals', window=self.handle, ref=self.refs()['entry'], value='done')
        def text():
            self.raw['children'][0]['text'] = 'done'
            self.publish(self.raw)
        self.sample_then(text)
        result = self.run_action(self.params([self.click(self.step_expect)]))
        self.assertEqual((result['status'], result['effect']['status']), ('success', 'verified'), result)
        self.assertEqual(self.samples, [(False, False), (False, False)])

    def test_text_equals_post_dispatch_different_throughout_times_out(self):
        self.step_expect = dict(kind='text_equals', window=self.handle, ref=self.refs()['entry'], value='done')
        def different_text():
            self.raw['children'][0]['text'] = 'still different'
            self.publish(self.raw)
        self.sample_then(different_text)
        result = self.run_action(self.params([self.click(self.step_expect)]))
        self.assertEqual((result['effect']['status'], result['reason']), ('not_observed', 'timeout'), result)
        self.assertEqual(result['steps'][0]['dispatch_status'], 'accepted')
        self.assertGreaterEqual(result['steps'][0]['timing_ms']['total'], 300)
        self.assertGreater(len(self.samples), 2)
        self.assertTrue(all(sample == (False, False) for sample in self.samples))
        self.assertGreater(self.w.cache.facets['tree['+self.handle+']'], result['rev']['dispatched'])

    def test_fresh_tree_mismatches_are_unmet_across_predicates(self):
        entry, root = self.refs()['entry'], self.refs()['root']
        dispatched = self.w.cache.rev
        self.raw['children'][1]['states'] = ['selected']
        self.publish(self.raw)
        predicates = [dict(kind='node_state', ref=entry, state=state) for state in
                      ('focused', 'selected', 'checked', 'expanded', 'disabled')]
        predicates += [dict(kind='node_state', ref=entry, state=state, value=False)
                       for state in ('enabled', 'editable')]
        predicates += [dict(kind='text_equals', ref=entry, value='different'),
            dict(kind='text_contains', ref=entry, value='different'),
            dict(kind='node_present', ref='n99999'), dict(kind='node_absent', ref=entry),
            dict(kind='item_present', container=root, name='Missing'),
            dict(kind='item_absent', container=root, name='Input'),
            dict(kind='scroll_value', ref=entry, axis='y', value=3)]
        for p in predicates:
            with self.subTest(predicate=p):
                self.assertEqual(self.e.verification_verdict(dict(p, window=self.handle), False, dispatched),
                                 (False, False))

    def test_every_tree_predicate_ignores_even_true_old_page(self):
        predicates = [self.predicate(),
            dict(kind='text_equals', ref=self.refs()['entry'], value='ab'),
            dict(kind='text_contains', ref=self.refs()['entry'], value='a'),
            dict(kind='node_present', ref=self.refs()['entry']),
            dict(kind='node_absent', ref=self.refs()['entry']),
            dict(kind='item_present', name='Input'), dict(kind='item_absent', name='Missing'),
            dict(kind='scroll_value', ref=self.refs()['entry'], axis='y', value=3),
            dict(kind='focus_is', ref=self.refs()['entry'])]
        dispatched = self.w.cache.rev
        for p in predicates:
            with self.subTest(kind=p['kind']):
                self.assertEqual(self.e.verification_verdict(dict(p, window=self.handle), False, dispatched),
                                 (False, False))

    def test_wait_can_use_current_tree_without_a_dispatch(self):
        self.raw['children'][0]['states'].append('focused')
        self.p.cache.update(self.handle, self.raw, 1)
        step = dict(intent=dict(kind='wait'), expect=self.predicate(), timeout_ms=300)
        result = self.run_action(self.params([step]))
        self.assertEqual(result['effect']['status'], 'verified', result)
        self.assertFalse(self.t.log)

    def test_composite_contradiction_respects_alternatives(self):
        focused = self.predicate()
        text = dict(kind='text_equals', window=self.handle, ref=self.refs()['entry'], value='done')
        dispatched = self.w.cache.rev
        self.raw['children'][1]['states'] = ['focused']
        self.publish(self.raw)
        self.assertEqual(self.e.verification_verdict(dict(kind='all', of=[focused, text]), False, dispatched),
                         (False, True))
        self.assertEqual(self.e.verification_verdict(dict(kind='any', of=[focused, text]), False, dispatched),
                         (False, False))
        self.raw['children'][0]['text'] = 'done'
        self.publish(self.raw)
        self.assertEqual(self.e.verification_verdict(dict(kind='any', of=[focused, text]), False, dispatched),
                         (True, False))

    def test_post_dispatch_unexpected_popup_contradicts_even_with_old_tree(self):
        def popup(event):
            if event[0] != 'button_up':
                return
            self.w.cache.upsert(11, dict(self.w.cache.windows[10], xid=11, type='dialog',
                                       title='Unexpected', transient_for=10, is_modal=True))
            h = self.w.cache.handle(11)
            self.w.cache.set('focus', h)
            self.w.cache.set('modal', [h])
            self.w.cache.commit()
        self.t.on_write = popup
        result = self.run_action(self.params([self.click()]))
        self.assertEqual((result['effect']['status'], result['reason']),
                         ('contradicted', 'effect_contradicted'), result)
        self.assertLessEqual(self.w.cache.facets['tree['+self.handle+']'], result['rev']['dispatched'])
        self.assertGreater(self.w.cache.facets['modal'], result['rev']['dispatched'])


class FixtureRejection(unittest.TestCase):
    def test_xtest_ignored_and_logged_other_sources_and_default_normal(self):
        # Execute the fixture's actual interception function without GTK import.
        tree = ast.parse((Path(__file__).parents[1]/'fixtures/fx_log').read_text())
        fn = next(n for n in next(n for n in tree.body if isinstance(n, ast.ClassDef)).body
                  if isinstance(n, ast.FunctionDef) and n.name == 'input_event')
        normal = []
        namespace = dict(INPUT_TYPES={1}, POPUP_INPUT_TYPES={1}, Gtk=NS(main_do_event=normal.append))
        exec(compile(ast.Module(body=[fn], type_ignores=[]), '<fx_log>', 'exec'), namespace)
        for flag, source, expected in [(True, 'Virtual core XTEST keyboard', 'rejected_input'),
                                       (True, 'ark evdev keyboard', 'input'),
                                       (False, 'Virtual core XTEST keyboard', 'input')]:
            logs = []
            obj = NS(args=NS(reject_xtest=flag, slow_ms=0), popup_armed=False,
                     write=lambda kind, **fields: logs.append((kind, fields)))
            e = NS(type=type('EventType', (int,), {'value_nick': 'key-press'})(1),
                   get_source_device=lambda: NS(get_name=lambda: source), get_keycode=lambda: (True, 38),
                   get_button=lambda: (False, 0), get_root_coords=lambda: (False, 0, 0), get_time=lambda: 123)
            before = len(normal)
            namespace['input_event'](obj, e, None)
            self.assertEqual(logs[0][0], expected)
            self.assertEqual(logs[0][1]['source_device'], source)
            self.assertEqual(len(normal)-before, int(expected == 'input'))


class ProviderSetText(unittest.TestCase):
    def setUp(self):
        self.obj = tree_tests.FakeAccessible('/entry', role='entry', name='Input',
                         states=['enabled', 'sensitive', 'editable'])
        self.calls = []
        self.obj.queryEditableText = lambda: NS(setTextContents=lambda text: self.calls.append(text) or True)
        self.backend = Backend(tree_tests.fake_atspi())
        self.backend.objects['window'] = ({'entry': self.obj}, 'app', 1)
        self.task = dict(window='window', key='entry', role='entry', name='Input', text='é😀')

    def test_set_text_authority_immediately_precedes_mutation(self):
        with patch('worker_atspi.check_authority', side_effect=lambda task: self.calls.append('gate')):
            self.assertEqual(self.backend.set_text(self.task), dict(accepted=True))
        self.assertEqual(self.calls, ['gate', 'é😀'])

    def test_readonly_disabled_and_identity_changed_nodes_never_written(self):
        cases = [('readonly', lambda: self.obj.states.discard('editable')),
                 ('disabled', lambda: self.obj.states.discard('sensitive')),
                 ('identity', lambda: setattr(self.obj, 'name', 'Changed'))]
        for label, change in cases:
            self.setUp()
            change()
            with self.subTest(label=label), patch('worker_atspi.check_authority') as authority:
                with self.assertRaises(ValueError):
                    self.backend.set_text(self.task)
                authority.assert_not_called()
                self.assertFalse(self.calls)

    def test_control_denial_prevents_set_text(self):
        with patch('worker_atspi.check_authority', side_effect=ValueError('preempted')):
            with self.assertRaisesRegex(ValueError, 'preempted'):
                self.backend.set_text(self.task)
        self.assertFalse(self.calls)

    def test_window_only_fallback_rechecks_focused_state_in_child(self):
        with patch('worker_atspi.check_authority') as authority:
            with self.assertRaisesRegex(ValueError, 'precondition_failed'):
                self.backend.set_text(dict(self.task, focused_required=True))
            authority.assert_not_called()
        self.assertFalse(self.calls)


class MoreEngineRoutes(Harness):
    repeat = EngineRoutes.repeat
    def test_focus_wrong_at_dispatch_does_not_count(self):
        def focus_noop(*args):
            if False:
                yield
        self.e.focus = focus_noop
        self.w.cache.set('focus', None)
        self.w.cache.commit()
        for ident in ('one', 'two'):
            step = self.key(target=self.handle)
            r = self.repeat(step, ident)['steps'][0]
            self.assertEqual(r['dispatch_status'], 'accepted')
            self.assertNotIn('route_change', r)
        self.assertEqual(next(iter(self.e.routes.entries.values()))['states']['xtest']['misses'], 0)

    def test_focus_wrong_at_deadline_does_not_count(self):
        self.t.on_write = lambda ev: self.w.cache.set('focus', None)
        for ident in ('one', 'two'):
            self.w.cache.set('focus', self.handle)
            self.w.cache.commit()
            r = self.repeat(self.key(target=self.handle), ident)['steps'][0]
            self.assertNotIn('route_change', r)
        self.assertEqual(next(iter(self.e.routes.entries.values()))['states']['xtest']['misses'], 0)

    def test_unknown_step_between_misses_resets_streak(self):
        self.repeat(self.key(), 'one')
        self.t.ack = False
        self.t.on_write = lambda ev: self.t.emit(ev) if ev[0] == 'motion' else None
        self.repeat(self.key(), 'two')
        self.t.ack = True
        self.t.on_write = None
        r = self.repeat(self.key(), 'three')['steps'][0]
        self.assertNotIn('route_change', r)
        key = next(iter(self.e.routes.entries))
        self.assertEqual(self.e.routes.entries[key]['states']['xtest']['misses'], 1)


class BaselineRoutes(tree_tests.ProviderHarness):
    """A mapped window predates both provider startup and pixel tracking."""
    repeat = EngineRoutes.repeat

    def setUp(self):
        super().setUp()
        self.shadow = Shadow(640, 480)
        self.shadow.mode = 'mmap'  # Synthetic captured bytes; no display.
        self.addCleanup(self.shadow.close)
        self.w.pixels = Pixels(self.w, self.shadow)
        self.w.pixels.settle()
        self.w.cache.baseline()
        self.p.consumers[self.handle] = self.w.lease

    def tick(self):
        super().tick()
        for task in list(self.p.pending.values()):
            raw = self.raw if task['op'] == 'snapshot' else dict(alive=True)
            if task['op'] == 'snapshot' and task['window'] in getattr(self, 'no_tree_handles', set()):
                raw = dict(tree_tests.node(), status='unmatched', showing=False, visible=False, children=[])
            self.answer(raw, task=task)
        self.w.settle_pixels()

    def idle(self):
        for _ in range(2100):
            self.tick()

    def change_event(self):
        self.wire.incoming.append(dict(event='changed', app='101', window=self.handle))

    def test_preexisting_idle_family_stamp_with_provider_pump_and_pixel_damage(self):
        before = self.e.family_stamp(self.handle)
        rev = self.w.cache.rev
        # Same-content DAMAGE and semantic invalidation must settle without
        # changing a publication facet; the provider also runs liveness probes.
        self.w.pixels.mark([self.w.pixels.bounds])
        self.change_event()
        self.idle()
        self.assertFalse(self.p.dirty)
        self.assertFalse(self.w.pixels.dirty)
        self.assertTrue(any(t['op'] == 'probe' for t in self.wire.sent))
        self.assertEqual(self.w.cache.rev, rev)
        self.assertEqual(self.e.family_stamp(self.handle), before)

    def test_preexisting_delta_observe_is_unchanged_at_current_rev(self):
        for _ in range(3):
            self.idle()
            result = self.w.observation(dict(since_rev=self.w.cache.rev,
                scope=dict(kind='window', window=self.handle)))
            self.w.observed(self.peer, {}, result)
            self.assertTrue(result['unchanged'], result)
            self.assertNotIn('state', result)

    def test_ocr_fallback_can_report_changed_without_family_stamp_churn(self):
        self.p.apps['101'].degrade(self.now, 'application_unreachable')
        self.w.ocr = NS(state='down', reason='provider_unavailable', read=lambda *args: None,
            health=lambda: dict(state='down', reason='provider_unavailable'),
            coverage=lambda: dict(complete=False, reason='provider_degraded'))
        before, rev = self.e.family_stamp(self.handle), self.w.cache.rev
        for _ in range(3):
            result = self.w.observation(dict(since_rev=rev,
                scope=dict(kind='window', window=self.handle)))
            self.w.observed(self.peer, {}, result)
            self.assertFalse(result['unchanged'])
            self.assertNotIn('state', result)
            self.assertEqual(result['rev'], rev)
            self.assertEqual(self.e.family_stamp(self.handle), before)

    def test_preexisting_two_misses_flip_and_stamp_receipt(self):
        self.idle()
        first = self.repeat(self.key(), 'one')['steps'][0]
        key = next(iter(self.e.routes.entries))
        self.assertEqual(self.e.routes.entries[key]['states']['xtest']['misses'], 1)
        self.assertNotIn('route_change', first)
        second = self.repeat(self.key(), 'two')['steps'][0]
        for step in (first, second):
            self.assertEqual(step['dispatch_status'], 'accepted')
            self.assertEqual(step['attempts'][0]['server_ack']['expected'], 2)
            self.assertEqual(step['attempts'][0]['server_ack']['seen'], 2)
            self.assertEqual(step['effect']['status'], 'not_observed')
        self.assertEqual(second['route_change'], {'from': 'xtest', 'to': None,
            'reason': 'synthetic_input_rejected_suspect'})
        self.assertTrue(self.e.routes.path.exists())

    def test_preexisting_metadata_reply_pump_preserves_stamp_and_route_learning(self):
        self.metadata = DeferredMetadata(self)
        self.w.x = self.metadata.x
        self.t.refresh = self.metadata.transport.refresh
        self.metadata.x.refresh(10)
        for _ in range(200):
            self.tick()
        self.w.cache.baseline()
        before = self.e.family_stamp(self.handle)
        for ident in ('one', 'two'):
            step = self.repeat(dict(self.key(), timeout_ms=300), ident)['steps'][0]
            self.assertEqual(self.e.family_stamp(self.handle), before)
            self.assertEqual(step['effect']['status'], 'not_observed')
        self.assertEqual(step['route_change']['reason'], 'synthetic_input_rejected_suspect')

    def unsettled_route(self, reaction=False, publication_dirty=False):
        ref = self.refs()['entry']
        step = self.key(target=dict(window=self.handle, ref=ref), timeout_ms=300,
                        expect=dict(kind='text_equals', window=self.handle, ref=ref, value='never'))
        self.repeat(step, 'one')
        key = next(iter(self.e.routes.entries))
        self.assertEqual(self.e.routes.entries[key]['states']['xtest']['misses'], 1)
        snapshot = self.e.route_snapshot
        prepared, dispatched = [], []
        rect = dict(x=0, y=0, w=256, h=256)

        def prepare(*args):
            if not prepared:
                # The app already displays this content, but the cache still
                # has startup hashes/tree. The normal owner pump will publish
                # it during verification unless dispatch drains it first.
                self.shadow.update(rect, b'\x40\x00\x00\x00' * (256 * 256))
                self.w.pixels.mark([rect])
                self.raw['children'][0]['text'] = 'settled at dispatch'
                if publication_dirty:
                    self.raw['dirty'] = True
                task = self.p.request('snapshot', self.handle)
                self.assertIsNotNone(task)
                prepared.append(self.e.family_stamp(self.handle))
            return snapshot(*args)

        def write(event):
            if event[0] == 'key_down':
                r = self.e.active['receipt']['steps'][0]
                dispatched.append(copy.deepcopy(r['_family_before']))
                if publication_dirty:
                    self.assertIn(self.handle, self.p.dirty)
                    self.raw.pop('dirty')
            if reaction and event[0] == 'key_up':
                # A real post-dispatch edit is first revealed by verification's
                # ordinary hashing/publication, and must break the streak.
                self.shadow.update(dict(x=20, y=20, w=1, h=1), b'\xff\x00\x00\x00')
                self.w.pixels.mark([dict(x=20, y=20, w=1, h=1)])
                self.raw['children'][0]['text'] = 'real reaction'
                self.change_event()

        self.t.on_write = write
        with patch.object(self.e, 'route_snapshot', side_effect=prepare), \
                patch.object(self.shadow, 'digest', wraps=self.shadow.digest) as hashed:
            result = self.repeat(step, 'unsettled')['steps'][0]
        # Only the known 16 dirty tiles (plus the real reaction's one tile)
        # are hashed. Route stamping adds no full-screen scan or rehash.
        self.assertEqual(hashed.call_count, 16 + int(reaction))
        self.assertEqual(result['effect']['status'], 'not_observed')
        self.assertEqual(result['dispatch_status'], 'accepted')
        self.assertEqual(len(dispatched), 1)
        # Both the 16 dirty tiles and the pending tree have been published
        # before the baseline; there was no application reaction in doing so.
        self.assertNotEqual(prepared[0], dispatched[0])
        self.assertGreater(dispatched[0]['tree['+self.handle+']'],
                           prepared[0]['tree['+self.handle+']'])
        for tile in self.w.pixels.tiles(rect):
            facet = self.w.pixels.facet(tile)
            self.assertGreater(dispatched[0][facet], prepared[0][facet])
        after = self.e.family_stamp(self.handle)
        if reaction:
            self.assertNotEqual(dispatched[0], after)
            self.assertNotIn('route_change', result)
            self.assertEqual(self.e.routes.entries[key]['states']['xtest']['misses'], 0)
        else:
            self.assertEqual(dispatched[0], after)
            self.assertEqual(result['route_change']['reason'], 'synthetic_input_rejected_suspect')

    def test_unsettled_startup_pixels_and_tree_qualify_identical_content_miss(self):
        self.unsettled_route()

    def test_unsettled_startup_with_real_content_revealed_in_verification_resets(self):
        self.unsettled_route(reaction=True)

    def test_unsettled_completed_publication_does_not_wait_for_clean_flag(self):
        self.unsettled_route(publication_dirty=True)

    def pseudo_window(self, **changes):
        win = dict(self.w.cache.windows[10], xid=12, title='Openbox', type='popup',
                   geometry=dict(x=-100, y=-100, w=1, h=1))
        win.update(changes)
        self.w.cache.upsert(12, win)
        self.w.cache.commit()
        h = self.w.cache.handle(12)
        self.no_tree_handles = {h}
        return h

    def test_no_tree_offscreen_popup_materialization_preserves_stamp_and_flip(self):
        ref = self.refs()['entry']
        step = self.key(target=dict(window=self.handle, ref=ref), timeout_ms=300,
                        expect=dict(kind='text_equals', window=self.handle, ref=ref, value='never'))
        self.repeat(step, 'one')
        before = self.e.family_stamp(self.handle)
        def write(event):
            if event[0] == 'key_up':
                h = self.pseudo_window()
                # No AT-SPI object: even the unmatched publication facet is
                # scaffolding, as in the live step-2 geometry/tree diff.
                self.p.bump([h])
                self.w.pixels.mark([dict(x=0, y=0, w=1, h=1)])
        self.t.on_write = write
        result = self.repeat(step, 'two')['steps'][0]
        self.assertEqual(result['effect']['status'], 'not_observed')
        self.assertEqual(self.e.family_stamp(self.handle), before)
        self.assertEqual(result['route_change']['reason'], 'synthetic_input_rejected_suspect')

    def test_no_tree_offscreen_popup_removal_preserves_stamp(self):
        self.pseudo_window()
        before = self.e.family_stamp(self.handle)
        self.w.cache.remove(12)
        self.w.cache.commit()
        self.assertEqual(self.e.family_stamp(self.handle), before)

    def test_popup_content_or_dialog_relationship_is_still_a_family_member(self):
        before = self.e.family_stamp(self.handle)
        h = self.pseudo_window()
        self.assertEqual(self.e.family_stamp(self.handle), before)
        self.p.cache.update(h, tree_tests.node('popup', 'entry', 'Real content'))
        self.assertNotEqual(self.e.family_stamp(self.handle), before)
        self.p.cache.prune(h)
        for changes in (dict(transient_for=10), dict(is_modal=True),
                        dict(geometry=dict(x=0, y=0, w=1, h=1)), dict(type='normal')):
            with self.subTest(changes=changes):
                self.w.cache.upsert(12, dict(self.w.cache.windows[12], **changes))
                self.w.cache.commit()
                self.assertNotEqual(self.e.family_stamp(self.handle), before)
                self.pseudo_window()

    def real_change_breaks_streak(self, change):
        self.repeat(self.key(), 'one')
        key = next(iter(self.e.routes.entries))
        self.assertEqual(self.e.routes.entries[key]['states']['xtest']['misses'], 1)
        stamps = []
        def react(event):
            if event[0] == 'key_up' and not stamps:
                stamps.append(self.e.family_stamp(self.handle))
                change()
        self.t.on_write = react
        step = self.repeat(dict(self.key(), timeout_ms=300), 'reaction')['steps'][0]
        self.assertEqual(step['effect']['status'], 'not_observed')
        self.assertNotEqual(stamps[0], self.e.family_stamp(self.handle))
        self.assertNotIn('route_change', step)
        self.assertEqual(self.e.routes.entries[key]['states']['xtest']['misses'], 0)
        self.t.on_write = None
        self.repeat(self.key(), 'after')['steps'][0]
        self.assertEqual(self.e.routes.entries[key]['states']['xtest']['misses'], 1)

    def test_preexisting_real_text_change_breaks_streak(self):
        def change():
            self.raw['children'][0]['text'] = 'real edit'
            self.change_event()
        self.real_change_breaks_streak(change)

    def test_preexisting_real_tree_change_breaks_streak(self):
        def change():
            self.raw['children'].append(tree_tests.node('new', 'label', 'New content'))
            self.change_event()
        self.real_change_breaks_streak(change)

    def test_preexisting_geometry_changed_then_restored_breaks_streak(self):
        def change():
            win = self.w.cache.windows[10]
            self.w.cache.upsert(10, dict(win, geometry=dict(win['geometry'], x=20)))
            self.w.cache.commit()
            self.w.cache.upsert(10, win)
            self.w.cache.commit()
        self.real_change_breaks_streak(change)

    def test_preexisting_real_pixel_content_change_breaks_streak(self):
        def change():
            rect = dict(x=20, y=20, w=1, h=1)
            self.shadow.update(rect, b'\xff\x00\x00\x00')
            self.w.pixels.mark([rect])
        self.real_change_breaks_streak(change)

    def test_preexisting_family_window_added_and_removed_breaks_streak(self):
        def change():
            self.later()
            self.w.cache.remove(11)
            self.w.cache.commit()
        self.real_change_breaks_streak(change)


class PasteRecovery(PasteHarness):
    def test_fresh_engine_starts_at_persisted_text_paste_rung(self):
        first = self.run_action(self.params([self.key(intent=dict(kind='text', text='a'))]))
        self.e.pending_effects.clear()
        self.e.observed(self.peer, {}, {'full': True, 'settled': True, 'rev': self.w.cache.rev})
        second = self.run_action(self.params([self.key(intent=dict(kind='text', text='a'))], action_id='two'))
        self.assertEqual(second['steps'][0]['route_change']['to'], 'paste')
        self.e = self.w.act = Engine(self.w, self.t, clock=lambda: self.now)
        self.e.channel = dict(state='ok', keyboard='ok', pointer='ok')
        self.e.pending_effects.clear()
        self.e.attach('lease', self.peer, {})
        self.e.observed(self.peer, {}, {'full': True, 'settled': True, 'rev': self.w.cache.rev})
        self.t.on_write = lambda ev: self.later() if ev == ('key_down', 55) else None
        r = self.run_action(self.params([self.key(intent=dict(kind='text', text='a'))], action_id='three'))['steps'][0]
        self.assertEqual(r['attempts'][0]['route'], 'paste')
        self.assertEqual(r['effect']['status'], 'verified')
        self.assertEqual(r['route_change'], dict([('from', 'xtest'), ('to', 'paste'), ('reason', 'learned_route')]))

    def test_evidence_after_step_completion_does_not_mutate_durable_receipt(self):
        self.run_action(self.params([self.key(intent=dict(kind='text', text='😀'))]))
        before = copy.deepcopy(self.e.actions[(GEN, 'a')]['receipt'])
        self.e.clipboard.events([self.server.event(2)])
        self.assertEqual(self.e.actions[(GEN, 'a')]['receipt'], before)

    def test_other_client_selection_request_is_not_target_evidence(self):
        evidence = {}
        consume(self.e.clipboard.own('hello', self.handle, lambda: None, evidence))
        self.server.requestor.id = 1024
        self.e.clipboard.events([self.server.event(2)])
        self.assertFalse(evidence['selection_requested'])


    def test_request_during_verification_updates_step_and_attempt_evidence(self):
        def request():
            self.e.clipboard.events([self.server.event(2)])
            self.later()
        self.t.refresh_hook = request
        r = self.run_action(self.params([self.key(intent=dict(kind='text', text='😀'))]))['steps'][0]
        self.assertEqual(r['effect']['status'], 'verified')
        self.assertTrue(r['selection_requested'])
        self.assertTrue(r['attempts'][0]['selection_requested'])
        self.assertEqual(r['attempts'][0]['input_route'], 'xtest')
