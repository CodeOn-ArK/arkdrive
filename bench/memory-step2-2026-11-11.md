# Memory cleanup Step 2 — window and ref hygiene

2026-11-11. Implements Step 2 of
`hidden_files/COUNCIL-MEMORY-RECONCILED.md`. The reported live Step-0
baseline was **200 created/closed windows → +200 parent `atspi_apps`**.
**Live gates: NOT RUN in this sandbox.** No display, including `:0`, was
opened or driven. Nothing staged or committed.

## Behavior and forget protocol

The parent records the app identity associated with each requested window.
On close/unmap it prunes that window's tree/ref cache, publication status,
layout, modal classification, raw snapshot, freshness counters, consumers,
retry/debounce entries and on-demand semantic inventories. It also removes
the handle from every retained lease's `trees` revision baselines,
`tree_pages` delivery bookkeeping and `ocr_trees` fallback bookkeeping.
Lease budgets, suspension, full-observation/reconciliation revision and
in-flight action records remain available. Pruning runs during ingestion
and action/provider ticks, including when AT-SPI is down or unavailable.

The internal child request uses the existing monotonically increasing job
ID and framed transport:

```json
{"id":17,"op":"forget_window","app":"1234","window":"sN/g01234567/w2"}
```

It returns the ordinary job envelope with
`result: {"forgotten": true}`, app identity, timing and additive internal
memory counters. Repeating a forget of an absent window succeeds with the
same result. This is an internal provider operation; drivers gain no new
operation or receipt fields.

All jobs and forgets execute on the existing GLib binding owner, one job
per turn. A forget is accepted into its FIFO even when an earlier walk,
enumeration, probe or mutation for the app is queued/running. It cannot
enter a running walk or delete state during a binding call. Flight records
are now keyed by job ID so finishing the earlier job cannot accidentally
clear the queued forget's supervision. The parent's ordinary one-flight
per-app admission includes pending forgets. App liveness observations do
not treat a forget acknowledgement as an application round trip.

Before **every job result**, including errors and forget acknowledgements,
the child forcibly flushes pending coalesced global invalidations into
the same FIFO, even if their debounce deadline has not arrived. Targeted
notifications are already queued synchronously by the listener. A removal
or invalidation observed during work therefore precedes its result. Normal
idle debounce behavior remains intact.

The child deletes the window's `objects` entry, per-window version and
object-key reverse memberships, including identities discovered only by
enumeration or by an incomplete/failed walk. New window↔bus and window↔app
memberships let it remove `bus_apps` and app versions only after their last
window is forgotten. Shared identities/buses retain sibling memberships.
A window whose parent app identity changes also retires its old membership.

The parent retains pending task objects for their existing act/observe
waiters, but discards their closed-window results from publication and
health bookkeeping. A late snapshot cannot recreate a tree or advance its
publication revision; late enumeration/error replies cannot recreate
inventories, dirty flags or unavailable status. Closed observe waiters
finish without recreating health records. Retired flights stay retired
across unmap/remap of the same handle; a new snapshot must follow the
forget acknowledgement. Once all jobs and forgets drain,
apps with no mapped windows lose their breaker, sample and app epoch.
An internal forget error is retried with a new job ID. The existing
heartbeat/flight watchdog still supervises it; child death clears the
pending retirement state because the replacement child has empty maps.

## Snapshot retention and compatibility

`TreeCache.update` no longer builds or retains `enumeration` when
`rendered_only` is true, including an unchanged render. Production AT-SPI
snapshots carry that flag. Semantic item predicates still request the
separate complete `enumerate` job on demand, including offscreen and
unrealized items, with the existing freshness checks.

Complete injected/replayed snapshots without `rendered_only` keep their
inventory. Tests depending on it include W4
`test_item_absence_requires_complete_semantic_inventory_including_offscreen`
and W4 fix9
`test_complete_snapshot_inventory_still_counts_sentinel_subtree`.
W4 fix8's fake `enumerate` response now derives its inventory from its
complete dialog fixture rather than a production rendered-only cache; its
own/sibling-event freshness assertions are unchanged.

Each predecessor now contains only ordered `refs`, `lines` and `rev`.
Delta calculation reads exactly these fields; it retains no predecessor
node payloads, semantic enumeration or linked predecessor history.
Current nodes and act reconciliation's required snapshots remain intact.
Existing cursor lifetime and frozen continuation behavior survive close:
continuations still return their original lines/revision, but cannot
reintroduce identities that window pruning removed. Cursor snapshots remain
under the existing expiry/count policy; they are not evicted early.

## Additive counters

`system.info.memory.schema` remains **1**. Existing gauges, including
parent `atspi_apps`, keep their meaning. The new `atspi_child` gauge is an
object containing the latest child counters received with a job result:

| Field | Count |
| --- | --- |
| `windows` | Window entries in the child object map |
| `objects` | Object entries summed across those windows |
| `bus_apps`, `versions`, `window_versions` | Bus→app, app epoch and window epoch records |
| `event_keys`, `event_memberships` | Object identities and their total reverse window memberships |
| `window_keys` | Windows with remembered object identities |
| `window_apps`, `app_windows` | Window→app records and app→window sets |
| `window_buses`, `bus_windows` | Window→bus sets and bus→window sets |

It is `null` before the first result and after child loss/restart, never a
fabricated zero. Counts describe the last completed job, not an atomic
parent/child sample; ordinary events or a pending forget can make them lag.
`system.info` reads only this cached object and performs no child round
trip or binding access. Step-0 JSONL samples preserve the nested object;
its flat `memory_delta` helper does not calculate nested child deltas.

## Achieved bound and remaining growth

Both requested lifecycle cases are covered by the design and headless
tests, subject to receiving the close/unmap and draining/acknowledging
earlier jobs and the forget:

- An app whose last window closes returns its parent app records and child
  maps to baseline. This does not wait for process exit; an alive process
  with no remaining mapped windows also releases its tracked state.
- If an app keeps a mapped sibling window, the closed window's objects,
  versions and memberships disappear while sibling objects, app epochs,
  shared bus attribution and health remain. App-wide counts correctly stay
  above an empty-session baseline for the live sibling.

These are metadata ownership claims, **not live measurements or a claim
that libatspi's own caches/RSS return to baseline**. State tombstones,
removed-window history, tree facets and existing ref absence ranges remain
Step 3 work; cursor lifetime, frame identities, lease/peer retirement and
queue/journal bounds remain under their existing policies/later steps.
Total worker memory is not bounded by Step 2 alone.

## Headless validation

`wm/tests/test_worker_w_memory_step2.py` adds 20 tests. They exercise real
parent and child implementation paths with fake accessible objects,
transport/GLib and X, including:

- Idempotent forget, partial failed walks, enumeration-only identities,
  shared sibling identities and app-identity reassignment.
- A blocked binding walk with a forget arriving while it runs; queued
  mutation→forget ordering; forced notification→result ordering before
  the debounce deadline; and child map readback after acknowledgement.
- All lease maps, late cursor delivery, AT-SPI absent/down operation,
  late snapshot/enumeration/error suppression, forget retry/restart and
  closed-target `target_not_found` behavior, plus unmap/remap before an
  old flight returns.
- A pending act on a live window continuing successfully through sibling
  close and forget, plus delta and frozen-cursor correctness after pruning.

Two churn tests each run 200 cycles. One exercises the child directly;
the other uses actual parent requests, Backend snapshots, parent close and
Backend forget acknowledgements with distinct app identities/buses. At
every cycle it first asserts a matched snapshot with populated object and
bus maps. At **every cycle**, parent `atspi_apps` is back to zero after
acknowledgement,
and all 12 child gauges equal the initial zero baseline. These are fake
lifecycle counts, not processes opening 200 real windows or PSS samples.

Final full headless suite: **922 tests in 124.336 s; 921 passed, 1 skipped**,
exit 0, `OK (skipped=1)`. This is Step 1's 902 total tests plus 20 Step 2
tests. The existing skip is the OCR strace B6 check because this sandbox
denies ptrace. Log: `wm/tests/.tmp/memory-step2-full-suite.log`.
The final targeted run also passed all 20 tests, including the matched,
populated-map churn assertions, in 0.067 s; log:
`wm/tests/.tmp/memory-step2-targeted.log`.

The full headless command redirects test scratch storage beneath `wm/`:

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

## Raj's live checks — NOT RUN here

Use a fresh dedicated session (display 91–120) with this worker build and
record its generation, profile and storage setup. The Step-0 harness's
window-churn phase directly measures the former app-record leak:

```sh
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 wm/tests/memory_step0.py \
  --session sN --windows 200 --acts 1 --popups 1 --batch 20 \
  | tee wm/tests/memory-step2-live.jsonl
```

1. Compare each settled churn checkpoint and the final phase against its
   `before` sample. After close and queued forget acknowledgements drain,
   `atspi_apps` should return to baseline instead of +200. Inspect
   `sample.memory.atspi_child` for the child object and reverse-map counts.
   A state-only observation can see closure before forget acknowledgement;
   if the final sample still shows a flight, poll `system.info` until it
   drains before judging the result. The harness's later phases create
   their own live fixture/app records, so compare within each phase.
2. Separately keep a fixture application process alive: open two windows,
   publish both trees and invoke an item predicate to populate on-demand
   inventory. Close one window. Verify its objects/memberships disappear,
   while the sibling's refs, freshness, deltas and physical/semantic acts
   still work. Close the last window while leaving the process alive;
   tracked app/window maps should return to baseline even before exit.
3. Close during an outstanding walk/observe/act. Verify no late tree
   publication, no provider restart or sibling breaker breach from forget,
   existing closed-target failure behavior, and ordinary reconciliation
   for any already-dispatched act. Continue a previously issued cursor
   after close and confirm its original page remains readable.
4. Repeat the existing rapidly changing sibling/dialog and offscreen item
   freshness gates. No stale snapshot or semantic inventory may become
   verification evidence. Compare worker/child PSS and latency with Step 0;
   map-count cleanup alone does not prove a PSS plateau.

Live GTK/libatspi behavior, PSS/latency, long soak and hardware gates remain
**NOT RUN**. Changes are confined to `wm/` and this new bench note.
