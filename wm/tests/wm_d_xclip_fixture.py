#!/usr/bin/env python3
"""Headless X selection model: -in owns bytes until killed, never returns.

No X connection is made. Reads require a live owner and do not consume it.
State is independent of the promotion journal and survives controller restart.
"""
import json
import os
from pathlib import Path
import signal
import sys


root = Path(os.environ['ARK_TEST_CLIPBOARD'])
selection = root / (os.environ['ARK_SESSION_ID'] + '-' + os.environ['ARK_SESSION_INSTANCE'])
if '-in' in sys.argv:
    payload = sys.stdin.buffer.read()
    # Keep enough native process evidence to assert detachment and session scope.
    selection.with_suffix('.payload').write_bytes(payload)
    selection.with_suffix('.json').write_text(json.dumps(dict(
        pid=os.getpid(), sid=os.getsid(0), display=os.environ['DISPLAY'],
        home=os.environ['HOME'], runtime=os.environ['XDG_RUNTIME_DIR'],
        argv=sys.argv[1:])))
    while True:
        signal.pause()
elif '-out' in sys.argv or '-o' in sys.argv:
    if os.environ.get('ARK_TEST_CLIPBOARD_READ') == 'hang':
        while True:
            signal.pause()
    try:
        owner = json.loads(selection.with_suffix('.json').read_text())
        os.kill(owner['pid'], 0)
        payload = selection.with_suffix('.payload').read_bytes()
    except (OSError, ValueError):
        sys.exit(1)
    if os.environ.get('ARK_TEST_CLIPBOARD_READ') == 'mismatch':
        payload = b'foreign selection'
    sys.stdout.buffer.write(payload)
else:
    sys.exit(2)
