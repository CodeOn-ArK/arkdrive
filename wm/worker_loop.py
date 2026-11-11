"""Minimal W5 API driver over ark-act MCP. Stdlib only."""
import json
import math
import os
from pathlib import Path
import selectors
import subprocess
import sys
import time
import urllib.error
import urllib.request

from worker_adapter import AdapterError, Parser, check_kill, encoded

PRESETS = {
    'llama': ('http://127.0.0.1:8080', 'local-model'),
    'openai': ('https://api.openai.com', ''),
    'deepseek': ('https://api.deepseek.com', 'deepseek-chat'),
    'openrouter': ('https://openrouter.ai/api', ''),
    'anthropic': ('https://api.anthropic.com', ''),
}
SYSTEM = """You drive one isolated ArkDrive session through observe, act, wait, done.
Observe first and reason from each fresh result before more input. Act has at
most 8 sequential steps, each with an explicit strong application-state expect
(except worker strong defaults). Acknowledgement/reaction alone proves no task
success. Coordinates are native desktop pixels; use frame rect and scale to
map image coordinates. Never repeat unknown/unobserved input; observe and
reconcile. Authentication codes/passcodes require the human. Call done with
success only after verifying the task, or failed/blocked with a factual summary.

Valid expect kinds are window_present, window_absent, dialog_present,
dialog_absent, window_focused, pointer_at, node_present, node_absent,
text_equals, text_contains, node_state, item_present, item_absent.
Tree expects (node_*, text_*, item_*) can use a selector with role and/or name/name_re.
Put "provider": "atspi" INSIDE the selector object, for example
{"kind": "text_equals", "window": "<handle>", "selector": {"provider": "atspi", "role": "entry", "name": "Name"}, "value": "..."}.
Ref-based expects need no provider, for example
{"kind": "text_equals", "window": "<handle>", "ref": "n7", "value": "..."}.
Use these concrete kinds;
do not invent a generic "state" expect. Select from freshly observed state
and use the intended post-action value, rather than mere input acknowledgement.
A step that changes anything (key, click, text, invoke) must carry one strong
expect or the worker refuses it before dispatch (invalid_expectation).
If an act returns invalid_expectation or invalid_params, fix the FORM of the
step; do not repeat it unchanged. Re-observe if the needed selector is unclear.
If an observe shows no windows, the application is still starting; wait
briefly and observe again before acting.
"""


def provider_config(env=None):
    env = os.environ if env is None else env
    provider = env.get('CU_MODEL_PROVIDER', 'llama').lower()
    if provider not in PRESETS:
        raise AdapterError('unknown_provider')
    server, model = PRESETS[provider]
    server, model = env.get('CU_MODEL_SERVER', server).rstrip('/'), env.get('CU_MODEL', model)
    key = env.get('CU_MODEL_API_KEY', '')
    if not model:
        raise AdapterError('missing_model')
    if provider != 'llama' and not key:
        raise AdapterError('missing_api_key')
    if not server.startswith(('http://', 'https://')):
        raise AdapterError('invalid_model_server')
    try:
        token_budget = int(env.get('CU_LOOP_TOKEN_BUDGET', '7000' if provider == 'llama' else '0'))
        if token_budget < 0:
            raise ValueError
    except ValueError:
        raise AdapterError('invalid_loop_token_budget') from None
    try:
        http_timeout = float(env.get('CU_LOOP_HTTP_TIMEOUT', '120'))
        if not math.isfinite(http_timeout) or http_timeout <= 0:
            raise ValueError
    except (ValueError, TypeError, OverflowError):
        http_timeout = 120
    effort = env.get('CU_MODEL_EFFORT', 'medium')
    effort_fallback = effort not in ('low', 'medium', 'high', 'none')
    if effort_fallback:
        effort = 'medium'
    if provider in ('llama', 'anthropic'):
        effort = 'none'
    # A bounded context must leave room for the requested completion. Keep
    # the existing 4096 output cap when context budgeting is disabled.
    completion_tokens = 1024 if token_budget else 4096
    return dict(provider=provider, server=server, model=model, key=key,
                token_budget=token_budget, http_timeout=http_timeout,
                completion_tokens=completion_tokens,
                effort=effort, effort_fallback=effort_fallback,
                images=provider in ('llama', 'openai', 'openrouter', 'anthropic'))


def effort_summary(config):
    summary = dict(effort=config.get('effort', 'none'))
    if config.get('effort_fallback'):
        summary['notes'] = ['CU_MODEL_EFFORT empty or invalid; using default medium '
                            '(sent only for openai, deepseek, openrouter).']
    return summary


def tool_results(history):
    """Match results to their calls without adding metadata to provider messages."""
    names = {}
    for message in history:
        if message['role'] == 'assistant':
            for call in message.get('tool_calls', []):
                names[call['id']] = call['function']['name']
        elif message['role'] == 'tool':
            yield message, names.get(message['tool_call_id'])


def supersede_observes(history):
    """After appending an observe, retain only its predecessors' revisions."""
    observes = [message for message, name in tool_results(history) if name == 'observe']
    for message in observes[:-1]:
        stub_observe(message)


def stub_observe(message):
    rev = None
    for block in message['content']:
        if block['type'] != 'text':
            continue
        try:
            value = json.loads(block['text'])
        except ValueError:
            continue
        if isinstance(value, dict) and 'rev' in value:
            rev = value['rev']
            break
    message['content'] = [dict(type='text', text=encoded(dict(rev=rev, superseded=True)).decode())]


def estimated_prompt_tokens(history, tools):
    """Character-based estimate; not provider tokenizer/usage data."""
    return math.ceil((len(encoded(history).decode()) + len(encoded(tools).decode())) / 3)


def completion_reserve(config):
    budget = config.get('token_budget', 7000 if config.get('provider') == 'llama' else 0)
    return config.get('completion_tokens', 1024 if budget else 4096)


def compact_history(history, tools, budget, reserve=0):
    """Stub old act/wait content, then observes including the latest if needed.

    Adapter revision/identity/recovery state lives in ark-act, not history text.
    Calls and result envelopes stay intact so provider tool pairing is preserved.
    Return the remaining prompt estimate; the caller must reject requests that
    still cannot fit after reserving completion capacity.
    """
    if not budget:
        return estimated_prompt_tokens(history, tools)
    stub = [dict(type='text', text=encoded(dict(superseded=True)).decode())]
    results = list(tool_results(history))
    for message, name in results[:-2]:
        if estimated_prompt_tokens(history, tools) + reserve <= budget:
            break
        if name in ('act', 'wait') and message['content'] != stub:
            message['content'] = list(stub)
    for message, name in results:
        if estimated_prompt_tokens(history, tools) + reserve <= budget:
            break
        if name == 'observe':
            stub_observe(message)
    return estimated_prompt_tokens(history, tools)


def image_block(block, anthropic=False):
    if anthropic:
        return dict(type='image', source=dict(type='base64', media_type=block['mimeType'], data=block['data']))
    return dict(type='image_url', image_url={'url': 'data:'+block['mimeType']+';base64,'+block['data']})


def request_body(config, tools, history):
    """Canonical history uses MCP content on tool messages, OpenAI tool calls."""
    anthropic = config['provider'] == 'anthropic'
    messages, system = [], []
    def add(role, content):
        if anthropic and messages and messages[-1]['role'] == role:
            messages[-1]['content'].extend(content)
        else:
            messages.append(dict(role=role, content=content))
    for msg in history:
        role, content = msg['role'], msg.get('content')
        if anthropic:
            if role == 'system':
                system.append(content)
                continue
            if role == 'tool':
                blocks = [dict(type='text', text=b['text']) if b['type'] == 'text' else image_block(b, True)
                          for b in content if b['type'] == 'text' or b['type'] == 'image' and config['images']]
                add('user', [dict(type='tool_result', tool_use_id=msg['tool_call_id'], content=blocks,
                                  is_error=msg.get('is_error', False))])
            else:
                blocks = [dict(type='text', text=content)] if content else []
                for call in msg.get('tool_calls', []):
                    blocks.append(dict(type='tool_use', id=call['id'], name=call['function']['name'],
                                       input=json.loads(call['function']['arguments'])))
                add(role, blocks)
        elif role == 'tool':
            messages.append(dict(role='tool', tool_call_id=msg['tool_call_id'],
                                 content='\n'.join(b['text'] for b in content if b['type'] == 'text')))
            # Chat completions carries vision input in user content, after all
            # tool replies for this assistant turn (collected below).
        else:
            messages.append(dict(msg))
    if not anthropic and config['images']:
        # Rebuild with images following the complete contiguous tool-result group.
        expanded, pending = [], []
        for index, msg in enumerate(messages):
            expanded.append(msg)
            original = history[index]
            if original['role'] == 'tool':
                pending.extend(image_block(b) for b in original['content'] if b['type'] == 'image')
                if index+1 == len(messages) or messages[index+1]['role'] != 'tool':
                    if pending:
                        expanded.append(dict(role='user', content=pending))
                        pending = []
        messages = expanded
    completion_tokens = completion_reserve(config)
    body = dict(model=config['model'], messages=messages, max_tokens=completion_tokens, temperature=.1, top_p=1)
    if anthropic:
        body.update(system='\n'.join(system), tools=[dict(name=t['name'], description=t['description'], input_schema=t['inputSchema']) for t in tools])
    else:
        body.update(tools=[dict(type='function', function=dict(name=t['name'], description=t['description'], parameters=t['inputSchema'])) for t in tools], tool_choice='auto')
        if config['provider'] == 'llama':
            body.update(n_predict=completion_tokens, cache_prompt=True, top_k=40, repeat_penalty=1.0)
        elif config['effort'] != 'none':
            body['reasoning_effort'] = config['effort']
    return body


def normalize_response(config, response):
    if not isinstance(response, dict):
        raise ValueError('invalid_provider_response')
    usage = response.get('usage') or {}
    if not isinstance(usage, dict):
        raise ValueError('invalid_usage')
    if config['provider'] == 'anthropic':
        calls, text = [], []
        for block in response.get('content', []):
            if block['type'] == 'text':
                text.append(block['text'])
            elif block['type'] == 'tool_use':
                calls.append(dict(id=block['id'], type='function', function=dict(name=block['name'], arguments=encoded(block['input']).decode())))
        return dict(role='assistant', content='\n'.join(text) or None, tool_calls=calls), usage
    msg = response['choices'][0]['message']
    return {k: msg[k] for k in ('role', 'content', 'tool_calls') if k in msg}, usage


class Provider:
    def __init__(self, config):
        self.config = config

    def complete(self, tools, history):
        c = self.config
        anthropic = c['provider'] == 'anthropic'
        headers = {'Content-Type': 'application/json'}
        if anthropic:
            headers.update({'x-api-key': c['key'], 'anthropic-version': '2023-06-01'})
        elif c['key']:
            headers['Authorization'] = 'Bearer ' + c['key']
        suffix = '/v1/messages' if anthropic else '/v1/chat/completions'
        # Accept either a base endpoint (DRIVERS presets) or an explicit /v1 base.
        url = c['server'] + (suffix[3:] if c['server'].endswith('/v1') else suffix)
        req = urllib.request.Request(url, data=encoded(request_body(c, tools, history)), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=c.get('http_timeout', 120)) as reply:
                result = json.load(reply)
        except urllib.error.HTTPError as exc:
            # Never print server bodies, URLs, headers, or arbitrary exception text.
            code = exc.code
            exc.close()
            raise AdapterError('provider_http_' + str(code)) from None
        except (OSError, ValueError):
            raise AdapterError('provider_transport_error') from None
        try:
            return normalize_response(c, result)
        except (ValueError, KeyError, TypeError, IndexError):
            raise AdapterError('invalid_provider_response') from None


def tool_error(result):
    """Return an error label for MCP errors or JSON error fields, else None."""
    if 'error' in result:
        return str(result['error'])
    for block in result.get('content', []):
        if block.get('type') != 'text':
            continue
        try:
            value = json.loads(block['text'])
        except (ValueError, KeyError):
            continue
        if isinstance(value, dict) and 'error' in value:
            return str(value['error'])
    return 'tool_error' if result.get('isError') else None


class MCPClient:
    def __init__(self, session, images):
        env = dict(os.environ)
        env.pop('CU_MODEL_API_KEY', None)  # Only the API parent needs credentials.
        self.process = subprocess.Popen([str(Path(__file__).with_name('ark-act')), 'mcp', '--session', session, '--images', images],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
        self.ident, self.buffer = 0, bytearray()

    def call(self, method, params=None):
        self.ident += 1
        self.process.stdin.write(encoded(dict(jsonrpc='2.0', id=self.ident, method=method, params=params or {})) + b'\n')
        self.process.stdin.flush()
        until = time.monotonic() + 60
        with selectors.DefaultSelector() as sel:
            sel.register(self.process.stdout, selectors.EVENT_READ)
            while True:
                while b'\n' in self.buffer:
                    line, _, rest = self.buffer.partition(b'\n')
                    self.buffer = bytearray(rest)
                    response = json.loads(line)
                    if not isinstance(response, dict):
                        raise AdapterError('invalid_mcp_response')
                    if response.get('id') == self.ident:
                        if 'error' in response:
                            raise AdapterError('mcp_error')
                        return response['result']
                remaining = until - time.monotonic()
                if remaining <= 0 or not sel.select(remaining):
                    raise AdapterError('mcp_timeout')
                data = os.read(self.process.stdout.fileno(), 65536)
                if not data:
                    raise AdapterError('mcp_disconnected')
                self.buffer.extend(data)
                if len(self.buffer) > 32 * 1024 * 1024:
                    raise AdapterError('mcp_result_too_large')

    def start(self):
        self.call('initialize', dict(protocolVersion='2024-11-05', capabilities={}, clientInfo={'name': 'ark-loop', 'version': '0.5'}))
        self.process.stdin.write(encoded(dict(jsonrpc='2.0', method='notifications/initialized')) + b'\n')
        self.process.stdin.flush()
        return self.call('tools/list')['tools']

    def tool(self, name, arguments):
        return self.call('tools/call', dict(name=name, arguments=arguments))

    def close(self):
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process.stdout.close()


def run_loop(client, provider, task, max_turns=40):
    cap = min(max_turns, 40)
    summary = dict(turns_used=0, tool_calls_made=0, frames_viewed=0, token_usage={}, max_turns=cap,
                   peak_estimated_prompt_tokens=0, completion_reserve_tokens=completion_reserve(provider.config),
                   done=None, reason=None, **effort_summary(provider.config))
    history = [dict(role='system', content=SYSTEM), dict(role='user', content=task)]
    pending_results = []
    failed_call, failures = None, 0
    try:
        tools = client.start()
        allowed = {t['name'] for t in tools}
        for turn in range(cap):
            check_kill()
            summary['turns_used'] = turn + 1
            budget = provider.config.get('token_budget',
                         7000 if provider.config.get('provider') == 'llama' else 0)
            estimate = compact_history(history, tools, budget, summary['completion_reserve_tokens'])
            if budget and estimate + summary['completion_reserve_tokens'] > budget:
                summary['reason'] = 'token_budget_exceeded'
                break
            summary['peak_estimated_prompt_tokens'] = max(summary['peak_estimated_prompt_tokens'], estimate)
            pending_frames = sum(b['type'] == 'image' for message in pending_results
                                 for b in message['content']) if provider.config['images'] else 0
            assistant, usage = provider.complete(tools, history)
            summary['frames_viewed'] += pending_frames
            pending_results = []
            for key, value in usage.items():
                if type(value) in (int, float):
                    summary['token_usage'][key] = summary['token_usage'].get(key, 0) + value
            history.append(assistant)
            calls = assistant.get('tool_calls', [])
            if not calls:
                summary['reason'] = 'model_stopped_without_done'
                break
            hints = []
            for call in calls:
                check_kill()
                name = call['function']['name']
                arguments = json.loads(call['function']['arguments'])
                if name not in allowed:
                    raise AdapterError('unknown_model_tool')
                result = client.tool(name, arguments)
                summary['tool_calls_made'] += 1
                message = dict(role='tool', tool_call_id=call['id'], content=result['content'], is_error=result.get('isError', False))
                history.append(message)
                pending_results.append(message)
                error = tool_error(result)
                signature = (name, json.dumps(arguments, sort_keys=True, separators=(',', ':')))
                if error is None:
                    failed_call, failures = None, 0
                else:
                    failures = failures + 1 if signature == failed_call else 1
                    failed_call = signature
                    if failures == 3:
                        hints.append(dict(role='user', content=
                            f'The last 3 identical {name} calls failed with {error}. '
                            'Do not repeat the same call unchanged; change the step shape or observe first.'))
                if name == 'observe':
                    supersede_observes(history)
                if name == 'done' and not result.get('isError'):
                    # The acknowledged adapter result, rather than an unvalidated proposal.
                    done = json.loads(next(b['text'] for b in result['content'] if b['type'] == 'text'))
                    summary['done'] = dict(status=done['status'], summary=done['summary'])
                    return summary
            # Keep every assistant tool-call group paired before a user hint.
            history.extend(hints)
        else:
            summary['reason'] = 'turn_cap'
    except (AdapterError, OSError, EOFError, ValueError, KeyError, TypeError, StopIteration) as exc:
        summary['reason'] = exc.reason if isinstance(exc, AdapterError) else 'loop_failure'
    return summary


def main():
    parser = Parser(description=__doc__)
    parser.add_argument('--session', required=True)
    parser.add_argument('--task', required=True)
    parser.add_argument('--max-turns', type=int, default=40)
    parser.add_argument('--images', choices=['path', 'inline'], default='path')
    try:
        args = parser.parse_args()
    except AdapterError:
        print(encoded(dict(turns_used=0, tool_calls_made=0, frames_viewed=0, token_usage={}, done=None,
                           peak_estimated_prompt_tokens=0, completion_reserve_tokens=0,
                           reason='invalid_arguments', effort='none')).decode())
        return 1
    client = None
    config = {}
    try:
        if args.max_turns < 1:
            raise AdapterError('invalid_max_turns')
        config = provider_config()  # Validate before any socket/subprocess/network.
        check_kill()
        client = MCPClient(args.session, args.images)
        summary = run_loop(client, Provider(config), args.task, args.max_turns)
    except (AdapterError, OSError) as exc:
        summary = dict(turns_used=0, tool_calls_made=0, frames_viewed=0, token_usage={}, done=None,
                       peak_estimated_prompt_tokens=0, completion_reserve_tokens=completion_reserve(config),
                       reason=exc.reason if isinstance(exc, AdapterError) else 'loop_start_failed',
                       **effort_summary(config))
    finally:
        if client:
            client.close()
    print(encoded(summary).decode())
    return 0 if summary['done'] and summary['done']['status'] == 'success' else 1
