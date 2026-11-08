# Release checklist — what remains before this goes public

Everything below needs a decision or an outward-facing action, which
is why the release prep stopped here. The code is ready to flip.

## Decisions (owner)

- [x] **License.** DECIDED Nov 8, 2026: **MIT**, copyright holder
      **Saheb Panda** (filled in `LICENSE`).
- [ ] **Name.** Candidates from the plan: **FlightDeck**,
      **GroundTruth**, **Rung**, **Deadreckon**, **Veranda** — or
      another name entirely. Before adopting: check PyPI + GitHub for
      collisions, then rename the repo, the README title, and the
      `<NAME PENDING>` spots.
- [ ] **Headline benchmark models.** The launch table currently shows
      the local 27B model's row (V-cards failing, printed on purpose)
      plus the Codex-driver and human-reference rows. Decide: publish
      as-is, or first run the suite against one cloud model for a
      stronger V column (per-run API cost; screen content leaves the
      machine during bench runs).
- [ ] **Switch adjudicator** (an optional second-opinion model
      call at the strategy-switch point; spec-only today). Plan
      recommendation: ship v1 without it.
- [ ] **Lanes scope.** `lane.sh` ships as the reference multi-session
      setup. Decide: v1 as-is, or single-desktop only with lanes as a
      follow-up.

## Actions (after decisions)

- [ ] Create the public repo and push this tree (tagged `v0.1.0`).
- [ ] Fill the LICENSE copyright holder; replace name placeholders.
- [ ] Clean-VM check: fresh install via `install.sh` alone, then
      `tools/check_deps.py`, `tests/test_smoke.py`, and one B1 bench
      run — the release gate from the plan.
- [ ] Announcement post built around the per-model results table,
      including the honest V-column failure.

## State at prep time (Nov 8, 2026)

- Core tests: SMOKE_OK, CORE_OK on the release build.
- Benchmark integrity: silent failures 0 across all measured runs.
- Personal-identifier sweep over the tree: 0 hits.
- Full hardening suite: deferred (see CHANGELOG known limitations).
