"""Adapter packs: fake RPC/socket peers and filesystem fixtures; no desktop."""
import copy
import importlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_worker_w5 import a, shown, receipt, FakeWorker
from test_worker_w5_fix1 import adapter_with_rpc
from worker_act import predicate_schema
from packs import Registry, REGISTRY
from packs import thunar


def listing(names=('old',), *, focus='w1', wm_class='Thunar', rev=11):
    lines = ['n1 frame "Files" @0,0,600,400', ' n2 tree_table "" @0,0,600,400']
    for i, name in enumerate(names):
        row = 3 + i * 3
        lines.extend([f'  n{row} table_cell "" @0,0,100,20',
                      f'   n{row+1} table_cell {json.dumps(name)} [selected] @0,0,80,20',
                      f'   n{row+2} table_cell "4 KB" @80,0,20,20'])
    return dict(rev=rev, full=True, settled=True, state=dict(focus=focus, modal=[],
                windows=[dict(handle='w1', **{'class': wm_class}),
                         dict(handle='w2', **{'class': 'XTerm'})]),
                tree=dict(window=focus, rev=rev, lines=lines, cursor=None, truncated=False))


def create_publication(window='dialog', *, dialog='w3', button='n20', container='n30', rev=13):
    value = listing(rev=rev)
    value['state'].update(focus=dialog, modal=[dialog])
    value['state']['windows'].append(dict(handle=dialog, mapped=True, title='Create New Folder'))
    if window == 'dialog':
        value['tree'].update(window=dialog, lines=[f'n19 dialog "Create New Folder"',
                                                  f' {button} push_button "Create"'])
    else:
        value['tree']['lines'][1] = f' {container} tree_table ""'
    return value


def playbook_acts():
    tail = thunar.PLAYBOOK.split('Create-folder example', 1)[1]
    first, end = json.JSONDecoder().raw_decode(tail[tail.index('{"steps":'):])
    tail = tail[tail.index('{"steps":') + end:]
    second, _ = json.JSONDecoder().raw_decode(tail[tail.index('{"steps":'):])
    return first, second


def verified(steps, status='success'):
    value = receipt()
    value['status'] = status
    value['steps'] = [dict(id=s['id'], effect=dict(status='verified')) for s in steps]
    return value


class RegistryTests(unittest.TestCase):
    def test_discovery_contract_class_matching_and_static_description(self):
        self.assertEqual(REGISTRY.diagnostics, [])
        self.assertIs(REGISTRY.for_class('tHuNaR'), thunar)
        self.assertIsNone(REGISTRY.for_class('Thunar-extra'))
        self.assertIsNone(REGISTRY.for_class(None))
        tool = next(t for t in a.TOOLS if t['name'] == 'use_skill')
        self.assertEqual(tool['inputSchema']['required'], ['name', 'args'])
        self.assertEqual(tool['inputSchema']['properties'], dict(name={'type': 'string'}, args={'type': 'object'}))
        for name, (_, skill) in REGISTRY.skills.items():
            self.assertIn(name, tool['description'])
            self.assertIn(skill['summary'], tool['description'])
            for param in skill['params']['properties']:
                self.assertIn(param, tool['description'])
            self.assertFalse(set(skill['params']['properties']) & {'risk', 'allow_stale_pixels'})

    def test_broken_import_invalid_schema_and_duplicates_are_isolated(self):
        with tempfile.TemporaryDirectory(prefix='pack-registry-') as temp:
            package = Path(temp) / 'fixture_packs'
            package.mkdir()
            (package / '__init__.py').write_text('')
            good = ('NAME="good"\nCLASSES=("Good",)\nPLAYBOOK="facts"\n'
                    'SKILLS={"good.run":{"params":{"type":"object","properties":{},'
                    '"additionalProperties":False},"summary":"Run.","run":lambda a,b:{}}}\n')
            (package / 'a_good.py').write_text(good)
            (package / 'b_broken.py').write_text('raise RuntimeError("broken import")')
            (package / 'c_authority.py').write_text(good.replace('NAME="good"', 'NAME="bad"')
                .replace('good.run', 'bad.run').replace('"properties":{}', '"properties":{"nested":{"type":"object",'
                '"properties":{"risk":{"type":"string"}}}}'))
            (package / 'd_duplicate.py').write_text(good)
            (package / 'e_exit.py').write_text('raise SystemExit(1)')
            (package / 'f_invalid_params.py').write_text(good.replace('NAME="good"', 'NAME="badparams"')
                .replace('good.run', 'badparams.run').replace('"properties":{}', '"properties":7'))
            (package / 'g_good.py').write_text(good.replace('good', 'second').replace('Good', 'Second'))
            try:
                with patch.object(sys, 'path', [temp] + sys.path):
                    importlib.invalidate_caches()
                    registry = Registry('fixture_packs')
                self.assertEqual([p.NAME for p in registry.packs], ['good', 'second'])
                self.assertEqual(list(registry.skills), ['good.run', 'second.run'])
                self.assertEqual([d['module'].split('.')[-1] for d in registry.diagnostics],
                                 ['b_broken', 'c_authority', 'd_duplicate', 'e_exit', 'f_invalid_params'])
            finally:
                for key in list(sys.modules):
                    if key == 'fixture_packs' or key.startswith('fixture_packs.'):
                        sys.modules.pop(key)

    def test_generic_description_has_only_grammar_and_playbook_owns_example(self):
        description = next(t['description'] for t in a.TOOLS if t['name'] == 'act')
        for text in ('Create-folder', 'Thunar', 'Reports', 'Control_L', '{"steps":'):
            self.assertNotIn(text, description)
        self.assertIn('strong expect', description)
        self.assertIn('text_equals', description)
        self.assertIn('Bindings refer to verified earlier steps', description)
        self.assertIn('Thunar-specific Create-folder example', thunar.PLAYBOOK)
        for text in ('Ctrl+2', 'empty names', 'F2', 'Return', 'modal', '27+', 'virtualized', 'via:"keys"', '200'):
            self.assertIn(text, thunar.PLAYBOOK)
        example, _ = json.JSONDecoder().raw_decode(thunar.PLAYBOOK[thunar.PLAYBOOK.index('{"steps":'):])
        self.assertEqual(len(example['steps']), 2)

    def test_create_playbook_uses_two_acts_with_fresh_create_ref(self):
        first, second = playbook_acts()
        self.assertEqual(first['steps'][0]['expect']['bind'],
                         dict(dialog='dialog', entry=dict(role='text')))
        self.assertEqual(len(first['steps']), 2)
        self.assertEqual(second['steps'], [dict(id='3', timeout_ms=3500,
            target=dict(window='<dialog-handle>', ref='<create-ref>'),
            require=dict(modal='<dialog-handle>'), intent=dict(kind='invoke'),
            expect=dict(kind='all', of=[dict(kind='dialog_absent', handle='<dialog-handle>'),
                dict(kind='item_present', container='<folder-list-ref>', name='Reports')]))])
        self.assertIn('provider is busy re-walking', thunar.PLAYBOOK)
        self.assertNotIn('Return', thunar.PLAYBOOK.split('Create-folder example', 1)[1])


class ActivationTests(unittest.TestCase):
    def setUp(self):
        self.adapter = adapter_with_rpc()

    def observe(self, value, args=None):
        self.adapter.rpc.call.return_value = value
        return self.adapter.call('observe', args or {})

    def test_focused_class_selects_pack_and_note_follows_state(self):
        raw = listing(wm_class='tHUNAR')
        before = copy.deepcopy(raw)
        state_before = dict(self.adapter.state)
        result = self.observe(raw)
        self.assertIs(self.adapter.active_pack, thunar)
        self.assertEqual(shown(result), a.render_observe(raw))
        self.assertEqual(raw, before)
        self.assertEqual(result['content'][1]['text'], '[pack: thunar]\n' + thunar.PLAYBOOK + '\n[/pack]')
        self.assertEqual(self.adapter.state, dict(state_before, rev=11))
        self.assertTrue(self.adapter.ready)
        self.assertEqual(self.adapter.meta['lease'], 'lease')
        result = self.observe(listing(focus='w2'))
        self.assertIsNone(self.adapter.active_pack)
        self.assertEqual(len(result['content']), 1)

    def test_scoped_unfocused_tree_does_not_activate_its_pack(self):
        value = listing(focus='w2')
        value['state']['windows'] = value['state']['windows'][:1]
        value['tree']['window'] = 'w1'
        self.observe(value, dict(scope=dict(kind='window', window='w1'), full=True))
        self.assertIsNone(self.adapter.active_pack)

    def test_delta_merges_classes_and_removals_and_unchanged_keeps_pack(self):
        self.observe(listing())
        self.observe(dict(rev=12, full=False, settled=True, state=dict(focus='w2', windows=[], modal=[])))
        self.assertIsNone(self.adapter.active_pack)
        self.observe(dict(rev=13, full=False, settled=True, state=dict(focus='w1', windows=[], modal=[])))
        self.assertIs(self.adapter.active_pack, thunar)
        self.observe(dict(rev=14, full=False, settled=True, unchanged=True))
        self.assertIs(self.adapter.active_pack, thunar)
        self.observe(dict(rev=15, full=False, settled=True, state=dict(focus=None, removed=['w1'])))
        self.assertIsNone(self.adapter.active_pack)

    def test_unsettled_resync_and_live_modal_do_not_switch_owner(self):
        self.observe(listing())
        for extra in (dict(settled=False), dict(resync_required=True)):
            self.observe(dict(listing(focus='w2'), **extra))
            self.assertIs(self.adapter.active_pack, thunar)
        value = listing(focus='w2')
        value['state']['modal'] = ['w2']
        self.observe(value)
        self.assertIs(self.adapter.active_pack, thunar)
        self.observe(listing(focus='w2'))
        self.assertIsNone(self.adapter.active_pack)

    def test_settled_unchanged_uses_previously_unsettled_deltas_and_retains_modal(self):
        self.observe(listing())
        self.observe(dict(listing(focus='w2'), settled=False))
        self.assertIs(self.adapter.active_pack, thunar)
        self.observe(dict(rev=12, full=False, settled=True, unchanged=True))
        self.assertIsNone(self.adapter.active_pack)
        self.observe(listing())
        modal = listing(focus='w2')
        modal['state']['modal'] = ['w2']
        self.observe(modal)
        self.observe(dict(rev=13, full=False, settled=True, unchanged=True))
        self.assertIs(self.adapter.active_pack, thunar)
        self.observe(dict(rev=14, full=False, settled=True, state=dict(windows=[], modal=[])))
        self.assertIsNone(self.adapter.active_pack)

    def test_unknown_and_wrong_pack_errors_name_loaded_and_active_packs(self):
        with self.assertRaises(a.AdapterError) as exc:
            self.adapter.call('use_skill', dict(name='missing', args={}))
        self.assertIn('loaded skills: thunar.create_folder, thunar.rename', exc.exception.reason)
        self.adapter.rpc.call.assert_not_called()
        self.adapter.rpc.call.return_value = listing(focus='w2')
        with self.assertRaisesRegex(a.AdapterError, 'active pack: none'):
            self.adapter.call('use_skill', dict(name='thunar.rename', args=dict(old='old', new='new')))
        self.assertEqual([c.args[0] for c in self.adapter.rpc.call.call_args_list], ['window.observe'])

    def test_wrong_active_pack_error_and_malformed_tool_params(self):
        with patch.object(a.REGISTRY, 'for_class', return_value=type('Pack', (), {'NAME': 'other', 'PLAYBOOK': 'Other facts'})):
            self.adapter.rpc.call.return_value = listing()
            with self.assertRaisesRegex(a.AdapterError, 'active pack: other'):
                self.adapter.call('use_skill', dict(name='thunar.rename', args={}))
        for args in ({}, dict(name=2, args={}), dict(name='thunar.rename', args=[]),
                     dict(name='thunar.rename', args={}, risk='low')):
            with self.subTest(args=args), self.assertRaises(a.AdapterError):
                self.adapter.call('use_skill', args)

    def test_dispatch_errors_are_tool_results_over_mcp(self):
        self.adapter.rpc.call.return_value = listing(focus='w2')
        for name, phrase in (('missing', 'loaded skills:'), ('thunar.rename', 'active pack: none')):
            request = dict(jsonrpc='2.0', id=1, method='tools/call',
                           params=dict(name='use_skill', arguments=dict(name=name, args={})))
            outgoing = io.StringIO()
            a.mcp_serve(self.adapter, io.StringIO(json.dumps(request) + '\n'), outgoing)
            result = json.loads(outgoing.getvalue())['result']
            self.assertTrue(result['isError'])
            self.assertIn(phrase, shown(result)['error'])


class SkillTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='thunar-skills-')
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        (self.directory / 'home').mkdir()
        self.adapter = adapter_with_rpc()
        self.adapter.sd = self.directory
        self.observations = [listing()]
        self.observes = []
        self.acts = []
        self.status = 'success'
        self.mutate = lambda: None
        self.creating = False
        self.commit_observations = []
        self.receipts = {}
        def rpc(method, params, **kwargs):
            if method == 'window.observe':
                self.observes.append(copy.deepcopy(params))
                if self.creating:
                    return copy.deepcopy(self.commit_observations.pop(0) if self.commit_observations else
                        create_publication('main' if params.get('scope', {}).get('window') == 'w1' else 'dialog'))
                return copy.deepcopy(self.observations.pop(0) if len(self.observations) > 1 else self.observations[0])
            if method == 'window.act':
                self.acts.append(copy.deepcopy(params))
                if params['steps'][0]['intent'].get('keys') == ['Control_L', 'Shift_L', 'n']:
                    self.creating = self.status == 'success'
                else:
                    self.mutate()
                    if params['steps'][0]['intent']['kind'] == 'invoke':
                        self.creating = False
                value = verified(params['steps'], self.status)
                value.update(action_id=params['action_id'], generation=params['generation'])
                self.receipts[params['action_id']] = value
                return value
            if method == 'action.get':
                return dict(status='final', receipt=self.receipts[params['action_id']])
            raise AssertionError(method)
        self.adapter.rpc.call.side_effect = rpc

    def test_exact_rename_batch_row_target_name_cell_guard_and_oracle(self):
        (self.directory / 'home/old').touch()
        self.mutate = lambda: (self.directory / 'home/old').rename(self.directory / 'home/new')
        result = shown(self.adapter.call('use_skill', dict(name='thunar.rename', args=dict(old='old', new='new'))))
        self.assertEqual(result, dict(status='success', verified=True))
        expected = [
            dict(id='1', target=dict(window='w1', ref='n3'), require=dict(selected_ref='n4'),
                 intent=dict(kind='key', keys=['F2']), timeout_ms=2000,
                 expect=dict(kind='dialog_present', parent='w1', role='rename',
                             bind=dict(dialog='dialog', entry=dict(role='text')))),
            dict(id='2', target=dict(from_step='1', name='entry'), timeout_ms=3500,
                 intent=dict(kind='text', text='new', replace=True)),
            dict(id='3', target=dict(from_step='1', name='dialog'), timeout_ms=3500,
                 intent=dict(kind='key', keys=['Return']), expect=dict(kind='all', of=[
                     dict(kind='dialog_absent', handle=dict(from_step='1', name='dialog')),
                     dict(kind='node_present', window='w1', selector=dict(provider='atspi', role='table_cell', name='new'))]))]
        self.assertEqual(self.acts[0]['steps'], expected)
        self.assertEqual(len(self.acts), 1)
        for step in expected:
            if 'expect' in step:
                predicate_schema(step['expect'])
        self.assertEqual(self.acts[0]['base_rev'], 11)
        self.assertEqual(self.acts[0]['lease'], 'lease')
        self.assertEqual(self.acts[0]['control_epoch'], 3)
        self.assertNotIn('pending', self.adapter.state)

    def test_create_two_acts_bind_only_dialog_entry_and_resolve_fresh_targets(self):
        self.mutate = lambda: (self.directory / 'home/Reports').mkdir()
        self.assertEqual(thunar.create_folder(self.adapter, dict(name='Reports')), dict(status='success', verified=True))
        self.assertEqual(len(self.acts), 2)
        steps = self.acts[0]['steps']
        self.assertEqual(steps, [dict(id='1', target='w1', timeout_ms=2000,
            intent=dict(kind='key', keys=['Control_L', 'Shift_L', 'n']),
            expect=dict(kind='dialog_present', parent='w1', match=dict(title_re='[Cc]reate.*[Ff]older'),
                        bind=dict(dialog='dialog', entry=dict(role='text')))),
            dict(id='2', target=dict(from_step='1', name='entry'), timeout_ms=3500,
                 intent=dict(kind='text', text='Reports', replace=True))])
        self.assertEqual(self.acts[1]['steps'], [dict(id='3', timeout_ms=3500,
            target=dict(window='w3', ref='n20'), require=dict(modal='w3'), intent=dict(kind='invoke'),
            expect=dict(kind='all', of=[dict(kind='dialog_absent', handle='w3'),
                dict(kind='item_present', container='n30', name='Reports')]))])
        self.assertEqual(self.acts[1]['base_rev'], 13)
        self.assertEqual([c.args[0] for c in self.adapter.rpc.call.call_args_list],
                         ['window.observe', 'window.act', 'window.observe', 'window.observe', 'window.act'])
        for step in (steps[0], self.acts[1]['steps'][0]):
            predicate_schema(step['expect'])

    def test_create_dialog_title_fallback_and_scoped_button_tree(self):
        session = create_publication('main')
        session['state']['modal'] = []
        session['state']['windows'].insert(0, dict(handle='unmapped', mapped=False, title='Create Folder'))
        self.commit_observations = [session, create_publication(), create_publication('main')]
        self.assertEqual(thunar.create_folder(self.adapter, dict(name='new')),
                         dict(status='success', verified=False))
        self.assertEqual(self.acts[1]['steps'][0]['target'], dict(window='w3', ref='n20'))
        self.assertEqual(self.observes[-2]['scope'], dict(kind='window', window='w3'))

    def test_create_commit_retries_only_not_dispatched_once_with_fresh_targets(self):
        for dispatch in ('not_dispatched', 'accepted', 'unknown', None):
            for retry_dispatch in ('not_dispatched', 'accepted'):
                with self.subTest(dispatch=dispatch, retry_dispatch=retry_dispatch):
                    self.acts, self.observes, self.creating = [], [], False
                    self.commit_observations = [create_publication(), create_publication('main'),
                        create_publication(dialog='w4', button='n40'),
                        create_publication('main', dialog='w4', container='n50')]
                    original = self.adapter.rpc.call.side_effect
                    def rpc(method, params, **kwargs):
                        value = original(method, params, **kwargs)
                        if method == 'window.act' and params['steps'][0]['id'] == '3':
                            self.creating = True
                            value.update(status='failed', reason='provider_degraded')
                            value['steps'][0]['dispatch_status'] = dispatch if len(self.acts) == 2 else retry_dispatch
                        return value
                    with patch.object(self.adapter.rpc, 'call', side_effect=rpc), \
                            patch.object(thunar, '_oracle') as oracle:
                        self.assertEqual(thunar.create_folder(self.adapter, dict(name='new')),
                                         dict(status='failed', detail='provider_degraded'))
                        oracle.assert_not_called()
                    retried = dispatch == 'not_dispatched'
                    self.assertEqual(len(self.acts), 3 if retried else 2)
                    self.assertEqual(len(self.observes), 5 if retried else 3)
                    if retried:
                        step = self.acts[2]['steps'][0]
                        self.assertEqual(step['target'], dict(window='w4', ref='n40'))
                        self.assertEqual(step['require'], dict(modal='w4'))
                        self.assertEqual(step['expect']['of'][1]['container'], 'n50')

    def test_create_safe_commit_retry_can_succeed_and_run_oracle(self):
        original = self.adapter.rpc.call.side_effect
        def rpc(method, params, **kwargs):
            value = original(method, params, **kwargs)
            if method == 'window.act' and params['steps'][0]['id'] == '3' and len(self.acts) == 2:
                self.creating = True
                value.update(status='failed', reason='provider_degraded')
                value['steps'][0]['dispatch_status'] = 'not_dispatched'
            return value
        with patch.object(self.adapter.rpc, 'call', side_effect=rpc), \
                patch.object(thunar, '_oracle', return_value=True) as oracle:
            self.assertEqual(thunar.create_folder(self.adapter, dict(name='new')),
                             dict(status='success', verified=True))
            oracle.assert_called_once_with(self.adapter, dict(name='new'), 'new')
            lookup = [c for c in self.adapter.rpc.call.call_args_list if c.args[0] == 'action.get']
        self.assertEqual(len(self.acts), 3)
        self.assertEqual(len(lookup), 1)
        self.assertEqual(lookup[0].args[1]['action_id'], self.acts[1]['action_id'])

    def test_create_failed_commit_without_dispatch_proof_returns_original_outcome(self):
        original = self.adapter.rpc.call.side_effect
        def rpc(method, params, **kwargs):
            if method == 'action.get':
                raise OSError('receipt unavailable')
            value = original(method, params, **kwargs)
            if method == 'window.act' and params['steps'][0]['id'] == '3':
                value.update(status='failed', reason='provider_degraded')
            return value
        with patch.object(self.adapter.rpc, 'call', side_effect=rpc):
            self.assertEqual(thunar.create_folder(self.adapter, dict(name='new')),
                             dict(status='failed', detail='provider_degraded'))
        self.assertEqual(len(self.acts), 2)

    def test_create_missing_or_unsettled_post_dialog_targets_never_commit(self):
        for publication in (listing(), dict(create_publication(), settled=False),
                            dict(create_publication(), resync_required=True),
                            dict(create_publication(), tree=dict(window='w3', rev=13, lines=[]))):
            with self.subTest(publication=publication):
                self.acts, self.creating = [], False
                self.commit_observations = [publication]
                result = thunar.create_folder(self.adapter, dict(name='new'))
                self.assertIn(result['status'], ('observe_required', 'listing_unavailable'))
                self.assertEqual(len(self.acts), 1)

    def test_collision_not_found_ambiguity_short_circuit_without_act(self):
        cases = [(('old', 'new'), thunar.rename, dict(old='old', new='new'), dict(status='collision')),
                 (('new',), thunar.create_folder, dict(name='new'), dict(status='collision')),
                 (('old', 'old'), thunar.rename, dict(old='old', new='new'), dict(status='ambiguous')),
                 (tuple('abcdef'), thunar.rename, dict(old='missing', new='new'),
                  dict(status='not_found', candidates=list('abcde')))]
        for names, run, args, expected in cases:
            with self.subTest(names=names):
                self.observations = [listing(names)]
                self.assertEqual(run(self.adapter, args), expected)
                self.assertEqual(self.acts, [])

    def test_rename_switches_icon_view_before_resolving_fresh_listing(self):
        for names, expected in ((('other', 'old'), dict(status='success', verified=False)),
                                (tuple('abcdef'), dict(status='not_found', candidates=list('abcde'))),
                                ((), dict(status='not_found', candidates=[]))):
            with self.subTest(names=names):
                icon = listing(())
                icon['tree']['lines'] = ['n1 frame "Files" @0,0,100,100']
                self.observations = [icon, listing(names, rev=12)]
                self.observes, self.acts = [], []
                self.adapter.rpc.call.reset_mock()
                self.assertEqual(thunar.rename(self.adapter, dict(old='old', new='new')), expected)
                calls = self.adapter.rpc.call.call_args_list
                sequence = ['window.observe', 'window.act', 'window.observe']
                if expected['status'] == 'success':
                    sequence.append('window.act')
                    self.assertEqual(self.acts[1]['steps'][0]['target'], dict(window='w1', ref='n6'))
                    self.assertEqual(self.acts[1]['steps'][0]['require'], dict(selected_ref='n7'))
                    self.assertEqual(self.acts[1]['base_rev'], 12)
                self.assertEqual([call.args[0] for call in calls], sequence)
                self.assertEqual(self.acts[0]['steps'], [dict(id='details', target='w1', timeout_ms=2000,
                    intent=dict(kind='key', keys=['Control_L', '2']),
                    expect=dict(kind='node_present', window='w1',
                                selector=dict(provider='atspi', role='tree_table')))])

    def test_view_switch_once_then_fresh_resolution_and_collision(self):
        icon = listing(())
        icon['tree']['lines'] = ['n1 frame "Files" @0,0,100,100']
        self.observations = [icon, listing(('new',))]
        self.assertEqual(thunar.create_folder(self.adapter, dict(name='new')), dict(status='collision'))
        self.assertEqual(len(self.acts), 1)
        self.assertEqual(self.acts[0]['steps'], [dict(id='details', target='w1', timeout_ms=2000,
            intent=dict(kind='key', keys=['Control_L', '2']), expect=dict(kind='node_present', window='w1',
            selector=dict(provider='atspi', role='tree_table')))])

    def test_switch_cannot_expose_listing_is_not_repeated(self):
        self.observations = [listing(())]
        self.observations[0]['tree']['lines'] = []
        self.assertEqual(thunar.create_folder(self.adapter, dict(name='new')), dict(status='listing_unavailable'))
        self.assertEqual(len(self.acts), 1)

    def test_empty_details_view_switches_once_then_allows_create(self):
        self.observations = [listing(())]
        self.mutate = lambda: (self.directory / 'home/new').mkdir(exist_ok=True)
        self.assertEqual(thunar.create_folder(self.adapter, dict(name='new')), dict(status='success', verified=True))
        self.assertEqual([s['steps'][0]['id'] for s in self.acts], ['details', '1', '3'])

    def test_dirty_initial_page_reobserves_then_runs_mutation(self):
        for run, args in ((thunar.rename, dict(old='old', new='new')),
                          (thunar.create_folder, dict(name='new'))):
            with self.subTest(skill=run.__name__):
                dirty = listing(('new',))  # Must not retain a stale collision.
                dirty['tree']['dirty'] = True
                self.observations = [dirty, listing(rev=12)]
                self.observes, self.acts = [], []
                self.assertEqual(run(self.adapter, args), dict(status='success', verified=False))
                self.assertEqual(len(self.observes), 4 if run is thunar.create_folder else 2)
                self.assertEqual(len(self.acts), 2 if run is thunar.create_folder else 1)
                for observe in self.observes[:2]:
                    self.assertIsNone(observe['since_rev'])
                    self.assertEqual(observe['scope'], dict(kind='session'))
                    self.assertEqual(observe['channels'], ['state', 'tree'])
                self.assertEqual(self.acts[0]['base_rev'], 12)

    def test_dirty_icon_page_reobserves_before_details_switch_and_mutation(self):
        icon = listing(())
        icon['tree']['lines'] = ['n1 frame "Files" @0,0,100,100']
        dirty = copy.deepcopy(icon)
        dirty['tree']['dirty'] = True
        self.observations = [dirty, icon, listing()]
        self.mutate = lambda: (self.directory / 'home/new').mkdir(exist_ok=True)
        self.assertEqual(thunar.create_folder(self.adapter, dict(name='new')),
                         dict(status='success', verified=True))
        self.assertEqual(len(self.observes), 5)
        self.assertEqual(len(self.acts), 3)
        self.assertEqual([s['steps'][0]['id'] for s in self.acts], ['details', '1', '3'])

    def test_dirty_fallback_uses_last_complete_consistent_set_and_skill_proceeds(self):
        for run, args in ((thunar.rename, dict(old='old', new='new')),
                          (thunar.create_folder, dict(name='new'))):
            with self.subTest(skill=run.__name__):
                self.creating, self.acts, self.observes = False, [], []
                self.observations = [listing(('new',), rev=i) for i in range(11, 15)] + [listing(rev=15)]
                for value in self.observations:
                    value['tree']['dirty'] = True
                self.assertEqual(run(self.adapter, args), dict(status='success', verified=False))
                self.assertEqual(self.acts[0]['base_rev'], 15)
                self.assertEqual(len(self.acts), 2 if run is thunar.create_folder else 1)
                self.assertEqual(len(self.observes), 7 if run is thunar.create_folder else 5)

    def test_dirty_fallback_zero_complete_sets_returns_tree_dirty_without_act(self):
        dirty = listing()
        dirty['tree'].update(dirty=True, truncated=True)
        self.observations = [dirty]
        self.assertEqual(thunar.create_folder(self.adapter, dict(name='new')),
                         dict(status='listing_unavailable', detail='tree_dirty'))
        self.assertEqual(len(self.observes), 5)
        self.assertEqual(self.acts, [])

    def test_dirty_fallback_retains_complete_set_when_later_attempts_are_incomplete(self):
        complete = listing(('last complete',))
        complete['tree']['dirty'] = True
        incomplete = listing(('incomplete',), rev=12)
        incomplete['tree'].update(dirty=True, truncated=True)
        self.observations = [complete] + [incomplete] * 4
        result, error = thunar._observe(self.adapter)
        self.assertIsNone(error)
        self.assertEqual(result, ('w1', thunar._listing(complete['tree']['lines'])))
        self.assertEqual(len(self.observes), 5)
        self.assertEqual(self.acts, [])

    def test_dirty_paged_fallback_assembles_complete_set_with_separate_tree_revision(self):
        first = listing(('old', 'new'))
        first['tree']['rev'] = 7  # Tree publication rev need not equal session rev.
        last = copy.deepcopy(first)
        last['tree']['lines'] = first['tree']['lines'][3:]
        first['tree'].update(lines=first['tree']['lines'][:3], dirty=True, cursor='next', truncated=True)
        self.observations = [first, last] * 5
        self.assertEqual(thunar.rename(self.adapter, dict(old='old', new='new')), dict(status='collision'))
        self.assertEqual(len(self.observes), 10)
        self.assertEqual(self.acts, [])

    def test_dirty_paging_restart_bound_without_complete_set_has_no_act(self):
        first = listing()
        first['tree'].update(cursor='next', truncated=True)
        dirty = listing()
        dirty['tree'].update(dirty=True, cursor='last', truncated=True)
        self.observations = [first, dirty] * 4
        self.assertEqual(thunar.rename(self.adapter, dict(old='old', new='new')),
                         dict(status='listing_unavailable', detail='tree_dirty'))
        self.assertEqual(len(self.observes), 8)
        self.assertEqual(self.acts, [])

    def test_clean_fifth_initial_page_can_run_mutation(self):
        dirty = listing()
        dirty['tree']['dirty'] = True
        self.observations = [dirty] * 4 + [listing()]
        self.assertEqual(thunar.rename(self.adapter, dict(old='old', new='new')),
                         dict(status='success', verified=False))
        self.assertEqual(len(self.observes), 5)
        self.assertEqual(len(self.acts), 1)

    def test_dirty_initial_page_other_failures_still_fail_fast(self):
        for change, expected in (
                (dict(settled=False), dict(status='observe_required')),
                (dict(resync_required=True), dict(status='observe_required')),
                (dict(state=listing(focus='w2')['state']), dict(status='inactive_pack')),
                (dict(state=dict(listing()['state'], modal=['dialog'])),
                 dict(status='blocked', detail='session modal is present')),
                (dict(tree=dict(listing()['tree'], window='w2', dirty=True)),
                 dict(status='listing_unavailable'))):
            with self.subTest(change=change):
                value = listing()
                value['tree']['dirty'] = True
                value.update(change)
                self.observations = [value, listing()]
                self.observes = []
                self.assertEqual(thunar.rename(self.adapter, dict(old='old', new='new')), expected)
                self.assertEqual(len(self.observes), 1)
                self.assertEqual(self.acts, [])

    def test_unknown_contradicted_and_failed_never_replay_or_run_oracle(self):
        for run, args in ((thunar.rename, dict(old='old', new='new')),
                          (thunar.create_folder, dict(name='new'))):
            for status in ('unknown', 'contradicted', 'failed'):
                with self.subTest(skill=run.__name__, status=status):
                    self.status, self.acts = status, []
                    with patch.object(thunar, '_oracle') as oracle:
                        self.assertEqual(run(self.adapter, args), dict(status=status))
                        oracle.assert_not_called()
                    self.assertEqual(len(self.acts), 1)
                    self.assertNotIn('pending', self.adapter.state)

    def test_uncertain_details_switch_stops_before_mutation(self):
        self.observations = [listing(())]
        self.status = 'unknown'
        self.assertEqual(thunar.create_folder(self.adapter, dict(name='new')), dict(status='unknown'))
        self.assertEqual(len(self.acts), 1)

    def test_oracles_require_both_rename_paths_and_respect_explicit_dir(self):
        (self.directory / 'new').touch()
        args = dict(old='old', new='new', dir=str(self.directory))
        self.assertEqual(thunar.rename(self.adapter, args), dict(status='success', verified=True))
        (self.directory / 'old').touch()
        self.assertEqual(thunar.rename(self.adapter, args), dict(status='success', verified=False))
        (self.directory / 'new').unlink()
        self.assertEqual(thunar.rename(self.adapter, args), dict(status='success', verified=False))
        self.assertEqual(thunar.create_folder(self.adapter, dict(name='new', dir=str(self.directory))),
                         dict(status='success', verified=False))
        (self.directory / 'new').mkdir()
        self.assertEqual(thunar.create_folder(self.adapter, dict(name='new', dir=str(self.directory))),
                         dict(status='success', verified=True))
        self.assertEqual(thunar.create_folder(self.adapter, dict(name='new')), dict(status='success', verified=False))

    def test_invalid_args_never_observe_or_act(self):
        for args in ({}, dict(name='../bad'), dict(name='.'), dict(name='bad\0'), dict(name=''),
                     dict(name=5), dict(name='new', dir=3), dict(name='new', risk='safe')):
            with self.subTest(args=args):
                self.assertEqual(thunar.create_folder(self.adapter, args), dict(status='invalid_arguments'))
        self.adapter.rpc.call.assert_not_called()

    def test_unsettled_wrong_focus_and_modal_never_act(self):
        for value, status in ((dict(listing(), settled=False), 'observe_required'),
                              (dict(listing(), resync_required=True), 'observe_required'),
                              (listing(focus='w2'), 'inactive_pack')):
            self.observations = [value]
            self.assertEqual(thunar.rename(self.adapter, dict(old='old', new='new'))['status'], status)
        self.observations = [listing()]
        self.observations[0]['state']['modal'] = ['dialog']
        self.assertEqual(thunar.rename(self.adapter, dict(old='old', new='new'))['status'], 'blocked')
        self.assertEqual(self.acts, [])

    def test_listing_parser_handles_escaped_names_and_ignores_metadata(self):
        value = listing(('a"\\\nb',))
        data = thunar._listing(value['tree']['lines'])
        self.assertEqual(data['items'], [dict(name='a"\\\nb', row='n3', ref='n4', container='n2')])

    def test_complete_tree_paging_resolves_split_row_and_collision(self):
        value = listing(('old', 'new'))
        more = copy.deepcopy(value)
        more['tree']['lines'] = value['tree']['lines'][3:]
        value['tree'].update(lines=value['tree']['lines'][:3], truncated=True, cursor='next')
        self.observations = [value, more]
        self.assertEqual(thunar.rename(self.adapter, dict(old='old', new='new')), dict(status='collision'))
        self.assertEqual(self.acts, [])
        call = self.adapter.rpc.call.call_args_list[-1]
        self.assertEqual(call.args[1]['scope'], dict(kind='window', window='w1'))
        self.assertEqual(call.args[1]['cursor'], 'next')

    def test_dirty_continuation_discards_entire_page_set_and_restarts(self):
        stale = listing(('new', 'stale'))
        middle, dirty = copy.deepcopy(stale), copy.deepcopy(stale)
        middle['tree'].update(lines=stale['tree']['lines'][3:6], cursor='last', truncated=True)
        dirty['tree'].update(lines=stale['tree']['lines'][6:], dirty=True)
        stale['tree'].update(lines=stale['tree']['lines'][:3], cursor='next', truncated=True)
        fresh = listing(rev=12)
        last = copy.deepcopy(fresh)
        last['tree']['lines'] = fresh['tree']['lines'][3:]
        fresh['tree'].update(lines=fresh['tree']['lines'][:3], cursor='next', truncated=True)
        self.observations = [stale, middle, dirty, fresh, last]
        self.assertEqual(thunar.rename(self.adapter, dict(old='old', new='new')),
                         dict(status='success', verified=False))
        self.assertEqual(len(self.observes), 5)
        self.assertEqual([p.get('cursor') for p in self.observes], [None, 'next', 'last', None, 'next'])
        for i in (0, 3):
            self.assertIsNone(self.observes[i]['since_rev'])
            self.assertEqual(self.observes[i]['scope'], dict(kind='session'))
        self.assertEqual(len(self.acts), 1)
        self.assertEqual(self.acts[0]['base_rev'], 12)

    def test_dirty_continuation_allows_only_three_restarts(self):
        for exhaust in (False, True):
            with self.subTest(exhaust=exhaust):
                first = listing()
                first['tree'].update(cursor='next', truncated=True)
                dirty = listing()
                dirty['tree'].update(dirty=True, lines=[])
                clean = listing()
                clean['tree']['lines'] = []
                self.observations = [first, dirty] * 3 + [first, dirty if exhaust else clean]
                self.observes, self.acts = [], []
                result = thunar.rename(self.adapter, dict(old='old', new='new'))
                expected = dict(status='success', verified=False)
                self.assertEqual(result, expected)
                self.assertEqual(len(self.observes), 8)
                self.assertEqual(sum('cursor' not in p for p in self.observes), 4)
                self.assertEqual(len(self.acts), 1)

    def test_dirty_continuation_other_state_changes_still_fail_fast(self):
        for section, field, value in ((None, 'settled', False), (None, 'resync_required', True),
                                      (None, 'rev', 12), ('tree', 'rev', 12),
                                      ('tree', 'window', 'w2'), ('state', 'focus', 'w2'),
                                      ('state', 'modal', ['dialog'])):
            with self.subTest(section=section, field=field):
                first, dirty = listing(), listing()
                first['tree'].update(cursor='next', truncated=True)
                dirty['tree']['dirty'] = True
                (dirty if section is None else dirty[section])[field] = value
                self.observations = [first, dirty, listing()]
                self.observes = []
                self.assertEqual(thunar.rename(self.adapter, dict(old='old', new='new')),
                                 dict(status='listing_unavailable', detail='state changed during tree paging'))
                self.assertEqual(len(self.observes), 2)
                self.assertEqual(self.acts, [])

    def test_changed_paged_state_is_rejected_before_act(self):
        value = listing()
        value['tree'].update(truncated=True, cursor='next')
        self.observations = [value, listing(rev=12)]
        result = thunar.rename(self.adapter, dict(old='old', new='new'))
        self.assertEqual(result['status'], 'listing_unavailable')
        self.assertEqual(self.acts, [])

    def test_incomplete_success_receipt_is_not_claimed_verified(self):
        with patch.object(thunar, '_shown', side_effect=[listing(), dict(status='success', steps=[])]):
            self.assertEqual(thunar.rename(self.adapter, dict(old='old', new='new')), dict(status='unverified'))

    def test_lost_response_recovers_original_action_id_without_replaying_mutation(self):
        original = self.adapter.rpc.call.side_effect
        saved = {}
        def rpc(method, params, **kwargs):
            if method == 'window.act':
                saved['id'] = params['action_id']
                saved['receipt'] = original(method, params, **kwargs)
                raise OSError('lost response')
            if method == 'action.get':
                self.assertEqual(params['action_id'], saved['id'])
                return dict(status='final', receipt=saved['receipt'])
            return original(method, params, **kwargs)
        self.adapter.rpc.call.side_effect = rpc
        self.mutate = lambda: (self.directory / 'home/new').mkdir()
        result = thunar.create_folder(self.adapter, dict(name='new'))
        self.assertEqual(result, dict(status='success', verified=True))
        self.assertEqual(len(self.acts), 2)
        self.assertNotIn('pending', self.adapter.state)

    def test_prior_interrupted_skill_resolves_original_id_without_new_mutation(self):
        self.adapter.state['pending'] = dict(action_id='a-original', generation='g-original')
        original = self.adapter.rpc.call.side_effect
        def rpc(method, params, **kwargs):
            if method == 'action.get':
                self.assertEqual(params['action_id'], 'a-original')
                return dict(status='final', receipt=receipt())
            return original(method, params, **kwargs)
        self.adapter.rpc.call.side_effect = rpc
        result = shown(self.adapter.call('use_skill', dict(name='thunar.rename', args=dict(old='old', new='new'))))
        self.assertEqual(result, dict(status='success', detail='previous_action_resolved; skill not run'))
        self.assertEqual(self.acts, [])
        self.assertNotIn('pending', self.adapter.state)


class EntryPointTests(unittest.TestCase):
    def test_one_shot_and_mcp_use_same_skill_dispatch_and_compact_rendering(self):
        for mode in ('call', 'mcp'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(prefix='thunar-entry-') as temp:
                directory = Path(temp)
                (directory / 'home').mkdir()
                (directory / 'home/old').touch()
                worker = FakeWorker(directory)
                actions = []
                original = a.WorkerRPC._call
                def rpc(rpc_self, method, params, timeout=12):
                    value = original(rpc_self, method, params, timeout)
                    if method == 'window.observe':
                        if actions and actions[-1]['steps'][0]['id'] in ('1', '2') and actions[-1]['steps'][0]['intent'].get('keys') != ['F2']:
                            return create_publication('main' if params.get('scope', {}).get('window') == 'w1' else 'dialog', rev=value['rev'])
                        return listing(rev=value['rev'])
                    if method == 'window.act':
                        actions.append(params)
                        if params['steps'][0]['intent'].get('keys') == ['F2']:
                            (directory / 'home/old').rename(directory / 'home/new')
                        elif params['steps'][0]['intent']['kind'] == 'invoke':
                            (directory / 'home/Reports').mkdir()
                        value['steps'] = verified(params['steps'])['steps']
                    return value
                calls = [('use_skill', dict(name='thunar.rename', args=dict(old='old', new='new'))),
                         ('use_skill', dict(name='thunar.create_folder', args=dict(name='Reports')))]
                def invoke(argv, incoming=''):
                    with patch.object(sys, 'argv', argv), patch('sys.stdin', io.StringIO(incoming)), \
                            patch('sys.stdout', new_callable=io.StringIO) as output:
                        self.assertEqual(a.main(), 0)
                    return [json.loads(line) for line in output.getvalue().splitlines()]
                try:
                    with patch.object(a, 'session_dir', return_value=directory), \
                            patch.object(a.WorkerRPC, 'connect', new=lambda r: worker.connect(r)), \
                            patch.object(a.WorkerRPC, '_call', new=rpc):
                        if mode == 'call':
                            results = [invoke(['ark-act', 'call', name, json.dumps(args), '--session', 's1'])[0]
                                       for name, args in calls]
                            self.assertTrue(all(r['ok'] for r in results))
                            self.assertTrue((directory / 'driver/adapter-state.json').exists())
                        else:
                            incoming = ''.join(json.dumps(dict(jsonrpc='2.0', id=i, method='tools/call',
                                params=dict(name=n, arguments=args)))+'\n' for i, (n, args) in enumerate(calls, 1))
                            results = [r['result'] for r in invoke(['ark-act', 'mcp', '--session', 's1'], incoming)]
                    self.assertEqual([shown(r) for r in results], [dict(status='success', verified=True)] * 2)
                    self.assertEqual(len(actions), 3)
                    self.assertEqual(worker.errors, [])
                finally:
                    worker.close()


if __name__ == '__main__':
    unittest.main()
