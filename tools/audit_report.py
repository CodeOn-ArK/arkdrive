#!/usr/bin/env python3
"""Render a cu-executor progress log (JSONL) as a human-readable
audit timeline.

Every executor run appends one JSON object per step to its progress
log (CU_PROGRESS_LOG, default /tmp/cu-v8-progress.jsonl): timestamp,
run id, step, tool, duration, state hash before/after the step,
whether the screen changed, whether the call was wasted, and a note
(the tool result, truncated). This tool turns that flight recorder
into the timeline an operator actually reads after a run — and the
attachment a bug report should always include.

Usage: python3 tools/audit_report.py [progress.jsonl] [--run RUN_ID]
"""
import argparse
import json
import sys
from pathlib import Path


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("log", nargs="?",
                    default="/tmp/cu-v8-progress.jsonl")
    ap.add_argument("--run", default=None,
                    help="only this run_id (default: the last run in the log)")
    args = ap.parse_args()

    path = Path(args.log)
    if not path.exists():
        print("no progress log at %s" % path)
        return 1
    events = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    if not events:
        print("log has no parseable events")
        return 1
    run_id = args.run or events[-1].get("run_id")
    events = [e for e in events if e.get("run_id") == run_id]
    if not events:
        print("no events for run %s" % run_id)
        return 1

    print("run %s — %d steps" % (run_id, len(events)))
    total_ms = 0.0
    wasted = 0
    prev_hash = None
    for e in events:
        total_ms += float(e.get("ms") or 0)
        wasted += 1 if e.get("wasted") else 0
        h = str(e.get("state_hash") or "")[:10]
        delta = "" if prev_hash is None else (
            "  (screen changed)" if e.get("changed") else "  (no change)")
        print("  %s step %-3s %-12s %7.0fms  state %s%s" % (
            str(e.get("ts", ""))[:19], e.get("step"), e.get("tool"),
            float(e.get("ms") or 0), h, delta))
        note = str(e.get("note") or "").replace("\n", " ")[:140]
        if note:
            print("      %s" % note)
        prev_hash = h
    print("total tool time %.1fs across %d steps; wasted calls: %d"
          % (total_ms / 1000.0, len(events), wasted))
    return 0


if __name__ == "__main__":
    sys.exit(main())
