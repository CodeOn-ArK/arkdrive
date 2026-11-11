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

## Fix round 3

Raj's November 11 live probes found that Return leaves the Create New Folder
dialog open in this Thunar/GTK build: the entry text remains verified, but no
folder is created and the listing check reports `provider_degraded`.
`thunar.create_folder` now adds `create: {role: "push_button", name: "Create"}`
to step 1's dialog/entry bindings. Step 3 targets
`{from_step: "1", name: "create"}` with `intent: {kind: "invoke"}`, without an
action or keys field. It retains the original listing's `item_present`
expectation and 3500 ms timeout. Steps 1–2 otherwise retain their behavior.
The playbook's create-folder JSON example and explanation use the same
Create-button binding and invocation.

`thunar.rename` now calls `_ready_listing()` before resolving the old name,
so icon view gets the Details-view switch and a fresh listing. Missing-name
candidates come from that listing. The rename batch, including its proven
Return commit, is unchanged. No worker or adapter changes.

The headless pack test file has **39 passing tests**. Exact create-batch
assertions cover all three bindings, the selector, the invoke-only commit,
the original listing expectation and the timeouts. The parsed playbook
example asserts the same binding and commit and rejects Return anywhere in
the create example or its explanation. Fake-adapter rename tests assert the
observe → Details act → observe sequence before resolving a visible target,
five real-listing candidates for a missing target, or an empty listing.
The successful path checks the fresh row/name-cell refs and revision before
the rename act. The prior empty-listing short-circuit assertion was adjusted
to account for the Details switch. The existing worked-example assertion in
`wm/tests/test_worker_w5_fix2.py` now expects the Create-button binding and
invoke commit; all 16 tests in that file pass.

Full headless validation, with temporary paths in the checkout and bytecode
disabled:

```sh
TMPDIR="$PWD/wm/tests/.tmp" PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s wm/tests
```

**1038 tests in 132.925 s: 1037 passed, 1 skipped**, exit 0,
`OK (skipped=1)`. The existing sandbox-dependent ptrace/strace check remains
skipped. Log: `wm/tests/.tmp/packs-thunar-fix3-full-suite-rerun.log`.
The initial full run had only the old worked-example assertion failure;
the complete rerun passed after that assertion was updated.

**Raj's live skill gate re-run — NOT RUN.** No live session or display was
opened or driven, including display `:0`. Nothing staged or committed.
Existing unrelated checkout changes were preserved.

## Fix round 4

Full live receipts diagnosed the create failure as dispatch-time provider
readiness: immediately after typing, the AT-SPI child is still re-walking the
dialog. The in-batch invoke never dispatches (`not_dispatched`, no attempts),
while the same invoke in a separate act after fresh observes works.

`thunar.create_folder` now uses two acts. Act 1 keeps the open-dialog and
replace-text steps, binding only `dialog` and `entry`. After both steps verify,
fresh session/window observes resolve the dialog handle (preferring
`state.modal`, otherwise a mapped create-dialog title), its `Create`
`push_button` ref, and the main window's current `tree_table` container ref.
Act 2 contains only the invoke, targeting `{window: dialog, ref: create_ref}`,
requiring that modal, and expecting both `dialog_absent` and `item_present`
with a 3500 ms timeout. The playbook example uses the same two-act shape and
explains why the provider needs the gap after typing. The rename batch remains
unchanged.

Only an act-2 step receipt with `dispatch_status: not_dispatched` permits one
retry, after another fresh target resolution. Accepted/unknown dispatch,
missing dispatch proof, and a second failure never replay the commit. Because
the adapter's normal projection omits dispatch status, the pack reads the
completed receipt using the original action ID/generation through read-only
`action.get`; all input still goes through `Adapter.call('act', ...)`.
Other outcomes retain the existing compact `_outcome` handling, and the
filesystem oracle runs only after both acts verify.

`_observe()` retains the last complete, revision-consistent page set, including
sets flagged dirty. It still seeks a clean set within five initial reads and
keeps the three-restart limit for dirty continuations. A terminal dirty page
can complete a fallback set; a dirty continuation with another cursor still
discards the partial set and restarts. Exhausting the bounds returns the last
complete set if available, otherwise `listing_unavailable` / `tree_dirty`.
Incomplete later attempts cannot overwrite a complete fallback. Settled,
resync, window, class, focus, modal and per-page revision checks remain in
force. Tree publication revisions are checked separately from session-state
revisions, since those can legitimately differ. Worker ref admission, batch
expectations and the filesystem oracle still verify mutations.

Headless pack tests: **48 passed**. Updated assertions cover the two-act
playbook and skill, fresh post-dialog refs/container/revision, title fallback,
one safe retry (including successful recovery), exhausted retry, no retry
after accepted/unknown/missing dispatch proof, unavailable receipt handling,
missing/unsettled post-dialog targets, last-complete dirty fallback, split-page
assembly, preservation across later incomplete attempts, and zero-complete-set
no-act exits. The worked-example regression in `test_worker_w5_fix2.py` also
uses two acts.

Full headless validation, with temporary paths in the checkout and bytecode
disabled:

```sh
TMPDIR="$PWD/wm/tests/.tmp" PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s wm/tests
```

**1047 tests in 133.289 s: 1046 passed, 1 skipped**, exit 0,
`OK (skipped=1)`. The existing sandbox-dependent ptrace/strace check remains
skipped. Log: `wm/tests/.tmp/packs-thunar-fix4-full-suite.log`.

**Raj's live skill gate re-run — NOT RUN.** No live session or display was
opened or driven, including display `:0`. Nothing staged or committed.
Existing unrelated checkout changes were preserved.

## Fix round 5

Raj's November 11 live gate confirmed that create-folder and its collision
short-circuit work, and rename's `not_found` returns real candidates. Rename
still failed its F2 selection precondition for pre-existing files, and create
could report `unknown` / `provider_degraded` after the folder was already on
disk when the post-commit tree verification degraded.

The rename batch now starts with step `0`: click the name-cell
`{window, ref}`, then verify `node_state` / `selected` with a 2000 ms timeout.
Steps `1`–`3` retain their existing F2, bound text and Return shapes, including
the F2 `selected_ref` guard. Verification requires all four IDs
`['0', '1', '2', '3']`. The playbook explicitly says a file must be selected
before F2 and that the skill clicks its name cell first.

Both skills now consult their filesystem oracle before returning an
unverified mutation-flow outcome. Confirmation returns
`{status: 'success', verified: True, detail: 'filesystem oracle confirms; batch outcome was <original status>'}`;
otherwise the original compact outcome is returned unchanged. This includes
create's first-act and post-dialog target-resolution failures as well as its
final commit outcome. Argument/listing failures, collisions, missing targets
and ambiguity still short-circuit before the mutation flow without consulting
the oracle. A verified batch retains the existing filesystem-verification
result. Create's commit retry rule is unchanged: one fresh-target retry only
with receipt proof of `not_dispatched`.

Headless pack tests: **53 passed**. Coverage checks an initially unselected
name cell, the exact click/selected predicate and unchanged remaining rename
steps, verification of step `0`, uncertain final outcomes for both skills
with confirming and non-confirming filesystem fixtures, default and explicit
oracle directories, preservation of original status/detail, rename's required
old-path absence, create's earlier mutation-flow exits, and pre-check exits
against directories that would falsely confirm success. Existing dispatch
retry, MCP and one-shot regressions remain covered.

Full headless validation, with temporary paths in the checkout and bytecode
disabled:

```sh
TMPDIR="$PWD/wm/tests/.tmp" PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s wm/tests
```

**1052 tests in 133.193 s: 1051 passed, 1 skipped**, exit 0,
`OK (skipped=1)`. The existing sandbox-dependent ptrace/strace check remains
skipped. Log: `wm/tests/.tmp/packs-thunar-fix5-full-suite.log`.

**Raj's live skill gate re-run — NOT RUN.** No live session or display was
opened or driven, including display `:0`. Nothing staged or committed.
Existing unrelated checkout changes were preserved.

## Fix round 6

Raj's November 11 live gate found that adding selection to the rename batch
made its step timeouts total 11,000 ms. The worker's 500 ms deadline allowance
then exceeds the 10,000 ms hard cap, so the adapter returns `invalid_params`
before any input dispatches.

Rename now verifies selection in its own single-step act: step `0` clicks the
name-cell `{window, ref}`, expects `node_state` / `selected`, and retains its
2000 ms timeout. A failed or incomplete selection receipt returns the compact
outcome through the existing uncertainty/oracle rule without issuing the
rename batch. After verified selection, a fresh listing resolves current refs
and repeats the old-name uniqueness and new-name absence checks. A vanished,
ambiguous or colliding target returns the corresponding short-circuit status.
Post-selection observe failures retain the existing uncertainty/oracle rule.

The rename batch contains exactly the proven steps `1`–`3`, built from that
fresh listing, with the original 2000/3500/3500 ms timeouts and selected-ref
guard. Verification requires `['1', '2', '3']` on that batch. Its timeout sum
is 9000 ms (9500 ms with the allowance); selection alone sums to 2000 ms.
Create-folder, the playbook, worker and adapter code are unchanged.

Headless pack tests: **57 passed**. Assertions cover the separate selection
and rename act sequence, exact step shapes and deadline sums, changed refs
and revision after selection, failed/incomplete selection without a rename
batch, refreshed not-found/ambiguity/collision checks, and oracle handling
after selection or post-selection observe failures. Existing final-outcome,
dirty/paged listing, dispatch retry, MCP and one-shot regressions remain
covered with the two-act rename flow.

Full headless validation, with temporary paths in the checkout and bytecode
disabled:

```sh
TMPDIR="$PWD/wm/tests/.tmp" PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s wm/tests
```

**1056 tests in 132.824 s: 1055 passed, 1 skipped**, exit 0,
`OK (skipped=1)`. The existing sandbox-dependent ptrace/strace check remains
skipped. Log: `wm/tests/.tmp/packs-thunar-fix6-full-suite.log`.

**Raj's live skill gate re-run — NOT RUN.** No live session or display was
opened or driven, including display `:0`. Nothing staged or committed.
Existing unrelated checkout changes were preserved.
