# App packs step 2 — registry and Thunar — 2026-11-11

Built from `hidden_files/COUNCIL-AGENTS-RECONCILED.md` (present in this
checkout) and `bench/packs-adapter-2026-11-11.md`. This is adapter-level
specialization shared by Codex CLI over MCP and ark-loop. No worker code,
worker protocol, lease topology, loop system prompt, budget or reconciliation
rules changed. No live session, provider API, input device or display
(including `:0`) was opened/driven. Nothing staged or committed; existing
unrelated checkout changes were preserved.

## Pack contract and registry

`wm/packs/` is a stdlib-only Python package. A pack module exports:

- `NAME`: string; `CLASSES`: nonempty tuple of WM_CLASS strings, matched
  case-insensitively against the focused window's class.
- `PLAYBOOK`: prose, appended after the observe state as a separate,
  delimited `[pack: NAME] ... [/pack]` text block. The original observe JSON
  remains first and the stable system prompt stays unchanged. Both driver
  paths consume this same adapter rendering.
- `SKILLS`: skill name to `{params, summary, run}`; `params` is a closed
  object JSON-schema dict, `summary` is one line, and
  `run(adapter, args)` returns a compact result dict. Skill code validates
  its arguments; the registry validates metadata/schema structure, JSON
  serializability and absence of authority fields.

`REGISTRY` discovers modules in sorted order. An import failure (including
`SystemExit`), malformed contract, duplicate pack/skill name or authority
field in a params schema skips the entire offending module. Failures are
recorded as module/error-type entries in `REGISTRY.diagnostics`; successful
packs remain available. Publication is atomic per module. In overlapping
class registrations, the first discovered pack wins deterministically.
There are no authority fields in the advertised pack schemas. The registry
also rejects nested schema properties named risk, allow_stale_pixels, lease,
base_rev, control_epoch, action_id, generation or session.

## Adapter behavior

The adapter merges delivered window-state deltas and removals, tracking global
focus independently of the observed tree's window. It retains unsettled deltas
internally so a subsequent settled/unchanged result cannot lose window classes,
and retains the modal set when an unchanged result omits state. Full session
reads reset that cache; scoped reads do not substitute their anchor for focus. Unsettled
or resync-required reads cannot switch the pack. As required by the governing
plan, switching is deferred while a session modal is present; no skill starts
a mutation through that modal. A worker generation change clears pack state.
Switching does not acquire/release a lease, reset a budget or alter recovery.

The new `use_skill {name, args}` tool statically advertises every loaded skill,
summary and parameter name. Unknown skills name the loaded skills; inactive
skills name the active pack (or none). Each dispatch checks fresh full session
state; the skill then resolves its targets from its own fresh settled tree.
Every input goes through `Adapter.call('act', ...)`, preserving identity,
lease, revision, worker guards and automatic reconciliation. Unknown or
contradicted effects return their original status with no mutation replay.
If an earlier interrupted invocation left a pending action, `use_skill`
resolves that original action ID and reports `previous_action_resolved; skill
not run`, without starting the requested skill. Only compact skill results
escape, never raw receipts. Both one-shot and MCP entry points are exercised
with fake socket peers.

The Thunar create-folder example was removed from the generic act description
and moved verbatim into the qualified Thunar playbook. Generic step/expect and
binding grammar guidance remains. Existing tool-inventory and worked-example
tests were updated to reflect the new tool and location.

## Thunar pack

`wm/packs/thunar.py` applies to `CLASSES = ("Thunar",)`. Its playbook records
Details-view exposure, unnamed row/name-child structure, F2/Return and
session-wide modality, viewport virtualization around 27+ entries, clipboard
preservation via `via:"keys"` during copy→paste intervals, and the
Thunar-specific create-folder worked example.

Both skills parse fresh `lines/1` trees, preserving row versus name-cell refs
and decoding JSON-escaped names. Listing resolution excludes metadata cells
and app chrome. If a tree has continuation pages, skills read them against the
explicit content window and reject revision/focus/modal changes rather than
act on a mixed snapshot. They do not scroll or claim to enumerate virtualized
rows; a missing rename target returns up to five visible candidates.

- `thunar.create_folder {name, dir?}`: detects visible collisions before input;
  switches to Details view once if the table/items are absent and resolves
  again; binds the actual create dialog/entry, types with replace, then
  commits with an item-present check in the original listing.
- `thunar.rename {old, new, dir?}`: missing, ambiguous and collision checks
  return before any act. The D1 batch targets the unnamed row, guards the
  name cell's `selected_ref`, presses F2 with rename-dialog binding, replaces
  the bound entry text with implicit worker text_equals, then presses Return
  with `all(dialog_absent(bound handle), node_present(atspi table_cell/new))`.
  Timeouts are 2000/3500/3500 ms. Selection remains a worker precondition;
  an unselected name cell is not silently selected/retried by the skill.

After all three steps are verified, the filesystem oracle checks existence of
`dir/name` for create, or existence of `dir/new` and absence of `dir/old` for
rename. `dir` defaults to the session's `session_dir/home`, independent of the
adapter process's inherited HOME. An explicit `dir` selects the oracle path;
the skill operates on the current listing. `status:"success", verified:false` means
the worker batch verified but the filesystem check did not. An unverified
batch never runs the oracle or claims verified success. Filenames must be
single nonempty components; undeclared/authority arguments are rejected.

## Headless validation

`wm/tests/test_worker_w_packs_thunar.py`: **29 tests passed**. Coverage includes
registry discovery/failure isolation, metadata and authority-field rejection,
focus versus scoped-tree activation, settled/delta/removal/modal behavior,
playbook placement and generic-example removal, MCP dispatch errors, exact
create/rename step construction, no-act collision/ambiguity/not-found paths,
Details switching, tree paging, filesystem oracles against temporary paths,
uncertain and incomplete effects, recovery of the original ID after a lost
response, interrupted-invocation handling, and both MCP and one-shot skill
entry points. Fake worker peers and headless filesystem fixtures only.

Full default discovery (all temporary paths kept in the checkout and bytecode
disabled):

```sh
TMPDIR="$PWD/wm/tests/.tmp" PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s wm/tests
```

**1028 tests in 128.401 s: 1027 passed, 1 skipped**, exit 0,
`OK (skipped=1)`. This is the 999-test baseline plus 29 new tests. The
existing sandbox-dependent ptrace/strace check remains skipped.

Log: `wm/tests/.tmp/packs-thunar-full-suite.log`.

## Raj's live gates — NOT RUN

- **D1 harness rerun — NOT RUN.** On a dedicated session/display 91–120,
  rerun create/rename with 3/3 verified steps, filesystem oracle, 30/30-run
  gate and existing isolation observers. Check the row/name-cell selection
  guard, bound rename dialog and explicit AT-SPI selector after commit.
- **One-shot skills with filesystem oracle — NOT RUN.** Against that live
  session, run `ark-act call use_skill` for both `thunar.rename` and
  `thunar.create_folder`, checking actual session-home paths (and an explicit
  `dir`) independently. Check collision/not-found exits, icon→Details
  switching, unexpected modals and uncertainty without mutation replay.
  Example invocation shapes:
  `wm/ark-act call use_skill '{"name":"thunar.rename","args":{"old":"old","new":"new"}}' --session s1`
  and
  `wm/ark-act call use_skill '{"name":"thunar.create_folder","args":{"name":"Reports"}}' --session s1`.
- **Measurement pilot — NOT RUN; separate later step.** No paired generalist
  versus pack runs, benefit claims or measurement pilot were attempted.

## Fix round 2

Raj's November 11 live skill gate found that settled Thunar observes routinely
serve a tree page with `dirty:true` while icon view invalidates its AT-SPI
snapshot. That flag is a freshness caveat; immediately rejecting it prevented
the skills from reaching their work, including the create skill's Details
switch.

`wm/packs/thunar.py` now retries a dirty initial tree page with the same full
session observe, up to **five observes total per page-set attempt**. Exhaustion
returns `listing_unavailable` with detail `tree_dirty`, without input. A dirty
continuation discards the entire page set (lines and seen cursors) and starts
again from a full session observe, with **at most three restarts**. Exhausting
that restart limit preserves `listing_unavailable` / `state changed during
tree paging`. Each new page set establishes its own revision baseline.
All existing settled/resync, class, modal, window, revision, focus, repeated
cursor and missing-continuation checks remain; other failures still stop
immediately, even on a dirty page. Batches, timeouts, skill short-circuits and
filesystem oracles are unchanged. No worker or adapter changes.

The headless pack test file now has **37 passing tests** (eight added),
covering dirty-to-clean recovery for both skills, recovery through the icon
to Details switch and mutation, exactly five observes and zero acts for a
permanently dirty initial page, success on the fifth initial observe, full
page-set discard with a fresh revision and reused cursor, the three-restart
boundary, and fail-fast validation alongside dirty flags. Observe and act
counts are asserted.

Full headless validation, with temporary paths in the checkout and bytecode
disabled:

```sh
TMPDIR="$PWD/wm/tests/.tmp" PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s wm/tests
```

**1036 tests in 132.848 s: 1035 passed, 1 skipped**, exit 0,
`OK (skipped=1)`. The existing sandbox-dependent ptrace/strace check remains
skipped. Log: `wm/tests/.tmp/packs-thunar-fix2-full-suite.log`.

**Raj's live skill gate re-run — NOT RUN.** No live session or display was
opened or driven, including display `:0`. Nothing staged or committed.
Existing unrelated checkout changes were preserved.
