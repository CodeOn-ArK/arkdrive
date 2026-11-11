# Memory cleanup Step 0 — counters and soak harness

2026-11-11, headless implementation on this machine. Read
`hidden_files/COUNCIL-MEMORY-RECONCILED.md`. W0–W8 remain closed.
**Live baseline: NOT RUN** (no live sessions in this sandbox).
No display, including `:0`, was opened or driven. Nothing staged or committed.

## Production change: additive `system.info.memory`

`system.info` adds this stable schema-1 block alongside the existing
`counters` block; existing fields and the ten cumulative action counters
are unchanged. Other RPC results do not acquire this block.

```json
{
  "memory": {
    "schema": 1,
    "actions_retained": 0,
    "lease_records": 0,
    "removed_windows": 0,
    "frame_identities": 0,
    "frame_files": 0,
    "atspi_apps": 0,
    "journal_bytes": 0,
    "pending_x_requests": 0
  }
}
```

These are instantaneous gauges, not lifetime totals or byte estimates of
Python objects. Sizes may decrease under existing behavior.

| Key | Meaning/source | Unit / unavailable value |
| --- | --- | --- |
| `schema` | Version of this counter schema | Constant integer `1` |
| `actions_retained` | `len(Engine.actions)`, including completed and unfinished objects | Records; `0` before engine creation |
| `lease_records` | `len(Engine.lease_state)`, including retired leases still retained | Records; `0` before engine creation |
| `removed_windows` | `len(StateCache.removed)`; full session observe emits this history | Handles |
| `frame_identities` | `len(Pixels.frames.issued)` | Identities; `0` without a pixel provider |
| `frame_files` | Regular `*.png` entries directly in the frame store, including files surviving restart | Files; `0` without a pixel provider; `null` on directory/stat error |
| `atspi_apps` | `len(TreeProvider.apps)` in the worker process | Parent-side app breaker records; `0` without a tree provider |
| `journal_bytes` | `fstat()` size of the current generation's open journal stream | Bytes; `null` on descriptor/stat error |
| `pending_x_requests` | Sum of `len(XState.ax.pending)` and `len(XState.inp_ax.pending)` | Outstanding parent-side AsyncX requests; `null` without X state |

`pending_x_requests` is a cheap estimate, excluding server-side work, X
events, provider jobs and peer waits. `atspi_apps` does not inspect the
AT-SPI child's object/bus/version maps or queue. `frame_identities` and
`frame_files` differ when identical image bytes have different issuance
metadata. Temporary files and PNG-named directories are excluded from the
file count. Journal bytes exclude every other generation and sessiond's
journal. An unreadable measurement is unknown, never a fabricated zero.

Collection reads map/list lengths, one journal `fstat`, and one frame
directory scan (linear in directory entries). It does not evict, prune,
flush, fsync, repair, advance revisions, query X, or contact a child. There
are no new caps, retention rules, queues or background sweeps. Existing
`system.info` settlement remains in place. Sampling has CPU/I/O cost and
can affect workload timing; it does not estimate unexposed inventories.

## Harness

`wm/tests/memory_step0.py` requires either `--dry-run` or `--session sN`.
The live path uses one persistent JSON-RPC connection and lease in an
existing **dedicated, empty session with a ready worker**. It refuses
displays outside 91–120, constructs a session-only environment, honors the
kill file, and launches only the existing `wm/fixtures/fx_log`. It creates
no worker/session and never clears fences, changes control, or resends an
uncertain action. On failure it emits a JSON failure summary and stops.
Fixture termination uses only this harness's own Popen handles. Socket
closure releases the lease through normal grace; the session stays live.

The phases run in one generation so history accumulates:

1. **`window_churn`:** Open N `fx_log` main windows in batches; wait for
   their exact titles to appear in settled worker state; sample while
   mapped; terminate those fixtures; wait until their windows disappear;
   sample after removal. Each batch contributes to the removed history.
2. **`act_churn`:** Keep one fixture alive and execute M separate verified
   `wait/window_present` acts with unique IDs and an explicit window
   target. These exercise the real retained action objects and durable
   journals without mutating fixture content. Sample every batch and at
   phase boundaries. Fresh delta observations supply each act's base
   revision; checkpoints use full session observations. Popup triggers
   below separately exercise input dispatch.
3. **`popup_flood`:** Open batches of `fx_log --popup-after 10` fixtures;
   target each with one Tab act expecting its named dialog; wait for all
   batch dialogs to be visible; sample the accumulated dialogs; terminate
   the fixtures and wait for all main/dialog windows to disappear. The
   popup delay is configurable. Triggers are sequential verified acts;
   dialogs accumulate within each batch. This is a popup churn workload,
   not a claimed arbitrary-rate AT-SPI event flood benchmark.
4. **`observe_size`:** Report full session observe size and the window
   churn growth series, pairing removed-history counters with emitted
   removed counts, result bytes, and actual response bytes. Full session
   observations request `channels:["state"]`, isolating the history
   failure mode from tree/image payloads; ordinary provider/coverage and
   pixel-change metadata are still supplied by the worker.

JSONL stdout contains `event:"sample"` checkpoints and one
`event:"summary"` per phase. Every live checkpoint includes monotonic
time, counter gauges, worker state, observe sizes, and process memory.
Summaries include before/after checkpoints and numeric counter deltas;
`observe_size.growth_samples` preserves the window churn series. Completed
counts are reported only after fixture lifecycle/act verification.

Process discovery reads current `/proc/<pid>/task/<pid>/children` recursively
from the worker and recorded mkdevs/helper supervisor PIDs on every sample,
so current AT-SPI/OCR children and supervised helper replacements can be
seen. Each process includes PID, role, command line, `pss_kib` from
`smaps_rollup:Pss`, and `vmhwm_kib` from `status:VmHWM`. **VmHWM is peak RSS,
not PSS.** Missing/denied fields remain `null` with errors; descendant-list
read failures are explicit. Root-owned helper PSS may be unreadable to the
live user. An xvfb session has no evdev helper. Fixtures are not worker
children and are excluded from the worker memory totals; the script emits
individual process rows rather than silently summing incomplete readings.

The harness reads actual length-prefixed responses past the adapter's
unchanged **262144-byte payload cap**. `response_bytes` includes the
JSON-RPC envelope and excludes the four-byte prefix; `result_bytes` is a
compact re-encoding of the result only. `exceeds_adapter_cap:true` records
the adapter failure threshold without causing the measurement connection
to reject that response. This does not prove adapter delivery succeeded.
The harness has a separate 64 MiB read guard; the worker's existing peer
outgoing-buffer limit also still applies. No production transport limit
is raised. A very large response can still disconnect the measurement
connection, which becomes a failed phase instead of a successful baseline.

## Raj's live commands

From the repository root, start a fresh disposable session and its worker
using the returned session ID (replace `sN` below). No other driver should
attach to it. Start with a short smoke run:

```sh
./wm/ark-sessiond start --profile xvfb
./wm/ark-worker start --session sN
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 wm/tests/memory_step0.py \
  --session sN --windows 20 --acts 20 --popups 10 --batch 5 \
  | tee wm/tests/memory-step0-smoke.jsonl
```

Then use a fresh session for the baseline. This size crosses the council's
approximately 11000-window history failure range; no threshold is asserted
in advance. Each act causes multiple fsyncs, so the 100k-act run may take a
long time on persistent storage. As the council recommends, arrange tmpfs
storage for the dedicated account/session journal before a large run, and
record that storage setup with the output. Worker state discovery uses the
account-database home; changing `HOME`/`TMPDIR` alone does not relocate it.
The harness does not remount, redirect or delete production state.

```sh
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 wm/tests/memory_step0.py \
  --session sN --windows 12000 --acts 100000 --popups 1000 --batch 20 \
  | tee wm/tests/memory-step0-baseline.jsonl
```

Repeat with a fresh xorg session to measure the evdev helper, if that
profile is available. Retain raw JSONL, machine/profile/storage details,
and any permission errors. These are count-based workloads, not a 24-hour
plateau or an event-rate acceptance result. Stop only the dedicated session
after retaining results:

```sh
./wm/ark-worker stop --session sN
./wm/ark-sessiond stop --session sN
```

| Live measurement | Status |
| --- | --- |
| Window/PSS churn baseline | **NOT RUN** |
| Completed-act/journal growth baseline | **NOT RUN** |
| Popup storm baseline | **NOT RUN** |
| Observe history / 256 KiB threshold on a live worker | **NOT RUN** |
| xorg/helper PSS | **NOT RUN** |

## Headless validation

Dry run completed successfully on this machine:

```sh
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 wm/tests/memory_step0.py \
  --dry-run --windows 3 --acts 4 --popups 2
```

It imports the stdlib-only adapter helpers, parses the actual worker and
GTK fixture source, emits four `NOT RUN` phase summaries, and probes only
its own `/proc` memory fields. It does not import GTK, open a socket,
launch a fixture, or discover a live session. That process-memory probe
is **not a worker baseline**.

`wm/tests/test_worker_w_memory_step0.py` adds 15 tests: actual completed
engine acts/leases and window removal growth; independent frame identity
and file growth without eviction; parent app and both AsyncX queue sizes;
current-generation journal bytes; unknown file measurements; additive
system.info shape; headless dry-run isolation; display rejection and
session environment isolation; PSS versus VmHWM/error handling; current
child/helper discovery; fragmented oversized RPC response measurement;
no uncertain-act replay; and all four phases with fake fixture lifetimes
and the real `Engine.normalise` request validator. These fake lifecycle
results do not validate GTK popup timing or live window-manager behavior.

One existing W2 observation stub gained its missing empty `pending` list
so the newly sampled gauge can inspect the same field as real AsyncX.
Its reconciliation assertions are unchanged.

Final full headless suite: **886 tests in 124.137 s: 885 passed, 1 skipped**,
exit 0, `OK (skipped=1)`. This is the original 871 plus 15 Step-0 tests.
The skip remains the existing ptrace-dependent OCR strace check. The
initial full run found the older W2 stub's missing `pending` field;
the final run includes its correction and all new tests.

Runner (same runtime-only temporary-directory redirection used by W8; no
existing harness policy changed):

```sh
mkdir -p wm/tests/.tmp
PYTHONDONTWRITEBYTECODE=1 TMPDIR="$PWD/wm/tests/.tmp" /usr/bin/python3 - <<'PY'
import sys, tempfile, unittest
from pathlib import Path
original = tempfile.TemporaryDirectory
root = Path.cwd()
def local(*args, **kw):
    if kw.get('dir') is not None and Path(kw['dir']).resolve() == root:
        kw['dir'] = str(root / 'wm/tests/.tmp')
    return original(*args, **kw)
tempfile.TemporaryDirectory = local
suite = unittest.defaultTestLoader.discover('wm/tests', pattern='test_worker_w*.py')
result = unittest.TextTestRunner(verbosity=1).run(suite)
sys.exit(not result.wasSuccessful())
PY
```

Changed files: `wm/ark-worker`, `wm/tests/memory_step0.py`,
`wm/tests/test_worker_w_memory_step0.py`, the one-field W2 stub update in
`wm/tests/test_worker_w2.py`, and this new note. The W8 bench note is
unchanged. Temporary test/dry-run output stays under `wm/tests/.tmp`.
