# Memory cleanup Step 1 — retire durable completed actions

2026-11-11. Implements Step 1 of
`hidden_files/COUNCIL-MEMORY-RECONCILED.md`. The reported live Step-0
baseline was one retained action per completed act and about 4.3 KB of
journal per act. **Live gates: NOT RUN here; Raj runs them.** No display,
including `:0`, was opened or driven. Nothing staged or committed.

## Behavior and stub format

`Engine.actions` now holds full records only while they need retention.
After a receipt append, flush and fsync succeed, an eligible FINAL record
is replaced by this entry in the engine's insertion-ordered stub cache:

```text
SHA256(action_id UTF-8).digest() -> (request_hash, receipt_byte_offset)
```

The key is a 32-byte action-ID digest; the request hash is the existing
SHA256 hex string; the offset is an integer byte position at the beginning
of the receipt's JSONL row. The engine belongs to one generation, so the
generation is implicit. Stubs retain no request, receipt, peer, runner or
acknowledgement objects. The active generator can still reference its full
record until it returns to the owner loop.

Compaction requires a durable intent and receipt, FINAL state, a matching
published journal index entry, and a valid index. It refuses records with
unknown/contradicted effects (including any individual step), pending
effects in the receipt or current reconciliation queue, incomplete input
acknowledgements, or unconfirmed cleanup. Unknown/contradicted receipts
remain full even after a later observation acknowledges them. Records
whose write/fsync failed remain full; the existing `action.get` storage
validation and durable repair path can make an otherwise eligible record
compactable later. An unsuccessful repair leaves it pending.

Dedupe checks full records, stubs, then the generation's journal index,
all **before lease validation or provider admission**. A matching retry
returns the original receipt, including timings and evidence, without
writing another journal row or dispatching input. A changed normalized
request keeps the existing `action_id_conflict` result. A missing,
unreadable, invalid or mismatched indexed receipt fails closed; it never
turns into a new enqueue. The existing normalized-request hashing rules
are unchanged. Cross-generation submissions still fail before enqueue.

## Journal index and limits

The journal appends binary UTF-8 JSONL, with unchanged JSON content, so
offsets are bytes even when IDs or evidence contain non-ASCII characters.
Offsets become available only after successful fsync. `Journal.action()`
now seeks directly to indexed rows in the current generation. For an
ordinary completed action it reads one receipt row; reconciliations add
only that action's supplemental rows. Missing IDs require no journal
read. This avoids a full-file scan on each dedupe or `action.get` lookup.

The index maps the same action-ID digest to a slotted metadata record:
request hash, intent offset, latest receipt offset, and armed/reconciliation
offset lists. Armed offsets are discarded once a receipt is durable.
It contains no decoded requests or receipts. Opening a journal builds its
index once; explicit storage validation rebuilds it after syncing and
checking every JSONL row. Recovery of unresolved effects across generations
is unchanged. Older-generation retrieval uses the explicitly named
`Journal.history_action()` scan; streaming historical recovery and journal
retention remain Step 5 work.

**Stub cache ceiling: 4,096 entries per engine/generation**, oldest first,
without promoting entries on reads. This is a hot metadata cache budget,
not a claim about a hardware-derived total memory limit. Before dropping
the oldest stub, the engine verifies that its hash and receipt offset are
still present in the journal index. If that guarantee is absent it keeps
the stub, prioritizing dedupe over the ceiling; an invalid index also
prevents new compaction. Normal operation retains every index entry for
the generation, so the stub ceiling is enforceable and old dedupe works.

**The index itself grows with distinct actions. Total action metadata is
not bounded for an unlimited generation.** Exact dedupe is not possible
after deleting both an action's stub and its index entry without searching
or adding another store. This change does neither: no index eviction,
disk-backed dedupe database, generation rollover, or new admission limit.
Unsafe full records can also accumulate deliberately. The result removes
receipt-sized retention, not every source of RAM growth. Journal disk
growth remains approximately one intent/receipt history per act.

## `action.get` and memory counters

The common final result is:

```json
{"status":"final","receipt":{},"reconciliation":[{"step_id":"1","rev":2,"effect":"verified"}]}
```

`receipt` above stands for the complete original receipt. Reconciliation
is always a list (empty when absent); entries contain only `step_id`,
`rev`, and `effect`. RAM, indexed current journal, and historical journal
paths agree. Journal envelope fields (`ts`, `type`, `session`,
`generation`, `action_id`) no longer leak into historical supplemental
entries. Incomplete historical reads retain `history_complete:false` and
the existing recovered unknown receipt. Receipt contents and driver wire
schemas otherwise stay unchanged.

`system.info.memory` remains **schema 1**, with two additive gauges:

| Gauge | Meaning |
| --- | --- |
| `actions_retained` | Full records in `Engine.actions`, including unsafe FINALs; excludes stubs |
| `action_stubs` | Current engine's cached digest/hash/offset stubs, normally at most 4,096 |
| `journal_actions_indexed` | Current generation's indexed action identities, including unfinished intents; grows with history |

The first gauge still measures `len(Engine.actions)` as in Step 0; the
underlying store now retires eligible records. These counts are not byte
estimates. Existing memory gauges and cumulative action counters remain
unchanged. Collection does not prune or read receipt payloads.

## Headless validation

`wm/tests/test_worker_w_memory_step1.py` adds 16 tests covering identical
dedupe after compaction and lease removal; eviction and conflict checks;
unknown/contradicted retention even after observation; pending-effect
reconciliation; equal retrieval shapes; unfinished records; incomplete
acks and unconfirmed cleanup; actual intent/receipt fsync failures and
repair; unreadable receipt/index/offset failures; constant-row lookup;
Unicode index rebuilding; and churn plateau behavior.

The automated churn case performs 128 real engine wait/window-present
acts with real journal/fsync and fake X, using an eight-stub ceiling to
exercise repeated eviction. Every 16-act checkpoint reports zero full
records, eight stubs, and an index increasing from 16 to 128. Existing
Step-0 tests now assert the new full/stub split. Its fake popup lifecycle
also now models the harness's existing Return dismissals (four wait acts
plus two triggers plus two dismissals), fixing a stale six-act expectation.
W1/W2 historical-journal tests use the renamed historical scan method.

A separate headless run used the **default 4,096 ceiling** and 5,000 unique
wait/window-present actions, real journal/fsync, the W2 fake-X harness,
and temporary storage under `wm/tests/.tmp`. Response copies were cleared
after each act so the test client did not retain all receipts:

| Completed | Full records | Stubs | Indexed identities | Journal bytes | Elapsed seconds |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 100 | 0 | 100 | 100 | 402,984 | 0.110 |
| 1,000 | 0 | 1,000 | 1,000 | 4,044,401 | 1.041 |
| 4,096 | 0 | 4,096 | 4,096 | 16,616,965 | 4.210 |
| 5,000 | 0 | 4,096 | 5,000 | 20,287,205 | 5.131 |

Afterward the original lease/holder were removed. Retrying action zero,
whose stub had been evicted, returned the identical saved receipt. No X
input events were emitted. These are headless inventory and correctness
results, not worker PSS measurements or a live latency/24-hour soak gate.

Final full headless suite: **902 tests in 126.175 s; 901 passed, 1 skipped**,
exit 0, `OK (skipped=1)`. This is the Step-0 total of 886 plus 16 new
Step-1 tests. The skip remains the existing ptrace-dependent OCR strace
check. Output: `wm/tests/.tmp/memory-step1-full-suite.log`.

Full-suite command (same temporary-directory redirection as Step 0):

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

Use a fresh dedicated session, record its worker generation/profile and
storage setup, and rerun the Step-0 harness. A small window/popup workload
keeps the focus on act churn; use tmpfs for a large fsync-heavy run as
described in the Step-0 note:

```sh
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 wm/tests/memory_step0.py \
  --session sN --windows 20 --acts 100000 --popups 10 --batch 1000 \
  | tee wm/tests/memory-step1-live.jsonl
```

Check these against the original baseline:

- During successful act churn, full `actions_retained` should return to
  zero at settled samples; `action_stubs` should plateau at 4,096 while
  `journal_actions_indexed` and `journal_bytes` continue increasing.
  Compare worker/child PSS slopes, without claiming total RAM must be flat.
- Save one early request and receipt using a direct RPC client. After
  more than 4,096 actions and after releasing/expiring its original lease,
  resend the exact saved request in the same generation. It must return
  the same receipt and produce no additional dispatch or journal record.
  A changed request with the same ID must conflict.
- Compare early/late dedupe-hit and `action.get` latency, especially cold
  storage reads. They now require synchronous receipt reads; normal act
  verification adds no journal reads. Constant indexed row count is not
  proof of acceptable live I/O latency.
- Exercise an uncertain/contradicted act in the dedicated live fixture.
  Its full record must remain available, `awaiting_reconciliation` must
  block subsequent mutation as before, and holder full settled observation
  must reconcile it exactly as before. Supplemental `action.get` entries
  must have the same three-field shape before/after restart.

Live PSS, hardware latency, 100k-act, crash/disk-full and 24-hour gates
remain **NOT RUN**. Production changes are confined to `wm/ark-worker`
and `wm/worker_act.py`; test changes stay under `wm/tests/`; this is the
only new bench note. Steps 2–5 remain out of scope.

## Verifier addendum (Raj, 2026-11-11)

Suite on ArK2: OK (901 tests at build). Live check with the step-0
harness on a fresh xvfb session (--windows 10 --acts 300 --popups 2):

- act_churn: actions_retained delta 0 (baseline was +300, 1:1), with
  action_stubs +300 and journal_actions_indexed +300 - every completed
  act compacted to a stub, exactly as designed. Journal bytes still
  grow ~4.3 KB/act on disk (that is step 5's scope, not step 1's).
- window/popup phases unchanged (tombstones are steps 2-3 scope).

Committed 52cea81.
