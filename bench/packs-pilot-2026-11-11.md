# App packs measurement pilot — 2026-11-11

## Design

Tooling only. Raj runs the pilot on ArK2. This follows the Measurement section
of `hidden_files/COUNCIL-AGENTS-RECONCILED.md` and the live-verified Thunar pack
described in `bench/packs-thunar-2026-11-11.md`.

The 27B arms are the existing trimmed generalist (`ARK_ACT_PACKS=off`) and
the same driver with the Thunar pack (variable absent). The adapter reads the
switch once at process import: exactly `off` skips the pack import/discovery,
does not consult the registry, publishes no `use_skill`, and appends no pack
notes. Unset and every other value preserve the existing inventory and pack
behavior. Generic act grammar remains identical in both arms.

`hidden_files/packs_pilot.py` is a self-contained stdlib harness, intentionally
not tracked. It imports no other harness, worker or third-party module. One
invocation runs both tasks and arms, with `--runs 10` by default: create G(0),
P(0), G(1), P(1), …, G(9), P(9), then rename in the same order. This gives
10 pairs per task, 20 pairs / 40 runs total. Run indices are zero-based. Each
pair gets the same task text, filenames, seeds, fresh session state, model,
loop prompt, sampling, history budget, completion budget and 40-turn cap.
The fixed G→P order provides interleaving; it is not randomized or
counterbalanced and cannot eliminate systematic within-pair order effects.

Each run copies the slate recipe from `~/wm-w6-verify.py`: sessiond
`stop --all`, wait 0.5 seconds, delete `STATE/run/*`, delete
`STATE/acts/*.jsonl`. The copy checks successful teardown and an empty
registry before deleting. Sessiond state is `~/.local/state/ark-sessiond`.
No persistent route table is deleted by this recipe. A fresh xvfb session
and worker start next; agent control and a successful XTEST self-test are
required. The harness reads the registry's instance-specific `session_dir`
and `home`, checks the xvfb profile and display in 91–120, then seeds
`seed-a.txt` and `seed-b.txt`. Rename additionally seeds
`pilot-file-<i>.txt`. Thunar opens that home, and a visible, focused Thunar
window must appear before the loop starts. There is no view-switch setup.
No host display is inherited, opened or driven; `:0` is never used.

The loop runs as a subprocess with `CU_MODEL_PROVIDER=llama`, the preset's
default endpoint/model (inherited `CU_MODEL_SERVER` and `CU_MODEL` are
removed), and `CU_LOOP_HTTP_TIMEOUT=300`. Other loop budgets/settings are
inherited equally. Only G adds `ARK_ACT_PACKS=off`; P explicitly removes an
inherited switch. The loop receives `--max-turns 40` and exactly these task
texts, with the same index substitution for both arms:

```
In Thunar, create a new folder named pilot-probe-<i> in the home folder that is open. When it exists, call done.
In Thunar, rename pilot-file-<i>.txt to pilot-renamed-<i>.txt. When the rename is done, call done.
```

The 900-second run deadline begins before slate and limits subprocesses
through loop execution. Timeout kills the loop's process group, including
its MCP adapter child. Thunar startup and worker readiness each have a
30-second setup limit. Cleanup has a separate, bounded 60-second allowance,
so measured wall time can exceed 900 seconds. Kill-file and SIGINT/SIGTERM
stops terminate the active command, record the active run and stop the
matrix; ordinary harness/session failures record a failed row and continue.
Cleanup uses sessiond teardown, including for detached Thunar processes
carrying session/instance markers.

## Oracle and scoring bars

The session filesystem is the only task-success oracle, read before teardown:

- Create: `home/pilot-probe-<i>` is a directory.
- Rename: `home/pilot-renamed-<i>.txt` exists and
  `home/pilot-file-<i>.txt` does not.

An acknowledged `done(success)` cannot establish success. A loop-reported
failure can still have oracle success if the expected files are on disk.
Harness/setup/timeout/cleanup errors add `harness_error` and force
`oracle_success:false`, even if the filesystem happens to contain the target.
Unavailable observations are JSON `null`; they are never replaced with zero
or estimates. Each result is appended and fsynced to
`~/packs-pilot-results.jsonl`; existing rows are preserved. Separate harness
invocations append new matrices with repeated indices, so Raj should keep
their invocation boundaries when analyzing multiple batches.

The reconciled plan scores 27B on oracle success and invalid-form rate. Its
baseline has zero recorded passes, so tokens per success is undefined.
Compare success per arm/task and matched G/P outcomes; retain failure rows
in the denominators. An invalid-form result means a tool result/receipt
whose `reason` or `error` names `invalid_params`, `invalid_expectation`,
`invalid_request`, or `precondition_failed`, counted once per returned tool
call even if several nested steps qualify. The intended rate is qualifying
returned tool calls / returned tool calls, with the same definition in both
arms.

**Current observability limit:** `worker_loop.run_loop()` retains tool
results in memory, but neither it nor `main()` prints per-call results or
error counters. `main()` prints one final JSON summary only. Consequently
`errored_tool_calls` and `form_errors` are always `null` in this harness;
the invalid-form rate cannot yet be scored from its output. Journals are
not a complete substitute: they omit rejected pre-admission calls and do
not represent all MCP results. No benefit claim about invalid forms can be
made from these rows; complete call-result instrumentation is needed for
that comparison. The harness does not change loop logging or guess counts
from a final reason, hints, exit status or worker counters.

For the later Codex arm, the token bar is **≤50% of the passing baseline**,
with turns reported and a correctness-regression veto. This is follow-up
measurement; the 27B pilot does not establish a Codex token win.

## Field sources

The loop sources below refer to `wm/worker_loop.py`'s `run_loop()` summary
and the single JSON object printed by `main()`. The tool count is returned
MCP calls, including observe/wait/done/use_skill; it is not internal worker
acts/steps inside a skill.

| JSONL field | Source / absent behavior |
| --- | --- |
| `arm` | Harness matrix: `generalist` or `pack`. |
| `task` | Harness matrix: `create` or `rename`. |
| `run` | Zero-based pair index from `range(--runs)`. |
| `done_status` | Summary `done.status`, acknowledged by adapter; `null` without done/summary. Harness sets `timeout` for the 900-second deadline. |
| `turns` | Summary `turns_used`; `null` without summary. This counts attempted loop iterations, not necessarily completed provider calls. |
| `tokens_total` | Summary `token_usage.total_tokens`, accumulated from numeric provider usage. `null` if that exact total is absent; no character estimate or prompt/completion sum is substituted. |
| `wall_s` | Harness monotonic elapsed seconds from before slate through oracle and teardown, rounded to milliseconds; includes setup/cleanup. |
| `tool_calls` | Summary `tool_calls_made`, incremented after each returned MCP tool call; `null` without summary. |
| `errored_tool_calls` | `null`: per-call errors/results are not printed or summarized by the loop. |
| `form_errors` | `null`: qualifying reason/error names in returned results/receipts are not observable in loop stdout. Definition and limitation above. |
| `oracle_success` | Filesystem predicates above, before teardown; forced false for a harness failure. Never derived from done, exit code, receipt or model prose. |
| `harness_error` (failures only) | Caught harness/setup/oracle/cleanup exception; `wall_timeout` for the overall deadline. Not a tool-error counter. |
| `loop_reason` (summary available) | Summary `reason` verbatim, possibly null. Diagnostic only. |
| `loop_exit_code` (summary available) | Subprocess return code. Diagnostic only; not a success oracle. |

A timeout or missing/malformed summary leaves all unavailable summary
metrics null, including when some turns/calls may have occurred before the
process was killed. No partial totals are inferred.

## Running and validation

Raj can run the checkout copy directly:

```sh
python3 ~/arkdrive/hidden_files/packs_pilot.py --runs 10
```

To use the requested home-directory copy:

```sh
cp ~/arkdrive/hidden_files/packs_pilot.py ~/packs_pilot.py
python3 ~/packs_pilot.py --runs 10
```

The copy attempt during implementation was blocked by the environment:
`/home/ark/packs_pilot.py` is on a read-only filesystem outside the writable
workspace. The checkout copy is complete and runnable; no pilot or result
file was created during implementation. Nothing staged or committed.

Headless checks cover off/unset/on/other switch values, import/registry
bypass, settled Thunar observe block count, MCP inventory and skill denial.
Private harness checks in `hidden_files/test_packs_pilot.py` cover matrix
order, matched environments, actual summary fields/nulls, independent
filesystem oracles, failure continuation, pre-teardown oracle timing,
timeout recording and process-group termination. They use fake commands
and temporary filesystem fixtures only; no X display or session is started.

Validation commands (no live sessions):

```sh
TMPDIR="$PWD/wm/tests/.tmp" PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s wm/tests
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s hidden_files -p test_packs_pilot.py -v
```

Full headless suite: **1058 tests run in 132.785 s, 1057 passed and 1
skipped**, exit 0, `OK (skipped=1)`. The existing ptrace/strace check is
skipped in this environment. Log:
`wm/tests/.tmp/packs-pilot-full-suite.log`. Private harness checks: **10
passed**. The harness `--help` entry point also passed without starting a
session. The private tests stay alongside the untracked harness so the
repository's default suite does not depend on that local file.

## Results — NOT RUN

**NOT RUN. Raj runs the pilot and fills this section.** No live paired runs,
model calls, success-rate comparison or pack benefit claim was made here.

Held-out name collisions, the 27-folder Thunar case, unexpected modals,
app startup/closure timing and interrupted responses are follow-up work.
The two seeded happy-path tasks do not evaluate those cases.
