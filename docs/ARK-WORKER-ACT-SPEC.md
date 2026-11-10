# ArkDrive per session worker and act path

Status: implementation design for the component after acceptance-passing `ark-sessiond v0`. V2 is authoritative, including its final continuous-work directive; its older proposed-revision header does not reopen the user's approval. This specifies software and acceptance gates, not a new calendar.

Build a deterministic `ark-worker` per session, with persistent sensing and input connections, and the same driver adapter for Codex and API models. The worker performs no model calls. A driver supplies intent and explicit postconditions; the worker resolves targets, dispatches, evaluates predicates, and returns a receipt plus the resulting observation delta. Keep one ordered mutation queue per session.

## Evidence and number convention

**M means measured; T means target. Every new limit, default, count, timeout, threshold, protocol constant and example value below is T unless explicitly marked M.** Section numbers, source filenames, API/library names, kernel event codes and schema/version identifiers are identifiers, not performance measurements. Example handles, revisions and receipt timings are illustrative T values. An acceptance count is a T gate, never a completed test result.

Sources supplied with this request:

- **[V2]** `AGENTIC-WM-PLAN-V2.md`, especially §§3.1–3.12, §4, §§5.2–5.7 and the final schedule directive.
- **[S]** `SPEC.md`, the session manager implementation specification. Its setup waits and polling limits are recipes, not measured worker performance.
- **[R]** `wm-sessiond-v0-2026-11-09.md`, independent shell acceptance results on ArK2, November 9, 2026, evening PT.

| Established fact | Evidence and scope |
|---|---|
| Session lifecycle, private HOME/buses, device attachment, host exclusion and teardown passed | M: [R], Acceptance test and ground-truth recheck. This proves no worker input delivery or application-effect verification yet. |
| Xvfb cold start | M: total 610–624 ms; display 50 ms, bus 2–3 ms, accessibility 519–567 ms [R, xvfb start timing]. |
| Xorg-dummy cold start | M: total 1798–1819 ms; devices 1145 ms including the specified settle wait, Xorg 127–147 ms, accessibility approximately 519 ms [R, xorg start]. |
| Separate accessibility buses | M: Spike A PASS 15/15 as reported by [V2 §0.1]; [R] confirms GetAddress discovery and isolated session operation. Individual spike checks are not attached. |
| Explicit evdev routing | M: Spike B PASS 23/23 with host exclusion, as reported by [V2 §0.1]. [R] separately verifies nodes, attachment, enablement, host exclusion and teardown. |
| GPU side channel | M: headless EGL 8.5×, Cycles blocked on VRAM [V2 §0.1, reporting the spike]. No per-session worker or new GPU measurement is claimed. |
| Earlier sensing costs | M: AT-SPI snapshot 130 ms; region hash 1.6 ms; OCR 170 ms versus vision 5300 ms on the tested crop only [V2 §0.1]. These are V2-carried measurements, not measurements in [R]. |

All worker latency targets remain unmeasured. Report model time, application time, sensing time, durability time and local dispatch overhead separately.

## Scope and compatibility with the built session manager

### What is added

Under the existing `wm/` directory, add:

| Deliverable | Fixed choice |
|---|---|
| `ark-worker` and `ark_worker/` | Python 3 event loop; persistent Xlib connections via `ctypes` to libX11/libXtst/libXext/libXdamage/libXfixes/libXcomposite/libXi/libXrandr. One owner thread for Xlib. X I/O loss retires the generation. |
| Provider children | Persistent AT-SPI child using PyGObject/libatspi; persistent OCR child using `tesserocr` and Tesseract, English language initially. IPC is framed JSON plus shared-memory frame references. Children are started before readiness, never forked for each action. |
| CDP provider | Persistent `websocket-client` connection inside the worker; one session-local Chromium debugging endpoint, registered by the trusted application launcher. No browser auto-discovery on host ports. |
| `ark_gate.c` | Build-time native module implementing the shared control page with atomic acquire/release loads and a consistent sequence lock. Do not implement interprocess atomicity with Python object assignments. |
| `ark-broker-dev` | Thin development implementation of V2 broker responsibilities needed here: bind, lease, control page, intent journal, deduplication, per-session queue, receipt store and driver socket. No scheduler, warm pool, console, systemd or OS provisioning. Its wire methods and journal envelope are the production contract. |
| `ark-driver-adapter` | Long-lived JSON-RPC client; exposes `observe`, `act`, `wait`, `done` to either driver type. `action.get` and reconnect happen inside the adapter. No local model. |
| Input-helper extension | Extend the existing `mkdevs.py` owner of the uinput descriptors; retain its creation, discovery and shutdown recipe. Add the gated event socket described below. Do not create another pair of devices. |
| Profiles and fixtures | Versioned development profile JSON, minimal Openbox configuration with no F2 binding, instrumented test apps and independent shell acceptance harness. |

Dependencies use installed, acceptance-tested ArK2 versions, captured by package version and binary/library hash in the run manifest. The sessiond executable retains its stdlib-only constraint [S]; that constraint does not extend to worker providers. No CLI tool runs in a successful action's dispatch path. Startup diagnostics may use existing platform CLIs.

### Development profile data

Profile JSON is closed-schema, canonicalized with sorted keys and hashed with SHA-256. Required fields are `schema,backend,resolution,keymap,locale,wm,applications,input,capture,providers,policy`. Initial choices: backend `xvfb|xorg`, resolution `{width:1920,height:1080,depth:24}` T, keymap `us`, locale `C.UTF-8`, WM `openbox-desktop`, no WM keybindings and no automatic host activation. The trusted application launcher applies the profile locale/keymap to the session and launches each app with the built sessiond environment; arbitrary driver shell launching is outside this API.

`applications` is a map keyed by executable build hash measured at launch, with fields `canonical_app,surface,role_map,action_map,shortcuts,dialogs,text_mode,cdp`. `input` holds `backend,inter_key_delay_ms,double_click_gap_ms,pointer_tolerance_px`; defaults are exactly those below. `capture` holds explicit masks and fast-path capability; `providers` contains the timers below. `policy` initially allows ordinary workspace GUI actions and requires owner confirmation for V2's high-impact classes; no arbitrary script hooks in any field. At bind, unknown executable hashes receive metadata/pixel/physical-input capability only; semantics require observing actual refs and supported interfaces, never app-name guesses. Persisted learned routing is separately keyed by the exact application build.

Initial `thunar` application entry uses `surface:"controls"`, `text_mode:"plain"`, `shortcuts:{rename:["F2"],select_all:["Control_L","a"],paste:["Control_L","v"],accept:["Return"],cancel:["Escape"]}`. These are used only with observed focus and target eligibility. Canonical role map: `text` is an editable AT-SPI Text/Entry node; `button` is PushButton; `file_item` is Icon/ListItem/TableCell under the observed selected item's container; names remain exact observed names. The container is the selected item's observed parent/collection, not an assumed first tree list. Do not infer that a generic file item is a directory; directory truth belongs to the independent oracle.

The initial rename dialog classifier is: a newly mapped transient of the requested Thunar family, a semantic dialog root containing exactly one enabled editable text field whose initial value equals the selected item's pre-dispatch name, and enabled buttons with exact accessible names `Rename` and `Cancel` in the profile locale. Bind the unique field by editable-role constraint, without guessing its accessible name. `entry` binding selector below accepts canonical `role:"text"`; the classifier supplies its uniqueness. If this exact classifier does not match installed Thunar, the acceptance run records a profile incompatibility and the route is unavailable until an explicit profile revision; the worker never guesses a title/field. Confirming an edited rename reuses this dialog's identity, not its initial-name classifier.

Initial action alias `press` maps to an observed AT-SPI action named `click`, otherwise `press`, otherwise `activate`, in that fixed order; a target exposing none is unsupported before submission. Other action names must exactly match an observed action. Other native apps start with observed interface capabilities and no invented shortcut/dialog classifier. Canvas and EGL entries set `surface:"canvas"`; native dialogs are classified separately. Chromium sets `surface:"page"` only for a registered page target, and plain native controls elsewhere. Store concrete build hashes and observed interface/classifier diagnostics in the acceptance manifest; pinning a build does not assert its behavior passed.

### Binding the built session

The development broker binds an existing ready session from the sessiond registry under its lock. It verifies the X process identity using PID plus process start time, the session markers, socket, HOME and bus addresses; assigns a fresh random generation ID; computes a development profile hash; and starts the worker through `ark-sessiond run` so markers and environment are preserved. Only one worker may bind a session. Worker restart requires retiring/stopping that session and creating a new one. Session IDs alone can be reused by v0 and are never durable identity.

`ark-broker-dev bind --session sN` prints `{session,generation,profile_hash,socket,state}`. Unbind stops the session through sessiond. The broker supervises worker, X, both buses and WM: critical process death retires the generation and calls sessiond teardown. Provider-child death degrades that provider only. The worker's readiness includes WM, provider health, capture capability and backend self-test; sessiond's existing `ready` alone does not admit acts.

### Deliberate v0 bridge, without new authority

The built sessiond keeps ownership of display/bus lifecycle for this slice. The worker attaches; it does not launch duplicate buses or replace spike recipes. Later V2 lifecycle work moves those children under the worker without changing this API. The existing root helper remains a development exception until V2's unprivileged `ark-device-helper` lands. Record `development_same_uid=true`, `helper_identity=root`, `profile_signature=unsigned` and `audit_mode=jsonl` in health and every run manifest. These do not claim UID/namespace containment, signed profiles or production helper privilege separation.

There is one authoritative **action** journal, `STATE/act/journal.jsonl`, written only by the development broker under an exclusive lifetime lock. Sessiond's existing `STATE/journal.jsonl` remains its lifecycle log. Do not have workers append a competing receipt journal. The action journal uses durable intent, dispatch-boundary and final records; hash chains and signed checkpoints remain the later V2 audit milestone. Action IDs and intent deduplication are not deferred.

## Invariants that every build gate enforces

- Driver → broker → worker → session backend. The worker accepts mutations only from its authenticated broker connection, carrying a durable `journal_seq`, generation and control epoch. Direct driver X, AT-SPI, CDP or device access is outside the supported act path; the v0 same-UID environment does not yet enforce denial against hostile peers.
- One revision counter per generation, owned by the worker. One action executes per session; at most 8 actions wait (T, V2). Different sessions have separate queues and continue independently.
- Broker control page is the only mutation authority. Worker, semantic provider child and input helper check it immediately before each mutating request/write. All routes, including clipboard and CDP, obey pause and epoch fencing.
- Every step begins with verified targeting and ends with its explicit predicate. Flush, helper acknowledgement, XDamage and provider return values do not prove application effect.
- A batch is sequential and nontransactional. No rollback. Stop on the first unverified step. Never replay an unknown or merely unobserved action.
- Never inject on host DISPLAY; never invoke host ydotool; never reopen a global bus or clipboard. Xpra input/device creation remains disabled in agent mode. EGL affects application launch/rendering, never input ownership.

## Observation contract

### Profile defaults and escalation

Backend and surface type are separate dimensions. Switching to Xorg-dummy does not improve the accessibility tree. `gpu-egl` is a launch overlay on either backend, not a third input backend.

| Profile / surface | Default observation order after the unchanged check | When to use the next channel |
|---|---|---|
| `xvfb`, exposed desktop controls | Cached window/focus/modal metadata → targeted AT-SPI tree text/delta → source screenshot crop → OCR of a specified text ROI | Tree missing, incomplete for the target, stale or degraded: return a crop. OCR only for a requested text lookup or an OCR predicate. |
| `xorg`, exposed desktop controls | Same order as Xvfb | Same conditions; capture implementation differs, not semantic priority. |
| Chromium page content, either backend | Cached CDP accessibility/DOM summary → crop → targeted OCR | CDP is the page provider. Native browser chrome and native file dialogs use AT-SPI/X input. No cross-page coordinate guesses. |
| VNC, Blender viewport, CAD or other canvas, either backend including EGL | Cached window/focus/modal metadata → source screenshot crop → targeted OCR for labels/text | Do not spend a full tree traversal on the canvas. Still use an exposed tree for surrounding native dialogs. Driver vision interprets geometry or imagery; worker never calls a vision model. |

An OCR request runs the OCR child on the unscaled source crop; it is not automatic full-screen text mining. In `auto`, a known canvas returns a crop; an exposed-control surface returns tree text if coverage is sufficient, otherwise a crop with the tree/provider failure. `text_query` additionally asks for OCR when semantics cannot answer it. A screenshot is visual evidence; OCR is a derived text channel, not an input route. The driver may explicitly request any available channel.

### State cache and cheap unchanged

Subscribe to X lifecycle, geometry, focus, active-window, transient, stacking, selection, XI input, RandR and XDamage events; AT-SPI object/text/state/provider events; and CDP navigation/DOM events when a page is registered. Every ingested event, control transition and action completion consumes the next `rev`. Each facet records its last relevant revision. Semantic snapshots are immutable cache entries, not live remote proxies passed to the driver.

Use an event-draining barrier before publishing a coherent snapshot after pending capture/semantic work. A returned snapshot is ordered by worker arrival, not a claim that X and D-Bus events share causal order. Include each provider's sample revision, age, coverage and health. A facet whose events or refresh are outstanding is `dirty`, not unchanged.

`window.observe(since_rev=N)` returns `unchanged:true` only if the requested scope/facets have no relevant change, no pending dirty work, complete event coverage, valid transforms and healthy providers within the watchdog interval. It means “the monitored requested state has not changed as of this worker revision,” never “the application finished.” It returns current `rev` even if unrelated events advanced it. No screenshot readback, tree walk, OCR, hashing, X round trip or synchronous provider probe occurs on this path. T: ≤1 ms in worker / ≤3 ms end to end [V2 §3.12].

Missing AT-SPI or CDP coverage, bus disconnect, an event gap, unknown `since_rev`, old generation, a dirty region or watchdog overdue forbids an unchanged answer. Return `unchanged:null`, `resync_required:true` or a partial fresh result with exact uncovered facets. An empty accessible tree is not evidence of an unchanged canvas. A hidden/minimised canvas without valid backing pixels reports `pixels_unavailable` rather than reusing a preview.

Watchdog reconciliation every 5 s T [V2 §3.9]: refresh X window/focus state and invalidate or reconcile active semantic scopes, even without events. Health can be checked asynchronously; crossing the deadline invalidates the fast path. Changes caused by this reconciliation get new revisions. A full AT-SPI snapshot is built at most once per window per invalidation; subsequent reads use the cache. Provider refresh beyond its deadline returns a partial observation promptly.

### Pixels, OCR and bounded output

Maintain one session shadow framebuffer. Initial implementation on both backends: persistent MIT-SHM image requests for damaged rectangles; XComposite named pixmaps for covered window crops when available. Xvfb direct framebuffer mapping is optional only after S-CAP and a separately qualified launch configuration; [S] does not enable `-fbdir`, so do not assume a mappable framebuffer already exists. Xorg's DRI masking stays intact. If MIT-SHM or Composite is absent, mark the affected fast path unavailable and use persistent XGetImage with the capability and slower budget recorded; no screenshot CLI.

T: coalesce damage for 8 ms; use 16 ms above 500 events/s [V2]. Capture damaged rectangles, hash only dirty tiles, and avoid new evidence for identical content. Tile edge is 64 source pixels T; hash includes dimensions, pixel format and bytes. Caret/animation masks are explicit observed or profile-owned regions, returned with every affected result; never silently mask task pixels. Cursor position is a separate facet; cursor rendering is excluded from image hashes.

OCR cache key is `(source_pixel_hash, rect, engine_build, language, segmentation_mode, mask_hash)`. Cache successful empty results too. OCR response includes words, boxes and engine confidence as metadata, not calibrated correctness. T: single-line mode for text fields, block mode for text panels; region type is supplied in the request/profile. No fuzzy matching in worker verification. T: OCR confidence floor 90 for each required non-whitespace word; below that yields unknown. A positive OCR predicate needs exact case-sensitive text and the required geometric scope; negative OCR output alone cannot prove absence.

T limits from V2: delta ≤40 nodes or 2 KiB; full tree page ≤200 nodes or 16 KiB, with opaque continuation tied to the snapshot revision. Bytes mean UTF-8 JSON payload for semantic nodes, excluding the outer envelope/evidence references. Return `truncated`, `coverage` and continuation; never imply missing nodes are absent. Model crops default to a long side ≤1024 px, with `source_rect` and scale transform; full frame only on explicit request. Verification uses original source pixels. Sealed shared-memory frames move by Unix descriptor passing, referenced by `(frame_ref,rev,rect,hash,format)`; immutable evidence copies go to the broker store. The adapter encodes images into the driver's supported image input without recapture.

T capture targets [V2]: fresh 1080p crop ≤10 ms, full frame ≤15 ms; S-CAP failure selects the V2 fallback ≤30 ms target and labels the capability. These are not proven by session startup timings.

## Actuation and route selection

### Session backend

| Backend | Physical input route | Unsupported/failure response |
|---|---|---|
| Xvfb | Persistent XTEST: key/button events and absolute session pointer motion | `uinput` is `backend_unsupported`. Synthetic-rejecting app with no valid semantic route: `backend_unsupported`, recommend reopen in Xorg profile; do not move a live window or retry on the host. |
| Xorg-dummy | Worker → existing helper's gated socket → existing uinput keyboard/pointer → assigned evdev nodes → this session Xorg | Evdev is the physical route even if XTEST happens to be available. No silent downgrade to XTEST. Device loss/grab loss retires the generation. |

Keep Spike B/S recipes: udev `ID_INPUT=0`, `LIBINPUT_IGNORE_DEVICE=1`, `ID_SEAT=seat-arkdrive`; explicit evdev sections; `AutoAddDevices=false`, `AutoEnableDevices=false`, `AutoAddGPU=false`; dummy video; private mount namespace masking the same `/dev/dri` nodes; `-novtswitch -sharevts -noreset -nolisten tcp`; explicit reattach AND enable; pointer `Device Accel Profile -1`. Do not copy the old literal property-atom check: [R] corrected it to `Device Enabled \([0-9]+\):\s*1`.

Before worker input admission, additionally require V2's `GrabDevice=true` on both assigned evdev sections, the independent host Xorg ignore rule, udev exclusion and a live host sentinel. These are required extensions, not claimed checks in [R]. The sentinel closes all gates if any ArkDrive device appears on the host or yields host raw input. The helper/worker independently detect assigned-node identity loss, enabled/attachment changes and sentinel loss; close the affected gate. T sentinel detection ≤100 ms [V2 §5.3]. No auto repair or recreation inside a live generation.

The helper retains the only uinput write descriptors. Writing to `/dev/input/eventN` is not the injection implementation. Add `--event-socket PATH --broker-uid UID` to helper launch in sessiond and record its socket in session metadata; UID is sessiond's original development-user UID, passed as a numeric argument before sudo. These are the required launch additions. The AF_UNIX/SOCK_SEQPACKET socket, mode 0600, starts **dormant**: it accepts lifecycle `quit`, but cannot write input. Sessiond still creates devices before a worker generation exists.

At bind, the broker sends `{op:"bind",session,generation,worker_pid,worker_starttime,device_identities}` plus a read-only control-page descriptor via SCM_RIGHTS. Verify broker SO_PEERCRED UID equals the launch-time broker UID, verify PID/starttime against `STATE/act/broker.json` written under the broker's exclusive lifetime lock, verify worker UID/starttime/session markers and recorded device sysfs identities, require a closed control page, and return the helper instance ID. This development registration is trusted same-UID coordination, not a root-enforced hostile-peer boundary. Permit exactly one bind for this device lifetime; bind/reconnect never resets deduplication. A changed generation requires teardown/new devices. Subsequent event connection must match that pinned worker's SO_PEERCRED and process starttime. Parent verifies the helper instance. No helper restart or descriptor reopening is needed to adopt existing devices; no fd reaches the driver.

Each request is `{generation,epoch,journal_seq,step_id,chunk_id,events:[{device:"kbd"|"ptr",type,code,value}]}`. Allow only the fixed existing device capabilities. Ack is `{chunk_id,status:"written"|"not_written"|"unknown",events_written,error}`. Same chunk ID plus same payload returns its stored acknowledgement, never rewrites; changed payload is an error. Store IDs for the generation. Lost helper means lost devices and generation retirement, not a fresh helper replay. Register/inspect this capability before binding; an old `quit`-only helper yields `backend_unsupported` before any input, rather than creating replacement devices under a ready session.

The broker durably arms each mutating step before dispatch; its acknowledgement is required. Helper checks generation, epoch, AGENT state, lease hash and permit deadline before each write, including each delayed event group. It reports exact written prefix when known; any truncated/uncertain write stops the operation. Terminate event groups with SYN_REPORT. Gate closure permits only release of tracked held buttons/keys as cleanup; no new presses. Cleanup is journaled and never counted as completing the intent.

The existing pointer is relative. For an API absolute move: read cached pointer position; emit relative delta; confirm via XI motion/XQueryPointer if no event; allow at most 2 correction moves T, within 1 source pixel T. Bound the complete positioning stage to 100 ms T. No button press until position is confirmed. Acceleration disabling remains required; no new absolute device or unqualified coordinate mapping.

XTEST uses persistent Xlib/Xtst, not `xdotool`/cua subprocesses or XSendEvent. Keyboard mapping is a pinned profile keymap, initially `us` without compose/remapping. Translate X keysyms using the actual XKB mapping; evdev mapping is compiled from the pinned keymap, never assumed to be X keycode minus a constant. Reject unsupported symbols before any press. All key/button operations release their holds before returning. Key repeats and held modifiers never span driver calls.

Use a dedicated mutation X connection separate from sensing/capture. Buffer at most 100 text characters or 512 low-level events per group T, whichever is smaller; ordinary shorter text is one burst/flush. Check the gate before enqueueing each XTEST request and immediately before flush; do not append future groups speculatively. If a gate closes, call XTestDiscard on that dedicated connection before any cleanup, discarding unsent requests; only then enqueue release-only cleanup and flush. Auto-flushed requests count as possibly submitted and cannot be recalled. Larger text uses explicitly recorded bounded groups; profile key delays use timer groups. Helper event packets have the same 512-event T cap and gate checks. No queued new press may be flushed incidentally by release cleanup. Server processing barriers prove X request processing only, never application completion.

### Capability ladder, selected per operation

Persist route hints keyed by `(profile_hash, app_build, toolkit, surface_role, operation)`. Learned success only follows a verified effect; a provider's accepted request does not make it healthy for effect verification. A new application build invalidates hints. The selector tries the first eligible route; it does not spray attempts.

| Intent | `route:auto` order | Eligibility/fallback condition |
|---|---|---|
| `invoke` exposed action | CDP action for page ref → AT-SPI Action for a native ref → profile-declared shortcut → click on fresh exposed bounds via this profile's input backend | Only a route applicable to that same target/meaning is eligible. CDP does not act on native dialogs. Shortcut name/action and bounds come from profile or observed actionable node, never invented. |
| `text`, replace | CDP fill for page input → AT-SPI EditableText → profile backend select-all + mapped typing → private clipboard paste | Focused editable target required. Final whole-field equality verifies replacement. Clipboard is eligible only if the target has a declared paste shortcut and the private selection provider is healthy. |
| `text`, insert | CDP insert → AT-SPI insert → mapped typing → private clipboard paste | Caret/selection baseline and final exact content required for local verification. No insertion replay after a dispatched attempt. |
| `key`, `click`, `move`, `scroll`, `drag` | Only the profile's physical backend | These ask for physical input. They do not silently turn into semantic operations. |
| Canvas spatial target | Driver chooses point from screenshot; profile backend dispatches | OCR/vision supplies location or interpretation, not a separate actuation rung. |

Clipboard is per-session X selection ownership and a real UTF8_STRING transfer, including INCR when necessary. Save the previously owned worker selection, serve requested bytes, paste using the profile shortcut, verify the field, then restore previous worker-owned content or clear if the worker displaced an external owner; never read/restore host clipboard. Record selection request/transfer completion, hash and size for non-secret payloads. Transfer completion proves delivery to a selection consumer, not insertion. General clipboard import/export remains owner-only V2 `clipboard.transfer`.

For unmappable Unicode with `route:auto`, choose clipboard before dispatching any mapped prefix. Do not remap global keyboard state or assume the GTK Unicode-entry shortcut works in all apps. `route:input` rejects unmappable text. `replace:true` through physical input is a tracked macro: verify target focus, select-all, emit text, release holds, read final value; interruption is partial/unknown, never retry the macro. Only one focus-verified field can be edited per macro.

Fallback to another route is automatic only after **definite non-dispatch** (unsupported interface, failed local resolution, or provider error explicitly known to precede any call/write). A reply of false, timeout or disconnect after submitting a mutating RPC is not that proof. Reconcile the exact expectation; if true, finish using the original attempt; otherwise stop. An app known to reject XTEST is classified by a controlled fixture/profile, not by repeatedly clicking production UI until something changes. Xorg app rejecting semantic routes uses evdev; an app still rejecting evdev surfaces failure, never a new injector.

## Driver API and exact schema

### Transport and common values

Use V2 JSON-RPC 2.0 on a Unix stream socket. Prefix each UTF-8 JSON message with an unsigned 4-byte network-order length; T maximum 256 KiB, reject larger before parsing. Frames use descriptor passing, not JSON image bytes. Unknown keys/enums, NaN, out-of-bounds rectangles and ambiguous matches are invalid. Strings are Unicode, NUL forbidden. Text payload T maximum 4096 UTF-8 bytes. No shell, eval, JavaScript expression, arbitrary CDP method or application scripting operation is exposed.

Every RPC has positive `deadline_ms`, session and generation except connection-level health. Broker stamps an absolute CLOCK_MONOTONIC deadline at receive; queueing, retries, focus and verification consume it. API credentials/lease values are opaque and redacted in receipts. A request ID identifies the RPC; `action_id` identifies durable intent independently of transport retries.

The broker exposes `driver.attach({session,generation,deadline_ms})` only on that driver's socket. It checks SO_PEERCRED against the bind-time launcher registration, then returns `{lease,control_epoch,rev,capabilities,state}`. The per-boot MAC'd lease is bound to this peer, generation and epoch; T lifetime 60 s, auto-renew at 30 s while connected, lapse 5 s after disconnect [V2 lease policy]. New connections must attach again; they may query old action IDs but cannot revive old authority. Production adds V2 slot/cgroup verification; same-UID development registration is not that boundary.

Initial bind enters `BOUND`, gate closed. Attach grants observe-only scope. After a full fresh observation, adapter calls internal `driver.ready({session,generation,lease,observed_rev,deadline_ms})`. Only `BOUND`, owner-authorized `RESUME_PENDING`, or `RECONCILED_READY` states admit ready; broker verifies that this peer received that observation in the current epoch, required backend/sentinel health, absence of unresolved effects and a still-live worker. It journals activation, issues an act-capable lease, writes AGENT plus the permit deadline to the control page and returns `{lease,control_epoch,rev,state:"AGENT"}`. Adapter stores it before sending act. Attach/observe/ready during PAUSED, takeover, journal failure or unresolved reconciliation cannot open the gate. Owner resume explicitly enters `RESUME_PENDING`; a reconciled previous action enters `RECONCILED_READY` but still requires explicit next-driver ready after observation. Initial authorized bind is the initial admission authorization; no automatic driver/model task starts merely because sessiond listed a ready display.

Owner CLI `ark-broker-dev control --session sN --state pause|takeover|resume` calls `control.set({session,generation,state,deadline_ms})` on a separate mode-0600 owner socket. The broker increments epoch and closes the gate before acknowledging pause/takeover; resume stays closed until the attached driver obtains a new lease and a fresh observation. Response is `{state,control_epoch,rev}`. This slice supplies pause/takeover fencing and cancellation, not viewer human-input forwarding; report `human_transport:unavailable` and do not announce HUMAN input live. Owner-only registration and control sockets must not be mounted into a future driver namespace.

Adapter `done({summary,evidence_refs,success})` is a nonmutating job-result report to the broker. It records `driver_claimed_success` with linked receipt IDs, closes mutation authority and does not promote driver judgment to independent task verification or stop applications automatically. Observation and wait do not consume the mutation queue. A separate owner `unbind` tears the session down.

The trusted launcher registers Chromium with `{window_family,endpoint,process_pid,process_starttime,profile_dir}` on the broker's internal registration channel. Worker validates marker environment and endpoint locality/private profile, then owns the CDP connection. Associate a page target with its session browser window via CDP browser-window ID and launcher-owned X window identity; ambiguity disables the page route. On navigation, invalidate old page refs and transforms. CDP semantic refs use backend DOM node IDs plus document incarnation; reads use Accessibility/DOM snapshots and fresh AX value readback. Closed mutation implementations are: `invoke:press` uses DOM.focus/DOM.getContentQuads then Input.dispatchMouseEvent at a validated content point; text replace uses DOM.focus, Input.dispatchKeyEvent for select-all and Input.insertText, then Accessibility.getPartialAXTree value readback. Insert is eligible only when the provider exposes the current field value/caret/selection; otherwise it is unsupported before dispatch and uses a qualified alternative. Page fields without readable value or unique geometry fall back before dispatch; no arbitrary Runtime evaluation is accepted from a driver. Native choosers stay outside the CDP page route. CDP disconnect after input is submitted has the same uncertain-effect rule as other providers.

Types:

- `Scope = {kind:"session"} | {kind:"family"|"window",handle:string}`. Handles are generation-bound; raw XID is metadata only.
- `Point = {space:"desktop",x:int,y:int,at_rev:int} | {space:string,x:number,y:number,at_rev:int}`. The latter names a worker-issued window/preview/CDP/remote transform. Desktop is session source pixels at scale 1, origin top-left. Half-open bounds. Ties round to nearest, halves away from zero. Reject stale transform/geometry, outside points and unsupported spaces; never clamp a target silently. CDP CSS points are for the associated page only; VNC remote coordinates require a profile-owned validated transform.
- `Target = {window:handle,ref:string} | {window:handle,selector:Selector} | {window:handle,point:Point}`. Selector is `{provider:"atspi"|"cdp",role?:string,name?:string,within?:ref}` with exact case-sensitive equality and at least role or name. It must match exactly one live object in the given scope. No `nth` guessing.
- A ref is an opaque generation/provider-instance/object-incarnation token with its relevant facet revision. Provider restart invalidates its refs; a cache page's node fields are `{ref,role,name,value,states,actions,bounds,parent,children,facet_rev}` with protected value redaction.
- `Binding` is `{from_step:step_id,name:string}`. Can substitute for `target` or a predicate subject only if an earlier verified step exported that name; no forward refs, cycles or executable expressions.

### Observation and waiting

`window.observe` parameters:

```json
{"session":"s1","generation":"G","lease":"L","deadline_ms":1000,
 "scope":{"kind":"session"},"since_rev":null,
 "channels":["auto"],"subtree":null,"crop":null,"text_query":null,
 "budgets":{"nodes":40,"bytes":2048},"continuation":null}
```

`channels` is either `["auto"]` or a nonempty subset of `metadata,tree,pixels,ocr`; `ocr` requires `crop` and its line/block segmentation mode. `subtree` is a ref; `crop` is `{space,at_rev,x,y,width,height,mode:"line"|"block"}`. Explicit full frame is `crop:{full:true}` with pixels selected. `text_query` is an exact literal text and optional role, not an instruction. Null `since_rev` means full requested scope. Budgets may be smaller, never larger than the T delta/full caps. `continuation` excludes new scope/channel arguments and reads the immutable existing snapshot; invalidated/missing snapshot returns `resync_required`.

Response always has `{session,generation,rev,unchanged,scope,coverage,providers,windows,focus,modal,delta,nodes,frames,ocr,truncated,continuation,resync_required}`. Empty arrays are explicit; absent/unavailable data is null with coverage reason. Window metadata includes handle, family, app ID/build, title, mapped state, content/decorated bounds and geometry revision. `providers` contains `state:"healthy"|"degraded"|"unavailable"`, last sample revision/age, failure reason and retry-after time. `delta` contains upserts and removed handles/refs; deletions are explicit. Include outstanding action IDs and any effects still reconciling.

`window.wait` takes `{session,generation,lease,deadline_ms,since_rev,predicate}` and returns `{matched:true|false|null,rev,evidence,reason,state_delta}`. True means predicate proven, false means its defined contrary/expiry, null means unavailable evidence. Same predicate engine as act, no dispatch, no model calls. T maximum wait 10000 ms; repeated waits are safe, and a long render uses separate waits without holding the mutation queue. `since_rev` is an evidence lower bound, not proof of causal attribution.

### Mutations

`window.act` requires `{session,generation,lease,control_epoch,action_id,base_rev,deadline_ms,steps}`; optional `reviewed_action_id` is restricted to the visual-review continuation rule below. T: 1–8 steps; total deadline ≤10000 ms [V2]. A step is:

```json
{"id":"open-rename","target":{"window":"s1/G/w7","ref":"folder-ref"},
 "require":{"focus":"s1/G/w7","modal":null,"selected_ref":"folder-ref"},
 "intent":{"kind":"key","keys":["F2"],"route":"input"},
 "expect":{"kind":"dialog_present","parent":"s1/G/w7","role":"rename",
           "bind":{"dialog":"dialog","entry":{"role":"text"}}},
 "timeout_ms":1500}
```

All steps require `target`, `require`, `intent`, `expect`, unique `id` and positive `timeout_ms`. T timeout default in adapter 1500 ms, maximum remaining request deadline. No `continue_on_error`, arbitrary branching, sleeps, loops, detached actions or unspecified postcondition. `route` is `auto|cdp|atspi|input|clipboard` with the intent eligibility above; default `auto`. An explicitly requested route cannot fallback to a different route.

| `intent.kind` | Exact additional parameters | Semantics |
|---|---|---|
| `focus` | none | Raise/focus this window through WM/session control; prove focus. Unexpected modal blocks. |
| `invoke` | `action:string` | Named observed Action or profile action on ref/selector, e.g. `press`. No arbitrary app command. |
| `key` | `keys:[keysym,...]` | One chord; T maximum 4 keysyms, modifiers first, exactly one final nonmodifier. Press in order, release reverse. Separate chords are separate steps. |
| `text` | `text:string,replace:boolean` | Replace whole editable field or insert at observed caret/selection. Does not press Return. Target must be a field or validated focused canvas text editor. |
| `click` | `button:"left"|"middle"|"right",count:1|2` | Target ref resolves fresh bounds center, or explicit point. T double-click gap 80 ms; only a profile-qualified gap may override it. Both clicks are intentional parts of this single primitive. |
| `move` | none | Move to target point or ref center, no button. |
| `scroll` | `axis:"vertical"|"horizontal",ticks:int` | Nonzero T range −20…20; positive means down/right. Position before scrolling. Evdev uses wheel sign conversion; XTEST uses logical wheel buttons. No promise of pixel scroll distance. |
| `drag` | `to:Point,button:"left"|"middle"|"right",duration_ms:int` | T duration 50–1000 ms; straight path sampled in event-loop timer groups at 8 ms T; release at end. Start uses target point/ref center. Gate checked for every group; no cross-call holds. |
| `secret` | `ref:string,replace:boolean` | Broker-resolved protected value, owner confirmation required per V2. Broker passes it on protected local channel; never in model/receipt/journal. Target evidence masked before dispatch. If the resolver/confirmation is absent in development, reject `confirm_required` before dispatch. |

`click`, `scroll` and `drag` also accept optional `modifiers:[keysym,...]`, default empty, T maximum 3 distinct modifier keysyms from `Shift_L,Control_L,Alt_L,Super_L`. Position/focus first, then press modifiers, execute the pointer operation and release in reverse order even on cancellation. This is one tracked primitive, never persistent key state. Ref/point window targets must be mapped and hit-test unobscured at dispatch; the worker may raise the permitted target through its WM, but must confirm stacking and geometry before the press. Ambiguous/covered targets stop rather than click through another window.

`require` is a conjunction; allowed keys only: `focus` (window), `modal` (null or handle), `modal_role` (profile role), `selected_ref`, `editable_ref`, `ref` (must exist/live/enabled), `text_equals:{ref,value}`, `geometry:{window,rect}`, `pointer:{x,y}`. Empty conjunction still includes implicit generation, epoch, target existence, focus/modal policy, target transform and relevant ref freshness checks. `require.focus` describes the intended verified focus: if focus alone is wrong, the worker may focus that same permitted window and confirm it before input. A modal mismatch or target change is never repaired by dismissing UI.

At admission, all literal refs/points/preconditions must be valid as of `base_rev` and their dependent facets must not have changed since it. Do not compare `base_rev` to global `rev` for equality: unrelated damage is not a stale selection. Implicit dependencies include geometry and stacking for a point; tree incarnation/state for a ref; selection for F2; focused field/caret for text. Expired cache history yields `stale_rev`.

Use these exact guard dependencies: physical points guard window incarnation, geometry, stacking, transform, focus and modal; ref invocation guards object incarnation, enabled/action state and its observed bounds if physically actuated; keys guard window incarnation, focus/modal plus any explicit selection; text guards field incarnation, editability, focus/modal, value/caret/selection; predicate reads guard subject incarnation and fresh coverage, not equality to old contents. Every touched value facet must refresh for verification. Membership/content changes do not change container incarnation. A successful step advances only its declared written/expected facets in the batch context; unrelated event changes remain conflicts. Expected field value/focus updates are accepted only after that step's predicate proves them. A newly mapped modal is accepted only if its classifier and family predicate succeeds. Consequently the next Return may use the verified dialog/entry despite the batch's own text-value change, while a replaced field or unexpected focus change still aborts.

Between steps, literals stay guarded against their original dependencies. A later step needing changed state uses the prior step's verified bindings, which carry the verified revision and newly resolved object incarnation. Bind only unique objects found while proving that expectation; absence or multiple matches stops the batch. Only expect-declared focus/modal changes are allowed; unexpected changes abort. Drain events and verify focus/modal again before every input group. This makes a dialog created by step A usable by step B without pretending its ref existed at the batch's original revision.

T focus confirmation deadline 100 ms inside the step, at most one activation request. Default text inter-key delay is zero T, one burst/flush; qualified per-app key delay is profile data, included in receipts and the deadline. Gate checks still occur before every buffered group/write. A nonzero delay uses timers, not blocking sleeps.

### Predicate language and verification strength

Predicates are closed JSON unions: the encoding is always a flat object `{kind:<name>,...parameters}`, as in the rename example; table rows use `name:{parameters}` only as compact notation. Subjects use literal refs/windows or prior bindings. `all:{of:[...]}` and `any:{of:[...]}` combine predicates, T maximum nesting depth 4 and 16 leaves. No arbitrary code or general `not`. Unknown propagates unless the conjunction/disjunction is already decided. A negative condition needs valid complete coverage.

| `kind` and parameters | Proof, including what does not count |
|---|---|
| `focus_is:{window,ref?}` | X focus is that window/descendant and, if requested, semantic focused object is the ref. Backend acceptance is insufficient. |
| `pointer_at:{point,tolerance_px}` | Session pointer coordinates, T tolerance default/max 1 pixel. Proves only a move. |
| `dialog_present:{parent,role,bind?}` | Newly mapped transient in the permitted family with exact profile classifier. Profile classifier is explicit conjunction of window relationship plus exact accessible role/name or exact configured window class/title; never fuzzy role inference. Binding selectors must be unique. |
| `dialog_absent:{handle}` | Previously observed dialog unmapped/destroyed, complete X window coverage. It does not prove save/rename acceptance. |
| `text_equals:{ref,value}` | Fresh AT-SPI Text/CDP field value equals literal, including whitespace and case. Provider's set-text return value is insufficient. For insertion, read prevalue/caret/selection before dispatch, compute the exact expected full value, and require equality. |
| `node_state:{ref,state,value}` | Fresh semantic read of `selected,checked,expanded,focused,enabled` boolean only. A keypress alone is insufficient. |
| `item_present:{container,name,role}` | Exact item in current semantic container with verified containing window; no substring match. `item_absent` is supported only with complete fresh container enumeration and the same parameters. |
| `window_geometry:{window,rect}` | Fresh configured source bounds equal the requested rectangle. A generic drag completing is insufficient. |
| `scroll_value:{ref,axis,value}` | Fresh exposed scroll value equals exact target. For opaque scrolling use `ocr_text` on a new expected label, or request visual review. |
| `ocr_text:{crop,text,mode}` | Positive exact normalized text in a current source crop; normalization is Unicode NFC plus line-ending CRLF→LF only. Text field: whole line equals; panel: at least one complete OCR line equals. Required confidence and scope rules above apply. Does not prove absence or remote persistence. |
| `frame_matches:{crop,hash}` | Exact current source ROI pixel hash equals a previously supplied evidence hash under the same mask and format. Explicit visual state only; no business/task meaning inferred. |
| `visual_review:{crop}` | Always yields unknown with a fresh frame reference, halts the batch and requests driver interpretation. Used when a canvas effect has no deterministic local predicate. No hidden vision call. |

For `focus` only `focus_is` naming the intent's exact destination may be the main postcondition; for `move` only `pointer_at` at its exact destination. `text` requires `text_equals` on that same field with exactly the requested replacement or computed inserted whole value; a qualified canvas editor may instead use exact whole-line `ocr_text` equal to that value. Other mutating intents require an application-state predicate (text/node/dialog/item/geometry/scroll/OCR/frame), optionally combined with focus; focus or pointer position alone cannot verify a click/key/text/drag.

A predicate already true before dispatch yields `precondition_already_satisfied`: no mutation, `effect:verified`, disposition `satisfied_without_dispatch`; this is allowed only when the operation-specific target state is proven: focus/move at the exact requested destination, whole-field text exactly equal to `text replace` value, or `invoke` whose profile declares the exact state-setting action and same-target state predicate. No initial profile declares a state-setting invoke. An unrelated already-true label cannot skip an edit. For other intents reject an already-satisfied expectation as `invalid_expectation` before dispatch. This avoids accepting preexisting UI as a new action's effect.

Generic ROI change, event receipt, quiet pixels and selection transfer remain evidence only. They are not available as application-success predicates. A canvas single-step can honestly return accepted/unknown and a screenshot for driver review. The next driver call may act on newly interpreted state; it must not replay that action ID or claim machine-verified completion. The worker can prove a requested visible state; only independent ground truth proves a final filesystem result or remote CAD state.

### Rename batch example

Illustrative T values. The profile defines the rename dialog classifier and its unique entry. The adapter sends this in one driver turn after an observation showed `folder-ref` selected:

```json
{"session":"s1","generation":"G","lease":"L","control_epoch":7,
 "action_id":"rename-004","base_rev":120,"deadline_ms":4000,
 "steps":[
  {"id":"dialog","target":{"window":"s1/G/w7","ref":"folder-ref"},
   "require":{"focus":"s1/G/w7","modal":null,"selected_ref":"folder-ref"},
   "intent":{"kind":"key","keys":["F2"],"route":"input"},
   "expect":{"kind":"dialog_present","parent":"s1/G/w7","role":"rename",
     "bind":{"dialog":"dialog","entry":{"role":"text"}}},
   "timeout_ms":1500},
  {"id":"name","target":{"from_step":"dialog","name":"entry"},
   "require":{"modal_role":"rename"},
   "intent":{"kind":"text","text":"reports-2026","replace":true,"route":"auto"},
   "expect":{"kind":"text_equals","ref":{"from_step":"dialog","name":"entry"},
             "value":"reports-2026"},"timeout_ms":800},
  {"id":"commit","target":{"from_step":"dialog","name":"dialog"},
   "require":{"modal_role":"rename"},
   "intent":{"kind":"key","keys":["Return"],"route":"input"},
   "expect":{"kind":"all","of":[
     {"kind":"dialog_absent","handle":{"from_step":"dialog","name":"dialog"}},
     {"kind":"item_present","container":"folder-container-ref",
      "name":"reports-2026","role":"file_item"}]},"timeout_ms":1500}
 ]}
```

The container ref must remain live; its contents may refresh for verification. Only object-incarnation/parent changes invalidate that container identity; ordinary content additions invalidate its cached enumeration and trigger refresh. Predicate text uses canonical role names mapped by the profile to actual toolkit roles. If semantics disappear, a new driver turn may plan explicit screenshot/OCR-based predicates. Do not silently rewrite a previously requested `text_equals` into a weaker “pixels changed.”

## Receipts, durability and partial completion

Retain `{version,phase,dispatched,effect}` and add the V2 fields. `version:2` is the target schema identifier. `phase` is `validate|dispatch|verify|complete|reconcile`, identifying the stage at the stopping point. `dispatched` is true iff at least one mutating request/write is known to have been submitted; it is false if none are known. **False with `dispatch_status:unknown` is not non-dispatch.** `dispatch_status` is authoritative: `not_dispatched` only with proof no mutation was submitted; `accepted` means at least one submission is known; `unknown` means delivery cannot be established or an in-flight submission has an unknown extent. Known accepted delivery can still have an unknown application effect.

Every final receipt contains:

```text
version, phase, session, generation, window|null, action_id, request_hash,
journal_seq, profile_hash, control_epoch, actor_id, lease_hash,
build_id, boot_id, development_flags,
rev:{base,dispatched|null,verified|null,final},
status:success|failed|partial|unknown,
dispatched, dispatch_status,
effect:{status:verified|not_observed|contradicted|unknown,
        predicate, evidence:[], assurance:application_state|visual_state|position|none},
reason:null|enumerated_reason,
steps:[], completed_steps:[], stopped_step:null|step_id,
unexecuted_steps:[], pending_effects:[], state_delta,
timing_ms:{queue,focus,dispatch,application_wait,provider,capture,verify,durable,total}
```

Per-step entry:

```text
id, target_requested, target_resolved, require, intent_redacted, expect,
disposition:executed|satisfied_without_dispatch|failed|unexecuted,
route:null|cdp|atspi|xtest|evdev|clipboard,
attempts:[{route,started_ns,ended_ns,dispatch_status,accepted,
           written_prefix,provider_result,error,reason}],
focus:{before,after}, modal:{before,after},
rev:{base,dispatched|null,verified|null},
effect:{status,predicate,evidence,assurance},
bindings:{}, reason, timing_ms, cleanup:{released,unreleased,confirmed}
```

`accepted` is true/false/null, reflecting route submission only. `written_prefix` contains event/chunk counts when known, not a fabricated application-consumed count. Evidence entries are `{kind,provider,rev,sample_ns,scope,hash,ref,coverage,redactions}`. Record predicate operands, exact successful comparisons, route attempts including rejections, source/transform revisions, provider health, focus/modal changes and cleanup. Broker verifies artifact hashes before durable completion. Secrets include only opaque secret reference plus redaction marker, never values or value-derived hashes, OCR, keycodes, payload length or timing detail that encodes the secret. Non-secret text may be journaled as intent; private evidence follows V2 retention/access policy.

| Situation | Required final fields |
|---|---|
| All steps proven | `status:success`, aggregate `effect.status:verified`, `phase:complete`, all step effects verified. Dispatch may be absent for explicitly satisfied state-setting steps. |
| Rejected before any successful step or dispatch | `status:failed`, `dispatch_status:not_dispatched`, `dispatched:false`; `effect.status:not_observed` for unavailable/unmet expectation, or `contradicted` for proven conflicting state. |
| Submitted step, healthy coverage, expectation not seen by deadline | That step `dispatch_status:accepted`, `effect.status:not_observed`, `reason:timeout`; `status:failed` if no prior completion, otherwise `partial`. Never retry automatically. |
| Submitted step, evidence positively conflicts | `effect.status:contradicted`, e.g. fresh full field text is wrong after completion; status failed/partial. Mere waiting is not contradiction. |
| Delivery/provider/focus outcome unavailable, visual review required or crash boundary unresolved | `effect.status:unknown`; `status:unknown` if no prior completion, otherwise `partial`, preserving the unresolved step and `pending_effects`. |
| Earlier steps succeeded; later step rejected/preempted | `status:partial`; `completed_steps` names verified prefix; stopped step has its actual certainty; every remaining step is `unexecuted/not_dispatched`, effect `not_observed`, reason `prior_step_stopped`. No rollback. |

For a partial batch, aggregate effect copies the stopping step's effect, not the successful prefix. Aggregate dispatch certainty is unknown if any attempted dispatch extent is unknown, accepted if some are known submitted and none uncertain, otherwise not_dispatched. `window` is null for a multi-window batch. Receipt `state_delta` is the same bounded observation at `rev.final`; if unavailable, retain null facets and reasons instead of claiming post-action freshness.

Journal records: `intent` contains canonical request hash/identity; before each step's first possible mutation append/fsync `step_armed` with step ID; worker sends outcomes and broker appends/fsyncs `receipt`. The durable arm means “may have been dispatched,” not “was dispatched.” If broker cannot durably arm a step, no mutation occurs. If worker dies after arm, delivery is unknown unless independent retained evidence resolves it. An intent with no armed steps is safely not_dispatched after reconciliation. Uncertain recovery never sends input.

`action.get(session,generation,action_id,deadline_ms)` returns `pending|final`, receipt when final, and last durable arm information. Unknown IDs return `not_found` only when the complete retained journal for that generation proves absence. A previously live generation with an interrupted armed action returns unknown. Dedup key is `(generation,action_id)`: same canonical payload returns same pending/final action, no enqueue; changed payload returns `action_id_conflict`. Durable retention is the V2 receipt policy, 90 days T; expired generations reject acts and return `history_expired`, never a new live identity.

The caller's deadline stops further input and foreground verification; it cannot cancel input already submitted or a blocked disk fsync. If a final receipt cannot be made durable by response deadline, return `pending` plus action ID and close the mutation gate on journal failure; resolve via `action.get`. Never fabricate a durable receipt to satisfy a latency target. Late provider evidence may produce a durable `reconciliation` record linked to the original immutable receipt, with new revision/evidence and no dispatch. `action.get` returns the original outcome plus reconciliation, not a silently rewritten history.

**Unresolved effects block new mutation admission.** A timed-out/lost mutating RPC, uncertain input prefix, or healthy-but-unobserved submitted effect puts the session into `awaiting_reconciliation`, closes its gate and rejects later acts with the existing `pending_effects`. Read-only observe/wait/action.get continue. Resume requires both delivery quiescence (the original RPC completed or the input prefix and releases are confirmed, with no outstanding mutation RPC) and current evidence resolving the requested effect; issue a new epoch/lease and require a fresh observation. A read showing the old state alone cannot prove a delayed request will never execute. Killing an AT-SPI client does not cancel server-side work. If quiescence/effect cannot be established, the only automated recovery is session retirement/reopen; the worker never accepts a driver assertion that an in-flight RPC is cancelled.

`visual_review` is the separate, deliberate canvas case: after confirmed input-prefix processing and release, with no outstanding mutation RPC, record `awaiting_visual_review`. A subsequent model `act` may carry `reviewed_action_id` for that action and a `base_rev` from a newer full requested ROI observation. The adapter first calls internal `driver.review({session,generation,lease,action_id,observed_rev,deadline_ms})`. The broker validates that review-only state and observation ownership, writes a linked `driver_review` record, advances epoch and returns `{lease,control_epoch,rev}`. The adapter replaces epoch/lease in the new act before submission. This is a deliberate new action; the model still sees four tools. The old effect remains machine-unknown with `driver_assessed` supplemental evidence; no prior input is repeated. An act containing the marker without this successful review handshake is rejected. The field cannot unlock timed-out/unknown-delivery actions. If either uncertainty remains, use the reconciliation/retirement rule above.

## Failure and retry rules

### Fixed timers and bounds

All are T; V2-origin values are identified. Deadlines are monotonic and include queue time. Cancellation, gate closure, generation loss and provider health are serviced while awaiting results.

| Mechanism | Target rule |
|---|---|
| Batch and mutation queue | ≤8 steps, ≤10000 ms; ≤8 queued actions [V2]. Queue overflow rejects `queue_full`, no dispatch. Expired queued actions never execute. |
| Mutation gate | Permit lifetime 500 ms, broker renew every 100 ms [V2]. Read at every write/RPC, not just batch start. Epoch change cancels all old-epoch queued work and stops current work at the next write boundary. |
| Takeover/pause | Gate close ≤10 ms; tracked key/button release ≤30 ms [V2]. Release-only cleanup permitted under new epoch; unresolved cleanup closes admission. No resumption with old refs/lease. |
| Focus/position | Focus ≤100 ms, one activation; pointer ≤100 ms and ≤2 correction moves. Failure stops before click/key/text. |
| Semantic provider | Each ordinary AT-SPI/CDP request capped at 250 ms. AT-SPI: two consecutive breaches or one call exceeding 1000 ms marks degraded [V2]. Time out the caller at the smaller step deadline; monitor/cancel the child independently. A mutating timed-out call stays potentially in flight. |
| Degraded provider recovery | Read-only probes after 1, 2, 4 s, capped at 30 s [V2]. Recovery requires successful scoped read, new provider-instance refs and fresh observation; never repeats a mutation. |
| OCR | Cache miss computation ≤250 ms target for comparable small text crop [V2]; hard child task timeout 1000 ms, kill/restart child on breach. A large crop may return `budget_exceeded`; never block gate handling. |
| Input helper ack | 100 ms maximum within step; lost ack ⇒ delivery unknown. No blind resend. |
| Evidence settling | At most 100 ms visual-quiet wait for one-shot dialogs [V2]; semantic/event predicates finish immediately when conclusive. Quiet does not prove success; animation does not justify waiting forever. |
| Observation history | T event ring 1024 [V2]; event overflow invalidates relevant coverage and emits `resync_required`. Action-step dependencies are pinned during the batch; normal ring rollover does not erase in-flight guards. |
| Durable receipt | ≤10 ms after outcome known [V2]; disk error closes admission. Blocking durability cannot extend permission to act. |

All enumerated reasons: V2 `preempted, stale_rev, stale_generation, lease_expired, permit_expired, timeout, backend_unsupported, provider_degraded, queue_full, budget_exceeded, confirm_required`; plus `invalid_request, invalid_expectation, ambiguous_target, target_gone, transform_stale, focus_lost, unexpected_modal, input_rejected, input_partial, device_lost, isolation_violation, journal_unavailable, action_id_conflict, prior_step_stopped, driver_review_required, history_expired, worker_lost, resync_required, precondition_failed, precondition_already_satisfied`. Protocol validation failures use JSON-RPC invalid-params error before intent creation; once a valid action intent exists, it ends in a receipt or pending durable outcome.

### Retry matrix

| Failure | Permitted local behavior | Must surface/stop |
|---|---|---|
| Read-only observation/OCR request | Restart hung provider child; retry a read once within deadline using same scope. Cached OCR result may be reused only for identical input hash/key. | No good evidence ⇒ partial observation or predicate unknown, not success. |
| Unsupported route discovered before submission | Record rejected attempt; try next eligible route once. T maximum 4 distinct route attempts per step; exhausted/deadline ⇒ stop. | Cannot substitute a weaker expectation. |
| Pointer motion fails to reach absolute point before click | Bounded correction above; motion is state-setting and held buttons must be absent. | No click if position remains wrong. Never correct a drag this way. |
| Transport reply lost | Reconnect adapter; `action.get`; resend identical action ID only to retrieve deduplicated state. | Never generate a fresh ID to replay the same unknown effect. |
| Submitted click/key/text/semantic operation times out or returns an ambiguous error | Continue read-only reconciliation until remaining deadline. If expectation is proven, complete that step without another dispatch. | Missing effect is not proof of no effect; no automatic route fallback, even for replacement text. |
| Focus or unexpected modal changes during dispatch→verification | Release holds, retain evidence and submitted prefix. A profile-declared expected dialog transition is allowed only for that step. | Unknown/wrong-target outcome aborts; worker never dismisses a dialog to continue. |
| Helper loss, assigned device recreated, grab loss, X/bus/WM loss | Close affected gate, account for partial dispatch, retire generation, teardown via sessiond. | Reopen is a new generation and new driver observation, never replay. |
| Broker loss/permit expiry | Worker/helper stop writing and perform release-only cleanup. On restart broker reconciles journal and fences all old epochs. | No transparent continuation. Fresh lease, observation and explicit new action required. |
| Host/sibling input delivery or sentinel violation | Immediately fence every session; preserve diagnostics; teardown as needed to stop leakage. | STOP gate. No compatibility fallback may trade away isolation. |
| Model/API outage or spend policy refusal | Adapter reports waiting/driver failure. Applications stay intact; no new worker mutation. | Worker does not substitute another model or local executor. |

Text, Return, double-click, scroll, drag, invoke and clipboard paste are non-retryable after submission, including partial typing. State-setting operations can be safely satisfied without dispatch after observation; that is different from automatically reissuing an accepted operation. Profile-declared `confirm` actions must be authorized before arm/dispatch. Unsupported development confirmations and secrets fail closed.

## Build order and independent acceptance gates

Codex implements; Raj verifies from an independent shell harness, as in [R]. No calendar waits. Finish each gate before admitting its dependent capability. Tests produce a manifest hash, raw fixture logs, source-resolution evidence hashes, complete receipts, worker journal, fault injection trace, timings and host/sibling sentinel records. The fixture log/oracle is not readable by the worker or driver. Every verdict below is **NOT RUN — target gate**, not a claimed PASS.

### Build gates

| Gate / build order | Ground-truth check | Pass condition (T) / failure decision |
|---|---|---|
| W-A — bind, WM, broker gate and journal | Start isolated sessions via existing sessiond; bind workers; inspect process environments, `/proc` identities, private buses, Openbox bindings and X cookies. Bind duplicate worker, forged sequence, old generation/epoch and direct worker mutation request. | All invalid mutation requests rejected before fixture input; one worker/session. Existing sessiond acceptance remains passing. Any escape ⇒ STOP. |
| W-B — metadata, trees, capture and deltas | Instrumented app changes label, field, geometry, transient and canvas pixels independently. Harness separately reads X state and captures raw server pixels. Cover, minimise, hide preview, resize and reconnect. | Every returned state/crop matches server ground truth and transform; stale or unavailable data explicitly marked. A tree-less canvas never returns a misleading empty-tree success. No Xpra scaling in evidence. |
| W-C — unchanged and invalidation | After quiescence issue 5000 unchanged observes; count provider calls, capture reads, OCR invocations and hash work. Then inject tree-only, focus-only, pixel-only, selection and provider-disconnect changes plus ring overflow. | Unchanged calls invoke none of those expensive paths; ≤1 ms worker / ≤3 ms end-to-end p95. Every relevant injected change invalidates fast path; source failure cannot return true. Watchdog finds an intentionally missed semantic event. |
| W-D — Xvfb physical path | Instrumented input client records pointer/key events independently. Exercise chords, clicks, double-click, drag, horizontal/vertical wheel, ASCII, unmappable Unicode, native dialog focus and clipboard selection. Inspect event delivery and resulting widget values. | Correct session events and exact field contents, no held key/button after completion/cancellation. uinput request rejected. Semantic-only Calculator success cannot satisfy this gate. |
| W-E — verified batch and freshness | Rename through real Thunar GUI using the batch; independently list/stat resulting directories. Inject unexpected modal, stale selection/ref, geometry change, provider restart and transform change at every step boundary. | Exact final names by filesystem oracle; no step after a failed/unknown predecessor. 1000 stale dependency requests, zero dispatch. Later dialog bindings valid; ambiguous entry stops. Unrelated damage alone does not reject a valid batch. |
| W-F — semantic, OCR and provider fallback | Disconnect/blackhole AT-SPI in A while B works. Test hung mutation, explicit pre-submission unsupported error, delayed effect, partial text, stale OCR cache and hidden window. | A can complete a newly planned physical/OCR GUI workflow; B continues. No weaker predicate substitution, repeated mutation or duplicated text. Worker contains no model call. A mutating provider timeout yields unknown/in-flight evidence and stops. |
| W-G — Xorg evdev path | Preserve helper devices from session start; prove Xorg grabs with a competing EVIOCGRAB attempt returning busy before input; confirm explicit device nodes, enablement, route and no physical node use. Use fixture that ignores core XTEST deliveries but accepts input from its assigned XI source device; log fixture policy explicitly. | Session fixture receives assigned evdev input; Xvfb path demonstrably fails that fixture; Xorg passes. This is a controlled XTEST-rejection policy fixture, not a claim that all apps can detect XTEST. Any missing exclusion/grab/sentinel ⇒ no input admission. |
| W-H — crash, pause and deduplication | Kill broker/worker/helper before intent fsync, after intent, after arm, after input, after effect and before final receipt fsync. Lose replies, reuse action ID with changed payload, fill disk, expire permit and trigger takeover mid-text/mid-drag. | Independent effect counters show zero replay/duplicate effects. Receipt certainty matches each cut, unknown where required. No old-epoch action resumes; cleanup releases holds or session is fenced. Broker restart reopens no gate automatically. |
| W-I — two-driver task acceptance | Two Codex or API model drivers create/rename in isolated Thunar sessions while human types into host editor, host F2 still bound. Record physical keyboard and pointer route in both sessions. Repeat without host binding. | 3 consecutive paired model-driven runs correct by independent filesystem oracle (V2 D1 target). Publish failures, model IDs, tokens, spend, turns, vision calls and timing splits. This is a demonstration gate, not G1 qualification. |
| W-J — release-scale worker qualification | Run isolation, provider-fault, takeover and latency suites at admitted session counts; compare deterministic isolated versus paired makespan. | V2 G1/G2 criteria below; isolation defect ⇒ STOP. Fast-path budget miss follows V2 S-CAP/§2.1 rule, never becomes a fabricated measurement. |

A sibling observer records tagged input in its own test client; host raw-input listener and host editor output independently record leakage. Physical human keystrokes are recorded separately from tagged virtual-device traffic. For XTEST, test-window tokens/expected event sequences establish session delivery; do not pretend XTEST supplies evdev source IDs. For evdev, use kernel device identity plus XI source ID. Intentional kernel-grab tests use setup/diagnostic permissions only; production drivers receive no raw-device access.

### Required negative controls and operation oracles

| Test | Oracle outside the worker | Required result (T) |
|---|---|---|
| Accepted input, app ignores it | App counter/value unchanged, independent event log shows delivery | Accepted dispatch with unobserved effect; no retry. Never verified from flush/ack. |
| Delayed rename past timeout | Directory appears only after receipt timeout | Original receipt retained; later reconciliation linked; exactly one rename attempt by log. |
| Text partial prefix, wrong focus, duplicate text risk | Widget's independent recorded value and focused-widget ID | Receipt unknown/contradicted as evidence permits; zero automatic append/paste retry. |
| Click opens dialog, key changes selection, drag repositions object, scroll changes visible content | Test app exposes audit-only widget/object state to harness; no access from worker | Receipt's declared postcondition equals state; wrong predicate/target cannot pass. |
| OCR cache and quality | Known rendered strings including confusables, blank crop, low confidence, crop movement and mask change | Changed cache keys recompute; low-confidence/negative-only evidence cannot prove requested success. Report false-positive/false-negative counts, not “OCR works.” |
| Tree disconnected but pixels updating | Separate app framebuffer capture | Pixel-only observation remains live; metadata/tree coverage explicitly degraded. |
| Cross-session single-instance launch | `/proc` environment + each private app window/working directory | New Thunar/browser window belongs only to intended session; no resident host/sibling process handles it. |
| EGL regression | Pinned Spike C launch recipe and renderer/VRAM diagnostics; independently inspect render artifact | EGL remains a side channel; dummy Xorg never acquires DRM master or unmasks KMS nodes. Report Cycles as blocked unless its separate qualification passes. No new 8.5× assertion. |
| Shared VNC remote desktop | Two viewers intentionally connect to one remote desktop | Remote contention demonstrated and labelled; independent remote sessions required for positive parallel-CAD evidence. |
| Early stop in batch | Fault injected into each step position of an 8-step batch | Verified prefix, accurate stopped step, remaining suffix never dispatched; final delta describes actual resulting state. |

### Qualification numbers to publish, not infer

All in this table are T from V2 unless marked otherwise. Run dispatch benchmarks with providers warm and no first-use process launch; measure provider recovery separately. Count `execve` over action dispatch intervals, including provider/helper children: zero in normal dispatch. Provider crash recovery may restart a child, must be labelled and excluded from that fast-path sample rather than hidden.

| Item | Target / sampling rule |
|---|---|
| Paired Thunar correctness | 30/30 paired successes, independent final directory-name oracle [V2 G1]. Keep the failed historical baseline visible. |
| Isolation | Zero host/sibling deliveries over 10000 tagged events, including host typing, USB hotplug, virtual device destroy/recreate into new generations. Test each exclusion layer with the other removed under a controlled diagnostic setup and sentinel. Any delivered escape fails. |
| Deterministic concurrency | `(isolated A + isolated B) / paired makespan ≥1.5`; model-driven performance reported separately [V2 G1]. |
| Dispatch and verification | ≥5000 primitive actions at N=1 and N=2; request→input ≤3 ms with correct focus, ≤10 ms with focus work; helper-write→client ≤5 ms; event→cache ≤10 ms; event-class predicate decision ≤20 ms after causal event arrival [V2]. |
| Text and local overhead | 100 mapped characters ≤50 ms default profile; local event-verified overhead ≤50 ms, excluding model/application time [V2]. Report fsync/arming overhead explicitly. |
| Sensing | Cached delta ≤5 ms; OCR-class comparable crop ≤250 ms; capture targets above; 5000 captures for S-CAP [V2]. |
| Turn and token reduction | Thunar ≤50% of a newly passing isolated baseline's driver turns and tokens, equal correctness; zero vision calls is the Thunar target [V2]. The old failed task is not this baseline. |
| Takeover and capacity | Gate closure ≤10 ms p95; test 200 takeovers; N=4 p95 control latency ≤2× N=1 if N=4 is admitted [V2]. Report human typing impact separately. |
| No automatic model inference | Worker/providing children make zero model requests; only Codex/API driver adapter invokes models. Manifest records actual driver/model and settings. |

### Results note template for the implementer and verifier

Use [R]'s evidence-first structure:

```text
# ark-worker act path — per-session observation and verified input
- Spec: exact commit/hash of this document and V2.
- Host/date/build: actual values; manifest hash; sessiond baseline version.
- Code: paths and commits; development exceptions still present.
- Drivers: Codex/API model IDs, turns, tokens and spend per run.

## Acceptance test — VERDICT: NOT RUN / PASS / FAIL / BLOCKED
| Item | Result | Independent evidence |
| ...each build gate, negative control and operation... |

## Measured timing and counters
Actual count, median/p95, sample scope and cold/warm labels; failures retained.

## Defects found and fixed
Separate spec bugs, harness bugs and product bugs; show oracle rechecks.

## Simplifications vs V2
Development same UID/root helper, lifecycle attach bridge, unsigned profiles,
plain action JSONL, absent systemd/namespace/credential infrastructure.
Do not mark a missing mandatory gate as a simplification that passed.

## Cleanup
No marked session/worker/provider/helper processes, held buttons/keys,
devices, staged confs or stale registry entries. Host PID/starttime and
responsiveness unchanged. Preserve journal/evidence for review.
```

## Primary mechanism references

These establish mechanisms, not ArkDrive performance or acceptance results:

- [XTEST extension library](https://xorg.freedesktop.org/releases/current/doc/libXtst/xtestlib.html): simulated key, button and pointer requests. ArkDrive independently verifies application effects.
- [Linux uinput documentation](https://docs.kernel.org/input/uinput.html): virtual device creation and event writes by the owning process.
- [Debian evdev driver manual](https://manpages.debian.org/trixie/xserver-xorg-input-evdev/evdev.4.en.html): explicit device paths and `GrabDevice`; defaults are not proof a grab is enabled.
- [MIT-SHM extension](https://www.x.org/releases/current/doc/xextproto/shm.html): shared-memory images and extension capability checks; no assumption that shared pixmaps exist on every server.
- [AT-SPI EditableText replacement](https://gnome.pages.gitlab.gnome.org/at-spi2-core/libatspi/method.EditableText.set_text_contents.html): exposed-field replacement; ArkDrive reads back text rather than equating the method's return value with task completion.
