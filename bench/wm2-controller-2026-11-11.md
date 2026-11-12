# WM v1 / Stage WM-B — durable genealogy and thin controller

Built 2026-11-11 to `docs/WM-V1-ARCHITECTURE.md` and
`hidden_files/COUNCIL-WMV1-RECONCILED.md`, under the stage authorization.
Changes are confined to `wm/` and this note. No staging or commit. No display
was opened and no live gate was run, including on :0.

## Design as built

`ark-sessiond` remains the stdlib one-shot lifecycle CLI. `wm_registry.py`
adds durable metadata to its existing `sessions.json`, under the same `.lock`
used by workers and sessiond. Existing records upgrade once under that lock.
Every record has:

```json
{
  "session":"s2", "instance":"<process-marker-instance>",
  "incarnation":"<persistent-namespace>-42",
  "parent_incarnation":"<parent-incarnation>",
  "root_incarnation":"<root-incarnation>", "depth":1,
  "children":[], "lifecycle":"ready", "control_state":"AGENT",
  "budget":{
    "allocation":{"acts":100,"tokens":10000},
    "debited":{"acts":0,"tokens":0},
    "spent":{"acts":0,"tokens":0}
  },
  "task":{
    "id":"<task-id>", "text":"Export the report", "origin":"parent-agent",
    "source_incarnation":"<parent-incarnation>"
  },
  "result":null, "mailbox":[], "requests":{}, "attention":[]
}
```

`incarnation-sequence.json` contains a random persistent namespace and an
increasing counter. The counter is fsynced/replaced **before issuance**;
crashes can skip numbers, never reuse them. The reusable `sN` and existing
process-marker `instance` retain their old roles. Messages use incarnation
identity; worker journal ingestion additionally matches session, process
instance and generation. Counter issuance also occurs under `.lock`.

Successful teardown first writes `retired/<incarnation>.json`, then removes
the active record. Tombstones preserve task/result/ledger/genealogy and
spawn identity after `sN` reuse. A parent's `children` includes retired child
identities; fan-out counts **active records**, not this historical list.
Root depth is zero; the deepest permitted child has depth three.

`ark-sessiond stop --session sN` now stops that entire genealogy.
`stop --incarnation I` adds an idempotent identity-safe form; repeating an old
incarnation can never stop a later occupant of the same `sN`. `stop --all`
uses the same cascade. Sessiond persists `stopping` first, closes gates
top-down (parent first), then tears down bottom-up (children first). Gate
failure prevents reaping. A surviving/unsafe descendant retains its ancestors
and their closed gates for retry. `stop.gate` and `stop.reaped` journal events
include incarnation ids. This needs **only registry data**, not a running
controller, its journal or a worker lease. Stopped descendants are removed
from surviving ancestors' attention flags.

Session startup now commits its reservation/identity before launching any
process, marks `creating`, checkpoints acquired process identities and then
marks `ready`. Partial Xorg starts recover helper/X pidfiles during sessiond
teardown itself, preserving the existing helper-before-X safety order.
Sessiond readiness means the desktop/buses are ready. As before, a driver
supervisor starts `ark-worker` and attaches its one driver separately; the
controller does not start reasoning loops or acquire leases.

`ark-wmctl serve` is the new stdlib foreground daemon. Use a service manager
or a dedicated terminal to keep it running. It owns `.wmctl.lock` before
reading recovery state or removing a stale socket; a second daemon fails
with `controller_already_running`. State defaults to sessiond's existing
`~/.local/state/ark-sessiond`; `--state PATH` supports isolated fixtures and
must identify the same registry used by sessiond/workers.

The controller polls worker journals every 250 ms between API requests. It
also consumes WM-A on demand through `frames.get`, carrying the previous
generation/revision to the read-only `driver/frames-feed.sock`. It never
uses `session.attach`, `window.observe`, `window.act`, or a driver frame
store. Frame polling adds no lease or input authority. Worker generation
changes invalidate the cached feed revision. Feed paths remain ephemeral
WM-A artifacts, separate from durable document handoffs.

### Spawn transaction and recovery

Every mutating request has a caller-chosen `request_id`, unique across this
controller registry's retained history. Reuse it after
disconnect/crash; changing its method/arguments returns `request_conflict`.
Capabilities are excluded from the request fingerprint and journal.

1. Allocate a child incarnation/task id and fsync an `intent` to
   `wmctl-journal.jsonl` **before creating a child or charging anything**.
   The intent carries the prompt origin, source, text and exact reservation.
2. Hold sessiond's registry lock and the parent's existing control-page
   shared flock. Check its live page, durable fence events, stopping state,
   depth/fan-out/admission and available budget. A lease-free `system.info`
   health check on the same worker generation also refuses storage fences
   that could not be journaled, unsuccessful fence-clear attempts and pending
   reconciliation. An unavailable worker fails closed. HUMAN, paused, quiescing,
   fenced, missing/malformed control or unavailable journal refuses admission.
   Existing control writers take the exclusive page lock, so they serialize
   with spawn. No lease/control epoch operation is introduced.
3. Atomically replace the registry with **one** new reserved child, its
   initial provenance-tagged task mailbox event, parent/child links and the
   parent's debit. The child gets the reserved allocation, zero own debits
   and zero own charges. It never copies a parent's counters or full budget.
4. Launch the reserved desktop through sessiond, then fsync `complete`.

Startup replays unfinished intents. A reserved child is launched under the
same identity, with no second debit. A partially `creating` child is first
reaped using the existing instance/process markers, then launched again
under the same registry identity; old processes must be proved dead before
retry. A `ready` child completes the original request without launching
again, even if its parent has since entered HUMAN. A stopped child/tombstone
or stopped/reused parent is never resurrected. A pending reservation whose
parent has entered HUMAN is cancelled/reaped and reports `parent_human`.
Accepted reservations are conservatively burned on cancellation/startup
failure; no automatic refunds mint budget after an uncertain outcome.

Registry replacement and new directories are fsynced. An incomplete final
controller JSONL line is truncated before recovery; a malformed complete
line fails closed. An uncertain append/fsync refuses further mutations
until restart validates and syncs the journal. Mailbox mutation, queue ack
and budget charge results are committed with their per-record request ids,
so replay cannot duplicate them.

The ledger tracks delegated reservations in `debited` and prepaid local
charges in `spent`. `spent + debited <= allocation` for acts and tokens.
Drivers/model supervisors call `budget.debit` before consuming their local
allocation and retain the same request id across retries. The controller
does not count model-provider tokens or intercept worker input events.
Existing worker lease-local action budgets and fencing stay intact.

### Results and attention

The existing leased worker `session.done` accepts optional `artifacts`, an
explicit list of relative paths beneath that child's HOME (absolute paths
lexically beneath that HOME are also accepted). The adapter's `done` tool
exposes the list. Done still durably journals before releasing the same
driver lease, with the same status semantics. Its journal row additionally
tags incarnation/task identity when available. No controller lease exists.

The controller consumes durable `done` rows from current **and historical**
worker generation journals, including completion while it was down. It
copies the selected regular files to:

```text
STATE/exchange/CHILD_INCARNATION/TASK_ID/INDEX-BASENAME
```

Traversal uses directory-relative `openat` calls with `O_NOFOLLOW` at every
component and rejects HOME escapes, symlink ancestors/leaves and special
files. Copies are fsynced before registry publication; their metadata has
size, SHA-256, child incarnation and task id. The exchange directories are
0700 and files 0600. Source files can subsequently change without changing
the delivered copy. No private-HOME source path is handed to the parent.
A failed export produces `status:failed`, `artifacts:[]` and a named
`artifact_error`; partial handoffs are removed. Copying is bounded but does
not freeze a file an application is actively writing: drivers must explicitly
save/export a stable file before done.

One result slot per child task and the parent event are saved in one registry
replace. A crash before publication retries copying; a crash after publication
cannot duplicate delivery, including after the parent acknowledges it. Result
summary text is always data: no shell execution or prompt concatenation occurs.
The event explicitly marks child provenance and `untrusted:true`.

A worker can send leased metadata `session.human_need` with
`need:passcode|human|blocked|null`. This journals only the category, never a
passcode. `session.done` with `blocked` also raises attention. A controller
client can report the same category through `attention.report`. Attention
propagates to every ancestor record whether or not any view is expanded;
`need:null` clears it. Consumed worker attention offsets are persisted with
the flag, so historical rows cannot undo a later explicit clear after a
controller restart. No presenter, takeover implementation or passcode
entry is part of WM-B.

`stop-subtree` delegates to sessiond's cascade. The daemon checks `CU_KILL_FILE`
(default `/tmp/cu-executor.kill`) on API entry and every maintenance tick and
fans global kill out through the same sessiond stop-all cascade. A kill file
also refuses new spawns with `global_kill`. Session creation/teardown and
bounded file copies are synchronous, so maintenance runs between those
operations; this daemon is not a real-time kill transport. If the daemon is
dead, `ark-sessiond stop` remains the independent reaper.

## Local API and wire shapes

Owner-only Unix stream socket: `STATE/wmctl.sock`, bound under umask 0177,
chmod 0600, same-UID `SO_PEERCRED` check. At most eight clients, ten-second
connection deadline, one request/reply per connection. Framing is a four-byte
unsigned big-endian byte length followed by UTF-8 JSON-RPC 2.0. Requests are
at most 64 KiB; replies at most 4 MiB (`reply_too_large` otherwise).

The CLI builds/decodes this framing:

```sh
wm/ark-wmctl serve
wm/ark-wmctl call registry '{}'
wm/ark-wmctl call capability.issue '{"incarnation":"P"}'
```

Capability issuance returns a deterministic HMAC token scoped to that
incarnation and the delegate control operations. Issuance is journaled without
the token. A persisted owner-only key makes tokens stable across controller
restarts and different across incarnations. These are controller capabilities,
**not worker input/takeover capabilities**; WM-C's presenter-to-worker human
transport remains unimplemented. The local UID is the administrative trust
boundary: an owner can issue capabilities or stop sessions. This does not add
OS isolation between processes sharing that UID.

Spawn:

```json
{"jsonrpc":"2.0","id":1,"method":"spawn","params":{
  "request_id":"delegate-001","parent_incarnation":"P",
  "capability":"<P-delegate-capability>","task_text":"Export the report",
  "profile":"xvfb","origin":"parent-agent",
  "reservation":{"acts":100,"tokens":10000}
}}
```

```json
{"jsonrpc":"2.0","id":1,"result":{
  "ok":true,"child_incarnation":"C","session":"s2","task_id":"T",
  "reservation":{"acts":100,"tokens":10000}
}}
```

`origin` defaults explicitly to `parent-agent` in the intent; `human` is also
accepted. Named admission refusals are durable results, for example
`{"ok":false,"reason":"admission_limit"}`. Structural reasons are
`depth_limit` and `fanout_limit`; budget refusal is `budget_exhausted`.
Control reasons include `parent_human`, `parent_paused`, `parent_quiescing`,
`parent_fenced` and `parent_control_unavailable`. Identity/lifecycle recovery
can return `unknown_incarnation`, `child_stopped` or `creation_failed`.
Malformed/authentication requests return JSON-RPC errors with
`error.data.reason`, including `invalid_capability`, `invalid_request_id`,
`invalid_params`, `invalid_budget` and `request_conflict`.

Child driver/supervisor gets its initial task using
`capability.issue(C)` then `queue({incarnation:C,capability:...})`.
Only that session's normal worker/driver pair handles its native desktop.
Result delivery to `queue(P)`:

```json
{"type":"result","event_id":"done:<generation>:<journal-offset>",
 "child_incarnation":"C","status":"success","summary":"Report exported",
 "artifacts":[{"name":"report.txt","path":"STATE/exchange/C/T/0-report.txt",
   "bytes":123,"sha256":"<sha256>","child_incarnation":"C","task_id":"T"}],
 "provenance":{"origin":"child-agent","child_incarnation":"C",
   "task_id":"T","untrusted":true}}
```

Other methods (`I` is the addressed incarnation; protected methods carry its
`capability`):

| Method | Params besides capability | Result/purpose |
| --- | --- | --- |
| `registry` | none | active records and configured caps |
| `capability.issue` | `incarnation:I` | same-incarnation delegate token + scopes |
| `queue` | `incarnation:I` | non-destructive `events[]` |
| `queue.ack` | `request_id, incarnation:I, event_ids:[]` | durable idempotent ack |
| `prompt` | `request_id, incarnation:I, text, origin, source_incarnation` | tagged prompt event; parent-agent source must match target's parent |
| `budget.debit` | `request_id, incarnation:I, amounts:{acts,tokens}` | prepaid charge and ledger snapshot |
| `attention.report` | `request_id, incarnation:I, need` | propagate/clear human-need category |
| `stop-subtree` | `request_id, incarnation:I` (owner operation) | sessiond cascade |
| `frames.get` | `incarnation:I` (owner operation) | WM-A result + incarnation, or `feed_unavailable` |

Prompts retain `origin:human|parent-agent` and source incarnation in the
intent and delivered mailbox event. The first root prompt initializes its
task slot. Finished tasks cannot silently start another task under the same
result slot; spawn a new task/session. Queues, tombstones and durable journals
retain history; disk retention/compaction is not introduced in this stage.

## Caps as configured

`STATE/wm-config.json` is generated once with these values. Edit the existing
file and restart the controller to change admission/budgets/artifact quotas.
Unknown/malformed config is refused. Structural caps are fixed council rules.

```json
{
  "admission_limit":2,
  "root_budget":{"acts":1000,"tokens":100000},
  "child_reservation":{"acts":100,"tokens":10000},
  "artifact_bytes":16777216,
  "artifact_count":16
}
```

- Depth <= 3, fan-out <= 4 active children per parent.
- Admission starts at **2 total active/reserved/creating sessions**, including
  collapsed sessions and sessions whose task has finished but whose desktop
  still runs. Stopping a child releases its admission slot, not its budget debit.
- 16 MiB aggregate artifact bytes and 16 files per result by default. Worker
  `session.done` also has a protocol maximum of 16 artifact paths.
- Root allocation comes from config; child allocation is exactly its own
  reservation. A request can choose a smaller/larger reservation within its
  parent's available ledger. No automatic refund is performed.
- The existing :91–:120 allocator is unchanged and is not an admission target.
  Raise admission to 3/4 only after the council's host/VM measurements.

## Headless evidence

Reproduce without opening any display:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 wm/tests/run_wm_b_headless.py
PYTHONDONTWRITEBYTECODE=1 python3 wm/tests/run_wm_b_headless.py full
```

The runner confines scratch to `wm/tests/.tmp`. Targeted log:
`wm/tests/.tmp/wm-b-targeted.log`; full log:
`wm/tests/.tmp/wm-b-full-suite.log`.

Targeted: **44 tests, all passed, 3.128 s**. Full suite: **1,126 tests in
133.027 s, 1,125 passed, one existing environment-dependent skip**, exit 0,
`OK (skipped=1)`. The existing 1,082-test suite was also run separately before
the new test module was added: `OK (skipped=1)` in 129.381 s.

Coverage includes actual SIGKILL after durable intent, restart twice with one
child and one debit; a separate controller SIGKILL after creation followed by
a fresh interpreter loading **only sessiond**, which reaps the whole tree and
a real incarnation-marked sleeper process in cascade order. No X server or
input backend is involved. Other cases inject crashes after reservation,
before/after creation and completion, after artifact copy and after result
commit; test HUMAN recovery races, caps, budget retries, session-id reuse,
old-generation done, poisoned summary as data, prompt provenance, artifact
symlink/traversal/quota checks, attention and the unchanged leased done path.

As in WM-A, AF_UNIX binding and SO_PEERCRED are injected in sandbox tests,
while real socketpair FDs, fragmented framing, selector I/O and files exercise
the server. Mode 0600, foreign-UID rejection, client limits and oversized
request branches are covered. Kernel credential enforcement is a live check.

## Raj's live checks — NOT RUN here

Run these separately on the real host and the x86 Debian generic VM. Use only
the session allocator's displays :91–:120; never :0. Keep evidence per machine:
registry snapshots, controller/sessiond journals, worker journals, process/
device inventories, delivered event JSON and artifact checksums. A dedicated
registry/host session set is needed for the stop-all/global-kill check.

1. **Two-session spawn/result flow.** Start one parent with
   `wm/ark-sessiond start --profile xvfb` (repeat the approved Xorg profile
   on the real device stack as appropriate), record its session/incarnation,
   and start its worker with `wm/ark-worker start --session sN`. Run
   `wm/ark-wmctl serve`. Verify socket owner/mode, successful owner RPC and
   refusal from a different UID. Issue the parent's capability and call spawn
   with a unique request id. Require one flat child desktop, depth 1, exact
   parent debit and a separate child allocation with zero charges. A retry
   with the same request id must return the same incarnation and debit.
   Start the child's normal worker and its single driver; read its tagged task
   queue. Have it explicitly save a harmless report under its HOME and call
   leased `session.done` with status/summary/artifact list (the adapter `done`
   tool supports this). Poll the parent's queue: require one result with child
   incarnation/task provenance, `untrusted:true`, and a readable exchange copy
   with matching SHA-256. Change/remove the private source: the exchange copy
   must remain. Ack, restart the controller, and require no redelivery. Beside
   the held driver, use controller `frames.get` and confirm the WM-A feed never
   changes the driver's lease, frame quota or input delivery.
2. **Controller-killed genealogy reap (deciding gate).** Build parent+child;
   for a separate depth test only, temporarily raise admission after approved
   capacity measurement and add a grandchild. Record every incarnation, PID,
   display and device. SIGKILL the controller; leave it dead. Run
   `wm/ark-sessiond stop --incarnation P` (also exercise legacy
   `stop --session sN` on another tree). Require journal order: parent gate,
   descendant gates, deepest child reaped, then parent. Confirm every marked
   process has exited, X sockets/locks and per-session input devices/grabs are
   gone, and no subtree record remains. Host/sibling sessions must be intact.
   Restart the controller: no intent may resurrect the tree. Create another
   session, observe possible `sN` reuse with a different incarnation, and retry
   stop on the old incarnation: the replacement must survive.
3. **Admission/control/budget gates.** With the default two live sessions,
   another spawn returns `admission_limit`; collapsing or completing a task
   does not free admission. Use the existing worker control command to put the
   parent into HUMAN/paused, then require `parent_human`/`parent_paused`, zero
   new child and zero new debit. Do not enter a passcode. Restore control using
   the existing worker procedure (fresh driver attach/observation when needed).
   Exercise fan-out 4/depth 3 only with an explicitly raised, measured capacity
   or a headless registry fixture; the default admission cap intentionally
   binds first. Exhaust a small reservation and require `budget_exhausted`.
4. **Crash and attention.** Kill/restart the controller during provisioning;
   verify stable child incarnation and one debit, with no surviving duplicate
   processes/devices. A partial creating attempt must be reaped before retry.
   Have the child's leased worker report `session.human_need` as `passcode`
   while its view is collapsed/absent: require attention on all ancestors;
   clear with `need:null`. Inspect journals for category/provenance only, no
   human keystroke/passcode content.
5. **Global kill.** On the dedicated test session set, create the configured
   `CU_KILL_FILE`, allow the maintenance tick, and require every session's gate
   closed parent-first and the complete forest reaped child-first. Require
   `global_kill` on another spawn. Remove the kill file deliberately after
   checking the stopped state. No input is replayed. Separately keep the
   controller dead and use sessiond stop to repeat the independent reap gate.

Presenter transforms, nested hit-testing, takeover input capabilities and
promotion remain WM-C/WM-D work. Live results must be appended separately;
the sandbox result does not qualify either machine's live gates.
