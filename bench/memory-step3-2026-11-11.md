# Memory cleanup Step 3 — tombstone floor

2026-11-11. Implements Step 3 of
`hidden_files/COUNCIL-MEMORY-RECONCILED.md`.
**Live gates: NOT RUN in this sandbox.** No display, including `:0`, was
opened or driven. Nothing staged or committed. Changes are confined to
`wm/` and this note.

## Bound and frame budget

`StateCache.REMOVED_LIMIT = 4096` retains the newest 4,096 removed windows.
The reported Step-0 live measurement was 200 window cycles → +200 removed
windows, with full observe growing from 3,666 to 7,576 bytes:

```
(7576 - 3666) / 200 = 19.55 bytes/tombstone (~19.6)
4096 * 19.6          = 80,281.6 bytes (~78.4 KiB)
3666 + 80,281.6      = 83,947.6 bytes
262144 - 83,947.6    = 178,196.4 bytes remaining (~174 KiB)
```

The cap is deliberately well below the measured failure range near 13k
tombstones. History consumes about 31% of the adapter's 256 KiB payload
budget at that slope, leaving room for live windows, provider/coverage
metadata and the JSON-RPC envelope. Handle digit widths increase as churn
continues, so the measured slope is a sizing guide, not an absolute byte
proof. Arbitrarily many live windows or arbitrarily large titles/provider
payloads cannot be bounded by any tombstone count alone.

Every observe delivery therefore checks the **actual compact UTF-8
JSON-RPC response, including its request ID and envelope**, against 262,144
bytes. A response exceeding that limit becomes the ordinary RPC error
`response_too_large`, detail `observe_frame_cap`. This applies to both
immediate and deferred observe delivery, before `Engine.observed` can
acknowledge reconciliation. It does not return a partial live-window
snapshot or record an undelivered full sample as reconciliation evidence.
The adapter frame cap and receipt schemas are unchanged.

For full session state, the additive `state.removed_elided` integer is the
exact number of older removals omitted from that response in the current
baseline. It includes both permanently evicted entries and any additional
entries temporarily retained for an active batch but omitted on the wire.
The count is cumulative, not the number evicted by this observe call. It
is zero before elision and resets with the cache's startup baseline.
Scoped observations omit the count because evicted family attribution is
no longer available; a fabricated scoped count would be misleading.

## Eviction and revision semantics

Eviction drops the oldest entries from `removed` and `tombstones` together,
and removes their `geometry[h]` facets. Dead-XID `last_window` entries are
swept as well. All retained removal payloads and geometry histories are
bounded by the retained count plus live windows. After the provider's
Step-2 close/unmap pruning, the matching `tree[h]` facets are swept; tree
facets also disappear for unmapped windows that do not yet have a removal
tombstone. Frozen tree cursor pages retain their existing lifetime policy.

The worker's eviction gate checks `Engine.active is None`. Safe points are:

- Action ticks before selecting the next queued batch and after an active
  batch's generator finishes, including receipt creation/durability.
- Provider `prune_closed`, after pruning per-window state.
- The end of ingestion and observe preparation/assembly.

All these paths share the same gate. Uncommitted cache changes postpone
eviction until removal revisions are committed. An active batch keeps its
entire backing removal history, tombstone payloads and facet revisions;
observations during that batch do not evict it. This preserves before/after
`family_stamp` comparisons and verification evidence. New removals can
temporarily take the in-memory count above 4,096 until the batch finishes;
the bound is at safe points, not an eviction promise during input execution.
Wire observations always project at most the newest 4,096 removals, even
while backing history is pinned.

The revision floor advances to the largest revision of an omitted removal.
A delta is valid at the floor itself: deltas include only revisions
strictly greater than `since_rev`. A lower revision follows the existing
unknown-revision behavior (`None`, zero, future revision): `full: true`,
`unchanged: false`, and a full current state with bounded removal history.
The projected tail supplies the same effective floor while a batch is
active. Both the state cache and RPC observe preparation use this rule;
the RPC takes the existing full-sample pointer refresh path. Full tree
observations also reset the old tree delta baseline when the state falls
back to full. Scope/target and generation validation retain their existing
behavior; losing history does not relax action/ref validity.

## Headless measurements and regression tests

These are real cache/engine/framing paths with fake X and provider
transports, **not live windows, PSS samples, or latency gates**. The sizing
probe used full `channels:["state"]` worker observations and measured the
whole JSON-RPC payload with ID `floor-size`:

| Completed closes | Retained removed | Elided | Full response bytes |
| --- | --- | --- | --- |
| 4,096 | 4,096 | 0 | 86,147 |
| 8,192 | 4,096 | 4,096 | 87,256 |
| 12,288 | 4,096 | 8,192 | 89,546 |

The small byte increases reflect wider handle numbers and the elision
counter, rather than accumulating older removals. After that churn, a full
observation with **128 live windows, each additional window's title 517
UTF-8 bytes**, is **183,145 bytes**: 78,999 bytes (~77 KiB, 30%) below the
cap. Probe output: `wm/tests/.tmp/memory-step3-sizes.json`.

`wm/tests/test_worker_w_memory_step3.py` adds 11 regression tests:

- Three production-sized churn batches plateau the removed counter,
  tombstone payloads, geometry facets, dead-XID revisions and response size.
- Below-floor/unknown revisions return full in session and live window/
  family scopes; exact-floor and same-revision bulk removals preserve valid
  delta boundaries. Deferred RPC observes refresh the pointer and deliver
  the full response. Idle observe eviction and baseline reset are covered.
- A real input act starts with same-family tombstones, dispatches into a
  delayed acknowledgement, then crosses a small test floor during unrelated
  churn. Each close and an intervening observe preserve all original
  backing history and the original family stamp. After acknowledged input
  and batch completion, eviction runs; a below-floor full sample completes
  reconciliation, durably records `reconciled`, and preserves the receipt.
- A production-floor full response with 128 live windows is framed by the
  real response writer and remains at least 48 KiB below the adapter cap.
  An oversized live payload yields the bounded error without acknowledging
  reconciliation. Provider unmap pruning sweeps tree facets; a below-floor
  tree observation returns a full page rather than an old-baseline delta.

Targeted run: **11 tests passed in 0.990 s**. Full headless suite:
**933 tests in 126.269 s; 932 passed, 1 skipped**, exit 0,
`OK (skipped=1)`. This is Step 2's 922 total plus 11 new tests. The skip
is the existing ptrace-dependent OCR strace check. Log:
`wm/tests/.tmp/memory-step3-full-suite.log`.
The full runner uses the same runtime-only temporary-directory redirection
as Steps 0–2 and discovers `test_worker_w*.py`; it launches no live gates:

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

Production files changed: `wm/ark-worker`, `wm/worker_act.py`,
`wm/worker_tree.py`. The new tests and temporary logs are under `wm/tests/`.

## Raj's live checks — NOT RUN here

Use a fresh dedicated session with a ready worker, display 91–120, and no
other driver. Record generation, profile, machine and storage details. The
Step-0 harness's natural checks are `window_churn` and `observe_size`:

```sh
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 wm/tests/memory_step0.py \
  --session sN --windows 16000 --acts 1 --popups 1 --batch 20 \
  | tee wm/tests/memory-step3-live.jsonl
```

1. Check each settled post-close sample: `memory.removed_windows` rises
   to 4,096, then stays there. The emitted removed count also stays at
   4,096. `observe_size.growth_samples`/`response_bytes` must level off
   near the history budget rather than continue the old 19.6-byte-per-close
   linear growth. Every response remains below 262,144 bytes and
   `exceeds_adapter_cap` stays false. Inspect a full observe directly for
   the exact `state.removed_elided` count; the existing harness does not
   have a separate elision-count summary column.
2. Keep live fixture windows present while crossing the floor. Save a
   pre-floor revision; a later observe using it must return `full:true`
   with the complete live state and bounded removed list. Exercise the
   normal adapter as well as the harness connection, which deliberately
   tolerates oversized measurement responses. Check full tree pages and
   ordinary fresh deltas after resynchronizing.
3. Close/forget windows with published trees; check app/child map cleanup
   from Step 2 and absence of stale tree facets. Cross the floor during a
   delayed-input/verification batch; verify ordinary completion or honest
   existing failure behavior, followed by successful full-observe
   reconciliation, with no spurious route learning from history eviction.
4. Compare worker/child PSS and latency against Step 0. This change bounds
   the tombstone-owned metadata, not all session memory; remaining frame,
   queue, peer, lease and journal inventories belong to later steps.

Live churn, adapter delivery, GTK/libatspi timing, PSS/latency, long soak
and hardware gates remain **NOT RUN**.
