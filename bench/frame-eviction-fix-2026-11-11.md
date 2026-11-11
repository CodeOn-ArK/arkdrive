# Frame eviction and driver prompt fixes — 2026-11-11

`Frames.evict()` now orders known files by their first occurrence in the
insertion-ordered `issued` bookkeeping. Reissuing identical content, including
with different frame metadata, preserves the file's original position. Files
restored after restart have unknown insertion order: they precede current
issuances and use `(st_mtime_ns, filename)` only as their fallback ordering.
The 200 MiB default quota, last-50-receipt protection, frame format and API are
unchanged.

Only the tree-expect guidance passage in `worker_loop.SYSTEM` changed. Its
complete `text_equals` examples put `provider: atspi` inside `selector`, or use
`ref: n7` with no provider. The headless prompt test extracts and JSON-parses
both examples, calls the worker's `predicate_schema()` validator, and confirms
predicate-level `provider` is rejected with `invalid_params`.

Test evidence on ArK2 (headless):

- Before the production fixes, the new equal-mtime regression failed in both
  receipt-protected and unprotected subcases. It assigns equal mtimes after
  every put and inserts payloads in descending SHA-256 filename order. Thus
  the old `(mtime, name)` sort must select the newest existing candidate first;
  it cannot pass accidentally because of filesystem timestamp precision.
- After the fixes, all seven `FrameQuotaTests` and both `PromptCase` tests
  passed (9 tests). Coverage includes successive quota evictions, survival of
  the receipt-referenced oldest file, restart fallback, shared-file reissuance,
  quota bounds and receipt expiry.
- Full required suite: **770 tests in 123.300s, OK (skipped=1)**, exit 0.
  The existing strace gate is skipped where the sandbox denies ptrace.
  Existing W2 cleanup ResourceWarnings were non-fatal.

```sh
TMPDIR="$PWD/wm/tests/.tmp-frame-eviction-2026-11-11" \
  PYTHONDONTWRITEBYTECODE=1 \
  python3 -m unittest discover -s wm/tests -p 'test_worker_w*.py'
```

The guest-VM acceptance run remains Raj's. No staging or commits.

## Verifier addendum (Raj, 2026-11-11)

Full suite re-run by Raj on ArK2: 770 tests, OK (zero skips outside the
sandbox). Guest acceptance in the generic Debian 13 VM (the environment
where the defect was found): worker_pixels.py + test_worker_w3.py synced
into the guest's ~/arkdrive; all 7 FrameQuotaTests PASS, including
test_oldest_evicted_receipt_reference_protected (deterministic failure
pre-fix) and the new equal-mtime regression. Fix closed.
