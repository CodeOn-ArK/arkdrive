# Memory cleanup Step 4 — frames

2026-11-11. Implements Step 4 of
`hidden_files/COUNCIL-MEMORY-RECONCILED.md`.
**Live gates: NOT RUN in this sandbox.** No display, including `:0`, was
opened or driven. Nothing staged or committed. Changes are confined to
`wm/` and this note.

## Changes and caps

`Frames` now enforces both the existing **200 MiB** byte quota and a
**4,096 PNG file** limit. It reserves bytes and a file slot before writing a
new file; shared content consumes no additional slot. Eviction selects the
oldest eligible file until both budgets fit. If protected files prevent
that, the existing `provider_degraded`, detail `frame_quota`, is returned.
Receipt protection still covers all referenced files in the last **50
receipts**, including receipts restored from the journal before startup
quota enforcement. In-progress receipt pins remain separate.

A file's first-issue sequence now lives in `Frames.file_sequence`, separate
from the bounded `issued` transform cache. Reissuing shared content, looking
up transforms, and trimming identities never change file order. File
sequence entries disappear with deleted files. The Nov 11 insertion-order
regression tests in `test_worker_w3.py` are **unchanged byte for byte**.

Restart semantics remain those of that fix: files surviving a restart have
unknown historical issuance order, so they are ordered once by
`(mtime_ns, filename)`, before new issues. These ranks become independent
file sequences for the new worker lifetime. They survive reissuance and
identity trimming, even if mtimes subsequently change. There is no new
persistent index or wire metadata, and no change to the existing restart
fallback test's expected order.

The RAM identity cache retains at most **4,096 transforms**, trimming the
oldest cached identity on insertion. Reads and reissuance of an already
cached identity do not reorder it. Identity expiry can happen while its PNG
is still present or protected: file survival does not promise indefinite
coordinate-transform validity. `Pixels.desktop_point` already checks the
cache with `.get()` and returns `transform_stale` on a miss. Action points
and frame-based observation/ignore regions pass through that check before
using transform metadata; the new tests exercise those public paths,
including an action receipt with no dispatched input. No fallback transform
is inferred from shared PNG content.

At pixel-store construction, interrupted `*.tmp` files, including hidden
ones, are removed. PNG evidence and unrelated files/directories remain.
Quota eviction waits until durable receipt references have been restored.

Each lease pins its last **two delivered observation frames**. Only frames
in observations passing the existing response-size guard and matching the
current lease/peer advance that history. State-only/unchanged observations
leave the frame history alone. Observation pins are independent of receipt
pins, so completing receipt capture cannot clear them. They release on
`session.done`, disconnect-grace expiry (the existing **5 seconds**), or a
control epoch invalidating the lease. During disconnect grace they remain
protected. Only files are pinned; the transform-cache bound still applies.
Pins never override either quota: if protected evidence plus a new frame
cannot fit, the existing quota error remains the result.

There are no driver wire/receipt schema changes and no consumer
acknowledgement protocol. Production changes are in `wm/worker_pixels.py`,
`wm/ark-worker`, and `wm/worker_act.py`; new tests are in
`wm/tests/test_worker_w_memory_step4.py`.

## Headless validation

The 12 new tests use real frame files, PNG generation, worker observation
assembly/delivery and action processing with fake transports. They cover:

- Cache eviction with shared PNGs and different transforms: direct points,
  engine points, regions and an input action return `transform_stale`;
  retained transforms still produce the exact expected point.
- File-count eviction independent of cache identities, equal mtimes,
  shared-file reissuance and restored files; a production-sized startup
  inventory of 4,097 files is reduced to 4,096 and the next issue removes
  the next oldest file.
- Nested receipt evidence, protection through the 50th receipt, expiry at
  the 51st, protected-file quota failure, and restoration of receipt
  protection before startup count eviction.
- Orphan cleanup through `Pixels` construction, preserving PNG evidence,
  unrelated files and directories.
- Two delivered observation frames surviving byte-quota eviction while an
  older unreferenced observation file is removed; receipt-pin cleanup and
  observations without frames do not release lease pins.
- Lease completion, expiry boundary and control-epoch release; foreign
  peers, mismatched leases and oversized responses do not acquire pins.

Targeted frame-quota/action/Step-4 run: **26 passed in 0.957 s**.
Complete existing W3 suite after integration adjustment: **48 passed in
3.731 s**. Full headless suite: **945 tests in 126.359 s; 944 passed,
1 skipped**, exit 0, `OK (skipped=1)`. This is Step 3's 933 total plus
12 new tests. The skip is the existing ptrace-dependent OCR strace check.
Log: `wm/tests/.tmp/memory-step4-full-suite.log`.

The full runner discovers only `test_worker_w*.py`, uses
`PYTHONDONTWRITEBYTECODE=1`, places temporary directories under
`wm/tests/.tmp`, and redirects the W2 harness's repo-root temporary-directory
request there at runtime, as in Steps 0–3. No live gate is launched.

## Raj's live verification — NOT RUN here

Use a dedicated session on display 91–120, with a ready worker and a single
driver. Record generation, profile, hardware, storage and Step-0 baseline.

1. Repeatedly request changing pixel observations and cropped/scaled views.
   Check `system.info.memory.frame_identities` plateaus at 4,096 and
   `frame_files` stays at or below 4,096, while total PNG bytes stay at or
   below 209,715,200. Use enough distinct metadata to exhaust identities
   separately from content files. Submit an expired frame-space point and
   region; expect `transform_stale`, no input dispatch and a healthy worker.
   Confirm a fresh scaled frame maps to the correct native point.
2. Cross both quotas with changing views and receipt evidence. Compare
   file survival against first-issue order, including reissued shared files.
   Verify references in the last 50 receipts survive, become eligible after
   receipt 51, and quota exhaustion with only protected files returns the
   existing error without deleting evidence. On a worker restart, verify
   surviving-file fallback ordering and durable receipt protection.
3. Keep a lease active, obtain two distinct pixel views, and force eviction
   using later unrelated frame/receipt captures. Verify those two PNG paths
   remain readable while older unreferenced paths disappear. Obtain a third
   observation: its predecessor becomes eligible as it falls out of the
   two-frame history. Check state-only/unchanged observations and receipt
   completion preserve pins. Check lease completion, control-epoch changes
   and disconnect grace release them at the existing lifetime boundaries.
4. With the worker stopped, leave an interrupted `.tmp` file in its frame
   directory. Restart the worker; verify the temporary file is removed,
   PNG evidence remains and protected receipts are readable.
5. Compare worker PSS, frame issue/observe latency and restart time against
   Step 0 during churn and a longer soak, including on Pi hardware. Headless
   tests establish bounds and behavior, not live PSS or latency acceptance.
   Queue, peer and journal cleanup remains Step 5.

Live driver delivery, X/provider timing, crash-injection, disk-full, PSS,
latency, long-soak and hardware gates remain **NOT RUN**.
