# ArkDrive

**A model-agnostic GUI computer-use executor for Linux/X11** — a hardened
runtime that sits between any tool-calling LLM and a real desktop, and makes
GUI work *accountable*.

## Why

GUI agents demo well and fail quietly: they hang on a frozen window, call a
misclick a success, or narrate the screen and declare victory. ArkDrive puts
everything the model can't be trusted with — time, evidence, dispatch state,
termination — in executor code, not in the prompt:

- **Per-action deadlines** — LLM calls, tools, and the whole run are
  time-boxed; nothing waits forever.
- **Fallback input ladders** — clicks and typing walk multiple mechanisms
  (CDP → keyboard → coordinates → vision) until one *verifies*.
- **Structured dispatch receipts** — the input layer states
  `{version, phase, dispatched, effect}` as data; success is never inferred
  from error-message text or the model's prose.
- **Progress sensing** — a screen-state sensor forces a strategy switch
  instead of a stall, and ends dead runs with a named abort.
- **Honest aborts, zero silent failures** — every run ends DONE-verified or
  `ABORTED_<DEADLINE|STALLED|LOOP|EMPTY|TIMEOUT|CAP|KILLED>`, with a
  flight-recorder log of every step.

The driving model is bring-your-own: anything that speaks the
OpenAI-compatible chat API and emits tool calls can drive it. Results are
published **per model**, so executor quality and model quality are never
confused.

## Quickstart

```bash
./install.sh                      # system + pip dependencies
python3 tools/check_deps.py       # per-surface readiness report
export CU_MODEL_SERVER=http://127.0.0.1:8080   # your model endpoint
export CU_MODEL=<your-model>
python3 src/cu_executor.py --check-config
python3 src/cu_executor.py "Open the text editor, type hello, save as /tmp/hello.txt"
```

Preview a run without touching the desktop, and stop one mid-flight:

```bash
python3 src/cu_executor.py --dry-run "<task>"   # actions logged, nothing dispatched
touch /tmp/cu-executor.kill                      # kill switch: halts a live run cleanly
```

## Benchmarks (Nov 8, 2026 — short version)

Same cards, same machine, one row per driving model. Full results,
methodology, and the fun runs (an agent playing Doom; an agent modeling a
rocket in Blender through the GUI) are on the
[wiki](https://github.com/CodeOn-ArK/arkdrive/wiki/Test-Results).

| Model | B1 browser | N1 native | V2 nested-desktop (flagship) | Silent failures |
|---|---|---|---|---|
| Qwen3.8-27B (local, via ArkDrive) | fail (37.0s) | fail (37.2s) | fail (44.8s, ABORTED_CAP) | **0** |
| GPT-6.1 Sol (agentic driver, same toolset) | — | — | **PASS (159s, first attempt)** | 0 |
| Human (reference) | pass | pass | pass | — |

## Safety

Dry-run previews, a file-based kill switch checked before every action,
per-run deadlines and iteration caps, and a JSONL audit log of every step.
Authentication challenges are a hard stop for a human — the executor will
not type codes, and it rejects claims of a 2FA page that isn't on screen.
See [docs/SAFETY.md](docs/SAFETY.md).

## Repository layout

```
arkdrive/
├── src/          cu_executor.py (the executor) · cua · cua-lane · lane.sh
├── tools/        check_deps.py · audit_report.py
├── bench/        cards.json · run_bench.py · RESULTS.md · fixtures/ · results/
├── tests/        test_smoke.py · test_core.py · trio2_reference.py
├── docs/         ARCHITECTURE · SAFETY · RUNBOOK_PACKS · HARDENING
├── runbooks/     example task-family pack (fictional data)
├── wiki/         sources for the GitHub wiki pages
├── install.sh
├── LICENSE       MIT
└── CHANGELOG.md
```

## Documentation

- [Wiki](https://github.com/CodeOn-ArK/arkdrive/wiki) — test results,
  architecture and safety summaries, the hardening story
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — deadlines, ladders,
  receipts, micro-agents, sensors
- [docs/SAFETY.md](docs/SAFETY.md) — dry-run, audit log, kill switch,
  human-in-the-loop points
- [docs/RUNBOOK_PACKS.md](docs/RUNBOOK_PACKS.md) — tailoring the executor
  to a task family
- [docs/HARDENING.md](docs/HARDENING.md) — how this executor was hardened
  (125 adversarial review passes, ~250 defects)

## Requirements

Linux + X11 (or XWayland for the browser surface), Python ≥ 3.10.
Wayland-native and macOS/Windows are out of scope for v1.

## License

MIT — see [LICENSE](LICENSE).
