"""W5 fix round 4: prompt grammar and additive rendering, fake peers only."""
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_worker_w5 import a, loop, receipt, FakeWorker


KINDS = ('window_present, window_absent, dialog_present, dialog_absent, '
         'window_focused, pointer_at, node_present, node_absent, text_equals, '
         'text_contains, node_state, item_present, item_absent')
HINT = {'hint': "invalid step form. Tree expects need provider 'atspi' plus a role/name selector. "
        'Valid expect kinds: ' + KINDS + '. A mutating step needs one strong expect. '
        'Fix the form; do not resend unchanged.'}
HINT_BLOCK = dict(type='text', text=a.encoded(HINT).decode())


class PromptCase(unittest.TestCase):
    def test_system_names_grammar_dispatch_repair_and_startup_rules(self):
        prompt = ' '.join(loop.SYSTEM.split())
        self.assertIn('Valid expect kinds are ' + KINDS + '.', prompt)
        self.assertIn('Tree expects (node_*, text_*, item_*) must include "provider": "atspi"', prompt)
        self.assertIn('selector with role and/or name/name_re', prompt)
        self.assertIn('A step that changes anything (key, click, text, invoke) must carry one strong '
                      'expect or the worker refuses it before dispatch (invalid_expectation)', prompt)
        self.assertIn('If an act returns invalid_expectation or invalid_params, fix the FORM of the '
                      'step; do not repeat it unchanged', prompt)
        self.assertIn('If an observe shows no windows, the application is still starting; wait '
                      'briefly and observe again before acting', prompt)


class RenderingCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='w5-fix4-')
        self.addCleanup(temp.cleanup)
        self.worker = FakeWorker(Path(temp.name))
        self.addCleanup(self.worker.close)
        connector = patch.object(a.WorkerRPC, 'connect', new=lambda rpc: self.worker.connect(rpc))
        connector.start()
        self.addCleanup(connector.stop)
        self.adapter = a.Adapter(temp.name)
        self.addCleanup(self.adapter.close)
        self.adapter.call('observe', {})
        self.worker.messages.clear()

    def mcp(self, name='act', arguments=None):
        request = dict(jsonrpc='2.0', id=1, method='tools/call',
                       params=dict(name=name, arguments=arguments if arguments is not None else {'steps': [{}]}))
        outgoing = io.StringIO()
        a.mcp_serve(self.adapter, io.StringIO(json.dumps(request) + '\n'), outgoing)
        return json.loads(outgoing.getvalue())['result']

    def assert_receipt(self, batch_reason=None, step_reason=None, status='failed', recovered=False):
        captured = []
        def outcome(*args):
            value = receipt(*args)
            value.update(status=status, reason=batch_reason)
            # Check any step, including one after a successful first step.
            if step_reason:
                step = copy.deepcopy(value['steps'][0])
                step.update(id='2', reason=step_reason)
                step['effect']['status'] = 'not_observed'
                value['steps'].append(step)
            captured.append(copy.deepcopy(value))
            return value
        self.worker.drop_act = recovered
        with patch('test_worker_w5.receipt', side_effect=outcome):
            result = self.mcp()
        raw = captured[0]
        # Freeze the pre-round-4 JSON projection, including key order/encoding.
        expected = dict(action_id=raw['action_id'], generation=raw['generation'],
                        status=status, reason=batch_reason, unexecuted_steps=[],
                        state_delta=raw['state_delta'], advice='none', rev=raw['rev']['final'],
                        steps=[dict(id=step['id'], effect={'status': step['effect']['status']},
                                    route='xtest', reason=step['reason']) for step in raw['steps']])
        first = dict(type='text', text=a.encoded(expected).decode())
        hint = batch_reason in ('invalid_expectation', 'invalid_params') or step_reason in (
            'invalid_expectation', 'invalid_params')
        self.assertEqual(result, dict(isError=False, content=[first] + ([HINT_BLOCK] if hint else [])))
        self.assertEqual(self.worker.errors, [])
        self.assertNotIn('pending', self.adapter.state)
        self.assertEqual(self.adapter.state['rev'], raw['rev']['final'])

    def test_batch_reasons_append_one_hint_and_preserve_receipt_bytes(self):
        for reason in ('invalid_expectation', 'invalid_params'):
            with self.subTest(reason=reason):
                self.assert_receipt(batch_reason=reason)

    def test_any_step_reason_appends_one_hint_and_preserves_receipt_bytes(self):
        for reason in ('invalid_expectation', 'invalid_params'):
            with self.subTest(reason=reason):
                self.assert_receipt(step_reason=reason)
        self.assert_receipt(batch_reason='invalid_params', step_reason='invalid_expectation')

    def test_success_and_unrelated_failure_are_byte_identical_without_hint(self):
        self.assert_receipt(status='success')
        self.assert_receipt(batch_reason='timeout', step_reason='timeout')

    def test_recovered_receipt_gets_hint_without_replaying_act(self):
        self.assert_receipt(step_reason='invalid_expectation', recovered=True)
        self.assertEqual(len(self.worker.calls('window.act')), 1)
        self.assertEqual(len(self.worker.calls('action.get')), 1)

    def worker_error(self, reason):
        encode = a.encoded
        def reply(value):
            # Transform only the fake peer's act reply into a wire-level error.
            if isinstance(value, dict) and value.get('result', {}).get('version') == 2:
                value = dict(jsonrpc='2.0', id=value['id'],
                             error=dict(code=-32602, data={'reason': reason}))
            return encode(value)
        with patch.object(a, 'encoded', side_effect=reply):
            return self.mcp()

    def test_fake_peer_invalid_params_error_keeps_error_block_and_adds_hint(self):
        result = self.worker_error('invalid_params')
        self.assertEqual(result, dict(isError=True, content=[
            dict(type='text', text='{"error":"invalid_params"}'), HINT_BLOCK]))
        self.assertEqual(len(self.worker.calls('window.act')), 1)
        self.assertNotIn('pending', self.adapter.state)
        self.assertEqual(self.worker.errors, [])

    def test_unrelated_worker_error_and_non_act_or_local_errors_have_no_hint(self):
        self.assertEqual(self.worker_error('timeout'), dict(isError=True, content=[
            dict(type='text', text='{"error":"timeout"}')]))
        for name, failure in (('observe', a.WorkerError('invalid_params')),
                              ('act', a.AdapterError('invalid_params'))):
            with self.subTest(name=name, failure=type(failure).__name__), \
                    patch.object(self.adapter, 'call', side_effect=failure):
                result = self.mcp(name, {})
            self.assertEqual(result, dict(isError=True, content=[
                dict(type='text', text='{"error":"invalid_params"}')]))

    def test_cut_invalid_params_keeps_failure_envelope_and_exit_status(self):
        for reason in ('invalid_params', 'timeout'):
            adapter = Mock()
            adapter.call.side_effect = a.WorkerError(reason)
            output = io.StringIO()
            with self.subTest(reason=reason), patch.object(a, 'Adapter', return_value=adapter), \
                    patch.object(a, 'session_dir', return_value=self.worker.sd), \
                    patch.object(a.sys, 'argv', ['ark-act', 'call', 'act', '{"steps":[{}]}', '--session', 's1']), \
                    patch('sys.stdout', output):
                code = a.main()
            expected = dict(ok=False, error=reason)
            if reason == 'invalid_params':
                expected['content'] = [dict(type='text', text='{"error":"invalid_params"}'), HINT_BLOCK]
            self.assertEqual(code, 1)
            self.assertEqual(output.getvalue(), a.encoded(expected).decode() + '\n')


if __name__ == '__main__':
    unittest.main()
