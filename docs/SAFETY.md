# Safety model

A reliable GUI agent is dual-use: the same machinery that fills a form
for you can click consent, operate accounts, or delete files for
someone else. This executor is built so that an operator can answer,
at any moment, three questions: *what is it doing, what has it done,
and how do I stop it.*

## Stop it

- **Kill switch.** While the file named by `CU_KILL_FILE` (default
  `/tmp/cu-executor.kill`) exists, the executor stops before its next
  action and ends the run `ABORTED_KILLED` with a full summary.
  `touch /tmp/cu-executor.kill` from any shell halts a live run within
  one step. Set `CU_KILL_FILE=""` to disable (not recommended).
- **Deadlines.** Every run has a wall-clock budget (default 480s) and
  per-action timeouts; nothing waits forever on a hung window, a hung
  tool, or a hung model.
- **Iteration cap.** A run that cannot finish ends in a named abort,
  never an infinite loop.

## See it before it happens

- **Dry-run.** `--dry-run` (or `CU_DRY_RUN=1`) executes the full loop
  against a simulated desktop: the model plans, every intended action
  is logged, and **no input is dispatched** — no clicks, no typing, no
  clipboard changes. Use it to preview exactly what a task would do.
- **Action budgets.** `MAX_ITERS` + the global deadline bound the
  blast radius of any single run.

## See it after it happened

- **Audit log.** Every step is appended to a JSONL progress log
  (`CU_PROGRESS_LOG`, default `/tmp/cu-v8-progress.jsonl`): timestamp,
  run id, step, tool, duration, before/after state hash, whether the
  screen changed, and whether the call was wasted. `tools/audit_report.py`
  renders it as a human-readable action timeline. If you file a bug,
  attach this log — it is the flight recorder.
- **Secrets hygiene.** The executor never stores credentials. Auth
  challenges are a hard stop, not a puzzle: when a real 2FA/passcode
  page appears, the run ends with an `AT_DUO` completion and a human
  completes the sign-in. The executor will not type codes, and its
  guard rejects claims of a 2FA page that isn't actually on screen.

## Human-in-the-loop points

Designed stops where a person, not the model, decides:

1. **Authentication** — any genuine 2FA/passcode UI (above).
2. **Consequential tasks** — the operator chooses the task and reviews
   the audit log; run high-stakes tasks with `--dry-run` first.
3. **Runbook packs** — task-specific briefings are files the operator
   supplies and can read before a run (`CU_RUNBOOK_PACK`); nothing
   task-specific hides inside the executor.

## What it will not do

- It will not claim an action happened without evidence: dispatch is
  stated by structured receipts and effects are read back (typed text,
  file hashes, DOM state, OCR) — never taken from the model's prose.
- It will not fail silently: every run ends DONE-verified or in a
  named structured abort. If you see anything else, that's a bug —
  attach the audit log.
- It will not keep going when stopped: kill switch, deadline, and
  iteration cap are executor-enforced, not prompt suggestions.

## Deployment guidance

Run it on a desktop (or VM) you can afford to have automated: a
dedicated user session, no unrelated sensitive windows open, X11
session (Wayland's input model is out of scope for v1). Treat the
driving model as an untrusted planner with a supervised pair of
hands: the executor supervises the hands; you supervise the tasks.
