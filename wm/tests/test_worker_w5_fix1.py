"""W5 fix round 1: model rendering/history only, with no runtime connections."""
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_worker_w5 import a, loop, receipt, shown, GEN


def observation(rev=11):
    health = dict(state='degraded', reason='application_unreachable', pid=101,
                  breaches=3, next_probe_ms=999, detail={'raw': 'private'})
    app = dict(health, windows=['w1'], window_status={'w1': dict(health)})
    return dict(rev=rev, generation=GEN, full=True, settled=True, unchanged=False,
                state={'focus': 'w1', 'windows': [{'title': 'Résumé', 'handle': 'w1'}]},
                tree={'lines': ['n1 entry "Résumé"', 'n2 button "Open"'], 'cursor': None},
                pixels_changed=dict(bbox={'x': 0, 'y': 0, 'w': 1920, 'h': 1080},
                                    tiles=list(range(1000)),
                                    windows=[dict(window='w1', tiles=list(range(500)),
                                                  bbox={'x': 0, 'y': 0, 'w': 800, 'h': 600})]),
                providers=dict(atspi=dict(state='degraded', apps={'101': app}),
                               pixels=dict(state='healthy', reason=None, sample_rev=rev,
                                           age_ms=0, coverage='screen'),
                               fallback_available=['state', 'pixels', 'keyboard']),
                coverage={'tree': {'status': 'partial'}}, channels_used=['state', 'tree'])


def adapter_with_rpc():
    adapter = a.Adapter('/unused')
    adapter.rpc = Mock()
    adapter.meta = dict(lease='lease', session='s1', generation=GEN, control_epoch=3)
    adapter.ready = True
    adapter.state.update(generation=GEN, rev=10)
    return adapter


def add_result(history, name, value, image=False):
    ident = 'c' + str(len(history))
    history.append(dict(role='assistant', content=None, tool_calls=[
        dict(id=ident, type='function', function=dict(name=name, arguments='{}'))]))
    content = [dict(type='text', text=a.encoded(value).decode())]
    if image:
        content.append(dict(type='image', mimeType='image/png', data='eA=='))
    result = dict(role='tool', tool_call_id=ident, content=content, is_error=False)
    history.append(result)
    return result


def estimate(history, tools):
    return (len(a.encoded(history).decode()) + len(a.encoded(tools).decode())) / 3


class ObserveRenderingCase(unittest.TestCase):
    def test_observe_summary_preserves_tree_state_and_worker_result(self):
        raw = observation()
        before = a.encoded(raw)
        adapter = adapter_with_rpc()
        adapter.rpc.call.return_value = raw
        with patch.object(a, 'check_kill'):
            content = adapter.call('observe', {})
        result = shown(content)
        pixels = result['pixels_changed']
        self.assertEqual(pixels['tiles'], 1000)
        self.assertEqual(pixels['bbox'], raw['pixels_changed']['bbox'])
        self.assertEqual(pixels['windows'], [dict(window='w1', tiles=500,
                         bbox=raw['pixels_changed']['windows'][0]['bbox'])])
        self.assertNotIn('coverage', result)
        self.assertNotIn('channels_used', result)
        app = result['providers']['atspi']['apps']['101']
        health = dict(state='degraded', reason='application_unreachable')
        self.assertEqual(app, dict(health, window_status={'w1': health}))
        self.assertEqual(result['providers']['pixels'], dict(state='healthy', reason=None))
        for field in ('rev', 'generation', 'full', 'settled', 'unchanged', 'state', 'tree'):
            self.assertEqual(a.encoded(result[field]), a.encoded(raw[field]))
        self.assertEqual(a.encoded(raw), before)
        self.assertEqual(adapter.state['rev'], 11)

    def test_projection_is_shared_by_mcp_and_cut(self):
        raw = observation()
        expected = a.encoded(a.render_observe(raw)).decode()
        adapter = adapter_with_rpc()
        adapter.rpc.call.return_value = raw
        incoming = io.StringIO(json.dumps(dict(jsonrpc='2.0', id=1, method='tools/call',
                               params=dict(name='observe', arguments={}))) + '\n')
        outgoing = io.StringIO()
        with patch.object(a, 'check_kill'):
            a.mcp_serve(adapter, incoming, outgoing)
        self.assertEqual(json.loads(outgoing.getvalue())['result']['content'][0]['text'], expected)
        with tempfile.TemporaryDirectory() as directory:
            sd = Path(directory)
            (sd / 'driver').mkdir()
            rpc = Mock()
            rpc.call.side_effect = [dict(lease='lease', session='s1', generation=GEN, control_epoch=3), raw]
            with patch.object(a, 'session_dir', return_value=sd), patch.object(a, 'WorkerRPC', return_value=rpc), \
                    patch.object(a, 'check_kill'), patch.object(sys, 'argv', ['ark-act', 'call', 'observe', '{}', '--session', 's1']), \
                    patch('sys.stdout', new_callable=io.StringIO) as output:
                self.assertEqual(a.main(), 0)
            self.assertEqual(json.loads(output.getvalue())['content'][0]['text'], expected)

    def test_act_and_wait_rendering_unchanged(self):
        adapter = adapter_with_rpc()
        raw = observation()
        adapter.rpc.call.return_value = raw
        with patch.object(a, 'check_kill'):
            result = adapter.call('wait', dict(expect={'kind': 'window_present'}, timeout_ms=100))
        self.assertEqual(result['content'][0]['text'], a.encoded(raw).decode())
        act = receipt()
        adapter.rpc.call.return_value = act
        with patch.object(a, 'check_kill'):
            result = adapter.call('act', {'steps': [{}]})
        self.assertEqual(result['content'][0]['text'], a.encoded(a.render_receipt(act)).decode())


class HistoryCase(unittest.TestCase):
    def test_superseded_observes_keep_rev_and_leave_act_latest_and_envelopes(self):
        history = []
        first = add_result(history, 'observe', {'rev': 11, 'state': 'old'}, image=True)
        act = add_result(history, 'act', receipt())
        latest = add_result(history, 'observe', observation(13), image=True)
        before = copy.deepcopy(history)
        loop.supersede_observes(history)
        self.assertEqual(first['content'], [dict(type='text', text='{"rev":11,"superseded":true}')])
        self.assertEqual(act, before[3])
        self.assertEqual(latest, before[5])
        for now, old in zip(history, before):
            self.assertEqual({k: v for k, v in now.items() if k != 'content'},
                             {k: v for k, v in old.items() if k != 'content'})
        # Repeat on the next observe; an existing stub must retain its revision.
        add_result(history, 'observe', {'rev': 14})
        loop.supersede_observes(history)
        self.assertEqual(shown(first), {'rev': 11, 'superseded': True})
        self.assertEqual(shown(latest), {'rev': 13, 'superseded': True})

    def test_unknown_observe_revision_is_null(self):
        for content in ([dict(type='text', text='not json')],
                        [dict(type='text', text='[]')],
                        [dict(type='text', text='{"error":"unavailable"}')]):
            with self.subTest(content=content):
                history = []
                old = add_result(history, 'observe', {})
                old['content'] = content
                add_result(history, 'observe', {'rev': 2})
                loop.supersede_observes(history)
                self.assertEqual(shown(old), {'rev': None, 'superseded': True})

    def budget_history(self):
        history = [dict(role='system', content='System'), dict(role='user', content='Task')]
        for name in ('observe', 'act', 'wait', 'act', 'observe', 'act', 'wait'):
            add_result(history, name, {'rev': len(history), 'payload': 'x' * 1000}, image=True)
        return history

    def test_tiny_budget_stubs_old_act_wait_and_protects_last_two_and_observes(self):
        history = self.budget_history()
        before = copy.deepcopy(history)
        loop.compact_history(history, a.TOOLS, 1)
        results = list(loop.tool_results(history))
        originals = list(loop.tool_results(before))
        for index, ((result, name), (original, _)) in enumerate(zip(results, originals)):
            if index < len(results) - 2 and name in ('act', 'wait'):
                self.assertEqual(result['content'], [dict(type='text', text='{"superseded":true}')])
            else:
                self.assertEqual(result, original)
        # No eligible content remains; an unattainable budget still sends as-is.
        after = a.encoded(history)
        loop.compact_history(history, a.TOOLS, 1)
        self.assertEqual(a.encoded(history), after)

    def test_budget_stubs_oldest_first_and_stops_when_request_fits(self):
        history = self.budget_history()
        expected = copy.deepcopy(history)
        expected[5]['content'] = [dict(type='text', text='{"superseded":true}')]
        budget = estimate(expected, a.TOOLS)
        self.assertGreater(estimate(history, a.TOOLS), budget)
        loop.compact_history(history, a.TOOLS, budget)
        self.assertEqual(history, expected)

    def test_disabled_or_sufficient_budget_preserves_exact_history(self):
        for budget in (0, 100000):
            history = self.budget_history()
            before = a.encoded(history)
            loop.compact_history(history, a.TOOLS, budget)
            self.assertEqual(a.encoded(history), before)
        # Tool definitions alone can push the request over the threshold.
        history = self.budget_history()
        before = a.encoded(history)
        loop.compact_history(history, [], estimate(history, []))
        self.assertEqual(a.encoded(history), before)
        loop.compact_history(history, a.TOOLS, estimate(history, []))
        self.assertNotEqual(a.encoded(history), before)

    def test_budget_defaults_overrides_and_validation(self):
        for preset in loop.PRESETS:
            env = dict(CU_MODEL_PROVIDER=preset, CU_MODEL='model', CU_MODEL_API_KEY='key')
            config = loop.provider_config(env)
            self.assertEqual(config['token_budget'], 7000 if preset == 'llama' else 0)
            if preset != 'llama':
                history = self.budget_history()
                before = a.encoded(history)
                loop.compact_history(history, a.TOOLS, config['token_budget'])
                self.assertEqual(a.encoded(history), before)
            for setting in ('0', '123'):
                config = loop.provider_config(dict(env, CU_LOOP_TOKEN_BUDGET=setting))
                self.assertEqual(config['token_budget'], int(setting))
        for setting in ('-1', 'bad', '1.5', ''):
            with self.subTest(setting=setting), self.assertRaisesRegex(a.AdapterError, '^invalid_loop_token_budget$'):
                loop.provider_config({'CU_LOOP_TOKEN_BUDGET': setting})

    def test_frames_removed_before_delivery_are_not_counted_as_viewed(self):
        for names, budget, expected in [(('observe', 'observe'), 0, 1),
                                        (('act', 'wait', 'act', 'wait'), 1, 2)]:
            with self.subTest(names=names):
                client = Mock()
                client.start.return_value = a.TOOLS
                client.tool.return_value = dict(content=[dict(type='text', text='{"rev":11}'),
                    dict(type='image', mimeType='image/png', data='eA==')])
                provider = Mock()
                provider.config = dict(provider='llama', images=True, token_budget=budget)
                calls = [dict(id='c'+str(i), type='function', function=dict(name=name, arguments='{}'))
                         for i, name in enumerate(names)]
                provider.complete.side_effect = [(dict(role='assistant', content=None, tool_calls=calls), {}),
                                                  (dict(role='assistant', content='Stop'), {})]
                with patch.object(loop, 'check_kill'):
                    summary = loop.run_loop(client, provider, 'Task')
                self.assertEqual(summary['frames_viewed'], expected)

    def test_loop_compacts_before_provider_and_adapter_bookkeeping_is_independent(self):
        adapter = adapter_with_rpc()
        revisions, actions, snapshots = [], [], []
        def rpc(method, params, **kwargs):
            if method == 'session.done':
                return {}
            rev = 11 + len(revisions)
            if method == 'window.act':
                self.assertEqual(params['base_rev'], revisions[-1])
                actions.append(params['action_id'])
                result = receipt(rev, params['action_id'])
            else:
                self.assertEqual(params['since_rev'], adapter.state['rev'])
                result = observation(rev)
            revisions.append(rev)
            return result
        adapter.rpc.call.side_effect = rpc
        client = Mock()
        client.start.return_value = a.TOOLS
        client.tool.side_effect = adapter.call
        provider = Mock()
        provider.config = dict(provider='llama', images=False, token_budget=1)
        names = iter(('observe', 'act', 'observe', 'wait', 'act', 'act', 'done'))
        def complete(tools, history):
            snapshots.append(copy.deepcopy(history))
            name = next(names)
            arguments = {'steps': [{}]} if name == 'act' else (
                dict(expect={'kind': 'window_present'}, timeout_ms=100) if name == 'wait' else (
                    dict(status='success', summary='Verified') if name == 'done' else {}))
            return dict(role='assistant', content=None, tool_calls=[dict(id='c'+str(len(snapshots)),
                        type='function', function=dict(name=name, arguments=json.dumps(arguments)))]), {}
        provider.complete.side_effect = complete
        with patch.object(loop, 'check_kill'), patch.object(a, 'check_kill'):
            summary = loop.run_loop(client, provider, 'Task')
        self.assertEqual(summary['done'], dict(status='success', summary='Verified'))
        self.assertEqual(len(set(actions)), 3)
        results = list(loop.tool_results(snapshots[-1]))
        self.assertEqual(shown(results[0][0]), {'rev': 11, 'superseded': True})
        self.assertEqual(shown(results[1][0]), {'superseded': True})
        self.assertEqual(shown(results[2][0])['rev'], 13)
        self.assertEqual(shown(results[3][0]), {'superseded': True})
        self.assertEqual([shown(message)['rev'] for message, _ in results[-2:]], [15, 16])


if __name__ == '__main__':
    unittest.main()
