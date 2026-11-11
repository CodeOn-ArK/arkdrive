"""Pack arm selection; isolated module imports and fake RPC only."""
import importlib.util
import io
import json
import os
import sys
import unittest
from unittest.mock import Mock, patch

from test_worker_w5 import a, GEN
from test_worker_w_packs_thunar import listing


class PackKillSwitch(unittest.TestCase):
    def load(self, value):
        spec = importlib.util.spec_from_file_location('pilot_adapter', a.__file__)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ):
            os.environ.pop('ARK_ACT_PACKS', None)
            if value is not None:
                os.environ['ARK_ACT_PACKS'] = value
            spec.loader.exec_module(module)
        return module

    def observe(self, module):
        adapter = module.Adapter('/unused')
        adapter.rpc = Mock()
        adapter.rpc.call.return_value = listing()
        adapter.meta = dict(lease='lease', session='s1', generation=GEN, control_epoch=3)
        adapter.state.update(generation=GEN, rev=10)
        with patch.object(module, 'check_kill'):
            result = adapter.call('observe', {'full': True})
        return adapter, result

    def published(self, module, adapter):
        incoming = io.StringIO(json.dumps(dict(jsonrpc='2.0', id=1, method='tools/list')) + '\n')
        output = io.StringIO()
        module.mcp_serve(adapter, incoming, output)
        return json.loads(output.getvalue())['result']['tools']

    def test_off_never_imports_or_consults_registry_or_advertises_skills(self):
        # A broken registry cannot affect the generalist arm, even at import.
        with patch.dict(sys.modules, packs=None):
            module = self.load('off')
        self.assertFalse(module.PACKS_ENABLED)
        self.assertNotIn('REGISTRY', vars(module))
        with patch.object(module, 'REGISTRY', create=True) as registry:
            adapter, result = self.observe(module)
            self.assertEqual(len(result['content']), 1)
            self.assertIsNone(adapter.active_pack)
            self.assertEqual(adapter._pack_windows, {})
            self.assertNotIn('use_skill', [t['name'] for t in module.TOOLS])
            self.assertEqual(self.published(module, adapter), module.TOOLS)
            with patch.object(module, 'check_kill'):
                with self.assertRaisesRegex(module.AdapterError, '^invalid_tool$'):
                    adapter.call('use_skill', {'name': 'thunar.rename', 'args': {}})
            with self.assertRaisesRegex(module.AdapterError, '^invalid_tool$'):
                adapter.use_skill({'name': 'thunar.rename', 'args': {}})
            registry.assert_not_called()
            self.assertEqual(registry.mock_calls, [])

    def test_unset_on_and_every_other_value_keep_original_tools_and_note(self):
        for value in (None, 'on', '', 'OFF', 'false', '0'):
            with self.subTest(value=value):
                module = self.load(value)
                self.assertTrue(module.PACKS_ENABLED)
                self.assertEqual(module.TOOLS, a.TOOLS)
                adapter, result = self.observe(module)
                self.assertEqual(len(result['content']), 2)
                self.assertTrue(result['content'][1]['text'].startswith('[pack: thunar]\n'))
                self.assertIs(adapter.active_pack, module.REGISTRY.for_class('Thunar'))
                self.assertEqual(self.published(module, adapter), module.TOOLS)
