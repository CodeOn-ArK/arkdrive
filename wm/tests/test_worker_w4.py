#!/usr/bin/python3
"""W4 headless contracts. Fake child/transport, deterministic time, no X.

Uses the real worker cache, action engine, IPC framing and provider backend
with independent fake accessible objects. Never imports pyatspi or opens a
session, display, or D-Bus connection.
"""
import copy
import json
from pathlib import Path
import struct
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from test_worker_w2 import Harness, w, GEN
from worker_act import ActError, predicate_schema
from worker_tree import (TreeCache, Provider, Breaker, Wire, packet, included, line,
                         selector_schema, TREE_KINDS)
from worker_atspi import Backend, check_authority, match_frame


def node(key='root', role='frame', name='Fixture', children=(), **kw):
    return dict(key=key, role=role, name=name, children=list(children),
                showing=True, visible=True, states=[], bounds=[100, 100, 100, 50], **kw)


def fixture():
    entry = node('entry', 'entry', 'Input', text='ab', caret=1, selection=[0, 1])
    entry['states'] = ['focused', 'editable']
    button = node('button', 'push_button', 'Add to list')
    return node(children=[entry, button])


class Rendering(unittest.TestCase):
    def test_format_role_name_states_and_extents(self):
        n = node(role='push button', name='Create Folder')
        n.update(ref='n12', states=['showing', 'focused', 'selected', 'checked', 'expanded', 'editable', 'disabled', 'active'], bounds=[412, 300, 96, 28])
        self.assertEqual(line(n, 1), ' n12 push_button "Create Folder" [focused,selected,checked,expanded,editable,disabled] @412,300,96,28')

    def test_quotes_and_embedded_newline_stay_one_line(self):
        n = node(name='a"\nb\\c')
        n['ref'] = 'n1'
        result = line(n, 0)
        self.assertEqual(len(result.splitlines()), 1)
        self.assertIn('"a\\"\\nb\\\\c"', result)

    def test_text_is_80_unicode_characters_not_bytes(self):
        n = node(role='entry', text='é'*81)
        n['ref'] = 'n1'
        self.assertIn('text="'+'é'*80+'"', line(n, 0))
        self.assertNotIn('é'*81, line(n, 0))
        self.assertNotIn('text=', line(dict(n, role='label'), 0))

    def test_render_does_not_truncate_predicate_text(self):
        c = TreeCache()
        c.update('w', node(role='text', text='x'*100))
        self.assertEqual(next(iter(c.windows['w']['nodes'].values()))['text'], 'x'*100)
        self.assertNotIn('x'*81, c.page('w')['lines'][0])

    def test_hidden_parent_excludes_descendants(self):
        raw = node(children=[node('child', 'push_button', 'Hi')])
        for field in ('showing', 'visible'):
            with self.subTest(field=field):
                self.assertEqual(included(dict(raw, **{field: False})), [])

    def test_name_text_interactive_or_descendant(self):
        children = [node('a', name='Named'), node('b', name='', text='Text'),
                    node('c', 'push_button', ''), node('d', 'panel', '')]
        raw = node(children=children)
        self.assertEqual([n['key'] for n in included(raw)[0]['children']], ['a', 'b', 'c'])

    def test_unnamed_single_child_container_flattens(self):
        raw = node(children=[node('panel', 'panel', '', [node('button', 'push_button', 'Go')])])
        c = TreeCache()
        c.update('w', raw)
        self.assertEqual(len(c.page('w')['lines']), 2)
        self.assertTrue(c.page('w')['lines'][1].startswith(' n2 push_button'))

    def test_unnamed_interactive_single_child_is_retained(self):
        raw = node(role='combo_box', name='', children=[node('item', 'list_item', 'One')])
        self.assertEqual(included(raw)[0]['key'], 'root')

    def test_multiple_descendants_keep_unnamed_container(self):
        raw = node(name='', role='panel', children=[node('a'), node('b')])
        self.assertEqual(len(included(raw)[0]['children']), 2)


class CachedTrees(unittest.TestCase):
    def setUp(self):
        self.c = TreeCache()

    def test_100_reads_preserve_refs_and_snapshot(self):
        self.c.update('w', fixture(), 3)
        expected = self.c.page('w')
        for _ in range(100):
            self.assertEqual(self.c.page('w'), expected)
        self.assertFalse(self.c.update('w', fixture(), 4))
        self.assertEqual(self.c.windows['w']['rev'], 3)

    def test_existing_refs_survive_other_node_changes(self):
        raw = fixture()
        self.c.update('w', raw, 1)
        refs = list(self.c.windows['w']['nodes'])
        raw['children'][1]['name'] = 'Changed'
        self.c.update('w', raw, 2)
        self.assertEqual(list(self.c.windows['w']['nodes']), refs)

    def test_removed_returning_node_gets_new_ref(self):
        raw = fixture()
        self.c.update('w', raw, 1)
        first = self.c.find('w', dict(provider='atspi', name='Input'))['ref']
        self.c.update('w', node(children=[raw['children'][1]]), 2)
        self.c.update('w', raw, 3)
        self.assertNotEqual(self.c.find('w', dict(provider='atspi', name='Input'))['ref'], first)

    def test_parent_change_gets_new_ref(self):
        raw = node(children=[node('a', name='A', children=[node('leaf')]), node('b', name='B')])
        self.c.update('w', raw, 1)
        before = {n['key']: n['ref'] for n in self.c.windows['w']['nodes'].values()}
        raw['children'][1]['children'] = raw['children'][0]['children']
        raw['children'][0]['children'] = []
        self.c.update('w', raw, 2)
        after = {n['key']: n['ref'] for n in self.c.windows['w']['nodes'].values()}
        self.assertNotEqual(before['leaf'], after['leaf'])

    def test_restart_never_reuses_ref_and_expires_cursor(self):
        self.c.update('w', node(children=[node(str(i)) for i in range(210)]), 1)
        old = self.c.page('w')
        refs = set(self.c.windows['w']['nodes'])
        self.c.restart()
        self.c.update('w', fixture(), 1)
        self.assertFalse(refs & set(self.c.windows['w']['nodes']))
        with self.assertRaises(ActError):
            self.c.page('w', cursor=old['cursor'])

    def test_200_nodes_then_opaque_continuation(self):
        self.c.update('w', node(children=[node(str(i), name=str(i)) for i in range(220)]), 1)
        first = self.c.page('w')
        self.assertEqual(len(first['lines']), 200)
        self.assertTrue(first['truncated'])
        self.assertIsInstance(first['cursor'], str)
        rest = self.c.page('w', cursor=first['cursor'])
        self.assertEqual(len(rest['lines']), 21)
        self.assertFalse(rest['truncated'])
        self.assertEqual(first['lines']+rest['lines'], self.c.windows['w']['lines'])

    def test_16k_limit_and_complete_continuation(self):
        self.c.update('w', node(children=[node(str(i), name='é'*1000) for i in range(30)]), 1)
        all_lines, cursor = [], None
        while True:
            page = self.c.page('w', cursor=cursor)
            self.assertLessEqual(sum(len((s+'\n').encode()) for s in page['lines']), 16384)
            self.assertTrue(page['lines'])
            all_lines.extend(page['lines'])
            cursor = page['cursor']
            if cursor is None:
                break
        self.assertEqual(all_lines, self.c.windows['w']['lines'])

    def test_full_page_byte_limit_includes_json_escaping_and_metadata(self):
        self.c.update('w', node(children=[node(str(i), name='\\"'*1000) for i in range(20)]), 1)
        page = self.c.page('w')
        self.assertLessEqual(len(json.dumps(page, ensure_ascii=False, separators=(',', ':')).encode()), 16384)
        self.assertTrue(page['truncated'])

    def test_cursor_pins_snapshot_across_tree_change(self):
        self.c.update('w', node(children=[node(str(i)) for i in range(220)]), 1)
        cursor = self.c.page('w')['cursor']
        self.c.update('w', fixture(), 2)
        self.assertEqual(len(self.c.page('w', cursor=cursor)['lines']), 21)

    def test_delta_added_changed_removed(self):
        raw = fixture()
        self.c.update('w', raw, 1)
        removed = self.c.find('w', dict(provider='atspi', name='Input'))['ref']
        raw['children'] = [dict(raw['children'][1], name='Updated'), node('new', 'check_box', 'New')]
        self.c.update('w', raw, 2)
        delta = self.c.page('w', since=1)['delta']
        self.assertEqual(delta['removed'], [removed])
        self.assertEqual(len(delta['changed']), 1)
        self.assertIn('Updated', delta['changed'][0])
        self.assertEqual(len(delta['added']), 1)
        self.assertIn('New', delta['added'][0])

    def test_delta_node_budget_falls_back_full(self):
        self.c.update('w', fixture(), 1)
        self.c.update('w', node(children=[node(str(i)) for i in range(41)]), 2)
        self.assertTrue(self.c.page('w', 1)['full'])
        self.assertNotIn('delta', self.c.page('w', 1))

    def test_delta_byte_budget_falls_back_full(self):
        raw = fixture()
        self.c.update('w', raw, 1)
        raw['children'][0]['name'] = 'a'*2050
        self.c.update('w', raw, 2)
        self.assertTrue(self.c.page('w', 1)['full'])

    def test_reordered_nodes_fall_back_to_full(self):
        raw = fixture()
        self.c.update('w', raw, 1)
        raw['children'].reverse()
        self.c.update('w', raw, 2)
        self.assertTrue(self.c.page('w', 1)['full'])

    def test_removed_ref_can_prove_absence_but_restart_ref_is_stale(self):
        self.c.update('w', fixture(), 1)
        ref = self.c.find('w', dict(provider='atspi', name='Input'))['ref']
        self.c.update('w', node(), 2)
        self.assertIsNone(self.c.find('w', ref, missing=True))
        self.c.restart()
        self.c.update('w', fixture(), 3)
        with self.assertRaises(ActError) as cm:
            self.c.find('w', ref, missing=True)
        self.assertEqual(cm.exception.detail, 'provider_restarted')

    def test_ref_from_different_window_cannot_prove_absence(self):
        self.c.update('a', fixture(), 1)
        self.c.update('b', fixture(), 1)
        ref = self.c.find('a', dict(provider='atspi', name='Input'))['ref']
        with self.assertRaises(ActError):
            self.c.find('b', ref, missing=True)

    def test_single_line_exceeding_budget_fails_without_stuck_cursor(self):
        self.c.update('w', node(name='x'*17000), 1)
        with self.assertRaises(ActError) as cm:
            self.c.page('w')
        self.assertEqual(cm.exception.detail, 'tree_line_budget')

    def test_old_revision_gets_full_and_history_is_bounded(self):
        for i in range(100):
            self.c.update('w', node(name=str(i)), i+1)
        self.assertNotIn('previous', self.c.windows['w']['previous'])
        self.assertTrue(self.c.page('w', since=1)['full'])


class Selectors(unittest.TestCase):
    def setUp(self):
        self.c = TreeCache()
        self.c.update('w', node(children=[node('a', 'push_button', 'Go'), node('b', 'label', 'go')]))

    def test_exact_name_is_case_sensitive(self):
        self.assertEqual(self.c.find('w', dict(provider='atspi', name='Go'))['key'], 'a')
        self.assertEqual(self.c.find('w', dict(provider='atspi', name='go'))['key'], 'b')
        self.assertIsNone(self.c.find('w', dict(provider='atspi', name='GO'), True))

    def test_regex_and_role_conjunction(self):
        self.assertEqual(self.c.find('w', dict(provider='atspi', name_re='^[Gg]o$', role='push_button'))['key'], 'a')
        with self.assertRaises(ActError) as cm:
            self.c.find('w', dict(provider='atspi', name_re='^[Gg]o$'))
        self.assertEqual(cm.exception.reason, 'invalid_request')

    def test_zero_is_target_not_found_and_duplicate_is_invalid(self):
        with self.assertRaises(ActError) as cm:
            self.c.find('w', dict(provider='atspi', name='missing'))
        self.assertEqual(cm.exception.reason, 'target_not_found')
        self.c.update('w', node(children=[node('a', name='same'), node('b', name='same')]))
        with self.assertRaises(ActError) as cm:
            self.c.find('w', dict(provider='atspi', name='same'))
        self.assertEqual(cm.exception.reason, 'invalid_request')

    def test_within_restricts_descendants(self):
        raw = node(children=[node('a', name='A', children=[node('b', name='Go')]), node('c', name='Go')])
        self.c.update('w', raw)
        parent = self.c.find('w', dict(provider='atspi', name='A'))['ref']
        self.assertEqual(self.c.find('w', dict(provider='atspi', name='Go', within=parent))['key'], 'b')
        with self.assertRaises(ActError):
            self.c.find('w', dict(provider='atspi', name='Go', within='n999'))

    def test_schema_rejects_conflicts_bad_regex_or_unknown_provider(self):
        for s in [dict(provider='atspi'), dict(provider='cdp', name='x'),
                  dict(provider='atspi', name='x', name_re='x'),
                  dict(provider='atspi', name_re='['), dict(provider='atspi', name='x', nth=1)]:
            with self.subTest(s=s), self.assertRaises(ActError):
                selector_schema(s)


class Breakers(unittest.TestCase):
    def test_two_consecutive_breaches(self):
        b = Breaker()
        b.fail(0, .251)
        self.assertEqual(b.state, 'healthy')
        b.fail(1, .251)
        self.assertEqual(b.state, 'degraded')
        self.assertEqual(b.due, 2)

    def test_success_resets_breach_streak(self):
        b = Breaker()
        b.fail(0, .3)
        b.success()
        b.fail(1, .3)
        self.assertEqual(b.state, 'healthy')

    def test_one_call_over_second_degrades(self):
        b = Breaker()
        b.fail(0, 1.001)
        self.assertEqual(b.state, 'degraded')
        self.assertEqual(b.breaches, 1)

    def test_backoff_1_2_4_8_16_30_capped(self):
        b = Breaker()
        for interval in (1, 2, 4, 8, 16, 30, 30, 30):
            b.fail(10, 2)
            self.assertEqual(b.due-10, interval)
        b.success()
        b.fail(20, 2)
        self.assertEqual(b.due, 21)


class FakeWire:
    def __init__(self):
        self.incoming, self.sent = [], []
        self.closed = False

    def send(self, row):
        self.sent.append(copy.deepcopy(row))

    def pump(self):
        rows, self.incoming = self.incoming, []
        return rows

    def close(self):
        self.closed = True


class FakeChild:
    def __init__(self):
        self.code = None

    def poll(self):
        return self.code

    def kill(self):
        self.code = -9

    def wait(self):
        return self.code


class ProviderHarness(Harness):
    def setUp(self):
        super().setUp()
        self.children = []
        self.w.x = NS(window_pids={10: 101})
        def factory():
            wire, child = FakeWire(), FakeChild()
            wire.incoming.append(dict(event='ready'))
            self.children.append((wire, child))
            return wire, child
        self.provider_factory = factory
        self.w.tree = self.p = Provider(self.w, factory, clock=lambda: self.now)
        self.wire, self.child = self.children[-1]
        self.p.tick()
        self.answer(fixture())
        self.w.cache.baseline()
        self.p.cache.windows[self.handle]['rev'] = 1
        self.raw = fixture()

    def answer(self, raw=None, error=None, task=None, **timing):
        task = task or next(iter(self.p.pending.values()))
        self.wire.incoming.append(dict(id=task['id'], result=raw, error=error, **timing))
        self.p.tick()
        return task

    def refs(self):
        return {n['key']: r for r, n in self.p.cache.windows[self.handle]['nodes'].items()}

    def change(self, raw):
        self.wire.incoming.append(dict(event='changed', app='101'))
        self.p.tick()
        self.now += .041
        self.p.tick()
        self.answer(raw, task=next(t for t in self.p.pending.values() if t['window'] == self.handle))

    def tick(self):
        self.p.tick()
        super().tick()


class ProviderLifecycle(ProviderHarness):
    def test_initial_probe_and_100_cached_observations_no_ipc(self):
        self.assertEqual(self.p.probes[self.handle], 'usable')
        before = len(self.wire.sent)
        expected = self.w.observation(dict(channels=['tree'], scope=dict(kind='window', window=self.handle)))['tree']
        for _ in range(100):
            self.assertEqual(self.w.observation(dict(channels=['tree'], scope=dict(kind='window', window=self.handle)))['tree'], expected)
            self.p.tick()
        self.assertEqual(len(self.wire.sent), before)

    def test_one_build_per_change(self):
        before = len(self.wire.sent)
        self.wire.incoming.append(dict(event='changed', app='101'))
        self.p.tick()
        self.now += .041
        self.p.tick()
        for _ in range(100):
            self.p.ensure(self.handle)
        self.assertEqual(len(self.wire.sent), before+1)
        self.answer(self.raw)
        for _ in range(100):
            self.p.ensure(self.handle)
        self.assertEqual(len(self.wire.sent), before+1)

    def test_empty_probe_is_cached(self):
        self.change(node(name='', role='panel'))
        self.assertEqual(self.p.probes[self.handle], 'empty')
        before = len(self.wire.sent)
        for _ in range(10):
            self.p.ensure(self.handle)
        self.assertEqual(len(self.wire.sent), before)

    def test_tree_change_prevents_unchanged_only_in_tree_scope(self):
        base = self.w.cache.rev
        raw = fixture()
        raw['children'][0]['text'] = 'new'
        self.change(raw)
        state = self.w.observation(dict(channels=['state'], since_rev=base))
        tree = self.w.observation(dict(channels=['tree'], since_rev=base))
        self.assertTrue(state['unchanged'])
        self.assertFalse(tree['unchanged'])
        self.assertIn('tree', tree)

    def test_session_tree_is_focused_window(self):
        self.later()
        self.assertEqual(self.w.observation(dict(channels=['tree']))['tree']['window'], self.handle)
        self.w.cache.set('focus', self.w.cache.handle(11))
        self.w.cache.commit()
        result = self.w.observation(dict(channels=['tree']))
        self.assertEqual(result['tree']['status'], 'pending')

    def test_auto_usable_includes_tree_and_empty_skips(self):
        self.assertIn('tree', self.w.observation(dict(channels=['auto'])))
        self.change(node(name='', role='panel'))
        self.assertEqual(self.w.observation(dict(channels=['auto']))['tree']['status'], 'empty')
        self.assertIn('tree', self.w.observation(dict(channels=['tree'])))

    def test_child_death_invalidates_refs_then_restarts(self):
        refs = set(self.refs().values())
        self.child.code = 1
        self.p.tick()
        self.assertEqual(self.p.health()['atspi']['state'], 'down')
        self.assertFalse(self.p.cache.windows)
        self.now += .999
        self.p.tick()
        self.assertEqual(len(self.children), 1)
        self.now += .002
        self.p.tick()
        self.wire, self.child = self.children[-1]
        self.p.tick()
        self.answer(self.raw)
        self.assertFalse(refs & set(self.refs().values()))

    def test_hung_application_does_not_queue_other_app(self):
        self.later()
        other = self.w.cache.handle(11)
        self.w.x.window_pids[11] = 202
        self.p.dirty.add(self.handle)
        hung = self.p.request('snapshot', self.handle)
        self.p.tick()
        other_task = next(t for t in self.p.pending.values() if t['window'] == other)
        self.answer(fixture(), task=other_task)
        self.now += 2.001
        self.p.tick()
        self.assertTrue(hung['breached'])
        self.assertIsNone(self.p.request('snapshot', self.handle))
        self.assertTrue(self.p.healthy(other))
        self.now += .751
        self.wire.incoming.append(dict(event='pong'))
        self.p.tick()
        self.assertEqual(self.p.apps['101'].state, 'degraded')
        self.assertEqual(self.p.apps['202'].state, 'healthy')
        self.assertEqual(self.p.health()['atspi']['apps']['101']['reason'], 'application_unreachable')
        self.assertTrue(self.p.ready)

    def test_dead_child_main_loop_is_killed_after_unanswered_ping(self):
        self.p.dirty.add(self.handle)
        self.p.ensure(self.handle)
        self.now += 2.001
        self.p.tick()
        self.assertIn(dict(op='ping'), self.wire.sent)
        self.now += 1.001
        self.p.tick()
        self.assertEqual(self.child.code, -9)
        self.assertEqual(self.p.health()['atspi']['state'], 'down')

    def test_late_worker_tick_does_not_charge_child_call(self):
        task = self.p.request('invoke', self.handle, key='button')
        self.now += .251
        self.answer(dict(accepted=True), task=task)
        self.assertTrue(task['done'])
        self.assertFalse(task['breached'])
        self.assertEqual(self.p.apps['101'].breaches, 0)
        self.assertTrue(self.p.healthy(self.handle))

    def test_two_timeouts_short_circuit_and_recovery_reprobes(self):
        self.now += 2.001
        for _ in range(2):
            self.now = max(self.now, self.p.dirty_due.get(self.handle, 0))
            self.p.dirty.add(self.handle)
            self.p.ensure(self.handle)
            self.now += .251
            self.answer(error='timeout', breach=True, max_call_ms=250)
        b = self.p.apps['101']
        self.assertEqual(b.state, 'degraded')
        sent = len(self.wire.sent)
        for _ in range(50):
            self.p.ensure(self.handle)
        self.assertEqual(len(self.wire.sent), sent)
        self.now = b.due + .001
        self.p.tick()
        self.answer(self.raw)
        self.assertEqual(b.state, 'healthy')
        self.assertEqual(self.p.probes[self.handle], 'usable')

    def test_snapshot_race_is_rebuilt_without_breaker_breach(self):
        self.p.dirty.add(self.handle)
        self.p.ensure(self.handle)
        self.answer(dict(self.raw, dirty=True))
        self.assertEqual(self.p.apps['101'].breaches, 0)
        self.now += .041
        self.p.tick()
        self.assertTrue(self.p.pending)

    def test_stale_tree_base_ref_and_cursor_are_rejected(self):
        old = self.refs()['entry']
        self.change(node(children=[node('other', 'entry', 'Other', text='')]))
        for base in (1, self.w.cache.rev):
            with self.assertRaises(ActError) as cm:
                self.p.resolve(self.handle, old, base)
            self.assertEqual(cm.exception.reason, 'stale_rev')

    def test_ambiguous_selector_rejected_before_journal_admission(self):
        self.change(node(children=[node('a', 'push_button', 'Same'), node('b', 'push_button', 'Same')]))
        p = self.params([dict(target=dict(window=self.handle, selector=dict(provider='atspi', name='Same')),
                             intent=dict(kind='click'), expect=dict(kind='node_present', ref='n999'))],
                        base_rev=self.w.cache.rev)
        count = len(self.rows())
        with self.assertRaises(ActError) as cm:
            self.submit(p)
        self.assertEqual(cm.exception.reason, 'invalid_request')
        self.assertEqual(len(self.rows()), count)
        self.assertFalse(self.t.log)

    def test_window_geometry_change_schedules_one_new_snapshot(self):
        count = len(self.wire.sent)
        win = dict(self.w.cache.windows[10])
        win['geometry'] = dict(x=30, y=30, w=400, h=300)
        self.w.cache.upsert(10, win)
        self.w.cache.commit()
        self.p.tick()
        self.assertEqual(len(self.wire.sent), count+1)
        self.answer(self.raw)
        for _ in range(10):
            self.p.tick()
        self.assertEqual(len(self.wire.sent), count+1)

    def test_gone_window_observation_keeps_removal_without_tree(self):
        self.w.cache.remove(10)
        self.w.cache.set('focus', None)
        self.w.cache.commit()
        self.p.tick()
        result = self.w.observation(dict(channels=['auto'], scope=dict(kind='window', window=self.handle)))
        self.assertNotIn('tree', result)
        self.assertEqual(result['state']['removed'], [self.handle])

    def test_no_provider_write_for_state_only_idle(self):
        base, count = self.w.cache.rev, len(self.wire.sent)
        for _ in range(1000):
            result = self.w.observation(dict(channels=['state'], since_rev=base))
            self.assertTrue(result['unchanged'])
        self.assertEqual(len(self.wire.sent), count)
        self.assertFalse(self.t.log)


class Predicates(ProviderHarness):
    def test_each_tree_kind(self):
        refs = self.refs()
        entry, root, button = refs['entry'], refs['root'], refs['button']
        tests = [(dict(kind='node_present', ref=entry), True),
                 (dict(kind='node_absent', ref=entry), False),
                 (dict(kind='node_absent', ref='n99999'), False),
                 (dict(kind='text_equals', ref=entry, value='ab'), True),
                 (dict(kind='text_equals', ref=entry, value='AB'), False),
                 (dict(kind='text_contains', ref=entry, value='b'), True),
                 (dict(kind='node_state', ref=entry, state='editable', value=True), True),
                 (dict(kind='node_state', ref=entry, state='enabled', value=True), True),
                 (dict(kind='node_state', ref=entry, state='checked', value=False), True),
                 (dict(kind='item_present', container=root, name='Add to list', role='push_button'), True),
                 (dict(kind='item_absent', container=root, name='Missing', role='push_button'), True),
                 (dict(kind='item_absent', container=root, name='Add to list', role='push_button'), False)]
        for p, expected in tests:
            with self.subTest(p=p):
                predicate_schema(p)
                self.assertIs(self.e.evaluate(p), expected)
        self.assertTrue(TREE_KINDS - {'scroll_value'} <= {p['kind'] for p, _ in tests})
        scroll = node('scroll', 'scroll_bar', 'Scroll', value=3, axis='vertical')
        self.change(node(children=[scroll]))
        ref = self.refs()['scroll']
        self.assertTrue(self.e.evaluate(dict(kind='scroll_value', ref=ref, axis='vertical', value=3)))
        self.assertFalse(self.e.evaluate(dict(kind='scroll_value', ref=ref, axis='horizontal', value=3)))

    def test_zero_selector_matches_is_failed_verification(self):
        for kind in ('node_present', 'node_absent', 'text_equals', 'node_state'):
            p = dict(kind=kind, selector=dict(provider='atspi', name='Missing'))
            self.assertIs(self.e.evaluate(p), False)

    def test_degraded_predicates_unknown_and_dirty_not_evidence(self):
        ref = self.refs()['entry']
        self.p.dirty.add(self.handle)
        self.assertIsNone(self.e.evaluate(dict(kind='text_equals', ref=ref, value='ab')))
        self.p.dirty.clear()
        self.p.apps['101'].fail(self.now, 2)
        self.assertIsNone(self.e.evaluate(dict(kind='node_absent', ref=ref)))

    def test_item_absence_requires_complete_semantic_inventory_including_offscreen(self):
        raw = fixture()
        hidden = node('hidden', 'list_item', 'Offscreen')
        hidden['showing'] = False
        raw['children'].append(hidden)
        self.change(raw)
        root = self.refs()['root']
        self.assertFalse(any('Offscreen' in line for line in self.p.cache.page(self.handle)['lines']))
        self.assertTrue(self.e.evaluate(dict(kind='item_present', container=root, name='Offscreen', role='list_item')))
        self.assertFalse(self.e.evaluate(dict(kind='item_absent', container=root, name='Offscreen', role='list_item')))

    def test_removed_known_node_verifies_absence(self):
        ref = self.refs()['entry']
        self.change(node(children=[self.raw['children'][1]]))
        self.assertTrue(self.e.evaluate(dict(kind='node_absent', ref=ref)))

    def test_composite_predicates(self):
        p = dict(kind='all', of=[dict(kind='node_present', ref=self.refs()['entry']),
                                dict(kind='text_contains', ref=self.refs()['entry'], value='a')])
        self.assertTrue(self.e.evaluate(p))
        p['of'][1]['value'] = 'q'
        self.assertFalse(self.e.evaluate(p))

    def test_require_selected_editable_text_and_modal(self):
        ref = self.refs()['entry']
        self.p.requirements(dict(editable_ref=ref, text_equals=dict(ref=ref, value='ab')), self.handle, 1)
        with self.assertRaises(ActError) as cm:
            self.p.requirements(dict(selected_ref=ref), self.handle, 1)
        self.assertEqual(cm.exception.reason, 'precondition_failed')
        self.p.cache.windows[self.handle]['nodes'][ref]['states'].append('selected')
        self.p.requirements(dict(selected_ref=ref), self.handle, 1)
        self.w.cache.modal = [self.handle]
        self.p.requirements(dict(modal_role='frame'), self.handle, 1)
        with self.assertRaises(ActError):
            self.p.requirements(dict(modal_role='dialog'), self.handle, 1)

    def test_require_failure_never_dispatches(self):
        s = dict(target=dict(window=self.handle, ref=self.refs()['entry']),
                 require=dict(text_equals=dict(ref=self.refs()['entry'], value='WRONG')),
                 intent=dict(kind='text', text='a', replace=True))
        r = self.run_action(self.params([s]))
        self.assertEqual(r['reason'], 'precondition_failed')
        self.assertFalse(self.t.log)


class Actions(ProviderHarness):
    def text_step(self, **kw):
        return dict(target=dict(window=self.handle, ref=self.refs()['entry']),
                    intent=dict(kind='text', text='a', replace=True), **kw)

    def test_computed_default_replacement_and_insert_selection(self):
        s = self.text_step()
        normal, _ = self.e.normalise(self.params([s]))
        s = normal['steps'][0]
        h, pt = self.e.resolve(s, 1)
        expect = self.e.expectation(s, h, pt)
        self.assertEqual(expect, dict(kind='text_equals', window=self.handle, ref=self.refs()['entry'], value='a'))
        s['intent']['replace'] = False
        self.assertEqual(self.e.expectation(s, h, pt)['value'], 'ab')
        s['_node'].pop('selection')
        self.assertEqual(self.e.expectation(s, h, pt)['value'], 'aab')

    def test_explicit_expect_wins(self):
        explicit = dict(kind='text_equals', ref=self.refs()['entry'], value='explicit')
        s = self.text_step(expect=explicit)
        normal, _ = self.e.normalise(self.params([s]))
        s = normal['steps'][0]
        h, pt = self.e.resolve(s, 1)
        self.assertEqual(self.e.expectation(s, h, pt), explicit)

    def test_no_semantic_text_keeps_explicit_expect_required(self):
        r = self.run_action(self.params([dict(target=self.handle, intent=dict(kind='text', text='a'))]))
        self.assertEqual(r['reason'], 'invalid_expectation')
        self.assertFalse(self.t.log)

    def test_text_default_verifies_full_field_on_physical_route(self):
        changed = False
        def effect():
            nonlocal changed
            if not changed and ('key_down', 38) in self.t.log and len([ev for ev in self.t.log if ev == ('key_down', 38)]) >= 2:
                changed = True
                self.raw['children'][0]['text'] = 'a'
                self.change(self.raw)
        self.t.hook = effect
        r = self.run_action(self.params([self.text_step()]))
        self.assertEqual(r['status'], 'success')
        self.assertEqual(r['steps'][0]['route'], 'xtest')
        self.assertEqual(r['steps'][0]['expect_source'], 'default')
        self.assertEqual(r['steps'][0]['predicate_class'], 'tree')
        self.assertEqual(r['steps'][0]['effect']['evidence'][0]['provider'], 'atspi')
        self.assertFalse(any(row['op'] == 'invoke' for row in self.wire.sent))

    def test_same_replacement_can_skip_dispatch(self):
        s = self.text_step()
        s['intent']['text'] = 'ab'
        r = self.run_action(self.params([s]))
        self.assertEqual(r['status'], 'success')
        self.assertEqual(r['steps'][0]['disposition'], 'satisfied_without_dispatch')
        self.assertFalse(self.t.log)

    def test_node_click_is_physical_center(self):
        expected = dict(kind='text_equals', ref=self.refs()['entry'], value='a')
        s = dict(target=dict(window=self.handle, selector=dict(provider='atspi', name='Add to list')),
                 intent=dict(kind='click'), expect=expected, timeout_ms=100)
        def effect():
            if ('button_down', 1) in self.t.log and self.raw['children'][0]['text'] != 'a':
                self.raw['children'][0]['text'] = 'a'
                self.change(self.raw)
        self.t.hook = effect
        r = self.run_action(self.params([s]))
        self.assertEqual(r['status'], 'success')
        self.assertIn(('motion', 0, 150, 125), self.t.log)
        self.assertIn(('button_down', 1), self.t.log)
        self.assertFalse(any(row['op'] == 'invoke' for row in self.wire.sent))

    def test_insert_default_requires_known_insertion_position(self):
        n = self.p.cache.windows[self.handle]['nodes'][self.refs()['entry']]
        n.pop('caret')
        n.pop('selection')
        with self.assertRaises(ActError):
            self.p.computed_text(n, dict(text='a', replace=False))

    def test_dedupe_returns_prior_receipt_even_if_selector_is_now_ambiguous(self):
        s = dict(target=dict(window=self.handle, selector=dict(provider='atspi', name='Add to list')),
                 intent=dict(kind='click'), expect=dict(kind='text_equals', ref=self.refs()['entry'], value='x'),
                 timeout_ms=50)
        p = self.params([s])
        original = self.run_action(p)
        self.change(node(children=[node('a', name='Add to list'), node('b', name='Add to list')]))
        before = list(self.t.log)
        self.submit(p)
        self.assertEqual(self.result(), original)
        self.assertEqual(self.t.log, before)

    def test_restarted_provider_cannot_reconcile_old_absence_expectation(self):
        ref = self.refs()['entry']
        self.e.pending_effects = [dict(action_id='old', step_id='one', generation=GEN,
                                      delivery_quiescent=True, rev=1, expect=dict(kind='node_absent', ref=ref))]
        self.p.cache.restart()
        self.p.cache.update(self.handle, self.raw, 2)
        self.e.reconcile()
        self.assertTrue(self.e.pending_effects)
        self.assertTrue(self.e.pending_effects[0]['observed_unresolved'])

    def test_changed_node_identity_rejects_old_ref(self):
        s = self.text_step()
        self.raw['children'][0]['name'] = 'Different'
        self.change(self.raw)
        r = self.run_action(self.params([s]))
        self.assertEqual(r['reason'], 'stale_rev')
        self.assertFalse(self.t.log)

    def test_invoke_routes_async_and_named_action_is_forwarded(self):
        s = dict(target=dict(window=self.handle, ref=self.refs()['button']),
                 intent=dict(kind='invoke', action='press'),
                 expect=dict(kind='text_equals', ref=self.refs()['entry'], value='a'))
        self.submit(self.params([s]))
        for _ in range(100):
            self.tick()
            tasks = [t for t in self.p.pending.values() if t['op'] == 'invoke']
            if tasks:
                break
        else:
            self.fail('invoke never reached provider')
        request = self.wire.sent[-1]
        self.assertEqual(request['action'], 'press')
        self.assertEqual(request['key'], 'button')
        self.assertEqual(request['generation'], GEN)
        self.assertEqual(request['epoch'], 1)
        self.assertTrue(any(row['type'] == 'step_armed' for row in self.rows()))
        self.answer(dict(accepted=True, action='press'), task=tasks[0])
        self.raw['children'][0]['text'] = 'a'
        self.change(self.raw)
        self.drain()
        r = self.result()
        self.assertEqual(r['status'], 'success')
        self.assertEqual(r['steps'][0]['route'], 'atspi')
        self.assertFalse(self.t.log)


class OrderingAndRecovery(ProviderHarness):
    def test_late_snapshot_cannot_clear_new_invalidation(self):
        self.p.dirty.add(self.handle)
        self.p.ensure(self.handle)
        task = next(iter(self.p.pending.values()))
        self.wire.incoming.extend([dict(event='changed', app='101'),
                                  dict(id=task['id'], result=self.raw)])
        self.p.tick()
        self.assertIn(self.handle, self.p.dirty)
        self.assertEqual(self.p.apps['101'].breaches, 0)
        self.assertFalse(self.p.pending)
        self.now += .041
        self.p.tick()
        self.assertTrue(self.p.pending)
        self.answer(self.raw)
        self.assertNotIn(self.handle, self.p.dirty)

    def test_remove_return_same_object_path_does_not_reuse_ref(self):
        old = self.refs()['entry']
        self.wire.incoming.append(dict(event='changed', app='101', removed='entry'))
        self.p.tick()
        self.now += .041
        self.p.tick()
        self.answer(self.raw)
        self.assertNotEqual(self.refs()['entry'], old)

    def test_health_probe_catches_silent_hang_without_rebuilding_tree(self):
        snapshot = self.p.cache.windows[self.handle]
        self.now += 2.001
        self.p.ensure(self.handle, health=True)
        self.assertFalse(self.p.busy(dict(channels=['tree'])))
        task = next(iter(self.p.pending.values()))
        self.assertEqual(task['op'], 'probe')
        self.now += .251
        self.answer(error='application_unreachable', task=task, breach=True, max_call_ms=250)
        self.assertFalse(self.p.healthy(self.handle))
        self.assertIs(self.p.cache.windows[self.handle], snapshot)
        self.assertEqual(self.w.observation(dict(channels=['tree']))['tree']['status'], 'degraded')
        self.now = max(self.p.apps['101'].due, self.p.dirty_due[self.handle]) + .001
        self.p.tick()
        task = next(iter(self.p.pending.values()))
        self.assertEqual(task['op'], 'snapshot')
        self.answer(error='application_unreachable', task=task, breach=True, max_call_ms=250)
        self.assertEqual(self.p.apps['101'].state, 'degraded')

    def test_healthy_probe_never_rebuilds_snapshot_or_changes_refs(self):
        before = copy.deepcopy(self.p.cache.windows[self.handle])
        self.now += 1.001
        self.p.ensure(self.handle, health=True)
        self.answer(dict(name='Fixture'))
        self.assertEqual(self.p.cache.windows[self.handle], before)
        self.assertEqual(self.p.probes[self.handle], 'usable')
        self.assertFalse(self.p.pending)

    def test_state_only_observations_do_not_start_health_probes(self):
        before = len(self.wire.sent)
        self.now += 10
        for _ in range(100):
            self.assertFalse(self.p.busy(dict(channels=['state'])))
            self.w.observation(dict(channels=['state'], since_rev=1))
        self.assertEqual(len(self.wire.sent), before)

    def test_repeated_child_crashes_back_off_even_if_child_says_ready(self):
        for delay in (1, 2, 4, 8, 16, 30):
            self.child.code = 1
            self.p.tick()
            self.assertEqual(self.p.restart_at-self.now, delay)
            self.now = self.p.restart_at
            self.p.tick()
            self.wire, self.child = self.children[-1]
            self.p.tick()

    def test_pending_observe_expires_without_waiting_on_hung_child(self):
        self.now += 1.001
        self.p.ensure(self.handle, health=True)
        self.peer.waits = [dict(kind='observe', id=7, params=dict(channels=['tree'], lease='lease'), deadline=0)]
        self.w.peers = [self.peer]
        self.w.pending()
        self.assertFalse(self.peer.waits)
        self.assertIn('state', self.result())
        self.assertTrue(self.p.pending)

    def test_tree_scope_filters_other_window_tree_facets(self):
        self.later()
        self.w.cache.changed.add('tree['+self.w.cache.handle(11)+']')
        self.w.cache.commit()
        base = self.w.cache.rev
        self.change(dict(self.raw, name='Changed'))
        result = self.w.observation(dict(channels=['tree'], since_rev=base,
                                        scope=dict(kind='window', window=self.w.cache.handle(11))))
        self.assertTrue(result['unchanged'])

    def test_degraded_auto_observe_keeps_state_and_pixels(self):
        pixels = Mock()
        pixels.failure = None
        pixels.region.return_value = (dict(x=0, y=0, w=200, h=200), list(self.w.cache.windows.values()))
        pixels.tiles.return_value = set()
        pixels.delta.return_value = None
        pixels.settle.return_value = True
        pixels.frame.return_value = dict(frame='oracle-frame')
        self.w.pixels = pixels
        self.p.apps['101'].fail(self.now, 2)
        result = self.w.observation(dict(channels=['auto']))
        self.assertEqual(result['tree']['status'], 'degraded')
        self.assertIn('state', result)
        self.assertEqual(result['frames'], [dict(frame='oracle-frame')])
        self.assertEqual(result['providers']['atspi']['state'], 'degraded')


class FakeAccessible:
    def __init__(self, path, role='frame', name='Fixture', children=(), states=(), bounds=(100, 100, 100, 50)):
        self.path, self.role, self.name = path, role, name
        self.children, self.states, self.bounds = list(children), set(states), bounds
        self.app = NS(bus_name=':1.42')
        self.pid = 42
        self.names, self.calls = ['click', 'press'], []
        self.text = None

    def __iter__(self):
        return iter(self.children)

    def clearCache(self):
        pass

    def getState(self):
        return NS(contains=lambda name: name in self.states)

    def getRoleName(self):
        return self.role

    def get_process_id(self):
        return self.pid

    def queryComponent(self):
        return NS(getExtents=lambda space: self.bounds)

    def queryAction(self):
        def perform(index):
            self.calls.append(self.names[index])
            return True
        return NS(nActions=len(self.names), getName=lambda i: self.names[i], doAction=perform)

    def queryText(self):
        if self.text is None:
            raise NotImplementedError()
        return NS(characterCount=len(self.text), getText=lambda a, b: self.text,
                  caretOffset=1, getNSelections=lambda: 1, getSelection=lambda i: (0, 1))

    def queryValue(self):
        raise NotImplementedError()


def fake_atspi(apps=()):
    a = NS(Registry=NS(getDesktop=lambda i: apps), DESKTOP_COORDS=0)
    for state in ('SHOWING', 'VISIBLE', 'ENABLED', 'SENSITIVE', 'FOCUSED', 'SELECTED', 'CHECKED',
                  'EXPANDED', 'EDITABLE', 'DEFUNCT', 'VERTICAL', 'HORIZONTAL'):
        setattr(a, 'STATE_'+state, state.lower())
    return a


class BackendContracts(unittest.TestCase):
    def setUp(self):
        self.obj = FakeAccessible('/button', role='push button', name='Create Folder',
                                  states=['showing', 'visible', 'enabled', 'sensitive', 'focused'])
        frame = FakeAccessible('/frame', children=[self.obj], states=['showing', 'visible', 'enabled', 'sensitive'])
        app = FakeAccessible('/app', children=[frame])
        self.b = Backend(fake_atspi([app]))
        self.task = dict(id=1, op='snapshot', app='42', window='w', pid=42,
                         rect=dict(x=100, y=100, w=100, h=50))

    def test_backend_snapshot_calls_real_adapter_methods_with_fake_accessibles(self):
        raw = self.b.snapshot(self.task)
        n = raw['children'][0]
        self.assertEqual(n['role'], 'push_button')
        self.assertEqual(n['name'], 'Create Folder')
        self.assertEqual(n['bounds'], [100, 100, 100, 50])
        self.assertEqual(n['states'], ['focused'])
        self.assertEqual(n['actions'], ['click', 'press'])
        self.assertEqual(n['key'], ':1.42:/button')

    def test_backend_text_retains_whole_field_caret_and_selection(self):
        self.obj.role, self.obj.text = 'text', 'x'*100
        self.obj.states.add('editable')
        n = self.b.snapshot(self.task)['children'][0]
        self.assertEqual(n['text'], 'x'*100)
        self.assertEqual(n['caret'], 1)
        self.assertEqual(n['selection'], [0, 1])

    def test_explicit_named_action_default_press_then_first(self):
        self.b.snapshot(self.task)
        task = dict(self.task, op='invoke', key=':1.42:/button')
        with patch('worker_atspi.check_authority'):
            self.assertEqual(self.b.invoke(dict(task, action='click'))['action'], 'click')
            self.assertEqual(self.b.invoke(task)['action'], 'press')
            self.obj.names = ['activate']
            self.assertEqual(self.b.invoke(task)['action'], 'activate')
        self.assertEqual(self.obj.calls, ['click', 'press', 'activate'])

    def test_invalid_action_and_stale_version_never_invoke(self):
        self.b.snapshot(self.task)
        task = dict(self.task, op='invoke', key=':1.42:/button')
        with self.assertRaises(ValueError):
            self.b.invoke(dict(task, action='arbitrary-command'))
        self.obj.name = 'Changed'
        with self.assertRaises(ValueError):
            self.b.invoke(dict(task, role='push_button', name='Create Folder'))
        self.assertFalse(self.obj.calls)

    def test_defunct_or_disabled_node_never_invokes(self):
        self.b.snapshot(self.task)
        for states in ({'enabled', 'defunct'}, set()):
            self.obj.states = states
            with self.assertRaises(ValueError):
                self.b.invoke(dict(self.task, key=':1.42:/button'))
        self.assertFalse(self.obj.calls)

    def test_child_event_bumps_app_version_and_sends_removed_identity(self):
        self.b.snapshot(self.task)
        results = Mock()
        event = NS(source=self.obj, type='object:children-changed:remove', any_data=self.obj)
        self.b.changed(event, results)
        self.assertEqual(self.b.versions['42'], 1)
        self.assertEqual(results.put.call_args.args[0], dict(event='changed', app='42', removed=':1.42:/button'))

    def test_backend_overlap_fallback_only_when_pid_is_missing(self):
        self.assertEqual(self.b.snapshot(dict(self.task, pid=None))['key'], ':1.42:/frame')
        self.assertEqual(self.b.snapshot(dict(self.task, pid=999))['status'], 'unmatched')

    def test_tree_change_during_build_is_installed_dirty(self):
        original = self.obj.getState
        def raced():
            self.b.versions['42'] = self.b.versions.get('42', 0) + 1
            return original()
        self.obj.getState = raced
        self.assertTrue(self.b.snapshot(self.task)['dirty'])
        self.assertTrue(self.b.objects)

    def test_missing_application_is_empty_snapshot(self):
        self.b.a.Registry.getDesktop = lambda i: []
        raw = self.b.snapshot(self.task)
        self.assertFalse(raw['showing'])
        self.assertFalse(included(raw))


class Dialogs(ProviderHarness):
    def dialog(self):
        self.later()
        win = self.w.cache.windows[11]
        win.update(type='dialog', transient_for=10, is_modal=True)
        self.w.x.window_pids[11] = 101
        handle = win['handle']
        self.w.cache.set('focus', handle)
        self.w.cache.set('modal', [handle])
        self.w.cache.commit()
        entry = node('rename-entry', 'text', '', text='Folder')
        entry['states'] = ['editable', 'focused']
        raw = node('dialog', 'dialog', 'Rename', children=[entry,
            node('rename', 'push_button', 'Rename'), node('cancel', 'push_button', 'Cancel')])
        self.p.cache.update(handle, raw, self.w.cache.rev)
        self.p.probes[handle] = 'usable'
        return handle, raw

    def test_exact_rename_classifier_and_reuse_after_edit(self):
        handle, raw = self.dialog()
        self.assertFalse(self.p.classify(handle, 'rename', initial='wrong'))
        result = self.p.classify(handle, 'rename', initial='Folder', remember=True)
        self.assertTrue(result)
        raw['children'][0]['text'] = 'New Name'
        self.p.cache.update(handle, raw, self.w.cache.rev)
        self.assertEqual(self.p.classify(handle, 'rename'), result)
        self.p.requirements(dict(modal_role='rename'), handle, self.w.cache.rev)
        raw['children'].pop()
        self.p.cache.update(handle, raw, self.w.cache.rev)
        self.assertFalse(self.p.classify(handle, 'rename'))

    def test_dialog_tree_predicate_exports_verified_bindings(self):
        handle, raw = self.dialog()
        self.e.dialog_initial = 'Folder'
        p = dict(kind='dialog_present', parent=self.handle, role='rename',
                 bind=dict(dialog='dialog', entry=dict(role='text')))
        self.assertTrue(self.e.evaluate(p))
        exports = self.e.exports(p)
        self.assertEqual(exports['dialog'], dict(window=handle))
        self.assertEqual(exports['entry']['window'], handle)
        bound = self.e.bound(dict(target=dict(from_step='dialog', name='entry'),
                                 expect=dict(kind='text_equals', ref=dict(from_step='dialog', name='entry'), value='New')),
                            dict(dialog=exports))
        self.assertEqual(bound['target'], exports['entry'])
        self.assertEqual(bound['expect']['ref'], exports['entry']['ref'])

    def test_three_step_rename_batch_with_verified_cross_step_refs(self):
        item = node('folder', 'icon', 'Folder')
        item['states'] = ['selected']
        root_raw = node(children=[item])
        self.p.cache.update(self.handle, root_raw, 1)
        self.w.cache.baseline()
        refs = self.refs()
        dialog_handle = None
        renamed = []
        phase = 0
        def oracle():
            nonlocal phase, dialog_handle
            if phase == 0 and ('key_down', 68) in self.t.log:
                phase = 1
                dialog_handle, raw = self.dialog()
            elif phase == 1 and len([ev for ev in self.t.log if ev == ('key_down', 38)]) >= 3:
                phase = 2
                snap = self.p.cache.windows[dialog_handle]
                entry = next(n for n in snap['nodes'].values() if n['role'] == 'text')
                raw = node('dialog', 'dialog', 'Rename', children=[
                    node('rename-entry', 'text', '', text='aa'),
                    node('rename', 'push_button', 'Rename'), node('cancel', 'push_button', 'Cancel')])
                raw['children'][0]['states'] = ['editable', 'focused']
                self.p.cache.update(dialog_handle, raw, self.w.cache.rev+1)
                self.p.bump([dialog_handle])
            elif phase == 2 and ('key_down', 36) in self.t.log:
                phase = 3
                renamed.append('aa')
                self.w.cache.remove(11)
                self.w.cache.set('focus', self.handle)
                self.w.cache.set('modal', [])
                self.w.cache.commit()
                root_raw['children'][0]['name'] = 'aa'
                self.p.cache.update(self.handle, root_raw, self.w.cache.rev+1)
                self.p.bump([self.handle])
        self.t.hook = oracle
        steps = [dict(id='dialog', target=dict(window=self.handle, ref=refs['folder']),
                      require=dict(selected_ref=refs['folder']), intent=dict(kind='key', keys=['F2']),
                      expect=dict(kind='dialog_present', parent=self.handle, role='rename',
                                  bind=dict(dialog='dialog', entry=dict(role='text')))),
                 dict(id='name', target=dict(from_step='dialog', name='entry'), require=dict(modal_role='rename'),
                      intent=dict(kind='text', text='aa', replace=True)),
                 dict(id='commit', target=dict(from_step='dialog', name='dialog'), require=dict(modal_role='rename'),
                      intent=dict(kind='key', keys=['Return']), expect=dict(kind='all', of=[
                          dict(kind='dialog_absent', handle=dict(from_step='dialog', name='dialog')),
                          dict(kind='item_present', container=refs['root'], name='aa', role='icon')]))]
        r = self.run_action(self.params(steps))
        self.assertEqual(r['status'], 'success', r)
        self.assertEqual(r['completed_steps'], ['dialog', 'name', 'commit'])
        self.assertEqual(renamed, ['aa'])
        self.assertEqual(r['steps'][1]['expect']['value'], 'aa')
        self.assertEqual(r['steps'][1]['target_resolved'], dialog_handle)

    def test_unverified_or_forward_bindings_rejected(self):
        with self.assertRaises(ActError) as cm:
            self.e.bound(dict(from_step='future', name='entry'), {})
        self.assertEqual(cm.exception.reason, 'invalid_request')

    def test_duplicate_editable_fields_reject_classifier(self):
        handle, raw = self.dialog()
        other = node('other-entry', 'text', '', text='Folder')
        other['states'] = ['editable']
        raw['children'].append(other)
        self.p.cache.update(handle, raw, self.w.cache.rev)
        self.assertFalse(self.p.classify(handle, 'rename', initial='Folder'))


class IPC(unittest.TestCase):
    def test_fragmented_length_prefix_and_backpressure(self):
        raw = packet(dict(id=1, result='é'))
        sock = Mock()
        sock.send.side_effect = [BlockingIOError(), 2, len(raw)]
        sock.recv.side_effect = [raw[:2], BlockingIOError(), raw[2:5], BlockingIOError(), raw[5:], BlockingIOError()]
        wire = Wire(sock)
        wire.send(dict(id=1, result='é'))
        self.assertEqual(wire.pump(), [])
        self.assertEqual(wire.pump(), [])
        self.assertEqual(wire.pump(), [dict(id=1, result='é')])
        self.assertFalse(wire.output)

    def test_eof_and_oversized_packet_fail_closed(self):
        for raw in (b'', struct.pack('!I', 10_000_000)):
            sock = Mock()
            sock.recv.side_effect = [raw, BlockingIOError()]
            with self.assertRaises((EOFError, ValueError)):
                Wire(sock).pump()

    def test_pid_first_then_iou_with_threshold(self):
        frames = [dict(pid=1, bounds=[0, 0, 10, 10]), dict(pid=2, bounds=[0, 0, 100, 100]),
                  dict(pid=1, bounds=[0, 0, 20, 20])]
        rect = dict(x=0, y=0, w=100, h=100)
        self.assertIsNone(match_frame(frames, 1, rect))
        self.assertIsNone(match_frame(frames, 999, rect))
        self.assertIs(match_frame(frames, None, rect), frames[1])
        self.assertIsNone(match_frame([], 1, rect))

    def test_provider_spawn_environment_is_session_private(self):
        worker = NS(record=dict(display=93, home='/session/home', runtime_dir='/session/runtime',
                                atspi_address='unix:path=/session/a11y', bus_address='unix:path=/session/bus'),
                    sid='s1', state=Path('/session/state'), sd=Path('/session'))
        provider = Provider.__new__(Provider)
        provider.w = worker
        parent, child = Mock(), Mock()
        child.fileno.return_value = 9
        with patch('worker_tree.socket.socketpair', return_value=(parent, child)), \
             patch('worker_tree.Path.open', return_value=Mock()), \
             patch('worker_tree.subprocess.Popen') as popen, \
             patch.dict('os.environ', dict(DISPLAY=':0', AT_SPI_BUS_ADDRESS='host', DBUS_SESSION_BUS_ADDRESS='host')):
            provider.spawn()
        env = popen.call_args.kwargs['env']
        self.assertEqual(env['DISPLAY'], ':93')
        self.assertEqual(env['AT_SPI_BUS_ADDRESS'], 'unix:path=/session/a11y')
        self.assertEqual(env['DBUS_SESSION_BUS_ADDRESS'], 'unix:path=/session/bus')
        self.assertEqual(env['ARK_SESSION_ID'], 's1')
        self.assertEqual(env['HOME'], '/session/home')
        self.assertEqual(popen.call_args.kwargs['pass_fds'], (9,))

    def test_provider_spawn_refuses_host_or_unallocated_display(self):
        for display in (0, 90, 121, ':91'):
            p = Provider.__new__(Provider)
            p.w = NS(record=dict(display=display, atspi_address='private', bus_address='private'))
            with self.subTest(display=display), patch('worker_tree.subprocess.Popen') as popen, self.assertRaises(ValueError):
                p.spawn()
            popen.assert_not_called()

    def test_missing_session_bus_never_discovers_host_bus(self):
        p = Provider.__new__(Provider)
        p.w = NS(record={})
        with patch('worker_tree.subprocess.Popen') as popen, self.assertRaises(ValueError):
            p.spawn()
        popen.assert_not_called()

    def test_invoke_child_checks_control_and_deadline(self):
        task = dict(control='/fake/control', generation=GEN, epoch=5, deadline=20)
        good = struct.pack('<4sI16sQIIQ16s', b'ARKC', 1, bytes.fromhex(GEN), 5, 0, 0, 0, bytes(16))
        with patch('worker_atspi.Path.read_bytes', return_value=good), patch('worker_atspi.time.monotonic', return_value=10):
            check_authority(task)
            for changes in (dict(epoch=4), dict(generation='f'*32), dict(deadline=5)):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    check_authority(dict(task, **changes))
        paused = struct.pack('<4sI16sQIIQ16s', b'ARKC', 1, bytes.fromhex(GEN), 5, 3, 0, 0, bytes(16))
        with patch('worker_atspi.Path.read_bytes', return_value=paused), self.assertRaises(ValueError):
            check_authority(task)


if __name__ == '__main__':
    unittest.main(verbosity=2)
