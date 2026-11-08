#!/usr/bin/env python3
"""Core regression subset for v1 (lean, per the release plan amendment).

Runs only the hardening-suite functions that cover the guarantees the
release stands on:

- the dispatch-receipt seam (the v8<->lane boundary, rebuilt in the
  receipt redesign): pass113 / pass114 / pass116 fix groups
- per-action deadlines turning into structured aborts:
  test_midtype_timeout_aborts
- effect verification after input: test_rung_effect_verification
- progress sensing (the sensor that forces strategy switches):
  test_spa_sensor_refresh, test_taskbar_strip_excluded

Everything else in trio2_reference.py is deferred, not deleted.
Offline: all boundaries are stubbed by the suite itself.
Prints CORE_OK on success. Run: python3 tests/test_core.py
"""
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

import trio2_reference as trio

CORE = [
    "test_taskbar_strip_excluded",
    "test_spa_sensor_refresh",
    "test_midtype_timeout_aborts",
    "test_rung_effect_verification",
    "test_pass113_fixes",
    "test_pass114_fixes",
    "test_pass116_fixes",
]


def main():
    v8 = trio.v8
    for name in CORE:
        # Same per-test run-state reset the full suite's runner uses.
        if hasattr(v8, "_seed_retired"):
            v8._seed_retired = False
        if hasattr(v8, "_last_cdp_window_id"):
            v8._last_cdp_window_id = ""
        if hasattr(v8, "_last_cdp_visible"):
            v8._last_cdp_visible = None
        getattr(trio, name)()
        print("PASS", name)
    print("CORE_OK")


if __name__ == "__main__":
    main()
