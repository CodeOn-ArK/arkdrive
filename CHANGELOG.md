# Changelog

## v0.1.0 — 2026-11-08 (pre-release; not yet published)

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
