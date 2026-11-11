# Packs prerequisite: four adapter fixes — 2026-11-11

This build fixes the adapter prerequisites only. Packs are the next build.
**Live gates NOT RUN**: no session, desktop, display `:0`, provider API or
input device was opened/driven. Nothing staged or committed. Changes are
confined to `wm/` and this note; existing unrelated work is preserved.

`hidden_files/COUNCIL-AGENTS-RECONCILED.md` was absent from this checkout
(also not found elsewhere under `/home/ark`). Implementation follows the
four explicit requirements in the build prompt, with the existing council
reviews consulted for context. The missing plan was requested from Raj.

## Changes

1. **Adapter ready gate.** `worker_adapter.py` now uses the same full,
   settled, session-scope, no-resync rule as `Engine.observed`. Window/family
   full observes still advance the model's delivered revision but cannot
   set adapter ready. The private pre-act session/state handshake validates
   its result too; an unsettled handshake returns `observe_required`
   before reserving an action ID or sending input. It does not advance
   the model's revision behind its back.

2. **Tree delivery follows family content.** `worker_tree.Provider.target`
   re-resolves the mapped focused member of the requested family on every
   call. A focused dialog supplies its own tree, and closing/replacing it
   resolves the current member again. A tombstoned anchor can still identify
   a surviving family through existing state selection. Focus outside the
   family falls back to the anchor. Explicit `window` scopes retain their
   named target, including intentional unfocused-window reads; session
   scopes continue to follow focus. State selection, lease/act rules and
   worker request/response shapes are unchanged. A continuation should keep
   its original content window: use explicit window scope when continuing
   a page across a family focus change; existing cursor/window validation
   still rejects mismatched continuations.

3. **Observe paging surface.** MCP `observe` now advertises optional
   `budget: {nodes, bytes}` and opaque `cursor`. Both MCP and one-shot
   `ark-act call observe '<JSON>'` use the same pass-through path, existing
   worker validation, node/byte caps and snapshot continuations. Rendering
   preserves the worker-limited page, truncation and cursor metadata; it
   does not re-expand the tree. No new worker wire fields were introduced.

4. **Loop context accounting.** A nonzero `CU_LOOP_TOKEN_BUDGET` is a total
   estimated context ceiling, including an explicit **1,024-token completion
   reserve**. The actual request's `max_tokens` (and llama `n_predict`) matches
   that reserve. With budgeting disabled (`0`), the existing 4,096 completion
   cap is retained. Older act/wait results compact first, preserving the last
   two result envelopes; if still oversized, observes compact through the
   latest one, retaining revision stubs and removing images. Calls/results
   stay paired, and adapter identity/recovery state remains independent.
   An irreducible request stops with `token_budget_exceeded` before provider
   dispatch. Summaries include `completion_reserve_tokens` and
   `peak_estimated_prompt_tokens`: the maximum post-compaction prompt
   estimate actually sent, zero if none was sent. The estimate remains
   `ceil((history JSON characters + tools JSON characters) / 3)`, including
   tool definitions. It is an estimate of canonical input, not provider
   tokenization, vision token accounting or measured API usage. The guard
   guarantees its estimated ceiling; Raj must compare it with real usage.

## Headless validation

`wm/tests/test_worker_w_packs_adapter.py` adds ten regression tests covering
session-only readiness, private handshake acceptance/rejection, family dialog
focus and close/replacement, removed anchors, explicit unfocused targets,
unrelated-family focus, real worker tree paging through MCP and one-shot
adapters, model-visible node/byte bounds and full continuation, latest-observe
stubbing, completion reserve/request cap agreement, peak prompt reporting,
disabled budgets and refusal of irreducible requests.

Existing W5 history tests were updated where they explicitly required the
old behavior (protecting every observe and dispatching an impossible budget).
They still check compaction order, frame counts, tool pairing and independent
adapter revision/action-ID bookkeeping with feasible budgets.

Targeted adapter/loop run: **73 passed**, 0.766 s. Full headless suite:
**992 tests in 129.473 s; 991 passed, 1 skipped**, exit 0,
`OK (skipped=1)`. This is the checkout's 982-test baseline plus ten new
regressions; the existing ptrace-dependent OCR strace check remains skipped.
The full runner discovers only
`test_worker_w*.py`, disables bytecode and redirects temporary directories
into `wm/tests/.tmp`, including the W2 fixture's repo-root temp request:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 wm/tests/.tmp/memory-step5-fix1-run.py full
```

Full log: `wm/tests/.tmp/packs-adapter-full-suite.log`.

## Raj's live checks — NOT RUN here

- **Items 1, 3, 4: run `ark-loop` against a dedicated live session on display
  91–120.** Begin with a full scoped observe, then act; verify that the
  adapter obtains the session handshake and no repeated
  `precondition_failed`/`retry_ok` cycle occurs. Also reconnect/use separate
  one-shot calls and exercise an unsettled observe; the worker's authority
  and stale-revision guards must still hold.
- In that run request a deliberately small tree budget, page the returned
  cursor to completion, and check all expected controls appear exactly once
  without a larger rendered page. Repeat through MCP and `ark-act call`.
  Exercise a large folder/tree and a low feasible total loop budget: inspect
  the latest observe's revision stub, reserve/request cap agreement and
  reported peak. Compare the character estimate with provider prompt-token
  usage and the model's actual context limit; record turns, tool calls,
  cumulative usage and wall time separately. Try an impossible budget and
  verify zero over-budget provider calls and `token_budget_exceeded`.
- **Item 2: rerun the W4 D1 create/rename harness.** Keep the parent family
  scope while a dialog takes focus; the returned tree must name that dialog
  and expose its entry/buttons. Close/reopen/replace dialogs, then verify
  delivery returns to the current content window. Explicitly read an
  unfocused window and an unrelated focused family to check anchor behavior.
  Preserve the D1 filesystem oracle, 3/3 verified steps and 30/30-run gate,
  plus existing isolation observers. No headless result establishes D1.

## Fix round 1

Fixed the step-5 session layout regression in `wm/worker_adapter.py` only.
`session_dir(session)` still validates `s[1-9][0-9]*` and resolves the account
home through `pwd.getpwuid(os.getuid())`, never inherited `HOME`. It now reads
that account's `.local/state/ark-sessiond/sessions.json` and uses the matching
session record's direct `session_dir`, including `run/<sid>/<instance>/`.
Missing/unreadable/malformed registries, missing records or fields, and unusable
field values retain the legacy `run/<session>` fallback for fabricated layouts.
Sessiond, the worker and the registry format are unchanged.

Audited every adapter directory use: MCP and one-shot both receive the resolved
directory; `driver/act.sock`, `driver/adapter-state.lock` and
`driver/adapter-state.json` derive from it. Inline frames read the worker-returned
paths directly, with no separate legacy directory construction.

Seven new headless regressions in `wm/tests/test_worker_w_packs_adapter.py` cover
instance directory/socket resolution despite inherited `HOME`, absent registry,
absent record/field, unreadable/malformed registry and invalid field types,
unchanged session-ID rejection, and both one-shot and MCP observe/act entry
points using a fake worker. Entry-point checks include instance socket selection,
inline frame reads and one-shot state/lock persistence in the instance directory.
Targeted pack-adapter run: **17 tests passed**, exit 0.

Full headless suite: **999 tests in 134.909 s; 998 passed, 1 skipped**, exit 0,
`OK (skipped=1)`. The existing sandbox-dependent ptrace/strace check remains
skipped. Ran the full default discovery with all temporary files kept in the
checkout and bytecode generation disabled:

```sh
TMPDIR="$PWD/wm/tests/.tmp" PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s wm/tests
```

Full log: `wm/tests/.tmp/packs-adapter-fix1-full-suite.log`.

**Live re-check — NOT RUN.** Raj reruns the one-shot observe/budget/cursor/act
smoke and D1 live gate. No live session, provider API or display (including `:0`)
was opened or driven. Nothing staged or committed.
