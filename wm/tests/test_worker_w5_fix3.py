"""W5 fix round 3: model-facing step fields, fake worker only, headless."""
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_worker_w5 import a, CHILD_BOOT, FakeWorker, shown, WM


class StepSurfaceCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='w5-fix3-')
        self.addCleanup(temp.cleanup)
        self.sd = Path(temp.name)
        self.worker = FakeWorker(self.sd)
        self.addCleanup(self.worker.close)
        connector = patch.object(a.WorkerRPC, 'connect', new=lambda rpc: self.worker.connect(rpc))
        connector.start()
        self.addCleanup(connector.stop)
        self.save = Mock()
        self.adapter = a.Adapter(self.sd, save=self.save)
        self.addCleanup(self.adapter.close)

    def mcp(self, method, params=None):
        incoming = io.StringIO(json.dumps(dict(jsonrpc='2.0', id=1, method=method,
                                               params=params or {})) + '\n')
        outgoing = io.StringIO()
        a.mcp_serve(self.adapter, incoming, outgoing)
        response = json.loads(outgoing.getvalue())
        self.assertNotIn('error', response)
        return response['result']

    def assert_rejected(self, steps, field):
        before = copy.deepcopy(self.adapter.state)
        messages = copy.deepcopy(self.worker.messages)
        self.save.reset_mock()
        with patch.object(a.uuid, 'uuid4') as allocate:
            result = self.mcp('tools/call', dict(name='act', arguments=dict(steps=steps)))
        self.assertTrue(result['isError'])
        text = result['content'][0]['text']
        self.assertIn(field, text)
        self.assertIn('not settable through this tool', text)
        self.assertIn('resend the batch without it', text)
        self.assertEqual(self.worker.calls('window.act'), [])
        self.assertEqual(self.worker.messages, messages)
        self.assertEqual(self.adapter.state, before)
        self.save.assert_not_called()
        allocate.assert_not_called()

    def test_published_act_step_schema_omits_driver_only_fields(self):
        tools = self.mcp('tools/list')['tools']
        act = next(tool for tool in tools if tool['name'] == 'act')
        properties = act['inputSchema']['properties']['steps']['items']['properties']
        self.assertNotIn('risk', properties)
        self.assertNotIn('allow_stale_pixels', properties)

    def test_risk_is_rejected_even_in_later_step_without_worker_or_pending(self):
        self.adapter.call('observe', {})
        for value in ('low', '', None, False):
            with self.subTest(value=value):
                self.assert_rejected([dict(intent=dict(kind='wait')),
                                      dict(intent=dict(kind='wait'), risk=value)], 'risk')
                self.assertNotIn('pending', self.adapter.state)

    def test_allow_stale_pixels_is_rejected_by_presence_without_worker_or_pending(self):
        self.adapter.call('observe', {})
        for value in (True, False, None):
            with self.subTest(value=value):
                self.assert_rejected([dict(intent=dict(kind='wait'), allow_stale_pixels=value)],
                                     'allow_stale_pixels')
                self.assertNotIn('pending', self.adapter.state)

    def test_rejection_precedes_attach_and_observation_handshake(self):
        for field in ('risk', 'allow_stale_pixels'):
            with self.subTest(field=field):
                self.assert_rejected([{field: None}], field)
                self.assertEqual(self.adapter.state, {})
                self.assertIsNone(self.adapter.meta)

    def test_rejection_preserves_existing_pending_without_polling(self):
        self.adapter.state['pending'] = dict(action_id='a-unresolved', generation='old-generation')
        for field in ('risk', 'allow_stale_pixels'):
            with self.subTest(field=field):
                self.assert_rejected([{field: None}], field)
        self.assertEqual(self.worker.calls('action.get'), [])

    def cut(self, tool, arguments):
        with self.worker.client_socket() as channel:
            result = subprocess.run(
                [sys.executable, '-c', CHILD_BOOT, str(WM), str(self.sd), str(channel.fileno()),
                 'call', tool, json.dumps(arguments), '--session', 's1'],
                pass_fds=(channel.fileno(),), capture_output=True, text=True, timeout=10)
        self.assertEqual(result.stderr, '')
        return result.returncode, json.loads(result.stdout)

    def test_cut_rejections_do_not_change_persisted_state(self):
        code, _ = self.cut('observe', {})
        self.assertEqual(code, 0)
        path = self.sd / 'driver/adapter-state.json'
        before = path.read_bytes()
        self.assertNotIn('pending', json.loads(before))
        for field in ('risk', 'allow_stale_pixels'):
            with self.subTest(field=field):
                code, result = self.cut('act', dict(steps=[{field: None}]))
                self.assertEqual(code, 1)
                self.assertFalse(result['ok'])
                self.assertIn(field, result['error'])
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(self.worker.calls('window.act'), [])
        self.assertEqual(self.worker.errors, [])

    def test_clean_act_forwards_identical_step_bytes(self):
        self.adapter.call('observe', {})
        steps = [dict(id='1', target='w1', intent=dict(kind='key', keys=['F2']),
                      expect=dict(kind='dialog_present', match=dict(title_re='^Rename'),
                                  bind=dict(dialog='dialog', entry=dict(role='text'))),
                      require=dict(focus='w1'), timeout_ms=900),
                 dict(id='2', target=dict(from_step='1', name='entry'),
                      intent=dict(kind='text', text='Résumé', replace=True))]
        before = a.encoded(steps)
        result = self.mcp('tools/call', dict(name='act', arguments=dict(steps=steps, deadline_ms=2500)))
        self.assertFalse(result['isError'])
        self.assertEqual(shown(result)['status'], 'success')
        calls = self.worker.calls('window.act')
        self.assertEqual(len(calls), 1)
        self.assertEqual(a.encoded(calls[0]['steps']), before)
        self.assertEqual(a.encoded(steps), before)
        self.assertEqual(calls[0]['deadline_ms'], 2500)
        self.assertNotIn('pending', self.adapter.state)
        self.assertEqual(self.worker.errors, [])


if __name__ == '__main__':
    unittest.main()
