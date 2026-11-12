"""Thin WM-B control plane; no session lease or per-event input path."""
from contextlib import contextmanager
import copy
import fcntl
import hashlib
import hmac
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import secrets
import selectors
import socket
import stat
import struct
import time

from input_protocol import PAGE, packet
from worker_human import identity, signature
from wm_registry import (atomic_json, amounts, by_incarnation, config,
                         DEPTH_LIMIT, FANOUT_LIMIT, new_incarnation, sync_directory, durable_mkdir)

WM = Path(__file__).resolve().parent
loader = importlib.machinery.SourceFileLoader('wm_sessiond', str(WM / 'ark-sessiond'))
spec = importlib.util.spec_from_loader(loader.name, loader)
sessiond = importlib.util.module_from_spec(spec)
loader.exec_module(sessiond)
MAX_REQUEST = 65536
MAX_CLIENTS = 8
MAX_REPLY = 4 * 1024 * 1024
SCOPES = ['spawn', 'promote', 'prompt', 'queue', 'queue.ack', 'budget.debit', 'attention.report']


class Refused(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


class IntentLog:
    """Fsync before side effects; only an incomplete final line is discardable."""
    def __init__(self, path):
        self.path = path
        self.failed = False
        path.touch(mode=0o600, exist_ok=True)
        self.rows = []
        with path.open('r+b') as stream:
            while True:
                offset = stream.tell()
                line = stream.readline()
                if not line:
                    break
                if not line.endswith(b'\n'):
                    stream.truncate(offset)
                    stream.flush()
                    os.fsync(stream.fileno())
                    break
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError('corrupt_controller_journal')
                self.rows.append(row)
            # A complete final row may have come from an interrupted fsync.
            # Establish durability before replaying it into the registry.
            stream.flush()
            os.fsync(stream.fileno())
        sync_directory(path.parent)

    def append(self, kind, **fields):
        if self.failed:
            raise OSError('controller_journal_unavailable')
        row = dict(type=kind, ts=time.time(), **fields)
        self.failed = True  # Uncertain append: restart/validate before retrying.
        with self.path.open('ab') as stream:
            stream.write(json.dumps(row, allow_nan=False).encode() + b'\n')
            stream.flush()
            os.fsync(stream.fileno())
        self.rows.append(row)
        self.failed = False
        return row


class Controller:
    def __init__(self, state, *, crash=None):
        self.state = Path(state).absolute()
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        sessiond.STATE = self.state
        with sessiond.locked_registry():
            self.cfg = config(self.state)
        key_path = self.state / 'wmctl-key.json'
        if not key_path.exists():
            atomic_json(key_path, secrets.token_hex(32))
        self.key = bytes.fromhex(json.loads(key_path.read_text()))
        self.log = IntentLog(self.state / 'wmctl-journal.jsonl')
        self.crash = crash or (lambda phase: None)
        self.kill_file = Path(os.environ.get('CU_KILL_FILE', '/tmp/cu-executor.kill'))
        self.frames = {}
        self.app_processes = []
        from wm_promotion import Backend
        self.promotion_backend = Backend(self)
        self.scan_offsets = {}
        self.intents, self.completed = {}, {}
        for row in self.log.rows:
            if row['type'] == 'intent':
                self.intents[row['request_id']] = row
            elif row['type'] == 'complete':
                self.completed[row['request_id']] = row['response']

    def capability(self, incarnation):
        return hmac.new(self.key, ('delegate:' + incarnation).encode(), hashlib.sha256).hexdigest()

    def authorize(self, incarnation, token):
        if not isinstance(token, str) or not hmac.compare_digest(self.capability(incarnation), token):
            raise Refused('invalid_capability')

    def record(self, records, incarnation):
        record = by_incarnation(records, incarnation)
        if record is None:
            raise Refused('unknown_incarnation')
        return record

    @contextmanager
    def parent_gate(self, parent):
        """Serialize admission with existing worker control writers.

        Never take a lease or change the page. A missing worker page is closed.
        The registry lock orders this transaction against sessiond stop.
        """
        if parent['lifecycle'] != 'ready':
            raise Refused('parent_stopping')
        try:
            stream = (Path(parent['session_dir']) / 'control').open('rb')
        except FileNotFoundError:
            raise Refused('parent_control_unavailable')
        with stream:
            fcntl.flock(stream, fcntl.LOCK_SH)
            raw = stream.read(PAGE.size + 1)
            if len(raw) != PAGE.size:
                raise Refused('parent_control_unavailable')
            magic, version, generation, epoch, state, flags, permit, lease = PAGE.unpack(raw)
            if magic != b'ARKC' or version != 1 or not epoch or permit or state not in range(4):
                raise Refused('parent_control_unavailable')
            if state != 0:
                raise Refused({1: 'parent_quiescing', 2: 'parent_human', 3: 'parent_paused'}[state])
            if parent.get('control_state', 'AGENT').upper() != 'AGENT':
                raise Refused('parent_' + parent['control_state'].lower())
            # Fences/reconciliation are worker-owned. Read their durable events;
            # never attach to or reset the worker to make spawn succeed.
            fence = False
            journal = self.state / 'acts' / (generation.hex() + '.jsonl')
            if journal.exists():
                with journal.open('rb') as rows:
                    for line in rows:
                        if not line.endswith(b'\n'):
                            raise Refused('parent_journal_unavailable')
                        try:
                            row = json.loads(line)
                        except ValueError:
                            raise Refused('parent_journal_unavailable')
                        if not isinstance(row, dict):
                            raise Refused('parent_journal_unavailable')
                        if row.get('instance') != parent.get('instance'):
                            continue
                        if row.get('type') == 'fence':
                            fence = True
                        elif row.get('type') == 'fence_clear':
                            fence = False
            if fence:
                raise Refused('parent_fenced')
            self.worker_health(parent, generation.hex())
            yield

    def worker_health(self, parent, generation):
        # A storage fence may have no durable fence row, and a fence-clear
        # attempt can leave held inputs fenced. Ask the worker's read-only
        # health surface; never infer authority from journal text alone.
        try:
            response = local_call(Path(parent['session_dir']) / 'driver/act.sock',
                                  'system.info', {}, timeout=0.25)
            health = response['result']
            worker = health['worker']
            if worker['generation'] != generation or health['session']['id'] != parent['session']:
                raise Refused('parent_control_unavailable')
            if worker['state'] in ('fenced', 'awaiting_reconciliation') or worker.get('fence_reason'):
                raise Refused('parent_fenced')
            if worker['state'] != 'ready' or health.get('control', {}).get('state') != 'agent':
                raise Refused('parent_control_unavailable')
        except (OSError, KeyError, ValueError, TypeError):
            raise Refused('parent_control_unavailable')

    def check_spawn(self, records, parent, reservation):
        if self.kill_file.exists():
            raise Refused('global_kill')
        if parent['depth'] >= DEPTH_LIMIT:
            raise Refused('depth_limit')
        if sum(r.get('parent_incarnation') == parent['incarnation']
               for r in records.values()) >= FANOUT_LIMIT:
            raise Refused('fanout_limit')
        # Reserved/creating/collapsed/done-but-still-running sessions all count.
        if len(records) >= self.cfg['admission_limit']:
            raise Refused('admission_limit')
        ledger = parent['budget']
        if any(ledger['spent'][k] + ledger['debited'][k] + reservation[k] >
               ledger['allocation'][k] for k in reservation):
            raise Refused('budget_exhausted')

    def complete(self, intent, response):
        self.log.append('complete', request_id=intent['request_id'], response=response)
        self.completed[intent['request_id']] = response
        self.crash('complete')
        return copy.deepcopy(response)

    def submit(self, method, params):
        """All durable mutations have a client-supplied retry identity."""
        if self.log.failed:
            raise OSError('controller_journal_unavailable')
        request_id = params.get('request_id')
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise Refused('invalid_request_id')
        clean = {k: v for k, v in params.items() if k != 'capability'}
        if method == 'spawn':
            clean.setdefault('origin', 'parent-agent')
        digest = fingerprint(dict(method=method, params=clean))
        prior = self.intents.get(request_id)
        if prior:
            if prior['fingerprint'] != digest:
                raise Refused('request_conflict')
            if request_id in self.completed:
                return copy.deepcopy(self.completed[request_id])
            return self.apply(prior)
        extra = {}
        if method in ('spawn', 'promote'):
            try:
                reservation = amounts(params.get('reservation', self.cfg['child_reservation']))
            except ValueError:
                raise Refused('invalid_budget')
            with sessiond.locked_registry():
                if method == 'spawn':
                    extra = dict(child_incarnation=new_incarnation(self.state),
                                 task_id=secrets.token_hex(16), reservation=reservation)
                else:
                    extra = dict(promotion_id=secrets.token_hex(16), reservation=reservation)
        elif method == 'prompt':
            extra = dict(task_id=secrets.token_hex(16))
        intent = self.log.append('intent', method=method, params=clean,
                                 fingerprint=digest, request_id=request_id, **extra)
        self.intents[request_id] = intent
        self.crash('intent')
        return self.apply(intent)

    def apply(self, intent):
        try:
            if intent['method'] == 'promote':
                from wm_promotion import Promotion
                return self.complete(intent, Promotion(self, intent).run())
            elif intent['method'] == 'stop-subtree':
                result, code = sessiond.stop_sessions(None, incarnation=intent['params']['incarnation'])
                if code:
                    # Retry incomplete teardown on recovery; never mark it done.
                    raise OSError(result['error'])
            else:
                with sessiond.locked_registry() as records:
                    if intent['method'] == 'spawn':
                        result = self.spawn(records, intent)
                    else:
                        result = self.mutate(records, intent)
            return self.complete(intent, dict(result, ok=True))
        except Refused as exc:
            if intent['method'] == 'spawn':
                # A pending reservation rejected after recovery (for example a
                # parent now in HUMAN) must not leave an unlaunched orphan.
                # Reservations are never refunded/minted after acceptance.
                with sessiond.locked_registry() as records:
                    child = by_incarnation(records, intent['child_incarnation'])
                    if child is not None and child['lifecycle'] != 'ready':
                        _, errors = sessiond.stop_tree(records, child['session'])
                        if errors:
                            raise OSError('; '.join(errors))
            return self.complete(intent, dict(ok=False, reason=exc.reason))

    def spawn(self, records, intent):
        p = intent['params']
        if self.kill_file.exists():
            raise Refused('global_kill')
        child = by_incarnation(records, intent['child_incarnation'])
        retired = self.state / 'retired' / (intent['child_incarnation'] + '.json')
        if retired.exists():
            # Parent/child may have been stopped while the controller was dead.
            raise Refused('child_stopped')
        if child is not None and child['lifecycle'] == 'ready':
            # Launch committed before a crash; subsequent HUMAN/stop changes
            # cannot turn this already accepted spawn into a second operation.
            return dict(child_incarnation=child['incarnation'], session=child['session'],
                        task_id=child['task']['id'], reservation=intent['reservation'])
        parent = self.record(records, p['parent_incarnation'])
        with self.parent_gate(parent):
            if child is None:
                self.check_spawn(records, parent, intent['reservation'])
                child = sessiond.allocate_session(p['profile'], records,
                                                  incarnation=intent['child_incarnation'])
                child.update(parent_incarnation=parent['incarnation'],
                             root_incarnation=parent['root_incarnation'], depth=parent['depth'] + 1,
                             spawn_request=intent['request_id'])
                if p.get('promotion_id'):
                    child['promotion_lineage'] = dict(promotion_id=p['promotion_id'],
                                                     source_incarnation=parent['incarnation'])
                child['budget']['allocation'] = dict(intent['reservation'])
                child['task'] = dict(id=intent['task_id'], text=p['task_text'],
                                     origin=p.get('origin', 'parent-agent'),
                                     source_incarnation=parent['incarnation'])
                event = dict(type='prompt', event_id='task:' + intent['task_id'],
                             incarnation=child['incarnation'], task_id=intent['task_id'], **child['task'])
                child['mailbox'].append(event)
                parent['children'].append(child['incarnation'])
                for key, value in intent['reservation'].items():
                    parent['budget']['debited'][key] += value
                # Reservation + genealogy + task event + debit are ONE replace.
                sessiond.save_registry(records)
                self.crash('reserved')
            if self.kill_file.exists():
                raise Refused('global_kill')
            if child['lifecycle'] == 'creating':
                # Side effects might have happened before a PID checkpoint.
                # Prove the old incarnation's marked processes dead before retry.
                self.recover_creation(child, records)
            if child['lifecycle'] == 'reserved':
                self.crash('before_creation')
                result, code = sessiond.launch_session(child, records)
                self.crash('created')
                if code:
                    raise Refused('creation_failed')
            elif child['lifecycle'] != 'ready':
                raise Refused('child_stopping')
        return dict(child_incarnation=child['incarnation'], session=child['session'],
                    task_id=child['task']['id'], reservation=intent['reservation'])

    def recover_creation(self, child, records):
        directory = Path(child['session_dir'])
        # Complete pidfile information if the controller died during wait_file.
        for name, field in (('devices.json.supervisor.pid', 'mkdevs_supervisor_pid'),
                            ('devices.json.pid', 'mkdevs_pid'), ('xorg.pid', 'pid_x')):
            try:
                child[field] = int((directory / name).read_text())
            except FileNotFoundError:
                pass
        sessiond.teardown(child, records, retain=True)
        for key in sessiond.PID_FIELDS:
            child[key] = None
        child.update(display=None, bus_address=None, atspi_address=None, lifecycle='reserved')
        sessiond.save_registry(records)
        self.crash('creation_reaped')

    def mutate(self, records, intent):
        p, method = intent['params'], intent['method']
        record = self.record(records, p['incarnation'])
        prior = record['requests'].get(intent['request_id'])
        if prior is not None:
            return prior
        if record['lifecycle'] != 'ready':
            raise Refused('session_stopping')
        if method == 'prompt':
            if record['task'] and record['result']:
                raise Refused('task_finished')
            if record['task'] is None:
                record['task'] = dict(id=intent['task_id'], text=p['text'], origin=p['origin'],
                                      source_incarnation=p.get('source_incarnation'))
            event = dict(type='prompt', event_id='prompt:' + intent['request_id'],
                         text=p['text'], origin=p['origin'],
                         incarnation=record['incarnation'],
                         source_incarnation=p.get('source_incarnation'),
                         task_id=record['task']['id'] if record['task'] else None)
            record['mailbox'].append(event)
            result = dict(event_id=event['event_id'])
        elif method == 'queue.ack':
            record['mailbox'] = [e for e in record['mailbox'] if e['event_id'] not in p['event_ids']]
            result = dict(acked=p['event_ids'])
        elif method == 'budget.debit':
            debit = amounts(p['amounts'])
            ledger = record['budget']
            if any(ledger['spent'][k] + ledger['debited'][k] + debit[k] >
                   ledger['allocation'][k] for k in debit):
                raise Refused('budget_exhausted')
            for key, value in debit.items():
                ledger['spent'][key] += value
            result = dict(budget=copy.deepcopy(ledger))
        elif method == 'attention.report':
            record['human_need'] = p['need']
            self.update_attention(records)
            result = dict(attention=record['attention'])
        else:
            raise Refused('unknown_method')
        record['requests'][intent['request_id']] = result
        sessiond.save_registry(records)
        self.crash('mutation_saved')
        return result

    def update_attention(self, records):
        for record in records.values():
            record['attention'] = []
        for record in records.values():
            if not record.get('human_need'):
                continue
            event = dict(incarnation=record['incarnation'], need=record['human_need'],
                         task_id=record['task']['id'] if record['task'] else None)
            node, seen = record, set()
            while node and node['incarnation'] not in seen:
                seen.add(node['incarnation'])
                node['attention'].append(event)
                node = by_incarnation(records, node['parent_incarnation'])

    def recover(self):
        # Kill and stopping records take precedence over pending spawn intents.
        self.enforce_kill()
        with sessiond.locked_registry() as records:
            stopping = [r['incarnation'] for r in records.values() if r['lifecycle'] == 'stopping']
        for incarnation in stopping:
            result, code = sessiond.stop_sessions(None, incarnation=incarnation)
            if code:
                raise OSError(result['error'])
        for request_id, intent in list(self.intents.items()):
            if request_id not in self.completed:
                self.apply(intent)
        self.tick()

    def enforce_kill(self):
        if self.kill_file.exists():
            with sessiond.locked_registry() as records:
                if not records:
                    return
            result, code = sessiond.stop_sessions(None)
            if code:
                raise OSError(result['error'])

    def copy_artifacts(self, child, paths):
        if not isinstance(paths, list) or len(paths) > self.cfg['artifact_count']:
            raise Refused('invalid_artifacts')
        target = self.state / 'exchange' / child['incarnation'] / child['task']['id']
        durable_mkdir(target)
        results, total = [], 0
        for index, raw in enumerate(paths):
            if not isinstance(raw, str):
                raise Refused('invalid_artifacts')
            path = Path(raw)
            if path.is_absolute():
                try:
                    path = path.relative_to(child['home'])
                except ValueError:
                    raise Refused('artifact_outside_home')
            if not path.parts or any(p in ('.', '..') for p in path.parts):
                raise Refused('artifact_outside_home')
            # openat traversal rejects symlink ancestors as well as leaf links;
            # a child cannot race resolve()/open() to hand off another HOME.
            descriptor = os.open(child['home'], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                for part in path.parts[:-1]:
                    next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                      dir_fd=descriptor)
                    os.close(descriptor)
                    descriptor = next_fd
                source = os.open(path.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=descriptor)
            finally:
                os.close(descriptor)
            destination = target / (str(index) + '-' + path.name)
            temporary = destination.with_suffix(destination.suffix + '.tmp')
            digest, size = hashlib.sha256(), 0
            try:
                with os.fdopen(source, 'rb') as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise Refused('artifact_not_regular')
                    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(fd, 'wb') as output:
                        while True:
                            block = stream.read(min(65536, self.cfg['artifact_bytes'] - total + 1))
                            if not block:
                                break
                            total += len(block)
                            size += len(block)
                            if total > self.cfg['artifact_bytes']:
                                raise Refused('artifact_quota')
                            output.write(block)
                            digest.update(block)
                        output.flush()
                        os.fsync(output.fileno())
                temporary.replace(destination)
                sync_directory(target)
            finally:
                temporary.unlink(missing_ok=True)
            results.append(dict(path=str(destination), name=path.name, bytes=size,
                                sha256=digest.hexdigest(), child_incarnation=child['incarnation'],
                                task_id=child['task']['id']))
        return results

    def deliver(self, records, child, row, event_id):
        if child['result'] is not None or not child['task']:
            return
        parent = by_incarnation(records, child['parent_incarnation'])
        artifact_error = None
        try:
            artifacts = self.copy_artifacts(child, row.get('artifacts', []))
        except (OSError, Refused) as exc:
            # Never deliver private source paths as if they were shared files.
            artifacts = []
            artifact_error = exc.reason if isinstance(exc, Refused) else 'artifact_unavailable'
            target = self.state / 'exchange' / child['incarnation'] / child['task']['id']
            for path in target.glob('*'):
                path.unlink()
            if target.exists():
                sync_directory(target)
        result = dict(type='result', event_id=event_id, child_incarnation=child['incarnation'],
                      status=row['status'], summary=row['summary'], artifacts=artifacts,
                      provenance=dict(origin='child-agent', child_incarnation=child['incarnation'],
                                      task_id=child['task']['id'], untrusted=True))
        if artifact_error:
            result['artifact_error'] = artifact_error
            result['status'] = 'failed'
        self.crash('artifacts_copied')
        self.log.append('result.intent', event=result,
                        incarnation=parent['incarnation'] if parent else child['incarnation'])
        child['result'] = result
        child['human_need'] = 'blocked' if row['status'] == 'blocked' else None
        if parent:
            parent['mailbox'].append(result)
        self.update_attention(records)
        sessiond.save_registry(records)
        self.crash('result_saved')
        self.log.append('result.delivered', event_id=event_id,
                        incarnation=parent['incarnation'] if parent else child['incarnation'])

    def consume_worker(self, records, record):
        # Bind BOTH reusable sN and immutable process instance. Offsets are cache only;
        # durable result slots suppress duplicate deliveries after restart.
        # A worker can finish and exit/restart while this controller is down.
        # Discover historical generation journals too; worker.json alone would
        # lose that durable done. An old sN incarnation can never match.
        paths = sorted((self.state / 'acts').glob('*.jsonl'), key=lambda p: p.stat().st_mtime_ns)
        for path in paths:
            generation = path.stem
            if len(generation) == 32 and all(c in '0123456789abcdef' for c in generation):
                self.consume_journal(records, record, path, generation)

    def consume_journal(self, records, record, path, generation):
        try:
            stream = path.open('rb')
        except FileNotFoundError:
            return
        with stream:
            cache_key = (record['incarnation'], str(path))
            stream.seek(self.scan_offsets.get(cache_key, 0))
            for _ in range(256):
                offset = stream.tell()
                line = stream.readline(4 * MAX_REQUEST + 1)
                if not line or not line.endswith(b'\n') or len(line) > 4 * MAX_REQUEST:
                    break
                try:
                    row = json.loads(line)
                except ValueError:
                    break
                if not isinstance(row, dict):
                    break
                if (row.get('session') == record['session'] and
                        row.get('instance') == record['instance'] and row.get('generation') == generation):
                    if row.get('type') == 'done' and row.get('status') in ('success', 'failed', 'blocked') and isinstance(row.get('summary'), str):
                        self.deliver(records, record, row, 'done:' + generation + ':' + str(offset))
                    elif row.get('type') == 'human_need':
                        seen = record.setdefault('attention_offsets', {})
                        if offset >= seen.get(generation, 0):
                            # Commit consumption with the flag. Re-reading old
                            # worker history after a restart must not undo a
                            # later owner/driver attention.clear report.
                            seen[generation] = stream.tell()
                            record['human_need'] = row.get('need')
                            self.update_attention(records)
                            sessiond.save_registry(records)
                self.scan_offsets[cache_key] = stream.tell()

    def tick(self):
        self.app_processes = [p for p in self.app_processes if p.poll() is None]
        self.enforce_kill()
        with sessiond.locked_registry() as records:
            for record in records.values():
                self.consume_worker(records, record)

    def frame(self, incarnation):
        # Explicit read-only consumption of WM-A, on demand. Never session.attach
        # or window.observe. Frames stay in the worker's separate feed quota.
        with sessiond.locked_registry() as records:
            record = copy.deepcopy(self.record(records, incarnation))
        params = {}
        previous = self.frames.get(incarnation)
        if previous:
            params = dict(generation=previous['generation'], since_rev=previous['rev'])
        path = str(Path(record['session_dir']) / 'driver/frames-feed.sock')
        try:
            reply = local_call(path, 'frames.get', params, timeout=0.2)
        except (OSError, ValueError):
            return dict(incarnation=incarnation, status='feed_unavailable')
        if 'error' in reply:
            self.frames.pop(incarnation, None)
            return dict(incarnation=incarnation, status='feed_unavailable', error=reply['error'])
        result = reply['result']
        if result.get('session') != record['session']:
            raise Refused('feed_session_mismatch')
        self.frames[incarnation] = result
        return dict(incarnation=incarnation, **result)

    def dispatch(self, method, params):
        if not isinstance(params, dict):
            raise Refused('invalid_params')
        self.enforce_kill()
        if method == 'registry':
            with sessiond.locked_registry() as records:
                return dict(ok=True, sessions=copy.deepcopy(list(records.values())),
                            caps=dict(depth=DEPTH_LIMIT, fanout=FANOUT_LIMIT, **self.cfg))
        if method == 'takeover.issue':
            if params.get('explicit_action') is not True or type(params.get('layout_version')) is not int or params['layout_version'] < 1:
                raise Refused('explicit_action_required')
            if self.kill_file.exists():
                raise Refused('global_kill')
            with sessiond.locked_registry() as records:
                record = copy.deepcopy(self.record(records, params['incarnation']))
            if record['lifecycle'] != 'ready':
                raise Refused('session_stopping')
            worker = json.loads((Path(record['session_dir']) / 'worker.json').read_text())
            if params.get('generation') != worker['generation']:
                raise Refused('stale_generation')
            grant = dict(incarnation=record['incarnation'], generation=worker['generation'],
                         layout_version=params['layout_version'], nonce=secrets.token_hex(32),
                         expires=time.time() + 3600, controller_pid=os.getpid(), controller_start=identity(os.getpid()))
            self.log.append('takeover.issued', incarnation=record['incarnation'], generation=worker['generation'],
                            layout_version=params['layout_version'])
            return dict(ok=True, grant=grant, signature=signature(self.key, grant),
                        socket=str(Path(record['session_dir']) / 'driver/act.sock'))
        if method == 'capability.issue':
            with sessiond.locked_registry() as records:
                record = self.record(records, params['incarnation'])
                self.log.append('capability.issued', incarnation=record['incarnation'], scopes=SCOPES)
                return dict(ok=True, incarnation=record['incarnation'],
                            capability=self.capability(record['incarnation']), scopes=SCOPES)
        if method == 'frames.get':
            return dict(ok=True, **self.frame(params['incarnation']))
        if method == 'stop-subtree':
            if not isinstance(params.get('incarnation'), str) or not params['incarnation']:
                raise Refused('invalid_params')
            return self.submit(method, params)
        if method == 'promote':
            from promotion_recipes import RECIPES, PromotionError
            self.authorize(params['source_incarnation'], params.get('capability'))
            if (not isinstance(params.get('window'), str) or not params['window'] or
                    type(params.get('transfer_clipboard', False)) is not bool):
                raise Refused('invalid_params')
            if params.get('app_class') not in RECIPES:
                raise Refused('no_recipe')
            # Refuse detectable unsaved state before creating any intent,
            # reservation, gate, exchange directory, or checkpoint.
            if params.get('request_id') not in self.intents:
                with sessiond.locked_registry() as records:
                    source = self.record(records, params['source_incarnation'])
                    with self.parent_gate(source):
                        observation = self.promotion_backend.settled(source)
                window = next((w for w in observation['windows'] if
                               w['handle'] == params['window'] and w.get('mapped') and
                               w['class'] == params['app_class']), None)
                if window is None:
                    raise Refused('source_window_missing')
                try:
                    RECIPES[params['app_class']].export(window, observation.get('trees', {}).get(window['handle']), source['home'])
                except PromotionError as exc:
                    raise Refused(str(exc))
        elif method == 'spawn':
            self.authorize(params['parent_incarnation'], params.get('capability'))
            if (not isinstance(params.get('task_text'), str) or not params['task_text'] or
                    params.get('profile') not in ('xvfb', 'xorg') or
                    'promotion_id' in params or
                    params.get('origin', 'parent-agent') not in ('human', 'parent-agent')):
                raise Refused('invalid_params')
        elif method in ('prompt', 'queue', 'queue.ack', 'budget.debit', 'attention.report'):
            self.authorize(params['incarnation'], params.get('capability'))
            if method == 'prompt':
                if not isinstance(params.get('text'), str) or params.get('origin') not in ('human', 'parent-agent'):
                    raise Refused('invalid_params')
                if params['origin'] == 'parent-agent':
                    with sessiond.locked_registry() as records:
                        target = self.record(records, params['incarnation'])
                        if params.get('source_incarnation') != target['parent_incarnation']:
                            raise Refused('invalid_prompt_source')
            elif method == 'queue':
                with sessiond.locked_registry() as records:
                    return dict(ok=True, events=copy.deepcopy(self.record(records, params['incarnation'])['mailbox']))
            elif method == 'queue.ack':
                if not isinstance(params.get('event_ids'), list) or any(not isinstance(v, str) for v in params['event_ids']):
                    raise Refused('invalid_params')
            elif method == 'attention.report':
                if 'need' not in params or params['need'] not in (None, 'passcode', 'human', 'blocked'):
                    raise Refused('invalid_params')
            elif method == 'budget.debit':
                try:
                    amounts(params['amounts'])
                except ValueError:
                    raise Refused('invalid_budget')
        else:
            raise Refused('unknown_method')
        return self.submit(method, params)

    def message(self, payload):
        ident = None
        try:
            msg = json.loads(payload, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
            if (not isinstance(msg, dict) or msg.get('jsonrpc') != '2.0' or
                    type(msg.get('id')) not in (int, str) or not isinstance(msg.get('method'), str)):
                raise Refused('invalid_request')
            ident = msg['id']
            return dict(jsonrpc='2.0', id=ident, result=self.dispatch(msg['method'], msg.get('params', {})))
        except Refused as exc:
            reason = exc.reason
        except (KeyError, TypeError):
            reason = 'invalid_params'
        except (ValueError, UnicodeError, RecursionError):
            reason = 'parse_error'
        except OSError:
            reason = 'storage_or_backend_unavailable'
        return dict(jsonrpc='2.0', id=ident, error=dict(code=-32000, message=reason, data=dict(reason=reason)))


def local_call(path, method, params, timeout=5):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(path))
        sock.sendall(packet(dict(jsonrpc='2.0', id=1, method=method, params=params)))

        def read(size):
            data = bytearray()
            while len(data) < size:
                block = sock.recv(size - len(data))
                if not block:
                    raise ValueError('disconnected')
                data.extend(block)
            return data

        size, = struct.unpack('!I', read(4))
        if not 0 < size <= 4 * 1024 * 1024:
            raise ValueError('reply_too_large')
        return json.loads(read(size))


class Server:
    def __init__(self, controller):
        self.c = controller
        self.path = controller.state / 'wmctl.sock'
        self.selector = selectors.DefaultSelector()
        self.listener = None
        self.clients = {}
        self.bound = False

    def start(self):
        self.path.unlink(missing_ok=True)
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        previous = os.umask(0o177)
        try:
            self.listener.bind(str(self.path))
            self.bound = True
        finally:
            os.umask(previous)
        self.path.chmod(0o600)
        self.listener.listen(MAX_CLIENTS)
        self.listener.setblocking(False)
        self.selector.register(self.listener, selectors.EVENT_READ)

    def accept(self):
        sock, _ = self.listener.accept()
        try:
            _, uid, _ = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != os.getuid() or len(self.clients) >= MAX_CLIENTS:
                sock.close()
                return
            sock.setblocking(False)
            self.clients[sock] = dict(incoming=bytearray(), outgoing=bytearray(), deadline=time.monotonic() + 10)
            self.selector.register(sock, selectors.EVENT_READ)
        except OSError:
            sock.close()

    def drop(self, sock):
        self.clients.pop(sock, None)
        self.selector.unregister(sock)
        sock.close()

    def io(self, sock, mask):
        client = self.clients[sock]
        try:
            if mask & selectors.EVENT_WRITE:
                outgoing = client['outgoing']
                del outgoing[:sock.send(outgoing)]
                if not outgoing:
                    self.drop(sock)  # One request/reply per connection.
                return
            data = sock.recv(8192)
            if not data:
                self.drop(sock)
                return
            incoming = client['incoming']
            incoming.extend(data)
            if len(incoming) < 4:
                return
            size, = struct.unpack('!I', incoming[:4])
            if not 0 < size <= MAX_REQUEST or len(incoming) > size + 4:
                self.drop(sock)
                return
            if len(incoming) == size + 4:
                reply = self.c.message(incoming[4:])
                body = json.dumps(reply, allow_nan=False, separators=(',', ':')).encode()
                if len(body) > MAX_REPLY:
                    body = json.dumps(dict(jsonrpc='2.0', id=reply.get('id'), error=dict(
                        code=-32000, message='reply_too_large', data=dict(reason='reply_too_large')))).encode()
                client['outgoing'] = bytearray(struct.pack('!I', len(body)) + body)
                self.selector.modify(sock, selectors.EVENT_WRITE)
        except BlockingIOError:
            pass
        except (OSError, ValueError):
            if sock in self.clients:
                self.drop(sock)

    def run(self):
        self.c.recover()
        self.start()
        due = time.monotonic()
        try:
            while True:
                for key, mask in self.selector.select(0.1):
                    if key.fileobj is self.listener:
                        self.accept()
                    else:
                        self.io(key.fileobj, mask)
                now = time.monotonic()
                if now >= due:
                    self.c.tick()
                    due = now + 0.25
                for sock, client in list(self.clients.items()):
                    if now >= client['deadline']:
                        self.drop(sock)
        finally:
            self.close()

    def close(self):
        for sock in list(self.clients):
            self.drop(sock)
        if self.listener:
            self.listener.close()
        self.selector.close()
        if self.bound:
            self.path.unlink(missing_ok=True)
