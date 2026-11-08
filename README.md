# ArkDrive

A **model-agnostic GUI computer-use executor** for Linux/X11: a hardened
runtime that sits between any tool-calling LLM and a real desktop, and
makes GUI work *accountable*. Per-action deadlines, a screen-progress
sensor that forces strategy switches instead of stalls, fallback input
ladders with structured dispatch receipts, deterministic micro-agents
for mechanical sub-tasks, and a flight-recorder log for every run —
every run ends in DONE-verified or a named structured abort
(`ABORTED_DEADLINE / STALLED / LOOP / EMPTY / TIMEOUT / KILLED`), never
a silent failure.

The driving model is bring-your-own: anything that speaks the
OpenAI-compatible chat API and emits tool calls can drive it. Scores
are published **per model** (see `bench/`) so the executor's quality
and the model's quality are never confused.

## Quickstart

```bash
./install.sh                      # system + pip dependencies
python3 tools/check_deps.py       # per-surface readiness report
export CU_MODEL_SERVER=http://127.0.0.1:8080   # your model endpoint
export CU_MODEL=<your-model>
python3 src/cu_executor.py --check-config
python3 src/cu_executor.py "Open the text editor, type hello, save as /tmp/hello.txt"
```

Planning a run without touching the desktop:

```bash
python3 src/cu_executor.py --dry-run "<task>"   # actions logged, nothing dispatched
touch /tmp/cu-executor.kill                      # kill switch: halts a live run cleanly
```

## Documentation

- `docs/ARCHITECTURE.md` — deadlines, ladders, receipts, micro-agents, sensors
- `docs/SAFETY.md` — dry-run, audit log, kill switch, human-in-the-loop points
- `docs/RUNBOOK_PACKS.md` — tailoring the executor to a task family
- `docs/HARDENING.md` — how this executor was hardened (125 adversarial passes)
- `bench/` — the public benchmark suite and per-model results

## Requirements

Linux + X11 (or XWayland for the browser surface), Python ≥ 3.10.
Wayland-native and macOS/Windows are out of scope for v1.
