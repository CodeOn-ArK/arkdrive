"""WM-B durable metadata. All mutations require sessiond's registry lock.

No worker lease, input channel, X connection or third-party dependency lives here.
"""
import json
import os
from pathlib import Path
import secrets
import time


DEFAULT_CONFIG = {
    'admission_limit': 2,
    'root_budget': {'acts': 1000, 'tokens': 100000},
    'child_reservation': {'acts': 100, 'tokens': 10000},
    'artifact_bytes': 16 * 1024 * 1024,
    'artifact_count': 16,
}
DEPTH_LIMIT = 3
FANOUT_LIMIT = 4


def sync_directory(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def durable_mkdir(directory):
    directory = Path(directory)
    if not directory.exists():
        durable_mkdir(directory.parent)
        directory.mkdir(mode=0o700, exist_ok=True)
        sync_directory(directory.parent)


def atomic_json(path, value):
    path = Path(path)
    durable_mkdir(path.parent)
    temporary = path.with_name(path.name + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, allow_nan=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    sync_directory(path.parent)


def amounts(value):
    if (not isinstance(value, dict) or set(value) != {'acts', 'tokens'} or
            any(type(v) is not int or v < 0 for v in value.values())):
        raise ValueError('invalid_budget')
    return dict(value)


def config(state):
    path = Path(state) / 'wm-config.json'
    if not path.exists():
        atomic_json(path, DEFAULT_CONFIG)
    value = json.loads(path.read_text())
    if set(value) != set(DEFAULT_CONFIG):
        raise ValueError('invalid_config')
    for key in ('admission_limit', 'artifact_bytes', 'artifact_count'):
        if type(value[key]) is not int or value[key] < 1:
            raise ValueError('invalid_config')
    amounts(value['root_budget'])
    amounts(value['child_reservation'])
    return value


def new_incarnation(state):
    # Counter is committed BEFORE issuance, so a crash can skip but never reuse
    # an id, including after all sN records have been stopped. Namespace also
    # prevents collisions when importing an old registry without a counter.
    path = Path(state) / 'incarnation-sequence.json'
    value = json.loads(path.read_text()) if path.exists() else {
        'namespace': secrets.token_hex(16), 'next': 1}
    incarnation = value['namespace'] + '-' + str(value['next'])
    value['next'] += 1
    atomic_json(path, value)
    return incarnation


def metadata(state, record, allocation=None):
    """Upgrade pre-WM-B records once, persisting even on read-only list calls."""
    if 'incarnation' in record:
        return False
    record.update(
        incarnation=new_incarnation(state), parent_incarnation=None,
        root_incarnation=None, depth=0, children=[],
        budget={'allocation': amounts(allocation or config(state)['root_budget']),
                'debited': {'acts': 0, 'tokens': 0},
                'spent': {'acts': 0, 'tokens': 0}},
        task=None, result=None, mailbox=[], requests={}, attention=[],
        control_state='AGENT', lifecycle='ready')
    record['root_incarnation'] = record['incarnation']
    return True


def by_incarnation(records, incarnation):
    return next((r for r in records.values() if r.get('incarnation') == incarnation), None)


def retire(state, record):
    if record.get('incarnation'):
        # A tombstone is durable before removal from the active registry. It
        # preserves genealogy, request identity and spent budgets across sN reuse.
        atomic_json(Path(state) / 'retired' / (record['incarnation'] + '.json'),
                    dict(record, lifecycle='stopped', stopped_at=time.time()))
