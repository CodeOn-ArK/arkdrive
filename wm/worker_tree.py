"""W4 cached tree model and nonblocking, restartable AT-SPI child transport.

Only worker_atspi imports pyatspi. All methods here either read cached data or
queue bounded IPC; tick is called by the worker's existing selector loop.
"""
import copy
import json
import os
from pathlib import Path
import re
import secrets
import socket
import struct
import subprocess
import time
import traceback

from worker_act import ActError, TreePending

INTERACTIVE = set('push_button toggle_button check_box radio_button menu_item text entry combo_box list_item table_cell tree_item icon link tab page_tab'.split())
STATES = ('focused', 'selected', 'checked', 'expanded', 'editable', 'disabled')
TREE_KINDS = {'node_present', 'node_absent', 'text_equals', 'text_contains', 'node_state',
              'item_present', 'item_absent', 'scroll_value'}
MAX_IPC = 4 * 1024 * 1024
MAP_GRACE = 2.0


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()


def packet(value):
    raw = encoded(value)
    if len(raw) > MAX_IPC:
        raise ValueError('tree_ipc_budget')
    return struct.pack('!I', len(raw)) + raw


class Wire:
    def __init__(self, sock):
        self.sock, self.input, self.output = sock, bytearray(), bytearray()
        sock.setblocking(False)

    def send(self, value):
        raw = packet(value)
        if len(self.output) + len(raw) > MAX_IPC:
            raise ValueError('tree_ipc_backpressure')
        self.output.extend(raw)

    def pump(self):
        if self.output:
            try:
                count = self.sock.send(self.output)
                del self.output[:count]
            except BlockingIOError:
                pass
        # Bound each turn so a faulty child cannot monopolise the owner.
        for _ in range(16):
            try:
                raw = self.sock.recv(65536)
            except BlockingIOError:
                break
            if not raw:
                raise EOFError('tree_child_closed')
            self.input.extend(raw)
            if len(self.input) > MAX_IPC + 4:
                raise ValueError('tree_ipc_budget')
        rows = []
        while len(self.input) >= 4:
            count = struct.unpack('!I', self.input[:4])[0]
            if count > MAX_IPC:
                raise ValueError('tree_ipc_budget')
            if len(self.input) < count + 4:
                break
            value = json.loads(self.input[4:count+4])
            if not isinstance(value, dict):
                raise ValueError('tree_ipc_object_required')
            rows.append(value)
            del self.input[:count+4]
        return rows

    def close(self):
        self.sock.close()


def selector_schema(s):
    if (not isinstance(s, dict) or set(s) - {'provider', 'role', 'name', 'name_re', 'within'}
            or s.get('provider') != 'atspi' or not any(k in s for k in ('role', 'name', 'name_re'))
            or any(not isinstance(v, str) or len(v) > 512 for v in s.values())
            or ('name' in s and 'name_re' in s)):
        raise ActError('invalid_request')
    if 'name_re' in s:
        try:
            re.compile(s['name_re'])
        except re.error:
            raise ActError('invalid_request')


def line(node, depth):
    role = node['role'].replace(' ', '_')
    states = [s for s in STATES if s in node.get('states', [])]
    result = ' ' * depth + node['ref'] + ' ' + role + ' ' + json.dumps(node.get('name', ''), ensure_ascii=False)
    if states:
        result += ' [' + ','.join(states) + ']'
    if role in ('text', 'entry') and 'text' in node:
        result += ' text=' + json.dumps(node['text'][:80], ensure_ascii=False)
    return result + ' @' + ','.join(str(int(v)) for v in node.get('bounds', [0, 0, 0, 0]))


def included(raw):
    """Filter bottom-up; flatten only unnamed, noninteractive containers."""
    if not raw.get('showing', False) or not raw.get('visible', False):
        return []
    # AT-SPI's INT_MIN origin marks unrealized layout, including descendants.
    # Real negative coordinates remain addressable; inventory is independent.
    if -2147483648 in raw.get('bounds', [])[:2]:
        return []
    children = [n for c in raw.get('children', []) for n in included(c)]
    meaningful = bool(raw.get('name') or raw.get('text') or raw['role'] in INTERACTIVE)
    if not meaningful and len(children) == 1:
        return children
    if not meaningful and not children:
        return []
    return [dict(raw, children=children)]


class TreeCache:
    def __init__(self):
        self.next_ref = self.min_ref = 1
        self.ref_windows = {}
        self.identities, self.unmapped_refs = {}, []
        self.windows, self.cursors = {}, {}
        self.epoch = 0

    def restart(self):
        # The counter deliberately never resets, so n1 cannot alias after restart.
        self.windows.clear()
        self.cursors.clear()
        self.epoch += 1
        self.min_ref = self.next_ref
        self.ref_windows.clear()
        self.identities.clear()
        self.unmapped_refs.clear()

    def prune(self, window):
        # Compact numeric tombstones retain current-generation absence proof
        # without retaining dead windows or their node objects.
        refs = sorted(int(r[1:]) for r, h in self.ref_windows.items() if h == window)
        ranges = self.unmapped_refs + [(r, r) for r in refs]
        merged = []
        for lo, hi in sorted(ranges):
            if merged and lo <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(hi, merged[-1][1]))
            else:
                merged.append((lo, hi))
        self.unmapped_refs = merged
        for r in ['n'+str(n) for n in refs]:
            self.ref_windows.pop(r, None)
            self.identities.pop(r, None)
        self.windows.pop(window, None)

    def unmapped(self, ref):
        return (isinstance(ref, str) and re.fullmatch(r'n[0-9]+', ref) is not None and
                any(lo <= int(ref[1:]) <= hi for lo, hi in self.unmapped_refs))

    def update(self, window, raw, rev=0):
        old = self.windows.get(window, {})
        previous = old.get('nodes', {})
        by_key = {n['key']: n for n in previous.values()}
        nodes, lines, enumeration = {}, [], []
        def enumerate_raw(n, parent=None):
            enumeration.append(dict(key=n['key'], parent=parent, name=n.get('name', ''), role=n['role']))
            for child in n.get('children', []):
                enumerate_raw(child, n['key'])
        enumerate_raw(raw)
        def visit(n, depth, parent=None):
            key = n['key']
            prior = by_key.get(key)
            ref = prior['ref'] if prior and prior.get('parent') == parent else None
            if ref is None:
                ref = 'n' + str(self.next_ref)
                self.next_ref += 1
            value = {k: copy.deepcopy(v) for k, v in n.items() if k not in
                     ('children', 'elapsed_ms', 'max_call_ms', 'breach', 'dirty', 'status', 'rendered_only')}
            self.ref_windows[ref] = window
            self.identities.setdefault(ref, (value['key'], value['role'], value.get('name', '')))
            value.update(ref=ref, parent=parent)
            nodes[ref] = value
            lines.append(line(value, depth))
            for child in n.get('children', []):
                visit(child, depth+1, ref)
        for root in included(raw):
            visit(root, 0)
        changed = nodes != previous or lines != old.get('lines')
        if not changed:
            old['enumeration'] = enumeration
            return False
        # One previous revision permits exact deltas; older readers get full.
        self.windows[window] = dict(nodes=nodes, lines=lines, enumeration=enumeration, rev=rev,
                                    previous=old, epoch=self.epoch)
        # Avoid an unbounded linked history.
        if old:
            old.pop('previous', None)
        return True

    def page(self, window, since=None, cursor=None, budget=None, dirty=False):
        now = time.monotonic()
        self.cursors = {k: v for k, v in self.cursors.items() if now < v[3]}
        start = 0
        if cursor is not None:
            value = self.cursors.get(cursor)
            if value is None or value[0] != window:
                raise ActError('stale_rev', 'tree_cursor')
            snap = value[1]
            start = value[2]
        else:
            snap = self.windows[window]
        node_limit = min(200, (budget or {}).get('nodes', 200))
        byte_limit = min(16384, (budget or {}).get('bytes', 16384))
        result = dict(window=window, format='lines/1', status='usable' if any(
            n['role'] in INTERACTIVE for n in snap['nodes'].values()) else 'empty',
                      rev=snap['rev'], truncated=False, cursor=None)
        if dirty and cursor is None:
            result['dirty'] = True
        if node_limit < 1 or len(encoded(dict(result, lines=[], full=True))) > byte_limit:
            raise ActError('provider_degraded', 'tree_page_budget')
        old = snap.get('previous')
        if cursor is None and since is not None and old and old['rev'] == since < snap['rev']:
            before = {v['ref']: s for v, s in zip(old['nodes'].values(), old['lines'])}
            current = {v['ref']: s for v, s in zip(snap['nodes'].values(), snap['lines'])}
            delta = dict(added=[s for r, s in current.items() if r not in before],
                         changed=[s for r, s in current.items() if r in before and s != before[r]],
                         removed=[r for r in before if r not in current])
            ordered_before = [r for r in before if r in current]
            ordered_now = [r for r in current if r in before]
            if (ordered_before == ordered_now and sum(map(len, delta.values())) <= 40 and
                    sum(map(len, delta.values())) <= node_limit and
                    len(encoded(dict(result, delta=delta))) <= min(2048, byte_limit)):
                self.issued(snap, delta['added'] + delta['changed'])
                return dict(result, delta=delta)
        lines = []
        size = len(encoded(dict(result, lines=[], full=True, truncated=False, cursor='x'*32)))
        for s in snap['lines'][start:]:
            cost = len(encoded(s)) + bool(lines)
            if size + cost > byte_limit and not lines:
                raise ActError('provider_degraded', 'tree_line_budget')
            if len(lines) == node_limit or size + cost > byte_limit:
                break
            lines.append(s)
            size += cost
        end = start + len(lines)
        if end < len(snap['lines']):
            token = next((k for k, v in self.cursors.items() if v[0] == window and
                          v[1] is snap and v[2] == end), None) or secrets.token_urlsafe(24)
            expires = value[3] if cursor is not None else now + 60
            self.cursors[token] = (window, snap, end, expires)
            # Continuations are reusable and bounded, not consumed by reads.
            if len(self.cursors) > 512:
                del self.cursors[next(iter(self.cursors))]
            result.update(truncated=True, cursor=token)
        self.issued(snap, lines)
        return dict(result, lines=lines, full=True)

    def issued(self, snap, lines):
        for s in lines:
            ref = s.lstrip().split(' ', 1)[0]
            n = snap['nodes'][ref]
            self.identities[ref] = (n['key'], n['role'], n.get('name', ''))

    def find(self, window, subject, missing=False):
        nodes = self.windows.get(window, {}).get('nodes', {})
        if isinstance(subject, str):
            if re.fullmatch(r'n[0-9]+', subject) and 0 < int(subject[1:]) < self.min_ref:
                raise ActError('stale_rev', 'provider_restarted')
            owner = self.ref_windows.get(subject)
            if owner is not None and owner != window:
                raise ActError('stale_rev', 'ref_window')
            node = nodes.get(subject)
            if node is None and not missing:
                raise ActError('stale_rev', 'tree_ref')
            return node
        selector_schema(subject)
        within = subject.get('within')
        if within is not None and within not in nodes:
            if missing:
                return None
            raise ActError('stale_rev', 'tree_within')
        def inside(n):
            parent = n.get('parent')
            while parent:
                if parent == within:
                    return True
                parent = nodes[parent].get('parent')
            return False
        matches = [n for n in nodes.values() if
                   ('role' not in subject or n['role'] == subject['role']) and
                   ('name' not in subject or n.get('name', '') == subject['name']) and
                   ('name_re' not in subject or re.search(subject['name_re'], n.get('name', ''))) and
                   (within is None or inside(n))]
        if len(matches) > 1:
            raise ActError('invalid_request', 'ambiguous_selector')
        if not matches and not missing:
            raise ActError('target_not_found')
        return matches[0] if matches else None


class Breaker:
    def __init__(self):
        self.breaches, self.attempt, self.due = 0, 0, 0
        self.state, self.reason = 'healthy', None

    def fail(self, now, elapsed, reason='application_unreachable'):
        self.breaches += 1
        if self.breaches >= 2 or elapsed > 1:
            self.state, self.reason = 'degraded', reason
            self.due = now + min(30, 2 ** min(self.attempt, 5))
            self.attempt += 1

    def success(self):
        self.breaches = self.attempt = 0
        self.state, self.reason, self.due = 'healthy', None, 0

    def degrade(self, now, reason):
        self.state, self.reason = 'degraded', reason
        self.due = now + min(30, 2 ** min(self.attempt, 5))
        self.attempt += 1


class Provider:
    def __init__(self, worker, factory=None, clock=time.monotonic):
        self.w, self.clock = worker, clock
        self.factory = factory or self.spawn
        self.cache = TreeCache()
        self.apps, self.probes, self.dirty, self.pending = {}, {}, set(), {}
        self.samples, self.unavailable, self.modals = {}, set(), {}
        self.layouts = {}
        # App epochs advance ONLY for explicit app/global fallback rows.
        # Attributed events have an independent counter for their window.
        self.serials = {}
        self.window_serials = {}
        self.first_map, self.dirty_due, self.retry, self.consumers = {}, {}, {}, {}
        self.raw, self.inventories = {}, {}
        self.wire = self.child = None
        self.seq, self.ready, self.restart_at, self.restart_attempt = 0, False, 0, 0
        self.call_progress = False
        self.serialized_jobs = False
        self.ping, self.last_ping = None, float("-inf")
        self.start()

    def spawn(self):
        record = self.w.record
        # Refuse missing session plumbing: never allow libatspi bus discovery.
        if (not record.get('atspi_address') or not record.get('bus_address') or
                type(record.get('display')) is not int or not 91 <= record['display'] <= 120):
            raise ValueError('session_a11y_bus_missing')
        parent, child = socket.socketpair()
        env = os.environ.copy()
        env.update(DISPLAY=':' + str(record['display']), HOME=record['home'],
                   XDG_RUNTIME_DIR=record['runtime_dir'],
                   DBUS_SESSION_BUS_ADDRESS=record['bus_address'],
                   AT_SPI_BUS_ADDRESS=record['atspi_address'],
                   ARK_SESSIOND=str(self.w.state), ARK_SESSION_ID=self.w.sid,
                   PYTHONDONTWRITEBYTECODE='1')
        log = (self.w.sd / 'logs/atspi-provider.log').open('ab')
        try:
            proc = subprocess.Popen(['/usr/bin/python3', str(Path(__file__).with_name('worker_atspi.py')),
                                     str(child.fileno())], pass_fds=(child.fileno(),), env=env,
                                    stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        except Exception:
            parent.close()
            raise
        finally:
            child.close()
            log.close()
        return Wire(parent), proc

    def start(self):
        try:
            self.wire, self.child = self.factory()
            self.started = self.clock()
            self.ready = False
            self.call_progress = False
            self.serialized_jobs = False
        except (OSError, ValueError):
            self.down()

    def down(self):
        if self.wire:
            self.wire.close()
        if self.child and self.child.poll() is None:
            self.child.kill()
        if self.child:
            # poll reaps exited children without waiting on a running one.
            self.retired = getattr(self, 'retired', []) + [self.child]
        self.cache.restart()
        self.probes.clear()
        self.samples.clear()
        self.layouts.clear()
        self.unavailable.clear()
        self.modals.clear()
        self.serials.clear()
        self.window_serials.clear()
        self.dirty_due.clear()
        self.retry.clear()
        self.raw.clear()
        self.inventories.clear()
        self.dirty.clear()
        for task in self.pending.values():
            task['done'], task['error'] = True, 'provider_degraded'
        self.pending.clear()
        self.ready, self.wire, self.child, self.ping = False, None, None, None
        self.restart_at = self.clock() + min(30, 2 ** min(self.restart_attempt, 5))
        self.restart_attempt += 1
        # A child death/restart is provider failure, never an app breach.
        # Keep measured history, but require fresh recovery snapshots before
        # publishing healthy apps (or historical app-failure reasons) again.
        for b in self.apps.values():
            b.state, b.reason, b.due = 'degraded', 'provider_restarting', self.restart_at

    def close(self):
        self.down()
        # Cleanup is outside the owner loop; reap killed children.
        for proc in getattr(self, 'retired', []):
            proc.wait()

    def bump(self, handles):
        self.w.cache.changed.update('tree['+h+']' for h in handles)
        self.w.cache.commit()

    def health(self):
        apps = {}
        severity = {'healthy': 0, 'pending': 1, 'degraded': 2, 'down': 3}
        for win in self.w.cache.windows.values():
            if not win['mapped']:
                continue
            app = self.app(win)
            b = self.apps.get(app, Breaker())
            pid = getattr(self.w.x, 'window_pids', {}).get(win['xid']) if getattr(self.w, 'x', None) else None
            state = 'down' if not self.ready else self.readiness(win['handle'])
            due = max(b.due, self.retry.get(win['handle'], (0, 0))[0], self.dirty_due.get(win['handle'], 0))
            detail = dict(pid=pid, state=state, reason='provider_restarting' if not self.ready else
                          b.reason or ('application_unreachable' if state == 'degraded' else None),
                          breaches=b.breaches, next_probe_ms=max(0, int((due-self.clock())*1000)))
            prior = apps.setdefault(app, dict(detail, windows=[], window_status={}))
            prior['windows'].append(win['handle'])
            prior['window_status'][win['handle']] = dict(detail)
            if severity[state] > severity[prior['state']]:
                prior.update(detail)
        state = max((a['state'] for a in apps.values()), key=severity.get,
                    default='healthy' if self.ready else 'down')
        return dict(atspi=dict(state=state, apps=apps),
                    fallback_available=['state', 'pixels', 'keyboard'])

    def status(self, window):
        state = self.readiness(window)
        if state != 'healthy':
            # Keep the tree publication status for an unavailable snapshot.
            # health().apps[].window_status exposes whether admission can
            # await its retry, independently of this page's usability.
            if window in self.unavailable:
                return 'degraded'
            return 'unmatched' if state == 'pending' and self.probes.get(window) == 'unmatched' else state
        if window not in self.probes:
            return 'pending'
        return self.probes[window]

    def app(self, win):
        # PID separates independent instances whose WM_CLASS is identical.
        pid = getattr(self.w.x, 'window_pids', {}).get(win['xid']) if getattr(self.w, 'x', None) else None
        return str(pid) if pid else win.get('app') or win.get('class') or win['handle']

    def healthy(self, window):
        return self.readiness(window) == 'healthy'

    def readiness(self, window):
        """Shared per-window health: retry uncertainty is pending, never proof.

        Only a live child and closed app breaker may wait for a snapshot
        retry. An unavailable window without that recovery path is degraded.
        Ordinary dirty snapshots keep their separate freshness semantics.
        """
        try:
            win = self.window(window)
        except ActError:
            return 'degraded'
        if (not self.ready or self.child is None or self.child.poll() is not None or
                self.wire is None or self.apps.get(self.app(win), Breaker()).state != 'healthy'):
            return 'degraded'
        if self.probes.get(window) == 'unmatched':
            return 'pending' if self.clock() - self.first_map.get(window, self.clock()) < 30 else 'degraded'
        if window in self.unavailable:
            scheduled = window in self.dirty and window in self.dirty_due
            inflight = any(t['window'] == window and t['op'] == 'snapshot' and
                           (t['retry'] or t['breached']) for t in self.pending.values())
            return 'pending' if scheduled or inflight else 'degraded'
        return 'healthy'

    def can_wait(self, window):
        return self.readiness(window) != 'degraded'

    def window(self, handle):
        win = next((v for v in self.w.cache.windows.values() if v['handle'] == handle and v['mapped']), None)
        if win is None:
            raise ActError('target_not_found')
        return win

    def freshness(self, window, app=None):
        """Target-window counter plus the explicit app/global fallback epoch."""
        return (self.serials.get(app if app is not None else self.app(self.window(window)), 0),
                self.window_serials.get(window, 0))

    def request(self, op, window, **fields):
        win = self.window(window)
        app = self.app(win)
        breaker = self.apps.setdefault(app, Breaker())
        now = self.clock()
        self.first_map.setdefault(window, now)
        if not self.ready or (breaker.state == 'degraded' and now < breaker.due):
            return None
        if any(v['app'] == app for v in self.pending.values()):
            return None
        self.seq += 1
        task = dict(id=self.seq, app=app, window=window, op=op, started=now,
                    done=False, error=None, breached=False, recovery=breaker.state == 'degraded',
                    retry=window in self.unavailable,
                    serial=self.freshness(window, app), key=fields.get('key'))
        task['measured_calls'] = self.call_progress
        self.pending[self.seq] = task
        pid = getattr(self.w.x, 'window_pids', {}).get(win['xid']) if getattr(self.w, 'x', None) else None
        self.wire.send(dict(id=self.seq, op=op, app=app, window=window, pid=pid, title=win.get('title', ''),
                            rect=win['geometry'], window_events=True, **fields))
        return task

    def target(self, scope):
        if scope.get('kind', 'session') == 'session':
            return self.w.cache.focus
        return scope.get('window', scope.get('handle'))

    def ensure(self, window, health=False):
        if not window or not any(w["handle"] == window and w["mapped"] for w in self.w.cache.windows.values()):
            return
        if health:
            self.consumers[window] = getattr(self.w, 'lease', None)
        now = self.clock()
        if (window in self.probes and self.probes[window] == 'unmatched' and
                window not in self.dirty):
            if now - self.first_map.get(window, now) < 30 and now >= self.retry.get(window, (0, 0))[0]:
                self.request('snapshot', window)
        elif window not in self.probes or window in self.dirty:
            if now < self.dirty_due.get(window, 0):
                return
            self.request('snapshot', window)
        elif health:
            app = self.app(self.window(window))
            if window in self.unavailable or self.clock() - self.samples.get(app, 0) >= 1:
                self.request('probe', window)

    def busy(self, params):
        if params.get('since_rev') is not None or params.get('cursor') is not None:
            return False
        if not any(c in ('auto', 'tree') for c in params.get('channels', ['auto'])):
            return False
        handle = self.target(params.get('scope', {}))
        if handle:
            self.ensure(handle)
        return any(t['window'] == handle and t['op'] == 'snapshot' and not t['breached'] for t in self.pending.values())

    def tick(self):
        now = self.clock()
        self.retired = [p for p in getattr(self, 'retired', []) if p.poll() is None]
        if self.wire is None:
            if now >= self.restart_at:
                self.start()
            return
        try:
            if self.child.poll() is not None:
                raise EOFError('provider_exited')
            for row in self.wire.pump():
                if row.get('event') == 'ready':
                    self.ready = True
                    self.call_progress = bool(row.get('call_progress'))
                    self.serialized_jobs = bool(row.get('serialized_jobs'))
                elif row.get('event') == 'pong':
                    self.ping = None
                elif row.get('event') == 'progress':
                    task = self.pending.get(row['id'])
                    if task is not None:
                        task['progress'] = row
                elif row.get('event') == 'changed':
                    windows = [w for w in self.w.cache.windows.values() if w['mapped'] and
                               (row.get('global_invalidation') or
                                (self.app(w) == row['app'] and
                                 (not row.get('window') or w['handle'] == row['window'])))]
                    if row.get('global_invalidation') or not row.get('window'):
                        apps = {self.app(w) for w in windows} if row.get('global_invalidation') else {row['app']}
                        for app in apps:
                            self.serials[app] = self.serials.get(app, 0) + 1
                    else:
                        for win in windows:
                            h = win['handle']
                            self.window_serials[h] = self.window_serials.get(h, 0) + 1
                    handles = [w['handle'] for w in windows]
                    for h in handles:
                        if h not in self.dirty:
                            self.dirty_due[h] = now + .04
                        self.dirty.add(h)
                    removed = row.get('removed')
                    if removed:
                        for h in handles:
                            snap = self.cache.windows.get(h)
                            if snap:
                                invalid = {r for r, n in snap['nodes'].items() if n['key'] == removed}
                                for _ in range(len(snap['nodes'])):
                                    more = {r for r, n in snap['nodes'].items() if n.get('parent') in invalid}
                                    if more <= invalid:
                                        break
                                    invalid.update(more)
                                for r in invalid:
                                    snap['nodes'][r]['key'] += ':removed:' + str(
                                        self.freshness(h))
                elif 'id' in row:
                    task = self.pending.pop(row['id'], None)
                    if task is None:
                        continue
                    if self.serialized_jobs and self.ping is not None:
                        # The GLib owner can service its queued ping only after
                        # work returns. Give that IPC turn its normal budget.
                        self.ping = now
                    task.update(done=True, result=row.get('result'), error=row.get('error'))
                    result = task['result'] or {}
                    if task['op'] == 'snapshot' and result:
                        self.raw[task['window']] = copy.deepcopy(result)
                        self.raw[task['window']].update({k: row[k] for k in
                            ('elapsed_ms', 'max_call_ms', 'breach') if k in row})
                    b = self.apps[task['app']]
                    # Child measurements, never the interval between worker ticks.
                    breach = bool(row.get('breach', result.get('breach', False)))
                    deterministic = task['error'] and task['error'] not in ('timeout', 'application_unreachable')
                    grace = now - self.first_map.get(task['window'], now) < MAP_GRACE
                    if task['error'] in ('stale_rev', 'invalid_request', 'backend_unsupported',
                                         'preempted', 'precondition_failed'):
                        continue
                    # The end-to-end watchdog does not measure an app call.
                    # A completed, measured healthy reply supersedes it.
                    measured = 'max_call_ms' in row or 'max_call_ms' in result
                    if measured and not breach and not task['error']:
                        task['breached'] = False
                    if task['error'] or breach or task['breached']:
                        self.unavailable.add(task['window'])
                        if deterministic:
                            b.degrade(now, task['error'])
                        elif not task['breached'] and not grace:
                            b.fail(now, row.get('max_call_ms', result.get('max_call_ms', 200))/1000)
                        task['breached'] = task['breached'] or breach
                        # A single breach also retries by snapshot, never by a
                        # lightweight probe that could re-publish a stale tree.
                        self.dirty.add(task['window'])
                        self.dirty_due[task['window']] = max(now + 1, b.due)
                        continue
                    b.success()
                    self.samples[task['app']] = now
                    self.unavailable.discard(task['window'])
                    if task['op'] == 'enumerate':
                        key = (task['window'], task['key'])
                        if result.get('dirty'):
                            self.inventories.pop(key, None)
                        else:
                            self.inventories[key] = (task['serial'], result['items'])
                    if task['op'] == 'snapshot':
                        h = task['window']
                        if self.cache.update(h, result, self.w.cache.rev + 1):
                            self.bump([h])
                        if result.get('status') == 'unmatched':
                            self.probes[h] = 'unmatched'
                            attempt = self.retry.get(h, (0, 0))[1]
                            self.retry[h] = (now + min(30, 2 ** min(attempt, 5)), attempt+1)
                        else:
                            nodes = self.cache.windows[h]['nodes'].values()
                            self.probes[h] = 'usable' if any(n['role'] in INTERACTIVE for n in nodes) else 'empty'
                            self.retry.pop(h, None)
                        if result.get('dirty') or task['serial'] != self.freshness(h, task['app']):
                            self.dirty.add(h)
                            self.dirty_due[h] = now + .04
                        else:
                            self.dirty.discard(h)
                            self.dirty_due.pop(h, None)
                        self.layouts[h] = self.w.cache.facets.get('geometry['+h+']', 1)
            if not self.ready and now - self.started > 5:
                raise EOFError('provider_ready_timeout')
            for task in self.pending.values():
                elapsed = now - task['started']
                if elapsed > 5:
                    raise EOFError('provider_job_wedged')
                if elapsed > 2:
                    task['overdue'] = True
                    progress = task.get('progress')
                    # Legacy children lack progress telemetry; current children
                    # distinguish slow RPCs from provider work/scheduling.
                    legacy = progress is None and not task['measured_calls']
                    app_elapsed = elapsed if legacy else max(
                        (progress or {}).get('active_call_ms', 0), (progress or {}).get('max_call_ms', 0))/1000
                    if not task['breached'] and (legacy or (progress or {}).get('breach')):
                        task['breached'] = True
                        if now - self.first_map.get(task['window'], now) >= MAP_GRACE:
                            self.apps[task['app']].fail(now, app_elapsed)
                        self.unavailable.add(task['window'])
                    if self.ping is None:
                        self.ping = self.last_ping = now
                        self.wire.send(dict(op='ping'))
            if (any(t.get('overdue') for t in self.pending.values()) and self.ping is None and
                    now - self.last_ping >= .25):
                self.ping = self.last_ping = now
                self.wire.send(dict(op='ping'))
            if (self.ping is not None and now - self.ping > 1 and
                    not (self.serialized_jobs and self.pending)):
                raise EOFError('provider_heartbeat_timeout')
            if self.ready and now - self.started > 30:
                self.restart_attempt = 0
            live = {w['handle'] for w in self.w.cache.windows.values() if w['mapped']}
            tracked = (set(self.cache.windows) | set(self.probes) | set(self.layouts) |
                       set(self.modals) | set(self.unavailable) | set(self.first_map) | set(self.window_serials))
            for h in tracked - live:
                self.cache.prune(h)
                for mapping in (self.probes, self.layouts, self.modals, self.first_map,
                                self.dirty_due, self.retry, self.raw, self.window_serials):
                    mapping.pop(h, None)
                self.unavailable.discard(h)
                self.dirty.discard(h)
                self.consumers.pop(h, None)
                self.inventories = {k: v for k, v in self.inventories.items() if k[0] != h}
            for win in list(self.w.cache.windows.values()):
                if not win['mapped']:
                    continue
                h = win['handle']
                self.first_map.setdefault(h, now)
                if self.w.cache.facets.get('geometry['+h+']', 1) > self.layouts.get(h, 0):
                    self.dirty.add(h)
                b = self.apps.get(self.app(win))
                if b and b.state == 'degraded':
                    if now >= b.due:
                        self.request('snapshot', h)
                else:
                    self.ensure(h, health=bool(getattr(self.w, 'lease', None)) and
                                self.consumers.get(h) == self.w.lease)
        except Exception:
            self.log_exception()
            self.down()

    def log_exception(self):
        path = self.w.sd / 'logs/atspi-provider.log'
        path.parent.mkdir(exist_ok=True)
        with path.open('a') as stream:
            traceback.print_exc(file=stream)

    def snapshot(self, window, completed=False):
        """Admission can use an installed snapshot while its successor builds.

        Dirty is uncertainty for effect verification, not provider health.
        Keep the default fresh-read semantics for post-input verification.
        """
        if window:
            self.ensure(window, health=True)
        state = self.readiness(window)
        if state == 'degraded':
            raise ActError('provider_degraded')
        if (state == 'pending' or window not in self.cache.windows or
                (not completed and window in self.dirty)):
            raise TreePending(window)
        return self.cache.windows[window]

    def resolve(self, window, subject, base=None, missing=False, completed=False):
        if not window and isinstance(subject, str):
            window = next((h for h, snap in self.cache.windows.items() if subject in snap['nodes']), None)
        if not window:
            window = self.w.cache.focus
        self.snapshot(window, completed)
        if not isinstance(subject, str) and base is not None and self.w.cache.facets.get('tree['+window+']', 1) > base:
            raise ActError('stale_rev', 'tree_changed')
        node = self.cache.find(window, subject, missing)
        if isinstance(subject, str) and node is not None:
            if self.cache.identities.get(subject) != (node['key'], node['role'], node.get('name', '')):
                raise ActError('stale_rev', 'tree_identity')
        return node

    def admission(self, value, window=None):
        if isinstance(value, dict):
            target = value.get('target')
            if isinstance(target, dict):
                window = target.get('window', window)
            window = value.get('window', window)
            if 'selector' in value:
                selector_schema(value['selector'])
                try:
                    self.resolve(window, value['selector'], missing=True, completed=True)
                except ActError as exc:
                    if exc.reason == 'invalid_request':
                        raise
            for item in value.values():
                self.admission(item, window)
        elif isinstance(value, list):
            for item in value:
                self.admission(item, window)

    def predicate_window(self, p):
        window = p.get('window')
        within = p.get('within')
        if window is None and within == 'modal':
            window = next(iter(self.w.cache.modal), None)
        if window is None and any(w['handle'] == within for w in self.w.cache.windows.values()):
            window = within
        identity = p.get('ref', p.get('container', p.get('within')))
        if window is None and isinstance(identity, str):
            window = next((h for h, snap in self.cache.windows.items() if identity in snap['nodes']), None)
        return window or self.w.cache.focus

    def evaluate(self, p):
        window = self.predicate_window(p)
        subject = p.get('ref', p.get('selector'))
        if subject is None and p['kind'] in ('node_present', 'node_absent'):
            match = p.get('match', {k: p[k] for k in ('name', 'role') if k in p})
            if match:
                subject = dict(provider='atspi', **match)
        if p['kind'] == 'node_absent' and isinstance(subject, str):
            owner = self.cache.ref_windows.get(subject)
            if self.cache.unmapped(subject) or (owner is not None and not any(
                    w['handle'] == owner and w['mapped'] for w in self.w.cache.windows.values())):
                if p.get('window') is not None and owner is not None and p['window'] != owner:
                    raise ActError('stale_rev', 'ref_window')
                return True
        item_query = subject is None and p['kind'].startswith('item_')
        container = p.get('container', p.get('within'))
        if 'container' not in p and container in (window, 'modal'):
            container = None
        if item_query:
            try:
                root = self.resolve(window, container)
            except ActError as exc:
                if exc.reason == 'provider_degraded':
                    return None
                return False
            # Enumerate the complete semantic container, including offscreen
            # items omitted by lines/1's showing+visible display filter.
            if self.raw.get(window, {}).get('rendered_only'):
                serial, items = self.inventories.get((window, root['key']), (-1, []))
                if serial != self.freshness(window):
                    self.request('enumerate', window, key=root['key'])
                    return None
            else:
                # Injected/replayed complete fixtures already have an inventory.
                items = self.cache.windows[window]['enumeration']
            by_key = {item['key']: item for item in items}
            def contained(item):
                parent = item.get('parent')
                while parent:
                    if parent == root['key']:
                        return True
                    parent = by_key[parent].get('parent')
                return False
            matches = [item for item in items if item['name'] == p.get('name') and
                       ('role' not in p or item['role'] == p['role']) and contained(item)]
            if len(matches) > 1:
                raise ActError('invalid_request', 'ambiguous_selector')
            return bool(matches) if p['kind'] == 'item_present' else not matches
        if container:
            try:
                self.resolve(window, container)
            except ActError as exc:
                if exc.reason == 'provider_degraded':
                    return None
                return False
            if isinstance(subject, dict):
                subject = dict(subject, within=container)
            else:
                # Ref subjects must also be descendants of the given container.
                parent = self.resolve(window, subject, missing=True)
                nodes = self.cache.windows[window]['nodes']
                while parent and parent['ref'] != container:
                    parent = nodes.get(parent.get('parent'))
                if parent is None:
                    return False
        try:
            node = self.resolve(window, subject, missing=True)
        except ActError as exc:
            if exc.reason == 'provider_degraded':
                return None
            raise
        k = p['kind']
        if node is None:
            # Zero selector matches fails verification, including absence.
            return isinstance(subject, str) and subject in self.cache.ref_windows and k in ('node_absent', 'item_absent')
        if k in ('node_present', 'item_present'):
            return True
        if k in ('node_absent', 'item_absent'):
            return False
        if k == 'text_equals':
            return 'text' in node and node['text'] == p.get('value', p.get('text'))
        if k == 'text_contains':
            return 'text' in node and p.get('value', p.get('text', '')) in node['text']
        if k == 'node_state':
            actual = 'disabled' not in node.get('states', []) if p.get('state') == 'enabled' else p.get('state') in node.get('states', [])
            return actual == p.get('value', True)
        if k == 'scroll_value':
            return ('value' in node and node['value'] == p.get('value') and
                    p.get('axis') == node.get('axis'))
        return None

    def requirements(self, req, window, base, completed=False):
        for field, state in (('ref', None), ('selected_ref', 'selected'), ('editable_ref', 'editable')):
            if field in req:
                n = self.resolve(window, req[field], base, completed=completed)
                if 'disabled' in n.get('states', []) or state and state not in n.get('states', []):
                    raise ActError('precondition_failed')
        if 'text_equals' in req:
            n = self.resolve(window, req['text_equals']['ref'], base, completed=completed)
            if 'text' not in n or n['text'] != req['text_equals'].get('value', req['text_equals'].get('text')):
                raise ActError('precondition_failed')
        if 'modal_role' in req:
            if not any(self.classify(h, req['modal_role'], completed=completed) for h in self.w.cache.modal):
                raise ActError('precondition_failed')

    def classify(self, window, role, initial=None, remember=False, completed=False):
        try:
            snap = self.snapshot(window, completed)
        except ActError:
            if completed:
                raise
            return None
        nodes = snap['nodes']
        if role != 'rename':
            return {"dialog": window} if any(n['role'] == role for n in nodes.values()) else False
        roots = [n for n in nodes.values() if n['role'] in ('dialog', 'alert')]
        entries = [n for n in nodes.values() if n['role'] in ('text', 'entry') and
                   'editable' in n.get('states', []) and 'disabled' not in n.get('states', [])]
        buttons = [n['name'] for n in nodes.values() if n['role'] == 'push_button' and
                   'disabled' not in n.get('states', [])]
        if len(roots) != 1 or len(entries) != 1 or 'Rename' not in buttons or 'Cancel' not in buttons:
            return False
        entry = entries[0]
        prior = self.modals.get(window)
        if initial is not None:
            if entry.get('text') != initial:
                return False
        elif not prior or prior.get('entry') != entry['ref']:
            return False
        result = dict(dialog=window, entry=entry['ref'])
        if remember:
            self.modals[window] = result
        return result

    def computed_text(self, node, intent):
        if 'editable' not in node.get('states', []) or 'text' not in node:
            raise ActError('invalid_expectation')
        if intent.get('replace'):
            return intent['text']
        if 'caret' not in node and 'selection' not in node:
            raise ActError('invalid_expectation', 'text_insertion_position_unavailable')
        start, end = node.get('selection', [node.get('caret', 0)] * 2)
        start, end = sorted((start, end))
        return node['text'][:start] + intent['text'] + node['text'][end:]
