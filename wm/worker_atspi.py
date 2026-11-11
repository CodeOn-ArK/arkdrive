#!/usr/bin/python3
"""Session-only pyatspi provider child. Never imported by the worker.

The thread running the GLib main context owns ALL pyatspi/libatspi access,
including startup, event metadata and complete Backend jobs. IPC submissions
queue here; no application job threads enter the binding. Event listeners
read only local identity fields and never enter synchronous binding calls.
A blocked owner is bounded by the independent worker's heartbeat/flight
supervision. libatspi also has a 250 ms D-Bus call timeout.
"""
from collections import deque
import os
import queue
import socket
import struct
import sys
import threading
import time
import traceback

from worker_tree import Wire, encoded, MAX_IPC
from pathlib import Path

ROLE_NAMES = {'button': 'push_button'}
EVENT_DEBOUNCE = .04
EVENT_LIMIT = 256
EVENT_BYTES = 256 * 1024


class Results:
    """Owner-thread queue: coalesce only within the current result barrier.

    Removal identities are never merged away: conflicting removals promote to
    the existing global invalidation. A job result seals the event segment.
    The pump drains before running another job, so at most one MAX_IPC job
    result can accompany the bounded notification segment.
    """
    def __init__(self):
        self.rows = deque()
        self.events = {}
        self.bytes = 0
        self.global_event = False
        self.jobs = 0
        self.job_bytes = 0

    def put(self, row):
        if row.get('event') != 'changed':
            size = len(encoded(row))
            if self.jobs or size > MAX_IPC - 4:
                raise ValueError('tree_ipc_backpressure')
            self.jobs += 1
            self.job_bytes += size
            self.rows.append(row)
            self.events = {}
            self.global_event = False
            return
        if self.global_event:
            if row.get('removed'):
                global_row = self.events[(None, None)]
                self.bytes -= len(encoded(global_row))
                global_row['removed_all'] = True
                self.bytes += len(encoded(global_row))
            return
        key = (row.get('app'), row.get('window'))
        prior = self.events.get(key)
        if prior == row:
            return
        size = len(encoded(row))
        if (row.get('global_invalidation') or
                prior is not None and prior.get('removed') != row.get('removed') or
                len(self.rows) >= EVENT_LIMIT or self.bytes + size > EVENT_BYTES):
            # Only replace notifications after the last job-result barrier.
            removed = bool(row.get('removed') or any(e.get('removed') for e in self.events.values()))
            for event in self.events.values():
                self.rows.pop()
                self.bytes -= len(encoded(event))
            self.events.clear()
            row = dict(event='changed', global_invalidation=True)
            if removed:
                row['removed_all'] = True
            key = (None, None)
            self.global_event = True
            size = len(encoded(row))
        elif prior is not None:
            return
        self.rows.append(row)
        self.events[key] = row
        self.bytes += size

    def get_nowait(self):
        if not self.rows:
            raise queue.Empty
        row = self.rows.popleft()
        if row.get('event') != 'changed':
            self.jobs -= 1
            self.job_bytes -= len(encoded(row))
        if row.get('event') == 'changed':
            self.bytes -= len(encoded(row))
            key = (row.get('app'), row.get('window'))
            if self.events.get(key) is row:
                del self.events[key]
                if row.get('global_invalidation'):
                    self.global_event = False
        return row

    def qsize(self):
        return len(self.rows)

    def peek(self):
        return self.rows[0] if self.rows else None


EVENT_CLASSES = (
    'object:children-changed', 'object:property-change', 'object:state-changed',
    'object:text-changed', 'object:text-caret-moved', 'object:text-selection-changed',
    'object:selection-changed', 'object:bounds-changed', 'object:value-changed',
    'object:visible-data-changed', 'object:active-descendant-changed',
    'object:row-inserted', 'object:row-deleted', 'object:column-inserted',
    'object:column-deleted', 'object:model-changed', 'window')


def canonical_role(role):
    role = role.replace(' ', '_')
    return ROLE_NAMES.get(role, role)


def overlap(a, b):
    intersection = max(0, min(a[0]+a[2], b['x']+b['w'])-max(a[0], b['x'])) * max(
        0, min(a[1]+a[3], b['y']+b['h'])-max(a[1], b['y']))
    union = max(0, a[2])*max(0, a[3]) + max(0, b['w'])*max(0, b['h']) - intersection
    return intersection / union if union else 0


def match_frame(frames, pid, rect, title=''):
    candidates = [f for f in frames if (not pid or f['pid'] == pid) and overlap(f['bounds'], rect) > .5]
    return max(candidates, key=lambda f: (overlap(f['bounds'], rect), f.get('name') == title), default=None)


class Backend:
    def __init__(self, atspi):
        self.a = atspi
        self.objects, self.bus_apps, self.versions = {}, {}, {}
        self.window_versions, self.event_windows, self.window_keys = {}, {}, {}
        self.window_apps, self.app_windows = {}, {}
        self.window_buses, self.bus_windows = {}, {}
        self.lock = threading.Lock()
        self.local = threading.local()
        self.activity = {}
        self.activity_lock = threading.Lock()
        self.event_drops, self.event_log_at = 0, float('-inf')
        self.global_version, self.global_due = 0, None

    def begin(self):
        self.local.started, self.local.max_call = time.monotonic(), 0
        self.local.call_error = None

    def timing(self):
        return dict(elapsed_ms=(time.monotonic()-self.local.started)*1000,
                    max_call_ms=self.local.max_call*1000, breach=self.local.max_call >= .2)

    def call(self, fn, *args):
        job = getattr(self.local, 'job', None)
        if job is not None:
            with self.activity_lock:
                started = time.monotonic()
                self.activity.setdefault(job, {})['started'] = started
        else:
            started = time.monotonic()
        try:
            return fn(*args)
        except Exception as exc:
            self.local.call_error = exc
            raise
        finally:
            self.local.max_call = max(getattr(self.local, 'max_call', 0), time.monotonic()-started)
            if job is not None:
                with self.activity_lock:
                    self.activity[job].update(started=None, max_call_ms=self.local.max_call*1000)

    def progress(self, job):
        with self.activity_lock:
            state = self.activity.get(job, {})
            active = (time.monotonic()-state['started'])*1000 if state.get('started') is not None else 0
            maximum = state.get('max_call_ms', 0)
        return dict(active_call_ms=active, max_call_ms=maximum, breach=max(active, maximum) >= 200)

    def read(self, obj, attr):
        return self.call(getattr, obj, attr)

    def children(self, obj):
        iterator = self.call(iter, obj)
        while True:
            child = self.call(next, iterator, None)
            if child is None:
                return
            yield child

    def key(self, obj):
        return str(self.read(self.read(obj, 'app'), 'bus_name')) + ':' + str(self.read(obj, 'path'))

    def changed(self, event, results):
        # This is the registered dispatch boundary. Never let malformed local
        # identities or provider logic errors unwind into pyatspi's event loop.
        try:
            self._changed(event, results)
        except Exception:
            # Even an unexpected listener fault must not hide a possible edit.
            self.invalidate_global()
            self.event_drop('worker_fault', event)

    def event_drop(self, reason, event):
        self.event_drops += 1
        try:
            now = time.monotonic()
            if now >= self.event_log_at:
                self.event_log_at = now + 5
                print('AT-SPI event dropped: '+reason+'; type='+str(getattr(event, 'type', ''))+
                      '; total='+str(self.event_drops), file=sys.stderr, flush=True)
        except Exception:
            pass  # Logging failure must not escape the listener either.

    def event_field(self, obj, attr):
        # Only app, bus_name and path are allowed here: they are local
        # identity fields on the delivered proxy. In particular, parent is a
        # synchronous libatspi read that can dispatch more events reentrantly.
        # Do not route these through read()/call(), or add remote properties.
        if attr not in ('app', 'bus_name', 'path'):
            raise ValueError('nonlocal event field')
        try:
            return getattr(obj, attr, None)
        except Exception:
            return None

    def event_owner(self, source):
        """O(1) local attribution; unresolved identities invalidate globally.

        Never walk ancestry here. A synchronous call in an event callback
        can pump the next event inside this one and starve the IPC job pump.
        """
        if source is None:
            return None
        application = self.event_field(source, 'app')
        for obj in (application, source):
            bus = self.event_field(obj, 'bus_name') if obj is not None else None
            if bus:
                bus = str(bus)
                with self.lock:
                    app = self.bus_apps.get(bus)
                if app is not None:
                    return bus, app
        return None

    def invalidate_global(self):
        # Version EVERY signal, even when its notification is coalesced. A
        # snapshot overlapping any of them must retain its follow-up rebuild.
        self.global_version += 1
        if self.global_due is None:
            self.global_due = time.monotonic() + EVENT_DEBOUNCE

    def flush_events(self, results, force=False):
        # Called by the existing IPC pump, on the binding owner. Keep a fixed
        # first-event deadline: a continuous flood cannot postpone delivery.
        if self.global_due is not None and (force or time.monotonic() >= self.global_due):
            results.put(dict(event='changed', global_invalidation=True))
            self.global_due = None

    def event_window(self, source, owner):
        """Reverse lookup of a walked identity; no binding access or ancestry.

        Unknown or multiply matched identities deliberately fall back to the
        app. Each callback does a constant number of local dictionary lookups.
        """
        path = self.event_field(source, 'path')
        if owner is None or not path:
            return None
        windows = self.event_windows.get(owner[0] + ':' + str(path), ())
        return next(iter(windows)) if len(windows) == 1 else None

    def remember_window(self, window, key):
        self.event_windows.setdefault(key, set()).add(window)
        self.window_keys.setdefault(window, set()).add(key)

    def forget_window(self, task):
        """Owner-only job: retire a window after all earlier jobs have returned.

        The dispatcher queues this even when the app has a flight. No binding
        calls or nested event dispatch occur here, so a partial/failed walk's
        memberships are retired just like a successfully published walk's.
        """
        window = task['window']
        self.objects.pop(window, None)
        self.window_versions.pop(window, None)
        for key in self.window_keys.pop(window, ()):
            windows = self.event_windows[key]
            windows.discard(window)
            if not windows:
                del self.event_windows[key]
        for bus in self.window_buses.pop(window, ()):
            windows = self.bus_windows[bus]
            windows.discard(window)
            if not windows:
                del self.bus_windows[bus]
                self.bus_apps.pop(bus, None)
        app = self.window_apps.pop(window, task['app'])
        windows = self.app_windows.get(app)
        if windows is not None:
            windows.discard(window)
            if not windows:
                del self.app_windows[app]
        if app not in self.app_windows:
            self.versions.pop(app, None)
        return dict(forgotten=True)

    def memory_counters(self):
        return dict(windows=len(self.objects), objects=sum(len(v[0]) for v in self.objects.values()),
                    bus_apps=len(self.bus_apps), versions=len(self.versions),
                    window_versions=len(self.window_versions), event_keys=len(self.event_windows),
                    event_memberships=sum(map(len, self.event_windows.values())),
                    window_keys=len(self.window_keys), window_apps=len(self.window_apps),
                    app_windows=len(self.app_windows), window_buses=len(self.window_buses),
                    bus_windows=len(self.bus_windows))

    def freshness(self, window, app):
        # Only fallback events advance the app epoch; known sibling events
        # advance that sibling's counter alone. Include coalesced globals now,
        # before their notification reaches the worker.
        return (self.versions.get(app, 0), self.window_versions.get(window, 0), self.global_version)

    def _changed(self, event, results):
        source = getattr(event, 'source', None)
        if source is None:
            self.event_drop('unattributed: no source', event)
            return
        owner = self.event_owner(source)
        source_window = self.event_window(source, owner) if owner is not None else None
        removed_owner, path = None, None
        removed_window = None
        fallback = False
        removal = 'children-changed:remove' in str(getattr(event, 'type', ''))
        if removal:
            removed = getattr(event, 'any_data', None)
            removed_owner = self.event_owner(removed)
            path = self.event_field(removed, 'path')
            removed_window = self.event_window(removed, removed_owner) if removed_owner is not None else None
            if removed_owner is None or not path:
                # We can still invalidate the source app, but cannot safely
                # retire a ref without the removed object's bus/path identity.
                fallback = True
                self.event_drop('unattributed removal', event)
        if (owner is None and removed_owner is None) or fallback:
            self.invalidate_global()
        targets = {(owner[1], source_window)} if owner is not None else set()
        if removed_owner is not None:
            targets.add((removed_owner[1], removed_window))
        # An app-wide fallback already covers that app's attributed target.
        targets = {(app, window) for app, window in targets
                   if window is None or (app, None) not in targets}
        for app, window in targets:
            with self.lock:
                if window is None:
                    self.versions[app] = self.versions.get(app, 0) + 1
                else:
                    self.window_versions[window] = self.window_versions.get(window, 0) + 1
            row = dict(event='changed', app=app)
            if window is not None:
                row['window'] = window
            if (removed_owner is not None and path and removed_owner[1] == app and
                    (window is None or window == removed_window)):
                row['removed'] = removed_owner[0] + ':' + str(path)
            results.put(row)

    def snapshot(self, task):
        self.begin()
        raw = self._snapshot(task)
        raw.update(rendered_only=True, **self.timing())
        return raw

    def _snapshot(self, task):
        window, app_id = task['window'], task['app']
        if window in self.window_apps and self.window_apps[window] != app_id:
            self.forget_window(task)
        self.window_apps[window] = app_id
        self.app_windows.setdefault(app_id, set()).add(window)
        version = self.freshness(window, app_id)
        deadline = time.monotonic() + 2
        def check():
            if time.monotonic() >= deadline:
                raise ValueError('tree_internal_budget')
        desktop = self.call(self.a.Registry.getDesktop, 0)
        frames = []
        apps = [(app, self.call(app.get_process_id)) for app in self.children(desktop)]
        preferred = [(app, pid) for app, pid in apps if task.get('pid') and pid == task['pid']]
        for app, pid in preferred if task.get('pid') else apps:
            check()
            for frame in self.children(app):
                check()
                self.call(frame.clearCache)
                component = self.call(frame.queryComponent)
                bounds = self.call(component.getExtents, self.a.DESKTOP_COORDS)
                frames.append(dict(obj=frame, pid=pid, bounds=list(bounds), name=self.read(frame, 'name')))
        selected = match_frame(frames, task.get('pid'), task['rect'], task.get('title', ''))
        if selected is None:
            return dict(key='unmatched:'+task['window'], role='frame', showing=False, visible=False, status='unmatched')
        frame = selected['obj']
        # Binding reads can deliver an event reentrantly. Hold no bookkeeping
        # lock across them, even though all access now belongs to one thread.
        bus = str(self.read(self.read(frame, 'app'), 'bus_name'))
        with self.lock:
            self.bus_apps[bus] = task['app']
            self.window_buses.setdefault(window, set()).add(bus)
            self.bus_windows.setdefault(bus, set()).add(window)
        objects, seen = {}, set()
        count = 0
        deadline = time.monotonic() + 2
        def walk(obj):
            nonlocal count
            check()
            key = self.key(obj)
            if key in seen:
                return None
            seen.add(key)
            # The request opts into window-attributed changed rows. Legacy
            # callers retain app-only rows. Register before binding reads so
            # reentrant events during this first walk can already attribute.
            if task.get('window_events'):
                self.remember_window(window, key)
            count += 1
            if count > 10000:
                raise ValueError('tree_internal_budget')
            self.call(obj.clearCache)
            states = self.call(obj.getState)
            showing = self.call(states.contains, self.a.STATE_SHOWING)
            visible = self.call(states.contains, self.a.STATE_VISIBLE)
            role = canonical_role(self.call(obj.getRoleName))
            value = dict(key=key, role=role, name=self.read(obj, 'name'), showing=showing,
                         visible=visible, states=[], bounds=[0, 0, 0, 0], children=[])
            for state in ('focused', 'selected', 'checked', 'expanded', 'editable'):
                if self.call(states.contains, getattr(self.a, 'STATE_'+state.upper())):
                    value['states'].append(state)
            if not self.call(states.contains, self.a.STATE_ENABLED) or not self.call(states.contains, self.a.STATE_SENSITIVE):
                value['states'].append('disabled')
            try:
                if showing and visible:
                    component = self.call(obj.queryComponent)
                    value['bounds'] = list(self.call(component.getExtents, self.a.DESKTOP_COORDS))
            except NotImplementedError:
                pass
            if showing and visible and role in ('text', 'entry'):
                try:
                    text = self.call(obj.queryText)
                except Exception:
                    text = None
                if text is not None:
                    if self.read(text, 'characterCount') > 65536:
                        raise ValueError('tree_text_budget')
                    value['text'] = self.call(text.getText, 0, -1)
                    value['caret'] = self.read(text, 'caretOffset')
                    if self.call(text.getNSelections):
                        value['selection'] = list(self.call(text.getSelection, 0))
            try:
                value['value'] = self.read(self.call(obj.queryValue), 'currentValue')
            except NotImplementedError:
                pass
            try:
                actions = self.call(obj.queryAction)
                value['actions'] = [self.call(actions.getName, i) for i in range(self.read(actions, 'nActions'))]
            except NotImplementedError:
                value['actions'] = []
            if self.call(states.contains, self.a.STATE_VERTICAL):
                value['axis'] = 'vertical'
            elif self.call(states.contains, self.a.STATE_HORIZONTAL):
                value['axis'] = 'horizontal'
            objects[key] = obj
            for child in self.children(obj) if showing else ():
                if child is not None:
                    n = walk(child)
                    if n is not None:
                        value['children'].append(n)
            check()
            return value
        raw = walk(frame)
        if raw is None:
            raw = dict(key=self.key(frame), role='frame', showing=False, visible=False)
        try:
            raw['toolkit'] = str(self.read(self.read(frame, 'app'), 'toolkitName'))
        except (AttributeError, NotImplementedError):
            raw['toolkit'] = 'unknown'
        with self.lock:
            current = self.freshness(window, app_id)
            raw['dirty'] = current != version
            self.objects[window] = (objects, app_id, current)
            if task.get('window_events'):
                for key in self.window_keys.get(window, set()) - set(objects):
                    windows = self.event_windows[key]
                    windows.discard(window)
                    if not windows:
                        del self.event_windows[key]
                self.window_keys[window] = set(objects)
        return raw

    def enumerate(self, task):
        """Complete container inventory, requested only by item predicates."""
        self.begin()
        version = self.freshness(task['window'], task['app'])
        with self.lock:
            objects, app, _ = self.objects.get(task['window'], ({}, None, None))
            root = objects.get(task['key'])
        if root is None:
            raise ValueError('stale_rev')
        rows, seen = [], set()
        deadline = time.monotonic() + 2
        def walk(obj, parent=None):
            key = self.key(obj)
            if key in seen:
                return
            if len(seen) >= 10000 or time.monotonic() >= deadline:
                raise ValueError('tree_internal_budget')
            seen.add(key)
            if task.get('window_events'):
                self.remember_window(task['window'], key)
            self.call(obj.clearCache)
            rows.append(dict(key=key, parent=parent, name=self.read(obj, 'name'),
                             role=canonical_role(self.call(obj.getRoleName))))
            for child in self.children(obj):
                walk(child, key)
        walk(root)
        return dict(items=rows, dirty=version != self.freshness(task['window'], task['app']), **self.timing())

    def probe(self, task):
        self.begin()
        with self.lock:
            objects, app, version = self.objects.get(task['window'], ({}, None, None))
            obj = next(iter(objects.values()), None)
        if obj is None:
            return dict(alive=False)
        self.call(obj.clearCache)
        # A real application round trip, independent of the cached tree.
        return dict(name=self.read(obj, 'name'))

    def mutation_node(self, task):
        self.begin()
        with self.lock:
            objects, app, version = self.objects.get(task['window'], ({}, None, None))
            obj = objects.get(task['key'])
            if obj is None:
                raise ValueError('stale_rev')
        self.call(obj.clearCache)
        states = self.call(obj.getState)
        if self.call(states.contains, self.a.STATE_DEFUNCT):
            raise ValueError('stale_rev')
        if not self.call(states.contains, self.a.STATE_ENABLED) or not self.call(states.contains, self.a.STATE_SENSITIVE):
            raise ValueError('precondition_failed')
        role, name = canonical_role(self.call(obj.getRoleName)), self.read(obj, 'name')
        if task.get('role', role) != role or task.get('name', name) != name:
            raise ValueError('stale_rev')
        return obj

    def set_text(self, task):
        obj = self.mutation_node(task)
        states = self.call(obj.getState)
        if not self.call(states.contains, self.a.STATE_EDITABLE):
            raise ValueError('backend_unsupported')
        if task.get('focused_required') and not self.call(states.contains, self.a.STATE_FOCUSED):
            raise ValueError('precondition_failed')
        editable = self.call(obj.queryEditableText)
        check_authority(task)
        return dict(accepted=bool(self.call(editable.setTextContents, task['text'])))

    def invoke(self, task):
        obj = self.mutation_node(task)
        action = self.call(obj.queryAction)
        names = [self.call(action.getName, i) for i in range(self.read(action, 'nActions'))]
        requested = task.get('action')
        if requested is not None and requested not in names:
            raise ValueError('invalid_request')
        index = names.index(requested) if requested else names.index('press') if 'press' in names else 0
        if not names:
            raise ValueError('backend_unsupported')
        check_authority(task)
        return dict(accepted=bool(self.call(action.doAction, index)), action=names[index])


def check_authority(task):
    page = struct.unpack('<4sI16sQIIQ16s', Path(task['control']).read_bytes())
    magic, version, generation, epoch, state, flags, permit, lease = page
    if (magic != b'ARKC' or version != 1 or generation.hex() != task['generation'] or
            epoch != task['epoch'] or state != 0 or time.monotonic() >= task['deadline'] or
            permit and time.monotonic_ns() >= permit):
        raise ValueError('preempted')


def main(fd):
    if not os.environ.get('AT_SPI_BUS_ADDRESS') or not os.environ.get('DBUS_SESSION_BUS_ADDRESS'):
        raise RuntimeError('session_a11y_bus_missing')
    import gi
    gi.require_version('Atspi', '2.0')
    import pyatspi
    from gi.repository import GLib
    pyatspi.setTimeout(250, 2000)
    backend = Backend(pyatspi)
    wire, results = Wire(socket.socket(fileno=fd)), Results()
    flights, jobs = {}, deque()
    loop = GLib.MainLoop()

    def work(task):
        backend.local.job, backend.local.call_error = task['id'], None
        backend.begin()
        try:
            value = getattr(backend, task['op'])(task)
            result = dict(id=task['id'], result=value)
            if len(encoded(result)) > MAX_IPC - 1024:
                raise ValueError('tree_internal_budget')
        except Exception as exc:
            if exc is backend.local.call_error:
                reason = 'application_unreachable'
            elif isinstance(exc, ValueError) and str(exc) in (
                    'tree_internal_budget', 'tree_text_budget', 'stale_rev', 'invalid_request',
                    'backend_unsupported', 'preempted', 'precondition_failed'):
                reason = str(exc)
            else:
                reason = 'worker_fault'
            if reason == 'worker_fault':
                traceback.print_exc()
            result = dict(id=task['id'], error=reason)
        result.update(app=task['app'], **backend.timing())
        # A coalesced invalidation may have arrived reentrantly during the
        # job, with a deadline still in the future. Deliver it BEFORE the
        # result, including forget acknowledgements, irrespective of debounce.
        backend.flush_events(results, force=True)
        result['memory'] = backend.memory_counters()
        results.put(result)
        backend.local.job = None

    def pump():
        try:
            for task in wire.pump():
                if task['op'] == 'ping':
                    wire.send(dict(event='pong'))
                    continue
                if task['op'] != 'forget_window' and any(
                        f['app'] == task['app'] for f in flights.values()):
                    # The parent permits one flight per app; this is our
                    # transport/bookkeeping fault, not app unreachability.
                    backend.flush_events(results, force=True)
                    results.put(dict(id=task['id'], app=task['app'], error='worker_fault',
                                     memory=backend.memory_counters()))
                    continue
                # Forget is sequenced on the same owner queue, including when
                # a preceding walk/job for this app is queued or in progress.
                flights[task['id']] = dict(id=task['id'], app=task['app'],
                                          started=time.monotonic(), reported=0)
                jobs.append(task)
            # This GLib source is the only job dispatcher. Run one complete
            # job per turn so queued events and IPC get a turn between jobs.
            # Never wait for another thread or iterate a nested main context.
            # If a call wedges this context, the parent still expires flights
            # and restarts the child without waiting for a child reply.
            # No second job/result until the previous barrier is drained.
            if jobs and not results.jobs:
                work(jobs.popleft())
            backend.flush_events(results)
            for _ in range(256):
                head = results.peek()
                if head is not None and len(encoded(head)) + 4 + len(getattr(wire, 'output', b'')) > MAX_IPC:
                    break  # Drain transport first; retain the ordered result.
                try:
                    result = results.get_nowait()
                except queue.Empty:
                    break
                if 'id' in result:
                    flights.pop(result['id'], None)
                    with backend.activity_lock:
                        backend.activity.pop(result['id'], None)
                wire.send(result)
            now = time.monotonic()
            for flight in flights.values():
                if now-flight['started'] >= .25 and now-flight['reported'] >= .25:
                    wire.send(dict(event='progress', id=flight['id'], **backend.progress(flight['id'])))
                    flight['reported'] = now
        except (EOFError, OSError, ValueError):
            loop.quit()
            return False
        return True

    # Listener callbacks enqueue invalidations; they never build a snapshot.
    pyatspi.Registry.registerEventListener(lambda e: backend.changed(e, results),
                                          *EVENT_CLASSES)
    wire.send(dict(event='ready', call_progress=True, serialized_jobs=True))
    GLib.timeout_add(5, pump)
    loop.run()
    wire.close()


if __name__ == '__main__':
    main(int(sys.argv[1]))
