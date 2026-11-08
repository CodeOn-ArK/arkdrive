# Architecture

cu-executor is a single Python process (`src/cu_executor.py`) that runs
a tool-calling LLM against a real Linux/X11 desktop. The model plans;
the executor enforces. Everything the model cannot be trusted with —
time, evidence, dispatch state, termination — lives in executor code,
not in the prompt.

```
operator ──task──> executor loop ──tool calls──> micro-agents / rungs
                      │  ▲                              │
                      │  └── observe / OCR / pagetext ──┤ cua / cua-lane
                      └──── progress log (JSONL)        └── desktop (X11)
```

## The loop

One tool call per model turn. Before every LLM call and every tool,
the executor checks the global deadline (default 480s,
`EXEC_DEADLINE_S`) and the kill switch. Each run ends in exactly one
of: a model `done` the guards accept, or `ABORTED_<KIND>` —
`DEADLINE`, `STALLED`, `LOOP`, `EMPTY`, `TIMEOUT`, `CAP`, `KILLED` —
followed by a run summary (steps, elapsed, steps/sec, wasted calls,
LLM ms vs tool ms). An empty or missing final result is structurally
impossible: a fallback constructs one.

## Deadlines

| Scope | Default | Knob |
|---|---|---|
| LLM call | 60s | `CU_TIMEOUT_LLM` |
| shell / read / edit / verify | 30s / 10s | `CU_TIMEOUT_<TOOL>` |
| observe / pagetext / OCR | 15–20s | same pattern |
| micro-agents (kbd/mouse/win) | 25s | same pattern |
| whole run | 480s | `EXEC_DEADLINE_S` |
| iterations | 10 | `MAX_ITERS` |

Timeouts are enforced with an alarm around the call, so even a runner
that ignores its own timeout is interrupted and reported as a
structured `ERROR: TIMEOUT <tool>`.

## Input ladders and dispatch receipts

Clicks and typing never rely on a single mechanism. Each action walks
a **ladder** of rungs until one verifies:

- click, browser (CDP): CDP → keyboard → cua coords → vision
- click, native/VNC: keyboard → xdotool coords → cua coords → AT-SPI → vision
- type: focus-verify → safe-clipboard paste → xdotool type → cua type
  (VNC: chunked xdotool with pixel verification; never clipboard chords)

On the browser lane, every CDP input command returns a **structured
dispatch receipt** — `{version, phase, dispatched, effect}` — from
`cua-lane`, and the executor branches on `receipt.dispatched` only.
Dispatch state is data stated by the sender; it is never inferred from
error-message text. The contract is versioned: a version skew raises
`ReceiptVersionError` (loud, never a silent fallback), and a lane call
that produces no receipt has exactly one defined behavior — treated
as not dispatched, the ladder advances. (This boundary previously
classified failures by matching error-string prefixes; it was the one
structural defect found by the hardening passes and was rebuilt. See
HARDENING.md.)

## Micro-agents

Deterministic Python sub-agents handle mechanical work with zero LLM
calls: `kbd_agent` (focus-walk, key chords, type-with-readback
verification), `mouse_agent` (the click ladders), `win_agent`
(list/focus/raise windows). Anything that can be verified mechanically
— typed text read back, file bytes hashed — is verified mechanically.

## Progress sensing

After each tool step the executor hashes the state (window title, URL,
page text, or a region hash of the visible pixels) and records it in
the JSONL progress log. Two consecutive no-change steps inject a
`SWITCH` instruction naming a different ladder; two more end the run
`ABORTED_STALLED`. The sensor of record depends on the surface:
browser surfaces use DOM/page state; native uses focus + pixel hash;
**on VNC the visible screen is the only sensor of record** — observe
carries OCR of the same frontmost pixels the OCR tool reads, and
focused-window metadata is advisory only, because inside a remote
desktop the focused local window and the visible content routinely
disagree.

## Completion discipline

Models surrender: they narrate the screen and call it done, claim a
2FA page that isn't showing, or write a conclusion so long it
truncates into malformed JSON. The executor defends:

- **Auth-evidence guard:** a completion claiming an auth/2FA page
  (the `AT_DUO` result prefix) is rejected unless an auth UI actually
  appeared in the run's observations. The marker list is configurable
  (`CU_AUTH_MARKERS`).
- **Engagement floor (VNC):** a `done` is rejected (max 3) until the
  run has genuinely engaged both input modalities the task requires;
  each rejection names the literal next call.
- **Truncation boost:** a reply cut off at the generation cap gets one
  retry at a larger budget with a hard brevity instruction, instead of
  dying as an empty response. Done results are capped at 40 words by
  instruction.
- **Done is for completed work:** results that merely narrate visible
  state are called out in the prompt contract and, on VNC, by the
  engagement floor.

## Surfaces

`browser` (Chromium via cua), `browser_cdp` (lane-launched Chromium
with a debug port — the receipt path), `native` (any X11 app), `vnc`
(a remote desktop inside a viewer window). Surface is classified from
the focused window at run start and re-probed per step.

## The substrate

`src/cua` is the X11 computer-use CLI (screenshots, input, tabs); it
emits exactly one JSON object per invocation. `src/cua-lane` adds the
CDP command set + receipts for lane-launched browsers. `src/lane.sh`
is an optional reference setup for running several isolated browser
sessions (per-lane displays, profiles, debug ports); single-desktop
use doesn't need it.

## Configuration

Environment first, optional JSON file (`CU_CONFIG`) as defaults:
`CU_MODEL_SERVER`, `CU_MODEL`, `CU_VISION_SERVER`, `CUA_BIN`,
`CUA_CDP_PORT`, `CU_RUNBOOK_PACK`, `CU_KILL_FILE`, `CU_DRY_RUN`,
`CU_PROGRESS_LOG`, `CU_AUTH_MARKERS`, plus the timeout knobs above.
`--check-config` prints the effective configuration. Legacy `QWEN_*`
names still work as fallbacks. Task-specific behavior belongs in a
runbook pack (RUNBOOK_PACKS.md), never in the executor.
