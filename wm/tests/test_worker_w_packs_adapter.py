"""Pre-pack adapter regressions. Fake worker/provider only; no live gates."""
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from test_worker_w4 import ProviderHarness, node
from test_worker_w5 import a, loop, shown, receipt, GEN, FakeWorker
from test_worker_w5_fix1 import adapter_with_rpc, add_result


class SessionDirectories(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='packs-session-dir-')
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.root = self.home / '.local/state/ark-sessiond'
        self.root.mkdir(parents=True)
        self.registry = self.root / 'sessions.json'
        self.legacy = self.root / 'run/s1'
        self.instance = self.legacy / 'instance-1'
        account = patch.object(a.pwd, 'getpwuid', return_value=NS(pw_dir=str(self.home)))
        account.start()
        self.addCleanup(account.stop)

    def register(self):
        self.registry.write_text(json.dumps({'s1': {'session_dir': str(self.instance)}}))

    def test_registry_resolves_instance_directory_without_inherited_home(self):
        self.register()
        (self.instance / 'driver').mkdir(parents=True)
        (self.instance / 'driver/act.sock').touch()  # Fake socket location only.
        with patch.dict(os.environ, HOME=str(self.home / 'inherited-home')):
            self.assertEqual(a.session_dir('s1'), self.instance)
        self.assertEqual(a.Adapter(a.session_dir('s1')).rpc.path,
                         self.instance / 'driver/act.sock')

    def test_missing_registry_uses_legacy_directory(self):
        self.assertEqual(a.session_dir('s1'), self.legacy)

    def test_missing_record_or_field_uses_legacy_directory(self):
        for registry in ({}, {'s2': {'session_dir': str(self.instance)}}, {'s1': {}},
                         {'s1': {'session_dir': None}}, {'s1': {'session_dir': ''}}):
            with self.subTest(registry=registry):
                self.registry.write_text(json.dumps(registry))
                self.assertEqual(a.session_dir('s1'), self.legacy)

    def test_unreadable_or_invalid_registry_uses_legacy_directory(self):
        with patch.object(Path, 'read_text', side_effect=PermissionError):
            self.assertEqual(a.session_dir('s1'), self.legacy)
        for text in ('{', '[]', 'null', '{"s1": []}', '{"s1": {"session_dir": 42}}'):
            with self.subTest(text=text):
                self.registry.write_text(text)
                self.assertEqual(a.session_dir('s1'), self.legacy)

    def test_invalid_session_ids_still_fail_before_registry_read(self):
        with patch.object(Path, 'read_text') as read:
            for session in ('', 's0', 's01', 's-1', '../s1', 's1/instance-1'):
                with self.subTest(session=session):
                    with self.assertRaisesRegex(a.AdapterError, '^invalid_session$'):
                        a.session_dir(session)
            read.assert_not_called()

    def entry_point(self, mode):
        self.register()
        self.instance.mkdir(parents=True)
        worker = FakeWorker(self.instance)
        self.addCleanup(worker.close)
        (self.instance / 'driver/act.sock').touch()
        frame = self.instance / 'frames/frame.png'
        frame.parent.mkdir()
        frame.write_bytes(b'fake-image')
        worker.frames = [dict(frame='f:1', path=str(frame), rev=11)]
        paths = []
        def connect(rpc):
            paths.append(rpc.path)
            self.assertEqual(rpc.path, self.instance / 'driver/act.sock')
            worker.connect(rpc)
        def invoke(argv, incoming=''):
            with patch.object(sys, 'argv', argv), \
                    patch('sys.stdin', io.StringIO(incoming)), \
                    patch('sys.stdout', new_callable=io.StringIO) as output:
                self.assertEqual(a.main(), 0)
                return [json.loads(line) for line in output.getvalue().splitlines()]
        calls = [('observe', {}), ('act', {'steps': [{}]})]
        with patch.object(a.WorkerRPC, 'connect', new=connect):
            if mode == 'call':
                results = [invoke(['ark-act', mode, name, json.dumps(args), '--session', 's1',
                                   '--images', 'inline'])[0] for name, args in calls]
                self.assertTrue(all(result['ok'] for result in results))
                state = json.loads((self.instance / 'driver/adapter-state.json').read_text())
                self.assertEqual(state['rev'], shown(results[-1])['rev'])
                self.assertTrue((self.instance / 'driver/adapter-state.lock').exists())
                self.assertFalse((self.legacy / 'driver').exists())
            else:
                incoming = ''.join(json.dumps(dict(jsonrpc='2.0', id=i, method='tools/call',
                    params=dict(name=name, arguments=args)))+'\n'
                    for i, (name, args) in enumerate(calls, 1))
                results = [reply['result'] for reply in invoke(
                    ['ark-act', mode, '--session', 's1', '--images', 'inline'], incoming)]
        self.assertTrue(paths)
        self.assertTrue(all(not result['isError'] for result in results))
        self.assertEqual(results[0]['content'][1]['data'], 'ZmFrZS1pbWFnZQ==')
        self.assertEqual(shown(results[0])['frames'][0]['path'], str(frame))
        self.assertEqual(shown(results[-1])['status'], 'success')
        self.assertEqual(len(worker.calls('window.act')), 1)
        self.assertEqual(worker.errors, [])

    def test_one_shot_socket_state_frames_and_act_follow_instance_directory(self):
        self.entry_point('call')

    def test_mcp_socket_frames_and_act_follow_instance_directory(self):
        self.entry_point('mcp')


class Readiness(unittest.TestCase):
    def test_only_full_settled_session_observe_sets_ready(self):
        for scope in ({'kind': 'window', 'window': 'w1'},
                      {'kind': 'family', 'window': 'w1'},
                      {'kind': 'session'}, {}):
            for full, settled, resync in ((True, True, False), (False, True, False),
                                          (True, False, False), (True, True, True)):
                with self.subTest(scope=scope, full=full, settled=settled, resync=resync):
                    adapter = adapter_with_rpc()
                    adapter.ready = False
                    adapter.rpc.call.return_value = dict(rev=11, full=full, settled=settled,
                                                         resync_required=resync)
                    adapter.call('observe', dict(scope=scope, full=True))
                    self.assertEqual(adapter.ready, scope.get('kind', 'session') == 'session'
                                     and full and settled and not resync)

    def test_scoped_observe_requires_private_session_handshake_before_act(self):
        adapter = adapter_with_rpc()
        adapter.ready = False
        observation = dict(rev=11, full=True, settled=True)
        adapter.rpc.call.side_effect = [observation, observation, receipt()]
        adapter.call('observe', dict(scope=dict(kind='window', window='w1'), full=True))
        self.assertFalse(adapter.ready)
        adapter.call('act', {'steps': [{}]})
        calls = adapter.rpc.call.call_args_list
        self.assertEqual([c.args[0] for c in calls], ['window.observe', 'window.observe', 'window.act'])
        self.assertEqual(calls[1].args[1]['channels'], ['state'])
        self.assertIsNone(calls[1].args[1]['since_rev'])
        self.assertNotIn('scope', calls[1].args[1])
        self.assertEqual(calls[2].args[1]['base_rev'], 11)

    def test_unsettled_private_handshake_does_not_send_act(self):
        adapter = adapter_with_rpc()
        adapter.ready = False
        adapter.rpc.call.return_value = dict(rev=11, full=True, settled=False)
        with self.assertRaisesRegex(a.AdapterError, '^observe_required$'):
            adapter.call('act', {'steps': [{}]})
        self.assertFalse(adapter.ready)
        self.assertNotIn('pending', adapter.state)
        self.assertEqual(adapter.rpc.call.call_count, 1)


class ScopedTrees(ProviderHarness):
    def add_window(self, xid, family='x:10', title='Dialog'):
        self.w.cache.upsert(xid, dict(self.w.cache.windows[10], xid=xid, family=family,
                                     title=title, type='dialog', transient_for=10))
        handle = self.w.cache.handle(xid)
        self.p.cache.update(handle, node(key=title, role='entry', name=title), self.w.cache.rev)
        self.p.probes[handle] = 'usable'
        return handle

    def observe_scope(self, kind='family', window=None, **params):
        return self.w.observation(dict(channels=['tree'], scope=dict(
            kind=kind, window=window or self.handle), **params))

    def test_family_tree_follows_focused_dialog_and_lifecycle(self):
        scope = dict(kind='family', window=self.handle)
        dialog = self.add_window(11)
        self.w.cache.set('focus', dialog)
        self.w.cache.commit()
        result = self.observe_scope()
        self.assertEqual(result['tree']['window'], dialog)
        self.assertIn('Dialog', result['tree']['lines'][0])
        # The same scope is resolved again after close and replacement.
        self.w.cache.remove(11)
        self.w.cache.set('focus', self.handle)
        self.w.cache.commit()
        self.assertEqual(self.observe_scope()['tree']['window'], self.handle)
        replacement = self.add_window(12, title='Replacement')
        self.w.cache.set('focus', replacement)
        self.w.cache.commit()
        self.assertEqual(self.p.target(scope), replacement)
        self.assertEqual(self.observe_scope()['tree']['window'], replacement)
        # A removed anchor can still identify its surviving family.
        self.w.cache.remove(10)
        self.w.cache.commit()
        self.assertEqual(self.observe_scope()['tree']['window'], replacement)

    def test_explicit_window_and_unrelated_focused_family_keep_anchor(self):
        dialog = self.add_window(11)
        self.w.cache.set('focus', dialog)
        self.w.cache.commit()
        result = self.observe_scope('window')
        self.assertEqual(result['tree']['window'], self.handle)
        self.assertIn('Fixture', result['tree']['lines'][0])
        other = self.add_window(12, family='other', title='Unrelated')
        self.w.cache.set('focus', other)
        self.w.cache.commit()
        self.assertEqual(self.observe_scope()['tree']['window'], self.handle)

    def bridge(self, method, params, **kwargs):
        if method == 'session.attach':
            return dict(lease='lease', session='s1', generation=GEN, control_epoch=1)
        self.assertEqual(method, 'window.observe')
        self.w.validate_params(method, params)
        result = self.w.observation(params)
        self.w.observed(self.peer, params, result)
        return result

    def test_mcp_budget_and_cursor_render_complete_continuation(self):
        self.p.cache.update(self.handle, node(children=[node(str(i), 'entry', 'Field '+str(i))
                                                      for i in range(15)]), 2)
        adapter = adapter_with_rpc()
        adapter.rpc.call.side_effect = self.bridge
        args = dict(scope=dict(kind='window', window=self.handle), channels=['tree'],
                    full=True, budget=dict(nodes=3, bytes=700))
        lines, cursor = [], None
        while True:
            if cursor:
                args['cursor'] = cursor
                args['full'] = False
            incoming = io.StringIO(json.dumps(dict(jsonrpc='2.0', id=1, method='tools/call',
                params=dict(name='observe', arguments=args)))+'\n')
            outgoing = io.StringIO()
            a.mcp_serve(adapter, incoming, outgoing)
            result = json.loads(outgoing.getvalue())['result']
            self.assertFalse(result['isError'])
            tree = shown(result)['tree']
            self.assertLessEqual(len(tree['lines']), 3)
            self.assertLessEqual(len(a.encoded(tree)), 700)
            lines.extend(tree['lines'])
            cursor = tree['cursor']
            if not cursor:
                break
        self.assertEqual(lines, self.p.cache.windows[self.handle]['lines'])
        self.assertEqual(self.e.lease_state['lease']['trees'][self.handle], 2)

    def test_one_shot_budget_and_cursor_use_same_path(self):
        self.p.cache.update(self.handle, node(children=[node(str(i), 'entry', str(i))
                                                      for i in range(6)]), 2)
        lines, cursor = [], None
        with tempfile.TemporaryDirectory() as directory:
            sd = Path(directory)
            (sd / 'driver').mkdir()
            while True:
                args = dict(scope=dict(kind='window', window=self.handle), channels=['tree'],
                            budget=dict(nodes=2, bytes=700))
                if cursor:
                    args['cursor'] = cursor
                rpc = Mock()
                rpc.call.side_effect = self.bridge
                with patch.object(a, 'session_dir', return_value=sd), \
                        patch.object(a, 'WorkerRPC', return_value=rpc), \
                        patch.object(sys, 'argv', ['ark-act', 'call', 'observe', json.dumps(args), '--session', 's1']), \
                        patch('sys.stdout', new_callable=io.StringIO) as output:
                    self.assertEqual(a.main(), 0)
                result = json.loads(output.getvalue())
                self.assertTrue(result['ok'])
                tree = shown(result)['tree']
                self.assertLessEqual(len(tree['lines']), 2)
                lines.extend(tree['lines'])
                cursor = tree['cursor']
                if not cursor:
                    break
        self.assertEqual(lines, self.p.cache.windows[self.handle]['lines'])


class LoopBudget(unittest.TestCase):
    def test_latest_observe_is_stubbed_reserve_kept_and_peak_reported(self):
        client, provider = Mock(), Mock()
        client.start.return_value = a.TOOLS
        client.tool.side_effect = [dict(content=[dict(type='text', text=json.dumps(
            dict(rev=11, tree='x'*30000))), dict(type='image', mimeType='image/png', data='eA==')]),
            dict(content=[dict(type='text', text='{"status":"success","summary":"Verified"}')])]
        provider.config = loop.provider_config({})
        snapshots = []
        def complete(tools, history):
            snapshots.append(copy.deepcopy(history))
            name = 'observe' if len(snapshots) == 1 else 'done'
            return dict(role='assistant', content=None, tool_calls=[dict(id=name, type='function',
                function=dict(name=name, arguments='{}'))]), {}
        provider.complete.side_effect = complete
        summary = loop.run_loop(client, provider, 'Task')
        self.assertEqual(summary['done']['status'], 'success')
        self.assertEqual(shown(snapshots[1][-1]), dict(rev=11, superseded=True))
        self.assertEqual(summary['frames_viewed'], 0)
        estimates = [loop.estimated_prompt_tokens(h, a.TOOLS) for h in snapshots]
        self.assertEqual(summary['peak_estimated_prompt_tokens'], max(estimates))
        self.assertEqual(summary['completion_reserve_tokens'], 1024)
        for h, estimate in zip(snapshots, estimates):
            body = loop.request_body(provider.config, a.TOOLS, h)
            self.assertEqual(body['max_tokens'], 1024)
            self.assertEqual(body['n_predict'], 1024)
            self.assertLessEqual(estimate + body['max_tokens'], provider.config['token_budget'])

    def test_irreducible_request_is_not_dispatched(self):
        client, provider = Mock(), Mock()
        client.start.return_value = a.TOOLS
        provider.config = dict(provider='llama', images=False, token_budget=1)
        summary = loop.run_loop(client, provider, 'Task')
        provider.complete.assert_not_called()
        client.tool.assert_not_called()
        self.assertEqual(summary['reason'], 'token_budget_exceeded')
        self.assertEqual(summary['peak_estimated_prompt_tokens'], 0)

    def test_completion_reserve_can_trigger_compaction_and_unlimited_retains_cap(self):
        history = []
        result = add_result(history, 'observe', dict(rev=1, tree='x'*10000))
        estimate = loop.estimated_prompt_tokens(history, a.TOOLS)
        self.assertEqual(loop.compact_history(history, a.TOOLS, estimate, reserve=0), estimate)
        compacted = loop.compact_history(history, a.TOOLS, estimate, reserve=1024)
        self.assertEqual(shown(result), dict(rev=1, superseded=True))
        self.assertLessEqual(compacted + 1024, estimate)
        config = loop.provider_config({'CU_LOOP_TOKEN_BUDGET': '0'})
        self.assertEqual(loop.request_body(config, [], [])['max_tokens'], 4096)


if __name__ == '__main__':
    unittest.main()
