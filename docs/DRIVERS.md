# Drivers: models and agents that run ArkDrive

ArkDrive has two integration surfaces, and both are first-class:

1. **The executor loop** — ArkDrive's own runtime
   (`src/cu_executor.py`) calls a driving model one tool call per
   step and enforces deadlines, ladders, and honest aborts around it.
   The model is configured with provider presets (below).
2. **The agentic driver** — an external coding agent (Codex, Claude
   Code, or any agent that can run shell commands and view images)
   skips the loop and drives the same tool layer — `cua` and
   `cua-lane` — directly, planning in its own turns. This is the
   shape that passed the flagship nested-desktop benchmark card.

Pick the loop when the model is small/local and needs guardrails.
Pick an agentic driver when the model is a strong coding agent that
does better with free-form perception and its own verify loops.

---

## 1. Executor-loop models: provider presets

Set `CU_MODEL_PROVIDER`; a preset only fills defaults, and
`CU_MODEL_SERVER` / `CU_MODEL` always override it:

| Preset | Default endpoint | Wire format | Default model | API key |
|---|---|---|---|---|
| `llama` (default) | `http://127.0.0.1:8080` | OpenAI-compatible + llama.cpp extensions | `local-model` | not required |
| `openai` | `https://api.openai.com` | OpenAI chat completions | — set `CU_MODEL` | required |
| `deepseek` | `https://api.deepseek.com` | OpenAI chat completions | `deepseek-chat` | required |
| `openrouter` | `https://openrouter.ai/api` | OpenAI chat completions | — set `CU_MODEL` | required |
| `anthropic` | `https://api.anthropic.com` | Anthropic Messages (translated) | — set `CU_MODEL` | required |

```bash
export CU_MODEL_PROVIDER=deepseek
export CU_MODEL_API_KEY=...            # environment only, see below
python3 src/cu_executor.py --check-config
python3 src/cu_executor.py "<task>"
```

Rules that apply to every preset:

- **Keys come from the environment only** (`CU_MODEL_API_KEY`). They
  are never read from the `CU_CONFIG` file, never written to disk by
  ArkDrive, and never logged — `--check-config` prints
  `set (value redacted)` or `not set`, never the value.
- **Any other OpenAI-compatible endpoint** works without a preset:
  use the `openai` wire format by setting `CU_MODEL_SERVER` and
  `CU_MODEL` (a local vLLM/SGLang/Ollama server, a corporate gateway,
  a proxy in front of Anthropic, …).
- **Anthropic is translated, not proxied.** The executor's single
  HTTP call site reshapes the request for the Messages API: system
  messages lift to the top-level `system` field, auth is `x-api-key`
  + `anthropic-version`, the token budget becomes `max_tokens`, and
  the response's content blocks and `stop_reason` map back to the
  text + OpenAI `finish_reason` spellings the loop already handles.
- **Sampling is tight by design** (temperature 0.1): the loop wants
  one exact tool call per step, not creativity. Hosted presets send
  only portable fields (`max_tokens`, `temperature`, `top_p`);
  llama.cpp extensions (`n_predict`, `cache_prompt`, `top_k`,
  `repeat_penalty`) go only to the `llama` preset.
- **The vision rung is independent**: `vision_text` always speaks
  OpenAI-compatible chat to `CU_VISION_SERVER` (default a local
  server on :8081), whatever preset drives the main loop.
- `--check-config` validates per preset — unknown provider, missing
  key, or missing model are reported in `config_problems` with exit
  status 1, before any desktop or network action happens.

The driving model must emit exactly one tool call per turn in the
executor's JSON tool format (see `SYSTEM_PROMPT` in
`src/cu_executor.py`). That requirement — not the vendor — is what
makes a model compatible with the loop.

---

## 2. Agentic drivers: the tool-layer contract

An external agent drives the desktop by running two CLI tools and
reading their JSON. No SDK, no daemon, no protocol version to
negotiate beyond what's below.

### Tool inventory

**`src/cua`** — desktop input/sensing over X11 (XTest):

| Command | Effect |
|---|---|
| `cua shot` | screenshot (see conventions) |
| `cua click <x> <y> [--right\|--middle]` | click at native pixels |
| `cua move <x> <y>` | move pointer |
| `cua scroll <up\|down> <count>` | scroll at pointer |
| `cua key <combo>` | key chord, xdotool syntax (`ctrl+l`, `Return`, `alt+Tab`) |
| `cua type <text>` | type text |
| `cua paste <text>` | set clipboard + paste |
| `cua clip get [target]` / `cua clip set <text>` | clipboard read/write |
| `cua win list` | list windows |
| `cua win focus <window>` | focus a window (id or title match) |
| `cua tab goto <url>` / `cua tab new <url>` | drive cua's own browser tab |
| `cua release` | release cua's held resources |

**`src/cua-lane`** — everything `cua` does, plus Chrome DevTools
Protocol commands against a lane browser (lanes are managed by
`src/lane.sh <1|2|3> start|claim|release|env`):

| Command | Effect | Receipt |
|---|---|---|
| `cua-lane cdp-click <selector>` | click element by CSS selector | ✅ |
| `cua-lane cdp-clicktext <text>` | click element containing text | ✅ |
| `cua-lane cdp-clickxy <x> <y>` | click at page coordinates | ✅ |
| `cua-lane cdp-type <text>` | type into the focused element | ✅ |
| `cua-lane cdp-key <combo>` | key chord via CDP | ✅ |
| `cua-lane cdp-setfile <selector> <path>` | attach a file to a file input | ✅ |
| `cua-lane cdp-text` | page text (DOM) | — |
| `cua-lane cdp-eval <expr>` | evaluate JS, return result | — |
| `cua-lane cdp-shot` | page screenshot | — |

**`src/cu_executor.py "<task>"`** — a driver may also delegate a
whole sub-task to the executor loop (with its deadlines and
ladders) and just read the `FINAL_RESULT:` line.

### Conventions

- **One JSON object per invocation**, printed to stdout. Success:
  `{"ok": true, "window": <focused title>, "window_id": <id>,
  "shot": <path>, "screen_size": [w, h], "shot_size": [w, h], ...}`.
  Failure: `{"ok": false, "error": "<what happened>"}` and exit
  code 1. Parse the JSON; never scrape human text.
- **Screenshots are downscaled.** `shot` points to a JPEG capped at
  960 px wide (a 1920×1080 desktop yields a 960×540 shot).
  `screen_size` is the real desktop size, `shot_size` the image's.
- **Coordinates are native desktop pixels.** `click`/`move` take
  screen pixels, not image pixels: multiply image coordinates by
  `screen_size / shot_size` (exactly ×2 on a 1920-wide desktop
  with a 960-wide shot). Getting this wrong lands clicks at
  quarter-offsets — the classic first-run bug.
- **Receipts are the dispatch truth.** The CDP input commands carry
  `"receipt": {"version": 1, "phase": ..., "dispatched": bool,
  "effect": ...}` in their result — including inside the error
  envelope when a command fails after dispatch. `phase` walks
  `lookup → dispatch → done`; `dispatched` flips true the moment
  the browser accepts the input, so a failure *after* that point
  still reports a sent click. `effect` is `verified` when a
  readback confirmed the result (e.g. `cdp-setfile`), else
  `unverified`. **Branch on the receipt, never on error text.** A
  receipt `version` you don't recognize means stop and reconcile —
  do not fall back to guessing from messages.
- **Work in a shot → act → shot loop.** Every cua result already
  includes a fresh screenshot; take the next action from what it
  shows, not from what you expected. Never batch blind actions.
- **Windows are not tabs.** Focus and verify by window title/id
  from `cua win list` / the result's `window` fields; a click into
  the wrong window is the second classic bug.

### Safety rails

Driving through the executor loop gets you its rails for free:
per-action deadlines, `CU_DRY_RUN=1` (actions logged, nothing
dispatched), the kill switch, and the JSONL audit log
(`CU_PROGRESS_LOG`, rendered by `tools/audit_report.py`).

Driving `cua` directly, you hold the tools yourself — honor the
same conventions:

- **Kill switch**: the executor checks for the kill file
  (`CU_KILL_FILE`, default `/tmp/cu-executor.kill`) before every
  action and ends `ABORTED_KILLED` if it appears. A driver should
  check for the same file between steps and stop the same way.
- **Audit trail**: cua's per-command JSON *is* your log — keep the
  outputs (they carry window, screenshot path, and the receipt).
- **Human-in-the-loop stops**: authentication challenges (2FA,
  passcodes, push approvals) are for the human. Never type codes.
- **Lanes are exclusive**: claim a lane (`lane.sh <n> claim`) before
  driving its browser and `release` it after; two drivers in one
  lane corrupt each other's state.

---

## 3. Per-agent quickstart

### Codex — proven

Verified end-to-end on the recorded benchmark runs (see
`bench/RESULTS.md`): Codex (GPT-6.1 Sol) driving `cua` passed the
flagship nested-desktop card on the first attempt (159 s), and
completed the Doom and Blender fun runs the same way.

```bash
# detached, with the desktop in scope -- the pattern that worked:
setsid nohup codex exec --skip-git-repo-check -m <model> \
  -s danger-full-access < /dev/null > run.log 2>&1 &
# prompt sketch: state the task, name the tools (cua path), the
# coordinate rule (native = shot x2), "work shot -> act -> shot,
# verify from the screen, report facts only", and any read-only or
# scope limits for the task.
```

What made it work: vision on every step (screenshots viewed
natively, crops/upscales when text is small), self-verification
inside the target app, and recovery in stride from its own tooling
errors instead of surrendering.

### Claude Code — same contract

Claude Code drives identically: its shell tool runs `cua` /
`cua-lane`, and it can view the screenshot files cua returns. Give
it the same prompt skeleton as Codex: task, tool paths, the ×2
coordinate rule, shot → act → shot discipline, receipt branching
for CDP commands, kill-file checks between steps, and the task's
scope limits. An `AGENTS.md` / `CLAUDE.md` note pointing at this
document is the durable way to brief it (this repo ships an
`AGENTS.md` that does exactly that).

### Generic agents — requirements checklist

Any agent that can do all four of these can drive ArkDrive:

1. run shell commands and capture stdout,
2. view an image file (the `shot` JPEG) — or read screen text via
   OCR/`cdp-text` if it cannot,
3. parse one JSON object per command result,
4. hold a multi-step plan across turns.

Meet those and the contract above is the whole integration: no
ArkDrive code changes, no registration, no server to run. If the
agent cannot view images, prefer `cua-lane`'s CDP commands
(DOM-level, coordinate-free) wherever the target is a browser.
