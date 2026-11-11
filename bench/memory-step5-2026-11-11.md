# Memory cleanup Step 5 — queues, peers, journals and scratch

2026-11-11. Implements the final step of
`hidden_files/COUNCIL-MEMORY-RECONCILED.md`.
**Live gates: NOT RUN in this sandbox.** No display, including `:0`, was
opened or driven. Nothing staged or committed. Changes are confined to
`wm/` and this note. Driver RPC and receipt schemas are unchanged; overload
uses existing `queue_full` and incomplete/degraded observation behavior.

## Child results queue

`worker_atspi.Results` replaces the unbounded `queue.Queue`. Within each
job-result barrier, repeated notifications coalesce by `(app, window)`.
Every original signal still increments the child's freshness version;
coalescing does not turn an overlapping snapshot into fresh evidence.
An explicit global signal, notification count overflow, byte overflow, or
conflicting removal identities collapses that segment to the existing
global invalidation. Subsequent notifications merge into that global row.

The notification budget is **256 entries / 256 KiB of encoded JSON**.
There is at most **one job result**, limited by the existing **4 MiB IPC
budget**. A result seals its preceding notifications; overflow after that
barrier cannot reorder or erase them. The complete queue has an upper
bound of **258 entries / 4 MiB + 256 KiB + 128 bytes of encoded JSON**:
256 preceding notifications, one result, and one overflow/global row after
the result. The 128-byte allowance covers that global row and its optional
removal flag. This is a serialized-payload bound, with bounded per-entry
Python container overhead, not an exact allocator/PSS measurement.

The owner runs no further job while a result remains queued. It checks
remaining transport space before draining a row, retaining the result if
the bounded output buffer must drain first. Thus a flood does not turn
notification backlog into a backpressure restart loop. Duplicate job
submissions from a faulty parent still fail closed rather than accumulating
unbounded error results. Coalesced global notifications flush before every
job result, including forget acknowledgements, as required by Step 2.

A removal cannot disappear behind coalescing. If the global row subsumes
removals, an internal child-to-parent `removed_all` flag invalidates cached
identity continuity across the affected trees. A rebuilt object reusing an
old bus/path receives a new ref. Ordinary global notifications retain their
existing behavior, including verification of computed text. This flag is
internal provider IPC; it is absent from driver RPCs and receipts.

## Peers and X work

Disconnect immediately removes the socket from the selector/live peer set
and clears its input/output buffers and deferred observations. The peer's
existing **5-second grace** still controls lease and observation-frame pin
release. Lease `done`, suspension, misses and budget accounting remain
reachable while any queued or active act uses that lease. Acts continue to
finish through the existing engine, including durable preempted receipts
when `done` was set. After grace and completion, the lease record disappears
and retained uncertain action records release their peer reference. This
also covers a peer that released its lease before disconnecting. Pending
effects and receipts remain available for reconciliation and dedupe. A
new connection creates a new peer and lease state; live leases are retained.

Both async X connections share a **2,048-request ceiling**, checked before
constructing/sending a request. Accounting includes requests temporarily
held in the pump's local iteration list, so callbacks cannot over-admit by
observing a temporarily empty `pending` list. Completion releases a slot
before its callback runs. Existing per-peer limits of eight deferred RPCs
and the engine's eight queued acts remain in force; all resulting X fanout
also shares the 2,048 total ceiling.

Sizing: a window metadata refresh issues 12 initial requests and at most
eight ancestry probes. The Step-0 batch size of 20 windows needs about 240
initial requests; 2,048 leaves substantial headroom for discovery, the
active action's dependency probes, pointer queries and capture, and for
larger live inventories such as 128 windows. A refresh reserves headroom
by refusing to start within 20 slots of the ceiling. The hard check on
every individual request remains authoritative. This is a conservative
engineering bound, **not a hardware-tuned Pi PSS or latency result**. At the
worst 64-KiB property reply size, 2,048 requests could represent roughly
128 MiB of property payload, plus object overhead and other reply types;
the count cap is not a tighter total-memory claim.

At saturation, new observations/waits use `queue_full`; newly admitted
acts use the existing nondispatched `queue_full` receipt. Dedupe retrieval
still precedes admission checks. Internal request rejection leaves coverage
incomplete rather than publishing a partial metadata group. After outstanding
X work drains, interrupted refresh bookkeeping is discarded and the global
watchdog refresh is forced. Rejected pixel captures leave dirty tiles eligible
for retry. Heavy or persistently oversized live inventories may remain degraded;
the ceiling never expands to make them appear healthy.

## Streaming journals and 90-day retention

Recovery scans JSONL rows without retaining resolved requests/receipts or
sets of all reconciled action IDs. An intent is retained only until its
receipt; the receipt reduces it to that action's own unresolved effects,
or removes it immediately. Reconciliation rows remove effects/actions as
they are encountered. A second streaming acknowledgement pass handles
cross-generation acknowledgements regardless of filename/mtime ordering.
This adds sequential parsing, not a checkpoint, segment, database or input
replay. Retention remains proportional to unfinished/unresolved work,
generation summaries and 50 recent receipt identities per session instance,
plus the current decoded row. Genuine unresolved work is deliberately not
capped or silently discarded. The existing current-generation offset index
is separate and unchanged.

After successful resource cleanup, an idle, unfenced worker with no queued,
active or uncertain-durability actions appends and fsyncs the terminal
`generation_closed` journal marker. A crash, cleanup error, busy shutdown,
fence or missing durable final prevents that marker. A clean close may
still contain unresolved final effects: the marker alone never authorizes
deletion. Any later row revokes terminal closure.

On worker startup, archive maintenance may delete a generation only when:

- It has a terminal clean-close marker and no unfinished intents or
  unreconciled effects, including cross-generation acknowledgements.
- It is not the current generation and supplies none of the last **50
  durable receipt frame pins** for its session instance.
- At least **90 days** have elapsed since both its latest row timestamp
  and its file modification time. There is no Pi-specific exception.

Archives carrying necessary acknowledgement proof for a retained generation
are retained too. Eligible source archives are unlinked before archives
carrying their acknowledgements, so a crash between deletions cannot resurrect
an old unresolved effect. A directory fsync completes reclamation. Corrupt
or torn history prevents the sweep from deleting anything; current-instance
recovery retains its existing F3 fencing behavior. Legacy archives without
a clean marker remain retained. Maintenance runs at startup, outside action
verification; no new synchronous disk read is added to dispatch checks.

Frame pin restoration also streams globally into a 50-receipt reference
window, rather than retaining 50 decoded receipts for every generation.
Last-50 selection, frame paths, existing insertion order, quota and lease
pins are preserved. Generation deletion cannot remove the history needed
to restore those pins after restart. No evidence PNG is deleted on stop.

## Diagnostics and session scratch

`worker_housekeeping.py` truncates diagnostic logs **in place**, preserving
the inode held by append-mode child descriptors. The cap is **4 MiB per
named log / 64 MiB aggregate**. Named files are `worker.log`,
`atspi-provider.log`, `ocr-provider.log`, `openbox.log`, `mkdevs.log`,
`xorg-launch.log`, `xvfb.log`, `atspi-launcher.log` and `registryd.log`.
With nine named logs, the per-file caps currently imply at most **36 MiB**
after a sweep. Caps are checked every second by a small stdlib guard even
when no worker is running, by the worker's existing loop, at child spawn,
and on sessiond registry operations. These are **sweep bounds**: writers
can exceed them between sweeps; this is not a synchronous per-write quota.
The guard is an additional session process whose PSS must be measured live.

No log rotation copies or diagnostic archive allocation is introduced.
Truncation errors are best-effort and never escape over the act engine's
F3 handling. Act journals and sessiond's audit `journal.jsonl` are never
truncated. Input still requires durable intent/arm writes; disk-full write
failure closes F3 before dispatch, and diagnostic maintenance cannot clear
that fence. Crash/disk-full ordering has headless regression coverage;
filesystem/device behavior remains a live gate.

New sessions use immutable random 128-bit instance identities and scratch
paths `run/<sid>/<instance>/`, with a matching `.instance` marker. The
identity is carried in process environments, privileged process scans and
internal journal envelopes. Recovery and receipt-pin restoration distinguish
incarnations of a recycled sid. Archive maintenance can still inspect old
instances for eligible 90-day reclamation. Legacy sessions retain their
existing layout and are not scratch-collected without an instance marker.

Sessiond preserves its existing helper-gate/quit/death-before-Xorg ordering.
It now verifies every recorded PID is dead and the final instance-marked
process scan is empty before reclaiming scratch or removing the registry
entry. Signal success and registry absence are insufficient. A surviving
process, failed scan, mismatched instance, or cleanup error retains the
record and scratch for recovery. Successful cleanup removes framebuffer,
runtime, logs, sockets, helper outputs and other regenerable scratch.
User `home/`, `driver/frames/` and the instance marker remain at their
original paths; retained user work and receipt evidence are not scratch.
A reused sid never adopts those old directories.

## Headless validation

`wm/tests/test_worker_w_memory_step5.py` adds **28 tests** covering:

- Simulated 1k/10k-event bursts: repeats coalesce, distinct-window floods
  end in global invalidation with bounded count/bytes, every signal advances
  freshness, removal conflicts remain conservative, and result barriers hold.
- Grace-boundary peer retirement, queued durable completion, preserved
  done/suspension/budget state, weak-reference collection, and uncertain
  receipts no longer retaining a disconnected transport.
- Shared X admission including callbacks during pump iteration, busy shapes
  without extra waits/intents/input, bounded rejected refresh bookkeeping,
  and incomplete coverage followed by forced resynchronization after drain.
- **50,000 resolved actions** in a synthetic generation larger than 10 MiB:
  production recovery's per-row retained-action high-water proxy is exactly
  **one**. This proves historical resolved payloads do not accumulate; it is
  not a PSS or arbitrary-unresolved-work bound.
- Old clean/reconciled archive deletion; retention of recent, unclean,
  unfinished, unreconciled, frame-pinned and corrupt archives; unordered
  cross-generation acknowledgements and preservation of acknowledgement proof.
- Clean-close marker ordering after resource cleanup, failure/busy/fenced
  shutdowns, append-descriptor survival after log truncation, disk-full log
  errors, verified teardown, preserved home/evidence and recycled sid isolation.

The prior extension-query fake accepts the shared X budget; the event-pump
assertion accepts global coalescing; teardown fakes now model non-helper
process death after SIGKILL so that the real final-death check is exercised.
The existing Step-4 insertion-order frame regressions are unchanged.

Targeted Step-5 run: **28 passed in 0.784 s**. Full headless suite:
**973 tests in 127.879 s; 972 passed, 1 skipped**, exit 0,
`OK (skipped=1)`. This is Step 4's 945 total plus 28 new tests. The
skip remains the existing ptrace-dependent OCR strace check.
Logs: `wm/tests/.tmp/memory-step5-targeted.log` and
`wm/tests/.tmp/memory-step5-final-full-suite.log`. The full runner uses the
same runtime-only temporary-directory redirection into `wm/tests/.tmp` as
Steps 0–4 and discovers only `test_worker_w*.py`. No live gate is launched.

## Raj's live verification — NOT RUN here

Use a dedicated session on display **91–120**, one driver, and record
machine/storage/profile/instance/generation and the original Step-0 samples.

1. Run known-window and unattributed AT-SPI floods at 1k and 10k/s while
   requesting trees and performing verification. Confirm child PSS plateaus,
   the provider does not restart repeatedly, and overflow invalidates all
   relevant windows. Include children removals/path reuse and a queued forget;
   an old ref must not gain authority over a replacement object. Repeat the
   computed-text and sibling/dialog freshness gates.
2. Disconnect with queued acts and with `session.done`/budget suspension.
   Check acts receive durable final receipts, lease pins remain through five
   seconds, then old lease records/peer-owned buffers disappear after work
   finishes. Reconnect with a fresh peer and verify unresolved effects still
   require ordinary reconciliation; inspect old receipts through `action.get`.
3. Stall/delay the dedicated X server's replies and generate metadata/observe
   pressure. `memory.pending_x_requests` must never exceed 2,048 across both
   connections. At saturation expect existing busy/degraded results, no
   unbounded enqueue and no partial verification evidence. After pressure
   subsides, verify watchdog refresh, pixels, coverage and normal input recover.
   Measure latency and PSS with typical and large live window inventories,
   especially on Pi; record any persistently degraded inventory size.
4. Restart after a large resolved journal and compare recovery PSS/time with
   Step 0. Inject crashes before/after arms, receipts, acknowledgements and
   closure; unclean/unresolved history must survive and input must never replay.
   On a disposable archive copy, verify 90-day eligibility, recent-history
   protection, dependency deletion order and last-50 frame pins after restart.
   Include unclean source generations reconciled by a later clean generation.
5. Hold append-mode child log descriptors open across multiple 4-MiB sweeps;
   verify inode stability and bounded post-sweep size. Check the log guard
   without a worker. In a disposable storage fixture, fill the journal's
   filesystem and verify F3 closes before any input; freeing diagnostic space
   must not reopen it. Include guard PSS and between-sweep disk excursions.
6. Verify helper death before Xorg signals, final marked-process absence and
   scratch cleanup on normal/failed starts and stops. Force an unkillable or
   uninspectable survivor: registry and scratch must remain. Recycle a sid;
   confirm a different instance path/environment, no inherited control/socket/
   framebuffer/profile scratch, and readable old user files and frame evidence.
7. Repeat the Step-0 window/act/popup workload and a 24-hour soak, sampling
   worker, child, helper and log-guard PSS, file counts and latency. Compare
   post-warm-up slope against the plan's <1 MiB/hour target and set the actual
   hardware ceiling. Headless count bounds alone do not establish that gate.

Live event rates, desktop/provider behavior, crash injection, disk-full,
PSS/latency, 24-hour soak and hardware gates remain **NOT RUN**.

## Plan-level summary against Step 0

| Step | Step-0 baseline/inventory | Implemented change |
| --- | --- | --- |
| 0 | Growth lacked consistent counters and workload evidence | Added schema-1 memory gauges and a guarded soak/PSS/file-count harness. Live measurements were supplied separately; the sandbox notes do not claim a live baseline. |
| 1 | One full action retained per finalized act; reported journal growth about 4.3 KB/act | Eligible durable finals become digest/hash/offset stubs, with a 4,096 hot-cache cap and indexed exact dedupe; uncertain outcomes remain retained. Final reconciliation shapes agree across RAM/disk paths. |
| 2 | Reported 200 window cycles added 200 parent app records; child/window/ref maps and redundant tree payloads persisted | Sequenced child forget, reverse-map/app/window cleanup, per-window lease pruning, removal of unused rendered-only enumeration and slim predecessors. |
| 3 | Reported removed history grew about 19.6 bytes per close and eventually broke full-observe framing | Newest 4,096 removals plus a revision floor/full fallback; active-act history guards, tree-facet cleanup and an actual 256-KiB response guard. |
| 4 | PNG bytes already had a 200-MiB quota, but identity/file counts and interrupted temporaries grew | 4,096 identities and 4,096 PNG files, independent insertion order, orphan cleanup, two delivered observation pins per lease, preserved last-50 receipt evidence. |
| 5 | Notification queue, dead-peer references, aggregate X requests, recovery payloads, diagnostics and stopped-session scratch could accumulate | Bounded/coalesced results and X work, grace-safe peer retirement, streaming recovery, closed/reconciled 90-day archive GC, append-inode log caps and verified instance-scoped scratch reclamation. |

Steps 0–5 implementation is complete. This does **not** promise constant RAM
or disk for unlimited work: Step 1's exact current-generation offset index
still grows with distinct action IDs, uncertain/unreconciled outcomes remain
retained, live windows consume live state, unclean archives are conserved,
and homes/evidence follow their own retention rules. Journal bytes per act
are not reduced by segmentation or checkpoints. The cleanup removes the
identified avoidable retention and adds explicit admission/fallback bounds;
Raj's live soak and hardware gates must establish the resulting PSS plateau
and latency against the Step-0 baseline.

## Fix round 1

Raj reported `queue_full` after roughly two batches of ten `fx_log` windows
on a fresh step-5 Xvfb session, followed by healthy idle probes. This round
uses **fake X only**: **live gates NOT RUN**, including the actual session
soak harness. No display, including `:0`, was opened or driven. Nothing
staged or committed; changes stay within `wm/` and this note.

### Diagnosis before changing admission

`wm/tests/memory_step5_fix1_trace.py` exercises production `XState.events`,
metadata/ancestry reads, refresh completion, snapshots and `AsyncX.ask/pump`
with deferred fake replies. Its counters independently track issuance,
server completion, worker collection, rejected asks and peak truly
outstanding requests. Replies are supplied at 128 per connection poll.
The decoration count is a synthetic stress shape, **not a measurement of
Raj's Openbox session**.

The original code exhibits two separate problems:

- **Real fanout (suspect b):** ten client windows with twenty child windows
  each, each producing map/configure/reparent notifications, create 630
  structural events. Before any reply can complete, the original code
  admits 2,048 requests and rejects another 214 individual asks, as well as
  refusing metadata groups. This is true outstanding work, not completed
  reply inflation. It issues a root `QueryTree` for every structural event
  and starts metadata groups for descendant windows too. The original
  sizing argument counted client metadata, omitting these paths. The
  admitted requests subsequently all complete and collect: 2,177 issued /
  2,177 completed after callback ancestry requests, zero remaining, but the
  overload latch remains set until normal ingest recovery.
- **Collection lag (suspect a, with a correction):** the pump does **not**
  impose a fixed number of reply collections per sweep; it scans every
  tracked request after nonblocking transport polling. However, it releases
  slots one at a time immediately before each callback. A deterministic
  completion batch of 2,000 replies, whose first 1,000 callbacks each ask
  for two requests, first rejects with **2,048 counted, only 97 truly
  outstanding, and 1,951 completed but uncollected**. It rejects 952 asks
  altogether. This workload needs at most 2,000 outstanding slots when
  completed replies are collected before callbacks run.

There is **no noncompletion leak (suspect c) in these reproductions**: all
admitted requests drain. These traces establish both failure mechanisms in
production paths; they do not identify the exact proportions in Raj's live
failure or rule out other live-only defects.

Pre-edit traces are in
`wm/tests/.tmp/memory-step5-fix1-before-trace.jsonl`. The before/after
collection experiment is in
`wm/tests/.tmp/memory-step5-fix1-accounting-trace.jsonl`. Two regression
assertions were also replayed with the original step-5 pump and refresh
methods restored only in the test process: both fail (completed-batch
fanout and decorated-ten-window admission). That replay retains the new
root-query coalescing, demonstrating that coalescing alone is insufficient.
See `wm/tests/.tmp/memory-step5-fix1-baseline-regressions.log`.

### Fix and bounds

The aggregate hard ceiling stays **2,048** and still rejects **before
constructing/sending** an individual request. Its number was neither raised
nor bypassed.

1. Collect the entire ready reply/error batch and release its slots before
   invoking any callback. Incomplete requests remain counted across both
   connections. Callback order and existing optional/fatal X error handling
   remain intact. The completion experiment now admits all 4,000 requests
   over its two generations, with zero rejection and 2,000 new outstanding.
2. Pace background metadata refreshes with an insertion-ordered queue of
   **window IDs only**. At most `floor(2048 / (2 * 20)) = 51` groups are
   active; each reserves its worst-case twenty-request footprint, at most
   **1,020**, leaving at least **1,028** slots for foreground sampling,
   capture, pointer and other requests. This is scheduling beneath the
   existing ceiling, not a replacement or larger admission ceiling. The
   per-request shared check still decides admission under mixed pressure.
   Queue plus active IDs cannot exceed **2,048**; a larger distinct-window
   flood sets the existing overload latch and stops growing bookkeeping.
   Queued notifications coalesce, and their snapshots sample the latest
   state when started. Active refreshes still retain one `again` sample.
   Completion advances the queue fairly; queued IDs stay in `refreshing`,
   so coverage cannot claim completion before their snapshots finish.
3. Read root tree, geometry, focus and client/active properties once per
   event batch when invalidated. All events still reach the input consumers
   and revision/invalidation accounting. A 1,800-structural-event test now
   emits one root query plus ten ancestry queries in its initial batch.
4. Overload recovery clears queued IDs along with interrupted refreshes
   after issued requests drain, then forces the existing watchdog resync.
   A bounded queue cannot keep a genuine overflow permanently wedged.

For the identical 630-event decorated batch, the fixed trace has **613 peak
truly outstanding**, **3,381 issued / 3,381 completed**, **zero rejection**,
zero pending and no overload latch after collection. More requests can
complete successfully overall because refresh groups are no longer shed.
See `wm/tests/.tmp/memory-step5-fix1-after-trace.jsonl`.

`system.info.memory.pending_x_requests` now reports the **shared budget
count**: admitted AsyncX requests awaiting completion collection across both
connections, including any detached pump iteration, excluding the collected
callback batch. Queued window IDs have not issued requests and are excluded.
A reply that arrived since the last collection can remain counted until its
next pump; this is a parent bookkeeping gauge, not an exact server-flight
measurement. Unlike summing `pending` list lengths, the shared count stays
truthful during callbacks. The schema and unit (request count) are unchanged;
this refines the Step-0 list-length implementation documented above.
The callback batch is itself bounded by the 2,048 requests admitted before
collection and is released when that pump returns. Thus at most 2,048
collected callback rows can coexist with the 2,048 newly admitted requests;
the request ceiling is not a claim that only 2,048 Python reply objects or
128 MiB of payload can ever be retained during callback dispatch.

### Headless regression coverage

`wm/tests/test_worker_w_memory_step5_fix1.py` adds nine tests covering the
two failures, root query coalescing, queued snapshot freshness, shared
stall admission, bounded internal backlog and resync, callback-time gauge
accuracy, foreground action sampling during churn, and the Step-0-sized
**200 windows / 300 acts / 40 popups** sequence. The sequence uses batches
of ten, twenty synthetic decorations per client during window churn,
observe RPCs submitted while metadata work is pending, real action engine
waits with production dependency sampling, popup map/remove cycles, and
late optional BadWindow replies after destruction. All observations settle,
all acts succeed, no RPC returns `queue_full`, and all issued requests drain.
Popup input/provider behavior is not simulated by this fake-X check.

A stalled two-connection flood still admits exactly **2,048**, rejects the
next ask without construction, stays bounded while stalled, then collects
all replies when released. A 10,000-distinct-window internal refresh flood
also sheds with bounded queued IDs and recovers through production ingest.
**All original twenty-eight Step-5 tests remain unchanged and pass**,
including shared detached-work accounting, rejected-refresh bookkeeping,
RPC/act `queue_full` shapes and post-drain resynchronization.

Targeted Step-5 plus fix-round run: **37 passed in 2.049 s**. Log:
`wm/tests/.tmp/memory-step5-fix1-targeted.log`.

Full headless suite: **982 tests in 128.172 s; 981 passed, 1 skipped**,
exit 0, `OK (skipped=1)`. This is the prior 973 total plus nine new tests;
the existing ptrace-dependent OCR strace check remains skipped. Log:
`wm/tests/.tmp/memory-step5-fix1-full-suite.log`. The runner discovers only
`test_worker_w*.py`, sets `PYTHONDONTWRITEBYTECODE=1`, and redirects temporary
directories (including the W2 harness's repo-root request) into
`wm/tests/.tmp`, as in Steps 0–5. Reproduce with
`PYTHONDONTWRITEBYTECODE=1 python3 wm/tests/.tmp/memory-step5-fix1-run.py full`;
run it without `full` for the 37-test targeted set. The trace helper runs
headlessly with
`PYTHONDONTWRITEBYTECODE=1 python3 wm/tests/memory_step5_fix1_trace.py`.

The actual `fx_log`/Openbox/AT-SPI soak, live latency/PSS, popup input,
24-hour and hardware gates remain **NOT RUN**. Raj should repeat the
original Step-0 harness unchanged on a dedicated display 91–120 before
calling the live regression closed.
