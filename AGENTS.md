# AGENTS.md — driving ArkDrive as an external agent

This repo is **ArkDrive**, a GUI computer-use toolkit for Linux/X11.
If you are a coding agent (Codex, Claude Code, or any shell-capable
agent) asked to *drive a desktop* with it, read
**[docs/DRIVERS.md](docs/DRIVERS.md)** first — it is the contract:

- `src/cua` (desktop) and `src/cua-lane` (desktop + browser CDP)
  each print exactly **one JSON object** per invocation; parse it,
  never scrape text. Failures are `{"ok": false, "error": ...}`
  with exit code 1.
- Screenshots are downscaled (≤960 px wide); **click coordinates
  are native desktop pixels** — multiply image coordinates by
  `screen_size / shot_size` (×2 on a 1920-wide desktop).
- CDP input commands return a **receipt**
  (`{version, phase, dispatched, effect}`) — branch on it, never
  on error-message text.
- Work **shot → act → shot**: verify every action from the fresh
  screenshot in the result before the next action.
- Honor the kill switch: if the file named by `CU_KILL_FILE`
  (default `/tmp/cu-executor.kill`) exists, stop. Never type 2FA /
  passcodes — those are the human's.

To run ArkDrive's own executor loop instead of driving directly:
`python3 src/cu_executor.py --check-config`, then
`python3 src/cu_executor.py "<task>"`. Driving models are set with
`CU_MODEL_PROVIDER` presets (`llama`, `openai`, `deepseek`,
`openrouter`, `anthropic`) — see docs/DRIVERS.md §1. API keys come
from the environment only (`CU_MODEL_API_KEY`); never put one in a
file or a commit.

Repo map: `src/` code · `tools/` dependency checker + audit report
· `bench/` benchmark cards and results · `tests/` smoke + core
suites · `docs/` architecture, safety, runbook packs, hardening.
