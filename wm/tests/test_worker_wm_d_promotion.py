"""Headless promotion transactions. No display, app, or clipboard is opened."""
import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_worker_wm_b_controller as wm_b
from wm_controller import Controller, Refused, sessiond
from wm_promotion import Promotion, PromotionError, Backend, copy_tree
from promotion_recipes import RECIPES, visible
from wm_registry import atomic_json, by_incarnation
from test_worker_w2 import Harness


def unmatched_tree(handle):
    # Raw promotion.observe tree when AT-SPI has not matched the window.
    return dict(key='unmatched:' + handle, role='frame', showing=False,
                visible=False, status='unmatched', rendered_only=True)


class World:
    """Independent external state survives controller reconstruction."""
    def __init__(self, case):
        self.case = case
        self.windows = {}
        self.trees = {}
        self.clipboards = {}
        self.events = []
        self.fail = None
        self.c = case.c
        self.workers = {case.root}

    def observe(self, record):
        return self.settled(record)

    def settled(self, record):
        incarnation = record['incarnation']
        if incarnation not in self.workers:
            raise PromotionError('child_worker_missing')
        if incarnation != self.case.root and self.fail == 'verify':
            raise PromotionError('verify_failed')
        return dict(incarnation=incarnation, settled=True,
                    windows=copy.deepcopy(self.windows.get(incarnation, [])),
                    trees=copy.deepcopy(self.trees.get(incarnation, {})))

    def clipboard_read(self, record):
        self.events.append(('clipboard_read', record['incarnation']))
        return self.clipboards.get(record['incarnation'], '')

    def start_worker(self, record):
        self.events.append(('worker', record['incarnation']))
        self.workers.add(record['incarnation'])

    def clipboard_set(self, record, text):
        self.events.append(('clipboard_set', record['incarnation']))
        self.clipboards[record['incarnation']] = text

    def reopen(self, record, manifest, root):
        self.events.append(('reopen', record['incarnation']))
        if self.fail == 'reopen':
            raise PromotionError('reopen_failed')
        # Model the launched app's native WM_CLASS independently of app_class.
        app_class = {'gnome-text-editor': 'gnome-text-editor',
                     'thunar': 'Thunar'}[manifest['launch_argv'][0]]
        document = root / manifest['document_paths'][0]
        title = str(document)
        if app_class == 'gnome-text-editor':
            parent = document.parent.relative_to(record['home'])
            title = f'{document.name} (~/{parent}) - Text Editor'
        self.windows[record['incarnation']] = [dict(handle='child-window', xid=20, mapped=True,
                    title=title, **{'class': app_class})]
        self.trees[record['incarnation']] = {'child-window': unmatched_tree('child-window')}

    def retire(self, record, window):
        self.events.append(('retire', record['incarnation']))
        if self.fail == 'retire':
            raise PromotionError('retire_failed')
        self.windows[record['incarnation']].remove(window)


class PromotionCase(unittest.TestCase):
    records = wm_b.ControllerCase.records
    record = wm_b.ControllerCase.record
    capacity = wm_b.ControllerCase.capacity
    page = wm_b.ControllerCase.page
    stop_fakes = wm_b.ControllerCase.stop_fakes

    def setUp(self):
        wm_b.ControllerCase.setUp(self)
        self.world = World(self)
        self.c.promotion_backend = self.world
        self.home = Path(self.record(self.root)['home'])
        (self.home / 'docs').mkdir()
        self.document = self.home / 'docs/note.txt'
        self.document.write_text('saved contents\n')
        self.window = dict(handle='source-window', xid=10, mapped=True, family='pid:123',
                           title='note.txt (~/docs) - Text Editor', **{'class': 'gnome-text-editor'})
        self.world.trees[self.root] = {'source-window': unmatched_tree('source-window')}
        self.world.windows[self.root] = [self.window, dict(handle='unrelated-window', xid=11, mapped=True,
                                                         title='Other App', **{'class': 'Other'})]
        self.world.clipboards[self.root] = 'selected text'
        self.reap = patch.object(sessiond, 'marked_pids', return_value=[]).start()
        patch.object(sessiond.time, 'sleep').start()
        patch.object(sessiond, 'alive', return_value=False).start()

    def params(self, **extra):
        return dict(source_incarnation=self.root, window='source-window', app_class=self.window['class'],
                    request_id='promote-1', capability=self.c.capability(self.root), **extra)

    def promote(self, **extra):
        return self.c.dispatch('promote', self.params(**extra))

    def restart(self):
        self.c = Controller(self.state)
        self.c.kill_file = self.state / 'kill'
        self.world.c = self.c
        self.c.promotion_backend = self.world
        self.c.recover()

    def source_running(self):
        return any(w['handle'] == 'source-window' for w in self.world.windows[self.root])

    def assert_failed(self, result, reason):
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], reason)
        self.assertTrue(self.source_running())
        self.assertEqual(len(self.records()), 1)
        self.assertNotIn(('retire', self.root), self.world.events)
        self.assertFalse((Path(self.record(self.root)['session_dir']) / 'promotion-gate.json').exists())

    def crash_at(self, phase):
        self.c.crash = lambda at: (_ for _ in ()).throw(SystemExit(at)) if at == phase else None
        with self.assertRaises(SystemExit):
            self.promote()

    def test_success_bundle_transfer_lineage_and_verify_before_retire(self):
        verify = Promotion.verified
        events = self.world.events
        def checked(operation, *args):
            result = verify(operation, *args)
            events.append(('verified', args[0]['incarnation']))
            return result
        with patch.object(Promotion, 'verified', checked):
            result = self.promote()
        self.assertTrue(result['ok'], result)
        manifest = json.loads(Path(result['checkpoint']).read_text())
        self.assertEqual(manifest['promotion_id'], result['promotion_id'])
        self.assertEqual(manifest['app_class'], 'gnome-text-editor')
        self.assertEqual(manifest['launch_argv'], ['gnome-text-editor', 'documents/note.txt'])
        self.assertEqual(manifest['cwd'], '.')
        self.assertEqual(manifest['document_paths'], ['documents/note.txt'])
        self.assertNotIn('clipboard_payload', manifest)
        self.assertFalse(self.source_running())
        self.assertEqual(self.world.windows[self.root][0]['handle'], 'unrelated-window')
        self.assertIsNotNone(self.record(self.root))
        child = self.record(result['child_incarnation'])
        self.assertEqual(child['parent_incarnation'], self.root)
        self.assertEqual(child['promotion_lineage']['promotion_id'], result['promotion_id'])
        self.assertEqual(self.record(self.root)['budget']['debited']['acts'], 100)
        installed = Path(child['home']) / 'Promotions' / result['promotion_id'] / manifest['document_paths'][0]
        self.assertEqual(installed.read_text(), self.document.read_text())
        self.assertEqual(installed.stat().st_mode & 0o777, 0o600)
        self.assertLess([e[0] for e in events].index('verified'), [e[0] for e in events].index('retire'))
        self.assertEqual(self.promote(), result)
        self.assertEqual([e[0] for e in events].count('retire'), 1)

    def test_desktop_file_id_is_not_a_recipe_alias(self):
        params = dict(self.params(), app_class='org.gnome.TextEditor')
        before = self.records()
        with self.assertRaisesRegex(Refused, 'no_recipe'):
            self.c.dispatch('promote', params)
        self.assertEqual(self.records(), before)
        self.assertEqual(self.c.log.rows, [])
        self.assertFalse((self.state / 'exchange').exists())
        self.assertFalse((Path(self.record(self.root)['session_dir']) / 'promotion-gate.json').exists())
        self.assertTrue(self.source_running())
        self.assertEqual(self.world.events, [])

    def test_both_recipes_verify_exact_native_class_and_document(self):
        for app_class, wrong_class in [('gnome-text-editor', 'org.gnome.TextEditor'),
                                       ('Thunar', 'thunar')]:
            with self.subTest(app_class=app_class):
                recipe = RECIPES[app_class]
                window = dict(self.window, **{'class': app_class})
                window['title'] = str(self.document)
                self.assertTrue(recipe.verify(window, None, str(self.document)))
                self.assertFalse(recipe.verify(dict(window, **{'class': wrong_class}),
                                               None, str(self.document)))
                self.assertFalse(recipe.verify(dict(window, title='different.txt'),
                                               None, str(self.document)))

    def test_unsaved_and_no_recipe_leave_no_trace(self):
        for title in ('*notes.txt', 'notes.txt*', '• notes.txt', '● notes.txt', 'Untitled',
                      'New Document', 'Unsaved Document - Text Editor', 'notes.txt (modified)'):
            self.window['title'] = title
            before = self.records()
            with self.assertRaisesRegex(Refused, 'unsaved_state'):
                self.promote()
            self.assertEqual(self.records(), before)
            self.assertEqual(self.c.log.rows, [])
            self.assertFalse((self.state / 'exchange').exists())
            self.assertFalse((Path(self.record(self.root)['session_dir']) / 'promotion-gate.json').exists())
            self.assertTrue(self.source_running())
        self.window['class'] = 'Unknown'
        with self.assertRaisesRegex(Refused, 'no_recipe'):
            self.promote()
        self.assertEqual(self.c.log.rows, [])

    def test_clipboard_selected_is_set_before_reopen(self):
        result = self.promote(transfer_clipboard=True)
        self.assertTrue(result['ok'], result)
        child = result['child_incarnation']
        self.assertEqual(self.world.clipboards[child], 'selected text')
        names = [e[0] for e in self.world.events]
        self.assertLess(names.index('clipboard_set'), names.index('reopen'))
        self.assertEqual(json.loads(Path(result['checkpoint']).read_text())['clipboard_payload'], 'selected text')

    def test_clipboard_unselected_neither_reads_nor_touches_child(self):
        result = self.promote()
        self.assertTrue(result['ok'])
        self.assertFalse(any('clipboard' in e[0] for e in self.world.events))
        self.assertNotIn(result['child_incarnation'], self.world.clipboards)

    def test_export_failure_never_spawns_or_retires(self):
        with patch.object(Promotion, 'export', side_effect=PromotionError('export_failed')):
            self.assert_failed(self.promote(), 'export_failed')
        self.assertEqual(self.launch.call_count, 0)

    def test_spawn_failure_keeps_good_checkpoint(self):
        self.launch.side_effect = lambda *args: ({}, 1)
        self.assert_failed(self.promote(), 'creation_failed')
        self.assertEqual(len(list((self.state / 'exchange').rglob('manifest.json'))), 1)

    def test_reopen_and_verify_failures_reap_child_keep_source_checkpoint(self):
        self.world.fail = 'reopen'
        self.assert_failed(self.promote(), 'reopen_failed')
        checkpoint = next((self.state / 'exchange').rglob('manifest.json'))
        self.assertEqual((checkpoint.parent / 'documents/note.txt').read_text(), 'saved contents\n')
        self.assertTrue(list((self.state / 'retired').glob('*.json')))
        self.world.fail = 'verify'
        self.assert_failed(self.c.dispatch('promote', dict(self.params(), request_id='verify-failure')), 'verify_failed')

    def test_thunar_snapshots_and_reopens_directory(self):
        directory = self.home / 'Projects'
        directory.mkdir()
        (directory / 'example.txt').write_text('project')
        self.window.update(title=str(directory), **{'class': 'Thunar'})
        result = self.promote()
        self.assertTrue(result['ok'], result)
        manifest = json.loads(Path(result['checkpoint']).read_text())
        self.assertEqual(manifest['launch_argv'], ['thunar', 'documents/Projects'])
        self.assertEqual((Path(result['checkpoint']).parent / 'documents/Projects/example.txt').read_text(), 'project')

    def test_human_control_refuses_before_trace(self):
        self.page(self.root, 2)
        with self.assertRaisesRegex(Refused, 'parent_human'):
            self.promote()
        self.assertEqual(self.c.intents, {})
        self.assertTrue(self.source_running())

    def test_admission_and_budget_caps_use_normal_spawn(self):
        self.capacity(1)
        self.assert_failed(self.promote(), 'admission_limit')
        self.capacity(10)
        self.assert_failed(self.c.dispatch('promote', dict(self.params(reservation=dict(acts=10001, tokens=0)),
                                                         request_id='over-budget')), 'budget_exhausted')

    def test_depth_and_fanout_caps_use_normal_spawn(self):
        with sessiond.locked_registry() as records:
            record = by_incarnation(records, self.root)
            record['depth'] = 3
            sessiond.save_registry(records)
        self.assert_failed(self.promote(), 'depth_limit')
        with sessiond.locked_registry() as records:
            record = by_incarnation(records, self.root)
            record['depth'] = 0
            for i in range(4):
                child = sessiond.allocate_session('xvfb', records)
                child['parent_incarnation'] = self.root
            sessiond.save_registry(records)
        result = self.c.dispatch('promote', dict(self.params(), request_id='fanout'))
        self.assertEqual(result['reason'], 'fanout_limit')
        self.assertTrue(self.source_running())

    def test_restart_after_export_never_reexports_checkpoint(self):
        self.crash_at('promotion.export.effect')
        bundle = next((self.state / 'exchange').rglob('manifest.json')).parent
        old = (bundle / 'documents/note.txt').read_bytes()
        self.document.write_text('later file change')
        with patch.object(Promotion, 'export', side_effect=AssertionError('must reuse checkpoint')):
            self.restart()
        result = self.c.completed['promote-1']
        self.assertTrue(result['ok'], result)
        self.assertEqual((bundle / 'documents/note.txt').read_bytes(), old)
        self.assertEqual([e[0] for e in self.world.events].count('retire'), 1)

    def test_restart_after_reopen_reconciles_without_second_launch(self):
        self.crash_at('promotion.reopen.effect')
        self.restart()
        self.assertTrue(self.c.completed['promote-1']['ok'])
        self.assertEqual([e[0] for e in self.world.events].count('reopen'), 1)

    def test_uncertain_reopen_without_observed_window_fails_without_replay(self):
        self.crash_at('promotion.reopen.armed')
        with patch.object(Promotion, 'verified', side_effect=PromotionError('verify_failed')):
            self.restart()
        self.assert_failed(self.c.completed['promote-1'], 'verify_failed')
        self.assertNotIn('reopen', [e[0] for e in self.world.events])

    def test_restart_after_retire_dispatch_does_not_double_retire(self):
        self.crash_at('promotion.retire.dispatched')
        self.restart()
        self.assertTrue(self.c.completed['promote-1']['ok'])
        self.assertEqual([e[0] for e in self.world.events].count('retire'), 1)
        self.assertEqual(len(self.records()), 2)

    def test_source_worker_restart_is_not_mistaken_for_retirement(self):
        self.crash_at('promotion.verify.done')
        self.window['handle'] = 'new-generation/window'
        self.restart()
        result = self.c.completed['promote-1']
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'source_identity_changed')
        self.assertNotIn(('retire', self.root), self.world.events)
        self.assertEqual(len(self.records()), 1)
        self.assertTrue(any(w['xid'] == 10 for w in self.world.windows[self.root]))

    def test_uncertain_retire_with_source_present_never_resends_close(self):
        self.crash_at('promotion.retire.armed')
        self.restart()
        self.assert_failed(self.c.completed['promote-1'], 'retire_uncertain')

    def test_restart_after_spawn_commit_reuses_child_and_budget(self):
        self.crash_at('created')
        self.restart()
        self.assertTrue(self.c.completed['promote-1']['ok'])
        self.assertEqual(self.launch.call_count, 1)
        self.assertEqual(self.record(self.root)['budget']['debited']['acts'], 100)

    def test_failure_cleanup_retries_reap_before_ungating(self):
        self.world.fail = 'reopen'
        with patch.object(sessiond, 'stop_sessions', side_effect=OSError('reap interrupted')):
            with self.assertRaises(OSError):
                self.promote()
        self.assertTrue((Path(self.record(self.root)['session_dir']) / 'promotion-gate.json').exists())
        self.restart()
        self.assert_failed(self.c.completed['promote-1'], 'reopen_failed')

    def test_source_change_before_retirement_keeps_both_original_apps(self):
        original = self.world.reopen
        def reopen(*args):
            original(*args)
            self.window['title'] = '*notes.txt'
        self.world.reopen = reopen
        self.assert_failed(self.promote(), 'source_changed')

    def test_checkpoint_hash_oracle_rejects_corruption(self):
        self.crash_at('promotion.export.effect')
        bundle = next((self.state / 'exchange').rglob('manifest.json')).parent
        (bundle / 'documents/note.txt').write_text('corrupt')
        self.restart()
        self.assert_failed(self.c.completed['promote-1'], 'invalid_checkpoint')

    def test_good_bundle_is_immutable_when_export_is_called_again(self):
        self.crash_at('promotion.export.effect')
        intent = self.c.intents['promote-1']
        operation = Promotion(self.c, intent)
        manifest = operation.checkpoint()
        self.document.write_text('new contents')
        with patch.object(self.world, 'clipboard_read', side_effect=AssertionError('no export')):
            self.assertEqual(operation.export(None), manifest)
        self.assertEqual((operation.bundle / 'documents/note.txt').read_text(), 'saved contents\n')

    def test_transfer_failure_reaps_and_leaves_source(self):
        with patch.object(Promotion, 'install', side_effect=PromotionError('transfer_failed')):
            self.assert_failed(self.promote(), 'transfer_failed')

    def test_gate_is_durable_before_export_and_released_after_verify(self):
        original = Promotion.export
        def checked(operation, observation):
            gate = Path(self.record(self.root)['session_dir']) / 'promotion-gate.json'
            self.assertEqual(json.loads(gate.read_text())['promotion_id'], operation.id)
            self.assertEqual(operation.steps['export']['status'], 'armed')
            return original(operation, observation)
        with patch.object(Promotion, 'export', checked):
            self.assertTrue(self.promote()['ok'])

    def test_restart_after_clipboard_effect_reconciles_without_setting_twice(self):
        self.c.crash = lambda at: (_ for _ in ()).throw(SystemExit()) if at == 'promotion.clipboard.effect' else None
        with self.assertRaises(SystemExit):
            self.promote(transfer_clipboard=True)
        self.restart()
        self.assertTrue(self.c.completed['promote-1']['ok'])
        self.assertEqual([e[0] for e in self.world.events].count('clipboard_set'), 1)

    def test_armed_clipboard_with_unknown_effect_never_sets_on_recovery(self):
        self.c.crash = lambda at: (_ for _ in ()).throw(SystemExit()) if at == 'promotion.clipboard.armed' else None
        with self.assertRaises(SystemExit):
            self.promote(transfer_clipboard=True)
        self.restart()
        self.assert_failed(self.c.completed['promote-1'], 'clipboard_uncertain')
        self.assertNotIn('clipboard_set', [e[0] for e in self.world.events])

    def test_human_change_before_spawn_is_refused_by_spawn_path(self):
        original = Promotion.spawn
        def human(operation):
            self.page(self.root, 2)
            return original(operation)
        with patch.object(Promotion, 'spawn', human):
            self.assert_failed(self.promote(), 'parent_human')
        self.assertEqual(self.launch.call_count, 0)

    def test_recipe_path_from_tree_and_parent_escape_refusal(self):
        recipe = RECIPES[self.window['class']]
        title_only = dict(self.window, title='note.txt — Text Editor')
        self.assertEqual(recipe.export(title_only, {'uri': self.document.as_uri()}, self.home), str(self.document))
        outside = self.home / '..' / 'outside.txt'
        outside.write_text('private')
        with self.assertRaisesRegex(PromotionError, 'document_outside_home'):
            recipe.export(dict(self.window, title=str(outside)), None, self.home)

    def test_editor_title_paths_with_unmatched_or_absent_tree(self):
        recipe = RECIPES['gnome-text-editor']
        for title in (self.window['title'],
                      f'note.txt ({self.document.parent}) - Text Editor',
                      str(self.document), self.document.as_uri(),
                      f'note.txt — {self.document.parent} — Text Editor',
                      f'note.txt – {self.document.parent} – Text Editor'):
            for tree in (None, unmatched_tree('source-window')):
                with self.subTest(title=title, tree=tree):
                    window = dict(self.window, title=title)
                    self.assertEqual(recipe.export(window, tree, self.home), str(self.document))
                    self.assertTrue(recipe.verify(window, tree, str(self.document), self.home))

    def test_editor_promotion_with_full_tree(self):
        self.world.trees[self.root]['source-window'] = dict(
            key='editor:frame', role='frame', name=self.window['title'],
            showing=True, visible=True, children=[
                dict(key='editor:document', role='document', name=self.document.name,
                     uri=self.document.as_uri(), children=[
                         dict(key='editor:text', role='text', text=self.document.read_text())])])
        result = self.promote()
        self.assertTrue(result['ok'], result)
        self.assertFalse(self.source_running())
        self.assertEqual((Path(result['checkpoint']).parent / 'documents/note.txt').read_text(),
                         self.document.read_text())

    def test_parenthesized_title_requires_matching_name_and_directory(self):
        recipe = RECIPES['gnome-text-editor']
        other = self.home / 'other/note.txt'
        other.parent.mkdir()
        other.write_text('another document')
        for title in ('different.txt (~/docs) - Text Editor',
                      'note.txt (~/other) - Text Editor',
                      f'note.txt ({other.parent}) - Text Editor',
                      'note.txt (~/missing) - Text Editor'):
            with self.subTest(title=title):
                window = dict(self.window, title=title)
                self.assertFalse(visible(window, None, str(self.document), self.home))
                self.assertFalse(recipe.verify(window, unmatched_tree('source-window'),
                                               str(self.document), self.home))
        # A same-name URI from another directory cannot override the title.
        self.assertEqual(recipe.export(self.window, {'uri': other.as_uri()}, self.home),
                         str(self.document))
        with self.assertRaisesRegex(PromotionError, 'document_path_unavailable'):
            recipe.export(dict(self.window, title='note.txt (~/missing) - Text Editor'),
                          {'uri': self.document.as_uri()}, self.home)

    def test_parenthesized_title_preserves_file_and_home_guards(self):
        recipe = RECIPES['gnome-text-editor']
        outside = self.home.parent / 'outside.txt'
        outside.write_text('private')
        for title, reason in (
                ('missing.txt (~/docs) - Text Editor', 'document_path_unavailable'),
                ('docs (~) - Text Editor', 'document_path_unavailable'),
                ('note.txt (docs) - Text Editor', 'document_path_unavailable'),
                ('note.txt (~/docs/../docs) - Text Editor', 'document_outside_home'),
                (f'outside.txt ({outside.parent}) - Text Editor', 'document_outside_home')):
            with self.subTest(title=title):
                with self.assertRaisesRegex(PromotionError, reason):
                    recipe.export(dict(self.window, title=title),
                                  unmatched_tree('source-window'), self.home)

    def test_parenthesized_title_keeps_ambiguous_paths_refused(self):
        other = self.home / 'other.txt'
        other.write_text('another document')
        with self.assertRaisesRegex(PromotionError, 'document_path_unavailable'):
            RECIPES['gnome-text-editor'].export(dict(self.window, description=str(other)),
                                               unmatched_tree('source-window'), self.home)

    def test_recipe_refuses_tree_path_that_names_a_different_document(self):
        recipe = RECIPES[self.window['class']]
        with self.assertRaisesRegex(PromotionError, 'document_path_unavailable'):
            recipe.export(dict(self.window, title='different.txt — Text Editor'),
                          {'uri': self.document.as_uri()}, self.home)

    def test_unsaved_detection_uses_indicators_not_contents_or_columns(self):
        editor = RECIPES[self.window['class']]
        tree = dict(role='text', text='The file describes unsaved and modified buffers.')
        self.assertEqual(editor.export(self.window, tree, self.home), str(self.document))
        with self.assertRaisesRegex(PromotionError, 'unsaved_state'):
            editor.export(self.window, dict(role='document', states=['modified']), self.home)
        thunar = RECIPES['Thunar']
        self.assertEqual(thunar.export(dict(title=str(self.home)),
                         dict(role='table_column_header', name='Date Modified'), self.home), str(self.home))

    def test_snapshot_refuses_symlinks_and_special_files(self):
        self.document.unlink()
        self.document.symlink_to(self.home / 'missing')
        with self.assertRaises(OSError):
            copy_tree(self.document, self.state / 'target', self.c.cfg)

    def test_snapshot_refuses_destination_symlink_ancestors(self):
        (self.state / 'target-dir').mkdir()
        (self.state / 'target-link').symlink_to(self.state / 'target-dir')
        with self.assertRaises(OSError):
            copy_tree(self.document, self.state / 'target-link/copied', self.c.cfg)
        self.assertEqual(list((self.state / 'target-dir').iterdir()), [])

    def test_backend_never_opens_host_display(self):
        record = dict(self.record(self.root), display=0)
        with patch('wm_promotion.subprocess.run') as run, patch('wm_promotion.subprocess.Popen') as popen:
            with self.assertRaisesRegex(PromotionError, 'unsafe_display'):
                Backend(self.c).run(record, ['xclip'])
            with self.assertRaisesRegex(PromotionError, 'unsafe_display'):
                Backend(self.c).reopen(record, {}, Path('.'))
        run.assert_not_called()
        popen.assert_not_called()


class WorkerPromotionGate(Harness):
    def test_promotion_gate_prevents_input_and_survives_worker_reconstruction(self):
        # Exercise the actual mutation gate with an ordinary worker fixture.
        (self.w.sd / 'promotion-gate.json').write_text('{"promotion_id":"test", "window":"source"}')
        with self.assertRaisesRegex(Exception, 'preempted'):
            self.e.promotion_gate('source')

    def test_promotion_gate_leaves_other_apps_actionable(self):
        (self.w.sd / 'promotion-gate.json').write_text('{"promotion_id":"test", "window":"source"}')
        self.e.promotion_gate('other-app')

    def test_source_action_is_preempted_before_input(self):
        atomic_json(self.w.sd / 'promotion-gate.json', dict(promotion_id='test', window=self.handle))
        result = self.run_action()
        self.assertEqual(result['reason'], 'preempted')
        self.assertFalse(self.t.log)

    def test_other_app_action_still_dispatches(self):
        atomic_json(self.w.sd / 'promotion-gate.json', dict(promotion_id='test', window='other-source'))
        result = self.run_action()
        self.assertTrue(self.t.log)
        self.assertEqual(result['steps'][0]['effect']['status'], 'verified')

    def test_source_family_dialog_is_gated(self):
        self.later()
        atomic_json(self.w.sd / 'promotion-gate.json', dict(promotion_id='test', window=self.handle))
        with self.assertRaisesRegex(Exception, 'preempted'):
            self.e.promotion_gate(self.w.cache.handle(11))

    def test_gate_survives_worker_generation_change(self):
        atomic_json(self.w.sd / 'promotion-gate.json',
                    dict(promotion_id='test', window='old-generation/window', family='x:10', xid=10))
        with self.assertRaisesRegex(Exception, 'preempted'):
            self.e.promotion_gate(self.handle)


def crash_boundary(phase, success):
    def check(self):
        self.crash_at(phase)
        self.restart()
        result = self.c.completed['promote-1']
        self.assertEqual(result['ok'], success, result)
        names = [e[0] for e in self.world.events]
        self.assertLessEqual(names.count('reopen'), 1)
        self.assertLessEqual(names.count('retire'), 1)
        self.assertEqual(self.source_running(), not success)
        self.assertEqual(len(self.records()), 2 if success else 1)
        self.assertFalse((Path(self.record(self.root)['session_dir']) / 'promotion-gate.json').exists())
    return check


# Exercise every durable transition around pure/idempotent operations and the
# once-only external actions. An export armed without a complete manifest is
# deliberately failed; recovery never guesses that it may safely export again.
for _phase, _success in [
        ('intent', True), ('promotion.quiesce.armed', True),
        ('promotion.quiesce.effect', True), ('promotion.quiesce.done', True),
        ('promotion.export.armed', False), ('promotion.export.done', True),
        ('promotion.spawn.armed', True), ('promotion.spawn.effect', True),
        ('promotion.spawn.done', True), ('promotion.worker.armed', False),
        ('promotion.worker.effect', True), ('promotion.worker.done', True),
        ('promotion.transfer.armed', True), ('promotion.transfer.effect', True),
        ('promotion.transfer.done', True), ('promotion.reopen.done', True),
        ('promotion.verify.armed', True), ('promotion.verify.effect', True),
        ('promotion.verify.done', True), ('promotion.retire.effect', True),
        ('promotion.retire.done', True), ('promotion.ungate.armed', True),
        ('promotion.ungate.effect', True), ('promotion.ungate.done', True),
        ('complete', True)]:
    setattr(PromotionCase, 'test_crash_boundary_' + _phase.replace('.', '_'), crash_boundary(_phase, _success))


if __name__ == '__main__':
    unittest.main()
