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

## Fix round 1

Raj's ArK2 live gate found that GNOME Text Editor 50.1 exposes WM_CLASS
`gnome-text-editor`, while its recipe used the desktop-file id
`org.gnome.TextEditor`. Recipe selection accepted the latter but source
lookup required that same string as the native class; the real class had
no recipe. The original headless fixture repeated the incorrect id and
hid the mismatch.

The chosen contract is exact native WM_CLASS throughout `app_class`:
`gnome-text-editor` for GNOME Text Editor and `Thunar` for Thunar. The editor
recipe key now matches the live class. No desktop-file-id aliases or class
normalization are accepted: `org.gnome.TextEditor` returns `no_recipe`
before creating any intent, gate, checkpoint, or reservation. Source
lookup, manifest identity, destination verification, and retirement all
use the same native class. Both recipes' `verify()` methods were audited:
each requires exact class equality plus visible document evidence. Thunar's
recipe and behavior are unchanged. The WM README documents this contract.

Stage-D source fixtures now use `gnome-text-editor`; reopened fixtures
derive the native class from the launched executable independently of the
manifest's `app_class`. The existing end-to-end success test checks the
native class in the checkpoint. Two added regression tests cover
desktop-file-id refusal without traces and both recipes' exact-class and
document verification. Before the recipe fix, the native-class success
test failed with `no_recipe`, and the desktop-file-id refusal test received
`source_window_missing` instead of `no_recipe`.

Validation used the following environment for both discovery commands:

```sh
TMPDIR=/home/ark/arkdrive/wm/tests/.tmp \
PYTHONPATH=/home/ark/arkdrive/wm \
PYTHONDONTWRITEBYTECODE=1 DISPLAY= WAYLAND_DISPLAY= \
python3 -m unittest discover -s wm/tests
```

The full WM suite ran **1,234 tests in 135.635 seconds: OK (skipped=1)**,
with no failures or errors. Stage D separately ran **66 tests in 2.585
seconds: OK**, using the same command with
`-p test_worker_wm_d_promotion.py`. Scratch stayed within the repository.
No commit or staging was performed, and no display :0 was opened. Live
gates remain Raj's; this round provides headless validation only.

## Fix round 2

Raj's next ArK2 live gate reported that Thunar promotion passes end to end,
but Text Editor export fails with `document_path_unavailable`. GNOME Text
Editor 50.1 titles saved files as `note.txt (~/docs) - Text Editor`; the
recipe only extracted whole path parts from em/en-dash titles and could
not corroborate the name before the parenthesized directory. A newly
opened editor commonly has only an unmatched `rendered_only` tree stub,
so tree content cannot supply the missing path.

The shared recipe code now recognizes the parenthesized directory form
with an absolute or tilde-abbreviated directory and joins it with the
filename. Visibility corroboration checks both the filename and the
directory. Tilde expansion uses the owning worker's HOME for source export
and destination verification; the recipe verification interface accepts
that HOME explicitly. Existing absolute-path, file-URI, and em/en-dash
forms remain supported. The unique-candidate, regular-file, beneath-HOME,
no-`..`, and window-visibility guards remain in place.

The default source fixture now uses native class `gnome-text-editor`,
title `note.txt (~/docs) - Text Editor`, and the unmatched frame stub from
the real observer (`showing=false`, `visible=false`, `status=unmatched`,
`rendered_only=true`). The reopened editor also uses a parenthesized
tilde-directory title and an unmatched stub, exercising destination
verification and recovery without accessible content. A full-tree
promotion variant remains covered. Added regression cases cover legacy
title forms, absolute directories, missing or mismatched names/directories,
conflicting tree paths, multiple candidates, missing files, directories,
path traversal, and paths outside HOME. Unsaved detection retains all
existing indicators and now includes an `Unsaved Document - Text Editor`
fixture, refusing it before an intent, gate, checkpoint, or reservation.

Before the recipe fix, the updated end-to-end success fixture reproduced
`document_path_unavailable` at preflight. After the fix, the Stage-D module
passed **71 tests in 2.481 seconds: OK**. Validation uses the same environment
and discovery commands as Fix round 1, with all temporary files under
`wm/tests/.tmp` and both display environment variables cleared.
The full WM suite passed **1,239 tests in 134.981 seconds:
OK (skipped=1)**, with no failures or errors.

No commit or staging was performed, no files were written outside
`~/arkdrive`, and no display :0 was opened. This round's validation is
headless; live gates remain Raj's.

## Fix round 3

Raj's ArK2 gate found that selected clipboard transfer waited for
`xclip -selection clipboard -in` to exit. A selection holder must remain
alive to serve its payload; the blocking subprocess call consequently
timed out even after installation. The earlier in-memory clipboard fixture
returned immediately and could not expose this process-lifetime defect.

Clipboard installation now uses a short-lived launcher which forks a
holder into its own POSIX session and exits. The controller waits only for
the launcher. The detached holder inherits the destination's DISPLAY,
HOME, runtime/bus environment, and exact sessiond/incarnation markers, so
ordinary destination teardown finds and reaps it. Its input is a bounded,
immediately unlinked temporary file inside the destination session; its
output goes to the destination promotion log. No controller pipe or
payload in argv/logs is retained. `xclip -quiet -loops 0` keeps the holder
in the foreground of its own session and serves unlimited selection
requests. There is no timer or read-count lifetime bound: it ends when
selection ownership is replaced, the destination X server exits, or
sessiond tears down the destination. Verification reads do not consume it.

Both initial installation and recovery of an armed clipboard step now
require destination `xclip -out` read-back equal to the checkpoint payload,
with bounded retries while ownership is being acquired. Launcher failures
report `clipboard_set_failed`; unverifiable destination state reports
`clipboard_uncertain`. Read-command timeouts report clipboard-specific
errors rather than raw subprocess exception text. A failed transfer is
recorded before normal child teardown and source gate release; it cannot
reach reopen or source retirement. Recovery reads native state without
installing another holder.

The added executable xclip fixture never opens X. Its `-in` process stores
the payload and remains alive indefinitely; `-out` requires a live owner
and reads without consuming ownership. Before the fix, the transfer test
reproduced the reported raw five-second subprocess timeout. The success
regression now promotes a near-quota Unicode payload through an actual
controller subprocess that exits before read-back, checks destination
environment and sessiond ownership, and reads the same payload repeatedly
before and after controller reconstruction. Further cases cover mismatched
read-back, a non-returning reader, and launcher timeout, including source
preservation, no reopen/retirement, child cleanup, and step-specific reasons.
The process fixture adopts and reaps its detached holders; native X
socket/lock cleanup is disabled because no X server was created.

Stage D passed **75 tests in 20.991 seconds: OK** using the same environment
and discovery commands as Fix round 1. The full WM suite passed **1,243
tests in 153.585 seconds: OK (skipped=1)**, with no failures or errors.
Logs are `wm/tests/.tmp/wm-d-fix3-targeted.log` and
`wm/tests/.tmp/wm-d-fix3-full-suite.log`. The full run emitted existing
resource-cleanup warnings from W2 fixtures.

No commit or staging was performed, no files were written outside
`~/arkdrive`, and no display :0 was opened. Validation is headless;
live gates remain Raj's.

## Raj verifier addendum (Nov 11, 2026)

Suite: 1,243 tests OK verified across the build and three fix
rounds (commits 83dd249, 7e0d260, 0b754aa, 39c0026). Runtime
dependencies the live gates needed on the host: wmctrl and
xclip (xclip was NOT installed on ArK2 before the gate; both
belong on the Phase 5 image list).

Live gate (~/wm_d_gate.py, real GNOME Text Editor 50.1 + Thunar
on the ArK2 stack): **PASS 19/19**. Editor saved-file promotion
end to end: child spawned with promotion lineage, checkpoint
manifest written (no clipboard payload when unselected),
document installed under the child's Promotions/<id>/documents
with identical content, editor reopened in the child naming the
file, source window retired only after verification, source
session alive with its other app intact; retry with the same
request id returns the same promotion and child. Thunar
directory promotion likewise (reopened at the directory, source
retired). A new unsaved document ("New Document (Draft) - Text
Editor") is refused unsaved_state with no checkpoint or session
created; an unknown class is refused no_recipe. With
transfer_clipboard the payload set in the source reads back
byte-exact in the child.

The three fix rounds were all live-only defects of one family —
the code was written against assumed app behavior the fixtures
then repeated: (1) the editor recipe keyed on the desktop-file
id org.gnome.TextEditor while the real WM_CLASS is
gnome-text-editor, so it could never match (app_class is now the
exact native WM_CLASS for both recipes); (2) saved_path and
visible assumed an em-dash title, while the real title is
"note.txt (~/docs) - Text Editor", and promotion.observe often
returns an unmatched tree stub, so the recipe now works from
the title alone; (3) the clipboard step waited for xclip -in to
exit, which by design never happens while it holds the
selection — the transfer now installs a detached holder in the
destination and verifies by read-back, and the fake xclip
models the non-returning form.

## VM verification (x86 Debian generic VM, Nov 11, 2026)

Live gate in the guest: **PASS 19/19** (xclip and wmctrl
installed in the guest first — both are runtime deps). Suite in
the guest: 1,243 run; the only failures are the 9 known
test_worker_w7_corpus font-calibration cases, plus one apparent
extra failure that was an invocation artifact — the stage-D
clipboard test spawns a subprocess importing wm modules and
needs the documented PYTHONPATH=<repo>/wm; with it, the test
passes in the guest.
