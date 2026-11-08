# Safety Model

Summary of `docs/SAFETY.md` in the repo — read that file for the full
text.

A reliable GUI agent is dual-use: the same machinery that fills a form
for you can click consent, operate accounts, or delete files for
someone else. ArkDrive is built so an operator can answer, at any
moment, three questions: *what is it doing, what has it done, and how
do I stop it?*

## Stop it

- **Kill switch.** While the file named by `CU_KILL_FILE` (default
  `/tmp/cu-executor.kill`) exists, the executor stops before its next
  action and ends the run `ABORTED_KILLED` with a full summary.
  `touch /tmp/cu-executor.kill` halts a live run within one step.
- **Deadlines.** Every run has a wall-clock budget (default 480s) plus
  per-action timeouts; nothing waits forever on a hung window, tool,
  or model.
- **Iteration cap.** A run that cannot finish ends in a named abort,
  never an infinite loop.

## See it before it happens

- **Dry-run.** `--dry-run` (or `CU_DRY_RUN=1`) executes the full loop
  with no input dispatched — the model plans, every intended action is
  logged, nothing is clicked or typed. Preview exactly what a task
  would do.

## See it after it happened

- **Audit log.** Every step is appended to a JSONL progress log:
  timestamp, run id, step, tool, duration, before/after state hash,
  whether the screen changed, whether the call was wasted.
  `tools/audit_report.py` renders it as a human-readable timeline.
  It is the flight recorder — attach it to any bug report.

## Human-in-the-loop points

1. **Authentication** — auth challenges are a hard stop, not a puzzle.
   When a genuine 2FA/passcode page appears, the run ends and a human
   completes the sign-in. The executor will not type codes, and its
   guard rejects claims of a 2FA page that isn't actually on screen.
2. **Consequential tasks** — the operator chooses the task and reviews
   the audit log; run high-stakes tasks with `--dry-run` first.
3. **Runbook packs** — task-specific briefings are files the operator
   supplies and can read before a run; nothing task-specific hides
   inside the executor.

## What it will not do

- Claim an action happened without evidence: dispatch is stated by
  structured receipts and effects are read back — never taken from the
  model's prose.
- Fail silently: every run ends DONE-verified or in a named structured
  abort.
- Keep going when stopped: kill switch, deadline, and iteration cap
  are executor-enforced, not prompt suggestions.

## Deployment guidance

Run it on a desktop (or VM) you can afford to have automated: a
dedicated user session, no unrelated sensitive windows open, X11
session. Treat the driving model as an untrusted planner with a
supervised pair of hands: the executor supervises the hands; you
supervise the tasks. The executor never stores credentials.
