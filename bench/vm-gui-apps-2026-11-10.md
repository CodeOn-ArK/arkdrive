# GUI app tests in the generic Debian VM (Raj, Nov 10, 2026)

Five real GUI applications driven end-to-end by the ark worker inside the
generic Debian 13 VM (see bench/vm-smoke-2026-11-10.md): launch through
sessiond, wait for the window in worker state, capture a frame, act
(focus / keys / text / close), and verify against an out-of-band oracle —
never the worker's own say-so. Every event of every run was logged as JSONL
(launches, observes with durations, acts with full receipts, reconciles,
disk/pixel verifications); run logs are kept with the test driver in the
repo's hidden_files (vm_gui_test.py, gui-test-log-run{1,2,3}.jsonl).

## Result: 5/5 PASS (run 3, 13.1 s wall for the whole suite)

| App | Window up | What was done | Oracle |
|---|---|---|---|
| xterm | 425 ms | typed two shell commands | `term.txt` on disk contains exactly `gui-test-term`; both text steps verified by real post-conditions (the commands set the window title via escape sequences, ARKTERM then ARKDONE) |
| GNOME Text Editor | 433 ms | Ctrl+A, typed a sentence, Ctrl+S on an existing file | `edit.txt` on disk contains exactly `ArkDrive GUI test line.` |
| Thunar | 433 ms | Ctrl+Shift+N, typed a folder name, Return | `arkfolder/` directory exists on disk |
| GNOME Calculator | 425 ms | typed `7+5`, Return | frame bytes changed (pixel evidence only — the weakest oracle of the set) |
| Firefox ESR | 906 ms | opened a local HTML page | window title in worker state became `ArkTestPage — Mozilla Firefox`; frame captured |

Speed inside the VM (run 3): session start ~0.64 s, worker attach ~0.2 s;
state observes median 20 ms; frame observes median 12 ms (max 38 ms);
verified acts ~50-120 ms (act median 93 ms is skewed by oracle-pattern
acts that burn their 700 ms timeout on purpose — see below).

## Run 1: a driver defect, caught by the event log

Every content step in run 1 was rejected at validation with
`invalid_expectation` and nothing was dispatched (the calculator's
before/after frames were byte-identical). Cause: the driver set each
step's `expect` to `window_present` of the app's own window, which is
already true before dispatch. The spec's rule (build spec R6) is that a
mutating step's expect must be a post-condition that is false before and
true after; an already-true strong predicate is invalid. Firefox, which
needed no acts, passed in run 1 as well.

Fix pattern used from run 2 on: where the app offers a natural
post-condition, declare it (xterm title escapes; dialog appear/disappear);
otherwise use the oracle pattern from W2 gate E4 — expect a title that
never appears with a 700 ms timeout: the step dispatches
(`dispatch_status: accepted`), the receipt reads failed/timeout by
design, one full settled observe reconciles the pending action, and the
disk/pixel oracle decides the verdict.

## Run 2: the Thunar modal rule (product behavior, now documented)

Run 2 passed 4/5. Thunar failed with a new signature: Ctrl+Shift+N
dispatched, its receipt read `effect_contradicted`, and every later act —
including the close — was refused at admission with
`modal_still_present`, `dispatch_status: not_dispatched`.

Root cause, confirmed by a probe and by reading worker_act.py: this
Thunar's Ctrl+Shift+N opens a real modal dialog titled **Create New
Folder** (type dialog, is_modal, takes focus). While a modal is up, the
worker's precondition code refuses any step whose target is not the
modal window itself, and a modal appearing undeclared contradicts the
batch that caused it. This is the R6 unexpected-state protection working
as designed; the driver was at fault.

Correct driver protocol for modals (run 3, all steps verified):
1. The step that opens the dialog declares it:
   `expect = dialog_present(match title)`.
2. Read the modal's handle from state (it is in `state.windows` with
   `is_modal: true`, and its handle is in `state.modal`).
3. Address subsequent steps to the modal's handle; declare
   `require = {"modal": {match}}` on them.
4. Commit with a step whose expect is `dialog_absent(match)`.

With that, Thunar's n1 verified `success` outright, the name typing
dispatched to the modal, Return closed it with a verified
`dialog_absent`, and the folder was on disk.

## Not covered

GNOME Calculator's verdict rests on pixel change only; reading its
display text waits for the W4 tree provider. No app was tested on the
xorg profile or on bare metal; these runs are all xvfb inside the VM.
