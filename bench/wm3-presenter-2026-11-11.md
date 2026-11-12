# WM-C — presenter overlay and human takeover (2026-11-11)

Built against `docs/WM-V1-ARCHITECTURE.md` and
`hidden_files/COUNCIL-WMV1-RECONCILED.md`. WM-A's read-only feed and WM-B's
durable genealogy/controller remain the sources of pixels and ancestry.
Promotion is outside this stage. No display, live X server, input device or
live gate was opened/run in the sandbox; in particular, display `:0` was
never opened. Nothing is staged or committed.

## Design as built

`wm/ark-present` is the entry point; `presenter.py`, `presenter_scene.py`
and `presenter_x11.py` separate RPC/event handling, pure scene logic and
host rendering. Python dependencies are stdlib and the worker's existing
python-xlib. Rendering uses the installed system `libX11`, `libXrender`
and `libxkbcommon` through ctypes. The explicit host display must be local
and nonzero; `:0`, screen variants and leading-zero variants are refused
before either host X connection opens. Session workers keep their own
session displays. The presenter creates no window inside a session.

The selected view root fills the host window. Its immediate children are
flat native session images overlaid inside that area. Exactly root +
children are live; deeper descendants are status chips, carrying attention
categories from the registry. Other roots remain reachable through chips.
Selecting a deeper chip re-roots the view; Parent returns up the genealogy.
Chips wrap and scroll, with an aggregate attention count in the status line.
Collapse removes only a preview: it sends no session lifecycle, device,
driver, budget or stop operation. Collapsed sessions still count toward
WM-B admission.

The renderer uploads WM-A's native RGB PNG into a host pixmap per live
surface. It does not encode a composite frame or recursively feed a
composed parent back into a worker. It caches an immutable scale/offset
transform on placement/viewport/native-size changes, then installs the
inverse scale in an XRender picture. Normal frame uploads reuse that
picture and transform. Compositing uses host XRender; there is no Python
per-frame image resampling. Cursor pictures get the same cached transform.
The inverse hit mapping uses the same surface bounds and native dimensions;
a drag clamps at its original session edge.

Selection is a pure `Scene` operation: it does not attach a driver, move a
session pointer, send a WM focus request or change native X focus. Native
clicks/keys occur only after the explicit **Takeover** control for the
selected surface. Drag a child title to move it; right-drag to resize.
Move/resize/collapse/re-root while human-held ends that takeover safely;
there is no silent continuation against a new layout.

WM-A gains optional `metadata:true` on `frames.get`. This includes current
cursor and settled native dialog rectangles on an `unchanged` reply without
creating another PNG. Callers omitting this option keep the original exact
unchanged-response shape. Cursor sensing is an optional, bounded XFixes
query on the worker's existing session X connection (10 Hz; at most one
outstanding query, image bounded to 128 × 128). ARGB pixels and hotspot are
served directly; a position crosshair is the fallback when image sensing
is unavailable. Driver PNG quotas and lease admission are untouched.

A parent's native modal/dialog region is repainted above child pixels.
While present, all child surface/title hit testing is disabled; any child
human takeover ends. Child chrome/cursors cannot cover the raised dialog.
Clearing the worker metadata restores hit testing. This is the reconciled
presenter overlay rule, with no parent-session slot window.

Registry/attention polling runs on a daemon thread, outside the input loop.
Frame queries go directly to each live worker's read-only socket. Neither
motion, keystrokes nor human heartbeats pass through the controller.

## Takeover state machine

`worker_human.Takeover` runs cooperatively on the worker's sole selector
thread, beside the existing ordered action engine. It uses the existing
control page, release machinery and session devices/grabs.

| State | Transition / authority |
| --- | --- |
| `idle` / `paused` | Explicit action plus a signed, unspent, current grant can begin. No event dispatch yet. |
| `closing` | Store QUIESCING first, bump epoch, poll the worker control change, revoke the old driver lease. Wait for queued/in-flight driver actions and self-tests to finish their preemption/cleanup. |
| `closing` → `human` | Run existing engine release; require no remaining driver holds. Release helper holds and independently confirm native key/button state. Check the control epoch still matches. Only then install the random human token hash and HUMAN state. Reply with the token after durable `human.ready`. |
| `human` | Admit only the bound stream, token, incarnation, generation, layout and next event sequence. Driver attach is refused. Human holds are tracked only in memory. |
| `releasing` | Close gate and bump epoch before draining a submitted helper exchange. Release known wire holds using `release_held`, including the existing supervised-helper recovery path when needed. XTEST cleanup is release-only. Confirm through a same-injection-connection barrier followed by session keymap/pointer sensing. |
| `paused` | Token is gone, gate closed, holds released. Disconnect, heartbeat expiry, controller death/expiry, global kill, focus loss, binding changes and delivery failure end here. Recover through another explicit Takeover, then Resume. Unconfirmed release keeps the gate closed and fences F1. |
| `resuming` | Explicit Resume first performs the same release. Mark the whole native framebuffer for a fresh capture and require its settled feed snapshot. Only then reopen AGENT with another epoch. |
| `idle` after resume | Fresh driver attach produces a new lease. Its first observe must request a full session sample (no `since_rev`, window scope, crop or ignore regions); automation stays unobserved until a healthy full settled observe. Old-lease pending observations cannot satisfy this. Existing effect reconciliation still applies. |

Control-page closing stores always precede epoch/hash changes. Opening checks
both the expected state and epoch. Takeover never carries the legacy
clear-fence flag into an opening transition. The fresh-frame resume check accepts newer settled captures when damage
arrives during the wait. The existing control CLI can
hold its lock while awaiting worker health: closing removes authority even
under that contention; opening fails `control_busy` instead of blocking the
worker's selector. No driver lease/fence bypass is introduced.

The helper's normal gate remains AGENT-only. The sole added input operation
is `op:"human"`, requiring HUMAN state, matching worker generation/epoch and
a constant-time match against the page's token hash. It rechecks this gate
and the exclusive grab before each native frame. The token is installed only
after release confirmation. Sequence/reply caching and helper incarnation
fencing remain in place; cached payload identity is a digest, not key data.
A helper restart is cleanup-only; a lost acknowledgement never licenses
replaying an effect.

The controller grant pins `/proc` PID start time, not just PID. The worker
checks it on each event and each loop tick; SIGKILL/zombie/PID reuse closes
human authority independently of controller RPC. Controller stalls leave the
direct stream usable. Presenter heartbeats are sent about every 200 ms;
one second without events/heartbeat expires human authority. Grants expire
after one hour. Presenter exit/focus loss closes the stream. `CU_KILL_FILE`
is checked by both presenter and worker, including resume; WM-B's existing
parent-first global kill/reap still operates independently. Releases bypass
the closed mutation gate, as in the existing worker.

Drags pin the target incarnation, generation, original layout/transform and
random drag id until the last pressed mouse button is released. Crossing a
child/parent/sibling/host boundary never changes the target. Layout or worker
generation changes reject the old binding and end input, releasing holds.
The presenter does not retry uncertain RPC events. In-flight driver effects
use the existing preemption receipts and pending-effect reconciliation:
partial/unknown effects are recorded, never replayed on resume.

## Wire shapes

All sockets use the existing four-byte unsigned big-endian length followed
by UTF-8 JSON-RPC 2.0. Worker request cap stays 256 KiB; controller cap stays
64 KiB. Worker `act.sock` keeps its owner-only permissions and SO_PEERCRED
check. Human calls are lease-free on a **persistent, separate presenter
connection**, not the driver's leased stream. End closes that connection.
The account UID remains the existing administrative trust boundary.

The following are JSON-RPC `params`/`result` objects, not complete envelopes.
`I` denotes a durable registry incarnation; `G` a worker generation.

Controller `takeover.issue` (only on explicit presenter action):

```json
{"incarnation":"I","generation":"G","layout_version":12,"explicit_action":true}
```

```json
{"ok":true,"socket":"STATE/run/sN/INSTANCE/driver/act.sock",
 "grant":{"incarnation":"I","generation":"G","layout_version":12,
          "nonce":"RANDOM","expires":1794459600.0,
          "controller_pid":1234,"controller_start":"PROC_START_TICKS"},
 "signature":"HMAC_SHA256"}
```

The HMAC uses the existing owner-only `wmctl-key.json`, with a separate
`human:` domain. It is scoped to incarnation, generation, layout, nonce,
expiry and controller process identity. Delegate tokens cannot authorize
human input. Nonces are one-use per worker generation (bounded to 4096
grants; exhaustion fails closed). Issuance journals identity/layout only,
not the grant, signature or input token.

Direct worker `human.begin`:

```json
{"grant":{"incarnation":"I","generation":"G","layout_version":12,
          "nonce":"RANDOM","expires":1794459600.0,
          "controller_pid":1234,"controller_start":"PROC_START_TICKS"},
 "signature":"HMAC_SHA256","explicit_action":true}
```

```json
{"state":"human","token":"RANDOM_256_BITS","generation":"G",
 "layout_version":12,"control_epoch":8}
```

Every `human.event` carries the fixed binding, token and next sequence:

```json
{"incarnation":"I","generation":"G","layout_version":12,
 "token":"RANDOM_256_BITS","seq":1,"kind":"motion","x":300,"y":200,
 "drag_id":"D"}
```

`kind` is `motion`, `key_down`, `key_up`, `button_down` or `button_up`.
Motion coordinates are native session pixels; key `detail` is an XKB keysym
name mapped using the target session's keyboard map; button `detail` is 1–7.
The same `drag_id` accompanies a held drag's moves, keys and button releases.
An event success returns `{state:"human",seq:1,dispatched:true}`. This means
input submission, not verification of an application effect.

`human.heartbeat` carries incarnation/generation/layout/token and returns
state. `human.layout` carries incarnation/generation/token and a strictly
newer layout version; an active hold ends takeover. The stock presenter
ends input on every layout change, even without a hold. `human.end` carries
the identity/token and `resume:true|false`; it permits stale layout cleanup
so a release is never remapped into a new target. Successful end returns
state plus `fresh_attach_required:true, full_observe_required:true`.

Rejections use the existing JSON-RPC error `data.reason`, including
`explicit_action_required`, `invalid_capability`, `stale_capability`,
`spent_capability`, `stale_incarnation`, `stale_generation`, `stale_layout`,
`stale_sequence`, `drag_target_changed`, `control_changed`, `control_busy`,
`human_delivery_unknown`, `release_unconfirmed` and `fresh_frame_required`.
A rejected native event is never retargeted. Lost/uncertain delivery ends
human authority and releases holds.

Helper transport (worker-only; one short native frame per request):

```json
{"seq":31,"instance":"HELPER_INSTANCE","op":"human",
 "generation":"G","human_epoch":8,"human_token":"RANDOM_256_BITS",
 "dev":"ptr","frames":[[[2,0,20]]],"gap_us":0}
```

Human journal rows contain transition metadata or aggregates:

```json
{"type":"human.events","session":"sN","generation":"G",
 "ts":"UTC","count":1,"kinds":{"key_down":1}}
```

The normal journal also adds its session instance id. No key names, keycodes,
text, drag payloads, input tokens, wire holds or human event arrays enter the
human journal. Human input does not call `Engine.send`/`durable_cleanup`,
which would journal its event payload. All human cleanup and helper recovery
keeps those holds in memory. Driver action journaling remains unchanged.

## Headless evidence

From the repository root, without opening a display:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 wm/tests/run_wm_c_headless.py
PYTHONDONTWRITEBYTECODE=1 python3 wm/tests/run_wm_c_headless.py full
```

The runner confines temporary files under `wm/tests/.tmp`. Targeted log:
`wm/tests/.tmp/wm-c-targeted.log`. Full log:
`wm/tests/.tmp/wm-c-final-suite.log`.

Targeted: **41 tests passed in 0.289 s**. Final full suite: **1,167 tests in
132.277 s; 1,166 passed, one existing environment-dependent skip**, exit 0,
`OK (skipped=1)`. The WM-A feed regression suite also passed all **18 tests**.
The presenter entry point's `--help` check passed without opening X.

The tests use real control mappings/stores, fsynced journals, the existing
worker/engine, the helper protocol, and independent fake native input
recorders. They cover gate → epoch → revoke → confirmed release ordering;
in-flight preemption and stale-lease dispatch refusal; stale layout,
generation, incarnation and sequence rejection; drag pinning and multiple
buttons; global kill; real process SIGKILL during a drag; disconnect/timeout;
release confirmation failure and helper replacement cleanup; fresh capture,
new attach and full observe on resume; old-lease observe rejection; unchanged
fencing; journal privacy; transform reuse, mapping, modal hit exclusion,
collapse, depth/chips, cursor image metadata, optional cursor sensing under a
full driver query budget, and host display refusal.
Headless evidence does not qualify rendering or physical device delivery.

## Raj's live checks — NOT RUN in the sandbox

Use the real shell on the x86 Debian generic VM. Record the host image,
font/Xorg/XRender/XKB versions, profile, CPU/RSS, session count, display
allocation, registry/control snapshots and relevant receipt timestamps.
Use disposable sessions, default admission 2, a separate virtual host display
outside the allocator (for example `:121`), and session displays :91–:120.
Never open or drive `:0`. Start the parent/child workers and controller by
the already-qualified WM-A/WM-B procedures, then launch:

```sh
wm/ark-present --host-display :121
```

Use `--state PATH` on both controller and presenter if testing a dedicated
registry. The host X server must offer depth-24 RGB/LSB pixels and XRender;
ARGB cursor pixmaps use its depth-32 render format. Use the host's appropriate
X authorization, not an inherited session display. Do not run live promotion.

1. **Presenter and isolation.** Show a native parent and its independent
   child. Confirm no new window appears in either session's native window
   enumeration or focus history when the presenter opens, selects, moves,
   resizes, collapses or expands a surface. The child continues its task
   while collapsed; budgets/admission stay unchanged. A grandchild appears
   as an attention chip and becomes live only after re-rooting. Report/clear
   a blocked/passcode category while collapsed and require the flag to update.
   Exercise Parent, chip scrolling and another root. Raise a native parent
   modal over the child area: its dialog pixels must remain above child
   content/chrome; child hits and child takeover must stop until it clears.
2. **Takeover latency p95 (required live gate).** Measure from explicit
   Takeover action/request to the ready reply after confirmed release, and
   separately first human native-event delivery. Collect at least 100 samples
   for N=1 and the parent+child N=2 case, including takeover during active
   driver key/button holds and paced batches. Save raw monotonic timings and
   p50/p95/max, CPU/RSS and frame age; compare against the agreed VM latency
   budget (no numeric live target is invented by this sandbox report).
   Require gate-close → epoch → lease revoke → release confirmation before
   any human press, and no further agent press/motion after gate close.
   Continue the WM-A quota/observe-latency checks beside the acting driver.
   N=3/4 admission is raised only after the council's measurement approval.
3. **Mapping accuracy after transforms (required live gate).** With an
   instrumented native canvas in each session, exercise a grid including
   corners/edges/center at original size, several shrink/enlarge ratios,
   offsets, letterboxing and repeated moves/resizes. Compare aimed inverse
   mapping to delivered native coordinates; record maximum and percentile
   error (expect at most integer pixel rounding). Repeat the evdev profile
   and check actual pointer acceleration/relative-motion behavior. Require
   zero host/sibling deliveries. Drag from child across parent/sibling/host
   boundaries and release outside: the whole lifetime stays on the child,
   including multiple held buttons. Resize or restart the target worker
   while a drag is held: reject the old binding and release, never retarget.
   Replay deliberately old layout/generation/sequence messages from a test
   presenter connection and require named refusal with zero native writes.
4. **Controller killed mid-drag (required live gate).** Hold a child button
   and modifier, move, then SIGKILL the controller and leave it dead. The
   worker's PID/start-time check must close the human gate and release both
   holds without controller help. Query native keys/button masks and helper
   holds; require nothing stuck down and no more native events from later
   motion. Repeat with SIGSTOP rather than SIGKILL: direct human events must
   continue while the controller is merely stalled; Resume must not require
   a controller event relay. Separately kill the presenter, lose its host
   focus, stop heartbeats and restart/kill the helper during a hold. Require
   release-only cleanup or a closed F1 fence on unconfirmed release, with no
   prefix replay. Verify sessiond still reaps the tree with controller dead.
5. **Resume and privacy.** Interrupt a driver action after its first native
   event, take over, then Resume. Require a new native full-frame capture
   before AGENT admission, a different lease, and full settled session
   observation before any act. Try a delta/window/cropped first observe and
   the old lease: require refusal. Inspect interrupted receipts/reconciliation
   for partial/unknown effects and require no replay. Type only a synthetic
   nonsecret canary via the human fixture; inspect controller, worker and
   helper logs/journals for its absence and check `human.events` contains only
   counts/kinds plus identity/timestamp fields. Real passcodes remain the
   human's and must not be used as test evidence.
6. **Global kill during takeover and resume.** On a dedicated disposable
   tree, create its configured `CU_KILL_FILE` during a drag; repeat during
   the fresh-frame resume wait. Require worker/presenter takeover termination,
   gate closure, confirmed release and the WM-B parent-first gate/child-first
   reap fan-out. Leave the controller dead for an independent worker check.
   Remove the kill file deliberately only after verifying stopped state.
   No input may resume automatically or replay afterward.

Append VM live results and raw evidence separately. Latency, mapping and
controller-kill delivery gates remain unqualified until Raj runs them.

## Fix round 1

Raj's live startup gate on ArK2 (Xvfb `:77`) found that the presenter
immediately exited with `{"ok": false, "error": ...}`: the em dash in
`set_wm_name` could not be encoded by python-xlib's latin-1 `WM_NAME` path.
The window title now uses the ASCII string `ArkDrive - session presenter`.
The presenter path has no other string-valued X property setters:
`set_wm_protocols` receives atom IDs, and no `_NET_WM_NAME` is set.

Added `PresenterCase.test_startup_wm_name_is_latin1_encodable`, which runs
the real presenter startup with both X connections mocked and checks every
title passed to `set_wm_name` by encoding it as latin-1. It uses the actual
title from `Presenter.run`, without copying that value into the test. The
test reproduced the reported `UnicodeEncodeError` before the title fix.

Validation from the repository root, without opening a display:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 wm/tests/run_wm_c_headless.py
PYTHONDONTWRITEBYTECODE=1 python3 wm/tests/run_wm_c_headless.py full
```

Targeted: **42 tests passed in 0.298 s**. Full suite: **1,168 tests in
132.407 s; 1,167 passed, one existing environment-dependent skip**, exit 0,
`OK (skipped=1)`. Logs: `wm/tests/.tmp/wm-c-fix1-before.log`,
`wm/tests/.tmp/wm-c-fix1-targeted.log` and
`wm/tests/.tmp/wm-c-fix1-full.log`.

Live gates remain Raj's; none were run here. No display (including `:0`)
was opened. Nothing was staged or committed, and all writes stayed within
the repository.
