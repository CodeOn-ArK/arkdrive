"""W5 driver adapter. Stdlib only; no desktop/runtime imports.

Worker RPC uses length-prefixed JSON; MCP stdio uses newline-delimited JSON.
Attach metadata is private: only results actually returned advance last_rev.
"""
import argparse
import base64
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import socket
import struct
import sys
import time
import uuid

from packs import REGISTRY

MAX_FRAME = 256 * 1024


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()


class AdapterError(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class WorkerError(AdapterError):
    pass


def step_form_hint():
    return dict(type='text', text=encoded({'hint':
        "invalid step form. Tree expects need provider 'atspi' plus a role/name selector. "
        'Valid expect kinds: window_present, window_absent, dialog_present, dialog_absent, '
        'window_focused, pointer_at, node_present, node_absent, text_equals, text_contains, '
        'node_state, item_present, item_absent. A mutating step needs one strong expect. '
        'Fix the form; do not resend unchanged.'}).decode())


def render_error(reason, name, exc):
    content = [dict(type='text', text=encoded({'error': reason}).decode())]
    if name == 'act' and isinstance(exc, WorkerError) and reason == 'invalid_params':
        content.append(step_form_hint())
    return dict(isError=True, content=content)


def session_dir(session):
    if not re.fullmatch(r's[1-9][0-9]*', session):
        raise AdapterError('invalid_session')
    # Same account-database root as ark-worker; never trust inherited HOME.
    root = Path(pwd.getpwuid(os.getuid()).pw_dir) / '.local/state/ark-sessiond'
    try:
        registry = json.loads((root / 'sessions.json').read_text())
        record = registry.get(session) if isinstance(registry, dict) else None
        path = record.get('session_dir') if isinstance(record, dict) else None
        if isinstance(path, str) and path:
            return Path(path)
    except (OSError, ValueError):
        pass
    return root / 'run' / session


def check_kill():
    if Path(os.environ.get('CU_KILL_FILE', '/tmp/cu-executor.kill')).exists():
        raise AdapterError('ABORTED_KILLED')


class WorkerRPC:
    def __init__(self, path):
        self.path, self.sock, self.ident = path, None, 0

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def connect(self):
        self.close()
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(3)
        try:
            self.sock.connect(str(self.path))
        except Exception:
            self.close()
            raise

    def read(self, size):
        value = bytearray()
        while len(value) < size:
            part = self.sock.recv(size-len(value))
            if not part:
                raise EOFError('worker_disconnected')
            value.extend(part)
        return value

    def call(self, method, params, timeout=12):
        try:
            return self._call(method, params, timeout)
        except WorkerError:
            raise
        except (AdapterError, OSError, EOFError, ValueError, KeyError, TypeError):
            self.close()
            raise

    def prepare(self, method, params):
        body = encoded(dict(jsonrpc='2.0', id=self.ident + 1, method=method, params=params))
        if len(body) > MAX_FRAME:
            raise AdapterError('request_too_large')
        return body

    def _call(self, method, params, timeout=12):
        body = self.prepare(method, params)
        self.ident += 1
        self.sock.settimeout(timeout)
        # Any sendall failure is ambiguous too. Callers must never replay act.
        self.sock.sendall(struct.pack('!I', len(body)) + body)
        size = struct.unpack('!I', self.read(4))[0]
        if not 0 < size <= MAX_FRAME:
            raise AdapterError('invalid_worker_frame')
        response = json.loads(self.read(size))
        if not isinstance(response, dict):
            raise AdapterError('invalid_worker_response')
        if response.get('id') != self.ident or response.get('jsonrpc') != '2.0':
            raise AdapterError('invalid_worker_response')
        if 'error' in response:
            raise WorkerError(response['error'].get('data', {}).get('reason', 'worker_error'))
        return response['result']


def frame_refs(value):
    found = {}
    def visit(item):
        if isinstance(item, dict):
            if 'frame' in item and isinstance(item.get('path'), str):
                found[item['frame']] = {k: item[k] for k in
                    ('frame', 'path', 'rev', 'rect', 'scale', 'space') if k in item}
            for v in item.values():
                visit(v)
        elif isinstance(item, list):
            for v in item:
                visit(v)
    visit(value)
    return list(found.values())


def render_receipt(receipt):
    """Project receipt v2, excluding attempts, intents, predicates and raw evidence."""
    def short(value):
        return value[:512] if isinstance(value, str) else value
    result = {k: receipt.get(k) for k in
              ('action_id', 'generation', 'status', 'reason', 'unexecuted_steps', 'state_delta', 'advice')}
    result['reason'] = short(result['reason'])
    result['rev'] = receipt.get('rev', {}).get('final')
    result['steps'] = [dict(id=s['id'], effect={k: short(s['effect'][k]) for k in
                         ('status', 'predicate_class', 'expect_source', 'assurance', 'fallback_from') if k in s['effect']},
                         route=short(s.get('route')), reason=short(s.get('reason')))
                       for s in receipt.get('steps', [])[:8]]
    frames = frame_refs(receipt)
    if frames:
        result['frames'] = frames
    return result


def render_observe(observation):
    """Compact model content only; leave worker evidence and tree/state intact."""
    result = {k: v for k, v in observation.items() if k not in ('coverage', 'channels_used')}
    if 'pixels_changed' in result:
        def pixels(value):
            summary = {k: value[k] for k in ('bbox', 'window') if k in value}
            summary['tiles'] = len(value.get('tiles', []))
            if 'windows' in value:
                summary['windows'] = [pixels(window) for window in value['windows']]
            return summary
        result['pixels_changed'] = pixels(result['pixels_changed'])
    if 'providers' in result:
        def status(value):
            return {k: value[k] for k in ('state', 'reason') if k in value}
        providers = {}
        for name, value in result['providers'].items():
            if not isinstance(value, dict):
                providers[name] = value
                continue
            compact = status(value)
            if 'apps' in value:
                compact['apps'] = {}
                for app, detail in value['apps'].items():
                    health = status(detail)
                    if 'window_status' in detail:
                        health['window_status'] = {window: status(info) for window, info in detail['window_status'].items()}
                    compact['apps'][app] = health
            providers[name] = compact
        result['providers'] = providers
    return result


def session_ready(params, result):
    """Match the worker's session observed-state admission gate."""
    return bool(params.get('scope', {}).get('kind', 'session') == 'session'
                and result.get('full') and result.get('settled')
                and not result.get('resync_required'))


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # Do not echo argv: a mistaken credential argument must not be logged.
        raise AdapterError('invalid_arguments')


def schema(properties, required=()):
    return dict(type='object', properties=properties, required=list(required), additionalProperties=False)


POINT = dict(type='object', description='Named coordinate space: desktop, window:<handle>, or frame:<frame id>. Native desktop pixels unless a frame space is named.',
             properties=dict(space={'type': 'string'}, x={'type': 'integer'}, y={'type': 'integer'}), required=['space', 'x', 'y'])
AT = dict(type='object', description='Exactly one of {ref:"n12"}, {point:{space,x,y}}, {text:"Open",nth:0,within:"<window>"}. Use refs from the last observation.',
          properties=dict(ref={'type': 'string'}, point=POINT, text={'type': 'string'}, nth={'type': 'integer'}, within={'type': 'string'}))
PREDICATE = dict(type='object', description='Strong worker predicate. State match uses title/title_re/class/type; tree match uses role/name/name_re. text_equals uses ref or match plus value. item_present/absent uses container and name. Composite all/any has of:[2–4 non-composite predicates]. reaction is not proof and is rejected.',
                properties=dict(kind={'type': 'string'}, match={'type': 'object'}, window={'type': ['string', 'object']},
                                ref={'type': 'string'}, value={}, within={'type': 'string'}, state={'type': 'string'},
                                text={'type': 'string'}, re={'type': 'string'}, region={'type': 'object'},
                                container={}, name={'type': 'string'}, point=POINT, bind={'type': 'object'},
                                of={'type': 'array', 'items': {'type': 'object'}}), required=['kind'])
INTENT = dict(type='object', description='One intent. key sends one chord as keysyms, e.g. ["Control_L","Shift_L","n"] or ["F2"], never "Ctrl+n". text uses text/replace/via. click/move/scroll use at. invoke uses ref/action. focus_window/close_window use window.',
              properties=dict(kind={'type': 'string', 'enum': ['key', 'text', 'click', 'move', 'drag', 'scroll', 'invoke', 'focus_window', 'close_window', 'wait']},
                              keys={'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 4},
                              text={'type': 'string', 'maxLength': 2000}, replace={'type': 'boolean'},
                              via={'type': 'string', 'enum': ['auto', 'keys', 'paste']}, allow_control={'type': 'boolean'},
                              at=AT, button={'type': 'string', 'enum': ['left', 'middle', 'right']}, count={'type': 'integer'},
                              modifiers={'type': 'array', 'items': {'type': 'string'}}, to=AT,
                              dx={'type': 'integer'}, dy={'type': 'integer'}, ref={'type': 'string'},
                              action={'type': 'string'}, window={'type': ['string', 'object']}), required=['kind'])
INTENT['properties']['from'] = AT
TOOLS = [
    dict(name='observe', description='Read fresh session state/tree/pixels. Observe before acting; coordinates are desktop pixels. full resets the delta baseline. Tree pages/deltas retain worker budgets and cursor metadata. The adapter keeps your view current and reconciles automatically after uncertain acts; you do not need full:true for correctness.',
         inputSchema=schema(dict(scope=dict(type='object', properties=dict(kind={'type': 'string', 'enum': ['session', 'window', 'family']}, window={'type': 'string'})),
                                 channels={'type': 'array', 'items': {'type': 'string', 'enum': ['auto', 'state', 'tree', 'ocr', 'pixels']}},
                                 budget=schema(dict(nodes={'type': 'integer', 'minimum': 0,
                                                          'description': 'Tree page node limit (worker cap 200).'},
                                                    bytes={'type': 'integer', 'minimum': 0,
                                                           'description': 'Tree page JSON byte limit (worker cap 16384).'})),
                                 cursor={'type': 'string', 'description': 'Opaque tree continuation from the previous page; retain its scope.'},
                                 region=dict(type='object', description='Rectangle {space,x,y,w,h} in a named coordinate space.'), full={'type': 'boolean'}))),
    dict(name='act', description='Execute 1–8 sequential steps, stopping on an unverified effect. Each step needs explicit strong expect, except move→pointer_at, focus_window→window_focused, close_window→window_absent, text→whole-field text_equals. Never replay uncertain input. The adapter supplies identity and the revision last shown. '
         'Bindings refer to verified earlier steps; do not invent refs for a dialog that has not appeared.',
         inputSchema=schema(dict(steps={'type': 'array', 'minItems': 1, 'maxItems': 8,
                                       'items': {'type': 'object', 'properties': {
                                           'intent': INTENT, 'expect': PREDICATE,
                                           'target': {'type': ['string', 'object'], 'description': 'Window handle or {from_step:"1",name:"dialog"} exported by a verified prior step.'},
                                           'require': {'type': 'object', 'description': 'Optional guards: focus, modal, ref, window. Omission guards unchanged focus/modal.'},
                                           'timeout_ms': {'type': 'integer', 'minimum': 50, 'maximum': 5000}, 'id': {'type': 'string'}}, 'required': ['intent']}},
                                 deadline_ms={'type': 'integer', 'minimum': 1, 'maximum': 10000}), ['steps'])),
    dict(name='use_skill', description=REGISTRY.description(),
         inputSchema=schema(dict(name={'type': 'string'}, args={'type': 'object'}), ['name', 'args'])),
    dict(name='wait', description='Wait without input for a worker predicate; returns fresh state and revision.',
         inputSchema=schema(dict(expect=PREDICATE, timeout_ms={'type': 'integer', 'minimum': 1, 'maximum': 30000}), ['expect', 'timeout_ms'])),
    dict(name='done', description='Finish the task and release the driver lease. Report only verified success.',
         inputSchema=schema(dict(status={'type': 'string', 'enum': ['success', 'failed', 'blocked']}, summary={'type': 'string'}), ['status', 'summary']))]


class Adapter:
    def __init__(self, sd, images='path', state=None, save=None, recovery_seconds=15):
        self.sd, self.images = Path(sd), images
        self.rpc = WorkerRPC(self.sd / 'driver/act.sock')
        self.state = dict(state or {})
        self.save = save or (lambda value: None)
        self.meta = None
        self.ready = False
        self.finished = False
        self.recovery_seconds = recovery_seconds
        self.active_pack = None
        self._pack_windows = {}
        self._pack_focus = None
        self._pack_modal = []

    def close(self):
        self.rpc.close()

    def attach(self):
        check_kill()
        self.rpc.connect()
        until = time.monotonic() + 6
        while True:
            try:
                meta = self.rpc.call('session.attach', {'client': 'codex', 'name': 'ark-act'})
                break
            except WorkerError as exc:
                if exc.reason != 'lease_held' or time.monotonic() >= until:
                    raise
                check_kill()
                time.sleep(.1)
        if self.state.get('generation') != meta['generation']:
            self.state.pop('rev', None)
            self.active_pack = None
            self._pack_windows.clear()
            self._pack_focus = None
            self._pack_modal = []
        self.state['generation'] = meta['generation']
        self.meta, self.ready = meta, False

    def leased(self, params):
        return dict(params, lease=self.meta['lease'], session=self.meta['session'], generation=self.meta['generation'])

    def resolve(self):
        pending = self.state['pending']
        until = time.monotonic() + self.recovery_seconds
        while True:
            check_kill()
            try:
                if self.rpc.sock is None:
                    # action.get is lease-free, including across worker generations.
                    self.rpc.connect()
                    self.meta, self.ready = None, False
                value = self.rpc.call('action.get', pending, timeout=3)
                if value and value.get('status') == 'final':
                    return render_receipt(value['receipt'])
                if value and value.get('version') == 2:
                    return render_receipt(value)
                # pending/not_found are inconclusive while the original request
                # may still be draining. Poll the same ID, never clear or replay it.
            except (OSError, EOFError):
                self.rpc.close()
            if time.monotonic() >= until:
                self.save(self.state)
                raise AdapterError('action_resolution_timeout')
            time.sleep(.05)

    def update_pack(self, params, result):
        """Publish only settled focus; scoped windows never stand in for focus."""
        # Retain delivered deltas even while unsettled: the next settled
        # result can be unchanged and omit all of those window records.
        state = result.get('state', {})
        if result.get('full') and params.get('scope', {}).get('kind', 'session') == 'session':
            self._pack_windows.clear()
        for handle in state.get('removed', []):
            self._pack_windows.pop(handle, None)
        for window in state.get('windows', []):
            self._pack_windows[window['handle']] = window
        if 'focus' in state:
            self._pack_focus = state['focus']
        if 'modal' in state:
            self._pack_modal = state['modal']
        if not result.get('settled') or result.get('resync_required'):
            return
        # Keep one owner through a modal sequence, per the governing plan.
        if self._pack_modal:
            return
        focused = self._pack_windows.get(self._pack_focus, {})
        self.active_pack = REGISTRY.for_class(focused.get('class', ''))

    def use_skill(self, arguments):
        name, args = arguments['name'], arguments['args']
        if not isinstance(name, str) or not isinstance(args, dict):
            raise AdapterError('invalid_arguments')
        if name not in REGISTRY.skills:
            raise AdapterError('unknown_skill: ' + name + '; loaded skills: ' + ', '.join(REGISTRY.skills))
        pack, skill = REGISTRY.skills[name]
        # A one-shot adapter has no cached focus. Every invocation rechecks it.
        observation = self.call('observe', {'full': True, 'scope': {'kind': 'session'},
                                            'channels': ['state']})
        state = json.loads(observation['content'][0]['text'])
        if self.active_pack is not pack:
            active = self.active_pack.NAME if self.active_pack else 'none'
            raise AdapterError('inactive_skill_pack: ' + pack.NAME + '; active pack: ' + active)
        if not state.get('settled') or state.get('resync_required'):
            raise AdapterError('observe_required')
        # A previous interrupted invocation must be recovered, never mistaken for
        # this invocation's mutation (nor followed by another mutation).
        if self.state.get('pending'):
            recovered = json.loads(self.call('act', {'steps': []})['content'][0]['text'])
            result = dict(status=recovered['status'], detail='previous_action_resolved; skill not run')
        else:
            result = skill['run'](self, args)
        return dict(content=[dict(type='text', text=encoded(result).decode())], isError=False)

    def call(self, name, arguments):
        check_kill()
        if self.finished:
            raise AdapterError('session_done')
        tool = next((t for t in TOOLS if t['name'] == name), None)
        if tool is None or not isinstance(arguments, dict):
            raise AdapterError('invalid_tool')
        spec = tool['inputSchema']
        if set(arguments) - set(spec['properties']) or set(spec['required']) - set(arguments):
            raise AdapterError('invalid_arguments')
        if 'full' in arguments and type(arguments['full']) is not bool:
            raise AdapterError('invalid_arguments')
        if name == 'wait' and (type(arguments['timeout_ms']) is not int or not 1 <= arguments['timeout_ms'] <= 30000):
            raise AdapterError('invalid_arguments')
        if name == 'act' and isinstance(arguments['steps'], list):
            for step in arguments['steps']:
                if isinstance(step, dict):
                    for field in ('risk', 'allow_stale_pixels'):
                        if field in step:
                            raise AdapterError(f'{field} is not settable through this tool; resend the batch without it.')
        if name == 'use_skill':
            return self.use_skill(arguments)
        clear_pending = False
        if name == 'act' and self.state.get('pending'):
            result = self.resolve()
            clear_pending = True
        else:
            if self.meta is None or self.rpc.sock is None:
                self.attach()
            since = self.state.get('rev')
            if name == 'observe':
                params = {k: v for k, v in arguments.items() if k != 'full'}
                params['since_rev'] = None if arguments.get('full') else since
                result = self.rpc.call('window.observe', self.leased(params))
                self.ready |= session_ready(params, result)
                self.update_pack(params, result)
            elif name == 'act':
                if since is None:
                    raise AdapterError('observe_required')
                if not self.ready:
                    # New CUT/reconnected lease needs the worker's fresh-observation
                    # handshake. This private read never advances the model baseline;
                    # a newer handshake rev makes the worker reject the old base_rev.
                    handshake = {'since_rev': None, 'channels': ['state']}
                    result = self.rpc.call('window.observe', self.leased(handshake))
                    self.ready = session_ready(handshake, result)
                    self.update_pack(handshake, result)
                    if not self.ready:
                        raise AdapterError('observe_required')
                pending = dict(action_id='a-' + uuid.uuid4().hex, generation=self.meta['generation'])
                params = self.leased(dict(arguments, action_id=pending['action_id'], base_rev=since,
                                          control_epoch=self.meta['control_epoch']))
                self.rpc.prepare('window.act', params)  # Local failures precede ID reservation.
                check_kill()
                self.state['pending'] = pending
                self.save(self.state)  # CUT crash recovery reserves the ID before send.
                try:
                    result = render_receipt(self.rpc.call('window.act', params))
                except (OSError, EOFError):
                    self.rpc.close()
                    result = self.resolve()
                except WorkerError:
                    self.state.pop('pending', None)
                    self.save(self.state)
                    raise
                clear_pending = True
            elif name == 'wait':
                result = self.rpc.call('window.wait', self.leased(dict(expect=arguments['expect'],
                                       since_rev=since, deadline_ms=arguments['timeout_ms'])),
                                       timeout=arguments['timeout_ms']/1000 + 2)
            else:
                self.rpc.call('session.done', self.leased(arguments))
                result = dict(arguments)
                self.finished = True
        observed = False
        if name == 'act' and (result.get('advice') == 'observe_first' or
                              result.get('reason') == 'awaiting_reconciliation' or
                              result.get('status') in ('unknown', 'contradicted')):
            result['reconciled'] = False
            try:
                # Use the ordinary observe path, including rendering and revision
                # persistence. One read only; the original act is never replayed.
                observation = self.call('observe', {'full': True, 'scope': {'kind': 'session'}})
                observed = True
                state_now = json.loads(observation['content'][0]['text'])
                if state_now.get('full') and state_now.get('settled') and not state_now.get('resync_required'):
                    result.update(reconciled=True, state_now=state_now)
            except (AdapterError, OSError, EOFError, ValueError, KeyError, TypeError) as exc:
                if isinstance(exc, AdapterError) and exc.reason == 'ABORTED_KILLED':
                    raise
                # Return the original receipt/advice even if reconciliation fails.
        rendered = render_observe(result) if name == 'observe' else result
        content = [{'type': 'text', 'text': encoded(rendered).decode()}]
        if name == 'observe' and self.active_pack is not None:
            content.append(dict(type='text', text='[pack: ' + self.active_pack.NAME + ']\n' +
                                self.active_pack.PLAYBOOK + '\n[/pack]'))
        if self.images == 'inline':
            for frame in frame_refs(result):
                content.append(dict(type='image', mimeType='image/png', data=base64.b64encode(Path(frame['path']).read_bytes()).decode()))
        if name == 'act' and (result.get('reason') in ('invalid_expectation', 'invalid_params') or
                              any(step.get('reason') in ('invalid_expectation', 'invalid_params')
                                  for step in result.get('steps', []))):
            content.append(step_form_hint())
        # Commit only after the complete result is ready to show, including images.
        if clear_pending:
            self.state.pop('pending', None)
        if not observed and type(result.get('rev')) is int:
            self.state['rev'] = result['rev']
        self.save(self.state)
        return dict(content=content, isError=False)


def mcp_serve(adapter, incoming, outgoing):
    """MCP stdio: one JSON-RPC object per line, no stdout diagnostics."""
    for line in incoming:
        ident = None
        try:
            msg = json.loads(line)
            if not isinstance(msg, dict) or msg.get('jsonrpc') != '2.0' or not isinstance(msg.get('method'), str):
                raise AdapterError('invalid_request')
            if 'id' not in msg:
                continue
            ident = msg['id']
            method, params = msg['method'], msg.get('params', {})
            if not isinstance(params, dict):
                raise AdapterError('invalid_params')
            if method == 'initialize':
                result = dict(protocolVersion=params.get('protocolVersion', '2024-11-05'), capabilities={'tools': {}},
                              serverInfo={'name': 'ark-act', 'version': '0.5'})
            elif method == 'tools/list':
                result = {'tools': TOOLS}
            elif method == 'tools/call':
                try:
                    result = adapter.call(params['name'], params.get('arguments', {}))
                except (AdapterError, OSError, EOFError, ValueError, KeyError, TypeError):
                    # Never expose arbitrary exception text (paths may be sensitive).
                    exc = sys.exc_info()[1]
                    reason = exc.reason if isinstance(exc, AdapterError) else 'adapter_io_error'
                    result = render_error(reason, params.get('name'), exc)
            elif method == 'ping':
                result = {}
            else:
                outgoing.write(encoded(dict(jsonrpc='2.0', id=ident, error={'code': -32601, 'message': 'method_not_found'})).decode()+'\n')
                outgoing.flush()
                continue
            response = dict(jsonrpc='2.0', id=ident, result=result)
        except (ValueError, TypeError, KeyError, AdapterError):
            response = dict(jsonrpc='2.0', id=ident, error={'code': -32600, 'message': 'invalid_request'})
        outgoing.write(encoded(response).decode() + '\n')
        outgoing.flush()


def main():
    parser = Parser(description=__doc__)
    parser.add_argument('mode', choices=['mcp', 'call'])
    parser.add_argument('tool', nargs='?')
    parser.add_argument('arguments', nargs='?', default='{}')
    parser.add_argument('--session', default=os.environ.get('ARK_SESSION_ID'))
    parser.add_argument('--images', choices=['path', 'inline'], default='path')
    try:
        args = parser.parse_args()
    except AdapterError:
        if 'call' in sys.argv[1:]:
            print(encoded(dict(ok=False, error='invalid_arguments')).decode())
        else:
            print('ark-act: invalid_arguments', file=sys.stderr)
        return 1
    adapter = None
    try:
        sd = session_dir(args.session or '')
        if args.mode == 'mcp':
            adapter = Adapter(sd, args.images)
            adapter.attach()
            mcp_serve(adapter, sys.stdin, sys.stdout)
        else:
            # Separate lock inode: atomic state replacement must not unlock peers.
            with (sd / 'driver/adapter-state.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                path = sd / 'driver/adapter-state.json'
                state = json.loads(path.read_text()) if path.exists() else {}
                def save(value):
                    temp = path.with_suffix('.tmp')
                    with temp.open('w') as stream:
                        os.chmod(temp, 0o600)
                        stream.write(encoded(value).decode() + '\n')
                        stream.flush()
                        os.fsync(stream.fileno())
                    temp.replace(path)
                adapter = Adapter(sd, args.images, state, save)
                result = adapter.call(args.tool, json.loads(args.arguments))
                print(encoded(dict(ok=True, **result)).decode())
    except (AdapterError, OSError, EOFError, ValueError, KeyError, TypeError) as exc:
        if args.mode == 'call':
            reason = exc.reason if isinstance(exc, AdapterError) else 'adapter_io_error'
            result = dict(ok=False, error=reason)
            content = render_error(reason, args.tool, exc)['content']
            if len(content) > 1:
                result['content'] = content
            print(encoded(result).decode())
        else:
            print('ark-act: ' + (exc.reason if isinstance(exc, AdapterError) else 'adapter_io_error'), file=sys.stderr)
        return 1
    finally:
        if adapter:
            adapter.close()
    return 0
