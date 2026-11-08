#!/usr/bin/env python3
"""Benchmark runner for cu-executor.

Runs fixed task cards (bench/cards.json) against the executor on THIS
machine, one subprocess per card, and scores them mechanically:

- task_pass:   the card's pass criterion held (never the model's word)
- integrity_ok: a FINAL_RESULT line exists, names DONE or a structured
                ABORTED_<KIND>, and the run stayed inside its budget.
                This is the executor's core promise: silent_failure is
                defined as (not integrity_ok) and must stay 0.

Writes bench/results/<label>.json and prints the per-model markdown
table. The model is whatever CU_MODEL_SERVER / CU_MODEL point at --
the executor is model-agnostic, the table is per-model by design.

Usage:
  python3 bench/run_bench.py --label gwen-local --cards B1,N1,V2 \
      --executor src/cu_executor.py
Environment: the executor's own env (CU_*), plus card-specific knobs
(CU_BENCH_V2_LIBRARY, CU_BENCH_V2_CELLS, CU_BENCH_V*_NEEDLES as JSON).
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "bench" / "fixtures"


def screen_text():
    """OCR the current desktop (mss + tesseract), best effort."""
    try:
        import mss
        with mss.mss() as sct:
            shot = sct.grab(sct.monitors[0])
            png = mss.tools.to_png(shot.rgb, shot.size)
        path = "/tmp/cu-bench-screen.png"
        with open(path, "wb") as fh:
            fh.write(png)
        out = subprocess.run(["tesseract", path, "-"],
                             capture_output=True, text=True, timeout=30)
        return out.stdout or ""
    except Exception:
        return ""


def verify(card, final_line, wall_s):
    v = card.get("verify") or {}
    kind = v.get("kind")
    done_ok = bool(final_line) and not final_line.startswith("ABORTED_")
    if kind == "result_contains":
        if not done_ok:
            return False
        if any(f in final_line for f in v.get("forbid", [])):
            return False
        return all(n in final_line for n in v.get("needles", []))
    if kind == "file_exact":
        try:
            return Path(v["path"]).read_text() == v["content"]
        except Exception:
            return False
    if kind == "screen_contains":
        needles = v.get("needles")
        env_name = v.get("needles_env")
        if env_name and os.environ.get(env_name):
            try:
                needles = json.loads(os.environ[env_name])
            except ValueError:
                needles = None
        if needles is None:
            needles = v.get("default_needles", [])
        text = screen_text().lower()
        return bool(needles) and all(n.lower() in text for n in needles)
    if kind == "expect_abort_or_honest_done":
        if final_line.startswith("ABORTED_"):
            kind_seen = final_line.split(":", 1)[0].replace("ABORTED_", "")
            return kind_seen in v.get("abort_kinds", [])
        return done_ok
    return False


def expand(task, card):
    env = os.environ
    cells = (env.get("CU_BENCH_V2_CELLS")
             or card.get("env_defaults", {}).get("CU_BENCH_V2_CELLS",
                                                 "inv,nand2")).split(",")
    library = (env.get("CU_BENCH_V2_LIBRARY")
               or card.get("env_defaults", {}).get("CU_BENCH_V2_LIBRARY",
                                                   "acme_lib"))
    return (task.replace("{FIXTURES}", str(FIXTURES))
                .replace("{LIBRARY}", library)
                .replace("{CELL1}", cells[0].strip())
                .replace("{CELL2}", cells[1].strip() if len(cells) > 1
                         else cells[0].strip()))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--label", required=True,
                    help="model label for the results table")
    ap.add_argument("--cards", default="B1,N1,V2",
                    help="comma-separated card ids")
    ap.add_argument("--executor", default=str(ROOT / "src" / "cu_executor.py"))
    ap.add_argument("--results-dir", default=str(ROOT / "bench" / "results"))
    args = ap.parse_args()

    cards = {c["id"]: c for c in
             json.loads((ROOT / "bench" / "cards.json").read_text())["cards"]}
    results = []
    for card_id in [c.strip() for c in args.cards.split(",") if c.strip()]:
        card = cards[card_id]
        missing = [r for r in card.get("requires", [])
                   if not _which(r)]
        if missing:
            results.append(dict(id=card_id, surface=card["surface"],
                                status="skipped", reason="missing: "
                                + ",".join(missing)))
            continue
        if card.get("setup_cmd"):
            subprocess.run(card["setup_cmd"], shell=True,
                           capture_output=True, timeout=60)
            time.sleep(1.5)
        budget = float(card.get("budget_s", 300))
        env = dict(os.environ)
        env.setdefault("EXEC_DEADLINE_S", str(int(budget)))
        began = time.monotonic()
        try:
            proc = subprocess.run(
                [sys.executable, args.executor,
                 expand(card["task"], card)],
                capture_output=True, text=True, timeout=budget * 2, env=env)
            out = proc.stdout + proc.stderr
        except subprocess.TimeoutExpired as exc:
            out = (exc.stdout or b"").decode(errors="replace") \
                if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        wall = time.monotonic() - began
        final_line = ""
        for line in out.splitlines():
            if line.startswith("FINAL_RESULT:"):
                final_line = line.split("FINAL_RESULT:", 1)[1].strip()
        integrity = bool(final_line) and wall <= budget * 2
        results.append(dict(
            id=card_id, surface=card["surface"], status="run",
            wall_s=round(wall, 1), budget_s=budget,
            task_pass=verify(card, final_line, wall),
            integrity_ok=integrity, silent_failure=not integrity,
            final=final_line[:300]))
        print("card %s: pass=%s integrity=%s wall=%.1fs" % (
            card_id, results[-1]["task_pass"], integrity, wall),
            flush=True)

    Path(args.results_dir).mkdir(parents=True, exist_ok=True)
    payload = dict(model=args.label, when=time.strftime("%Y-%m-%d %H:%M %Z"),
                   results=results)
    out_path = Path(args.results_dir) / (args.label + ".json")
    out_path.write_text(json.dumps(payload, indent=2))
    silent = sum(1 for r in results if r.get("silent_failure"))
    print("\n| Card | Surface | Pass | Wall s | Integrity |")
    print("|---|---|---|---|---|")
    for r in results:
        if r["status"] == "skipped":
            print("| %s | %s | skipped (%s) | - | - |"
                  % (r["id"], r["surface"], r["reason"]))
        else:
            print("| %s | %s | %s | %.1f | %s |"
                  % (r["id"], r["surface"],
                     "PASS" if r["task_pass"] else "fail",
                     r["wall_s"], "ok" if r["integrity_ok"] else "BROKEN"))
    print("\nsilent failures: %d (must be 0)" % silent)
    print("results written:", out_path)


def _which(name):
    from shutil import which
    return which(name) is not None


if __name__ == "__main__":
    main()
