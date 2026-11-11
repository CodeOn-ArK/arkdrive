#!/usr/bin/python3
"""Measurement only. --dry-run is headless; --session uses an existing worker.

Run live only in a disposable, dedicated ArkDrive session (display 91..120).
JSONL stdout: checkpoint samples and one summary per phase. See the bench note.
No session/worker creation, pause, fence clearing, or uncertain input replay.
"""
import argparse
import ast
import json
import os
from pathlib import Path
import pwd
import socket
import struct
import subprocess
import sys
import time
import uuid

WM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WM))
from worker_adapter import MAX_FRAME, check_kill, encoded


def emit(**value):
    print(json.dumps(value, separators=(',', ':'), allow_nan=False), flush=True)


class MeasureRPC:
    """Read actual wire bytes past the adapter cap, without changing the worker.

    The 64 MiB measurement guard is local to this harness. No adapter code or
    production limit is changed. The persistent connection owns a single lease.
    """
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(15)
        try:
            self.sock.connect(str(path))
        except Exception:
            self.sock.close()
            raise
        self.ident, self.response_bytes = 0, 0

    def read(self, size):
        data = bytearray()
        while len(data) < size:
            part = self.sock.recv(size - len(data))
            if not part:
                raise EOFError('worker disconnected')
            data.extend(part)
        return data

    def call(self, method, params):
        check_kill()
        self.ident += 1
        body = encoded(dict(jsonrpc='2.0', id=self.ident, method=method, params=params))
        if len(body) > MAX_FRAME:
            raise RuntimeError('request exceeds adapter cap')
        self.sock.sendall(struct.pack('!I', len(body)) + body)
        size = struct.unpack('!I', self.read(4))[0]
        self.response_bytes = size
        if not 0 < size <= 64 * 1024 * 1024:
            raise RuntimeError(f'response measurement guard: {size} bytes')
        result = json.loads(self.read(size))
        if result.get('jsonrpc') != '2.0' or result.get('id') != self.ident:
            raise RuntimeError('invalid RPC response')
        if 'error' in result:
            raise RuntimeError('worker RPC error: ' + json.dumps(result['error']))
        return result['result']

    def close(self):
        self.sock.close()


def proc_memory(pid, proc=Path('/proc')):
    """KiB, with explicit nulls/errors for denied or vanished processes.

    VmHWM is peak RSS, never mislabeled as PSS. Prefer smaps_rollup Pss.
    """
    result = dict(pid=pid, pss_kib=None, vmhwm_kib=None, errors={})
    for filename, field, key in (('smaps_rollup', 'Pss:', 'pss_kib'),
                                 ('status', 'VmHWM:', 'vmhwm_kib')):
        try:
            rows = (proc / str(pid) / filename).read_text().splitlines()
            result[key] = next((int(row.split()[1]) for row in rows
                                if row.startswith(field)), None)
            if result[key] is None:
                result['errors'][filename] = 'field_missing'
        except (OSError, ValueError) as exc:
            result['errors'][filename] = type(exc).__name__
    return result


def process_samples(worker_pid, record, proc=Path('/proc')):
    """Discover current descendants on every sample, including child restarts.

    Helpers may be siblings/root-owned; sample sessiond's recorded helper PIDs
    as well. /proc permission failures stay visible instead of becoming zero.
    """
    found = {worker_pid: 'worker'}
    for key in ('mkdevs_pid', 'mkdevs_supervisor_pid'):
        if record.get(key):
            found.setdefault(record[key], key)
    todo, discovery_errors = list(found), {}
    while todo:
        pid = todo.pop()
        try:
            children = (proc / str(pid) / 'task' / str(pid) / 'children').read_text().split()
        except OSError as exc:
            discovery_errors[pid] = type(exc).__name__
            children = []
        for value in children:
            child = int(value)
            if child not in found:
                found[child] = 'worker_descendant' if found[pid].startswith('worker') else 'helper_descendant'
                todo.append(child)
    result = []
    for pid, role in sorted(found.items()):
        row = dict(role=role, **proc_memory(pid, proc))
        if pid in discovery_errors:
            row['children_error'] = discovery_errors[pid]
        try:
            row['cmdline'] = (proc / str(pid) / 'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace').strip()
        except OSError:
            row['cmdline'] = None
        result.append(row)
    return result


def session_environment(record, state):
    # No inherited host DISPLAY, XAUTHORITY, activation or bus variables.
    if type(record.get('display')) is not int or not 91 <= record['display'] <= 120:
        raise ValueError('requires ArkDrive display 91..120; display :0 is forbidden')
    return dict(PATH='/usr/local/bin:/usr/bin:/bin', LANG='C.UTF-8', LC_ALL='C.UTF-8',
                DISPLAY=f":{record['display']}", HOME=record['home'],
                XDG_RUNTIME_DIR=record['runtime_dir'],
                DBUS_SESSION_BUS_ADDRESS=record['bus_address'],
                AT_SPI_BUS_ADDRESS=record['atspi_address'],
                ARK_SESSIOND=str(state), ARK_SESSION_ID=record['session'],
                PYTHONDONTWRITEBYTECODE='1')


def observe_size(observation, wire_bytes):
    return dict(rev=observation['rev'], settled=observation.get('settled'),
                result_bytes=len(encoded(observation)), response_bytes=wire_bytes,
                removed_count=len(observation.get('state', {}).get('removed', [])),
                adapter_cap_bytes=MAX_FRAME, exceeds_adapter_cap=wire_bytes > MAX_FRAME)


def counter_delta(before, after):
    return {key: after[key] - value for key, value in before.items()
            if key != 'schema' and type(value) is int and type(after.get(key)) is int}


class Soak:
    def __init__(self, args, record, state):
        self.args, self.record, self.state = args, record, state
        self.env = session_environment(record, state)  # validate before any socket/launch
        self.rpc = MeasureRPC(Path(record['session_dir']) / 'driver/act.sock')
        self.meta, self.children, self.samples = None, [], []
        self.phase = 'startup'
        self.prefix = 'memory-step0-' + uuid.uuid4().hex[:8]
        self.seq, self.rev = 0, None

    def observe(self, full=True):
        result = self.rpc.call('window.observe', dict(lease=self.meta['lease'],
            generation=self.meta['generation'], scope={'kind': 'session'},
            channels=['state'], since_rev=None if full else self.rev, deadline_ms=10000))
        self.rev = result['rev']
        return result, observe_size(result, self.rpc.response_bytes)

    def wait_for(self, predicate, full=True):
        until = time.monotonic() + self.args.timeout
        while True:
            obs, size = self.observe(full=full)
            if obs.get('settled') and predicate(obs.get('state', {}).get('windows', [])):
                return obs, size
            if time.monotonic() >= until:
                raise RuntimeError('fixture/settled-state timeout')
            check_kill()
            time.sleep(.1)

    def sample(self, checkpoint, **extra):
        obs, size = self.observe()
        info = self.rpc.call('system.info', {})
        if info['worker']['generation'] != self.meta['generation']:
            raise RuntimeError('worker restarted; generation baseline invalid')
        row = dict(checkpoint=checkpoint, monotonic_s=time.monotonic(),
                   memory=info['memory'], worker_state=info['worker']['state'],
                   observe=size, processes=process_samples(info['worker']['pid'], self.record), **extra)
        self.samples.append(row)
        emit(phase=self.phase, event='sample', **row)
        return obs

    def begin(self, phase):
        self.phase, self.samples = phase, []
        self.sample('before')

    def finish(self, **extra):
        self.sample('after')
        emit(phase=self.phase, event='summary', status='MEASURED',
             sample_count=len(self.samples),
             memory_delta=counter_delta(self.samples[0]['memory'], self.samples[-1]['memory']),
             before=self.samples[0], after=self.samples[-1], **extra)

    def spawn(self, tag, popup=False):
        check_kill()
        command = ['/usr/bin/python3', str(WM / 'fixtures/fx_log'), '--tag', tag]
        if popup:
            command += ['--popup-after', str(self.args.popup_after)]
        child = subprocess.Popen(command, env=self.env, cwd=self.record['home'],
                                 stdout=subprocess.DEVNULL, stderr=sys.stderr)
        self.children.append(child)
        return child

    def stop_fixtures(self, children):
        # Only Popen handles created here; never kill by title/PID scan.
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=3)
            self.children.remove(child)

    def churn(self, phase, count, popup=False):
        self.begin(phase)
        for start in range(0, count, self.args.batch):
            tags = [f'{self.prefix}-{phase}-{i}' for i in range(start, min(count, start+self.args.batch))]
            children = [self.spawn(tag, popup) for tag in tags]
            obs, _ = self.wait_for(lambda wins: set(tags) <= {w['title'] for w in wins})
            handles = {w['title']: w['handle'] for w in obs['state']['windows'] if w['title'] in tags}
            self.sample('mapped', completed=start, batch_windows=len(tags))
            if popup:
                for tag in tags:
                    self.act(dict(target=handles[tag], intent=dict(kind='key', keys=['Tab']),
                                  expect=dict(kind='dialog_present', match={'title': tag+' popup'})))
                self.wait_for(lambda wins: {tag+' popup' for tag in tags} <= {w['title'] for w in wins})
                self.sample('popups', completed=start+len(tags))
            self.stop_fixtures(children)
            self.wait_for(lambda wins: not any(w['title'] in tags or w['title'] in
                          {tag+' popup' for tag in tags} for w in wins))
            self.sample('removed', completed=start+len(tags))
        self.finish(requested=count, completed=count)

    def act(self, step):
        obs, _ = self.wait_for(lambda wins: True, full=False)
        self.seq += 1
        # One new ID, one submit. Any uncertainty stops the run without replay.
        result = self.rpc.call('window.act', dict(session=self.record['session'],
            generation=self.meta['generation'], lease=self.meta['lease'],
            control_epoch=self.meta['control_epoch'], action_id=f'{self.prefix}-{self.seq}',
            base_rev=obs['rev'], deadline_ms=10000, steps=[dict(step, timeout_ms=5000)]))
        if result.get('status') != 'success' or result.get('pending_effects'):
            raise RuntimeError('act did not succeed; no replay: ' + json.dumps(result))
        return result

    def act_churn(self):
        tag = self.prefix + '-acts'
        children = [self.spawn(tag)]
        obs, _ = self.wait_for(lambda wins: any(w['title'] == tag for w in wins))
        handle = next(w['handle'] for w in obs['state']['windows'] if w['title'] == tag)
        self.begin('act_churn')
        for i in range(self.args.acts):
            self.act(dict(target=handle, intent=dict(kind='wait'),
                          expect=dict(kind='window_present', window=handle)))
            if (i+1) % self.args.batch == 0:
                self.sample('completed', completed=i+1)
        self.finish(requested=self.args.acts, completed=self.args.acts, intent='wait/window_present')
        self.stop_fixtures(children)
        self.wait_for(lambda wins: not any(w['title'] == tag for w in wins))

    def run(self):
        info = self.rpc.call('system.info', {})
        if info['session']['display'] != self.env['DISPLAY'] or info['worker']['state'] != 'ready':
            raise RuntimeError('worker must be ready on the recorded dedicated display')
        if info.get('memory', {}).get('schema') != 1:
            raise RuntimeError('worker lacks Step-0 counter schema 1')
        self.meta = self.rpc.call('session.attach', dict(client='api', name='memory-step0'))
        # A dedicated session must contain no pre-existing application windows.
        self.wait_for(lambda wins: not wins)
        self.churn('window_churn', self.args.windows)
        window_samples = list(self.samples)
        self.act_churn()
        self.churn('popup_flood', self.args.popups, popup=True)
        self.begin('observe_size')
        self.finish(growth_samples=[dict(checkpoint=s['checkpoint'],
                    removed_windows=s['memory']['removed_windows'], **s['observe'])
                    for s in window_samples],
                    note='Full state-only session observe; response_bytes includes JSON-RPC envelope, excludes 4-byte prefix.')

    def close(self):
        self.stop_fixtures(list(self.children))
        # Closing the socket releases through normal lease grace. No done record
        # or control transition is needed; the dedicated session remains live.
        self.rpc.close()


def dry_run(args):
    # Parse the real fixture and worker without importing GTK, Xlib or libatspi.
    for path in (WM / 'fixtures/fx_log', WM / 'ark-worker'):
        ast.parse(path.read_text(), filename=str(path))
    for phase, requested in (('window_churn', args.windows), ('act_churn', args.acts),
                             ('popup_flood', args.popups), ('observe_size', args.windows)):
        emit(phase=phase, event='summary', status='NOT RUN', mode='headless-dry-run',
             requested=requested, batch=args.batch,
             checks=['stdlib/adapter imports', 'fixture/worker syntax'],
             live_run='NOT RUN')
    emit(phase='proc_probe', event='summary', status='MEASURED',
         note='Harness process only; no worker/session/display opened', **proc_memory(os.getpid()))


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('must be positive')
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--session', help='Existing dedicated session with a ready worker')
    parser.add_argument('--windows', type=positive, default=1000)
    parser.add_argument('--acts', type=positive, default=1000)
    parser.add_argument('--popups', type=positive, default=100)
    parser.add_argument('--batch', type=positive, default=10)
    parser.add_argument('--popup-after', type=positive, default=10, help='fx_log delay in ms')
    parser.add_argument('--timeout', type=positive, default=30, help='fixture wait in seconds')
    args = parser.parse_args(argv)
    if args.dry_run:
        dry_run(args)
        return 0
    soak = None
    try:
        check_kill()
        state = Path(pwd.getpwuid(os.getuid()).pw_dir) / '.local/state/ark-sessiond'
        record = json.loads((state / 'sessions.json').read_text())[args.session]
        if record['session'] != args.session:
            raise ValueError('registry session mismatch')
        soak = Soak(args, record, state)
        soak.run()
        return 0
    except Exception as exc:
        emit(phase=soak.phase if soak else 'startup', event='summary', status='FAILED',
             error=str(exc), error_type=type(exc).__name__)
        return 1
    finally:
        if soak:
            soak.close()


if __name__ == '__main__':
    raise SystemExit(main())
