"""W5 fix round 2: fake worker peers and provider histories, headless only."""
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_worker_w5 import a, loop, receipt, shown, FakeWorker
from test_worker_w5_fix1 import observation


class AutoReconcileCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='w5-fix2-')
        self.addCleanup(temp.cleanup)
        self.worker = FakeWorker(Path(temp.name))
        self.addCleanup(self.worker.close)
        connector = patch.object(a.WorkerRPC, 'connect', new=lambda rpc: self.worker.connect(rpc))
        connector.start()
        self.addCleanup(connector.stop)
        self.saved = []
        self.adapter = a.Adapter(temp.name, save=lambda value: self.saved.append(copy.deepcopy(value)))
        self.addCleanup(self.adapter.close)
        self.adapter.call('observe', {})
        self.worker.messages.clear()

    def act(self, **fields):
        def outcome(*args):
            return dict(receipt(*args), **fields)
        with patch('test_worker_w5.receipt', side_effect=outcome):
            return shown(self.adapter.call('act', {'steps': [{}]}))

    def assert_calls(self, observes=1):
        self.assertEqual(len(self.worker.calls('window.act')), 1)
        reads = self.worker.calls('window.observe')
        self.assertEqual(len(reads), observes)
        if observes:
            self.assertIsNone(reads[0]['since_rev'])
            self.assertEqual(reads[0]['scope'], {'kind': 'session'})
        self.assertEqual(self.worker.errors, [])

    def test_advice_triggers_one_full_session_read_and_preserves_new_revision(self):
        original = self.adapter.rpc._call
        def read(method, params, timeout=12):
            value = original(method, params, timeout)
            return observation(value['rev']) if method == 'window.observe' else value
        with patch.object(self.adapter.rpc, '_call', side_effect=read):
            result = self.act(status='failed', advice='observe_first')
        self.assert_calls()
        self.assertTrue(result['reconciled'])
        self.assertEqual(result['state_now'], a.render_observe(observation(result['rev'] + 1)))
        self.assertEqual(self.adapter.state['rev'], result['state_now']['rev'])
        self.assertEqual(self.saved[-1]['rev'], result['state_now']['rev'])
        self.assertNotIn('pending', self.adapter.state)
        self.adapter.call('observe', {})
        self.assertEqual(self.worker.calls('window.observe')[-1]['since_rev'], result['state_now']['rev'])

    def test_other_receipt_triggers(self):
        for fields in (dict(reason='awaiting_reconciliation'), dict(status='unknown'),
                       dict(status='contradicted')):
            with self.subTest(fields=fields):
                self.worker.messages.clear()
                result = self.act(**fields)
                self.assertTrue(result['reconciled'])
                self.assert_calls()

    def test_success_and_failure_with_other_advice_do_not_observe(self):
        for status in ('success', 'failed'):
            with self.subTest(status=status):
                self.worker.messages.clear()
                result = self.act(status=status, advice='none')
                self.assertNotIn('reconciled', result)
                self.assertNotIn('state_now', result)
                self.assert_calls(observes=0)

    def test_failed_auto_observe_keeps_original_receipt_and_never_resends_act(self):
        original = self.adapter.rpc._call
        for failure in (a.WorkerError('observe_unavailable'), OSError('read failed')):
            with self.subTest(failure=type(failure).__name__):
                # Reconnect outside the counted act if the preceding read disconnected.
                self.adapter.call('observe', {})
                self.worker.messages.clear()
                def read(method, params, timeout=12):
                    value = original(method, params, timeout)
                    if method == 'window.observe':
                        raise failure
                    return value
                with patch.object(self.adapter.rpc, '_call', side_effect=read):
                    result = self.act(status='unknown', advice='observe_first')
                self.assertFalse(result['reconciled'])
                self.assertNotIn('state_now', result)
                self.assertEqual((result['status'], result['advice']), ('unknown', 'observe_first'))
                self.assertEqual(self.adapter.state['rev'], result['rev'])
                self.assertNotIn('pending', self.adapter.state)
                self.assert_calls()

    def test_unsettled_or_resync_read_is_not_reconciled_but_updates_revision(self):
        original = self.adapter.rpc._call
        for fields in (dict(settled=False), dict(resync_required=True), dict(full=False)):
            with self.subTest(fields=fields):
                self.worker.messages.clear()
                def read(method, params, timeout=12):
                    value = original(method, params, timeout)
                    return dict(value, **fields) if method == 'window.observe' else value
                with patch.object(self.adapter.rpc, '_call', side_effect=read):
                    result = self.act(status='contradicted', advice='observe_first')
                self.assertFalse(result['reconciled'])
                self.assertNotIn('state_now', result)
                self.assertEqual(result['advice'], 'observe_first')
                self.assertEqual(self.adapter.state['rev'], result['rev'] + 1)
                self.assert_calls()

    def test_recovered_receipt_also_reconciles_without_act_replay(self):
        self.worker.drop_act = True
        result = self.act(status='unknown', advice='observe_first')
        self.assertTrue(result['reconciled'])
        self.assert_calls()
        self.assertEqual(len(self.worker.calls('action.get')), 1)
        self.assertEqual(self.worker.calls('action.get')[0]['action_id'], result['action_id'])
        self.assertEqual(self.adapter.state['rev'], result['state_now']['rev'])

    def test_kill_during_auto_observe_is_not_swallowed(self):
        original = self.adapter.rpc._call
        def read(method, params, timeout=12):
            value = original(method, params, timeout)
            if method == 'window.observe':
                raise a.AdapterError('ABORTED_KILLED')
            return value
        with patch.object(self.adapter.rpc, '_call', side_effect=read), \
                self.assertRaisesRegex(a.AdapterError, '^ABORTED_KILLED$'):
            self.act(status='unknown')
        self.assert_calls()


class DescriptionCase(unittest.TestCase):
    def test_worked_example_uses_three_real_steps_and_verified_bindings(self):
        description = next(tool['description'] for tool in a.TOOLS if tool['name'] == 'act')
        example, _ = json.JSONDecoder().raw_decode(description[description.index('{"steps":'):])
        first, second, third = example['steps']
        self.assertEqual([step['id'] for step in example['steps']], ['1', '2', '3'])
        self.assertEqual(first['intent'], dict(kind='key', keys=['Control_L', 'Shift_L', 'n']))
        self.assertEqual(first['expect']['kind'], 'dialog_present')
        self.assertEqual(first['expect']['bind'], dict(dialog='dialog', entry=dict(role='text')))
        self.assertEqual(second['target'], dict(from_step='1', name='entry'))
        self.assertEqual(second['intent'], dict(kind='text', text='Reports', replace=True))
        self.assertEqual(third['target'], dict(from_step='1', name='dialog'))
        self.assertEqual(third['intent'], dict(kind='key', keys=['Return']))
        self.assertEqual(third['expect']['kind'], 'item_present')
        self.assertEqual(third['expect']['name'], second['intent']['text'])

    def test_observe_description_explains_automatic_reconciliation(self):
        description = next(tool['description'] for tool in a.TOOLS if tool['name'] == 'observe')
        self.assertIn('The adapter keeps your view current and reconciles automatically after uncertain acts; '
                      'you do not need full:true for correctness.', description)


class RepetitionCase(unittest.TestCase):
    failure = dict(isError=True, content=[dict(type='text', text='{"error":"invalid_params"}')])
    success = dict(isError=False, content=[dict(type='text', text='{"rev":12}')])

    def run_calls(self, calls, results, batch=False):
        client, provider = Mock(), Mock()
        client.start.return_value = a.TOOLS
        client.tool.side_effect = results
        provider.config = dict(images=False, token_budget=0)
        snapshots = []
        calls = iter([calls] if batch else [[call] for call in calls])
        def complete(tools, history):
            snapshots.append(copy.deepcopy(history))
            proposed = next(calls, [])
            return dict(role='assistant', content=None, tool_calls=proposed), {}
        provider.complete.side_effect = complete
        with patch.object(loop, 'check_kill'):
            summary = loop.run_loop(client, provider, 'Task')
        self.assertEqual(summary['reason'], 'model_stopped_without_done')
        return snapshots

    def calls(self, names, arguments=None):
        return [dict(id='c'+str(i), type='function', function=dict(name=name,
                     arguments=(arguments[i] if arguments else '{"steps":[]}')))
                for i, name in enumerate(names)]

    def hints(self, history):
        return [message['content'] for message in history if message['role'] == 'user'
                and message['content'].startswith('The last 3 identical')]

    def test_hint_after_third_failure_exactly_once_in_ten_call_streak(self):
        snapshots = self.run_calls(self.calls(['act'] * 10), [self.failure] * 10)
        self.assertEqual([len(self.hints(h)) for h in snapshots], [0, 0, 0] + [1] * 8)
        self.assertEqual(self.hints(snapshots[-1]), [
            'The last 3 identical act calls failed with invalid_params. '
            'Do not repeat the same call unchanged; change the step shape or observe first.'])

    def test_changed_arguments_tool_or_success_resets_streak(self):
        for changed in ('arguments', 'tool', 'success'):
            with self.subTest(changed=changed):
                calls = self.calls(['act'] * 8)
                results = [self.failure] * 8
                if changed == 'arguments':
                    calls[3]['function']['arguments'] = '{"steps":[{}]}'
                elif changed == 'tool':
                    calls[3]['function']['name'] = 'observe'
                else:
                    results[3] = self.success
                snapshots = self.run_calls(calls, results)
                self.assertEqual([len(self.hints(h)) for h in snapshots], [0, 0, 0, 1, 1, 1, 1, 2, 2])

    def test_changed_calls_before_threshold_get_no_hint(self):
        snapshots = self.run_calls(self.calls(['act', 'act', 'observe', 'act', 'act']),
                                   [self.failure] * 5)
        self.assertFalse(self.hints(snapshots[-1]))

    def test_error_fields_without_mcp_flag_and_mcp_flag_without_json(self):
        for result, label in ((dict(content=[dict(type='text', text='{"error":"bad_shape"}')]), 'bad_shape'),
                              (dict(error='top_error', content=[]), 'top_error'),
                              (dict(isError=True, content=[dict(type='text', text='failed')]), 'tool_error')):
            with self.subTest(label=label):
                snapshots = self.run_calls(self.calls(['act'] * 3), [result] * 3)
                self.assertIn(f'failed with {label}.', self.hints(snapshots[-1])[0])

    def test_argument_json_order_and_whitespace_do_not_break_streak(self):
        arguments = ['{"steps":[],"deadline_ms":10}', '{ "deadline_ms": 10, "steps": [] }',
                     '{"steps": [], "deadline_ms":10}']
        snapshots = self.run_calls(self.calls(['act'] * 3, arguments), [self.failure] * 3)
        self.assertEqual(len(self.hints(snapshots[-1])), 1)

    def test_batched_tool_results_stay_paired_before_hint(self):
        snapshots = self.run_calls(self.calls(['act'] * 4), [self.failure] * 4, batch=True)
        self.assertEqual([message['role'] for message in snapshots[-1]],
                         ['system', 'user', 'assistant', 'tool', 'tool', 'tool', 'tool', 'user'])
        self.assertEqual(len(self.hints(snapshots[-1])), 1)


class TimeoutCase(unittest.TestCase):
    def test_default_override_and_invalid_values_reach_http_timeout(self):
        for setting, expected in ((None, 120), ('600', 600), ('240.5', 240.5),
                                  ('garbage', 120), ('', 120), ('0', 120), ('-2', 120),
                                  ('nan', 120), ('inf', 120), ('1e9999', 120)):
            with self.subTest(setting=setting):
                env = {} if setting is None else {'CU_LOOP_HTTP_TIMEOUT': setting}
                config = loop.provider_config(env)
                self.assertEqual(config['http_timeout'], expected)
                response = dict(choices=[dict(message=dict(role='assistant', content='Done'))])
                with patch.object(loop.urllib.request, 'urlopen') as open_url:
                    open_url.return_value.__enter__.return_value = io.BytesIO(json.dumps(response).encode())
                    loop.Provider(config).complete([], [])
                self.assertEqual(open_url.call_args.kwargs['timeout'], expected)


if __name__ == '__main__':
    unittest.main()
