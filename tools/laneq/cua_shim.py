#!/usr/bin/env python3
"""Gate cua writes on whole-work ownership of the shared desktop lane."""

import json
import os
from pathlib import Path
import subprocess
import sys


def read_only(arguments):
    return bool(arguments) and (
        arguments[0] == "shot"
        or tuple(arguments[:2]) in (("win", "list"), ("tab", "list"), ("clip", "get"))
    )


def main():
    real = os.environ.get("LANEQ_CUA_REAL", str(Path.home() / "bin" / "cua.real"))
    arguments = sys.argv[1:]
    if "LANEQ_OFF" not in os.environ and not read_only(arguments):
        pgid = os.getpgid(0)
        laneq = str(Path(__file__).absolute().with_name("laneq"))
        acquired = subprocess.run(
            [laneq, "acquire", "--pgid", str(pgid), "--leader", str(pgid),
             "--wait", os.environ.get("LANEQ_WAIT_S", "1200")],
            stdout=subprocess.PIPE,
        )
        if acquired.returncode:
            try:
                response = json.loads(acquired.stdout)
                if not isinstance(response, dict):
                    raise ValueError("acquire did not return an object")
                sys.stdout.buffer.write(acquired.stdout)
                sys.stdout.buffer.flush()
            except (ValueError, UnicodeError):
                print("laneq acquire returned invalid JSON", file=sys.stderr)
                print(json.dumps({"ok": False, "error": "laneq_acquire_failed"}))
            return acquired.returncode if acquired.returncode > 0 else 128 - acquired.returncode
    # Preserve argument bytes (Python's surrogateescape), descriptors and signals.
    os.execvpe(real, [real, *arguments], os.environ)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except OSError as exc:
        print(f"cua laneq shim: {exc}", file=sys.stderr)
        print(json.dumps({"ok": False, "error": "laneq_exec_failed"}))
        sys.exit(1)
