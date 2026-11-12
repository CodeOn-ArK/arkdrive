"""Headless crash fixture: metadata/control pages and optional marked sleepers.

Never launches a worker, X server, desktop, input device or privileged helper.
"""
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from input_protocol import PAGE
from wm_controller import Controller, sessiond


def provision(record, records, sleeper=False):
    record['lifecycle'] = 'creating'
    sessiond.save_registry(records)
    directory = Path(record['session_dir'])
    for name in ('home', 'runtime', 'logs'):
        (directory / name).mkdir(parents=True, exist_ok=True)
    (directory / '.instance').write_text(record['instance'])
    (directory / 'control').write_bytes(PAGE.pack(
        b'ARKC', 1, secrets.token_bytes(16), 1, 0, 0, 0, bytes(16)))
    if sleeper:
        record['pid_x'] = subprocess.Popen(
            [sys.executable, '-c', 'import time; time.sleep(120)'],
            env=sessiond.session_env(record), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).pid
    record['lifecycle'] = 'ready'
    sessiond.save_registry(records)
    with (sessiond.STATE / 'provision.jsonl').open('a') as stream:
        stream.write(json.dumps(dict(incarnation=record['incarnation'])) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    return dict(session=record['session']), 0


def main():
    state, phase, raw = sys.argv[1:]
    params = json.loads(raw)

    def crash(at):
        if at == phase:
            os.kill(os.getpid(), signal.SIGKILL)

    controller = Controller(state, crash=crash)
    controller.worker_health = lambda *_: None  # No worker/display in this fixture.
    sessiond.launch_session = lambda record, records: provision(record, records, sleeper=phase == 'created')
    if phase == 'recover':
        controller.recover()
    else:
        controller.dispatch('spawn', params)


if __name__ == '__main__':
    main()
