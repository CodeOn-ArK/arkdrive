# ArkDrive Agentic Window Manager — Architecture Plan v2

- Basis: `AGENTIC-WM-PLAN.md` (v1), the plan approved by ArK on Nov 9, 2026, including its Phase 5 (ArkDrive OS) extension and the driver-policy amendment. v1 is untouched; this file is a revision of it.
- Status: PROPOSED REVISION. The three approved decisions and the Codex/API driver policy are carried over unchanged. New items that need ArK's sign-off are listed in §7.3 with deadlines; until they are signed, work proceeds under v1's authority ("Phase 0 proceeds on this basis").
- Evidence rule: every number in this document is either **measured** (listed in §0.1, taken from v1 and its bench notes) or a **target / estimate** (everything else). No target here has been measured. Do not sum targets into a claimed task latency.

## What changed and why

**Faster**
1. **Session start has a real budget.** A warm pool of pristine, lease-less sessions and fd-based readiness (no sleeps): warm bind ≤300 ms, cold Xvfb session ≤2.5 s, two cold sessions ≤3 s (v1: two sessions ≤10 s). Why: startup was v1's only lifecycle number and it was loose.
2. **Fewer model turns, not just faster dispatch.** `window.act` now takes up to 8 locally verified steps and returns the post-action state delta in the receipt, so a rename is one driver turn instead of three or four. Why: the measured cost is 6.19–9.35 s per action and it is almost all model time; local overhead is capped at ≤50 ms per action so it stays under 1% of that.
3. **Observation is paid for once.** One damage-maintained shadow framebuffer per session; "nothing changed" answers in ≤1 ms without a capture; fresh 1080p crop ≤10 ms (v1 ≤30 ms); OCR results cached by content hash.
4. **Dispatch has no wasted motion.** Zero fork/exec per action, focus checked from cached events, request → input written ≤3 ms (v1: ≤2 ms + ≤10 ms in two separate budgets).
5. **Takeover is one atomic write.** A shared control page closes the gate in ≤10 ms (v1 <50 ms); a hung broker is detected in 500 ms (v1 2 s).

**More integrated**
6. **Nine logical components become one broker, one worker per session and three narrow helpers** (lifecycle, input device, admin), plus the console. Per-session systemd services drop from 7 to 5 because the worker is the session's init and owns display, buses, WM, sensing and verification, which share fate anyway. One process is added, for security: the model-credential proxy (item 10).
7. **One authority per fact** (§3.2): the control page says who may mutate, one per-session `rev` orders all state, the broker journal records intent, systemd records liveness, one signed profile defines a session, one health document says what is ready. v1 had separate geometry, semantic, event and frame revisions and several places that each knew the control state.
8. **One record, one adapter, no fork.** The receipt is the audit record; Codex and API-loop drivers use the same adapter; ArkDrive never carries an Xpra patch (spike S-XPRA picks one of two patch-free takeover paths).

**More secure (inside the trusted-automation-first decision)**
9. **Device scoping:** a non-root input helper is the only holder of uinput fds; session devices are kernel-grabbed by their own X server; host exclusion has two independent layers plus a continuous host sentinel that fences every session if an ArkDrive device ever appears on the human display.
10. **Credentials:** provider keys never enter a driver's address space (egress proxy with per-job tokens and spend caps), are TPM-sealed at rest where a TPM exists, and typed secrets are passed by reference so they never reach a model, a receipt or a screenshot.
11. **Session identity:** per-slot broker sockets, kernel peer credentials plus pidfd/cgroup check, and leases MAC'd with a per-boot key and bound to the peer. A copied token is useless from another slot or after reboot.
12. **Update signing:** offline root, hardware-backed release key, threshold-signed channel metadata with expiry, a monotonic anti-rollback counter, and a key-compromise drill as a release gate.
13. **Audit trail:** single-writer hash-chained journal, signed checkpoints, optional off-box export, and a tamper test that must fail loudly.

**Deadline-based**
14. **Every action has an owner, a hard date, a pass/fail gate and a pre-declared decision on failure** (§6). T0 is Mon 2026-11-09. Open-ended work is replaced by eight timeboxed spikes, on top of the three already run. The critical path is explicit: the earliest real two-agent demo is **Fri 2026-11-20**, the full Phase 1 gate is Fri 2026-12-04, and the ArkDrive OS stable-release decision is Fri 2027-05-07.

**Not changed:** option D (per-agent display servers), the three approved decisions, the Codex/API-only driver policy, every measured figure, and all of v1's "never" rules (never fall back to host ydotool, never treat a flush as proof, never promise exactly-once, never replay an uncertain action).

## 0. Evidence ledger and conventions

### 0.1 Measured evidence (unchanged; nothing in v2 contradicts it)

| Item | Measured result | Source |
|---|---|---|
| Virtuoso bench (Codex driver) | 159 s, 17 actions, 33,171 tokens → 9.35 s per action | v1 brief |
| Thunar bench (Codex driver) | 99 s, 16 actions, 22,102 tokens → 6.19 s per action; **failed** on F2 focus theft by the host | v1 brief |
| Calculator semantic clicks | four at ~0.2 s each | v1 brief |
| AT-SPI snapshot | 130 ms | v1 §3 |
| Thunar accessibility | AT-SPI bus drop during the agent-desktop trial | v1 brief |
| OCR vs vision | 0.17 s vs 5.3 s (~31×); byte-exact on the tested crop only | v1 brief |
| Region hash | 1.6 ms | v1 brief |
| **Spike A** — private Xvfb + private accessibility bus isolation | **PASS 15/15** | bench notes |
| **Spike B** — Xorg-dummy sessions, explicit evdev routing | **PASS 23/23 with host exclusion** | bench notes |
| **Spike C** — GPU path | **headless EGL 8.5×; Cycles blocked on VRAM** | bench notes |

Provenance note: the bench-notes file was not in the working directory when v2 was written, so the three spike results are used exactly as stated in the v2 brief and nothing is inferred beyond them (not the individual checks, not what the 8.5× is relative to, not why VRAM was short). Where v2 needs more than the stated result, it schedules a follow-up spike instead of assuming.

What the spikes change: v1's Phase 0 probes (1)–(3) have run. The session profile, the evdev route and the EGL GPU path have passed **at spike scale**. Release-scale gates are still owed: 10,000 tagged events with hotplug (S-B2), the Debian 13 image (S-DEB, milestone 5.0), and a Cycles decision (S-C2).

### 0.2 Conventions

- **"v1"** always means the approved plan document. The product's first release is **"OS v1"**.
- **T0 = Mon 2026-11-09**, the day the decisions were approved and Phase 0 began. Deadlines are end of day on the stated date and are also given as T0+N days.
- **Reference host:** ArK2-class desktop, RTX 3060 12 GB, i5, 32 GiB, one 1080p monitor. All targets are p95 on this host unless stated.
- **Owners.** **ArK**: decisions, physical-hardware steps, credential and signing custody, the human in every demo. **Raj**: specs and gate verification. **R**: Codex implementation lane for runtime (broker, worker, contract, sensing). **O**: Codex implementation lane for OS/platform (devices, GPU, image, updates). Both lanes run on ArK2. **UCGS**: design consultation on request, never on the critical path. **M-OS / M-RT**: the two accountable human maintainers Phase 5 requires; unnamed today, and naming them is a dated action (A-OWN).
- The lane that builds something never signs its own gate; Raj verifies, ArK decides.

## Recommendation (up front)

Build one isolated display session per agent or cooperating application group, each run by a single session worker, all governed by one broker that is the only authority on who may act. Present sessions through Xpra on the human desktop. Start on Xvfb with session-local XTEST and a minimal WM; keep the Xorg-dummy backend with explicitly routed, kernel-grabbed uinput devices for applications that reject XTEST (Spike B passed); use the headless-EGL launch profile for GPU applications (Spike C passed). Keep the backend replaceable so a later Wayland backend fits the same contract.

The agent session is the critical boundary. Each session has independent pointer and keyboard-focus state. Physical input serializes within a session; different sessions execute concurrently.

Three things carry the speed: fewer model turns per task (multi-step verified actions, bounded deltas), observation that costs nothing when nothing changed, and warm sessions. Three things carry the integration: one control page, one revision counter, one journal. Ship order is the critical path in §6.2: a real two-agent demo by Fri 2026-11-20, then the Phase 1 gate, then sensing and compatibility, then the owned OS image.

## 1. Architecture options

The option analysis is unchanged from v1 and is settled by decision 1; it is kept short here.

| Option | What it is | Effort (v1 estimate, engineer-weeks) | Verdict |
|---|---|---|---|
| A. Shared X11 with MPX | One master pointer/keyboard pair per agent on one X server | 4–8 prototype; 12–24+ qualification | Research branch. Core grabs in legacy apps can suppress other devices; all clients share one X trust domain. |
| B. GNOME Shell extension + Mutter | Labels, grouping and pause controls in the shell | 2–4 presentation; 16–32+ for independent control | Possible future host shell. An extension alone does not create independent input domains. |
| C. Custom wlroots compositor | Compositor-owned seat per agent, one Xwayland per agent domain | 8–12 narrow prototype; 24–48+ daily desktop | Strongest eventual compositor design; too much initial scope for a small team. |
| **D. Per-agent display servers, one presentation layer** | Private X server, buses, profile and input queue per agent; Xpra presents them | 2–4 for a trusted two-agent MVP; ~8–16 through a first qualified X11 release | **Chosen.** |

Backends under option D:

| Backend | Role | Status |
|---|---|---|
| Xvfb | Phase 1 backend; session-local XTEST; fastest start; framebuffer can be memory-mapped | Spike A PASS 15/15. Does not consume evdev devices; host ydotool is never a fallback. |
| Xorg + dummy video + evdev | Applications that reject XTEST | Spike B PASS 23/23 with host exclusion. Release-scale and Debian 13 qualification still owed. |
| Headless EGL launch profile | GPU rendering for applications in either backend | Spike C: 8.5× via headless EGL. Cycles blocked on VRAM; decided by S-C2. |
| Xephyr | Visible nesting for debugging only | Not a product backend. |
| Nested Wayland + per-session Xwayland | Later GPU / native-Wayland backend | Phase 4, optional. |

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

Choose D and keep the display backend replaceable.

Priorities set by the measurements, in order:

1. **Fix ownership first.** The F2 failure is an input-domain failure. Agent input never traverses host XFCE; the session WM binds no F2.
2. **Cut model turns before microseconds.** At 9.35 s (Virtuoso) and 6.19 s (Thunar) per action, saving 10 ms per dispatch saves 0.17 s over 17 actions; removing one model turn saves seconds. Multi-step actions and small deltas are the first speed investment; the local budgets in §3.12 exist so local work never becomes the bottleneck.
3. **Exploit working semantics, do not depend on them.** Calculator's ~0.2 s semantic clicks are a fast path. They do not prove another GTK modal exposes a usable tree. Thunar dropping off AT-SPI must degrade one provider, not blind the session.
4. **Replace vision where OCR applies.** One replaced vision call saves ~5.13 s in the measured sample. That result belongs to the tested crop, not to OCR in general.
5. **Separate input concurrency from compute concurrency.** Independent input domains create no extra GPU memory, model throughput, remote CAD sessions or licence seats. Two identical independent jobs have a theoretical 2× ceiling before contention. Re-establish a passing Thunar baseline before using it in any throughput comparison.

## 3. Recommended design in detail

### 3.1 Process inventory: fewer moving parts

v1 listed nine logical components and, by Phase 5, eight units per session. v2 fixes the process list from day one.

| Process | Identity | Owns | v1 components absorbed |
|---|---|---|---|
| `ark-broker` (one per machine) | `ark-broker`, unprivileged | All authoritative state: sessions, generations, leases, the control page (sole writer), one ordered queue per session, the journal and receipts, admission, the health document | Broker, verifier/audit service, console back end |
| `ark-worker` (one per session; the session's init) | `ark-appNNN` | Display server, session and accessibility buses and WM as its children; every X11, AT-SPI and CDP connection; `rev` assignment; state cache; shadow framebuffer; dispatch; the predicate engine | Per-session worker, display backend, WM policy, sensing service |
| `ark-sessiond` | root | Lifecycle actuation only: start or stop the units of an allowlisted profile for a (slot, generation). Holds no authoritative state | sessiond |
| `ark-device-helper` | `ark-input`, unprivileged; group access to `/dev/uinput` and nothing else | The uinput fds; writes input events; the last gate before the kernel | Device helper (was root in v1) |
| `ark-console` | the logged-in owner | Tiles, overlays, takeover UI, the host sentinel (§5.2) | Human console |
| `ark-admin` | root, socket-activated | Freeze and kill that work with a hung broker | Administrative socket |
| `ark-egress` | `ark-egress`, unprivileged | Provider credentials, per-job tokens, token metering (§5.3; adopted if S-CRED passes) | Per-driver credential files and per-slot API egress rules |

Rules that keep this small:

- The worker is the only process that talks to a session's X server, buses or CDP endpoint. Sensing, WM policy and verification are libraries inside it, not services. Calls that can hang (AT-SPI, OCR) run in restartable child processes of the worker, so a hung provider degrades one provider and never retires the generation.
- Display, buses, WM and worker share fate: v1 already retires the generation when any of them dies. Making the worker their parent turns three units (display, bus, worker) into one and removes the readiness handshakes between them.
- Connections are persistent. Nothing in the action path launches a command-line tool.
- All components ship in one image and one protocol definition (`ark-proto`), from which every binding is generated. A version mismatch refuses to start rather than negotiating.
- Phase 1 runs broker, workers and console only. Until slot identities land (action 1.4), a launcher script under the development user stands in for sessiond and the helper.

Four internal interfaces, each with a contract test suite in CI:

| Interface | Shape |
|---|---|
| I1 driver / console ⇄ broker | JSON-RPC over a Unix socket (§4) |
| I2 broker ⇄ worker | Framed messages, same schema; every action carries a broker-issued `journal_seq`; the worker accepts actions from no one else |
| I3 broker ⇄ sessiond | Three verbs: `start(profile_hash, slot, generation_id)`, `stop`, `inventory` |
| I4 worker ⇄ input helper | Event batches tagged with slot and generation over a per-session socket; the helper checks the control page before every write |

### 3.2 One authority per fact

Every fact has exactly one owner. Everyone else holds a stamped copy that is rejected when stale.

| Fact | Single authority | How others use it |
|---|---|---|
| Who may mutate a session right now (state, control epoch, permit deadline) | The **control page**: one shared-memory page per session, written only by the broker, mapped read-only (sealed against writes) into the worker, helper and console | Read at the point of use. Never cached across an IPC hop. |
| What the session looks like (windows, focus, modal, trees, pixels) | The worker's state cache, totally ordered by one per-session **`rev`** | Broker and drivers hold `rev`-stamped copies. |
| What was intended, authorised and done | The broker journal, single writer. **The receipt is the audit record.** | sessiond, console and drivers query it. No other log has authority. |
| What is alive | systemd units and cgroups | The broker reconciles one way only: alive without a live generation → killed; generation without a cgroup → marked lost. |
| What a session is allowed to be | One signed, immutable **profile**, referenced by hash in every receipt | Units, device templates, WM config, forwarding, limits, egress rules and route hints are generated from it. |
| Which device belongs to which session | The generation record, written from the helper's kernel-verified reply | `/run/arkdrive/devices/` is a derived view. |
| What is ready | One health document from the broker (`system.info`) | Boot health, console and admission read it. Nobody computes a private green light. |
| Time | Host `CLOCK_MONOTONIC` for every duration; wall clock only in journal segment headers | No time namespaces. |

**The single `rev`.** The worker stamps every event it ingests (X lifecycle, focus, property, damage batch, AT-SPI, CDP, control transition, action completion) with the next value of one per-session counter. Each state facet remembers the `rev` at which it last changed: `geometry`, `focus`, `modal`, `tree[window]`, `pixels[region]`, `transform[space]`. Consequences:

- An observation is "as of rev N". A precondition is "this facet has not changed since rev N", checked with one integer comparison and no round trip.
- Events, receipts, frames and coordinate transforms all cite the same counter, so "was that tree snapshot taken before or after the dialog mapped?" has one answer. v1's `geometry_rev`, semantic-ref revision, event `seq` and frame id collapse into it.
- The order is arrival order at the worker. X and AT-SPI events arrive on different connections, so it is a single consistent order, not a proof of causality. Predicates are written against state, not against event sequence.

**The control page** holds `{generation_id, control_epoch, state, permit_deadline_ns, lease_hash}`. Takeover, pause, lease expiry and broker death are all the same mechanism: the page stops authorising writes. The worker checks it immediately before writing to the X connection; the helper checks it immediately before writing to uinput.

### 3.3 Window namespace and ownership

Handles are opaque and generation-bound: `s-A/g3/w7` (session A, generation 3, window 7). XID, title, PID, app ID and AT-SPI reference are supporting metadata, never authority. Each generation also has a random 128-bit `generation_id` so a handle cannot be confused across a broker database reset or reinstall.

Maintain a window family (toplevels, transients, menus, override-redirect popups) from `WM_TRANSIENT_FOR` and client relationships, without depending on perfect metadata: an unexpected dialog inside the session must still be observable.

Every session has private: DISPLAY, XAUTHORITY and runtime sockets; D-Bus session and accessibility buses; browser profile and debugging endpoint; clipboard selections and working directory. AT-SPI uses a separate accessibility bus discovered through the session bus, so changing only DISPLAY is not isolation. Test single-instance behaviour explicitly: a new Thunar or Chromium launch must not reconnect to a resident process in another session.

Authority follows decision 1. A lease is per session and carries scopes (`observe`, `act.semantic`, `act.input`, `control`) and optionally a family restriction. Physical input is authorised for the whole session, because a raw shortcut can change windows or create new ones; a family-restricted lease can observe and perform validated semantic operations but cannot promise confinement for arbitrary keyboard input. Two agents that need simultaneous physical control get two sessions. Existing human-desktop windows are reopened inside a session; transparent migration between X servers is out of scope.

### 3.4 Session lifecycle and startup

A generation moves one way: `WARM → BOUND → ACTIVE → DRAINING → RETIRED`. It is never recycled. A retired generation's cgroup must be empty, its devices gone and its secrets revoked before the slot identity is reused.

**Warm pool.** After owner login (or the first authorised job), the broker keeps up to two warm sessions, never more than the free admitted slots: display, buses, WM and worker are up and self-tested; there is no application, no job credential, no lease and no driver. `session.create` binds a matching warm session, starts the applications unit with the profile's mounts, and issues a lease. The pool is keyed by (backend, resolution); the default is two Xvfb sessions at 1920×1080. Anything else starts cold. This replaces v1's single optional prewarmed session and still starts zero task sessions at boot.

**Readiness is event-driven.** The display server reports its display number on a file descriptor when it is accepting connections; the bus daemons report their addresses the same way; the worker notifies systemd only after it has connected to all of them and passed its self-test. No step sleeps or polls.

| Startup budget (targets) | Value |
|---|---|
| Warm bind: `session.create` → lease issued, excluding application launch | ≤300 ms |
| Cold Xvfb session: request → worker ready | ≤2.5 s (namespaces and units ≤600 ms; display ≤400 ms; buses ≤400 ms; WM ≤200 ms; worker connect and self-test ≤400 ms; slack 500 ms) |
| Two cold Xvfb sessions in parallel | ≤3 s (v1: ≤10 s) |
| Cold Xorg-dummy/evdev session | ≤4 s (adds device creation and udev settle ≤500 ms, Xorg start ≤1.5 s, 200-event routing self-test ≤300 ms) |
| Pool refill after a bind | ≤5 s, at lower priority than anything the human is doing |
| Graceful stop | ≤2 s typical; hard cap 15 s, then kill with a loss receipt (v1) |
| Application launch | Measured per profile in Phase 0; budget is measured p95 × 1.2; reported separately |

### 3.5 Input arbitration and dispatch

One ordered mutation queue per session; no global input queue. Every mutation route, including CDP and AT-SPI, passes the same gate. Otherwise a paused agent could keep changing the application through semantics while the human holds its keyboard.

The dispatch path, with where each check runs:

1. **Broker:** authenticate the peer, verify the lease MAC and scope, check generation and control epoch, deduplicate the action ID, append the intent to the journal, enqueue. Budget ≤1 ms.
2. **Worker, per step:** check preconditions against facet revisions (integer comparisons); resolve the active modal target; confirm focus from the cached focus events, setting and confirming it only when it is wrong; read the control page; write the input; flush once.
3. **Worker:** evaluate the step's postcondition on the event stream (§3.9). On success, continue to the next step. On anything else, stop the batch.
4. **Broker:** complete the receipt and make it durable.

Properties:

- **Multi-step actions.** One `window.act` carries up to 8 steps and a total deadline of at most 10 s. A step dispatches only if the previous step's postcondition was verified. Each step gets its own receipt entry. Takeover aborts between steps. This is the main speed lever: the driver spends one model turn where v1 spent one per primitive.
- **No wasted motion.** Zero fork/exec per action (enforced by counting `execve` in the worker cgroup during the dispatch benchmark). Focus is not re-queried when no focus or map event has arrived since it was last verified. Text is sent as one burst with one flush; a profile can set an inter-key delay for applications that drop keys.
- **No dispatch without a journal intent.** The worker refuses any action that lacks a broker-issued `journal_seq`.
- **Honest uncertainty.** A focus change or grab that arrives between `rev` at dispatch and `rev` at verification yields an uncertain or failed receipt. Focus checks reduce races; they do not make asynchronous X11 applications transactional.

The corrected F2 route is unchanged from v1: agent → session queue → session-local focus → session keyboard → Thunar. Host XFCE never receives it. Human typing through the viewer is different: host shortcuts may intercept keys unless takeover captures and forwards them or the console offers an explicit "send F2" control.

### 3.6 Ladders, capability-aware

Keep the existing action interface and scope every rung to a session. CDP handles Chromium page content. AT-SPI handles exposed controls when provider and target are healthy. Verified shortcuts and coordinate input handle dialogs and canvases. OCR or vision locates or interprets a target; an input backend performs the action. Clipboard paste stays inside the session and performs a real selection transfer. xdotool and XTest-based cua are not independent failure domains just because they are different executables.

- **Do not climb every rung on every action.** Keep a route table per surface: which provider works, how healthy it is, how long it takes. Persist it per profile, keyed by application version, toolkit and surface role, so a new session starts on the right rung. Target: the first route tried succeeds on ≥95% of actions after warm-up.
- **Degrade promptly.** When Thunar's accessibility disappears, bypass that provider and probe recovery with backoff: 1 s, 2 s, 4 s, capped at 30 s.
- **Reconcile before retrying.** After a timeout with unknown dispatch outcome, observe first. Otherwise fallback creates duplicate folders, text or clicks.

### 3.7 Where uinput fits

Xvfb cannot consume evdev input; its route is session-local XTEST. A request for uinput on an Xvfb session returns `backend_unsupported`. Global host ydotool is never a fallback.

The evdev route, proven at spike scale by Spike B (23/23 with host exclusion): broker → worker → helper → per-session uinput device → designated evdev node → session Xorg evdev driver → application. uinput creates devices and delivers events to whoever reads them; it knows nothing about agents or windows. Routing is entirely our configuration, so it is layered:

1. Host exclusion rules are installed before any device can be created.
2. The host X server ignores the ArkDrive device namespace by two independent matches (a device-class ignore rule and a udev ignore property).
3. The session Xorg disables automatic device addition and declares only its assigned keyboard and pointer.
4. The session Xorg takes an **exclusive kernel grab** on its assigned devices, so no other reader receives their events even if a rule above is wrong. (Add this if it is not already part of the Spike B configuration; S-B2 qualifies it.)
5. No physical keyboard or mouse node is visible inside a session.
6. Agents are enabled only after a startup test shows the assigned devices reach the session's test surface and nothing reaches the host or a sibling.
7. The host sentinel watches continuously (§5.2).

If the evdev route proves unreliable on a platform, keep Xvfb for qualified applications and use a nested compositor backend for rejecting surfaces. Never trade isolation for a fallback rung. Xpra's own uinput creation stays disabled; a second injector would bypass ownership.

### 3.8 Human preemption

States: `AGENT → QUIESCING → HUMAN → PAUSED → AGENT`.

On takeover the broker writes a new control epoch and state to the control page. That single write is the gate closing: the worker and helper read the page at the point of use, so the worst case is one primitive that was already written. Then: reject new agent mutations, cancel queued steps, release tracked pressed keys and buttons, account for dispatched operations, enable human input.

| Takeover budget (targets) | Value |
|---|---|
| Takeover request at the console → gate closed | ≤10 ms (v1: <50 ms) |
| Queued steps cancelled, pressed keys and buttons released | ≤30 ms |
| Human input live in the tile | ≤100 ms |
| Mutation permit lifetime / renewal interval | 500 ms / 100 ms (v1 heartbeat: 2 s). A hung broker closes every gate within 500 ms. |
| `ark-admin` freeze (cgroup freeze of applications and driver) | ≤100 ms, with no broker and no model involved |
| `ark-admin` kill | ≤2 s |

- Closing the gate does not stop an already-issued CDP command, VNC action or application operation. Show outstanding effects during QUIESCING.
- Old-epoch commands never resume. Resumption requires a fresh observation and a new lease.
- The 500 ms permit fails safe: a stalled broker costs agents a `permit_expired` rejection, never an unauthorised write. If false closes exceed 1 per 10,000 actions in the Phase 2 soak, the lifetime reverts to v1's 2 s (decided at G2).
- **Human input path (spike S-XPRA, no Xpra patch either way).** Pass: the broker flips stock Xpra between read-only and read-write per session at runtime and read-only leaks nothing; Xpra stays the human input transport and the broker reads the mode back before announcing HUMAN. Fail: Xpra stays permanently read-only as a pixel transport, and takeover input goes console → broker → worker under a human lease, through the same gated path as agent input.
- Ownership is enforced on the session side for keyboard, pointer, focus, clipboard and every other mutation. A read-only-looking viewer is not enforcement.
- In observe mode: incidental human motion does not reach the session; agent activation requests do not focus host windows; dialogs appear in their tile or as non-activating proxies; agent cursor movement never moves the host pointer.
- On X11 a global escape shortcut can be grabbed. The administrative socket and the cgroup freezer are the override that cannot be.

### 3.9 Event and sensing plane

The worker subscribes, per session, to: X create/map/unmap/destroy/configure/property events; focus, active-window, transient and selection changes; XDamage on relevant drawables; AT-SPI object, text, focus and property events and provider liveness; CDP navigation, DOM and accessibility events where supported; disconnects, restarts and stream gaps. Every event gets a `rev`. XDamage says which pixels changed; it does not say the task succeeded.

**Shadow framebuffer.** The worker keeps one shared-memory copy of the session's pixels and refreshes only damaged rectangles. On Xvfb the server's framebuffer can be memory-mapped directly; on Xorg-dummy the worker uses shared-memory image requests; obscured windows use composite-backed window pixmaps. Crops, hashes, OCR input and evidence frames all come from this one buffer, at source resolution, never from the scaled preview. Spike S-CAP qualifies the fast path and its coexistence with Xpra.

**Observation ladder, cheapest first; stop at the first conclusive answer:**

1. Nothing relevant changed since `since_rev` → say so. No capture, no tree walk.
2. Cached window, modal and focus state plus a targeted semantic delta.
3. Hash of damaged tiles only (hashing measured at ~1.6 ms per region) to suppress unchanged evidence.
4. OCR on damaged text-bearing regions, cached by content hash (OCR measured at 170 ms on the tested crop).
5. Vision, only for an unresolved spatial or canvas predicate (measured at 5,300 ms).

**Verification uses the same engine.** A postcondition and a `window.wait` predicate are the same predicate type evaluated against the same cache. The observation handed to the model and the evidence in the receipt come from the same `rev`. The filesystem oracle used in benches stays independent of all of it.

Tuning values (heuristics, to be tuned against measurements):

| Parameter | Start value |
|---|---|
| Damage coalescing | 8 ms; 16 ms when damage exceeds 500 events/s |
| Visual quiet window | 100 ms cap for one-shot dialogs (v1), with early exit on the first conclusive semantic or event predicate |
| Model-facing response | Delta: ≤40 nodes or 2 KiB. Full snapshot: ≤200 nodes or 16 KiB with continuation (v1) |
| Evidence crop sent to a model | ≤1,024 px on the long side by default; a full frame only on explicit request |
| Accessibility circuit breaker | 250 ms (v1); two consecutive breaches or one call over 1 s marks the provider degraded |
| AT-SPI full snapshot (measured 130 ms) | At most one per window per tree change; later reads come from the cache |
| Event subscription buffer | 1,024 events; overflow sends `resync_required` with the current `rev` |
| Watchdog resync | Every 5 s, low priority, to catch missed events |

Blinking carets, animations, VNC updates and renders need region masks and task-specific predicates. Event-driven sensing removes routine screenshot polling; it does not remove pixel inspection or remote-state verification.

### 3.10 Presentation

Xpra presents sessions on the human desktop: desktop mode first (one tile per session, Openbox inside the session), seamless mode later (Xpra owns the WM role; no competing Openbox). Seamless mode is a timeboxed spike (S-SEAM), not an assumption.

- The console draws labels, control state, cursor overlays and progress from the broker's event stream only. Xpra supplies pixels and nothing else, so the console and the session cannot disagree about who is in control.
- Limits: selected session up to 30 fps, thumbnails up to 5 fps on change, invisible sessions no preview stream. Source-side verification stays active regardless.
- Targets: damage → pixels in the selected tile ≤50 ms; host keystroke-to-echo regression ≤5 ms at N=4 compared with no sessions running.
- The preview and the shadow framebuffer are two capture pipelines per session. That duplication is accepted for OS v1. If G2 measurements put preview capture in the top three CPU costs, a shared-memory local viewer replaces Xpra for desktop-mode tiles; otherwise it is not built.
- Xpra is reachable only on a Unix socket authenticated by peer credentials, with file transfer, printing, audio, clipboard sync, notifications, remote command start, URL opening, webcam and network advertisement disabled.

### 3.11 Wayland and application compatibility

| Application | Initial X11 session | Future Wayland backend |
|---|---|---|
| Chromium | Dedicated profile and CDP; native-dialog fallback | CDP remains; compositor handles native UI |
| Thunar, Calculator | AT-SPI plus X events, OCR and input | AT-SPI remains; compositor replaces X-specific sensing and input |
| Virtuoso through VNC | Treat the viewer as a canvas | Native or Xwayland viewer; remote application unchanged |
| Direct Virtuoso | Native X11 qualification | Separate Xwayland session; qualify fonts, GLX, dialogs |
| Blender | Headless-EGL launch profile (Spike C: 8.5×); Cycles on GPU blocked on VRAM until S-C2 decides | Native or Xwayland, driver-dependent |

Accessibility and input injection are separate mechanisms. The tested CLI's refusal to handle web pages is a tool policy, not proof that browsers lack accessibility; CDP stays ArkDrive's browser provider. XTEST is not a native-Wayland injection API. Host uinput still needs explicit compositor device-to-seat routing. A compositor you control can accept private actions or libei events and authorise them before seat delivery.

Two limits that no window manager removes:

1. **VNC.** Two local viewers on the same remote desktop share its remote pointer and focus. Multi-Virtuoso needs independent remote sessions plus CAD file ownership and licence capacity.
2. **Graphics.** A virtual framebuffer renders in software unless the application is launched through the GPU profile. Xpra client-side OpenGL does not accelerate Blender. GPU memory is shared and not partitioned per session.

### 3.12 Latency and capacity budgets

All rows are targets at p95 on the reference host unless marked measured. Report each separately; never sum them into a task latency.

| Stage | v2 target | v1 |
|---|---|---|
| Driver adapter → broker → worker IPC, both hops | ≤1 ms | not stated |
| Broker: validate, journal intent, enqueue | ≤1 ms | ≤2 ms |
| Request received → input written, focus already verified | ≤3 ms | not stated |
| Same, when focus must be set and confirmed | ≤10 ms | ≤10 ms |
| evdev: helper write → event at a session test client | ≤5 ms | not stated |
| Text entry, 100 characters, default profile | ≤50 ms | not stated |
| Event → state cache (includes 8 ms coalescing) | ≤10 ms | ≤20 ms |
| Observe, nothing changed | ≤1 ms in the worker; ≤3 ms end to end | not stated |
| Observe, cached delta | ≤5 ms | ≤10 ms |
| Fresh 1080p crop / full frame, excluding OCR and vision | ≤10 ms / ≤15 ms (reverts to ≤30 ms if S-CAP fails) | ≤30 ms |
| Event-class postcondition decided after the causal event arrives | ≤20 ms | not stated |
| OCR-class postcondition on a comparable crop | ≤250 ms | not stated |
| Receipt durable after the outcome is known | ≤10 ms | not stated |
| **Local overhead per event-verified action** (everything except model and application time) | **≤50 ms** | not stated |
| Takeover request → gate closed | ≤10 ms | <50 ms |
| Four-session local-control latency | within 2× the one-session value | same |
| AT-SPI snapshot | measured 130 ms | |
| Semantic click | measured ~200 ms | |
| OCR / vision | measured 170 ms / 5,300 ms | |
| Hash comparison | measured ~1.6 ms | |

Reading the table: ≤50 ms of local overhead is 0.5–0.8% of the measured 9.35 s and 6.19 s per action. The remaining time is model generation and application response, which is why the turn-count targets matter more than any row above:

- **Thunar create-and-rename:** tokens ≤50% of a newly passing baseline at equal correctness (v1 target), driver turns ≤50% of that baseline's action count, zero vision calls. The historical 22,102-token failed run stays visible and is not the baseline.
- **Virtuoso:** report tokens, turns and vision calls; no reduction is promised for a canvas-heavy workload.

Capacity arithmetic (from v1): one 1920×1080×4 frame is 8.29 MB; four sessions with three buffers each (server, shadow, preview) is 99.5 MB before application, server and GPU allocations; four full-frame 60 Hz copies cost 1.99 GB/s per copy pass, which is why capture is damage-driven and previews are rate-limited. Start with two active sessions, then qualify four.

"Close to the kernel" helps routing and one XTEST rejection path. It does not remove model generation, application event loops, GPU readback or VNC delay.

## 4. Agent-facing contract

JSON-RPC 2.0 over a Unix-domain socket, length-prefixed messages. One protocol serves drivers, the console and the admin CLI; what a peer may call is decided by its identity and lease, not by which socket library it used. Pixels move as sealed shared-memory frame references identified by `(rev, rect)`, never as image bytes in JSON. Every call carries `deadline_ms`; the broker never holds a caller past it. OS v1 is local-only: there is no remote gateway (§7.3, N3).

**Identity at connect** (detail in §5.5): each driver slot has its own socket; the broker checks kernel peer credentials and the peer's cgroup, then issues a lease bound to that peer. Leases renew automatically while the connection is alive and lapse 5 s after it drops.

### Core calls

| Call | Input | Output |
|---|---|---|
| `session.create` | Profile hash, resource and backend requirements | Session, generation, lease with scopes, capability summary, current `rev`. Binds a warm session when one matches. |
| `window.observe` | Scope (session, family or window), `since_rev`, subtree or crop, budgets | State or delta as of a `rev`, provider health, evidence references. Scope `session` replaces v1's `window.list`. |
| `window.act` | Action ID, lease, `base_rev`, up to 8 steps (intent, requirement, expectation, timeout), total deadline | Receipt, including the state delta after the last executed step |
| `window.wait` | Predicate, `since_rev`, deadline | Evidence or timeout |
| `window.subscribe` | Filters, `from_rev` | Ordered events, or `resync_required` with the current `rev` |
| `action.get` | Action ID | Durable outcome after a reconnect |
| `control.set` | Target state (`pause`, `takeover`, `resume`); owner or admin identity | Transition and new control epoch. Replaces v1's three control calls. |
| `clipboard.transfer` | Source and destination; owner identity only | Journaled transfer (hash and size) |
| `session.stop` | Graceful or kill | Retired generation and cleanup result |
| `system.info` | — | Health document; build, image and boot IDs; profile status |

v1's `window.claim` is gone: scopes and family restrictions are properties of the lease. The driver adapter exposes four tools to a model (`observe`, `act`, `wait`, `done`), which keeps tool definitions small in every prompt. Codex drivers and API-model loops use the same adapter, as one long-lived process per driver that holds the connection and the lease.

### Example: rename in one driver turn

```json
{"jsonrpc":"2.0","id":41,"method":"window.act","params":{
  "session":"s-A","lease":"<opaque>","action_id":"rename-004",
  "base_rev":1871,"deadline_ms":4000,
  "steps":[
    {"target":"s-A/g3/w7",
     "require":{"focus":"s-A/g3/w7","modal":null,"ref":"tree:folder17"},
     "intent":{"kind":"key","keys":["F2"]},
     "expect":{"kind":"dialog_present","role":"rename"},"timeout_ms":1500},
    {"require":{"modal_role":"rename"},
     "intent":{"kind":"text","text":"reports-2026","replace":true},
     "expect":{"kind":"text_equals","ref":"modal:entry","value":"reports-2026"},"timeout_ms":800},
    {"intent":{"kind":"key","keys":["Return"]},
     "expect":{"kind":"all","of":[
        {"kind":"dialog_absent","role":"rename"},
        {"kind":"item_present","name":"reports-2026"}]},"timeout_ms":1500}
  ]}}
```

`require` entries are valid only if the named facet has not changed since `base_rev`. If step 1's dialog never appears, steps 2 and 3 are never dispatched. The receipt verifies what its predicates say and nothing more: the bench still confirms the final folder name through the filesystem oracle.

### Receipt v2

Extends the existing `{version, phase, dispatched, effect}`:

- Identity: `session`, `generation`, `window`, `action_id`, `journal_seq`, `profile_hash`, `control_epoch`.
- Ordering: `rev {base, dispatched, verified}`.
- Per step: `route`, `attempts[]` (each route and whether it was accepted), `focus {before, after}`, `effect {status, predicate, evidence[]}`, `timing_ms {queue, dispatch, verify}`.
- `dispatch_status`: `not_dispatched | accepted | unknown`. The existing boolean is kept for compatibility.
- `state_delta`: the bounded observation after the last executed step, so the driver needs no follow-up `observe`.

### Required semantics

- Distinguish definitely-not-dispatched, accepted and unknown delivery.
- `effect.status` is `verified | not_observed | contradicted | unknown`.
- Reasons are an enumerated set: `preempted`, `stale_rev`, `stale_generation`, `lease_expired`, `permit_expired`, `timeout`, `backend_unsupported`, `provider_degraded`, `queue_full`, `budget_exceeded`, `confirm_required`.
- Deduplicate action IDs within a session generation.
- Preserve every attempted route and its outcome.
- Never treat a server flush as proof the application processed input.
- Never promise exactly-once effects across a crash between journaling and delivery.
- A multi-step action runs under one control epoch; an epoch change aborts it between steps.
- At most one action executes per session at a time; at most 8 wait in the queue.
- **One canonical coordinate space:** session desktop pixels at scale 1. Window-content pixels, preview coordinates, CDP CSS pixels, decorations and remote-VNC coordinates are named spaces with versioned transforms. A driver passes `{space, x, y}`; only the worker converts.
- After an unclean machine shutdown, any action of a generation that was live at the last durable checkpoint and has no durable receipt is reported `unknown`, never `not_found`.

### Events

Versioned envelope: `{version, session, generation, rev, monotonic_ns, kind, window, payload}`. `rev` is the order; there is no separate sequence number. Kinds: window lifecycle, modal and focus changes, damage, semantic changes, control transitions, action completion, `provider.health`, backend restart, `resync_required`. Example `provider.health` payload: `{provider: atspi, state: degraded, reason: application_unreachable, fallback_available: [pixels, keyboard]}`. Ordering is per session.

## 5. Security and safety model

### 5.1 What "trusted automation first" covers

Decision 2 stands: the first release trusts the applications it launches and the tasks it is given, and does not claim to contain hostile code. v2 does not reopen that. It tightens what happens inside the decision, on one observation: **trusting an application is not trusting the content it displays.** A web page, a document or a remote desktop can carry text aimed at the model. So the controls below assume a driver can be misled even when every binary is trusted: drivers hold no long-lived secrets, cannot reach the network except through the broker and the model proxy, and cannot perform the listed high-impact action classes without a human.

Display isolation, capability enforcement and OS containment remain three separate boundaries.

### 5.2 Boundaries

| Boundary | Control |
|---|---|
| Agent → broker | Per-slot socket; kernel peer credentials and cgroup check; lease MAC'd with a per-boot key and bound to the peer; scope, generation, epoch and expiry checked on every call |
| Agent → display | No X socket or cookie, accessibility bus, CDP endpoint or device node. The driver lives in a different namespace from all of them. |
| Session → session | Private servers and IPC; separate slot UIDs from action 1.4 onward; private network namespace per slot |
| Session → host files | Private home, profile and working directory; explicit mounts; explicit output export. Never the owner's home, SSH agent, keyring or session bus. |
| Kernel input | Non-root helper is the only uinput holder; fixed device templates; exclusive grab; two-layer host exclusion; host sentinel; no `input` group membership anywhere |
| GPU | Render nodes only, never the KMS primary node; a session can never become DRM master; shared GPU memory is admission-controlled, not partitioned |
| Presentation → host | Trusted viewer on a peer-authenticated Unix socket; no automatic activation or URL opening |
| Forwarding | Clipboard, files, audio, printing and notifications off. Clipboard crosses a session boundary only through the owner's `clipboard.transfer`. |
| Network | No X TCP listener; applications get default-deny egress with a per-profile allowlist; drivers reach only the broker socket and the model proxy; no remote control gateway in OS v1 |
| Driver → model provider | Through `ark-egress` with a per-job token and spend cap (§5.4) |
| Human confirmation | Action classes a profile marks `confirm` return `confirm_required` until the owner approves at the console. Default list: entering credentials, payments or purchases, sending messages or mail, deleting or overwriting outside the working directory, changing account or security settings. |

### 5.3 Device scoping

- **One holder.** Only `ark-device-helper` opens `/dev/uinput`, by group permission, as an unprivileged user with a system-call filter. It never hands a uinput fd to anyone: a holder of that fd could destroy the device and create a differently named one that escapes host exclusion. The worker sends event batches; the helper writes them.
- **Fixed templates.** Capability bits, vendor and product IDs are fixed per device class in a reserved ArkDrive range. Names carry slot, generation and a per-boot nonce. The helper identifies devices by uinput sysfs identity and kernel event node, never by name alone, and event numbers are never hardcoded across boots.
- **Exclusive grab.** Each session Xorg grabs its devices in the kernel. A misconfigured host rule then yields no events instead of leaked events.
- **Two-layer host exclusion**, installed before the helper can run: a host X device-class rule ignoring the ArkDrive namespace, and a udev ignore property on the same devices. Either alone must hold; S-B2 tests each with the other removed.
- **Fail closed.** The helper refuses to create a device if the exclusion rules are not loaded, and reports a device ready only after confirming the new node carries the ignore property and the session's grab is in place.
- **Host sentinel.** The console runs a listener on the human display. If any device in the ArkDrive namespace appears in the host's input device list, or any raw event from one arrives, it fences every session at once and raises an alert. Target: detection ≤100 ms. v1 tested exclusion at startup; v2 also watches it continuously.
- **Per-identity proof.** As each ArkDrive identity, enumerating readable nodes under `/dev/input` returns only that slot's assigned devices. This is a release test.

### 5.4 Credential handling

- **Provider keys stay out of drivers.** `ark-egress` holds provider credentials and attaches them to outbound model calls. A driver gets a per-job token that is valid only for that job, only from that slot, with a token and spend cap, revoked at `session.stop`. A driver that is talked into dumping its environment has nothing durable to dump. The proxy also meters tokens per job, which gives every bench one consistent token count independent of driver self-reporting.
- **Spike S-CRED decides the mechanism.** Pass: `codex exec` and the API loops run through the proxy, and it is mandatory. Fail for a driver that needs its own login token: mount a per-job copy into that driver's private runtime as a service credential, readable only by its slot identity, and record that this token is exposed to that driver.
- **At rest.** Provider credentials and stored secrets are encrypted service credentials, sealed to the machine's TPM where one exists. That closes disk-removal exposure for these secrets without full-disk encryption. Without a TPM the v1 exposure stands (root or physical disk access can read them), and the installer says so. Root on the running machine can read them in either case.
- **Typed secrets by reference.** A driver sends `{"kind":"secret","ref":"…"}`; the broker resolves it and the worker types it. The value never enters model context, a receipt or the journal; evidence capture of the target region is masked while it is entered. v1 redacted after the fact; v2 never exposes the value.
- **Never** in global environment files, image contents, application homes or logs. No baked token, no shared default password, no automatic billable call.

### 5.5 Session identity

- **Where a call comes from is checked by the kernel.** Socket path (one per slot, mounted only into that slot's driver namespace), peer UID, and the peer's cgroup via a process file descriptor, so PID reuse cannot impersonate a driver.
- **Leases are not bearer strings.** A lease is `(session, generation_id, control_epoch, scopes, expiry)` under a MAC keyed by a secret the broker generates at each boot and never writes to disk. It is bound to the peer identity and lives 60 s between automatic renewals. Copying it to another slot, or keeping it across a reboot, yields a rejection.
- **The owner is an identity too.** `control.set` and `clipboard.transfer` require the logged-in owner's UID on the console socket, or root on the admin socket.
- **sessiond trusts only the broker's UID** and accepts only `(profile_hash, slot, generation_id)`. Profiles live in the signed read-only image, so even a compromised broker can start only allowlisted profiles.
- **Machine identity.** A per-machine signing key is generated at install, TPM-held where possible, and signs journal checkpoints.

### 5.6 Update signing

Summarised here; the mechanism is in Phase 5, "Updates and rollback".

- Two-level PKI: an offline root in the image keyring; a release signing key on a hardware token that requires a physical touch; development keys are a different keyring that production images reject.
- Channel metadata is separately signed by a threshold of maintainers (2 of 3), carries a monotonically increasing release sequence and an expiry, and is checked before a bundle is downloaded. That covers rollback, freeze and mix-and-match attacks that a bundle signature alone does not.
- The installed sequence is recorded on STATE and in the boot environment. A lower sequence is refused unless the owner authorises a recovery rollback at the console or recovery TTY.
- ArkDrive's own packages must build reproducibly; whole-image reproducibility is tracked and reported.
- A key-compromise drill (rotate the release key, revoke the old one, prove an old-key bundle is refused) is part of gate 5.3.
- TLS alone is not update authentication. Signed images on a read-only root are not verified boot; Secure Boot and measured boot stay outside OS v1.

### 5.7 Audit trail

- **One writer, one record.** The broker appends receipts and control transitions to a hash-chained journal. There is no separate audit service that could disagree with the receipt store.
- **Completeness by construction.** No input is dispatched without a journal intent (§3.5), so a dispatched action missing from the journal is an integrity failure, not a logging gap.
- **Checkpoints that make the chain meaningful.** Every 60 s or 1,000 records the chain head is signed with the machine key. Checkpoints go to an append-only file under a separate identity and, when the owner configures it, off the machine. Hash chaining detects edits only against a checkpoint the editor could not rewrite; off-box export is what provides that against local root.
- **Evidence** is content-addressed. Workers write frames to a per-slot spool with a quota; the broker verifies the hash and moves them into its own store, so a session cannot rewrite evidence afterwards.
- **Coverage:** actor and lease hash; session, generation, epoch; request, selected route and every attempt; timings; dispatch certainty and effect evidence; human interventions; admin-socket actions (kept in a minimal independent log and merged when the broker returns); model calls by job, model and token count; build, image and boot IDs.
- **Verification is a gate.** `ark audit verify` must pinpoint a modified, deleted or reordered record in a tamper test (gate G3).
- **Retention defaults (policy, adjustable):** receipts 90 days, evidence frames 14 days, session recordings off. Screenshots and recordings containing private content are protected like credentials.

### 5.8 What remains true

- Separate X servers under one unrestricted UID prevent accidental interference; they are not a hostile-code sandbox. Slot UIDs and namespaces narrow ambient access; the kernel and GPU are still shared failure and security dependencies.
- Applications sharing one X server share an X11 trust domain.
- An allowed terminal or file manager has ambient filesystem and network powers inside its session. A window capability cannot confine those.
- Pause and kill work without a model call and without the broker, even when accessibility or vision workers hang.
- Hostile multi-tenant isolation is not an OS v1 claim.

## 6. Execution plan: owners, deadlines, gates

### 6.1 Operating rules

1. **Every action has one owner, one hard date, one gate, and a decision already written for failure.** Nothing is "ongoing".
2. **A gate has three outcomes on its date:** PASS; CUT (take the pre-declared fallback and continue); STOP (ArK decides within one working day). There is no fourth outcome where the date quietly moves. A re-plan is a STOP outcome with a recorded new date.
3. **Default on silence.** If a decision requested from ArK is not recorded within one working day, the default written next to it takes effect.
4. **Pull in, never drift.** A gate passed early starts the next action on the next working day.
5. **Gate runs need a quiet machine.** Both lanes share ArK2. Gate measurements run Thursday night into Friday with no builds running; Raj publishes the result on the deadline day.
6. **Daily status, three lines per lane:** gate colour, blocker, next action. A blocker older than one working day goes to ArK.
7. **Isolation defects are never cut.** Any host or sibling input delivery, any silent replay, any stale-epoch action executing is a STOP.

**Capacity basis (estimate).** Two implementation lanes for 26 calendar weeks is 52 lane-weeks; Dec 21–Jan 1 is counted at half capacity, leaving 50. v1 estimated 28–48 engineer-weeks for Phases 0–3 plus Phase 5. Hard deadlines are set against the upper end. The "earliest" column assumes v1's low-end estimates and first-time passes. Phase 4 is not in this budget.

### 6.2 Critical path

```
T0  Mon 2026-11-09
 │  1.1 runtime v0: broker + worker + two-session launcher        [R]
 ├─ D0  Fri 2026-11-13  scripted two-session parallel trace
 │  1.2 session-scoped ladders + driver adapter + two drivers      [R]
 ├─ D1  Fri 2026-11-20  EARLIEST REAL TWO-AGENT PARALLEL DEMO
 │  1.3 takeover, epochs, receipt v2, read-only tiles              [R]
 ├─     Fri 2026-11-27
 │  1.4 slot identities + 10,000-event harness [O]; 1.5 acceptance [Raj]
 ├─ G1  Fri 2026-12-04  Phase 1 gate: parallel MVP accepted
 │  2.1 sensing plane, single rev, multi-step act                  [R]
 ├─     Fri 2026-12-18
 │  2.4 failure injection, Virtuoso pair                           [R]
 ├─     Fri 2027-01-08
 ├─ G2  Fri 2027-01-15  Phase 2 gate: fast, compatible, N=4 measured
 │  3.x presentation, containment tests, durable audit             [R]
 ├─ G3  Fri 2027-02-12  Phase 3 gate: two-hour mixed soak
 │  5.1 owned service lifecycle on the image                       [R]
 ├─     Fri 2027-03-12
 │  5.3 update and rollback                                        [O+R]
 ├─     Fri 2027-04-09
 │  5.4 RC1 Fri 2027-04-16 → 14-day soak from Mon 2027-04-19
 └─ G5  Fri 2027-05-07  ArkDrive OS v1 stable-release decision
```

**What ships first:** the runtime thin slice (broker, worker, launcher), because everything else attaches to it and Spike A already proved the session it launches.

**What blocks what:**

| Blocked item | Blocked by | Not blocked by |
|---|---|---|
| D1 demo | 1.1 runtime v0; ladders scoped to a session; an approved API spend cap (A-BUD) | evdev, GPU, Xpra integration, slot UIDs, baselines, Debian, audit durability |
| G1 | D1; takeover and epochs (1.3); S-XPRA outcome; slot identities and the 10,000-event harness (1.4); the AT-SPI break from S-AT (trigger or fixture) | evdev backend, GPU |
| 2.1 fast sensing | G1 contract frozen; S-CAP | evdev, GPU |
| G2 | 2.1–2.4; baselines (P0.2) for the token target | Phase 5 work |
| 5.0 boot feasibility | S-DEB; 2.2 evdev backend; a dedicated target SSD (A-RIG) | G2, G3 |
| 5.1 owned lifecycle | G3; 5.0; S-CRED outcome | installer |
| 5.2 installer and hardware profile | 5.0; 2.3 GPU profile; NVIDIA redistribution review (A-NV) | 5.1 |
| 5.3 update and rollback | 5.1; 5.2; signing ceremony (A-KEY) | — |
| G5 stable release | 5.3; 14-day soak; named maintainers (A-OWN) | Phase 4 |

**Float (off the critical path):** evdev productisation (2.2) has float until 5.0 starts on Mon 2027-01-18. The GPU profile (2.3) has float until 5.2 starts on Mon 2027-02-08. Baselines (P0.2) have float until the G2 token comparison. S-CRED has float until 5.1.

**Earliest date a two-agent parallel demo can be real: Fri 2026-11-20 (T0+11 days).** "Real" means: two Codex/API-driven agents, each in its own isolated session, both using physical keyboard and pointer paths, running at the same time while a human types in a host editor with F2 still bound on the host; final folder names confirmed by the filesystem oracle; zero cross-delivery; recordings, receipts, timings and token counts published together. It is the low end of v1's 2–4 engineer-week MVP estimate spread over two lanes, and it holds only if:

1. Runtime v0 passes D0 on Fri 2026-11-13.
2. The existing ladders can be scoped by session environment. Any rung that needs a redesign to lose a global singleton is left out of D1 rather than delaying it.
3. A-BUD is approved by Fri 2026-11-13.

If either of the first two slips, D1 moves once, to Fri 2026-11-27, and no further without a STOP. D1 is not the Phase 1 gate: it has no 30/30 statistics, no takeover, no slot UIDs, no evdev and no GPU. The date on which the parallel MVP is *accepted* is G1, Fri 2026-12-04.

### 6.3 Master schedule

| ID | Deliverable | Owner | Hard deadline | Earliest (estimate) |
|---|---|---|---|---|
| D0 | Scripted two-session parallel trace | R | Fri 2026-11-13 (T0+4) | same |
| G0 | Phase 0 closeout: profiles classified | Raj | Fri 2026-11-20 (T0+11) | same |
| D1 | First real two-agent parallel demo | R, ArK | Fri 2026-11-20 (T0+11) | same |
| G1 | Phase 1 gate: parallel MVP | Raj → ArK | Fri 2026-12-04 (T0+25) | Fri 2026-11-27 |
| G2 | Phase 2 gate: sensing, actuation, N=4 | Raj → ArK | Fri 2027-01-15 (T0+67) | Fri 2026-12-18 |
| G3 | Phase 3 gate: presentation, containment, audit | Raj → ArK | Fri 2027-02-12 (T0+95) | Fri 2027-01-08 |
| 5.0 | Boot and device feasibility on Debian 13 | O | Fri 2027-02-05 (T0+88) | — |
| 5.1 | Owned service lifecycle | R | Fri 2027-03-12 (T0+123) | — |
| 5.2 | Installer and hardware profile | O | Fri 2027-03-12 (T0+123) | — |
| 5.3 | Update and rollback | O, R | Fri 2027-04-09 (T0+151) | — |
| RC1 | First release promotion | M-OS | Fri 2027-04-16 (T0+158) | — |
| G5 | ArkDrive OS v1 stable-release decision | ArK | Fri 2027-05-07 (T0+179) | Fri 2027-03-19 |

Human-only actions on the path:

| ID | Action | Owner | Deadline | If missed |
|---|---|---|---|---|
| A-BUD | Approve an API/model spend cap for bench and demo runs | ArK | Fri 2026-11-13 | D1 runs scripted only and moves to Fri 2026-11-27 |
| A-OWN | Name M-OS (primary OS/release/security maintainer) and M-RT (runtime maintainer and release backup) | ArK | Fri 2026-12-04 | If still unnamed at G2 (Fri 2027-01-15), Phase 5 images stay "experimental" and G5 cannot declare a stable release (v1's rule) |
| A-RIG | Provide a dedicated target SSD for image work on the reference machine; a second RTX 3060 rig if available | ArK | Fri 2027-01-15 | 5.0 takes ArK2 away from lane R; G3 and 5.0 both slip by the contention, recorded as a STOP |
| A-NV | Review NVIDIA redistribution and firmware notices before any image is published | ArK, M-OS | Fri 2027-03-12 | Images stay internal; 5.2 can pass technically but nothing is published |
| A-KEY | Signing ceremony: offline root, hardware-backed release key, three maintainer metadata keys | M-OS, ArK | Fri 2027-03-12 | 5.3 cannot start; its deadline moves day for day |

### 6.4 Spike register

Each spike is timeboxed. It passes, or the written decision takes effect that day. No spike is extended.

| Spike | Question | Timebox, owner, deadline | Pass | Decision if it does not pass |
|---|---|---|---|---|
| A | Private Xvfb + private accessibility bus isolates a session? | Done | **PASS 15/15 (measured)** | — |
| B | Explicit evdev routing with host exclusion? | Done | **PASS 23/23 (measured)** | — |
| C | GPU path? | Done | **8.5× via headless EGL (measured)**; Cycles blocked on VRAM | Open part goes to S-C2 |
| S-AT | Can the Thunar AT-SPI outage be reproduced on demand? | 2 days, O, Mon 2026-11-16 | A trigger and a detection signature | Stop root-causing. Build a fault fixture that produces the same symptoms (bridge killed or bus blocked) and use it for the Phase 1 bench. |
| S-B2 | Does routing hold at release scale? | 2 days, O, Wed 2026-11-18 | Zero host/sibling deliveries over 10,000 tagged events with USB hotplug, device destroy/recreate, exclusive grab on, and each exclusion layer removed in turn | evdev profile is `blocked` for release; Xvfb path continues (decision 3 allows this) |
| S-C2 | Can Cycles render on the GPU within the admission budget? | 2 days, O with ArK, Fri 2026-11-20 | With a per-process VRAM ledger and no non-ArkDrive GPU tenant, the reference scene renders within 10 GiB | Blender profile ships as "viewport and render through headless EGL where it fits; Cycles on CPU". Cycles-GPU is listed blocked and revisited only at G2. |
| S-XPRA | Can stock Xpra be switched read-only ⇄ read-write per session at runtime, with zero leakage when read-only? | 2 days, O, Tue 2026-11-24 | Zero events delivered over 10,000 injected attempts in read-only; switch ≤50 ms | Xpra stays permanently read-only; takeover input goes console → broker → worker. No Xpra patch in either case. |
| S-DEB | Does Debian 13 run rootless Xorg-dummy sessions without VT or seat ownership? | 3 days, O, Fri 2026-11-27 | Two sessions beside a human login in a VM and from a live USB on the reference machine; no VT switch, no DRM master | Add the smallest launch wrapper that passes; if none passes in the timebox, OS v1 is Xvfb-only and the evdev OS profile is blocked at 5.0 |
| S-CRED | Do `codex exec` and the API loops work through the credential-injecting proxy? | 2 days, O, Tue 2026-12-01 | Both driver types complete a task with no provider credential in the driver's environment or files | Proxy is mandatory for drivers that support it; the others get a per-job mounted service credential, with the exposure recorded |
| S-CAP | Does the shadow framebuffer meet the capture budget, alongside Xpra? | 2 days, R, Tue 2026-12-08 | Fresh 1080p crop ≤10 ms and full frame ≤15 ms at p95 over 5,000 captures | Keep v1's ≤30 ms target with shared-memory image requests |
| S-SEAM | Is seamless mode dependable enough for OS v1? | 5 days, R, Fri 2027-01-22 | Thunar and Chromium under Xpra WM ownership: grouped dialogs, correct takeover gating, no host focus theft over 200 dialog cycles | OS v1 ships desktop-mode tiles; seamless moves after OS v1 |

### Phase 0 — Qualification closeout (T0 → Fri 2026-11-20)

v1's three probes have run (Spikes A, B, C). What remains is instrumentation, baselines, and scaling the spikes to release size.

| ID | Action | Owner | Deadline | Gate | If it fails |
|---|---|---|---|---|---|
| P0.1 | Instrumentation harness and pinned manifest: queue, focus, backend acceptance, application effect, capture, OCR, vision, model time, tokens, retries; distro, kernel, Xorg, Xpra, GPU versions; resolution; warm/cold; VNC topology | O | Thu 2026-11-12 | One command produces the per-action timing split for a Thunar run; every run record carries the manifest hash | Baselines start anyway with wall time and tokens only; the split is owed by Tue 2026-11-17 |
| P0.2 | Baselines: 10× Thunar in one isolated session, 10× Virtuoso; failures retained | Raj | Tue 2026-11-17 | 20 run records published; Thunar ≥9/10 correct by oracle | Two days to root-cause. Still failing: the Phase 1 concurrency ratio uses the scripted workload, and model-driven success is reported separately (v1 already separates them) |
| P0.3 | S-AT | O | Mon 2026-11-16 | See register | See register |
| P0.4 | S-B2 | O | Wed 2026-11-18 | See register | See register |
| P0.5 | S-C2 | O, ArK | Fri 2026-11-20 | See register | See register |
| G0 | Classification record: `xvfb-xtest`, `xorg-dummy-evdev`, `gpu-egl`, `cycles-gpu`, each marked qualified, spike-pass or blocked, with evidence | Raj | Fri 2026-11-20 | Every profile has a status and a link to its evidence | An unclassified profile is treated as blocked |

### Phase 1 — Smallest parallel MVP (T0 → Fri 2026-12-04)

Build: two Xvfb sessions with minimal Openbox; private buses, profiles and working directories; session-scoped existing ladders; one queue per session; the control page; read-only Xpra tiles; server-enforced pause and takeover with epoch rejection; worker/applications and driver under separate slot identities; no global DISPLAY, home or driver singletons.

| ID | Action | Owner | Deadline | Gate | If it fails |
|---|---|---|---|---|---|
| 1.1 | Runtime v0: broker, worker as session init, two-session launcher from the Spike A setup, per-session queue, control page, XTEST dispatch, journal intent and receipt | R | Fri 2026-11-13 | **D0:** two scripted drivers run Thunar create/rename concurrently with physical key and pointer input; host F2 still bound; human typing in a host editor; oracle correct; zero cross-delivery over 1,000 tagged events | Isolation defect → STOP. Tooling incomplete → D1 moves once to Fri 2026-11-27 |
| 1.2 | Session-scoped ladders; driver adapter; two Codex/API drivers bound to their sessions | R | Fri 2026-11-20 | **D1:** three consecutive paired model-driven runs correct by oracle, meeting the definition in §6.2 | CUT, in this order: integrated tiles → hand-started read-only Xpra or recordings; a rung that needs redesign → excluded; either driver type may drive both sessions. Not cuttable: two model-driven agents, physical input in both, human typing, oracle, zero cross-delivery |
| 1.3 | Takeover and epochs; receipt v2; integrated read-only tiles; takeover input path per S-XPRA | R (O for S-XPRA) | Fri 2026-11-27 | Human takes A while B continues; every old-epoch A action rejected; gate closure <50 ms p95 over 200 takeovers (v1's target; the 10 ms target is enforced at G2) | One week on lane R only; lane O proceeds |
| 1.4 | Slot identities; 10,000-event cross-delivery harness; AT-SPI break wired in from S-AT | O | Fri 2026-12-04 | Driver identity cannot open the session's X, accessibility or CDP sockets; harness runs unattended | G1 cannot pass without it |
| 1.5 | Acceptance run | Raj | Fri 2026-12-04 | **G1**, below | See below |

**G1 bench (all must hold; unchanged from v1):**

- Two Thunar create/rename tasks → correct, distinct final folder names.
- A Thunar modal breaks AT-SPI in session A → A completes through fallback; B continues.
- Host F2 stays bound to Application Finder → agent F2 opens only its own session's rename dialog. Repeat on an owned desktop without the host binding, so removing the binding cannot hide wrong routing.
- Simultaneous coordinate and key traces → independent cursors and focus; no cross-delivery.
- Human takes session A → B continues; old-epoch A actions rejected.
- Host typing throughout → agent actions never redirect human input.

Final verification is by an independent filesystem oracle; agents still perform the real GUI task.

**G1 acceptance (unchanged from v1):** 30/30 paired Thunar task successes; zero host or sibling deliveries across 10,000 tagged events; correct takeover receipts and no stale-command replay; concurrency ratio ≥1.5× for two comparable non-GPU-bound deterministic workloads, computed as (isolated T_A + isolated T_B) / paired makespan. Model-driven runs are reported separately, because API scheduling can hide real input concurrency. Semantic Calculator alone is not proof; both sessions must exercise physical keyboard and pointer paths. v2 adds: the §3.12 dispatch and startup rows are measured and published at G1 and enforced at G2.

**If G1 fails:** any isolation defect → STOP and an architecture review within two working days. A task-success shortfall with zero isolation defects → lane O starts Phase 2 on schedule and lane R stays on G1 for at most one more week (Fri 2026-12-11); a second miss is a STOP.

### Phase 2 — Fast sensing and compatible actuation (Mon 2026-12-07 → Fri 2027-01-15)

| ID | Action | Owner | Deadline | Gate | If it fails |
|---|---|---|---|---|---|
| 2.0 | S-CAP | R | Tue 2026-12-08 | See register | See register |
| 2.1 | Event and sensing plane; single `rev`; shadow framebuffer; multi-step `act` with state delta; route table; circuit breakers | R | Fri 2026-12-18 | Over ≥5,000 primitive actions at N=1 and N=2, the §3.12 dispatch, observe and verify rows hold; zero `execve` in the action path; 1,000 injected stale-`base_rev` requests, none dispatched; Thunar completes in ≤50% of baseline driver turns | A row missed by less than 2× → the measured value becomes the budget, with ArK's sign-off. Missed by 2× or more → that fast path is dropped and v1's target stands. No open-ended tuning. |
| 2.2 | evdev backend productised: non-root helper, templates, exclusive grab, two-layer exclusion, host sentinel | O | Fri 2026-12-18 | XTEST-rejection fixture ignores XTEST and accepts only its assigned evdev devices; S-B2's criteria hold on the productised path; cold start ≤4 s | evdev profile stays `blocked`; G2 content is Xvfb-only; re-tested at 5.0, where a failure blocks the evdev OS profile |
| 2.3 | GPU profile and Chromium: Blender viewport interaction, dialogs, render per the S-C2 outcome, renderer recorded, VRAM admission enforced; Chromium CDP task plus native file chooser while another session holds a GTK modal | O | Fri 2027-01-08 | Both workflows pass 10/10; a dark-render/relight workflow is evaluated with planner behaviour reported separately | Blender ships as a CPU/UI profile, labelled; GPU Blender is not advertised |
| 2.4 | Failure injection and remote CAD: hung accessibility, disconnect/reconnect, stale refs, missed events, crash after dispatch, broker and helper loss, reboot during dispatch, evdev recreation, namespace and NVIDIA-mismatch fixtures. Virtuoso on independent remote VNC desktops: A opens fa, Fit, verify; B opens alu8, Fit, verify; with human activity and a local GUI side task. Negative control: two viewers on one remote desktop must show contention. | R | Fri 2027-01-08 | Every injected fault ends in a correct receipt status; zero silent replays; zero duplicate effects by oracle; the negative control fails as designed | Silent replay or duplicate effect → STOP. Virtuoso pair blocked by remote capacity or licences → marked `blocked: external` and reported; not an architecture failure |
| G2 | Measure at N=1/2/4; decide N5 (single backend), the permit lifetime and the shared-memory viewer; export machine-readable health and readiness for boot admission | Raj → ArK | Fri 2027-01-15 | Four-session p95 local-control latency within 2× the one-session value with human responsiveness preserved; Thunar tokens ≤50% of the new passing baseline at equal correctness; takeover gate closure ≤10 ms p95; all §3.12 rows enforced | N=4 fails and N=2 passes → proceed admitting two sessions; N=4 becomes a Phase 3 item. Token target missed → publish the measured reduction and proceed; it is a target, not an isolation property. A §3.12 row missed → the 2.1 rule applies. |

Reporting rules (from v1): success counts, median and p95 wall time, actions, input and output tokens, vision calls, provider outages, retries, wrong-target events. Dispatch percentiles need thousands of primitive actions; thirty task runs are not enough. The historical 22,102-token failed Thunar run stays visible.

**Single-backend rule (N5), decided at G2:** if Xorg-dummy/evdev passes 2.2, starts cold in ≤4 s, meets the capture budget and shows no regression at N=4, it becomes the default backend on the OS image and Xvfb remains for CI, VMs and development. That halves the application qualification matrix. Otherwise both stay.

### Phase 3 — Presentation, containment tests, durable audit (Mon 2027-01-18 → Fri 2027-02-12)

| ID | Action | Owner | Deadline | Gate | If it fails |
|---|---|---|---|---|---|
| 3.1 | S-SEAM | R | Fri 2027-01-22 | See register | See register |
| 3.2 | Containment proof: cross-session file, socket and device denial under slot UIDs and namespaces; default-deny egress; per-identity `/dev/input` enumeration; driver unable to reach display, CDP or accessibility sockets | R, O | Fri 2027-01-29 | Every cell of the denial matrix is denied | STOP for that boundary. No release with a failing denial. |
| 3.3 | Durable audit (hash chain, signed checkpoints, `ark audit verify`); application lifetime split from presentation lifetime; maintenance admission, drain and checkpoint states; reopen manifests; per-session resource profiles; build, image and boot IDs in audit and `system.info`; N/N−1 state and profile compatibility checks; cursor overlays; resource admission | R | Fri 2027-02-05 | Tamper test pinpoints a modified, deleted and reordered record; viewer detach leaves applications running; drain defers on unsaved work | Tamper test failing → STOP. Others → one week, lane R only |
| G3 | Two-hour mixed workloads at N=1/2/4 with Chromium, Thunar, VNC and Blender; human editing; rapid dialogs; hidden previews; detach/reconnect; resize; pause, kill and restart injection. Publish CPU, RSS/PSS, GPU memory, capture bandwidth, event backlog, human input latency. Phase 4 go/no-go (N6). | Raj → ArK | Fri 2027-02-12 | Zero unexplained input escapes; zero silent replays; published resource report; §3.10 human-latency target held | N=4 fails, N=2 passes → OS v1 admits two sessions; N=4 is qualified after release. Any escape → STOP. |

A display-server crash invalidates its generation and normally loses its GUI clients. Recovery means restart and reopen, not transparent application migration.

### Phase 4 — Optional Wayland backend (not scheduled)

Decision N6 at G3, Fri 2027-02-12; **default: no-go for OS v1.** If approved it is 8–16+ engineer-weeks (v1 estimate) and starts only after G5 or on a funded third lane, so it cannot take capacity from the critical path.

Scope unchanged from v1: one nested compositor per session, one conventional agent seat, its own Xwayland; broker contract unchanged; native capture and input; session-level compositor override. A nested compositor cannot make an XFCE/X11 host shortcut ungrabbable, so the administrative path stays. Promote this backend only after the compatibility benches are repeated and it shows better graphics, capture efficiency, or required native-Wayland support. Replacing the human desktop is a separate project.

### Phase 5 — ArkDrive OS: bootable, owned Linux stack (Mon 2027-01-18 → Fri 2027-05-07)

Ship a Debian 13 (trixie), amd64 appliance/workstation image: one human presentation desktop, the broker, and isolated agent sessions created on demand. This extends option D. It does not replace the broker contract, the input ladders, the verification model or the Codex/API drivers. No local executor model, inference service or model-weight download is required; GPU memory belongs to applications, capture and presentation.

Dependency: qualified Phases 0–3. Phase 4 stays optional; OS v1 uses an owned X11 presentation configuration. Boot and device work (5.0) overlaps Phase 3. A bootable ISO is not evidence that the earlier compatibility gates passed. v1 estimated 16–24 engineer-weeks; all timing and capacity numbers below are targets.

#### Milestones and gates

| ID | Milestone | Owner | Deadline | Pass/fail gate | If it fails |
|---|---|---|---|---|---|
| **5.0** | Boot and device feasibility | O | Fri 2027-02-05 (T0+88) | Minimal Debian image boots on the physical reference desktop; human login and two Xorg-dummy/evdev sessions coexist with no extra VT or seat ownership. Zero host/sibling deliveries over 10,000 tagged events with hotplug and recreation; the XTEST-rejection fixture receives only its assigned evdev input. | The evdev OS profile is blocked even if Xvfb works; OS v1 proceeds Xvfb-only and says so |
| **5.1** | Owned service lifecycle | R (O supporting) | Fri 2027-03-12 (T0+123) | Core units start offline; zero task sessions by default; two authorised Codex/API drivers work after provisioning. 30/30 paired Thunar tasks meet the G1 oracle and takeover gates; a failed API causes a visible wait, never a wrong-target fallback. Kill broker, sessiond, helper, presentation and a session worker separately: gates close, nothing uncertain is replayed, no sibling or host input, surviving or reopened sessions carry correct epochs and generations. 30 cold boots with readiness percentiles published against the boot targets. | Replay or escape → STOP. A boot or startup target missed → the measured value is published and ArK signs the new budget or orders a fix |
| **5.2** | Installer and hardware profile | O | Fri 2027-03-12 (T0+123) | USB install and reinstall onto a blank dedicated SSD; no baked identities or secrets; explicit erase target; safe failure on unsupported storage or GPU. Reference NVIDIA module and userspace qualified; Blender workflow matches the Phase 2 result; two-hour mixed N=1/2/4 suite passes at admitted capacities. One other person installs and recovers from the published instructions without private help. | Unsupported GPU capability stays visibly disabled. Instructions fail → fix and repeat with a different person within one week |
| **5.3** | Update and rollback | O (R supporting) | Fri 2027-04-09 (T0+151) | Signed A→B→A and N/N−1 state and profile recovery pass. Corrupt, wrong-signature, wrong-profile, expired-metadata, lower-sequence and old-key bundles are all refused. Power-cut and reboot injection at download, inactive-slot write, boot selection and unconfirmed boot, at least five trials each: every trial ends on a usable confirmed root or the documented USB recovery, never touching the active root or user data. A candidate with failing broker, GPU or routing health falls back after reboot. An unsaved task defers the update at drain timeout; an authorised forced stop records loss and uncertainty. | Any data loss or unrecoverable machine → STOP; no stable release |
| **5.4** | Release operations and soak | M-OS, M-RT | RC1 Fri 2027-04-16; soak Mon 2027-04-19 → Mon 2027-05-03; second promotion by Fri 2027-04-30 | Fourteen calendar days on the reference hardware: daily local task pairs, repeated owner login, lock and detach, two complete release promotions, no unexplained input escape, no silent replay. Earlier success, token and latency reports re-run against a passing same-hardware baseline; four-session p95 within 2× where admitted. Manifest, supported profiles, patch and rollback runbook, key-rotation recovery and named primary and backup owners published. | See G5 |
| **G5** | Stable-release decision | ArK | Fri 2027-05-07 (T0+179) | 5.0–5.4 all pass; A-OWN and A-NV complete | Any unresolved isolation, data-loss or unrecoverable-update defect blocks stable release. The image stays "experimental" and the installable ArkDrive path remains the supported product |

First OS milestone (observable and narrow): boot the reference machine from the image, log in to the human console, start two independent Codex/API-driven folder tasks while the owner types, then restart the broker and show closed gates with no replay. A branded login screen or a fast boot without that trace is not the milestone.

#### Base distribution and shipped stack

**Debian 13 stable, pinned by codename and package manifest.** Take Debian kernels, systemd, Xorg, Mesa/GLVND, firmware and security updates; maintain ArkDrive packages and integration policy. Do not maintain a kernel fork, a display-server fork or a general-purpose package archive.

| Candidate | Fit | Decision |
|---|---|---|
| Debian 13 | Stable base; Xorg dummy and evdev drivers; systemd and cgroups; Xpra documents Xdummy integration improvements in trixie | Pick. NVIDIA qualification and image updates are ours. |
| Ubuntu LTS | Equally credible foundation, strong driver provisioning; Xpra documents TTY and session-isolation problems on releases before Ubuntu resolute and Debian trixie | Runner-up. Not a claim that every Ubuntu release is unsuitable. |
| Fedora | systemd and modern graphics; atomic variants are attractive | No decisive input-routing advantage; more frequent base transitions than a small team wants. |
| Arch | Everything available; easy to test current graphics | Rolling changes widen the tested matrix. Development host only. |

These are product trade-offs, not claims that a distribution supplies isolation. Upstream Xpra identifies trixie as the first Debian baseline enabling Xdummy by default; that supports the choice and does not qualify our explicit evdev configuration (S-DEB and 5.0 do). [P5-S1–S3.]

Release manifest: Debian snapshot and security package versions; ArkDrive commit, API and receipt versions; Xpra version (unpatched); Xorg, dummy and evdev versions; kernel, firmware, NVIDIA module and userspace versions; browser, Blender, OCR and Codex driver versions. Dependencies go into a signed, retained build input set. Use current supported versions that pass qualification, not a frozen first snapshot. If Debian's Xpra lacks something needed, rebuild a pinned upstream release as an ArkDrive `.deb` with named security ownership; never add an unpinned third-party repository to installed machines.

Default desktop: LightDM plus a minimal Openbox session running the ArkDrive console and Xpra clients. LightDM provides PAM login and seat0/logind integration. Openbox runs inside an agent session in desktop mode only; Xpra owns the WM role in seamless sessions. The presentation Openbox is a different WM on a different X server.

#### Boot flow and identities

1. UEFI → GRUB selects a confirmed A/B root slot → that slot's kernel and initramfs. The initramfs mounts the persistent state partition and installs the bounded identity and configuration bind mounts before ordinary services read them. Root is read-only; `/run` and the default `/var` tree are volatile. An unmountable state partition goes to recovery, never to an empty replacement home.
2. systemd brings up udev, local mounts, time sync, networking and the static service identities. Host device-exclusion rules are already in the image. `ark-provision.service` validates the installer-created owner account, machine identity, state directories and slot configuration. Missing model credentials do not block the broker, owner login or recovery.
3. `ark-device-helper`, `ark-sessiond`, `ark-broker` and `ark-egress` start. The broker restores its journal, marks interrupted actions uncertain, expires old leases (the per-boot lease key guarantees this), and keeps mutation admission closed. Local health tests validate policy, runtime ownership and backend profiles; only passing profiles become available. Readiness does not mean a model provider is reachable.
4. In parallel, LightDM starts the physical Xorg server and greeter on seat0. Owner login starts the presentation desktop and `ark-presentation.service` in the owner's systemd manager; it connects to the already-running broker and starts the host sentinel. No agent process gets the owner's X cookie, home, bus or login environment.
5. The warm pool fills. An authorised queued job or human launch calls `session.create`; the broker binds a warm session (or sessiond starts one cold), applications start with the profile's mounts, readiness is verified, then the separate Codex/API driver starts and receives a fresh lease. The first model call follows this local preparation. A network or auth failure leaves the job waiting with a visible reason.

**Zero task sessions at boot.** Warm sessions hold no job, credential, lease or driver. Remembered jobs resume only under an explicit persistent unattended-job policy; the default after reboot is paused, fresh observation required. Boot-health fixtures are short-lived test sessions.

**Slot identities.** A fixed pool of eight in the image: `ark-app001`…`ark-app008` and distinct `ark-driver001`…`ark-driver008`; system users, locked passwords, no login, no sudo. Slots are an identity mechanism, not a promise of eight concurrent sessions. One slot belongs to one job or application group at a time. Reuse requires the old cgroup empty, virtual devices gone, secrets revoked and the private runtime and home detached. Avoid `DynamicUser=` for persistent browser or profile ownership in OS v1.

The application/worker identity reaches its private X, AT-SPI and CDP resources. The driver identity reaches only its own broker socket, its task files and the model proxy; it has no display or device access and no general network route. Codex's coding and shell abilities run inside this driver boundary; an unrestricted host `codex exec` would bypass it. Credentials follow §5.4.

Agent sessions are system units. No interactive logind seat or VT and no lingering desktop user manager per agent. The human alone owns seat0. A dummy session must run without becoming DRM master or switching VTs. GPU access, where qualified, is a separate render permission. Owner lock or logout pauses agent mutation by default and preserves applications; explicitly authorised unattended jobs continue without the presentation process.

| Boot target (reference SSD desktop, p95) | Value |
|---|---|
| UEFI handoff → broker locally ready | ≤30 s (v1 gate); 20 s stretch target |
| Owner login (or first authorised job) → warm pool of two filled | ≤5 s |
| Authorised request → two empty 1080p sessions ready | ≤300 ms each from the warm pool; ≤3 s cold for two (v1: ≤10 s) |
| Userspace start → boot-health decision | ≤120 s (v1 gate); 60 s target |

Report firmware, password entry, first provisioning, cloud/API connection and model latency separately. None of this turns the historical 159 s Virtuoso task into a boot-time or execution-time promise.

#### Service layout, ordering and failure policy

`arkdrive.target` is enabled under `multi-user.target`; it wants the core services, not task sessions. LightDM stays under `graphical.target`. Offline boot must not wait on `network-online.target`. Template instances and per-slot drop-ins are generated only by sessiond from immutable profiles. Per-slot services drop from seven to five.

| Unit | Identity, start and ordering | Restart and isolation policy |
|---|---|---|
| `ark-provision.service` | root, oneshot; after state mounts, before core admission | No automatic destructive retry. Idempotent steps and a completion marker; failure exposes local provisioning and recovery only. |
| `ark-device-helper.service` | `ark-input`; after udev and provision | `Type=notify`, `Restart=on-failure`, 2 s backoff. Only this unit can open `/dev/uinput`; fixed templates; registered session IDs; system-call filter. Loss revokes affected routes; devices are recreated only in a new generation. |
| `ark-sessiond.service` | root lifecycle actuator; after helper and provision | `Type=notify`, watchdog 10 s, restart on failure. Accepts only `(profile_hash, slot, generation_id)` from the broker's UID; no caller-supplied shell or unit text. Stateless: on start it inventories systemd and waits for the broker's reconciliation. |
| `ark-broker.service` | `ark-broker`; after sessiond and provision | `Type=notify`, watchdog 10 s, restart on failure, 2 s backoff. Private devices; writes only journal and evidence state. Its death stops permit renewal, so every gate closes within 500 ms without any other component acting. Restart never replays uncertain effects. |
| `ark-egress.service` | `ark-egress`; after provision | Restart on failure. Holds provider credentials as encrypted service credentials. Down means drivers wait visibly; sessions and applications are unaffected. |
| `ark-slot@NNN.target` | sessiond starts and stops on demand | Owns the units below via `PartOf=`; critical members also use `BindsTo=` and `After=`. Target ordering alone is not readiness. A failed critical member causes whole-generation cleanup. |
| `ark-runtime@NNN.service` | namespace keeper; root setup, then fixed profile | Private mount, IPC and network namespaces, runtime directory, isolated `/tmp` and device view before child units. No arbitrary mount or exec API. Failure ends the generation. |
| `ark-session@NNN.service` | `ark-appNNN`; after runtime and device registration | The worker as session init: display server, session and accessibility buses, WM, provider children. notify and watchdog. No blind restart: its loss invalidates every reference, retires the generation and offers reopen. Hung AT-SPI or OCR children restart independently and report provider degradation. No physical input nodes. |
| `ark-apps@NNN.service` | `ark-appNNN`; after the worker's readiness | Application exit reports job state; no unconditional relaunch. |
| `ark-xpra@NNN.service` | `ark-appNNN`; after the worker's readiness | Stock Xpra with the forwarding policy of §3.10. Viewer detach preserves applications. Loss of the Xpra WM in seamless mode ends the generation; desktop-mode capture recovery may be qualified separately. |
| `ark-driver@NNN.service` | `ark-driverNNN`; after application readiness and broker authorisation | `Restart=no`. Reconnect or retry is a broker job-policy decision with a new observation and lease. Model or API errors cannot restart application mutations silently. Separate namespace from display and CDP sockets. |
| `ark-presentation.service` | owner's user unit; part of `graphical-session.target` | Restart on failure with limits. UI failure detaches previews and does not kill sessions; default policy pauses mutation. Runs the host sentinel. |
| `ark-update-check.timer` / `.service` | unprivileged checker; daily, randomised delay; no boot dependency on the Internet | Verifies threshold-signed channel metadata, then downloads the bundle under bandwidth and I/O limits. Does not install or reboot. |
| `ark-update-apply.service` | root maintenance coordinator; explicit activation after verified staging | oneshot, no retry loop, exclusive update lock. Verifies the broker's drain and fence acknowledgement and empty session cgroups independently, then invokes RAUC. Only this unit may request installation or slot selection. |
| `rauc.service` | root D-Bus daemon, activated for the coordinator | Keeps the pinned RAUC package's lifecycle. D-Bus and Polkit policy denies install and slot operations to drivers, applications and the ordinary owner UI. Boot health gets a narrow grant for mark-good and mark-bad on the booted candidate only. |
| `ark-boot-health.service` | after core readiness; local deterministic tests | Marks the candidate slot good only after the required tests. On timeout or failure keeps admission closed, records diagnostics and requests a fallback reboot. Never depends on a model call. |
| `ark-admin.socket` / `.service` | root or operator only; independent of broker and model | Freeze, kill and recovery work with a hung broker. An authenticated local recovery TTY is also kept; SSH is off until explicitly provisioned. |

Service defaults: `NoNewPrivileges=yes`, `ProtectSystem=strict`, `ProtectHome=yes`, no capabilities, restricted writable paths. Broker, egress and checker use `PrivateDevices=yes`. Application and session units need an explicit minimal `/dev` plus `DevicePolicy=closed` and the exact assigned evdev, render and NVIDIA nodes; `DeviceAllow=` alone does not make a node visible through `PrivateDevices=yes`. Browser sandbox compatibility must pass; never fix startup by disabling the browser sandbox globally. Use only directives available in the pinned Debian systemd.

Within one slot, services deliberately join the same prepared namespaces; unrelated `PrivateTmp=yes` per service would break X socket sharing. A private network namespace also separates abstract X sockets and loopback-only CDP. Applications get egress through a per-slot veth/NAT policy that is default-deny with the profile's allowlist. Presentation gets authenticated Xpra sockets, not raw X sockets.

For a 32 GiB reference host (initial limits to tune, not measured working sets): two active slots; `ark-agents.slice` at `MemoryHigh=20G`, `MemoryMax=24G`, lower CPU and I/O weight than presentation and core services; a standard slot at `MemoryHigh=4G`, `MemoryMax=6G`, `TasksMax=2048`, covering driver, applications and worker. Larger Blender profiles need separate admission. Core daemons stop restarting after five failures in 60 s and stay visibly failed. Session shutdown uses `KillMode=control-group` and a 15 s cleanup timeout, then forced termination with an explicit loss receipt.

After a broker restart, surviving applications can stay visible, but no mutation resumes before reconciliation, epoch renewal and a fresh observation. Any slot sessiond cannot account for is quarantined and stopped. A presentation restart never cascades into killing applications. A bounded broker drain runs before shutdown; sudden power loss still leaves accepted or unknown actions for reconciliation.

#### Owning the stack: input policy and remaining limits

Device policy is §3.7 and §5.3, installed in the image before the helper can create anything. [P5-S4, P5-S5.] Agent Xorg configuration disables `AutoAddDevices`, `AutoEnableDevices` and `AutoAddGPU`, declares only the assigned keyboard and pointer as explicit evdev inputs with an exclusive grab, and uses dummy video, through the pinned non-setuid launch path with no TCP listener and qualified no-VT-switch behaviour. A profile that needs broad root or input-group access, or steals a VT, fails release qualification. Each new generation proves its devices reach its test surface; the installed image passes repeated concurrent host and sibling non-delivery tests, including physical USB hotplug and virtual-device destroy and recreate while the human types.

| Existing host problem | What Phase 5 changes | What remains |
|---|---|---|
| XFCE steals F2 | Agent input goes straight to its private server; the shipped agent WM has no F2 binding; the presentation WM omits conflicting function-key and Alt bindings; takeover has explicit "send key" controls | An application can still grab keys inside its own session. X11 emergency shortcuts are still grabbable. |
| Session/host contention | The image owns boot ordering, udev and Xorg exclusions, UID, runtime, bus and profile allocation, and resource admission | Applications within a session share focus by design; files, accounts and remote desktops need task ownership. |
| Single-driver global state | One Codex/API driver and one ordered queue per slot, bound to a session lease | API rate limits and model latency still bound throughput; independent input does not guarantee a 2× task speedup. |
| Fragile host configuration | A signed image fixes the kernel/Xorg/Xpra/driver tuple and tests it together | OS ownership moves compatibility and patching responsibility to us. |

Removing shortcut bindings improves ordinary use. It does not make an owned X11 desktop into the compositor-level preemption guarantee discussed in Phase 4. No custom human compositor is a Phase 5 dependency.

#### Packaging, installation and first boot

Build versioned `.deb` packages for broker and sessiond, worker and providers, input policy and helper, console, driver adapter, egress proxy and OS configuration. Assemble them with Debian base packages in CI using a pinned rootfs builder, then generate a Debian live-build USB hybrid ISO containing the installer, recovery tools and the exact root image. Publish the ISO, signed checksums, package and SBOM manifest, source revisions, licence notices and the matching signed RAUC bundle. No CAD software or licence is bundled. [P5-S6.]

Default GPT layout: 1 GiB FAT ESP; 24 GiB ext4 ROOT-A; 24 GiB ext4 ROOT-B; the rest ext4 STATE. Each root slot carries its own `/boot` kernel and initramfs and matching modules and graphics userspace; GRUB reads them from the selected root. The ESP holds the common GRUB loader, config and boot-state environment. The installer assigns unique GPT PARTUUIDs and writes a persistent slot map used by RAUC and the initramfs; GRUB passes `root=PARTUUID=…` and `rauc.slot=A` or `B`. Cloned labels or UUIDs must not select the wrong slot. A/B reserves ~49 GiB; require a 128 GB SSD and recommend 256 GB or more.

Mount STATE early and bind only explicitly persistent paths. Shipped `/etc` defaults stay image-owned; the default `/var` is tmpfs initialised from image-owned rules before services start.

| State class | Location and lifetime | Update rule |
|---|---|---|
| ArkDrive jobs, audit journal, checkpoints, credentials, control database, release sequence | STATE-backed `/var/lib/arkdrive` | Persistent; N/N−1 schema and backup rules; separate evidence and work quotas so the journal cannot be starved |
| Owner home, assigned application profiles and workspaces | STATE-backed homes and work directories | Persistent; version or copy a profile when an older application could not reopen it. Never bind the owner home into an agent. |
| NetworkManager connections, machine and owner identity | Explicit STATE-backed config | Allowlisted and versioned; must work with both retained roots |
| Persistent system journal, crash diagnostics | STATE-backed, bounded retention | Logs are not an authority for boot decisions |
| dpkg inventory | Read-only, slot-owned, plus the release SBOM | Travels with root. Live apt/dpkg mutation is unsupported. |
| Other `/var` state | Volatile, recreated from the current image | A daemon needing durable state is added to the allowlist with a compatibility test. No blanket persistent `/var`. |

Service and slot users are predeclared in the image; the installer seeds persistent account files from that roster and creates the owner login. First boot validates the seed without resetting authentication. Machine ID, host keys if enabled, network settings and owner authentication persist; `/run`, display cookies, the lease key and control tokens do not. OS v1 supports one owner/admin account.

Install on an ArK2-like RTX 3060 12 GB / i5 / 32 GiB desktop:

1. Back up the target disk; download and verify the signed ISO; write it to an 8 GB+ USB stick. UEFI boot, Secure Boot disabled for this OS v1 profile, dedicated target SSD. The installer shows the exact drive and partitions to erase and requires confirmation; no dual-boot resizing.
2. Boot "hardware check" first: monitor, keyboard and mouse, Ethernet, storage, GPU, TPM presence. Choose the qualified NVIDIA profile; if acceleration is unavailable, show software/headless diagnostics rather than a GPU pass. Installation works offline.
3. The installer lays out A/B/STATE, installs the confirmed image in both slots, collects the owner account and password, generates the machine key, and initialises the boot policy. After owner login the console completes network, time, retention policy and provider authentication. No shared default password, baked API token or automatic billable call.
4. Run the local two-session input and Thunar tests and the backend and GPU diagnostics, then start a real Codex/API task explicitly. Show "GUI isolated / evdev qualified / GPU accelerated / model authorised / credentials TPM-sealed" as separate statuses from the one health document; one green must not imply another.

**NVIDIA policy.** Ship one tested kernel/driver/userspace tuple for the RTX 3060 first. Build the Debian-packaged NVIDIA kernel module in CI against the image kernel, include it in the slot and initramfs, and prohibit on-device DKMS rebuilds and `.run` installers. A module/userspace mismatch must fail health checks. Do not pin a known-vulnerable branch for compatibility. Review redistribution and firmware notices before publishing (A-NV). [P5-S7.]

**GPU profile.** Xorg-dummy renders in software; installing the NVIDIA driver does not change that. GPU applications run through the headless-EGL launch profile that Spike C measured at 8.5×, pinned and re-qualified on the image; record renderer, viewport latency, render time and copy/CPU overhead. Cycles on the GPU follows the S-C2 outcome. The profile exposes render access only, never physical input or the host home. If it fails on the image, ship the CPU/UI profile and do not advertise GPU Blender.

**VRAM admission.** On 12 GB, reserve 2 GiB for presentation and driver overhead and admit at most 10 GiB of applications, from measured per-profile peaks and live telemetry. One GPU-heavy render at a time and two ordinary sessions to start; qualify N=4 separately. Admission is not a hard per-session quota; allocations can spike; a GPU reset can affect the human desktop and every accelerated session. No local model holds a standing reservation.

#### Updates and rollback

**Atomic root-slot updates through RAUC; Debian packages are build inputs, not the live update mechanism.** OS and ArkDrive ship together in one signed release; an ArkDrive-only fix builds a new image. OS v1 has no hot-upgrade channel. That trades bandwidth and reboots for a testable rollback unit. RAUC provides installation and slot control; GRUB attempt handling, health policy and the installer are our integration work. [P5-S8, P5-S9.]

Signing and metadata (v2; summary in §5.6):

- The image keyring holds the offline root. Bundles are signed by the release key on a hardware token. Development-signed bundles verify only against the development keyring, which production images do not carry.
- `ark-update-check` first verifies channel metadata signed by 2 of 3 maintainer keys: build ID, hardware profile, release sequence, minimum updater/API/state versions, bundle hash, channel and expiry (14 days). Stale metadata raises "update channel stale" at the console rather than being silently accepted.
- The installed release sequence lives on STATE and in the boot environment. Lower sequences are refused; an explicit recovery rollback is an owner action at the console or recovery TTY and shows the old build and date.
- Private keys never touch public CI. Rotation and compromise recovery are drilled in 5.3.

State machine: AVAILABLE → VERIFIED → STAGED → DRAINING → REBOOT_PENDING → BOOT_TEST → CONFIRMED, or FAILED / ROLLED_BACK. The checker may download and verify while sessions run. Inactive-slot writing starts only in maintenance, after new-job admission is closed, sessions are drained and stopped, and all mutation gates are fenced.

Drain has two gates. DRAINING rejects new jobs and sessions but lets existing jobs finish bounded checkpoint and save actions under their current leases. FENCED revokes epochs through the control page and rejects every mutation. Drivers are told to finish at a checkpoint; saves go through supported GUI workflows; unsaved and unknown warnings are kept with receipts. The default drain timeout is 10 minutes, after which installation is deferred if work remains. Forced termination is an explicit owner/admin operation or an already authorised maintenance policy and records loss risk. Never silently kill an unsaved CAD or Blender task to meet a timer. GUI process state is not checkpointed or migrated across kernels; browser tabs can be reopened from a manifest.

After FENCED, the coordinator verifies closed gates and empty session cgroups, flushes audit and job state, backs up control databases and configuration, verifies state-schema compatibility, installs and verifies the inactive slot, and selects it for one trial boot. No reboot merely because a download finished. A critical remotely exploitable issue can trigger a declared maintenance requirement, network restriction or service stop under policy; it never justifies an invisible reboot mid-task.

GRUB consumes the candidate attempt before boot. `ark-boot-health.service` confirms the candidate only after mounts, broker, helper, greeter readiness and a local two-session routing test (2,000 tagged events) pass, with no dependency on owner login, model or network. The owner console is tested on login as a separate check. A failed userspace test requests a reboot; a kernel hang needs a hardware watchdog or a manual power cycle. On the next boot the unconfirmed candidate is skipped. RAUC's GRUB environment lives outside the replaceable slots. Test the actual motherboard and ESP; "RAUC installed" is not a rollback test.

Keep one known-good slot and a manual GRUB fallback entry. Rollback can reintroduce fixed vulnerabilities and must say so. Keep a rescue USB: the common ESP, firmware, the SSD and shared STATE are outside root-slot atomicity. Routine releases do not rewrite the bootloader; bootloader updates use a separate recovery-backed procedure and are not called atomic.

Persistent state is not rolled back with root. Control and audit schemas stay readable by N and N−1 through additive migrations. Before a profile-format upgrade, make a versioned copy or prevent the old application reopening it. A release with an unavoidable irreversible migration needs an explicit recovery procedure and cannot be an automatic update.

#### Hardware scope and support boundary

OS v1 supports: x86-64 desktop, UEFI/GPT, one SATA/NVMe SSD, one 1080p monitor, USB keyboard and mouse, Ethernet, at least 16 GiB RAM for a limited two-session CPU profile; 32 GiB is the reference. First accelerated support is one RTX 3060 12 GB configuration. Intel and AMD graphics are named profiles only after the same presentation, evdev and GPU tests. A KVM/virtio VM is a boot, update and CPU test target, not evidence for NVIDIA or physical-device routing. A TPM 2.0 is recommended and determines whether credentials are sealed (§5.4).

Out of scope: ARM, legacy BIOS, laptops and hybrid graphics, multi-GPU routing, GPU passthrough/vGPU, multi-monitor/HDR/VRR, Wi-Fi/Bluetooth provisioning guarantees, suspend/hibernate, audio/video conferencing and device forwarding, dual-boot resizing, RAID, full-disk encryption and Secure Boot, remote control gateway, arbitrary desktop environments, arbitrary root package installs, transparent live-session migration, hostile tenant isolation, bundled commercial CAD or licences. A display cable is optional only for a separately tested headless profile.

The installed root is appliance-managed. Applications enter through versioned, qualified profiles. Developer mode can make a machine mutable but labels it unsupported until restored. Browsers execute untrusted web content, so trusted jobs do not excuse missing browser or kernel patches.

#### Maintenance cost and minimum sustainable cadence

Owning a distribution means: monitoring Debian security advisories; rebuilding kernels, initramfs and NVIDIA modules; patching browsers, Xpra, Openbox, LightDM, broker, helper, OCR libraries and Codex adapters; retaining source and licence artifacts; operating signing and release infrastructure; testing installation, rollback and recovery; triaging hardware regressions and state compatibility. Plan the next base migration before trixie's full-support period ends in August 2028. [P5-S1.]

Smallest credible team: two named engineers with cross-trained signing and recovery access (M-OS primary for OS, release and security; M-RT for runtime, reviewing and covering releases). Budget roughly 0.5–1.0 engineer FTE ongoing after stabilisation, plus incident spikes and a separate base-upgrade project. That is a planning allowance, not a measured rate. Implementation lanes can build and test; they cannot hold signing keys or own a security response, which is why A-OWN is a dated action.

Cadence: automated build, dependency and advisory checks daily; human security triage every working day with an urgent-alert rota; a weekly signed candidate and maintenance window; stable promotion after at least 48 hours on the reference desktop. Critical-issue triage within 24 hours and a tested fix or documented mitigation within 72 hours when upstream and hardware allow; publish status if it slips. Run the short routing and rollback suite on every candidate, the N=1/2/4 mixed tests weekly and on every graphics or input change, and install, restore and power-loss drills monthly. Keep one physical RTX 3060 test rig and VM CI. If that capacity is not available, keep OS images experimental and maintain the installable ArkDrive path.

#### What Phase 5 forced back into Phases 0–4

v1 listed these as additions; v2 has placed each in a dated action:

| v1 addition | Where it lives in v2 |
|---|---|
| Full distro/kernel/Xorg/Xpra/GPU manifest | P0.1 |
| Debian rootless Xdummy/no-VT, static evdev routing, hotplug, image-level host exclusion | S-B2, S-DEB, 5.0 |
| GPU spike on the shipped kernel/driver tuple | Re-qualified on the image in 5.2 |
| Remove global DISPLAY, home and driver singletons; separate slot identities; F2 regression on stock XFCE and on an owned desktop | 1.2, 1.4, G1 bench |
| Machine-readable backend health and readiness | G2 |
| Broker/helper loss, reboot during dispatch, evdev recreation, namespace and NVIDIA-mismatch fixtures | 2.4 |
| Application vs presentation lifetime; drain and checkpoint states; N/N−1 compatibility; reopen manifests; resource profiles; build and boot IDs in audit; `system.info` | 3.3 |
| Phase 4 stays optional and is repackaged only after requalification on the image | Phase 4 |

## 7. Risks, unknowns and decisions

### 7.1 Risks

| Risk | Consequence | Status and first resolution | Owner, date |
|---|---|---|---|
| Xorg/evdev permissions, VT and logind behaviour | Routing needs distro-specific work | Spike B passed on the development host. Open on Debian 13: S-DEB, then 5.0 | O, Fri 2026-11-27 |
| Software-rendered or VRAM-starved Blender | Working UI, poor render performance | Spike C passed for headless EGL (8.5×). Cycles blocked on VRAM: S-C2 decides | O, Fri 2026-11-20 |
| Shared remote VNC desktop | False local-isolation success | Independent remote displays and a negative control in 2.4 | R, Fri 2027-01-08 |
| Accessibility outages, stale references | Blindness or wrong actions | Provider health, single `rev`, independent sensing; S-AT | O, Mon 2026-11-16 |
| Focus and grab races | Input reaches an unexpected target | Modal policy, shortcut allowlist, effect verification, uncertain receipts | R, G1 |
| Single-instance applications | A launch escapes its session | Private buses and profiles; launch tests in G1 | R, G1 |
| In-flight effects at takeover | Work completes after pause | Quiescing, epochs, drain and reconcile | R, Fri 2026-11-27 |
| Shared UID, GPU and kernel | Overstated security | Slot UIDs at 1.4; denial matrix at 3.2; §5.8 states the limits | O/R, Fri 2027-01-29 |
| Event storms, hidden-window throttling | Stale state or wasted resources | Bounded queues, adaptive coalescing, resync, source-side sensing | R, Fri 2026-12-18 |
| Dependency maintenance and licensing | Long-lived integration burden | Pinned versions, unpatched Xpra, A-NV review | M-OS, Fri 2027-03-12 |
| **New: multi-step actions widen the blast radius of a wrong target** | Several inputs land wrongly instead of one | Each step dispatches only after the previous step is verified; 8-step and 10 s caps; any focus or modal change aborts; the 2.1 stale-`base_rev` test | R, Fri 2026-12-18 |
| **New: a warm session is not pristine** | State leaks between jobs | Generations are single-use; warm sessions hold no credentials or applications; reuse test in 3.2 | R, Fri 2027-01-29 |
| **New: 500 ms permit closes gates under load** | Spurious `permit_expired` | Fails safe; false-close rate measured in Phase 2; reverts to 2 s if above 1 per 10,000 actions | Raj, G2 |
| **New: egress proxy is a single point for model access** | All drivers wait if it is down | Restart on failure; sessions unaffected; visible wait reason; kill test in 5.1 | O, Fri 2027-03-12 |
| **New: one machine for two lanes and all gates** | Bench noise, contention, image work blocking runtime work | Quiet gate windows (rule 5); A-RIG | ArK, Fri 2027-01-15 |
| **New: schedule assumes two full lanes** | Dates are estimates, not measured throughput | Dates set at the upper end of v1's estimates; half-capacity weeks counted; every gate has a CUT | ArK, each gate |
| **New: maintainers unnamed** | No one can hold keys or own security response | A-OWN; without it G5 cannot declare stable | ArK, Fri 2026-12-04 |

A window manager cannot infer the business meaning of a changed canvas. Independent input does not make concurrent edits to one CAD design, file or account safe; those need task-level ownership.

### 7.2 The three approved decisions (unchanged)

**Decisions taken (ArK, Nov 9, 2026): all three approved as recommended.**

1. **Execution domain and launch policy.** One session per agent or application group, with controlled launching and reopening.
2. **Trusted automation first; hostile-code containment later.** Trusted applications and restricted working mounts first; stronger UID and sandbox isolation before broader access. v2's security changes work inside this decision (§5.1).
3. **First-release compatibility bar.** The first release may prove parallelism on Xvfb/XTEST, with the evdev and GPU spikes run in parallel, and qualification required before claiming full workload compatibility. Spikes B and C have since passed at spike scale; D1 and G1 still run on Xvfb/XTEST as approved.

Phase 5 adds OS support, signing and release ownership as a fourth release commitment. It does not authorise hostile-code multitenancy or make a local executor a required component.

### 7.3 New decisions v2 asks of ArK

Apart from N1, each has a default that applies if no decision is recorded by the date.

| ID | Decision | Needed by | Default |
|---|---|---|---|
| N1 | Adopt v2: T0 = Mon 2026-11-09, the gate rules in §6.1 and the schedule in §6.3 | Fri 2026-11-13 | None. v1 governs until ArK signs; the default-on-silence rule is itself part of what N1 approves. Work due this week (1.1, P0.1, S-AT) is the same under both. |
| N2 | A-BUD: API/model spend cap for demos and benches | Fri 2026-11-13 | D1 runs scripted only and the model-driven demo moves to Fri 2026-11-27 |
| N3 | OS v1 is local-only; the remote TLS gateway moves after OS v1 | Fri 2026-12-04 | Local-only |
| N4 | The egress proxy is mandatory for every driver that passes S-CRED | Fri 2026-12-04 | Mandatory |
| A-OWN | Name M-OS and M-RT | Fri 2026-12-04 | Unnamed → Phase 5 images are experimental only |
| N5 | Single-backend rule (end of Phase 2) | Fri 2027-01-15 | Apply the rule as written |
| N6 | Phase 4 go/no-go | Fri 2027-02-12 | No-go for OS v1 |

### 7.4 First milestone

Observable and narrow, as in v1, now dated: **by Fri 2026-11-20 (D1)**, two agents independently create and rename folders while the human types elsewhere. **By Fri 2026-12-04 (G1)**, a modal breaks AT-SPI in one session and it still completes, takeover pauses only that session, and the 30/30 and 10,000-event statistics hold. Publish recordings, receipts, failures, timing breakdowns and token counts together.

## Amendment — driver model policy (ArK, Nov 9, 2026; unchanged)

ArK's direction: "Do not think in terms of qwen. Think in terms of codex or api."

- The agents that drive sessions are Codex drivers (`codex exec`, the shape that passed the Virtuoso bench) or API-model loops through ArkDrive's `CU_MODEL_PROVIDER` presets. The local Qwen executor is not a design dependency anywhere: not as a per-session worker brain, not as a builder, not in the token or latency budgets.
- The measured numbers already assume this (33,171 Virtuoso tokens and 22,102 Thunar tokens were Codex-driver runs). v2 leans further into it: with API-class drivers, the cost is model turns, so multi-step verified actions, small deltas and receipt discipline are where the speed is.
- v2 applies the policy in four places: one adapter for both driver types (§4), the model proxy and per-job tokens (§5.4), no VRAM reservation for any model (Phase 5), and the build arrangement below.
- Build execution: Raj specs, UCGS/Codex designs, Codex implements on ArK2 in lanes R and O, Raj verifies, ArK decides. Gwen lanes are not on the critical path.

## Phase 5 source notes

Primary documentation checked Nov 9, 2026 (carried from v1). Sources establish upstream mechanisms and package availability; architecture, staffing, timings and gate thresholds are proposals requiring measurement. v2's additions (control page, exclusive grab, service credentials, threshold-signed metadata, peer-credential identity) are design proposals that the spikes and gates above must qualify on the pinned versions.

- **P5-S1:** [Debian 13 release/support information](https://www.debian.org/releases/trixie/).
- **P5-S2:** [Debian trixie Xorg packages, including evdev/dummy](https://packages.debian.org/search?keywords=xserver-xorg&searchon=names&suite=trixie).
- **P5-S3:** [Xpra Xdummy documentation](https://xpra-org.github.io/xpra/Usage/Xdummy.html); [Xpra manual: desktop/seamless/shadow operation](https://xpra.org/manual).
- **P5-S4:** [Debian trixie xorg.conf manual: explicit input devices and automatic device/GPU controls](https://manpages.debian.org/trixie/xserver-xorg-core/xorg.conf.5.en.html).
- **P5-S5:** [Linux uinput documentation](https://kernel.org/doc/html/latest/input/uinput.html); [libevdev uinput device/sysfs identity API](https://www.freedesktop.org/software/libevdev/doc/latest/group__uinput.html).
- **P5-S6:** [Debian Live manual: live-build and hybrid ISO construction](https://live-team.pages.debian.net/live-manual/html/live-manual.en.html).
- **P5-S7:** [Debian NVIDIA driver package/dependencies](https://packages.debian.org/trixie/nvidia-driver).
- **P5-S8:** [RAUC integration: slots, stable device paths and GRUB boot-state integration](https://rauc.readthedocs.io/en/latest/integration.html).
- **P5-S9:** [RAUC successful/failed boot confirmation](https://rauc.readthedocs.io/en/latest/using.html).
- **P5-S10:** [systemd service readiness/watchdogs](https://www.freedesktop.org/software/systemd/man/systemd.service.html); [device/resource controls](https://www.freedesktop.org/software/systemd/man/systemd.resource-control.html); [execution namespaces and device visibility](https://www.freedesktop.org/software/systemd/man/systemd.exec.html). Implementation must use directives available in the pinned Debian systemd, not assume the latest online manual's entire feature set.

