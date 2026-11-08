# Architecture

Summary of `docs/ARCHITECTURE.md` in the repo — read that file for the
full text, configuration reference, and timeout table.

ArkDrive is a single Python process (`src/cu_executor.py`) that runs a
tool-calling LLM against a real Linux/X11 desktop. **The model plans;
the executor enforces.** Everything the model cannot be trusted with —
time, evidence, dispatch state, termination — lives in executor code,
not in the prompt.

## The loop

One tool call per model turn. Before every LLM call and every tool,
the executor checks the global deadline (default 480s) and the kill
switch. Each run ends in exactly one of: a model `done` the guards
accept, or a named abort — `ABORTED_DEADLINE / STALLED / LOOP / EMPTY /
TIMEOUT / CAP / KILLED` — followed by a run summary (steps, elapsed,
steps/sec, wasted calls, LLM ms vs tool ms). An empty or missing final
result is structurally impossible.

## Input ladders and dispatch receipts

Clicks and typing never rely on a single mechanism; each action walks a
ladder of rungs until one *verifies*:

- click, browser (CDP): CDP → keyboard → coordinates → vision
- click, native/VNC: keyboard → xdotool → cua → AT-SPI → vision
- type: focus-verify → safe-clipboard paste → xdotool → cua
  (VNC: chunked typing with pixel verification; never clipboard chords)

On the browser lane, every CDP input command returns a **structured
dispatch receipt** — `{version, phase, dispatched, effect}` — and the
executor branches on `receipt.dispatched` only. Dispatch state is data
stated by the sender, never inferred from error-message text. The
contract is versioned: skew raises a loud error, never a silent
fallback.

## Micro-agents

Deterministic Python sub-agents handle mechanical work with zero LLM
calls: `kbd_agent` (focus-walk, chords, type-with-readback),
`mouse_agent` (the click ladders), `win_agent` (list/focus/raise
windows). Anything that can be verified mechanically — typed text read
back, file bytes hashed — is verified mechanically.

## Progress sensing

After each step the executor hashes the state (window title, URL, page
text, or a region hash of the visible pixels) into the JSONL progress
log. Two consecutive no-change steps inject a strategy-switch
instruction naming a different ladder; two more end the run
`ABORTED_STALLED`. On VNC, the **visible screen is the only sensor of
record** — inside a remote desktop, the focused local window and the
visible content routinely disagree, so observation, OCR, and the
progress hash all read the same frame and window metadata is advisory
only.

## Completion discipline

Models surrender: they narrate the screen and call it done, claim a
2FA page that isn't showing, or write conclusions so long they
truncate into malformed output. The executor defends with an
auth-evidence guard (a claimed 2FA page must have appeared in the
run's observations), an engagement floor on VNC (a `done` is rejected
until the run has genuinely engaged the required input modalities),
and a truncation retry at a larger budget with a brevity instruction.

## Surfaces

`browser` (Chromium via cua), `browser_cdp` (lane-launched Chromium —
the receipt path), `native` (any X11 app), `vnc` (a remote desktop
inside a viewer window). Task-specific behavior belongs in a runbook
pack (`docs/RUNBOOK_PACKS.md`), never in the executor.
