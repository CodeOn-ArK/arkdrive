# Changelog

## v1.0.0 — 2026-11-09

First full release. The executor core is unchanged from v0.1.0. What
is new is benchmark evidence for how an external agentic driver
should be briefed, measured on a local GUI task.

### Added
- Benchmark suite (`bench/`): the 10 fixed task cards, the runner,
  and the per-model results, extended in this release with two A/B
  profile benches (a local GUI task and a nested-desktop Virtuoso
  task), each shipping both driver prompts, a runner note, and its
  results file.
- Lean driver profile for agentic drivers (Codex and peers):
  text-first observation (OCR, window titles, app state) instead of
  a screenshot at every step, a one-line-per-step state ledger
  instead of re-reading full history, batched scripted bursts for
  predictable stretches, and state-based verification, with a full
  screenshot kept at the final gate. It ships as bench prompts, a
  runner note, and results under `bench/`. It is not integrated
  into the executor core.

### Measured
- Local GUI A/B, same task and same machine, Codex driving both
  profiles: the lean profile used 52,372 tokens vs 120,557 for the
  standard screenshot-driven profile (56.6% fewer) and finished in
  159 s vs 223 s (28.7% faster).
- Accuracy: the lean run's saved file was byte-exact. The standard
  run failed on one extra trailing newline that its
  screenshot-per-step verification never caught, while still
  reporting success. One run per profile on one small task, so the
  percentages are directional; the defect class here is one that
  screenshots cannot see.

### Known limitations
- The Virtuoso (nested-desktop) A/B is staged but has not run: both
  runs are blocked on USC scf02 server access (SSH key authorization
  broke over Nov 8 to 9). The prompts are committed and the bench
  will run when access is restored.
- All v0.1.0 known limitations still apply.

## v0.1.0 — 2026-11-08

First packaged form of the executor, de-coupled from its original
single-machine deployment and prepared for public release.

### Added
- Runbook packs (`CU_RUNBOOK_PACK`): task-specific briefings, bootstrap
  targets, and done-guard hints as external JSON data; the executor
  itself is domain-blind. Fictional example pack included.
- Generic configuration: `CU_MODEL_SERVER` / `CU_MODEL` /
  `CU_VISION_SERVER`, optional JSON config file (`CU_CONFIG`), and a
  `--check-config` self-report.
- Kill switch (`CU_KILL_FILE`, default `/tmp/cu-executor.kill`):
  checked before every action; ends runs `ABORTED_KILLED`. Live-verified.
- `--dry-run` CLI flag; in dry mode the effect tools (`shell`, `edit`)
  are gated — intended actions are logged, never executed. Live-verified.
- `tools/check_deps.py`: per-surface (browser/native/vnc) readiness
  report, rung by rung.
- `tools/audit_report.py`: renders the JSONL progress log into a
  human-readable audit timeline.
- `install.sh`: dependencies, AT-SPI flag, ydotoold as a persistent
  user service (the daemon previously vanished silently on reboot).
- Public benchmark suite (`bench/`): 10 fixed task cards, a runner
  scoring task pass and run integrity separately, and the first
  per-model results table (`bench/RESULTS.md`). Silent failures: 0.
- Lean core regression set (`tests/test_core.py`) re-pointed at the
  dispatch-receipt contract; full hardening suite retained as
  `tests/trio2_reference.py` for progressive re-basing.
- Docs: architecture, safety model, runbook-pack format, and the
  hardening history (`docs/`).

### Changed
- The browser lane's dispatch contract is the structured receipt
  (`{version, phase, dispatched, effect}`); the executor branches on
  receipt data only. (Predates this changelog; first shipped here.)
- Auth-challenge markers are generic and configurable
  (`CU_AUTH_MARKERS`).

### Known limitations
- Linux + X11 only. Wayland-native, macOS, Windows: out of scope for v1.
- GUI task success is bounded by the driving model; see the per-model
  table for measured results, including the local model's failure on
  the flagship nested-desktop card.
- The full 103-function hardening suite has not been re-run since the
  receipt redesign; the core subset has. Stated plainly, on purpose.
