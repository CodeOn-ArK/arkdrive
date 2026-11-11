"""W8 session-local route policy and shared, atomic learned hints."""
import fcntl
import json
import os
from pathlib import Path
import tempfile

STATES = {'ok', 'suspect', 'rejected', 'down'}
KEY_FIELDS = ('profile_hash', 'app_build', 'toolkit', 'surface_role', 'operation', 'input_class')


def ladder(backend, kind, via='auto', typeable=True, length=0, editable=False, action=False):
    physical = ['evdev', 'xtest'] if backend == 'xorg' else ['xtest']
    if kind == 'invoke':
        return ['atspi']
    if kind in ('focus_window', 'close_window'):
        return ['wm']
    if kind == 'wait':
        return []
    if kind == 'text':
        keys = physical if via == 'keys' or via == 'auto' and typeable and length <= 200 else []
        return keys + ([] if via == 'keys' else ['paste'] + (['atspi'] if editable else []))
    return physical + (['atspi'] if kind == 'click' and action else [])


class RouteTable:
    def __init__(self, path, generation):
        self.path, self.generation = Path(path), generation
        self.entries = self.read()
        self.dirty = {}

    def read(self):
        try:
            value = json.loads(self.path.read_text())
            if value['version'] != 1 or not isinstance(value['entries'], dict):
                return {}
            for key, entry in value['entries'].items():
                if (not isinstance(json.loads(key), list) or len(json.loads(key)) != 6 or
                        not isinstance(entry['routes'], list) or
                        any(r not in ('evdev', 'xtest', 'paste', 'atspi', 'wm') for r in entry['routes']) or
                        any(v['state'] not in STATES for v in entry['states'].values())):
                    return {}
            return value['entries']
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return {}

    def entry(self, identity, routes, rejected=False):
        key = json.dumps([identity[f] for f in KEY_FIELDS], separators=(',', ':'), sort_keys=True)
        entry = self.entries.setdefault(key, dict(identity=identity, routes=routes, states={}))
        entry['routes'] = routes
        for route in routes:
            entry['states'].setdefault(route, dict(state='ok', misses=0))
        if rejected and entry['states'].get('xtest', {}).get('state') != 'rejected':
            self.flip(key, 'xtest', 'rejected')
        return key

    def state(self, key, route):
        return self.entries[key]['states'].get(route, {}).get('state', 'ok')

    def flip(self, key, route, state):
        entry = self.entries[key]
        old = entry['states'].setdefault(route, dict(state='ok', misses=0))
        if old['state'] != state:
            old.update(state=state, generation=self.generation)
            self.dirty.setdefault(key, set()).add(route)
            self.save()

    def next(self, key, routes):
        return next((r for r in routes if self.state(key, r) not in ('rejected', 'down')), None)

    def result(self, key, route, *, verified, acknowledged, focused_before, focused_after, no_change, effect, recover=True):
        state = self.entries[key]['states'].setdefault(route, dict(state='ok', misses=0))
        if verified:
            state['misses'] = 0
            if recover and state['state'] == 'suspect':
                self.flip(key, route, 'ok')
            return False
        qualifies = (route == 'xtest' and acknowledged and focused_before and focused_after and
                     no_change and effect == 'not_observed')
        state['misses'] = state.get('misses', 0) + 1 if qualifies else 0
        if qualifies and state['misses'] == 2:
            self.flip(key, route, 'rejected')
            return True
        return False

    def save(self):
        # Serialize writers across sessions and merge only locally changed entries.
        # Miss counters are process-local; only flips/recoveries are durable hints.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            merged = self.read()
            for key, routes in self.dirty.items():
                entry = self.entries[key]
                states = {r: dict(v, misses=0) for r, v in entry['states'].items()}
                states.update(merged.get(key, {}).get('states', {}))
                for route in routes:
                    states[route] = dict(entry['states'][route], misses=0)
                merged[key] = dict(entry, states=states)
            fd, name = tempfile.mkstemp(prefix='.route-table-', dir=self.path.parent)
            try:
                with os.fdopen(fd, 'w') as out:
                    json.dump(dict(version=1, entries=merged), out, sort_keys=True)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(name, self.path)
                directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
                self.dirty.clear()
            finally:
                if os.path.exists(name):
                    os.unlink(name)
