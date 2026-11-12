# WM v1 stage D — promotion — 2026-11-11

Implemented against the stage B controller and the locked architecture in
`docs/WM-V1-ARCHITECTURE.md` and `hidden_files/COUNCIL-WMV1-RECONCILED.md`.
No live promotion, memory migration, undo, selection, or login transfer.

`promote` is a synchronous controller mutation with the usual request id
and source-scoped capability. Required parameters are `source_incarnation`,
`window` (worker handle), and `app_class`; optional `transfer_clipboard`
defaults to false and optional `reservation` uses the ordinary spawn rules.
It returns a stable promotion id, completion/failure, and on success the
child incarnation and checkpoint path. Client timeout means retry the same
identity, never start another operation.

Each stage has an fsynced armed/done record: source app-family input gate,
settled observe, export, normal spawn, destination worker startup, explicit
document copy, selected clipboard, reopen, verify, source window retirement,
and gate release. The source session and other apps remain alive; worker
action tests demonstrate that another app can still receive input. Human
takeover is closed during the transaction. No driver lease is acquired by
the controller. `promotion.observe` provides read-only settled native
worker metadata; it does not allocate frames or attach a driver.

The exchange checkpoint is `STATE/exchange/promotions/ID/manifest.json`
plus `documents/`. The manifest includes version, promotion id, source
incarnation/window identity, app class, fixed recipe launch argv, cwd,
relative document paths, and a hash inventory. Clipboard payload is absent
unless selected. Copies refuse links/special files/path escapes and obey
stage B's artifact count/byte limits. Documents are explicitly installed in
`CHILD_HOME/Promotions/ID/` and checked against the snapshot before native
window/title/tree verification. Source retirement occurs only after that
verification, with another verification immediately before retirement.
The checkpoint remains after either outcome.

Recipes are separate deterministic modules for GNOME Text Editor
(`org.gnome.TextEditor`) and Thunar (`Thunar`). Paths must be unambiguous,
exposed by title/tree, corroborated by title, and beneath the source HOME.
Unsaved indicators cause `unsaved_state` before any intent, gate, bundle,
or reservation; unsupported classes cause `no_recipe`. Both recipes export
saved filesystem content without stopping the source app. Consequently
export/spawn/reopen/verify failure preserves the running source directly;
there is no stopped-profile recipe in this stage. Thunar transfers a
bounded directory snapshot. Runtime retire uses `wmctrl`; explicit
clipboard transfer uses `xclip`. New modules use only Python's stdlib.
All desktop subprocesses refuse displays outside :91–:120.

Recovery reuses a completed bundle and never exports over it. A partially
exported/uncertain bundle fails without another export. Normal spawn's
request identity preserves genealogy, admission/depth/fan-out checks and
one budget reservation. Child records include promotion lineage. Uncertain
worker startup, clipboard, reopen, and close are reconciled from native
state; they are not resent. Failed children are reaped by incarnation
through sessiond, including a child launched before the controller died.
Cleanup itself is recoverable. Retirement committed before a crash is
finalized without another close or reaping the verified child. Changed
source worker identities are refused rather than treating a stale handle
as proof of retirement. If retirement cannot be observed, both sides and
the gate remain pending for reconciliation.

Headless validation: `python3 -m unittest discover -s wm/tests` ran **1,232
tests in 138.624 seconds: OK (skipped=1), no failures or errors**. The run
used `TMPDIR=/home/ark/arkdrive/wm/tests/.tmp`. The stage D module separately
passed all **64 tests**, including crash boundaries,
immutable bundles, refusal without traces, failure cleanup, clipboard
selection/reconciliation, caps, source-family gating, identity changes,
filesystem oracles, and restart without duplicate reopen/retire. Fixtures
use real journals/registry/control pages and fake app/clipboard/window
state; they never open an X display. Scratch for the full run is routed to
`wm/tests/.tmp`. No commit or staging was performed.

Raj's Debian VM live gates:

| Gate | Status |
| --- | --- |
| GNOME Text Editor saved-file promotion and forced destination failure | NOT RUN |
| Thunar directory promotion and forced destination failure | NOT RUN |
| Source checkpoint restore, including forced destination failure | NOT RUN |
| Controller kill/restart during native export/reopen/retire; no repeated external actions | NOT RUN |
| Selected versus unselected native clipboard transfer | NOT RUN |

No display :0 was opened or driven. Live gates remain for Raj on the
Debian VM; headless success is not evidence that these live gates passed.
