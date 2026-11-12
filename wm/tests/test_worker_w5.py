#!/usr/bin/python3
"""W5 headless evidence: fake socket-pair worker, real adapter subprocesses, fake APIs.

No session, display, bus, pyatspi runtime or provider network is started.
"""
import base64
import copy
import io
import inspect
import json
import os
from pathlib import Path
import socket
import select
import struct
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from test_worker_w1 import WM, GEN, OTHER, w, window
from test_worker_w3 import decode
from worker_pixels import Pixels, Shadow
import worker_adapter as a
import worker_loop as loop


def receipt(rev=12, action_id='a-test'):
    return dict(version=2, generation=GEN, action_id=action_id, status='success', reason=None,
                rev=dict(base=11, final=rev), unexecuted_steps=[], state_delta=dict(rev=rev, state={'focus': 'w1'}),
                advice='none', steps=[dict(id='1', effect=dict(status='verified', evidence=[]),
                                          route='xtest', reason=None, attempts=[], intent_redacted={})])


def shown(result):
    return json.loads(result['content'][0]['text'])


class StreamSocket:
    """Use read/write on socketpair FDs: sandbox forbids sendto/recvfrom too."""
    def __init__(self, sock):
        self.sock, self.timeout = sock, 3

    def settimeout(self, timeout):
        self.timeout = timeout

    def recv(self, size):
        if not select.select([self.sock], [], [], self.timeout)[0]:
            raise socket.timeout()
        return os.read(self.sock.fileno(), size)

    def sendall(self, value):
        while value:
            written = os.write(self.sock.fileno(), value)
            value = value[written:]

    def close(self):
        self.sock.close()

    def shutdown(self, how):
        self.sock.shutdown(how)


CHILD_BOOT = "import sys, os, socket, select\n" + inspect.getsource(StreamSocket) + """
from pathlib import Path
sys.path.insert(0, sys.argv.pop(1))
import worker_adapter as a
sd = Path(sys.argv.pop(1))
fd = int(sys.argv.pop(1))
a.session_dir = lambda session: sd
def connect(rpc):
    rpc.close()
    rpc.sock = StreamSocket(os.fdopen(os.dup(fd), 'r+b', buffering=0))
    rpc.sock.settimeout(3)
a.WorkerRPC.connect = connect
raise SystemExit(a.main())
"""


class FakeWorker:
    """Real AF_UNIX socketpair framing; bind/connect discovery is injected.

    Socket listeners are blocked by the build sandbox. Inherited socketpair
    descriptors also allow testing real CUT/MCP subprocesses without a listener.
    """
    def __init__(self, sd):
        self.sd, self.rev = sd, 10
        self.messages, self.errors, self.actions = [], [], {}
        self.drop_act, self.pending_gets, self.hold_attach = False, 0, 0
        self.generation, self.attach_rev = GEN, None
        self.frames, self.act_frames = [], []
        self.stop = threading.Event()
        (sd / 'driver').mkdir()
        self.clients, self.threads = [], []

    def client_socket(self):
        client, conn = socket.socketpair()
        conn = StreamSocket(conn)
        self.clients.append(conn)
        thread = threading.Thread(target=self.connection, args=(conn, len(self.clients)))
        self.threads.append(thread)
        thread.start()
        return client

    def connect(self, rpc):
        rpc.close()
        rpc.sock = StreamSocket(self.client_socket())
        rpc.sock.settimeout(3)

    @staticmethod
    def read(conn, size):
        value = bytearray()
        while len(value) < size:
            chunk = conn.recv(size - len(value))
            if not chunk:
                raise EOFError
            value.extend(chunk)
        return value

    def connection(self, conn, ident):
        try:
            while not self.stop.is_set():
                size = struct.unpack('!I', self.read(conn, 4))[0]
                msg = json.loads(self.read(conn, size))
                self.messages.append((ident, msg))
                method, params = msg['method'], msg['params']
                if method == 'session.attach':
                    if self.hold_attach:
                        self.hold_attach -= 1
                        response = dict(jsonrpc='2.0', id=msg['id'], error=dict(code=-32000, data={'reason': 'lease_held'}))
                        data = a.encoded(response)
                        conn.sendall(struct.pack('!I', len(data))+data)
                        continue
                    value = dict(lease='lease-'+str(ident), session='s1', generation=self.generation,
                                 rev=self.attach_rev or self.rev, control_epoch=3)
                elif method == 'window.observe':
                    self.rev += 1
                    value = dict(rev=self.rev, full=params.get('since_rev') is None, settled=True,
                                 state={'focus': 'w1'}, frames=self.frames)
                elif method == 'window.act':
                    self.rev += 1
                    value = receipt(self.rev, params['action_id'])
                    value['steps'][0]['effect']['evidence'] = copy.deepcopy(self.act_frames)
                    self.actions[params['action_id']] = value
                    if self.drop_act:
                        return
                elif method == 'action.get':
                    if self.pending_gets:
                        self.pending_gets -= 1
                        value = dict(status='pending', action_id=params['action_id'])
                    else:
                        found = self.actions.get(params['action_id'])
                        value = dict(status='final', receipt=found) if found else dict(status='not_found')
                elif method == 'window.wait':
                    self.rev += 1
                    value = dict(rev=self.rev, satisfied=True, state_delta={'rev': self.rev}, reason=None)
                elif method == 'session.done':
                    value = {}
                else:
                    raise AssertionError(method)
                body = a.encoded(dict(jsonrpc='2.0', id=msg['id'], result=value))
                conn.sendall(struct.pack('!I', len(body)) + body)
        except (EOFError, ConnectionError, OSError):
            pass
        except Exception as exc:
            self.errors.append(exc)
        finally:
            conn.close()

    def calls(self, method):
        return [msg['params'] for _, msg in self.messages if msg['method'] == method]

    def close(self):
        self.stop.set()
        for conn in self.clients:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for thread in self.threads:
            thread.join(2)


class AdapterCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='w5-')
        self.addCleanup(temp.cleanup)
        self.sd = Path(temp.name)
        self.worker = FakeWorker(self.sd)
        self.addCleanup(self.worker.close)
        connector = patch.object(a.WorkerRPC, 'connect', new=lambda rpc: self.worker.connect(rpc))
        connector.start()
        self.addCleanup(connector.stop)
        self.adapter = a.Adapter(self.sd)
        self.addCleanup(self.adapter.close)

    def test_attach_is_private_and_first_act_requires_observe(self):
        self.adapter.attach()
        self.assertNotIn('rev', self.adapter.state)
        with self.assertRaisesRegex(a.AdapterError, 'observe_required'):
            self.adapter.call('act', {'steps': [{}]})
        self.assertFalse(self.worker.calls('window.act'))
        self.assertEqual(self.worker.calls('session.attach')[0]['client'], 'codex')

    def test_observe_act_wait_bookkeeping_and_owned_fields(self):
        first = shown(self.adapter.call('observe', {}))
        self.assertIsNone(self.worker.calls('window.observe')[-1]['since_rev'])
        out = shown(self.adapter.call('act', {'steps': [{'intent': {'kind': 'wait'}}], 'deadline_ms': 1000}))
        act = self.worker.calls('window.act')[-1]
        self.assertEqual(act['base_rev'], first['rev'])
        self.assertEqual((act['lease'], act['session'], act['generation'], act['control_epoch']), ('lease-1', 's1', GEN, 3))
        self.assertTrue(act['action_id'].startswith('a-'))
        self.assertEqual(out['rev'], self.adapter.state['rev'])
        wait = shown(self.adapter.call('wait', dict(expect={'kind': 'window_present'}, timeout_ms=500)))
        self.assertEqual(self.worker.calls('window.wait')[-1]['since_rev'], out['rev'])
        self.assertEqual(self.worker.calls('window.wait')[-1]['deadline_ms'], 500)
        self.adapter.call('observe', {})
        self.assertEqual(self.worker.calls('window.observe')[-1]['since_rev'], wait['rev'])
        last = self.adapter.state['rev']
        self.adapter.call('act', {'steps': [{}]})
        self.assertEqual(self.worker.calls('window.act')[-1]['base_rev'], last)
        self.assertNotEqual(act['action_id'], self.worker.calls('window.act')[-1]['action_id'])
        self.assertEqual(self.worker.errors, [])

    def test_full_clears_since_and_all_ordinary_observe_fields_pass(self):
        self.adapter.call('observe', {})
        args = dict(full=True, channels=['pixels'], scope={'kind': 'session'},
                    region=dict(space='desktop', x=0, y=0, w=20, h=20))
        self.adapter.call('observe', args)
        wire = self.worker.calls('window.observe')[-1]
        self.assertIsNone(wire['since_rev'])
        self.assertNotIn('full', wire)
        for key in ('channels', 'scope', 'region'):
            self.assertEqual(wire[key], args[key])

    def test_model_cannot_override_adapter_identity_or_revision(self):
        for name, args in [('observe', {'since_rev': 5}), ('act', {'steps': [], 'base_rev': 5}),
                           ('act', {'steps': [], 'action_id': 'a-forged'}),
                           ('done', {'status': 'success', 'summary': '', 'lease': 'forged'})]:
            with self.subTest(name=name, args=args), self.assertRaises(a.AdapterError):
                self.adapter.call(name, args)
        self.assertEqual(self.worker.messages, [])

    def test_drop_after_act_only_action_get_same_id_no_second_act(self):
        self.worker.drop_act = True
        self.worker.pending_gets = 2
        self.adapter.call('observe', {})
        result = shown(self.adapter.call('act', {'steps': [{}]}))
        acts = self.worker.calls('window.act')
        self.assertEqual(len(acts), 1)
        gets = self.worker.calls('action.get')
        self.assertEqual(len(gets), 3)
        self.assertTrue(all(p == {'action_id': acts[0]['action_id'], 'generation': GEN} for p in gets))
        self.assertEqual((result['action_id'], result['status']), (acts[0]['action_id'], 'success'))
        self.assertEqual(self.adapter.state['rev'], result['rev'])
        self.assertNotIn('pending', self.adapter.state)
        # Next tool re-attaches; the newer private attach rev is never shown.
        self.worker.attach_rev = 9999
        self.adapter.call('observe', {})
        self.assertEqual(self.worker.calls('window.observe')[-1]['since_rev'], result['rev'])
        self.assertEqual(len(self.worker.calls('session.attach')), 2)

    def test_timeout_after_send_also_resolves_without_replay(self):
        self.adapter.call('observe', {})
        original = self.adapter.rpc._call
        def timeout(method, params, timeout=12):
            value = original(method, params, timeout)
            if method == 'window.act':
                raise socket.timeout()
            return value
        with patch.object(self.adapter.rpc, '_call', side_effect=timeout):
            result = shown(self.adapter.call('act', {'steps': [{}]}))
        self.assertEqual(result['status'], 'success')
        self.assertEqual(len(self.worker.calls('window.act')), 1)
        self.assertEqual(self.worker.calls('action.get')[0]['action_id'], result['action_id'])

    def test_unresolved_id_survives_timeout_and_next_act_resolves_it(self):
        self.adapter.call('observe', {})
        self.worker.drop_act, self.worker.pending_gets = True, 2
        self.adapter.recovery_seconds = 0
        with self.assertRaisesRegex(a.AdapterError, 'action_resolution_timeout'):
            self.adapter.call('act', {'steps': [{}]})
        saved = copy.deepcopy(self.adapter.state)
        self.assertIn('pending', saved)
        self.adapter.close()
        second = a.Adapter(self.sd, state=saved)
        self.addCleanup(second.close)
        result = shown(second.call('act', {'steps': [{'new': 'intent'}]}))
        self.assertEqual(result['action_id'], saved['pending']['action_id'])
        self.assertEqual(len(self.worker.calls('window.act')), 1)

    def test_unknown_missing_intent_does_not_dispatch(self):
        self.adapter.state['pending'] = dict(action_id='a-missing', generation=GEN)
        self.adapter.recovery_seconds = 0
        with self.assertRaisesRegex(a.AdapterError, 'action_resolution_timeout'):
            self.adapter.call('act', {'steps': [{}]})
        self.assertIn('pending', self.adapter.state)
        self.assertEqual(self.worker.calls('window.act'), [])

    def test_oversized_or_nonfinite_act_is_rejected_without_pending_id(self):
        self.adapter.call('observe', {})
        for steps in ([{'huge': 'x' * a.MAX_FRAME}], [{'nonfinite': float('nan')}]):
            with self.subTest(steps_type=list(steps[0])[0]), self.assertRaises((a.AdapterError, ValueError)):
                self.adapter.call('act', {'steps': steps})
            self.assertNotIn('pending', self.adapter.state)
        self.assertFalse(self.worker.calls('window.act'))

    def test_fresh_handshake_never_replaces_persisted_act_base(self):
        self.adapter.state.update(generation=GEN, rev=5)
        result = shown(self.adapter.call('act', {'steps': [{}]}))
        self.assertEqual(self.worker.calls('window.act')[0]['base_rev'], 5)
        self.assertEqual(self.worker.calls('window.observe')[0]['since_rev'], None)
        self.assertGreater(self.worker.rev, 5)
        self.assertEqual(self.adapter.state['rev'], result['rev'])

    def test_generation_change_discards_old_baseline(self):
        self.adapter.state.update(generation=OTHER, rev=999)
        self.adapter.call('observe', {})
        self.assertIsNone(self.worker.calls('window.observe')[-1]['since_rev'])
        self.assertEqual(self.adapter.state['generation'], GEN)

    def test_lease_grace_retries_attach_by_reason(self):
        self.worker.hold_attach = 2
        self.adapter.call('observe', {})
        self.assertEqual(len(self.worker.calls('session.attach')), 3)

    def test_done_maps_and_stops_adapter(self):
        self.adapter.call('done', dict(status='blocked', summary='Needs human'))
        self.assertEqual(self.worker.calls('session.done')[-1]['status'], 'blocked')
        self.assertTrue(self.adapter.finished)
        with self.assertRaisesRegex(a.AdapterError, 'session_done'):
            self.adapter.call('observe', {})

    def test_path_default_and_inline_png_blocks(self):
        path = self.sd / 'fake.png'
        path.write_bytes(b'fake PNG bytes')
        self.worker.frames = [dict(frame='f:one', path=str(path), scale=1)]
        result = self.adapter.call('observe', {})
        self.assertEqual(len(result['content']), 1)
        self.assertEqual(shown(result)['frames'][0]['path'], str(path))
        self.adapter.images = 'inline'
        result = self.adapter.call('observe', {})
        self.assertEqual(result['content'][1], dict(type='image', mimeType='image/png', data=base64.b64encode(path.read_bytes()).decode()))

    def test_inline_failure_does_not_advance_last_shown_rev(self):
        self.adapter.call('observe', {})
        before = self.adapter.state['rev']
        self.worker.frames = [dict(frame='f:missing', path=str(self.sd / 'missing.png'))]
        self.adapter.images = 'inline'
        with self.assertRaises(OSError):
            self.adapter.call('observe', {})
        self.assertEqual(self.adapter.state['rev'], before)

    def test_act_inline_failure_keeps_id_until_result_can_be_shown(self):
        self.adapter.call('observe', {})
        before = self.adapter.state['rev']
        self.worker.act_frames = [dict(frame='f:missing', path=str(self.sd / 'missing.png'))]
        self.adapter.images = 'inline'
        with self.assertRaises(OSError):
            self.adapter.call('act', {'steps': [{}]})
        pending = dict(self.adapter.state['pending'])
        self.assertEqual(self.adapter.state['rev'], before)
        self.adapter.images = 'path'
        result = shown(self.adapter.call('act', {'steps': [{}]}))
        self.assertEqual(result['action_id'], pending['action_id'])
        self.assertNotIn('pending', self.adapter.state)
        self.assertEqual(len(self.worker.calls('window.act')), 1)

    def test_kill_switch_prevents_any_worker_contact(self):
        kill = self.sd / 'kill'
        kill.touch()
        with patch.dict(os.environ, CU_KILL_FILE=str(kill)), self.assertRaisesRegex(a.AdapterError, 'ABORTED_KILLED'):
            self.adapter.call('observe', {})
        self.assertEqual(self.worker.messages, [])

    def cut(self, tool, args):
        # Inject only the run-dir resolver in a fresh process. Its production
        # main, framing, lock, state loader/writer and exit handling are real.
        with self.worker.client_socket() as channel:
            result = subprocess.run([sys.executable, '-c', CHILD_BOOT, str(WM), str(self.sd), str(channel.fileno()), 'call', tool,
                                     json.dumps(args), '--session', 's1'], pass_fds=(channel.fileno(),), capture_output=True, text=True, timeout=10)
        self.assertEqual(result.stderr, '')
        return result.returncode, json.loads(result.stdout)

    def test_cut_state_persists_across_real_invocations_and_private_handshake(self):
        code, one = self.cut('observe', {})
        self.assertEqual(code, 0)
        first = shown(one)['rev']
        path = self.sd / 'driver/adapter-state.json'
        self.assertEqual(json.loads(path.read_text())['rev'], first)
        code, two = self.cut('observe', {})
        self.assertEqual(code, 0)
        self.assertEqual(self.worker.calls('window.observe')[-1]['since_rev'], first)
        second = shown(two)['rev']
        code, result = self.cut('act', {'steps': [{}]})
        self.assertEqual(code, 0)
        self.assertIsNone(self.worker.calls('window.observe')[-1]['since_rev'])
        self.assertEqual(self.worker.calls('window.act')[-1]['base_rev'], second)
        saved = json.loads(path.read_text())
        self.assertEqual(saved['rev'], shown(result)['rev'])
        self.assertNotIn('lease', saved)
        self.assertNotIn('pending', saved)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_cut_errors_are_one_json_and_nonzero(self):
        code, result = self.cut('act', {'steps': [{}]})
        self.assertEqual(code, 1)
        self.assertEqual(result, dict(ok=False, error='observe_required'))

    def test_mcp_stdio_real_subprocess_initialize_tools_and_tool_call(self):
        original = subprocess.Popen
        channel = self.worker.client_socket()
        self.addCleanup(channel.close)
        def spawn(argv, **kwargs):
            self.assertNotIn('CU_MODEL_API_KEY', kwargs['env'])
            return original([sys.executable, '-c', CHILD_BOOT, str(WM), str(self.sd), str(channel.fileno()), *argv[1:]],
                            pass_fds=(channel.fileno(),), **kwargs)
        with patch.dict(os.environ, CU_MODEL_API_KEY='must-not-reach-child'), patch.object(loop.subprocess, 'Popen', side_effect=spawn):
            client = loop.MCPClient('s1', 'path')
        try:
            tools = client.start()
            self.assertEqual({t['name'] for t in tools}, {'observe', 'act', 'use_skill', 'wait', 'done'})
            self.assertEqual(shown(client.tool('observe', {}))['rev'], 11)
            self.assertEqual(shown(client.tool('done', {'status': 'success', 'summary': 'Finished'}))['status'], 'success')
        finally:
            client.close()
        self.assertEqual(len(self.worker.calls('session.attach')), 1)


class RenderingCase(unittest.TestCase):
    def test_receipt_projection_is_bounded_and_omits_raw_receipt(self):
        r = receipt()
        r['steps'][0]['attempts'] = [{'secret': 'x'*100000}]*100
        r['steps'][0]['effect']['evidence'] = [{'raw': 'x'*100000}]
        r['steps'][0]['effect']['predicate'] = {'value': 'x'*100000}
        r['steps'][0]['intent_redacted'] = {'text': 'x'*100000}
        r['steps'][0]['reason'] = 'x'*100000
        r['steps'].append(dict(id='2', effect={'status': 'not_observed'}, route=None, reason='not_reached'))
        r['unexecuted_steps'] = ['2']
        result = a.render_receipt(r)
        self.assertLess(len(a.encoded(result)), 2048)
        self.assertEqual(set(result), {'action_id', 'generation', 'status', 'reason', 'unexecuted_steps', 'state_delta', 'advice', 'rev', 'steps'})
        self.assertEqual(set(result['steps'][0]), {'id', 'effect', 'route', 'reason'})
        self.assertEqual(result['unexecuted_steps'], ['2'])
        self.assertEqual(result['state_delta'], r['state_delta'])
        self.assertEqual(result['rev'], 12)

    def test_failure_frames_survive_projection_without_other_evidence(self):
        r = receipt()
        frame = dict(frame='f:1', path='/tmp/frame.png', scale=1, rect=dict(x=0, y=0, w=1, h=1))
        r['steps'][0]['effect']['evidence'] = [dict(frame, type='frame'), dict(type='tree', text='private')]
        result = a.render_receipt(r)
        self.assertEqual(result['frames'], [frame])
        self.assertNotIn('private', json.dumps(result))

    def test_worker_tree_budgets_remain_in_force(self):
        # Real worker renderer, fake snapshot. This is the tree data the adapter
        # forwards; no second truncation can invalidate the worker's ref baseline.
        from worker_tree import TreeCache
        tree = TreeCache()
        nodes = [dict(key=str(i), role='push_button', name='x'*100, showing=True, visible=True, states=['enabled'],
                      bounds=[1,1,10,10], children=[]) for i in range(250)]
        # TreeCache's public snapshot shape is covered by W4; build it below
        # through the same update method rather than mutating cached lines.
        root = dict(key='root', role='frame', name='root', showing=True, visible=True, states=[], bounds=[0,0,20,20], children=nodes)
        tree.update('w1', root, 1)
        page = tree.page('w1')
        self.assertTrue(page['truncated'])
        self.assertGreater(len(page['lines']), 1)
        self.assertLessEqual(len(page['lines']), 200)
        self.assertLessEqual(len(a.encoded(page)), 16384)
        small = copy.deepcopy(root)
        small['children'] = small['children'][:20]
        tree.update('w2', small, 2)
        small['children'][0]['name'] = 'renamed'
        tree.update('w2', small, 3)
        delta = tree.page('w2', since=2)
        self.assertIn('delta', delta)
        self.assertLessEqual(sum(map(len, delta['delta'].values())), 40)
        self.assertLessEqual(len(a.encoded(delta)), 2048)

    def test_mcp_parse_unknown_notifications_and_tool_error(self):
        adapter = Mock()
        adapter.call.side_effect = a.AdapterError('observe_required')
        incoming = io.StringIO('\n'.join([json.dumps(dict(jsonrpc='2.0', method='notifications/initialized')),
                    '{bad', json.dumps(dict(jsonrpc='2.0', id=2, method='missing')),
                    json.dumps(dict(jsonrpc='2.0', id=3, method='tools/call', params={'name':'act'})),
                    json.dumps(dict(jsonrpc='2.0', id=4, method='initialize', params=[]))])+'\n')
        out = io.StringIO()
        a.mcp_serve(adapter, incoming, out)
        rows = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[1]['error']['code'], -32601)
        self.assertTrue(rows[2]['result']['isError'])
        self.assertEqual(shown(rows[2]['result']), {'error': 'observe_required'})


class ProviderCase(unittest.TestCase):
    def config(self, provider='openai'):
        return loop.provider_config(dict(CU_MODEL_PROVIDER=provider, CU_MODEL='vision-model', CU_MODEL_API_KEY='private-key'))

    def history(self):
        return [dict(role='system', content='System'), dict(role='user', content='Task'),
                dict(role='assistant', content=None, tool_calls=[dict(id='c1', type='function', function=dict(name='observe', arguments='{}'))]),
                dict(role='tool', tool_call_id='c1', content=[dict(type='text', text='state'), dict(type='image', mimeType='image/png', data='eA==')])]

    def test_all_presets_defaults_overrides_and_validation(self):
        for preset, (server, default) in loop.PRESETS.items():
            with self.subTest(provider=preset):
                env = dict(CU_MODEL_PROVIDER=preset, CU_MODEL_API_KEY='private-key')
                if not default:
                    env['CU_MODEL'] = 'chosen'
                c = loop.provider_config(env)
                self.assertEqual((c['server'], c['model']), (server, default or 'chosen'))
                env.update(CU_MODEL_SERVER='http://local:9/v1/', CU_MODEL='override')
                c = loop.provider_config(env)
                self.assertEqual((c['server'], c['model']), ('http://local:9/v1', 'override'))
        self.assertEqual(loop.provider_config({})['model'], 'local-model')
        for env in ({'CU_MODEL_PROVIDER':'unknown'}, {'CU_MODEL_PROVIDER':'openai'},
                    {'CU_MODEL_PROVIDER':'deepseek'}, {'CU_MODEL_SERVER':'file:///tmp/a'}):
            with self.subTest(env=env), self.assertRaises(a.AdapterError):
                loop.provider_config(env)

    def test_openai_tool_definitions_results_and_images(self):
        body = loop.request_body(self.config(), a.TOOLS, self.history())
        self.assertEqual(body['tools'][0]['function']['parameters'], a.TOOLS[0]['inputSchema'])
        self.assertEqual(body['tools'][0]['type'], 'function')
        self.assertEqual(body['messages'][3], dict(role='tool', tool_call_id='c1', content='state'))
        self.assertEqual(body['messages'][4]['content'][0]['image_url']['url'], 'data:image/png;base64,eA==')
        self.assertNotIn('private-key', json.dumps(body))
        self.assertNotIn('n_predict', body)
        reply = dict(choices=[dict(message=dict(role='assistant', tool_calls=[{'id': 'x'}]))], usage={'prompt_tokens': 7})
        msg, usage = loop.normalize_response(self.config(), reply)
        self.assertEqual(msg['tool_calls'][0]['id'], 'x')
        self.assertEqual(usage['prompt_tokens'], 7)

    def test_effort_defaults_to_medium_for_hosted_chat_presets(self):
        for preset in ('openai', 'deepseek', 'openrouter'):
            with self.subTest(preset=preset):
                config = self.config(preset)
                body = loop.request_body(config, a.TOOLS, self.history())
                self.assertEqual(body['reasoning_effort'], 'medium')
                self.assertFalse(config['effort_fallback'])

    def test_explicit_effort_values_are_honored(self):
        for preset in ('openai', 'deepseek', 'openrouter'):
            for effort in ('low', 'medium', 'high'):
                with self.subTest(preset=preset, effort=effort):
                    config = loop.provider_config(dict(CU_MODEL_PROVIDER=preset, CU_MODEL='model',
                        CU_MODEL_API_KEY='private-key', CU_MODEL_EFFORT=effort))
                    self.assertEqual(loop.request_body(config, [], [])['reasoning_effort'], effort)
                    self.assertFalse(config['effort_fallback'])

    def test_none_omits_effort_parameter(self):
        for preset in ('openai', 'deepseek', 'openrouter'):
            with self.subTest(preset=preset):
                config = loop.provider_config(dict(CU_MODEL_PROVIDER=preset, CU_MODEL='model',
                    CU_MODEL_API_KEY='private-key', CU_MODEL_EFFORT='none'))
                self.assertNotIn('reasoning_effort', loop.request_body(config, [], []))
                self.assertEqual(config['effort'], 'none')
                self.assertFalse(config['effort_fallback'])

    def test_empty_and_invalid_effort_fall_back_to_medium(self):
        for effort in ('', 'invalid', 'HIGH', ' medium '):
            with self.subTest(effort=effort):
                config = loop.provider_config(dict(CU_MODEL_PROVIDER='openai', CU_MODEL='model',
                    CU_MODEL_API_KEY='private-key', CU_MODEL_EFFORT=effort))
                self.assertEqual(loop.request_body(config, [], [])['reasoning_effort'], 'medium')
                self.assertTrue(config['effort_fallback'])

    def test_llama_never_sends_effort(self):
        baseline = loop.request_body(self.config('llama'), a.TOOLS, self.history())
        self.assertNotIn('reasoning_effort', baseline)
        for effort in ('low', 'medium', 'high', 'none', '', 'invalid'):
            with self.subTest(effort=effort):
                config = loop.provider_config(dict(CU_MODEL_PROVIDER='llama', CU_MODEL='vision-model',
                    CU_MODEL_EFFORT=effort))
                self.assertEqual(config['effort'], 'none')
                self.assertEqual(loop.request_body(config, a.TOOLS, self.history()), baseline)

    def test_anthropic_payload_unchanged_by_effort(self):
        expected = dict(model='model', messages=[], max_tokens=4096, temperature=.1, top_p=1,
                        system='System', tools=[])
        for effort in (None, 'low', 'medium', 'high', 'none', '', 'invalid'):
            with self.subTest(effort=effort):
                env = dict(CU_MODEL_PROVIDER='anthropic', CU_MODEL='model', CU_MODEL_API_KEY='private-key')
                if effort is not None:
                    env['CU_MODEL_EFFORT'] = effort
                config = loop.provider_config(env)
                self.assertEqual(config['effort'], 'none')
                self.assertEqual(loop.request_body(config, [], [dict(role='system', content='System')]), expected)

    def test_openai_multi_tool_images_follow_all_matching_results(self):
        h = self.history()
        h[2]['tool_calls'].append(dict(id='c2', type='function', function=dict(name='wait', arguments='{}')))
        h.append(dict(role='tool', tool_call_id='c2', content=[dict(type='text', text='waited')]))
        messages = loop.request_body(self.config(), a.TOOLS, h)['messages']
        self.assertEqual([m['role'] for m in messages], ['system', 'user', 'assistant', 'tool', 'tool', 'user'])
        self.assertEqual(messages[4]['tool_call_id'], 'c2')

    def test_anthropic_tool_and_content_translation(self):
        c = self.config('anthropic')
        h = self.history()
        h[3]['is_error'] = True
        body = loop.request_body(c, a.TOOLS, h)
        self.assertEqual(body['system'], 'System')
        self.assertEqual(body['tools'][0]['input_schema'], a.TOOLS[0]['inputSchema'])
        self.assertEqual(body['messages'][1]['content'], [dict(type='tool_use', id='c1', name='observe', input={})])
        result = body['messages'][2]['content'][0]
        self.assertEqual((result['type'], result['tool_use_id'], result['is_error']), ('tool_result','c1',True))
        self.assertEqual(result['content'][1], dict(type='image', source=dict(type='base64', media_type='image/png', data='eA==')))
        response = dict(content=[dict(type='text', text='Done'), dict(type='tool_use', id='c9', name='done', input={'status':'success','summary':'OK'})], usage={'input_tokens': 9, 'output_tokens': 4})
        msg, usage = loop.normalize_response(c, response)
        self.assertEqual(json.loads(msg['tool_calls'][0]['function']['arguments'])['status'], 'success')
        self.assertEqual(usage, response['usage'])

    def test_text_only_deepseek_omits_image_blocks_and_llama_extensions(self):
        body = loop.request_body(self.config('deepseek'), a.TOOLS, self.history())
        self.assertNotIn('image_url', json.dumps(body))
        self.assertNotIn('cache_prompt', body)
        llama = loop.request_body(self.config('llama'), a.TOOLS, self.history())
        self.assertTrue(llama['cache_prompt'])
        self.assertIn('n_predict', llama)

    def test_provider_http_wire_headers_url_and_no_key_in_body(self):
        for preset, suffix, header in [('openai','/v1/chat/completions','Authorization'),
                                       ('anthropic','/v1/messages','X-api-key'),
                                       ('openrouter','/api/v1/chat/completions','Authorization')]:
            c = self.config(preset)
            result = dict(content=[], usage={}) if preset == 'anthropic' else dict(choices=[dict(message=dict(role='assistant', content='text'))])
            with patch.object(loop.urllib.request, 'urlopen') as open_url:
                open_url.return_value.__enter__.return_value = io.BytesIO(json.dumps(result).encode())
                loop.Provider(c).complete(a.TOOLS, self.history())
            req = open_url.call_args.args[0]
            self.assertTrue(req.full_url.endswith(suffix))
            self.assertIn('private-key', req.get_header(header))
            self.assertNotIn(b'private-key', req.data)
            if preset == 'anthropic':
                self.assertEqual(req.get_header('Anthropic-version'), '2023-06-01')

    def test_server_error_body_or_url_never_leaks_credentials(self):
        error = loop.urllib.error.HTTPError('https://private-key/', 401, 'private-key', {}, io.BytesIO(b'private-key'))
        with patch.object(loop.urllib.request, 'urlopen', side_effect=error), self.assertRaisesRegex(a.AdapterError, '^provider_http_401$'):
            loop.Provider(self.config()).complete(a.TOOLS, self.history())


class LoopCase(unittest.TestCase):
    @staticmethod
    def call(name, **arguments):
        return dict(role='assistant', content=None, tool_calls=[dict(id='c1', type='function', function=dict(name=name, arguments=json.dumps(arguments)))])

    def setup_loop(self, responses):
        client = Mock()
        client.start.return_value = a.TOOLS
        def tool(name, arguments):
            return dict(content=[dict(type='text', text=json.dumps(arguments if name == 'done' else {'rev': 2}))], isError=False)
        client.tool.side_effect = tool
        provider = Mock()
        provider.config = dict(images=True)
        provider.complete.side_effect = responses
        return client, provider

    def test_done_stops_immediately_and_aggregates_tokens(self):
        c, p = self.setup_loop([(self.call('observe'), {'prompt_tokens': 2, 'completion_tokens': 3}),
                                (self.call('done', status='success', summary='Verified'), {'prompt_tokens': 4, 'completion_tokens': 5})])
        result = loop.run_loop(c, p, 'Rename folder')
        self.assertEqual((result['turns_used'], result['tool_calls_made']), (2, 2))
        self.assertEqual(result['done'], dict(status='success', summary='Verified'))
        self.assertEqual(result['token_usage'], dict(prompt_tokens=6, completion_tokens=8))
        self.assertEqual(p.complete.call_count, 2)

    def test_run_summary_reports_effective_effort_and_fallback(self):
        for preset in loop.PRESETS:
            for effort in (None, 'low', 'high', 'none', '', 'invalid'):
                for failed in (False, True):
                    with self.subTest(preset=preset, effort=effort, failed=failed):
                        env = dict(CU_MODEL_PROVIDER=preset, CU_MODEL='model', CU_MODEL_API_KEY='private-key')
                        if effort is not None:
                            env['CU_MODEL_EFFORT'] = effort
                        c, p = self.setup_loop([(self.call('done', status='success', summary='Verified'), {})])
                        p.config = loop.provider_config(env)
                        if failed:
                            p.complete.side_effect = a.AdapterError('provider_http_429')
                        with patch.object(loop, 'check_kill'):
                            summary = loop.run_loop(c, p, 'Task')
                        expected = 'none' if preset in ('llama', 'anthropic') else effort if effort in ('low', 'high', 'none') else 'medium'
                        self.assertEqual(summary['effort'], expected)
                        self.assertEqual('notes' in summary, effort in ('', 'invalid'))
                        if 'notes' in summary:
                            self.assertIn('CU_MODEL_EFFORT empty or invalid; using default medium', summary['notes'][0])
                        self.assertEqual(summary['reason'], 'provider_http_429' if failed else None)

    def test_startup_failure_summary_retains_effort_fallback_note(self):
        env = dict(CU_MODEL_PROVIDER='openai', CU_MODEL='model', CU_MODEL_API_KEY='private-key', CU_MODEL_EFFORT='')
        with patch.object(sys, 'argv', ['ark-loop', '--session', 's1', '--task', 'Task']), \
                patch.dict(os.environ, env, clear=True), patch.object(loop, 'check_kill'), \
                patch.object(loop, 'MCPClient', side_effect=OSError), patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(loop.main(), 1)
        summary = json.loads(out.getvalue())
        self.assertEqual(summary['effort'], 'medium')
        self.assertIn('using default medium', summary['notes'][0])
        self.assertEqual(summary['reason'], 'loop_start_failed')

    def test_hard_turn_cap_40_even_if_requested_higher(self):
        c, p = self.setup_loop([(self.call('observe'), {})]*50)
        result = loop.run_loop(c, p, 'Task', 99)
        self.assertEqual((result['turns_used'], result['tool_calls_made'], result['max_turns']), (40, 40, 40))
        self.assertEqual(result['reason'], 'turn_cap')
        self.assertIsNone(result['done'])

    def test_lower_cap_and_natural_model_stop_are_reported(self):
        c, p = self.setup_loop([(self.call('observe'), {})]*3)
        self.assertEqual(loop.run_loop(c, p, 'Task', 2)['turns_used'], 2)
        c, p = self.setup_loop([(dict(role='assistant', content='finished'), {})])
        self.assertEqual(loop.run_loop(c, p, 'Task')['reason'], 'model_stopped_without_done')

    def test_done_error_does_not_stop_and_following_tool_stops_after_done(self):
        first = self.call('done', status='success', summary='Try')
        second = self.call('done', status='blocked', summary='Human needed')
        second['tool_calls'].append(self.call('act', steps=[])['tool_calls'][0])
        c, p = self.setup_loop([(first, {}), (second, {})])
        c.tool.side_effect = [dict(isError=True, content=[dict(type='text', text='error')]),
                              dict(isError=False, content=[dict(type='text', text=json.dumps({'status':'blocked','summary':'Human needed'}))])]
        result = loop.run_loop(c, p, 'Task')
        self.assertEqual(result['done']['status'], 'blocked')
        self.assertEqual(c.tool.call_count, 2)

    def test_frames_count_only_images_delivered_on_next_model_request(self):
        for supports in (True, False):
            c, p = self.setup_loop([(self.call('observe'), {}), (self.call('done', status='success', summary='OK'), {})])
            p.config['images'] = supports
            original = c.tool.side_effect
            def tool(name, args):
                value = original(name, args)
                if name == 'observe':
                    value['content'].append(dict(type='image', data='eA==', mimeType='image/png'))
                return value
            c.tool.side_effect = tool
            self.assertEqual(loop.run_loop(c, p, 'Task')['frames_viewed'], int(supports))
            c, p = self.setup_loop([(self.call('observe'), {})])
            c.tool.return_value = dict(content=[dict(type='image', data='eA==')])
            c.tool.side_effect = None
            self.assertEqual(loop.run_loop(c, p, 'Task', 1)['frames_viewed'], 0)

    def test_failures_and_unknown_tools_have_safe_summary(self):
        c, p = self.setup_loop([(self.call('unknown'), {})])
        self.assertEqual(loop.run_loop(c, p, 'Task')['reason'], 'unknown_model_tool')
        c, p = self.setup_loop([])
        p.complete.side_effect = a.AdapterError('provider_http_429')
        self.assertEqual(loop.run_loop(c, p, 'Task')['reason'], 'provider_http_429')

    def transcript_run(self, responses, results=None, *, enabled=True):
        temp = tempfile.TemporaryDirectory(prefix='loop-transcript-')
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / 'calls.jsonl'
        c, p = self.setup_loop(responses)
        if results is not None:
            c.tool.side_effect = results
        with patch.dict(os.environ), patch.object(loop, 'check_kill'):
            os.environ.pop('CU_LOOP_TRANSCRIPT', None)
            if enabled:
                os.environ['CU_LOOP_TRANSCRIPT'] = str(path)
            summary = loop.run_loop(c, p, 'Task', max_turns=len(responses))
        entries = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return summary, entries, path

    def test_transcript_records_act_receipt_and_done_without_payloads(self):
        act = receipt()
        act['state_now'] = {'frames': [{'data': 'PRIVATE FRAME'}]}
        act['steps'][0]['effect']['evidence'] = ['PRIVATE EVIDENCE']
        act['steps'][0]['status'] = 'completed'
        result = dict(isError=False, content=[dict(type='text', text=json.dumps(act)),
                      dict(type='image', data='PRIVATE IMAGE')])
        summary, entries, _ = self.transcript_run(
            [(self.call('act'), {}), (self.call('done', status='success', summary='OK'), {})],
            [result, dict(isError=False, content=[dict(type='text', text=json.dumps(
                dict(status='success', summary='OK')))])])
        self.assertEqual(summary['done']['status'], 'success')
        self.assertEqual(entries, [dict(turn=1, tool='act', is_error=False, status='success',
            reason=None, detail=None, steps=[dict(id='1', status='completed', reason=None,
                                                 effect=dict(status='verified'))]),
            dict(turn=2, tool='done', is_error=False, status='success', reason=None, detail=None)])

    def test_transcript_records_failed_act_and_step_reasons(self):
        act = receipt()
        act.update(status='failed', reason='invalid_params')
        act['steps'][0].update(reason='invalid_expectation')
        _, entries, _ = self.transcript_run([(self.call('act'), {})],
            [dict(isError=False, content=[dict(type='text', text=json.dumps(act))])])
        self.assertEqual(entries[0]['status'], 'failed')
        self.assertEqual(entries[0]['reason'], 'invalid_params')
        self.assertFalse(entries[0]['is_error'])
        self.assertEqual(entries[0]['steps'][0]['reason'], 'invalid_expectation')

    def test_transcript_records_observe_revision_or_error_and_bounds_strings(self):
        results = [dict(isError=False, content=[dict(type='text', text=json.dumps(
                       dict(rev=12, state={'private': 'DO NOT RECORD'}, frames=['PRIVATE FRAME'])))]),
                   dict(isError=True, content=[dict(type='text', text=json.dumps(
                       dict(error='invalid_request', detail='x' * 600)))])]
        _, entries, _ = self.transcript_run([(self.call('observe'), {})] * 2, results)
        self.assertEqual(entries[0], dict(turn=1, tool='observe', is_error=False, status=None,
                                         reason=None, detail=None, rev=12))
        self.assertEqual(entries[1], dict(turn=2, tool='observe', is_error=True, status=None,
                                         reason='invalid_request', detail='x' * 500, rev=None))

    def test_transcript_records_malformed_calls_and_unknown_tools_before_failure(self):
        malformed = self.call('act')
        malformed['tool_calls'][0]['function']['arguments'] = '{broken'
        for response, reason, summary_reason in (
                (malformed, 'malformed_call', 'loop_failure'),
                (dict(role='assistant', tool_calls=[{}]), 'malformed_call', 'loop_failure'),
                (self.call([]), 'malformed_call', 'loop_failure'),
                (a.AdapterError('invalid_provider_response'), 'malformed_call', 'invalid_provider_response'),
                (self.call('unknown'), 'unknown_tool', 'unknown_model_tool')):
            with self.subTest(reason=reason, response=response):
                summary, entries, _ = self.transcript_run(
                    [response if isinstance(response, Exception) else (response, {})])
                self.assertEqual(summary['reason'], summary_reason)
                self.assertEqual(entries, [dict(turn=1, tool=None, is_error=True,
                    status=None, reason=reason, detail=None)])
                self.assertEqual(summary['tool_calls_made'], 0)
        missing_id = self.call('observe')
        del missing_id['tool_calls'][0]['id']
        summary, entries, _ = self.transcript_run([(missing_id, {})])
        self.assertEqual(summary['reason'], 'loop_failure')
        self.assertEqual(summary['tool_calls_made'], 1)
        self.assertEqual(entries[0]['reason'], 'malformed_call')
        self.assertIsNone(entries[0]['tool'])

    def test_transcript_unset_creates_no_file_and_unwritable_does_not_change_summary(self):
        responses = [(self.call('done', status='success', summary='OK'), {})]
        summary, entries, path = self.transcript_run(responses, enabled=False)
        self.assertFalse(path.exists())
        self.assertEqual(entries, [])
        c, p = self.setup_loop(responses)
        with patch.dict(os.environ, CU_LOOP_TRANSCRIPT=str(path / 'missing' / 'calls.jsonl')), \
                patch.object(loop, 'check_kill'):
            self.assertEqual(loop.run_loop(c, p, 'Task', max_turns=1), summary)

    def test_transcript_appends_and_is_visible_before_next_call(self):
        with tempfile.TemporaryDirectory(prefix='loop-transcript-') as directory:
            path = Path(directory) / 'calls.jsonl'
            path.write_text('{"prior":true}\n')
            c, p = self.setup_loop([(self.call('observe'), {}),
                                   (self.call('done', status='success', summary='OK'), {})])
            original = c.tool.side_effect
            def tool(name, args):
                if name == 'done':
                    rows = [json.loads(line) for line in path.read_text().splitlines()]
                    self.assertEqual(rows[0], dict(prior=True))
                    self.assertEqual(rows[1]['tool'], 'observe')
                return original(name, args)
            c.tool.side_effect = tool
            with patch.dict(os.environ, CU_LOOP_TRANSCRIPT=str(path)), patch.object(loop, 'check_kill'):
                loop.run_loop(c, p, 'Task')
            self.assertEqual(len(path.read_text().splitlines()), 3)

    def test_cli_exit_codes_done_success_failed_blocked_and_cap(self):
        for status, expected in [('success', 0), ('failed', 1), ('blocked', 1), (None, 1)]:
            summary = dict(done={'status':status,'summary':'Final'} if status else None, reason=None if status else 'turn_cap')
            with patch.object(sys, 'argv', ['ark-loop', '--session','s1','--task','Task']), patch.dict(os.environ, CU_MODEL_PROVIDER='llama'), patch.object(loop, 'MCPClient') as client, patch.object(loop, 'run_loop', return_value=summary), patch('sys.stdout', new_callable=io.StringIO) as out:
                self.assertEqual(loop.main(), expected)
                self.assertEqual(json.loads(out.getvalue()), summary)
                client.return_value.close.assert_called_once()

    def test_cli_rejects_credential_arguments_without_echoing_them(self):
        for main, argv in [(loop.main, ['ark-loop','--session','s1','--task','Task','--api-key','private-key']),
                           (a.main, ['ark-act','call','observe','{}','--session','s1','--api-key','private-key'])]:
            with patch.object(sys, 'argv', argv), patch('sys.stdout', new_callable=io.StringIO) as out, patch('sys.stderr', new_callable=io.StringIO) as err:
                self.assertEqual(main(), 1)
                self.assertNotIn('private-key', out.getvalue() + err.getvalue())
                self.assertIn('invalid_arguments', out.getvalue())
                json.loads(out.getvalue())

    def test_invalid_config_fails_before_subprocess_and_key_is_not_cli(self):
        with patch.object(sys, 'argv', ['ark-loop','--session','s1','--task','Task']), patch.dict(os.environ, CU_MODEL_PROVIDER='bad', CU_MODEL_API_KEY='private-key'), patch.object(loop, 'MCPClient') as client, patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(loop.main(), 1)
            self.assertEqual(json.loads(out.getvalue())['reason'], 'unknown_provider')
            self.assertNotIn('private-key', out.getvalue())
            client.assert_not_called()


class PixelDefaultsCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='w5-pixels-')
        self.addCleanup(temp.cleanup)
        sd = Path(temp.name)
        owner = self.owner = w.Worker.__new__(w.Worker)
        owner.sd, owner.state, owner.sid, owner.generation = sd, sd, 's1', GEN
        owner.cache = w.StateCache('s1', GEN)
        owner.cache.upsert(10, window(geometry=dict(x=0,y=0,w=1920,h=1080)))
        owner.cache.baseline()
        owner.covered = lambda: True
        owner.providers, owner.coverage = lambda: {}, lambda: {}
        owner.x = NS(ax=NS(pending=[]), refreshing=set(), uncovered=set(), initialised=True)
        shadow = Shadow(1920,1080)
        self.addCleanup(shadow.close)
        owner.pixels = self.pixels = Pixels(owner, shadow)
        # Synthetic captured desktop; no X transport is involved in these tests.
        self.pixels.dirty.clear()
        self.pixels.hashes = [b'captured'] * len(self.pixels.rects)

    def test_1920_observe_default_is_640_360_scale_mapping_and_native_tiles(self):
        result = self.owner.observation({'channels':['pixels']})
        frame = result['frames'][0]
        self.assertEqual(decode(frame['path'])[:2], (640,360))
        self.assertEqual(frame['scale'], 1/3)
        self.assertEqual(self.pixels.desktop_point(dict(space='frame:'+frame['frame'], x=300, y=200), frame['rev']), (900,600))
        self.assertEqual(len(self.pixels.rects), 30*17)
        self.assertEqual(self.pixels.rects[0], dict(x=0,y=0,w=64,h=64))
        self.assertEqual(self.pixels.tiles(dict(x=64,y=64,w=1,h=1)), {31})

    def test_explicit_native_and_explicit_scale_unchanged(self):
        for scale, size in [(1,(1920,1080)), (.5,(960,540))]:
            frame = self.owner.observation({'channels':['pixels'], 'scale':scale})['frames'][0]
            self.assertEqual(decode(frame['path'])[:2], size)
            self.assertEqual(frame['scale'], scale)

    def test_receipt_failure_evidence_stays_native_for_all_uncertain_effects(self):
        for status in ('not_observed','contradicted','unknown'):
            r = receipt()
            r['steps'][0].update(disposition='executed', target_resolved=self.owner.cache.handle(10))
            r['steps'][0]['effect']['status'] = status
            self.pixels.receipt_frames(r)
            frame = r['steps'][0]['effect']['evidence'][0]
            self.assertEqual(frame['scale'], 1)
            self.assertEqual(decode(frame['path'])[:2], (1920,1080))

    def test_default_portrait_and_small_regions_fit_box_without_upscale(self):
        for rect, size in [(dict(x=0,y=0,w=200,h=1000),(72,360)),
                           (dict(x=0,y=0,w=100,h=50),(100,50))]:
            frame = self.pixels.frame(rect, [])
            self.assertEqual(decode(frame['path'])[:2], size)


if __name__ == '__main__':
    unittest.main()
