# ArkDrive Agentic Window Manager — Architecture Plan

- Source: UCGS design session (GPT-6.1 Sol, High, Work mode), fresh chat in the Raj project, Nov 9, 2026. Chat: https://chatgpt.com/g/g-p-6ab48a156898819195156a018ee4d79f-raj/c/6af276a1-ebd4-8329-bc27-54ea9584076e ("Design agentic window manager")
- Brief given to the designer: ArkDrive's measured state (ladders, receipts, Virtuoso bench 159s/17 actions/33,171 tokens; Thunar bench 99s/16 actions/22,102 tokens with F2 focus-theft failure; agent-desktop trial numbers incl. the Thunar AT-SPI bus-drop; OCR 0.17s vs vision 5.3s; region hash 1.6ms; single-driver contention; Xvfb/Xephyr portal lanes).
- Status: PLAN ONLY. Awaiting ArK's call on the 3 decisions at the end before any code.

## Recommendation (up front)

Build one isolated display session per agent, managed by an ArkDrive broker and presented through Xpra on the existing human desktop. Start with Xvfb and a minimal WM; qualify an Xorg dummy-driver backend with explicitly routed uinput devices for applications that reject XTEST. Preserve the same API for a later Wayland backend.

The critical boundary is the agent session, containing an application and its dialogs, or a small cooperating application group. Each session gets independent pointer and keyboard-focus state. Physical input serializes within a session; different sessions execute concurrently.

Note from the designer: the measurements supplied are the baseline. New latency figures are acceptance targets, and effort figures are engineering estimates, not measured results.

## 1. Architecture options

### A. Extend an X11 WM with MPX
Ordinary workspaces do not provide input isolation. A credible implementation needs XInput2/MPX: a master pointer/keyboard pair per agent, ClientPointer per client, device-specific keyboard focus, agent input devices routed to their master pair, physical devices left on the human pair. Problem: legacy application behavior. X.Org documents that core grabs can suppress other devices interacting with the same client (popup menus are a concrete example). Every app, input backend, and grab path needs qualification. All clients still share one X server and its broad trust domain.
- Performance ceiling: minimal presentation overhead, direct X events and XDamage, potentially concurrent device streams. Compatibility determines useful concurrency.
- Effort: 4–8 engineer-weeks prototype; 12–24+ for qualification.
- Assessment: useful research branch, uncertain foundation for the main product.

### B. GNOME Shell extension plus Mutter integration
A Shell extension can do agent labels, window grouping, overview, pause controls, presentation. It cannot by itself turn GNOME workspaces into independent input domains. GNOME's remote-desktop stack (PipeWire, libei, Mutter APIs) provides capture/input mechanisms, but multiple remote-control sessions must not be assumed to provide independent focus. Authoritative compositor policies for seats, ownership, shortcuts, Xwayland domains likely mean Mutter changes or separate nested sessions.
- Effort: 2–4 weeks presentation alone; 16–32+ for actual independent control and qualification.
- Assessment: plausible future host shell; not the shortest route to parallelism.

### C. Custom Wayland compositor using wlroots
Independent compositor-owned seats, each with its own focus, cursor, authorized surfaces. Physical devices feed only the human seat; agent input enters a designated seat via private API or libei. X11 apps get one Xwayland server + XWM per agent domain (sharing one Xwayland would retain shared X11 state/trust). Human interaction: compositor renders agent windows alongside human windows and owns takeover before input delivery.
- Performance ceiling: strongest long-term control over events, routing, buffer sharing. GPU readback, apps, and models still dominate many tasks.
- Compatibility risk: NVIDIA synchronization, toolkit seat behavior, popups, IME, relative pointers, legacy GLX. Xwayland is a full compatibility server, not a guarantee of native-Xorg equivalence.
- Effort: 8–12 weeks narrow prototype; 24–48+ for a dependable daily desktop.
- Assessment: strongest eventual compositor architecture; too much initial scope for a small team.

### D. Per-agent display servers with one presentation layer (RECOMMENDED)
Each agent receives: a private X server; independent focus and pointer state; its own input queue; private session/accessibility buses; application processes, browser profiles, clipboard selections. Xpra presents these sessions on the existing XFCE desktop: desktop mode first (each session = one tile), seamless mode later (individual windows presented). Run a minimal WM (Openbox) in desktop mode; do not run it alongside Xpra's competing WM role in seamless mode.

Backends:
- Xvfb — first parallel MVP, simple offscreen X11. Limitation: does not consume evdev devices; host ydotool is not a valid fallback.
- Xephyr — visible nesting and debugging. Parent-input and extra presentation interactions.
- Xorg + dummy video — qualified evdev/uinput route. Distro/device permissions and graphics acceleration need testing.
- Nested Wayland + per-session Xwayland — later GPU/native-Wayland backend. More integration work.
- Performance ceiling: genuine concurrent input across servers; extra processes, buffers, copies, possibly encoding.
- Compatibility risk: lowest disruption to current X11 ladders. GPU apps and remote VNC topology remain explicit gates.
- Effort: 2–4 engineer-weeks for a trusted two-agent MVP; ~8–16 through an initial qualified X11 release.

## 2. Trade-off matrix and recommendation

| Criterion | A: Shared X11/MPX | B: GNOME/Mutter | C: Custom wlroots | D: Per-agent servers |
|---|---|---|---|---|
| Independent input | Possible; legacy grabs complicate | Extension alone insufficient | Explicit compositor policy | Separate server state |
| Human desktop disruption | WM/device changes | GNOME migration | Desktop replacement | XFCE remains |
| Existing ladders | Need device-aware changes | Mixed Xwayland/native paths | New backend integration | Mostly session scoping |
| Event-driven sensing | X events/XDamage | Mutter integration | Direct compositor events | X events/XDamage per server |
| AT-SPI blind spots | Pixel fallback required | Pixel fallback required | Pixel fallback required | Pixel fallback required |
| XTEST rejection | Device routing required | Compositor/libei path | Seat-native path | Qualified evdev backend |
| GPU potential | High | High | High after qualification | Backend-dependent |
| Isolation | Weak shared X trust | Depends on domains | Strong with correct policy | Strong with OS containment |
| Small-team suitability | Risky qualification | High scope | Highest scope | Best initial fit |

Choose D, and make the display backend replaceable. That obtains the property needed now — independent execution — without first rebuilding a desktop environment.

Priorities set by the measurements:
- Fix ownership first. The F2 failure is an input-domain failure. Thunar should run inside a session whose WM does not bind F2; agent input should never traverse host XFCE.
- Exploit working semantics. Calculator's four ~0.2s semantic clicks demonstrate a valuable fast path. They do not establish that another GTK app's modal will expose a usable tree.
- Keep observation independent of accessibility. Thunar disappearing from AT-SPI must degrade one provider, not blind the entire session.
- Reduce model work before chasing microseconds. Virtuoso averaged 9.35 s per action across the full run; Thunar averaged 6.19 s, with failure. Saving 10 ms per dispatch saves only 0.17 s over 17 actions.
- Separate input concurrency from compute concurrency. Independent input domains do not create additional GPU capacity, model throughput, remote CAD sessions, or license seats.
- The OCR sample was ~31x faster than the vision call (5.3 / 0.17). Replacing one applicable vision call saves ~5.13 s in that sample. The byte-exact result belongs to that tested crop; not a universal OCR guarantee.
- For identical independent jobs, two sessions have a theoretical 2x throughput ceiling before resource contention. Re-establish a successful Thunar baseline before using it in throughput comparisons.

## 3. Recommended design in detail

### Components
- ark-sessiond — creates private sessions, display servers, buses, processes, resource limits, restart generations.
- ark-broker — authentication, capabilities, leases, ordered dispatch, cancellation, durable receipts.
- Per-session worker — owns that session's X11, AT-SPI, CDP, and input connections.
- Display backend — window lifecycle, geometry, focus, damage, capture, input primitives.
- WM policy — transients, modal targeting, focus rules, stacking, shortcut allowlist.
- Sensing service — state cache, semantic diffs, dirty regions, capture, hashes, OCR, vision escalation.
- Human console — observe, pause, takeover, resume, kill, cursor overlays, progress.
- Device helper — narrow privileged creation/access for designated virtual devices.
- Verifier/audit service — evaluates postconditions; records commands, outcomes, evidence, interventions.

These are logical boundaries. The MVP does not need nine daemons: one broker with per-session workers is reasonable; isolate potentially hanging accessibility calls in restartable workers. Keep connections persistent — repeatedly launching command-line tools adds avoidable startup and discovery work.

### Window namespace and ownership
Opaque, generation-bound handle: `s-A/g3/w7` (session A, generation 3, window 7). XID, title, PID, app ID, or AT-SPI reference is supporting metadata, never sufficient authority. Maintain a window family: toplevels, transients, menus, override-redirect popups. Use WM_TRANSIENT_FOR and client relationships, but do not depend on perfect metadata — an unexpected dialog inside the session must still be observable.

Every session has private: DISPLAY, XAUTHORITY and runtime sockets; D-Bus session and accessibility buses; browser profile and debugging endpoint; clipboard selections and working directory. AT-SPI uses a separate accessibility bus discovered through the session bus — changing only DISPLAY is insufficient isolation. Test single-instance behavior explicitly: a new Thunar or Chromium launch must not reconnect to a resident process in another session.

Observe and address families; authorize physical input for the whole session group (raw keyboard shortcuts can change windows or create new ones). A family-only capability can authorize observation and validated semantic operations, but cannot honestly promise hard confinement for arbitrary keyboard input. If two agents need simultaneous physical control, allocate two sessions. Existing human-desktop windows should be restarted/reopened inside a session; transparent migration between X servers is outside the initial design.

### Input arbitration
One ordered mutation queue per session, no global input queue. All mutation routes — including CDP and AT-SPI — pass through the lease gate (otherwise a paused agent could keep changing the app through semantics while the human controls its keyboard). Before dispatch: validate capability, session/window generation, lease epoch, deadline, action ID; resolve the active modal target; validate geometry and semantic-reference revisions; establish and verify session-local focus where necessary; dispatch through a supported backend; verify the requested effect independently. Focus checks reduce races; they do not make asynchronous X11 apps transactional. Unexpected focus changes or grabs produce an uncertain or failed receipt.

The corrected F2 route: Agent → session queue → session-local focus → session keyboard → Thunar. Host XFCE never receives it. Human typing through the viewer is different: host shortcuts may intercept keys unless takeover captures/forwards them or provides an explicit "send F2" control.

### Ladders, capability-aware
Preserve the action interface; scope every rung to a session. CDP handles Chromium page content. AT-SPI handles exposed controls when the provider and target are healthy. Verified shortcuts and coordinate input handle dialogs and canvases. OCR/vision locates or interprets a target; an input backend performs the action. Clipboard paste stays inside the session and implements actual selection transfer. xdotool and XTest-based cua are not independent failure domains just because they are different executables. Do not traverse every rung on every action: cache provider capabilities and health per surface. When Thunar's accessibility disappears, bypass that provider promptly and probe recovery with backoff. After a timeout with unknown dispatch outcome, reconcile before another attempt — otherwise fallback can create duplicate folders, text, or clicks.

### Where uinput fits
Xvfb cannot consume evdev input. Its supported MVP route is session-local XTEST. If uinput is requested, return backend_unsupported; never fall back to global host ydotool. For the full X11 backend, qualify: Broker → per-session uinput device → designated evdev node → session Xorg input driver → application. uinput creates virtual devices and delivers events to consumers; it does not know which agent or window should receive them. Xorg's evdev driver supports explicitly configured device paths and keyboard/pointer roles.

Routing policy: install host exclusion rules before device creation; configure host Xorg to ignore ArkDrive virtual devices; disable automatic device addition in session Xorg; configure only that session's assigned virtual devices; expose no physical keyboard/mouse nodes to agent sessions; enable agents only after a startup test shows zero host/sibling delivery. This is an early qualification spike. Xpra advertises several pointer backends including uinput, but that does not establish complete keyboard support or correct isolation in the installed configuration. If this route proves unreliable, retain Xvfb for qualified apps and use a nested compositor backend for rejecting surfaces. Do not sacrifice isolation to preserve a fallback rung.

### Human preemption
States: AGENT → QUIESCING → HUMAN → PAUSED → AGENT. On takeover: increment the control epoch immediately; reject new agent mutations; cancel queued operations; release tracked pressed keys/buttons; fence the backend and account for dispatched operations; enable human input. Target: under 50 ms p95 to close the local dispatch gate (that does not mean an already-issued CDP command, VNC action, or application operation has stopped). Show outstanding effects during QUIESCING. Old-epoch commands never resume automatically; resumption requires a fresh observation and lease. Enforce ownership in the server-side viewer input handler — keyboard, pointer, focus, clipboard, and other mutations. A read-only-looking viewer is insufficient. In observe mode: incidental human motion does not reach the session; agent activation requests do not focus host windows; dialogs appear in their tile or as nonactivating proxies; agent cursor movement never moves the host pointer. Provide an independent administrative socket/watchdog. On X11, a global escape shortcut cannot be treated as ungrabbable; a compositor you own can provide stronger host-level override.

### Event and sensing plane
Subscribe per session to: X create/map/unmap/destroy/configure/property events; focus, active-window, transient, selection changes; XDamage on relevant drawables; AT-SPI object/text/focus/property events and provider liveness; CDP navigation, DOM, accessibility events where supported; disconnects, restarts, stream gaps. XDamage identifies modified drawable regions; it does not establish task success. Composite-backed contents help capture obscured windows, but evidence still requires pixel access.

Observation pipeline: return cached window/modal/focus state and targeted semantic diffs; capture relevant regions from the source session, never the scaled preview; compare hashes to suppress unchanged evidence (capture cost remains); OCR applicable text; escalate to vision for unresolved spatial/canvas interpretation; evaluate an explicit postcondition.

Starting tuning values: damage coalescing 8–16 ms; visual quiet window for one-shot dialogs 100 ms; semantic response budget 200 nodes or 16 KiB with continuation; accessibility circuit-breaker budget 250 ms, then degrade provider. These are heuristics to tune. Blinking carets, animations, VNC updates, and renders need region masks and task-specific predicates. Keep a low-rate watchdog/resync for missed events. Event-driven sensing removes routine screenshot polling; it does not eliminate pixel inspection or remote-state verification.

### Wayland and application compatibility
- Chromium: initial X11 session = dedicated profile/CDP, native-dialog fallback. Future Wayland: CDP remains; compositor handles native UI.
- Thunar/Calculator: X11 = AT-SPI plus X events/OCR/input. Wayland: AT-SPI remains; compositor replaces X-specific sensing/input.
- Virtuoso through VNC: treat the viewer as a canvas. Wayland: native or Xwayland viewer; remote application unchanged.
- Direct Virtuoso: native X11 qualification. Wayland: separate Xwayland session; qualify fonts, GLX, dialogs.
- Blender: qualify viewport/render backend explicitly. Wayland: native or Xwayland, driver-dependent.

Accessibility and input injection are separate mechanisms. The tested CLI's refusal to handle web pages is a tool policy, not proof that browsers universally lack accessibility. Keep CDP as ArkDrive's browser provider. XTEST is not a universal native-Wayland injection API. Host uinput still requires explicit compositor device-to-seat routing. A compositor you control can accept private actions or libei events and apply authorization before normal seat delivery; libei supports distinguishing emulated input inside the compositor.

Two compatibility limits: (1) VNC — two local viewers connected to the same remote desktop still share its remote pointer and focus. Multi-Virtuoso requires independent remote sessions, plus appropriate CAD file ownership and license capacity. (2) Graphics — a virtual framebuffer commonly uses software OpenGL. Xpra client-side OpenGL does not accelerate Blender's rendering. Qualify VirtualGL/EGL or a GPU-backed nested backend on the RTX 3060.

### Latency and capacity budgets
Targets (p95) vs measured baselines: broker validate/enqueue ≤2 ms; focus validation + backend acceptance ≤10 ms; lifecycle/damage producer → cache ≤20 ms; cached targeted observation ≤10 ms; fresh native crop at 1080p ≤30 ms (excludes OCR/vision); AT-SPI snapshot measured 130 ms; semantic click measured ~200 ms; OCR measured 170 ms / vision 5,300 ms; hash comparison ~1.6 ms. Do not sum these targets into a claimed task latency.

Frame math at 1920x1080x4: one frame 8.29 MB; four sessions with three buffers each 99.5 MB excluding app/server/GPU allocations; four full-frame 60 Hz copies 1.99 GB/s per copy pass. Start with two active sessions, then qualify four. Suggested presentation limits: selected session up to 30 fps, thumbnails up to 5 fps on change, invisible sessions no preview stream. Source-side verification remains active.

"Close to the kernel" helps routing and bypassing a particular XTEST rejection path. It does not remove model generation, application event loops, GPU readback, or VNC delay. Persistent IPC, bounded observations, events, and fewer model turns are the first speed investments.

## 4. Agent-facing contract

JSON-RPC 2.0 over a Unix-domain socket, length-prefixed messages. Authenticate peer credentials and scoped capabilities. A subscription connection carries events. Pixels move as bounded frame references / shared memory, never image bytes embedded in JSON. Remote access goes through an authenticated TLS gateway.

Core calls:
- session.create — launch profile/resources/backend requirements → session ID, generation, capabilities.
- window.list — session scope → window families and stable handles.
- window.claim — family and operation scope → capability; physical control additionally requires session-wide input authority.
- window.observe — handle, revisions, subtree/crop, budgets → state, provider health, evidence.
- window.subscribe — event filters and cursor → ordered events or explicit gap.
- window.act — action ID, lease/epoch, preconditions, intent, deadline, postcondition → receipt.
- window.wait — predicate and starting revision/sequence → evidence or timeout.
- action.get — action ID → durable outcome after reconnect.
- control.pause / takeover / resume — human/admin authorization → transition and new epoch.
- session.stop — graceful/kill → stopped generation and cleanup result.

Example window.act params: session "s-A", window "s-A/g3/w7", lease (opaque capability), epoch 12, action_id "rename-004", precondition {geometry_rev 31, modal null, target_ref "tree8:folder17"}, intent {kind "key", keys ["F2"]}, expect {kind "dialog_present", role "rename"}, timeout_ms 2000.

Receipt v2 (extends the existing {version, phase, dispatched, effect}): adds session, generation, window, action_id, epoch, route, attempts[] (each route + accepted), focus {before, after}, events {from, through}, timing_ms {queue, dispatch, verify}, and effect {status, predicate, evidence[]}. Illustrative example in the design chat: rename action, route keyboard.xtest, effect verified via evidence frame:418 + ocr:92 — noting that receipt verifies the rename dialog opened; a separate action must verify the final folder name.

Required semantics:
- Distinguish definitely-not-dispatched, accepted, and unknown delivery. Preserve existing boolean compatibility via an additional dispatch_status if necessary.
- effect.status = verified | not_observed | contradicted | unknown. Reasons include preempted, stale_target, timeout, backend_unsupported.
- Deduplicate action IDs within a session generation.
- Preserve every attempted route and its outcome.
- Never treat a server flush as proof the application processed input.
- Never promise exactly-once effects across a crash between journaling and delivery.
- Version semantic refs and coordinate transforms.
- Coordinates must distinguish source-window content pixels, desktop pixels, preview coordinates, CDP CSS pixels, decorations, and remote-VNC coordinates.

Events: versioned envelope {version, session, generation, seq, monotonic_ns, kind, window, payload}. Example kind provider.health: {provider atspi, state degraded, reason application_unreachable, fallback_available [pixels, keyboard]}. Other kinds: window lifecycle, modal/focus changes, damage, semantic changes, control transitions, action completion, backend restart, resync_required. Ordering is per session. Monotonic clocks for durations; wall-clock timestamps for audit correlation.

## 5. Security and safety model

Display isolation, capability enforcement, and OS containment are separate boundaries.
- Agent → broker: authenticated peer; scoped capability, generation, operation, expiry, epoch.
- Agent → display: no direct X socket/cookie, accessibility bus, CDP endpoint, or device access.
- Session → session: private servers/IPC; separate UID or effective sandbox before mutually untrusted jobs.
- Session → host files: private home/profile/workdir; explicit mounts and output export.
- Kernel input: narrow helper; fixed device templates; no general input-group membership.
- Presentation → host: trusted viewer; no automatic activation or external URL opening.
- Forwarding: clipboard, files, audio, printing disabled unless deliberately enabled.
- Network: no X TCP listener; private debugging endpoints; authenticated gateway.
- GPU/remote CAD: explicit shared-resource and trust dependencies.

Separate X servers under the same unrestricted UID prevent accidental input interference; they are not a strong hostile-code sandbox. Use separate system users or sandboxed mount/PID/network environments and protected runtime directories before broader access. Do not expose the host home, SSH agent, keyring, or session bus by default. An allowed terminal or file manager has ambient filesystem/network powers — a window capability alone cannot confine those effects. Applications sharing one X server remain within the same X11 trust domain. Use Xpra's read-only and forwarding controls, but enforce ownership in ArkDrive's server integration; disable arbitrary remote command launching and unnecessary subsystems.

Audit record: actor and capability identity/hash; session/window generation and lease epoch; request, selected backend, and every attempt; queue/dispatch/verification timing; dispatch certainty and effect evidence; human interventions and termination outcomes. Content-addressed evidence with configurable retention. Redact typed secrets; protect recordings/screenshots containing private content. Hash chaining helps detect edits only when a trusted checkpoint is retained. Pause and kill must work without a model call, even when accessibility or vision workers hang. A shared GPU/kernel remains a shared failure and security dependency.

## 6. Phased implementation plan and benchmarks

Estimates are engineer-weeks. Two engineers can overlap some work, but input/GPU qualification remains a dependency. Phases 0–2 total ~8–16 engineer-weeks.

### Phase 0 — Instrumentation and backend qualification: 1–2 weeks
Instrument queue, focus, backend acceptance, application effect, capture, OCR, vision, model time, tokens, retries. Pin versions, resolution, warm/cold state, VNC topology. Three probes: (1) private Xvfb plus private accessibility bus; (2) two Xorg dummy sessions with explicit evdev routing and host exclusion; (3) Blender renderer identification and GPU-path measurement. Repeat the existing Virtuoso and Thunar benches at least ten times, retaining failures. Reproduce the accessibility outage without assuming its root cause. Gate: identify working session profiles; explicitly classify uinput and Blender acceleration as qualified or blocked.

### Phase 1 — Smallest parallel MVP: 2–4 weeks
Build: two Xvfb sessions with minimal Openbox configurations; private buses, profiles, working directories; session-scoped existing ladders; independent broker queues; read-only Xpra tiles; server-enforced pause/takeover and epoch rejection. Run two agents while the human types in a host editor.

Bench (all must hold):
- Two Thunar create/rename tasks → correct distinct final folder names.
- Thunar modal breaks AT-SPI in session A → A completes via fallback; B continues.
- Host F2 remains bound to Application Finder → agent F2 opens only its session's rename dialog.
- Simultaneous coordinate/key traces → independent cursors/focus; no cross-delivery.
- Human takes session A → B continues; old-epoch A actions rejected.
- Host typing throughout → agent actions never redirect human input.
Final verification via an independent filesystem oracle; agents still perform the actual GUI task.

Acceptance targets: 30/30 paired Thunar task successes; zero host/sibling deliveries across 10,000 tagged test events; correct takeover receipts and no stale-command replay; at least 1.5x concurrency ratio for two comparable non-GPU-bound deterministic workloads, computed as (isolated T_A + isolated T_B) / paired makespan. Report model-driven runs separately (API scheduling can hide genuine input concurrency). Semantic Calculator alone is insufficient proof: exercise physical keyboard/pointer paths in both sessions.

### Phase 2 — Compatible actuation and reliable sensing: 5–10 weeks
Lifecycle/damage-driven sensing, targeted semantic/CDP diffs, native crops, OCR fallback, circuit breakers, effect verification, qualified evdev input. Extended benches:
- Virtuoso: independent remote VNC desktops; A opens fa, Fit, verify; B opens alu8, Fit, verify. Include human activity and a local GUI side task.
- Negative VNC control: two viewers on one remote desktop must expose remote contention, not pass as isolated.
- Chromium: CDP task plus native file chooser while another session holds a GTK modal.
- XTEST rejection fixture: ignores XTEST, accepts only its assigned evdev keyboard/pointer.
- Failure injection: hung accessibility, disconnect/reconnect, stale refs, missed events, crash after dispatch.
- Blender: viewport interaction, dialogs, render, and a dark-render/relight workflow where planner behavior is evaluated separately.
Measure at N=1/2/4. Initially require four-session p95 local-control latency within 2x the one-session value, preserving human responsiveness. Target 50% fewer tokens against a newly passing Thunar baseline at equivalent correctness; keep the historical 22,102-token failed run visible. Do not promise the same reduction for Virtuoso's canvas-heavy workload. Report success counts, median/p95 wall time, actions, input/output tokens, vision calls, provider outages, retries, wrong-target events. Use thousands of primitive actions for dispatch percentiles; thirty task runs alone are insufficient.

### Phase 3 — Desktop presentation and containment: 4–8 weeks
Seamless windows, grouped dialogs, cursor overlays, resource admission, stronger containment, persistent audit/evidence. Switch seamless sessions to Xpra WM ownership rather than running a competing Openbox instance. Bench: two-hour mixed N=1/2/4 workloads with Chromium, Thunar, VNC, Blender; human editing; rapid dialogs; hidden previews; detach/reconnect; resize; pause/kill/restart injection. Publish CPU, RSS/PSS, GPU memory, capture bandwidth, event backlog, human input latency. Test denied cross-session file/socket/device access. A display-server crash invalidates its generation and normally loses GUI clients; recovery means restart/reopen, not transparent application migration.

### Phase 4 — Optional Wayland backend: 8–16+ weeks
One nested compositor per session, one conventional agent seat, its own Xwayland; broker contract unchanged. Native capture/input and session-level compositor override. A nested compositor cannot make an XFCE/X11 host shortcut ungrabbable; retain the independent administrative path. Promote this backend only after repeating compatibility benches and demonstrating better graphics, capture efficiency, or required native-Wayland support. Replacing the human desktop is a separate project.

### Phase 5 — ArkDrive OS: bootable, owned Linux stack: 16–24 engineer-weeks

- Extension source: UCGS session (GPT-6.1 Sol, High, Work mode), fresh chat in the Raj project, Nov 9, 2026. Chat: https://chatgpt.com/g/g-p-6ab48a156898819195156a018ee4d79f-raj/c/6af284fb-bc24-832b-87b7-c4e1c7016114 ("Extend ArkDrive OS Plan"). The model edited the attached plan file directly; the extension is a pure insertion, Phases 0–4 unchanged.

Ship a Debian 13 (trixie), amd64 appliance/workstation image: one human presentation desktop, the existing broker, and isolated agent display sessions created on demand. This extends option D; it does not replace the broker contract, input ladders, verification model, or Codex/API session drivers. No local executor model, inference service, or model-weight download is required. GPU memory belongs to applications, capture, and presentation.

Dependency: qualified Phases 0–3. Phase 4 remains optional: ArkDrive OS v1 uses an owned X11 presentation configuration, not a new compositor. Image-building and boot/device spikes can overlap Phase 2; a bootable ISO is not evidence that the earlier compatibility gates passed. Estimates below are engineering estimates; all new timing/capacity numbers are targets, not measurements.

#### Base distribution and shipped stack

**Choose Debian 13 stable, pinned by codename and package manifest.** Take Debian kernels, systemd, Xorg, Mesa/GLVND, firmware, and security updates; maintain ArkDrive packages and integration policy. Do not maintain a kernel fork, a display-server fork, or a general-purpose package archive.

| Candidate | Fit to this plan | Decision |
|---|---|---|
| Debian 13 | Stable package base; Xorg dummy and evdev drivers; systemd/cgroups; documented Xpra Xdummy integration improvements in trixie. Small, reproducible base without requiring a desktop shell. | Pick. NVIDIA qualification and image updates remain our responsibility. |
| Ubuntu LTS | Equally credible systemd/Xorg foundation, strong consumer-driver provisioning. The particular release matters: Xpra documents TTY/session-isolation problems on releases before Ubuntu resolute and Debian trixie. | Runner-up. Debian 13 gives a concrete, documented Xdummy baseline; do not claim every Ubuntu release is unsuitable. |
| Fedora | systemd and modern graphics; its atomic variants are attractive. Their desktop lifecycle and image integration still need qualification for this X11/evdev design. | No decisive input-routing advantage; more frequent base transitions than this small team needs. |
| Arch | All required mechanisms are available; easy to experiment with current graphics packages. | Rolling kernel/Xorg/driver changes expand the tested combinations and recovery burden. Use as a development host, not the v1 release base. |

These are product tradeoffs, not claims that a distribution supplies isolation automatically. Upstream Xpra specifically documents older Debian/Ubuntu Xdummy TTY permissions and possible interference with other X11 sessions; trixie is the first Debian baseline it identifies as enabling Xdummy by default. That supports this choice but does not qualify our explicit evdev configuration. [Sources: P5-S1–S3 below.]

Release manifest: Debian snapshot/security package versions; ArkDrive commit/API and receipt versions; Xpra version and any server input-gate patch; Xorg/dummy/evdev versions; kernel, firmware, NVIDIA kernel module and userspace versions; browser, Blender, OCR and Codex driver versions. Pull dependencies into a signed, retained build input set. Use current supported versions that pass qualification, not an eternally frozen initial snapshot. If Debian's Xpra package lacks a needed integration, rebuild a pinned upstream release as an ArkDrive `.deb`, with named security ownership; never add an unpinned third-party repository to installed machines.

Default desktop: LightDM + a minimal Openbox session running the ArkDrive human console and Xpra clients. LightDM provides PAM login and seat0/logind integration; do not invent a display manager. Session servers remain Xvfb for qualified XTEST workloads and Xorg-dummy/evdev for qualified rejecting surfaces. Openbox runs inside an agent desktop-mode session only; Xpra owns the WM role in seamless sessions. The presentation Openbox is a different WM on a different X server.

#### Boot flow and identities

Power-on sequence:

1. UEFI → GRUB selects a confirmed A/B root slot → that slot's kernel and initramfs. Initramfs mounts the persistent state partition and installs the bounded identity/configuration bind mounts before ordinary services read them. Root is read-only; `/run` and the default `/var` tree are volatile. An unmountable state partition goes to recovery, never an empty replacement home.
2. systemd brings up udev, local mounts, time synchronization, networking, and the static service identities. Host device-exclusion rules are already in the image. `ark-provision.service` validates the installer-created owner account, machine identity, state-directory initialization and slot configuration. Its local completion marker does not require provider setup. Missing model credentials do not block the broker, owner login or recovery; the authenticated console completes provider provisioning after login.
3. `ark-device-helper`, `ark-sessiond`, and `ark-broker` start. The broker restores journals, marks interrupted actions uncertain where appropriate, expires old leases, and initially closes mutation admission. Local health tests validate policy/runtime ownership and the backend profiles; only passing profiles become available. Readiness does not mean a model provider is reachable.
4. In parallel, LightDM starts the physical Xorg server and greeter on seat0. Owner login starts the presentation desktop and `ark-presentation.service` in the human user's systemd manager. It connects to the already-running broker. No agent process gets the owner's X cookie, home, bus, or login environment.
5. An authorized queued job or human launch requests `session.create`. Sessiond reserves resources and a UID slot; creates private namespaces/runtime directories; creates and routes virtual devices if needed; starts display/buses, worker and applications; verifies readiness; then starts the separate Codex/API driver and issues a fresh lease. The first model call follows this local preparation. Network/auth failure leaves the job waiting with a visible reason.

**Start zero task sessions at boot.** Keep broker/helper/sessiond resident. Allow one explicitly configured prewarmed empty session after login; it holds no job credentials or active lease. Remembered jobs resume only under an explicit persistent unattended-job policy; the default after reboot is paused, fresh observation required. Boot-health fixtures are short-lived test sessions, not resumed user work.

Reserve a fixed pool of eight slot identities in the image: `ark-app001`…`ark-app008` and distinct `ark-driver001`…`ark-driver008`, all system users, locked passwords, no interactive login or sudo. Slots are an identity mechanism, not a promise of eight concurrent sessions. One slot belongs to one agent job/application group at a time. UID reuse requires the old cgroup to be empty, virtual devices gone, secrets revoked, and the private runtime/home detached. Retained work is reopened only by explicit job/profile assignment. Avoid `DynamicUser=` for persistent browser/profile ownership in v1.

The app/worker identity can access its private X/AT-SPI/CDP resources. The driver identity can access only its broker endpoint, assigned task files, and scoped model credentials; it has no raw display or device access. Codex's coding/shell abilities must run inside this driver boundary: an unrestricted host `codex exec` would bypass it. Provider credentials are injected per driver via protected runtime credentials, never global environment files, image contents, or app homes. Persistent credentials live in owner-controlled protected state; without disk encryption, root or physical disk access can read them.

Use system units for agent sessions; do not create an interactive logind seat/VT or a lingering desktop user manager per agent. The human alone owns physical seat0. A dummy session must run without becoming DRM master or switching VTs. GPU access, where qualified, is a separate render permission. Owner lock/logout pauses agent mutation by default while preserving applications; explicitly authorized unattended jobs can continue without the presentation process.

Boot targets on the reference SSD desktop: UEFI handoff → broker locally ready ≤30 s p95; authorized request → two empty 1080p sessions ready ≤10 s p95. Report firmware, password entry, first provisioning, cloud/API connection and model latency separately. These targets do not turn the historical 159 s Virtuoso task into a boot-time or execution-time promise.

#### Service layout, ordering, and failure policy

`arkdrive.target` is enabled under `multi-user.target`; it wants the core services but not task sessions. LightDM stays under `graphical.target`. Offline boot must not wait indefinitely for `network-online.target`. The table gives required behavior; template instances and per-slot drop-ins are generated only by sessiond from immutable profiles.

| Unit | Identity / start and ordering | Restart and isolation policy |
|---|---|---|
| `ark-provision.service` | root, oneshot; after state mounts, before core admission | No automatic destructive retry. Idempotent steps and completion marker; failure exposes local provisioning/recovery only. |
| `ark-device-helper.service` | narrow root helper; after udev/provision | `Type=notify`, `Restart=on-failure`, 2 s backoff. Only helper opens `/dev/uinput`; fixed device templates and registered session IDs. Loss revokes affected routes; devices are recreated only in a new session generation. |
| `ark-sessiond.service` | root lifecycle service; after helper/provision | `Type=notify`, watchdog 10 s, restart on failure. Only allowlisted profiles, UID slots, mounts and systemd operations; no caller-supplied shell/unit text. Not the model driver. Reconcile surviving units before accepting creates. |
| `ark-broker.service` | `ark-broker`; after sessiond/provision | `Type=notify`, watchdog 10 s, restart on failure, 2 s backoff. Private devices; write only journal/evidence state. Existing workers close mutation gates on control-channel loss; restart never replays uncertain effects. |
| `ark-session@NNN.target` | sessiond starts/stops on demand | Owns the display, buses, worker, Xpra and app units via `PartOf=`; critical members also use `BindsTo=`/`After=` as appropriate. Target ordering alone is not readiness. A failed critical member causes explicit whole-generation cleanup. |
| `ark-runtime@NNN.service` | namespace keeper/setup; root setup, then fixed profile | Establish private mount/IPC/network namespaces, runtime, isolated `/tmp`, and device view before child units. No arbitrary session mount or exec API. Remains until children stop; failure ends the generation. |
| `ark-display@NNN.service`, `ark-bus@NNN.service` | `ark-appNNN`; after runtime and device registration | No blind restart. Loss of display or session/accessibility bus invalidates references; supervisor stops that generation and offers reopen. No physical input nodes. Display/bus namespaces are shared explicitly with that slot's apps/worker. |
| `ark-worker@NNN.service` | `ark-appNNN`; after display and bus readiness | notify/watchdog. On crash: gate closes, generation is retired, no automatic queued-action retry. Hanging AT-SPI/OCR subprocesses may restart independently, reporting provider degradation. |
| `ark-xpra@NNN.service`, `ark-apps@NNN.service` | `ark-appNNN`; after display/buses and worker handshake | Xpra forwarding/input gate uses Phase 3 policy. Viewer detach preserves apps; loss of the Xpra WM in seamless mode ends the generation. Desktop-mode capture recovery may be qualified separately. App exit reports job state, not unconditional relaunch. |
| `ark-driver@NNN.service` | `ark-driverNNN`; after worker/app readiness and broker authorization | `Restart=no` for task execution. Reconnect/retry is a broker job-policy decision with new observation/lease. Model/API errors cannot restart application mutations silently. Driver is in a separate namespace from raw display/CDP sockets. |
| `ark-presentation.service` | human user unit; after login's graphical environment, part of `graphical-session.target` | Restart on failure with limits. UI failure detaches previews, does not kill app sessions; default policy pauses mutation. LightDM's Openbox wrapper imports only its own DISPLAY/XAUTHORITY and starts/stops the user graphical target. |
| `ark-update-check.timer` / `.service` | unprivileged checker; daily, randomized delay; no boot dependency on Internet | Downloads signed release metadata/bundles with bandwidth/I/O limits; does not install arbitrary packages or reboot. Failure reports and retries next interval. |
| `ark-update-apply.service` | root maintenance coordinator, explicit activation after verified staging | oneshot/no automatic retry loop; exclusive update lock. Independently verify the broker drain/fence acknowledgement and empty session cgroups, then invoke RAUC. Only this coordinator may request installation or inactive-slot selection. |
| `rauc.service` | root, D-Bus update daemon; activated for the coordinator, after state mounts | Retain the pinned RAUC package's D-Bus/service lifecycle, not oneshot semantics. D-Bus/Polkit policy denies direct install/slot operations to drivers, apps and the ordinary owner UI; only the maintenance coordinator is authorized for installation/slot selection. Boot-health gets a separate narrow grant for mark-good/mark-bad on the currently booted candidate, not installation or arbitrary slot selection. Target writes are restricted to the approved inactive root and necessary boot-state metadata. |
| `ark-boot-health.service` | after core readiness; local deterministic tests | Marks candidate slot good only after required tests. On timeout/failure, keeps admission closed, records diagnostics and requests fallback reboot. Does not depend on an API model call. |
| `ark-admin.socket` / `.service` | root/operator-only Unix socket, independent of broker/model | Administrative pause/kill/recovery works with a hung broker. Also retain an authenticated local recovery TTY; SSH is off until explicitly provisioned. |

Service defaults: `NoNewPrivileges=yes`, `ProtectSystem=strict`, `ProtectHome=yes`, no capabilities, and restricted writable paths for unprivileged daemons. Broker/checker use `PrivateDevices=yes`. App/display units instead need an explicit minimal `/dev` plus `DevicePolicy=closed` and the exact assigned evdev/render/NVIDIA nodes; `DeviceAllow=` alone does not make a node visible through `PrivateDevices=yes`. Root lifecycle/device/update helpers receive only their required exceptions. Browser sandbox compatibility must pass; never fix startup by globally disabling the browser sandbox.

Within one slot, separate services deliberately join the same prepared namespaces. Giving each service an unrelated `PrivateTmp=yes` would break X socket sharing. A private network namespace also separates abstract X sockets and loopback-only CDP; disabling X TCP alone is insufficient. Apps get a qualified egress path through a per-slot veth/NAT policy for websites/VNC. Drivers get separate API egress and mounted broker sockets. Presentation gets authenticated Xpra sockets, not raw X sockets. No host session bus, SSH agent, clipboard or automatic Xpra network advertisement/remote-command service is exposed.

For a 32 GiB reference host: initial two active slots; aggregate `ark-agents.slice` `MemoryHigh=20G`, `MemoryMax=24G`, and lower CPU/I/O weight than the presentation/core services. A standard slot starts at `MemoryHigh=4G`, `MemoryMax=6G`, `TasksMax=2048`; the slot budget includes driver/apps/worker. Larger Blender profiles require separate admission. These are initial limits to tune, not measured working sets. Start-limit core daemons at five failures/60 s; then stay visibly failed rather than looping forever. Session shutdown uses `KillMode=control-group`, a 15 s cleanup timeout, then forced termination with an explicit loss receipt.

Workers and the input helper require a live broker control channel and a renewable mutation permit; disconnection closes dispatch immediately, heartbeat expiry within 2 s catches a hung broker. After broker restart, surviving apps can remain visible, but no mutation resumes before reconciliation, epoch renewal and observation. Sessiond crash recovery inventories systemd cgroups/device ownership; any ambiguous slot is quarantined and stopped. No dependency should cascade a presentation restart into killing every app. Before shutdown, a bounded broker drain runs; sudden power loss still leaves accepted/unknown actions for reconciliation.

#### Owning the stack: input policy and remaining limits

Install host exclusion before the helper can create any device: a presentation-Xorg `InputClass` ignores the reserved ArkDrive virtual-device namespace, and udev permissions prevent normal human/agent processes opening those nodes. The helper identifies actual devices through the created uinput sysfs identity and kernel event node, not an untrusted name alone. It publishes a generation-bound mapping under `/run/arkdrive/devices/`; event numbers are never hardcoded across boots. [P5-S4, P5-S5.]

Agent Xorg configuration disables `AutoAddDevices`, `AutoEnableDevices`, and `AutoAddGPU`; declares only the assigned keyboard/pointer as explicit evdev inputs; and uses dummy video. Use the pinned, non-setuid Xorg/Xdummy launch path, explicit runtime/config ownership, no TCP listener, and qualified no-VT-switch behavior. Session namespaces/device allowlists cannot see physical keyboard/mouse or sibling inputs. If a particular Xorg/evdev profile needs broad root/input-group access or steals a VT, that profile fails release qualification. Keep Xvfb for supported workloads; never enable global ydotool as a rescue route.

Each new generation must prove its assigned keyboard/pointer reaches its test surface; the installed image also passes repeated concurrent host/sibling non-delivery tests. Device names/exclusion rules alone are not proof. Test physical USB hotplug and virtual-device destroy/recreate while the human types. Xpra's own uinput creation stays disabled unless integrated into this same helper/registry; a second injector would bypass ownership.

| Existing host problem | What Phase 5 changes | What remains |
|---|---|---|
| XFCE steals F2 | Agent input goes directly to its private server; shipped agent WM has no F2 binding. Presentation WM also omits conflicting function-key/Alt bindings. Human takeover has explicit “send key” controls. | An application can still grab keys inside its own session. X11 emergency shortcuts are still grabbable. |
| Session/host contention | Image owns boot ordering, udev/Xorg exclusions, UID/runtime/bus/profile allocation, and resource admission. No opportunistic attachment to the human display. | Applications within a session intentionally share focus; files, accounts and remote desktops need task ownership. |
| Single-driver global state | One Codex/API driver and ordered queue per slot, bound to a session capability; independent workers and displays. | API rate limits and model latency can constrain throughput; independent input does not guarantee a 2x task speedup. |
| Fragile host configuration | Signed image specifies the kernel/Xorg/Xpra/driver tuple and tests it together; no unrelated desktop package upgrade changes routing underneath a running job. | OS ownership transfers compatibility and patching responsibility to us. |

Preserve the independent admin path. Removing shortcut bindings improves ordinary use; it does not turn an owned X11 desktop into the compositor-level preemption guarantee discussed in Phase 4. No custom human compositor is a Phase 5 dependency.

#### Packaging, installation, and first boot

Build versioned `.deb` packages for broker/sessiond, workers/providers, input policy/helper, console, driver adapters, and OS configuration. Assemble them with Debian base packages in CI using a pinned rootfs builder (e.g. mmdebstrap), then generate a Debian live-build USB hybrid ISO containing the installer, recovery tools and the exact root image. Publish the ISO, signed checksums, package/SBOM manifest, source revisions, license notices and matching signed RAUC `.raucb` update bundle. No CAD software or license is bundled; Virtuoso access remains an explicitly configured remote session or separately qualified licensed installation. [P5-S6.]

Default installed GPT layout: 1 GiB FAT ESP; 24 GiB ext4 ROOT-A; 24 GiB ext4 ROOT-B; remaining disk ext4 STATE. Each root slot includes its own `/boot` kernel/initramfs and matching `/usr/lib/modules`/graphics userspace; GRUB reads them from the selected ext4 root. ESP contains the common GRUB loader/config and its boot-state environment. Installer assigns unique GPT PARTUUIDs and writes a persistent machine slot-map used by RAUC and initramfs. GRUB passes the selected `root=PARTUUID=…` and `rauc.slot=A` or `B`; mount configuration is generated for that active slot. Cloned filesystem labels/UUIDs must not select the wrong slot. A/B costs ~49 GiB reserved, before state and recovery space; require a 128 GB SSD minimum and recommend 256 GB+ for artifacts/browser profiles.

Mount STATE early; bind only explicitly persistent paths. Keep shipped `/etc` defaults image-owned; do not persist all of `/etc` over new releases. The default `/var` tree is tmpfs, initialized from image-owned tmpfiles rules/current-release seed data before services start; it must not hide uninitialized image state. Use this policy:

| State class | Location/lifetime | Update rule |
|---|---|---|
| ArkDrive jobs, audit, credentials, control DB | STATE-backed `/var/lib/arkdrive` | Persistent; N/N−1 schema/backup rules apply. Separate evidence/work quotas prevent journal exhaustion. |
| Owner home, assigned app profiles/workspaces | STATE-backed homes/work directories | Persistent; version/copy profiles when old app compatibility would break. Never bind the owner home into an agent. |
| NetworkManager connections/state, machine/owner identity | Explicit STATE-backed config and `/var/lib/NetworkManager` | Allowlisted/versioned; installer seeds identity before normal boot. Changes must work with both retained roots. |
| Persistent system journal, retained crash diagnostics | STATE-backed `/var/log/journal` and diagnostic directories | Bounded retention; logs are not an authority for boot decisions. |
| dpkg inventory | Read-only slot-owned package DB bound at `/var/lib/dpkg`, plus release SBOM | Travels with root; never share the other slot's dpkg database. Live apt/dpkg mutation is unsupported. |
| Other `/var` state, including greeter caches, apt caches and transient service directories | Volatile; recreated using the current image's declared initialization | Any daemon requiring durable state must be added to the allowlist with a compatibility/migration test before release. No blanket persistent `/var` mount. |

Predeclare service/slot users in the image; the installer seeds persistent passwd/shadow/group files from that roster and creates the owner login. First boot validates ownership and the account seed without resetting authentication. Slot preparation validates/merges required service accounts with stable UIDs without erasing owner records. Machine ID, host keys if enabled, network settings and owner authentication persist; `/run`, display cookies and control tokens do not. For v1 only one owner/admin account is supported. State schema and account compatibility with the previous root are update gates.

Install on an ArK2-like RTX 3060 12 GB / i5 / 32 GiB desktop:

1. Back up the target disk; download and verify the signed ISO; write it to an 8 GB+ USB stick. Use UEFI boot with Secure Boot disabled for this v1 profile and a dedicated target SSD. The installer shows the exact drive/partitions to erase and requires confirmation; no default dual-boot resizing.
2. Boot “hardware check” first. Test monitor, physical keyboard/mouse, Ethernet, storage and GPU. Choose the qualified NVIDIA image/profile; if acceleration is unavailable, show software/headless diagnostics rather than claiming a GPU pass. Installation itself works offline; model execution needs network/authentication later.
3. The installer lays out A/B/state and installs the confirmed image in both root slots. Before reboot it collects the owner account/password, initializes persistent account files/home from the image roster, and initializes the boot policy; this creates the first authenticated login without depending on the broker. First boot validates this seed; after owner login, the console completes network/time configuration, retention policy and selected Codex/API authentication. No shared default password, baked API token, or automatic billable test call.
4. Run local two-session input/Thunar tests and backend/GPU diagnostics. Then the owner explicitly starts a real Codex/API task. Surface “GUI isolated / evdev qualified / GPU accelerated / model authorized” separately; one green status must not imply the others.

**NVIDIA policy:** ship one tested kernel/driver/userspace tuple for the RTX 3060 first. Build the Debian-packaged NVIDIA kernel module in CI against the image kernel, include it in that slot and initramfs, and prohibit on-device DKMS rebuilds or NVIDIA `.run` installers. Include the matching libraries; a kernel-module/userspace mismatch must fail health checks. The distro's available driver version is an input, not proof that its GPU path works or remains adequately patched. Do not pin a known-vulnerable branch just to preserve compatibility. Redistribution and firmware notices must be reviewed before publishing an NVIDIA image. [P5-S7.]

Xorg-dummy still normally renders via software; installing the NVIDIA driver does not change that. Qualify a pinned VirtualGL/EGL application-launch profile on the same GPU for Blender; record renderer, viewport latency, render time and copy/CPU overhead. A needed GPU service exposes only authorized render access, never physical input or host-home access. If this profile fails, ship the CPU/UI workload profile explicitly; do not advertise GPU Blender until the existing Phase 2 gate passes. Phase 4 may later supply a better GPU backend but cannot be silently made mandatory.

Admission on 12 GB VRAM initially reserves 2 GiB for presentation/driver overhead and budgets at most 10 GiB for admitted apps, using measured per-profile peaks and live telemetry. Start with one GPU-heavy render at a time and two ordinary agent sessions; qualify N=4 separately. NVML estimates/admission are not a hard per-session VRAM quota, and GPU allocations can spike. A GPU reset can affect the human desktop and every accelerated session. No local model consumes a standing VRAM reservation.

#### Updates and rollback

**Use atomic root-slot updates via RAUC; Debian packages are build inputs, not the live-machine update mechanism.** OS and ArkDrive ship together in one signed release. An ArkDrive-only fix builds a new image too; v1 has no independent hot-upgrade channel or live broker/Xpra replacement. This trades bandwidth/reboots for a testable rollback unit. RAUC provides installation/slot control, not the whole boot/recovery design: GRUB attempt handling, health policy and the installer are integration work. [P5-S8, P5-S9.]

Update state machine: AVAILABLE → VERIFIED → STAGED → DRAINING → REBOOT_PENDING → BOOT_TEST → CONFIRMED, or FAILED/ROLLED_BACK. The checker may download and verify while sessions run, under I/O limits. Inactive-slot writing starts only in maintenance, after new-job admission is closed, existing sessions are drained/stopped, and all mutation gates are fenced; this avoids disk pressure and state races in v1.

Drain uses two distinct gates: DRAINING rejects new jobs/sessions but permits existing jobs' bounded checkpoint/save actions under their current leases; FENCED revokes epochs and rejects every further mutation. Tell existing drivers to finish at a task checkpoint; request application saves through supported GUI workflows; retain unsaved/unknown warnings and receipts. A 10-minute default drain timeout defers installation/reboot if work remains. Force termination is an explicit owner/admin operation, or an already authorized maintenance policy, and records loss risk. Do not silently kill an unsaved CAD/Blender task to meet a timer. Browser tabs/windows can be reopened from a manifest, but GUI process state is not checkpointed or migrated across kernels.

After all sessions stop, enter FENCED; the root coordinator verifies closed gates and empty session cgroups before writing anything. Then flush audit/job state, back up control databases/configuration, verify state-schema compatibility, install the signed inactive-slot image, verify the installed contents, and select it for one trial boot. User data remains on STATE. No reboot merely because a download finished. A critical remotely exploitable issue can trigger a declared maintenance requirement, network restriction or service stop under policy; it does not justify an invisible reboot mid-task.

GRUB consumes the candidate attempt before boot; a candidate is confirmed only by `ark-boot-health.service` after mounts, broker/helper, LightDM/physical-display greeter readiness and local two-session routing tests pass, with no owner login/model/network dependency. Test the actual owner console on login as a separate readiness check; never wait for a password to confirm a candidate. A separately qualified headless profile substitutes its declared local display-health check. Target health decision ≤120 s after userspace starts. A failed userspace test requests reboot; a kernel hang requires a working hardware watchdog or manual power cycle. On the next boot, the unconfirmed candidate is skipped for the previous confirmed slot. RAUC's GRUB environment lives outside the replaceable root slots. Test actual motherboard/ESP behavior; “RAUC installed” is not a rollback test.

Retain one known-good root slot and a manual GRUB fallback choice. Both slots have the same security level only immediately after installation; rollback can reintroduce fixed vulnerabilities and must show the old build/date. Retain a rescue USB for ESP/GRUB corruption: the common ESP, firmware, SSD failure and shared STATE are outside root-slot atomicity. Routine releases do not rewrite the common bootloader. Bootloader/security updates use a separate documented recovery-backed procedure and carry their own power-loss risk; do not call that path atomic.

Persistent state is not rolled back with root. Control/audit schemas must remain readable by N and N−1; use additive migrations and preserve old readers until the previous slot is retired. Keep database/config backups for exceptional recovery, without rewinding user work automatically. Browser/profile and application-file compatibility also matters: before a profile-format upgrade, make a versioned copy or prevent the old application reopening that profile. If rollback needs restoration, reopen a preserved copy and show the recovery point; new user files are exported first. A release with an unavoidable irreversible state migration requires an explicit recovery procedure and cannot be an automatic update.

Signed metadata includes build ID, hardware profile, minimum updater/API/state versions, hashes and channel. Keep signing private keys off public CI; verify release/key rotation and reject accidental downgrades, while permitting explicit recovery rollback. TLS alone is insufficient update authentication. Signed images plus read-only root do not provide verified boot against a privileged or physical attacker; Secure Boot/measured boot are outside v1's trusted-automation promise.

#### Hardware scope and support boundary

v1 supported configuration: x86-64 desktop, UEFI/GPT, one SATA/NVMe SSD, one 1080p monitor, USB keyboard/mouse, Ethernet, at least 16 GiB RAM for a limited two-session CPU profile; 32 GiB is the reference qualification machine. First accelerated support is one RTX 3060 12 GB configuration. Intel/AMD graphics are additional named profiles only after the same presentation, evdev and GPU tests; generic Linux compatibility is not an ArkDrive support claim. An x86-64 KVM/virtio VM is a boot/update/CPU test target, not evidence for NVIDIA or physical-device routing.

Explicitly out of scope: ARM, legacy BIOS, laptops/hybrid graphics, multi-GPU routing or GPU passthrough/vGPU, multi-monitor/HDR/VRR qualification, Wi-Fi/Bluetooth provisioning guarantees, suspend/hibernate, audio/video conferencing and device forwarding, dual-boot resizing, RAID, full-disk encryption and Secure Boot support, arbitrary desktop environments, arbitrary root package installs, transparent live-session migration, hostile tenant isolation, and bundled commercial CAD/licenses. A display cable is optional only for a separately tested headless profile; do not infer headless boot from Xdummy session support.

The installed root is appliance-managed. Applications enter through versioned, qualified profiles/images/packages; developer mode can make a machine mutable but labels it unsupported until restored. Browsers still execute untrusted web content, so trusted jobs do not excuse missing browser/kernel patches. Separate UIDs/namespaces reduce ambient access; shared kernel/GPU and within-session X11 trust remain as stated in Section 5.

#### Maintenance cost and minimum sustainable cadence

A distro obliges us to monitor Debian security advisories/support coverage; rebuild kernels/initramfs/NVIDIA modules; patch browsers, Xpra, Openbox/LightDM, broker/helper, OCR libraries and Codex adapters; retain source/license notices and artifacts; operate signing/release infrastructure; test installation, rollback and recovery; triage hardware regressions and state compatibility. Debian support does not automatically cover every third-party package or the lifetime/security of a proprietary driver branch. Plan the next Debian base migration before trixie's full-support period ends in August 2028, not on its last day. [P5-S1.]

Smallest credible team: two named engineers with cross-trained signing/recovery access—one primary OS/release/security maintainer, one ArkDrive/runtime maintainer who reviews and can cover releases. Budget roughly 0.5–1.0 engineer FTE ongoing across them after stabilization, plus incident spikes and a separate future base-upgrade project. That is a planning allowance, not a measured staffing rate. A single occasional maintainer can run a private experiment; do not promise timely public security support on that basis.

Cadence: automated build/dependency/advisory checks daily; a human security triage every working day with an urgent-alert rota; weekly signed candidate and security maintenance window; normal stable promotion after at least 48 hours on the reference desktop. Target critical issue triage within 24 hours, and a tested fix or documented temporary mitigation within 72 hours when upstream remedy/hardware permits; publish status if it slips. Run the short routing/rollback suite for every candidate, broader N=1/2/4 mixed tests weekly and for every graphics/input change, plus monthly restore/install/power-loss drills. Keep one physical RTX 3060 test rig and VM CI; adding a hardware SKU expands this matrix. If that capacity is unavailable, keep OS images experimental and maintain the installable ArkDrive path.

#### Changes forced back into Phases 0–4

These are additions to the earlier gates, not a replacement architecture:
- **Phase 0:** capture a complete distro/kernel/Xorg/Xpra/GPU manifest; qualify the intended Debian rootless Xdummy/no-VT profile, static evdev routing, device hotplug and image-level host exclusion. Run the GPU spike on the actual shipped kernel/driver tuple. A passing stock-desktop result does not qualify the OS image.
- **Phase 1:** remove global DISPLAY/home/driver singletons; launch worker/apps and Codex/API driver under separate slot identities; retain session capability scoping and the original host-F2-bound regression on a stock XFCE host. Add the owned-desktop equivalent so removing the host binding cannot conceal incorrect routing. The installable-software path remains supported.
- **Phase 2:** export backend health/readiness as machine-readable data for boot admission; add broker/helper loss, reboot-during-dispatch, evdev recreation, namespace and NVIDIA-mismatch fixtures. Keep dispatch uncertainty and model latency accounting unchanged.
- **Phase 3:** split application lifetime from presentation lifetime; add maintenance admission/drain/checkpoint states, bounded shutdown, N/N−1 state/profile compatibility, durable reopen manifests and per-session resource profiles. Add boot/image/build ID to audit metadata, exposed by a versioned `system.info`/admin health endpoint; no change to `window.act` semantics is required.
- **Phase 4:** remains an optional backend. Package only after requalification on the OS image. Its nested compositor still does not own physical seat0; replacing the human X11 desktop remains a separate future decision.

Keep Section 7's three decisions and the Codex/API amendment in force. Phase 5 adds OS support, signing and release ownership as a fourth release commitment; it does not authorize hostile-code multitenancy or turn a local executor into a required component.

#### Phase 5 milestones and pass/fail gates

Estimates are engineer-weeks; 16–24 incremental beyond Phases 0–3. With two available engineers, allow roughly 10–14 calendar weeks including overlap/soak, assuming earlier backends already pass. This excludes building the optional Phase 4 compositor backend and adding extra GPU/hardware families. Unknown Xorg/evdev or GPU failures can extend the schedule; publish a blocked profile instead of weakening isolation.

| Milestone | Effort | Observable pass/fail gate |
|---|---|---|
| **5.0 — Boot/device feasibility** | 2–3 weeks | Minimal Debian image boots on the physical reference desktop; human login and two Xdummy/evdev sessions coexist without extra physical VT/seat ownership. Zero host/sibling deliveries over 10,000 tagged events with device hotplug/recreation; XTEST-rejection fixture receives only assigned evdev input. Failing evdev blocks that OS profile, even if Xvfb works. |
| **5.1 — Owned service lifecycle** | 3–4 weeks | Core units start offline; zero task sessions by default; two authorized Codex/API drivers work after provisioning. 30/30 paired Thunar tasks meet original filesystem/takeover gates; failed APIs cause visible wait, no wrong-target fallback. Kill broker/sessiond/helper/presentation/display separately: gates close, no uncertain action replay, no sibling/host input; surviving/reopened sessions have correct epochs/generations. Repeat cold boot 30 times and publish readiness percentiles. |
| **5.2 — Installer and hardware profile** | 3–5 weeks | USB install/reinstall onto a blank dedicated SSD, no baked identities/secrets, explicit erase target, safe failure on unsupported storage/GPU. Qualify reference NVIDIA module/userspace and Blender renderer/workflow against Phase 2; pass two-hour mixed N=1/2/4 suite at admitted capacities. Unsupported GPU capability stays visibly disabled. One other person follows published install/recovery instructions without private intervention. |
| **5.3 — Update and rollback** | 4–6 weeks | Signed A→B→A and N/N−1 state/profile recovery pass; reject corrupt/wrong-signature/wrong-profile bundles. Power-cut/reboot injections cover download, inactive-slot write, boot-selection and unconfirmed boot (at least five trials at each stage); always reach a usable confirmed root or documented USB recovery, never touch active root/user data. Fail candidate broker/GPU/routing health and verify fallback after reboot. An unsaved task defers update at drain timeout; an authorized forced stop records loss/uncertainty. |
| **5.4 — Release operations and soak** | 4–6 weeks | Fourteen calendar days on the reference hardware with daily local task pairs, repeated owner login/lock/detach and two complete release promotions; no unexplained input escape or silent replay. Re-run earlier success/token/latency reports against a passing same-hardware baseline; retain Phase 2's ≤2x local-control p95 at N=4 where admitted. Publish manifest, supported profiles, patch/rollback runbook, signing/key-rotation recovery and named primary/backup maintenance owners. Any unresolved isolation, data-loss or unrecoverable-update defect blocks stable release. |

First OS milestone: boot the reference machine from the image, log into the human console, start two independent Codex/API-driven folder tasks while the owner types, then restart the broker and demonstrate closed gates with no replay. A branded login screen or fast boot without that trace is not the milestone.

#### Phase 5 source notes

Primary documentation checked Nov 9, 2026. Sources establish upstream mechanisms and package availability; architecture, staffing, timings and gate thresholds above are designer proposals requiring measurement.

- **P5-S1:** [Debian 13 release/support information](https://www.debian.org/releases/trixie/).
- **P5-S2:** [Debian trixie Xorg packages, including evdev/dummy](https://packages.debian.org/search?keywords=xserver-xorg&searchon=names&suite=trixie).
- **P5-S3:** [Xpra Xdummy documentation: distro permission/session issues and launch path](https://xpra-org.github.io/xpra/Usage/Xdummy.html); [Xpra manual: desktop/seamless/shadow operation](https://xpra.org/manual).
- **P5-S4:** [Debian trixie xorg.conf manual: explicit input devices and automatic device/GPU controls](https://manpages.debian.org/trixie/xserver-xorg-core/xorg.conf.5.en.html).
- **P5-S5:** [Linux uinput documentation](https://kernel.org/doc/html/latest/input/uinput.html); [libevdev uinput device/sysfs identity API](https://www.freedesktop.org/software/libevdev/doc/latest/group__uinput.html).
- **P5-S6:** [Debian Live manual: live-build and hybrid ISO construction](https://live-team.pages.debian.net/live-manual/html/live-manual.en.html).
- **P5-S7:** [Debian NVIDIA driver package/dependencies](https://packages.debian.org/trixie/nvidia-driver).
- **P5-S8:** [RAUC integration: slots, stable device paths and GRUB boot-state integration](https://rauc.readthedocs.io/en/latest/integration.html).
- **P5-S9:** [RAUC successful/failed boot confirmation](https://rauc.readthedocs.io/en/latest/using.html).
- **P5-S10:** [systemd service readiness/watchdogs](https://www.freedesktop.org/software/systemd/man/systemd.service.html); [device/resource controls](https://www.freedesktop.org/software/systemd/man/systemd.resource-control.html); [execution namespaces and device visibility](https://www.freedesktop.org/software/systemd/man/systemd.exec.html). Implementation must use directives available in the pinned Debian systemd, not assume the latest online manual's entire feature set.

## 7. Risks, unknowns, and the top three decisions

Risks (consequence → first resolution):
- Xorg/evdev permissions, VT/logind behavior → routing may need distro-specific work → early keyboard/pointer isolation spike.
- Software-rendered Blender → functional UI, poor performance → inspect actual renderer; measure viewport/render.
- Shared remote VNC desktop → false local-isolation success → independent remote displays and negative control.
- Accessibility outages/stale refs → blindness or wrong actions → provider health, versioned refs, independent sensing.
- Focus/grab races → input reaches unexpected target → modal policy, shortcut allowlist, effect verification.
- Single-instance applications → launch escapes session → private buses/profiles and launch tests.
- In-flight takeover effects → work completes after pause → quiescing, epochs, drain/reconcile.
- Shared UID/GPU/kernel → overstated security → explicit trusted MVP; stronger containment later.
- Event storms/hidden-window throttling → stale state or wasted resources → bounded queues, resync, source-side sensing.
- Dependency maintenance/licensing → long-lived integration burden → pin versions; review external dependency modifications.

A WM cannot infer the business meaning of a changed canvas. Nor does independent input make concurrent edits to one CAD design, file, or account safe — those require task-level ownership.

### The three decisions to make before implementation (designer's recommendations noted)
1. Execution domain and launch policy. Accept one session per agent/application group, with controlled launching/reopening. Recommendation: yes. This makes isolation concrete and keeps the first implementation small.
2. Trusted automation vs hostile-code containment. Decide whether arbitrary code and real credentials must be supported immediately. Recommendation: trusted applications and restricted working mounts first; stronger UID/sandbox isolation before broader access.
3. First-release compatibility bar. Decide whether the parallelism proof may use Xvfb/XTEST or must already include rejecting surfaces and GPU-heavy Blender. Recommendation: prove concurrency first, run evdev/GPU spikes immediately, and require qualification before claiming full workload compatibility.

First milestone (observable and narrow): two agents independently create and rename folders while the human types elsewhere; a modal breaks AT-SPI in one session; takeover pauses only that session. Publish recordings, receipts, failures, timing breakdowns, and token counts together.

## Amendment — driver model policy (ArK, Nov 9, 2026)

ArK's direction after the plan landed: "Do not think in terms of qwen. Think in terms of codex or api."

Applied to this plan:
- The agents that drive sessions are Codex drivers (codex exec, the shape that passed the Virtuoso bench) or API-model loops via ArkDrive's CU_MODEL_PROVIDER presets. The local Qwen executor is not a design dependency anywhere in this architecture: not as a per-session worker brain, not as the Phase 0/1 builder, not in the token/latency budgets.
- The plan's numbers already assume this (Virtuoso 33,171 tokens and Thunar 22,102 tokens were Codex-driver runs), so no budget changes. The "model time dominates" conclusion strengthens: with API-class drivers, per-action quality and receipt discipline matter more than shaving local inference.
- Build execution for Phases 0–2: Raj specs, UCGS/Codex designs, Codex implements on ArK2, Raj verifies. Gwen lanes are not part of the critical path.
