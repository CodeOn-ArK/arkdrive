# ArkDrive session worker and act path — design (worker v0)

- Basis: `AGENTIC-WM-PLAN-V2.md` (V2), `SPEC.md` (ark-sessiond v0 as built), `wm-sessiond-v0-2026-11-09.md` ("the note"). Also read, because V2 and the note cite them: the three spike notes (`bench/wm-spike-{a,b,c}-2026-11-09.md`), `wm/xorg.conf.tmpl`, `wm/mkdevs.py`, `docs/DRIVERS.md`.
- Date: Nov 9, 2026. Host assumed: ArK2.
- Status: PROPOSED. Design only; no implementation code exists. Nothing in this document has been measured unless it carries an **M** tag.
- Component: the per-session worker (`ark-worker`), the act path through it, the input-helper interface, and the driver adapter (`ark-act`). It sits on top of sessions that `ark-sessiond` v0 starts.
- Build rule assumed (same as the note): Raj specs from this design, Codex implements on ArK2, Raj verifies with independent ground-truth checks. Schedule per ArK's Nov 9 directive: continuous work, gates judge done-ness only, so this document has an order and gates but no dates.

## 0. Decisions at a glance

1. **One `ark-worker` per session.** It attaches to a session that sessiond started, runs the session's window manager and the sensing providers, and is the only process that touches that session's X server, accessibility bus and input devices on a driver's behalf.
2. **One observation order on every profile: `state` → `tree` → `ocr` → `pixels`.** A per-window probe decides whether the accessibility tree is usable; profiles differ in capture mechanism and cost, not in order. The worker never calls a model: "vision" is the driver model looking at a frame the worker hands it.
3. **"Nothing changed" is one integer comparison.** One per-session `rev`; every facet remembers the `rev` at which it last changed. No capture, no tree walk, no hashing on the fast path.
4. **Actuation is physical-first.** XTEST on `xvfb`; evdev through the uinput helper on `xorg`, with session-local XTEST as its second rung. Clipboard paste and AT-SPI actions are later rungs. No rung leaves the session; exhausting the ladder is a stop, never a host fallback.
5. **Three evidence levels per step: written, server-acknowledged, effect.** Only an application-visible effect yields `verified`. A flush or a server acknowledgement never does.
6. **`window.act` carries up to 8 steps.** A step dispatches only if the previous step was verified and nothing the driver did not predict (focus or modal change) happened in between.
7. **The worker retries only when it can prove nothing reached the application, or the intent is idempotent.** Everything else stops the batch and returns the state the driver needs to decide.
8. **The evdev channel is enabled only behind an exclusive kernel grab.** Found during this design: an ungrabbed uinput keyboard is also attached to the kernel's SysRq and console keyboard handlers (§4.8). This adds one option to the Xorg template and one filter to the helper.
9. **One adapter, four tools.** `ark-act` is an MCP stdio server exposing `observe`, `act`, `wait`, `done`. Codex uses it natively; API-model loops speak the same protocol. The model never handles leases, revisions or action IDs.
10. **Build order:** core and XTEST act path first (D0-capable), then pixels, tree, adapter (D1-capable), then evdev, OCR, fallback ladder, and a measurement run.

## 1. Scope, evidence and conventions

### 1.1 In scope, out of scope

| In scope | Out of scope here (owner in V2) |
|---|---|
| Worker lifecycle on a sessiond session; WM; `rev` and state cache | Broker, leases with MAC, per-slot sockets (broker, action 1.1/1.4) |
| Observation channels and the "nothing changed" answer | Xpra tiles, console, host sentinel (1.3, 3.x) |
| XTEST and evdev actuation, fallback ladder | Non-root `ark-input` helper, device templates, release-scale routing (S-B2, 2.2) |
| Action API, predicates, receipts, act journal | Hash-chained journal, signed checkpoints (3.3) |
| Control-page gate as the worker and helper read it | Who writes the control page in production (broker); permit renewal |
| Driver adapter for Codex and API loops | Egress proxy and per-job tokens (S-CRED) |

Reserved but not built in worker v0, so the contract does not change later: route name `cdp` and observation channel `cdp` (Chromium page content), `window.subscribe`, `clipboard.transfer`, typed secrets by reference. Each returns `backend_unsupported`.

### 1.2 Evidence labels

Every number in this document carries one of these, in a Basis column or inline.

| Label | Meaning |
|---|---|
| **M:note** | Measured in `wm-sessiond-v0-2026-11-09.md` |
| **M:V2** | Measured, V2 §0.1 evidence ledger |
| **M:A / M:B / M:C** | Measured in the Spike A / B / C bench note |
| **T** | Target or start value chosen here; unmeasured |
| **T:V2** | Target or start value carried from V2 (§3.4, §3.8, §3.9, §3.12); unmeasured |
| **L** | Contract limit or constant: a rule, not a performance claim |
| **P** | Design-time check: observed once on ArK2 while writing this, on a throwaway `Xvfb :131` or by reading system state. A yes/no fact or a byte layout, never a budget. Where a build step depends on a P, its gate re-verifies it. |

Measured figures this design relies on:

| Figure | Value | Label |
|---|---|---|
| Model-driven cost per action | 9.35 s (Virtuoso), 6.19 s (Thunar) | M:V2 |
| AT-SPI snapshot | 130 ms | M:V2 |
| Semantic (AT-SPI) click | ~0.2 s each | M:V2 |
| OCR vs vision | 0.17 s vs 5.3 s on the tested crop | M:V2 |
| Region hash | 1.6 ms | M:V2 |
| xvfb session start | 610–624 ms total (display 50 ms, bus 2–3 ms, AT-SPI 519–567 ms) | M:note |
| xorg session start | 1,798–1,819 ms total (devices 1,145 ms including a 1 s udev settle, Xorg 127–147 ms, AT-SPI ~519 ms) | M:note |
| evdev pointer exactness with `Device Accel Profile -1` | click at (210,55) landed at (210,55); a (137,89) move measured (137,89) | M:B |
| evdev typing recipe that worked | 30 ms key-down, 30 ms key-up per character; pointer deltas chunked to ≤40 px with 10 ms gaps | M:B |
| GPU through headless EGL | 0.565 s vs 4.792 s on llvmpipe, 8.5× | M:C |

### 1.3 Design-time checks (P)

Done on a throwaway `Xvfb :131` (outside sessiond's 91–120 range), then removed. The host display `:0` was never opened.

| Check | Result |
|---|---|
| `Xvfb -fbdir <dir>` | Produces `<dir>/Xvfb_screen0`, an XWD file that can be memory-mapped: 8,297,632 bytes at 1920×1080×24 = a 3,232-byte header and colormap (`header_size` 160 + 256 colormap entries × 12) + 1920×1080×4 pixel bytes; 7,680 bytes per line; byte order B, G, R, X. A newly mapped window's pixels appeared in the mapping with no capture request. |
| Session X extensions | Stock Xvfb lists XTEST, DAMAGE, XFIXES, Composite, XInputExtension, MIT-SHM, RECORD. |
| XTEST is observable as raw input | With XI 2.2 negotiated, each XTEST fake key, button and motion produced XI2 raw events on a second connection, carrying the source device id of the `Virtual core XTEST keyboard` / `pointer` slave. Each arrives twice (slave copy and master copy). |
| python-xlib 0.33 | Has `xtest`, `damage`, `xfixes`, `composite`, `xinput`, `record`; no MIT-SHM. Raw-event payloads arrive unparsed (bytes). `DamageCreate` before `DamageQueryVersion` returns an X error, and python-xlib then crashes decoding it (`'BadRRCrtcError' object has no attribute 'sequence_number'`). Its default XI negotiation is 2.0; 2.2 must be requested explicitly. |
| Installed on ArK2 | pyatspi 2.58.1, Pillow 12.1.1, `libtesseract.so.5` (tesseract 5.5.0, `eng` data), codex-cli 0.163.0 with `codex mcp` and `-c` config overrides. **Not installed:** openbox, numpy. |
| Kernel input handlers | `kernel.sysrq = 176`. An existing uinput keyboard on the box lists `Handlers=sysrq rfkill kbd …`. The evdev driver man page documents `Option "GrabDevice"`. See §4.8. |
| sessiond code | Session ids restart at `s1` when the registry is empty, and `STATE/run/<sid>/` is created with `exist_ok`; stop does not remove it. A session HOME can therefore be inherited from an earlier session with the same id. |

### 1.4 Simplifications against V2 (worker v0)

(a) No broker: the worker's socket is the driver endpoint (V2 interfaces I1 and I2 collapsed), the worker issues `journal_seq` itself, and a lease is a random token bound to one connection, checked by peer UID only. (b) The worker attaches to a sessiond-started session instead of being the session's init; it parents only the WM and the provider children. (c) The control page is a file written by `ark-worker control`; there is no permit expiry. (d) The act journal is plain JSONL, one file per generation, no hash chain. (e) Profiles are bare names; `profile_hash` is `null`. (f) Pixels reach a driver as PNG files by path, not sealed shared-memory frame references. (g) The input helper is still the root `mkdevs.py` that sessiond launches. (h) No cgroups: "zero exec in the action path" is checked with `strace`. (i) Python implementation: the V2 §3.12 rows are measured and published in W9, and V2's own rule applies to any row missed (action 2.1). (j) The route table is keyed without application version.

### 1.5 Contract changes that need sign-off (Raj, then ArK)

1. **Physical-first actuation.** V2 §3.6 says AT-SPI "handles exposed controls". This design uses AT-SPI to *find and verify* (resolve a node to coordinates, read back the effect) and the session's physical channel to *act*; AT-SPI actions are an explicit operation and a fallback rung. Reasons: the D1 and G1 definitions require physical keyboard and pointer paths in both sessions; a semantic click measured ~0.2 s (M:V2) against a ≤3 ms dispatch target (T:V2); and a blocking AT-SPI action is a plausible way to reproduce the Thunar accessibility outage, which V2 says must degrade one provider and never stall the action path.
2. **New reason codes** beyond V2's enumerated set: `invalid_request`, `precondition_failed`, `target_not_found`, `focus_failed`, `input_unacknowledged`, `effect_not_observed`, `effect_contradicted`, `unexpected_state`, `channel_down`, `worker_fault`, and the step-only `not_reached`.
3. **Receipt additions:** batch `status` and `stopped_at`; per-step `server_ack`, `predicate_class`, `expect_source`; `effect: null` for a step that never ran.
4. **Predicates:** V2's example `"role":"rename"` becomes a `match` object; a step without `expect` gets a default expectation (§5.6); the unexpected-state stop rule (§6.4).
5. **`session.attach`**, a v0-only stand-in for the lease half of `session.create`.
6. **Three small changes to sessiond v0** (§2.3), one of which adds the exclusive grab V2 §3.7 item 4 already calls for.

## 2. Component shape

### 2.1 Processes and files

All new files live in `~/arkdrive/wm/`.

| Piece | What it is | Notes |
|---|---|---|
| `ark-worker` | Python 3 daemon plus CLI (`start`, `stop`, `status`, `control`) | python-xlib, Pillow, standard library. One single-threaded `selectors` loop; it blocks only in `select`. |
| `worker_atspi.py` | Provider child: all AT-SPI calls and event listeners | pyatspi (the binding the spikes used). Restartable. |
| `worker_ocr.py` | Provider child: OCR | `libtesseract.so.5` through `ctypes`, loaded once. Restartable. No `tesseract` command is ever run. |
| `openbox` + `openbox-rc.xml` | Session window manager, child of the worker | Pinned config: zero key bindings, click-to-focus, no focus-follows-mouse, one desktop, no animations. V2: the session WM binds no F2. |
| `mkdevs.py` (modified) | The existing per-session root uinput helper | Gains an event socket (§4.4). Still launched and stopped by sessiond. |
| `ark-act` | Driver adapter: MCP stdio server | Standard library only. One long-lived process per driver. |
| `ark-loop` | Minimal API-model loop that drives `ark-act` | Standard library only; uses the `CU_MODEL_PROVIDER` presets from `docs/DRIVERS.md`. |
| `fixtures/` | Acceptance fixtures (§8.2) | GTK3 Python applications, as in Spike B. |

Per-session paths (`SD` = `STATE/run/<sid>` from SPEC §1):

| Path | Content | Writer |
|---|---|---|
| `SD/worker.json` | `{pid, generation, socket, started_at}` | worker |
| `SD/control` | Control page, 64 bytes (§2.6) | `ark-worker start` (initialise) and `ark-worker control` only |
| `SD/fb/Xvfb_screen0` | xvfb framebuffer (XWD) | Xvfb |
| `SD/input.sock` | Helper event socket (xorg only) | helper |
| `SD/driver/act.sock` | Driver endpoint, mode 0600 | worker |
| `SD/driver/frames/<hash>.png` | Evidence frames, content-addressed | worker |
| `SD/logs/worker.log`, `openbox.log`, `atspi-provider.log`, `ocr-provider.log` | Logs | each process |
| `STATE/acts/<generation>.jsonl` | Act journal for one worker generation | that worker (single writer) |
| `STATE/route-table.json` | Persisted route flips (§4.6) | workers, under `flock`, atomic rename |

`SD/driver/` is a separate directory so that, when slot identities arrive (action 1.4), it is the only path mounted into the driver's namespace.

### 2.2 Prerequisites

`sudo apt-get install -y openbox` (P: not installed). Everything else needed is already on ArK2 (§1.3). The worker refuses to start if any of XTEST, DAMAGE, XFIXES or XInputExtension ≥ 2.2 is missing (`missing_extension:<name>`), or if the root visual is not depth 24 at 32 bits per pixel (`unsupported_visual`).

### 2.3 Required changes to sessiond v0

| # | Change | Why | If absent |
|---|---|---|---|
| S1 | Xvfb is started with one more argument: `-fbdir <SD>/fb` (directory created first) | Memory-mapped framebuffer (V2 §3.9; P) | Worker uses `getimage` capture on xvfb too; slower, still correct |
| S2 | `mkdevs.py` is started with `--event-socket <SD>/input.sock --owner-uid <uid> --control <SD>/control` | The evdev channel needs an acknowledged, gated write path (V2 interface I4) | evdev channel is `down`; xorg sessions act through XTEST only |
| S3 | `xorg.conf.tmpl`: add `Option "GrabDevice" "true"` to both `InputDevice` sections | Exclusive kernel grab (V2 §3.7 item 4; §4.8 here) | evdev channel is `down` with reason `no_exclusive_grab` |

Nothing else in sessiond changes. Its stop machinery already tears the worker down: the worker and all its children carry both marker variables, so the marker sweep (SPEC §8 step 3) finds them. Because the sweep reads `/proc/<pid>/environ`, which reflects the environment at `exec` time, `ark-worker start` must re-`exec` its daemon with the markers already in the environment; setting them after start is not enough.

### 2.4 Lifecycle and CLI

Each command prints exactly one JSON object on stdout, as sessiond does.

- `ark-worker start --session sN` → `{"session","generation","profile","socket","state":"ready","channels":{…},"timings":{"x_ms","wm_ms","providers_ms","selftest_ms","total_ms"}}`. On failure: `{"session","state":"failed","error"}`, exit 1, and everything the worker started is stopped.
- `ark-worker stop --session sN` → releases held keys, closes the socket, stops its children, journals `worker_stop`.
- `ark-worker status --session sN` → the health document (`system.info`, §5.9).
- `ark-worker control --session sN --state agent|paused|human [--clear-fence]` → writes the control page; prints `{"session","state","control_epoch"}`. This stands in for the broker's `control.set`.

Start sequence. Readiness is event-driven (a pipe from the daemon to the starting process); nothing sleeps.

1. Read the registry record under the `flock` (SPEC §1). Refuse if the X server pid is dead, if a live worker already exists for the session (`worker_already_running`), or if the display is outside 91–120 (L). The worker takes `DISPLAY`, the bus addresses, `HOME` and `XDG_RUNTIME_DIR` from the record only; it never reads them from its own environment and never opens `:0`.
2. Create a random 128-bit `generation`. Open `STATE/acts/<generation>.jsonl`. Initialise the control page if missing (state AGENT, epoch 1).
3. Re-`exec` as the daemon with the marker environment. Open two X connections: `ev` (events, queries, window-manager messages) and `inp` (XTEST only). Negotiate XI 2.2 explicitly; call `DamageQueryVersion` before `DamageCreate`; install an X error handler and treat any uncaught X-layer exception as `worker_fault` (all three from P).
4. If `SD/home/.config/gtk-3.0/settings.ini` and the `gtk-4.0` equivalent do not exist, write them with `gtk-cursor-blink=false` and `gtk-enable-animations=false`. A blinking caret would otherwise damage the screen continuously and defeat §3.4. Existing files are left alone.
5. Refuse if a window manager already owns the root (`wm_already_running`). Start openbox; ready when the root carries `_NET_SUPPORTING_WM_CHECK`.
6. Start both provider children; each reports ready on its socket. The AT-SPI child uses the `atspi_address` and `bus_address` from the registry (found by sessiond through `GetAddress`, the Spike A rule), never the X root property.
7. xorg only: connect to `SD/input.sock`; find the XI2 slave ids of the devices named `kbd` and `ptr` (M:B finding 4: the names are the config identifiers); ask the helper for a grab check (§4.4, §4.8).
8. Self-test (§4.9). A failed channel is marked `down`; the worker still starts if at least one input channel passes.
9. Subscribe (§2.5), take the initial window list and focus, hash every tile, set `rev = 1`, listen on `SD/driver/act.sock`, write `worker.json`, journal `worker_start`, report ready.

Applications are started after the worker is ready, by the job launcher, with the existing `ark-sessiond run`. The worker discovers their windows from X events. Drivers do not launch applications.

Startup targets on top of the measured sessiond figures: WM ready ≤200 ms (T:V2); worker connect and self-test ≤400 ms on xvfb (T:V2); evdev routing self-test ≤300 ms (T:V2).

Worker death retires the generation: a new `ark-worker start` creates a new one, applications keep running, and every handle, ref and lease from the old generation is rejected with `stale_generation`.

### 2.5 State model: one `rev`

As V2 §3.2. The worker stamps every ingested event batch (one per loop iteration per source), every control transition and every step completion with the next value of one per-session counter. Each facet stores the `rev` at which it last changed:

| Facet | Changes when | Source |
|---|---|---|
| `windows` | A toplevel or popup is mapped, unmapped or destroyed | Root `SubstructureNotify`, `_NET_CLIENT_LIST` |
| `geometry[w]` | Window moved or resized | `ConfigureNotify` |
| `focus` | Active window changed | `_NET_ACTIVE_WINDOW` property, `FocusIn`/`FocusOut` |
| `modal` | A modal dialog for the focused family appeared or went | `_NET_WM_STATE_MODAL`, or `WM_TRANSIENT_FOR` with dialog type |
| `tree[w]` | The accessibility tree text for window `w` changed (by hash) | AT-SPI events, then a re-snapshot |
| `pixels[tile]` | A tile's content hash changed | DAMAGE marks tiles dirty; hashing is lazy (§3.4) |
| `control` | Control page state or epoch changed | Control page |

Handles are opaque and generation-bound: `s1/g3fa9c1d2/w7` (session, first 8 hex digits of the generation, window number). Receipts carry the full generation. Accessibility refs are `n<int>`, assigned on first sight and keyed by (application bus name, object path); they are valid within one generation.

Override-redirect windows (menus, drop-downs, tooltips) are tracked as `popup` windows of their family, so an unexpected menu is observable.

Canonical coordinates are session desktop pixels at scale 1 (V2 §4). Named spaces a driver may use: `desktop`, `window:<handle>` (client-area relative), `frame:<id>` (pixels of a frame the worker issued; the worker stores each frame's scale and offset). Only the worker converts. A `frame:` or `window:` point is rejected with `stale_rev` if that window's `geometry` facet changed after the frame's or observation's `rev`. The screen size is read from the root window, never assumed: xvfb is 1920×1080 (SPEC §6), xorg is 1280×800 (`xorg.conf.tmpl`).

### 2.6 Control page (as read by the worker and the helper)

`SD/control`, 64 bytes, little-endian (L): magic `ARKC` (4), version u32 = 1, generation (16), `control_epoch` u64, `state` u32 (0 AGENT, 1 QUIESCING, 2 HUMAN, 3 PAUSED), `flags` u32 (bit 0: clear-fence request), `permit_deadline_ns` u64 (0 = no expiry in v0), `lease_hash` (16).

- The worker maps it read-only. The gate is open only if `state == AGENT`, the generation matches, and `control_epoch` equals the epoch read when the batch started.
- Write order makes a torn read safe: closing writes `state` first, then the epoch; opening writes the epoch first, then `state`. Each is one aligned store.
- The worker reads the page immediately before every input write (V2 §3.2) and between chunks of a long write (§4.3). The helper reads it before every frame it writes.
- An unreadable or malformed page is a closed gate.

## 3. Observation

### 3.1 Channels

| Channel | What it returns | Cost basis | Where it runs |
|---|---|---|---|
| `state` | Windows, titles, classes, geometry, focus, modal, popups, pointer position | Cached from X events; no round trip | Worker |
| `tree` | Accessibility tree of one window as compact text lines with refs | Snapshot 130 ms (M:V2), then cached until the tree changes | AT-SPI child |
| `ocr` | Text lines with boxes read from a window or region of the framebuffer | 0.17 s on the tested crop (M:V2), cached by content hash | OCR child |
| `pixels` | A PNG frame (window or region) for the driver model to look at | A model turn with an image; the old vision rung measured 5.3 s (M:V2) | Worker writes the file; the driver model is the vision |

### 3.2 Default order per profile

The order is the same everywhere; the table shows what actually differs.

| Profile | Order for `channels: ["auto"]` | Capture mechanism | Fresh-capture target | Before the tree probe finishes |
|---|---|---|---|---|
| `xvfb` | state → tree → ocr → pixels | Read-only memory map of `SD/fb/Xvfb_screen0`. The map *is* the shadow framebuffer; there is no capture step. | crop ≤10 ms, full frame ≤15 ms (T:V2) | Assume tree usable |
| `xorg` | state → tree → ocr → pixels | Worker-owned shadow buffer, refreshed with `GetImage` of the bounding box of dirty tiles (one full `GetImage` if more than half the screen is dirty) | ≤30 ms (T:V2, the S-CAP fallback row) | Assume tree usable |
| Either, application launched through the Spike C EGL profile | state → ocr → pixels; tree only if the probe says usable | Same as the session's backend. GPU output reaches the X framebuffer by the application's own readback, so capture does not change. | As the backend | Assume tree empty |

If S1 is absent, xvfb uses the xorg mechanism. MIT-SHM capture on xorg is left to spike S-CAP; python-xlib has no SHM (P).

### 3.3 When each channel is used

For `channels: ["auto"]` the worker stops at the first rung that produces text for the scope (V2 §3.9, cheapest first):

1. **Unchanged.** If nothing in scope changed since `since_rev`, return `unchanged: true` (§3.4). Nothing else runs.
2. **`state`.** Always included when a state facet changed. It costs nothing.
3. **`tree`,** for the focused window (or the scoped window), when that window's probe result is `usable` and the provider is healthy for its application. Returned as a delta when the delta fits the budget, otherwise as a full snapshot.
4. **`ocr`,** when the tree is `empty`, `degraded` or `down` for that window, or when the driver asks for it, or when a predicate is OCR-class.
5. **`pixels`,** only when the driver asks for it, or automatically when the window is *blind*: tree not usable and OCR finds fewer than 3 text lines (T). At most one frame per observation.

A window whose pixels changed while its usable tree did not (a canvas inside a toolkit window) is reported as `pixels_changed: {bbox, tiles}` without a frame; the driver decides whether to look.

**Tree probe.** On the first sight of a window the AT-SPI child takes one bounded snapshot. The result is `usable` if it contains at least one showing node with an interactive role (push button, toggle button, check box, radio button, menu item, text, entry, combo box, list item, table cell, tree item, icon, link, tab) (T, a heuristic); otherwise `empty`. The probe result is part of the route table key (§4.6) and is re-taken after a provider recovery.

### 3.4 Answering "nothing changed" cheaply

Tiles are 64×64 px (L): 510 on xvfb (30×17), 260 on xorg (20×13). DAMAGE on the root window (raw rectangles, coalesced per loop iteration; start value 8 ms, 16 ms above 500 events/s, T:V2) only marks tiles dirty and records `dirty_rev[tile]`. Hashing is lazy.

On `window.observe` with `since_rev = N`:

1. **Fast path.** If the generation matches and every facet in scope has `rev ≤ N` and no tile in scope has `dirty_rev > N`: return `{"unchanged": true, "rev": current}`. This is integer comparisons only. Target ≤1 ms in the worker, ≤3 ms end to end (T:V2).
2. **Damage-but-identical path.** If only tiles are dirty: refresh those tiles (xorg: `GetImage`; xvfb: nothing to do), hash them (BLAKE2b, 64-bit digest), and compare with the stored hashes. A tile whose hash is unchanged is cleaned without bumping its facet. If no tile hash changed, return `unchanged: true`. Target ≤10 ms (T). The measured basis for hashing is 1.6 ms per region (M:V2).
3. Otherwise continue with §3.3.

Rules that keep this honest:

- **Settle before reporting a `rev`.** Every response that reports `rev = N` (observation, receipt, wait) first re-hashes every dirty tile on the whole screen, so `pixels[tile]` is exact as of N and a later comparison against N is exact.
- **A false "unchanged" is the dangerous error**, so a watchdog re-reads the window list and focus and re-hashes all tiles every 5 s at low priority (T:V2). Any disagreement with the cache bumps `rev`, is counted as `resync_drift` in the health document, and is published in W9.
- `since_rev` from another generation returns a full observation with `full: true`, never `unchanged`.
- Animated content still defeats the fast path. The worker removes the commonest cause (caret blink, §2.4 step 4); a driver can pass `ignore_regions` (up to 4 rectangles, L) for anything else.

### 3.5 Tree text

Format `lines/1`, one node per line, one leading space per depth level:

`n12 push_button "Create Folder" [focused] @412,300,96,28`

Fields: ref; AT-SPI role name; accessible name in quotes; the true states among `focused, selected, checked, expanded, editable, disabled`; for text and entry roles `text="…"` truncated to 80 characters (L); extents in desktop pixels. A node is included if it is showing and visible and has a name, text, an interactive role, or an included descendant. Unnamed containers with one included child are flattened. Name matching is case-sensitive exact (`name`) or regular expression (`name_re`); Spike A found Thunar's application name is lowercase `thunar` (M:A).

Budgets: full snapshot ≤200 nodes or 16 KiB, with a continuation `cursor`; delta ≤40 nodes or 2 KiB (T:V2). A delta is `{added: [lines], changed: [lines], removed: [refs]}`; if it exceeds the delta budget the response is a full snapshot with `full: true`. At most one snapshot is taken per window per tree change; later reads come from the cache (V2 §3.9).

An AT-SPI application is matched to its X window by process id (`_NET_WM_PID`), then by the frame whose extents overlap the window most.

### 3.6 OCR

- Input comes from the shadow framebuffer at source resolution. Preprocessing start values (T): convert to greyscale, scale ×2, page segmentation "sparse text"; "single line" for regions at most 48 px high.
- Output is lines: `{"text", "box": [x,y,w,h], "conf"}` in desktop pixels; words below confidence 60 (T) are dropped. The measured "byte-exact" result belongs to the tested crop only (M:V2); OCR text is evidence, not truth.
- **Cache:** results are keyed by the content hash of the input pixels and the parameters; 256 entries, least recently used (T). An unchanged region costs nothing.
- **Incremental re-read:** when part of a window is damaged, the worker re-reads only a horizontal slab: the damaged bounding box extended to the window's full width and 24 px up and down (T). Cached lines inside the slab are replaced; lines touching the slab's top or bottom edge are discarded in favour of the cached line when that line lies outside the damage. Gate B6 checks that incremental results equal a full re-read.
- OCR is CPU-only. No observation path uses the GPU or holds VRAM.

### 3.7 Frames (the `pixels` channel)

A frame is a PNG of a window or region, long side ≤1,024 px by default (T:V2); `scale: 1` is allowed for regions that already fit. Files are content-addressed in `SD/driver/frames/`, with a 200 MB quota per session (T), oldest first, never deleting a frame referenced by the last 50 receipts (T). The response carries `{frame, path, rev, rect, scale, space}`. A driver that wants to click what it saw passes the point in that frame's space; it never does the scaling itself (the "quarter-offset" bug `docs/DRIVERS.md` warns about).

On a step that ends `not_observed`, `contradicted` or `unknown`, the receipt references one frame of the target window, so the driver does not need a follow-up observation to look.

### 3.8 Provider health

| Provider | Breaker | Degraded when | Recovery |
|---|---|---|---|
| AT-SPI, per application | 250 ms per call (T:V2) | Two consecutive breaches, or one call over 1 s (T:V2) | Child is killed and restarted; probes at 1, 2, 4 … 30 s (T:V2) |
| OCR | 1,000 ms per region (T) | A breach or a crash; restarted once at once, `down` on a second failure within 60 s (T) | Same backoff |
| Capture | — | `GetImage` error or a truncated map | Worker fault (§7.4) |

A degraded provider degrades one channel for one application. The response always carries `providers`, for example `{"atspi": {"state": "degraded", "apps": {"thunar": "application_unreachable"}}, "fallback_available": ["ocr", "pixels", "keyboard"]}`, which is V2's `provider.health` payload.

## 4. Actuation

### 4.1 Input channels per profile

| Route | Mechanism | `xvfb` | `xorg` |
|---|---|---|---|
| `xtest` | XTEST fake input on the worker's `inp` connection, session-local | **Primary** for keyboard and pointer | Second rung |
| `evdev` | Worker → helper socket → per-session uinput device → evdev node → session Xorg | `backend_unsupported` (Xvfb does not consume evdev; V2 §3.7) | **Primary** for keyboard and pointer, behind the exclusive grab |
| `paste` | Worker owns the CLIPBOARD selection, then sends `ctrl+v` on the profile's primary input route; a real selection transfer inside the session | Text rung | Text rung |
| `atspi` | `Action.doAction`, `EditableText.setTextContents` or `insertText`, in the AT-SPI child | Fallback rung; explicit `invoke` | Same |
| `wm` | EWMH client messages to openbox: `_NET_ACTIVE_WINDOW`, `_NET_CLOSE_WINDOW` | `focus_window`, `close_window` | Same |
| `cdp` | Reserved | `backend_unsupported` | `backend_unsupported` |

There is no route outside the session. Host `ydotool`, host XTEST and the host display are not rungs under any failure (V2 "never" rules).

### 4.2 Dispatch sequence for one batch

Follows V2 §3.5.

1. **Validate**: schema, ≤8 steps, deadline, lease, generation, action-ID dedupe. A failure returns a `rejected` receipt; nothing is dispatched.
2. **Journal the intent** and `fsync`. No intent, no dispatch. A journal write failure fences the worker (§7.4).
3. Read the pointer position and modifier mask once (one `QueryPointer`). If any modifier is down, send releases for every tracked key and re-check once; still dirty → `precondition_failed` (`detail: modifier_state_dirty`).
4. **Per step:**
   1. Batch deadline remaining; control gate open (§2.6), else `preempted`.
   2. Preconditions (§5.5), by integer comparison of facet revisions.
   3. Resolve the target window (an active modal becomes the effective target only where the preconditions allow it) and the `at` target to a desktop point (§5.4).
   4. Choose the route from the route table (§4.6).
   5. Keyboard-class intents: the target must be the active window according to cached focus events. Focus is not re-queried when no focus or map event has arrived since it was last confirmed. If it is wrong, send `_NET_ACTIVE_WINDOW` and wait for the focus event: ≤200 ms per attempt (T), 2 attempts (L), then `focus_failed` with nothing dispatched.
   6. Pointer-class intents: move, then one `QueryPointer`. The position must equal the target point and the window under the pointer must belong to the target (its frame or one of its popups). If it is occluded, raise the target and re-check once; then `precondition_failed` (`detail: occluded`). Only motion has been sent at that point.
   7. Read the control page again, write the input, flush once. Record `rev.dispatched`.
   8. Wait for the server acknowledgement (§4.5).
   9. Evaluate the step's expectation on the event stream (§6). Record `rev.verified`.
   10. Continue only if the effect is `verified` and the unexpected-state rule (§6.4) allows it.
5. Send releases for anything still tracked as pressed.
6. **Settle**: wait until no `rev`-bearing event has arrived for 40 ms (T), capped at 100 ms (T:V2), then compose `state_delta` (`settled: false` if the cap was hit).
7. Journal the receipt, `fsync`, reply. Target: receipt durable ≤10 ms after the outcome is known (T:V2).

Nothing in this path runs a command (V2: zero fork/exec per action). Local targets, all T:V2: request received → input written ≤3 ms with focus already confirmed, ≤10 ms when focus must be set; evdev helper write → event at a session client ≤5 ms; local overhead per event-verified action ≤50 ms.

### 4.3 Mechanics per intent

**Keys.** A chord is `modifier+…+key` in the syntax the existing `cua key` uses (`ctrl+l`, `Return`, `alt+Tab`); modifiers are `ctrl`, `shift`, `alt`, `super`; the key is an X keysym name. The worker resolves keysyms against the session server's own keyboard mapping. On `evdev` the Linux key code is the X keycode minus 8, taken from that same mapping, so both routes agree and there is no second key table. A keysym with no keycode in the mapping → `invalid_request`.

**Text.** A character is typeable if the server mapping has it at shift level 0 or 1. If every character is typeable and the text is at most 200 characters (T), the route is the profile's primary key route; otherwise the first rung is `paste`. `replace: true` sends `ctrl+a` first and types over the selection. Text containing a newline or tab is `invalid_request` unless `allow_control: true`; use a `key` step for `Return` and `Tab`. Maximum 2,000 characters per step (L).

| Pacing | `xtest` | `evdev` |
|---|---|---|
| Keys and text | One burst, one flush, in chunks of ≤32 characters (L) with a control-page read between chunks. Per-profile `inter_key_delay_us`, default 0 (T:V2). Target: 100 characters ≤50 ms (T:V2). | One evdev frame per key edge; ≤64 frames per helper request (L); 2 ms between frames (T). The helper checks the gate before every frame. |
| If keys are lost | Raise `inter_key_delay_us` for that profile | Double the gap until gate C4 passes, up to the spike-proven 30 ms per edge (M:B) |
| Pointer move | One absolute fake motion | Relative device: delta from the `QueryPointer` position, in frames of ≤40 px per axis (M:B recipe), 1 ms apart (T; falls back to the spike's 10 ms, M:B). One corrective delta if the position is off; a second miss is `channel_down`. Exact only with `Device Accel Profile -1` (M:B), which sessiond sets. |
| Click | 10 ms after the move (T), press, 15 ms hold (T), release; 60 ms between clicks of a multi-click (T) | Same timings |
| Scroll | Buttons 4/5/6/7, one press and release per notch | `REL_WHEEL` / `REL_HWHEEL`, one frame per notch |
| Drag | Press at `from`, at least 8 intermediate motions (L), release at `to` | Same |

**Paste.** The worker takes CLIPBOARD ownership on its own window, serves `UTF8_STRING`, `STRING` and `TARGETS`, then sends `ctrl+v` on the primary input route. The `SelectionRequest` from a window of the target client is recorded as evidence that the application asked for the data. The previous clipboard content is not restored; the receipt says `clipboard_overwritten: true`.

**Pressed-state tracking.** The worker tracks every key and button it has pressed per route. The helper keeps its own set and releases everything it holds if its connection drops or on `release_all`, which is accepted in any control state (V2 §3.8: release tracked keys on takeover; target ≤30 ms, T:V2).

### 4.4 Helper event socket (V2 interface I4, v0 form)

`SD/input.sock`, stream socket, owned by the session user, mode 0600; one connection at a time; peer UID must equal `--owner-uid`. Messages are a 4-byte big-endian length and a JSON body.

- Request: `{"seq", "generation", "dev": "kbd" or "ptr", "frames": [[[type, code, value], …], …], "gap_us"}`. Each inner list is one evdev frame; the helper appends the `SYN_REPORT`. `gap_us` is 0–50,000 (L).
- Limits (L): ≤64 frames per request, ≤4 events per frame.
- Validation; anything else is refused whole with `"error": "rejected"` and nothing written. `kbd`: `EV_KEY`, codes 1–248 **except 99 (`KEY_SYSRQ`)**, values 0 or 1. `ptr`: `EV_KEY` codes 0x110–0x112 with values 0 or 1; `EV_REL` codes 0, 1, 6, 8 with |value| ≤ 40.
- Before every frame the helper reads the control page; a closed gate stops the request there.
- Reply, sent after the last write returns: `{"seq", "ok", "frames_written", "error": null or "gate_closed" or "rejected" or "io_error"}`.
- `{"seq", "op": "release_all"}` writes a key-up for everything the helper holds down.
- `{"seq", "op": "grab_check"}` → `{"kbd": "busy" or "free", "ptr": "busy" or "free"}`. The helper opens each of its two event nodes and attempts an exclusive grab of its own; "busy" means another holder (the session Xorg) already has it. If the attempt succeeds the helper releases it at once and reports "free".

The helper never hands out a uinput descriptor (V2 §5.3). The FIFO stays as sessiond's `quit` path, unchanged.

### 4.5 Server acknowledgement

The worker selects XI2 raw key, button and motion events on the root window of its `ev` connection. Raw events are delivered regardless of focus and grabs, and carry the source device. For each input it writes, the worker expects the matching raw events, in order, whose device id and source id both equal the slave device of the route it used: the XTEST keyboard or pointer for `xtest` (P), the `kbd` or `ptr` slave for `evdev` (expected; verified at gate C4).

| Outcome | Meaning | `dispatch_status` |
|---|---|---|
| All expected raw events seen within 50 ms (`xtest`, T) or 150 ms plus pacing (`evdev`, T) | The session's own X server consumed the input from the intended device | `accepted` |
| Fewer seen | Input was written but the server did not (yet) consume it | `unknown` |
| An error before any write | Nothing was sent | `not_dispatched` |

This is the per-action form of Spike B's finding 2: a device can report `Device Enabled = 1` while the server drops its events, so routing is proved by an event, not a flag. It proves routing only. It says nothing about the application, and it never produces `verified`.

### 4.6 Route table and the fallback ladder

The route table is keyed by (profile, `WM_CLASS`, toolkit, window type, tree probe result, input class), where input class is `keyboard` or `pointer`. Each entry holds the ordered routes and, per route, a state: `ok`, `suspect`, `rejected`, `down`. Flips to `rejected` are persisted in `STATE/route-table.json` so a new session starts on the right rung (V2 §3.6). Target: the first route tried succeeds on ≥95% of steps after warm-up (T:V2).

| Profile | Intent | Rung 1 | Rung 2 | Rung 3 | Rung 4 | Then |
|---|---|---|---|---|---|---|
| `xvfb` | key | `xtest` | — | — | — | stop: `backend_unsupported` |
| `xvfb` | text | `xtest` keys (or `paste` first, §4.3) | `paste` | `atspi` set-text, if the focused node is editable | — | stop |
| `xvfb` | click, scroll | `xtest` | `atspi` action, if the target is a ref with an action (click only) | — | — | stop |
| `xvfb` | move, drag | `xtest` | — | — | — | stop |
| `xorg` | key | `evdev` | `xtest` | — | — | stop |
| `xorg` | text | `evdev` keys (or `paste` first) | `xtest` keys | `paste` | `atspi` set-text | stop |
| `xorg` | click, scroll | `evdev` | `xtest` | `atspi` action (click only) | — | stop |
| `xorg` | move, drag | `evdev` | `xtest` | — | — | stop |
| either | invoke | `atspi` | — | — | — | stop: `provider_degraded` |

When the ladder may advance:

1. **Within a step, automatically,** only when the previous rung provably delivered nothing (`not_dispatched`), or the intent is idempotent (§7.2). At most 3 attempts per step (L).
2. **Between steps, by learning,** when a route is marked `rejected` or `down` for that table entry (§4.7).
3. **Never** after `dispatch_status: unknown`, and never to "try the same thing harder" after input that was acknowledged but showed no effect. V2: reconcile before retrying; never replay an uncertain action.

### 4.7 When a channel fails, and when an application rejects synthetic input

| What happened | How the worker knows | Step outcome | Route consequence |
|---|---|---|---|
| X error on the fake-input request, or the `inp` connection write fails | X error or exception before any event | `not_dispatched` | Next rung in the same step |
| Helper refuses or is unreachable before any frame | `frames_written: 0`, or socket error | `not_dispatched` | Reconnect once (L); then `evdev` is `down` for the generation; next rung |
| Helper wrote some frames, then failed | `frames_written < sent` | `accepted`, reason `channel_down`, `written` reported exactly | `release_all`; `evdev` `down`; no retry |
| Written, acknowledgement incomplete | Raw-event count short at the timeout | `unknown`, reason `input_unacknowledged` | Route `suspect`; channel probe: a 1 px pointer move and back (L), expecting its raw events. Probe fails → `down` |
| Acknowledged, focus correct, no effect | Expectation `not_observed` | `accepted` / `not_observed` | Miss counter for this entry and route |
| AT-SPI call exceeds its breaker | Timeout | `unknown` if the call was sent, `not_dispatched` if the provider was already degraded | Provider degraded for that application (§3.8) |

**Rejection of synthetic input** cannot be seen directly; the worker infers it. A route is marked `rejected` for a table entry after 2 consecutive steps (T) on that entry where all of these hold: the route was `xtest`; the acknowledgement was complete; focus was on the target at dispatch and at the deadline; the effect was `not_observed`; and no facet of the target window family changed between dispatch and deadline (no reaction at all). Any verified `xtest` step of the same input class on the same entry resets the count. The second receipt carries `route_change: {from, to, reason: "synthetic_input_rejected_suspect"}`.

After the flip:

- on `xorg` it does not matter: `evdev` is the primary route there;
- on `xvfb`, later steps of that input class use the next rung if the intent has one (`paste`, `atspi`); if not, they return at once, undispatched, with `backend_unsupported`, `detail: "xtest_rejected"`, `escalate: "relaunch_on_xorg"`. Moving an application to an xorg session is a launcher or human decision; the worker does not do it.

If `evdev` goes `down` on xorg, later steps use `xtest` unless the entry is already `xtest`-rejected, in which case the result is `backend_unsupported` with `escalate: "restart_session"`. Devices are recreated only in a new generation (V2 Phase 5 service policy).

### 4.8 The evdev channel requires an exclusive grab

Finding (P, by reading system state; the kernel behaviour below is from the documented input subsystem and is not yet demonstrated by a test on ArK2):

- A uinput keyboard is attached to the kernel's own input handlers as well as its event node. An existing uinput keyboard on ArK2 lists `Handlers=sysrq rfkill kbd …`.
- `kernel.sysrq` is 176 on ArK2, which permits sync, read-only remount and reboot. `Alt+SysRq+<key>` typed on *any* attached keyboard reaches the kernel. A driver that sent that chord through the evdev route could reboot the host.
- The console keyboard handler would deliver session keystrokes to a text virtual terminal if one were in the foreground. Spike B's host sentinel watched the host's physical devices and the host X server; it could not have seen either path.
- `xorg.conf.tmpl` as built has no `GrabDevice` option. The evdev driver's man page states that the grab stops the device's events reaching other kernel consumers. V2 §3.7 item 4 already requires the grab "if it is not already part of the Spike B configuration".

Decisions:

1. Change S3 (§2.3) adds `Option "GrabDevice" "true"` to both input sections.
2. At start, and again before the first `evdev` write of every batch, the worker requires proof the grab is held: the helper's `grab_check` must report "busy" for both nodes. Anything else marks `evdev` `down` with reason `no_exclusive_grab`. Fail closed.
3. Independently, the helper refuses key code 99 (§4.4), and the worker rejects any chord containing `Sys_Req` or `Print` on `evdev` with `invalid_request`. On xorg those keys can still be sent through the `xtest` rung, which never touches the kernel input layer.
4. Gate C4 checks this with ground truth before any model-driven driver uses `evdev`. The ground truth needs no change to the host: under an exclusive grab the input core delivers a device's events to the grabbing handle only, so a second, non-grabbing reader on the session node must see **zero** events while the fixture inside the session logs all of them. That one observation covers every other consumer of the device, kernel handlers included. If the grab breaks routing, that is a STOP under V2 rule 7 and xorg sessions run on `xtest` until it is resolved. Isolation is not traded for a rung.
5. Consequence for the harness: Spike B's node watcher (its `host_to_session_no_unsolicited_events` check) reads the session nodes without a grab and will see nothing once S3 is in. The fixture log inside the session replaces it as the delivery record.

Also noted, not decided here: sessiond leaves the session event nodes at mode 0666 (a Spike B harness artefact). With the helper socket, nothing in production needs it; S-B2 should remove it.

### 4.9 Channel self-test at start

V2 §3.7 item 6: agents are enabled only after the assigned devices are shown to reach the session's test surface.

- Runs in full only when the session has no client windows. The worker maps a small override-redirect test window, focuses it, and sends 200 events (L; V2's count): 50 `Shift_L` press and release pairs and 100 pointer motions, through the profile's primary route, and through `xtest` as well on xorg.
- Pass: every raw event seen with the expected source device; the test window received exactly 50 key presses; the pointer ends where it started.
- If client windows already exist (a worker attached late), only the pointer part runs, and the health document reports `keyboard: "untested"` until the first verified keyboard step.
- The self-test proves delivery inside the session. Non-delivery to the host and to siblings is an acceptance check (§8.3 C5), because the worker is not allowed to open the host display.

### 4.10 GPU profile

Spike C: the session's X server never supplies GPU rendering; the GPU arrives through a headless EGL context and CUDA, independent of the display backend (M:C). For the worker this means: applications launched with the EGL profile are observed and driven exactly like any other window; the worker does not open, mask or depend on any `/dev/dri` node; the per-session `/dev/dri` mask applies only inside the session Xorg's private mount namespace (`ark-xorg-launch.sh`), so it does not close the side channel for applications; and render results are verified by predicates on the UI or by an external oracle, not by the worker inspecting the GPU.

## 5. Action API

### 5.1 Transport and identity

JSON-RPC 2.0 over the Unix stream socket `SD/driver/act.sock`; each message is a 4-byte big-endian length and a UTF-8 JSON body (V2 §4). Every call carries `deadline_ms`; the worker never holds a caller past it. The worker accepts a connection only if the peer UID equals its own. Method names and shapes are V2's, so the broker can later take over this socket's role without the adapter changing.

### 5.2 Methods

| Method | Purpose | Lease needed |
|---|---|---|
| `session.attach` | Get the lease, generation, current `rev`, capabilities | — |
| `window.observe` | State or delta as of a `rev` | yes |
| `window.act` | Up to 8 verified steps | yes |
| `window.wait` | Block until a predicate holds or the deadline passes | yes |
| `action.get` | Durable outcome of an action ID | no |
| `session.done` | Driver declares the task finished; releases the lease | yes |
| `system.info` | Health document | no |

Limits (L, from V2): one action executes per session at a time; at most 8 wait (`queue_full`); action IDs are deduplicated within a generation, and a repeated ID returns the stored receipt without dispatching again.

### 5.3 `session.attach` and `window.observe`

`session.attach` params: `{"client": "codex" or "api", "name", "budget": {"acts": int} (optional)}`. Result: `{"lease", "session", "generation", "profile", "rev", "screen": {"w","h"}, "channels": {…}, "limits": {"steps": 8, "deadline_ms": 10000}}`. One lease exists at a time (`lease_held` otherwise). It is bound to the connection, lapses 5 s after the connection drops (T:V2), and ends at `session.done`.

`window.observe` params:

| Param | Values | Default |
|---|---|---|
| `scope` | `{"kind": "session"}`, `{"kind": "window", "window": h}`, `{"kind": "family", "window": h}` | session |
| `since_rev` | integer or null | null (full) |
| `channels` | `["auto"]` or any of `state`, `tree`, `ocr`, `pixels` | `["auto"]` |
| `region` | `{"space", "x", "y", "w", "h"}`; restricts `ocr` and `pixels` | whole scope |
| `ignore_regions` | up to 4 rectangles (L) | none |
| `budget` | `{"nodes", "bytes"}` | 200 nodes, 16 KiB (T:V2) |
| `cursor` | continuation token | null |
| `deadline_ms` | 50–10,000 | 2,000 (T) |

Result: `{"rev", "generation", "unchanged", "full", "state": {"windows": […], "removed": […], "focus", "modal", "pointer"}, "tree": {"window", "format": "lines/1", "lines" or "delta", "truncated", "cursor"}, "ocr": {"window", "lines": […]}, "frames": […], "pixels_changed": {…}, "providers": {…}, "channels_used": […], "settled"}`. Only the parts that changed or were asked for are present.

### 5.4 `window.act`

Params: `{"lease", "action_id", "base_rev", "deadline_ms", "steps": [1–8 steps]}`. `deadline_ms` defaults to the sum of the step timeouts plus 500 ms (T) and may not exceed 10,000 (L, V2).

A step: `{"target": handle (optional), "require": {…} (optional), "intent": {…}, "expect": {…} (optional), "timeout_ms": 50–5,000, default 1,500 (T), "risk": class (optional)}`.

Intents:

| `kind` | Parameters | Input class | Default expectation |
|---|---|---|---|
| `key` | `keys`: 1–16 chords (L) | keyboard | `reaction` |
| `text` | `text`: 1–2,000 characters; `replace` (false); `via`: `auto`, `keys`, `paste` (`auto`); `allow_control` (false) | keyboard | `text_contains` on the focused node if it is editable text and the tree is usable; else `reaction` |
| `click` | `at`; `button`: `left`, `right`, `middle` (`left`); `count`: 1–3 (1); `modifiers` | pointer | `reaction` |
| `move` | `at` | pointer | `pointer_at` |
| `drag` | `from`, `to` (both `at` forms); `button` | pointer | `reaction` |
| `scroll` | `at`; `dy`, `dx` in notches, each −20…20 (L) | pointer | `reaction` |
| `invoke` | `ref`; `action` (first action) | semantic | `reaction` |
| `focus_window` | `window` | wm | `window_focused` |
| `close_window` | `window` | wm | `window_absent` |
| `wait` | none; `expect` is required | none | — |

An `at` target is exactly one of:

- `{"ref": "n12"}`: the centre of the node's extents, clipped to its window. Cached extents are used if `tree[w]` has not changed since they were read; otherwise one AT-SPI call refreshes them.
- `{"point": {"space", "x", "y"}}`: a point in a named space (§2.5).
- `{"text": "Open", "nth": 0, "within": handle}`: tree nodes whose name equals the text, then OCR lines containing it. More than one match without `nth` is `target_not_found` with up to 5 `candidates`.

`risk` is the hook for V2's human-confirmation classes (`credentials`, `payment`, `send`, `delete_outside_workdir`, `account_security`). Until a console exists, any step carrying `risk` returns `confirm_required` and is not dispatched.

### 5.5 `require` (preconditions)

`{"focus": handle or null, "modal": null or {"title_re": …} or "any", "ref": "n12", "window": handle}`; every key is optional.

- Step 1 is checked against `base_rev`; step *i* > 1 against the `rev` at which step *i*−1 was verified.
- For each named key the facet must not have changed since that `rev`, else **`stale_rev`**; and the stated value must hold, else **`precondition_failed`**. Both leave the step undispatched.
- **Default** when `require` is omitted: `focus` and `modal` unchanged since that `rev`. In addition, a `ref` target must still resolve to a node with the same role and name, and a `point` target requires the geometry of the window under it and the tile under the point to be unchanged since that `rev` (opt out with `"allow_stale_pixels": true`).
- The default target of step 1 is the window focused at `base_rev`; of step *i* > 1, the window focused when step *i*−1 was verified. If step 1 opens a dialog and says so in its expectation, step 2 targets the dialog without naming it.

### 5.6 `expect` (predicates)

One predicate type serves postconditions and `window.wait` (V2 §3.9). The set is closed.

| Class | `kind` | Parameters |
|---|---|---|
| state | `window_present`, `window_absent`, `dialog_present`, `dialog_absent` | `match`: `{title, title_re, class, type}`, all optional, all must hold |
| state | `window_focused` | `window` or `match` |
| state | `pointer_at` | `point` |
| tree | `node_present`, `node_absent` | `match`: `{role, name, name_re}`; `within`: handle or `"modal"` (default: target window) |
| tree | `text_equals`, `text_contains` | `ref` or `match`; `value`. `"ref": "focus"` means the focused node. |
| tree | `node_state` | `ref` or `match`; `state`: `focused`, `selected`, `checked`, `expanded`, `enabled`, `showing`; `value`: bool |
| ocr | `ocr_text_present`, `ocr_text_absent` | `text` or `re`; `region` |
| pixels | `region_changed` | `region`; compared with the content at dispatch |
| pixels | `region_stable` | `region`; `quiet_ms` |
| reaction | `reaction` | none: any facet of the target window family changed after dispatch (for pointer intents, after the button press, so a hover highlight from the move does not count) |
| composite | `all`, `any` | `of`: 2–4 predicates (L), not nested |

A "dialog" is a window with dialog type or modal state, or one that is transient for another window. `reaction` shows that the application responded to the input. It does not show that the intended thing happened; receipts mark it `predicate_class: "reaction"` so no gate mistakes it for more.

### 5.7 `window.wait`, `action.get`, `session.done`

- `window.wait {lease, expect, since_rev, deadline_ms}` (default 5,000, max 30,000, T) → `{satisfied, rev, evidence, state_delta, reason}`. It dispatches nothing.
- `action.get {action_id, generation (optional)}` → the stored receipt. For an earlier generation the worker reads that generation's journal: an intent with no receipt returns a synthesised receipt with `dispatch_status: unknown`, `effect.status: unknown`, `reason: worker_fault`; it is never `not_found` (V2 §4). `not_found` is returned only when no intent was ever journaled.
- `session.done {lease, status: "success" or "failed" or "blocked", summary}` → journals a `done` record and releases the lease.

### 5.8 The driver adapter

`ark-act mcp --session sN [--images path|inline]` is an MCP stdio server. It connects to `act.sock`, calls `session.attach`, and holds the connection and lease for the driver's lifetime. It exposes four tools (V2 §4):

| Tool | Model supplies | Adapter adds |
|---|---|---|
| `observe` | `scope`, `channels`, `region`, `full` (all optional) | `since_rev` = the `rev` of the last result this model was shown, unless `full` |
| `act` | `steps`, optional `deadline_ms` | `lease`, a fresh `action_id`, `base_rev` = the `rev` of the last result shown |
| `wait` | `expect`, `timeout_ms` | `lease`, `since_rev` |
| `done` | `status`, `summary` | `lease` |

- **`base_rev` is always "what the model last saw".** A model cannot act on a state newer than the one it reasoned about without the worker noticing.
- **No duplicate dispatch.** If the adapter loses the connection or times out after sending an `act`, it reconnects and calls `action.get` with the same ID. It never re-sends.
- **Bounded results.** Deltas ≤40 nodes or 2 KiB; full snapshots ≤200 nodes or 16 KiB (T:V2). An `act` result is rendered from the receipt as: batch status and reason; one entry per step (`effect`, `route`, `reason`); the list of steps not run; the state delta; and `advice` (§7.3). The full receipt stays in the journal.
- **Frames** are returned as file paths by default (Codex reads images from paths). With `--images inline` the frame is also returned as an MCP image block, for API loops whose provider takes images.
- **Codex:** `codex exec` with the adapter registered as an MCP server through `-c mcp_servers.ark.command=…` and `-c mcp_servers.ark.args=[…]` (P: codex-cli 0.163.0 on ArK2 has `codex mcp` and `-c`; the exact keys are confirmed at gate F1).
- **API loops:** `ark-loop` starts `ark-act mcp`, reads the tool list, calls the configured provider with those tools, forwards tool calls, and stops at `done` or after 40 turns (T). Keys come from `CU_MODEL_API_KEY` only (`docs/DRIVERS.md`).
- **Pre-declared CUT** if `codex exec` cannot use an MCP server in gate F1: `ark-act call <tool> '<json>'`, one JSON object per invocation in the `cua` style, attaching per call and keeping the last-shown `rev` in `SD/driver/adapter-state.json`.

### 5.9 `system.info`

`{"worker": {"pid", "generation", "state": "ready" or "fenced" or "stopping", "fence_reason"}, "session": {"id", "profile", "display"}, "rev", "control": {"state", "epoch"}, "channels": {"xtest": {"state", "keyboard", "pointer"}, "evdev": {"state", "grab", "reason"}, "paste": …, "atspi": …}, "providers": {…}, "capture": {"mode": "mmap" or "getimage", "w", "h"}, "counters": {"batches", "steps", "verified", "not_observed", "contradicted", "unknown", "retries", "route_changes", "resync_drift", "false_closes"}, "versions": {…}}`.

### 5.10 Example: rename in one driver turn

The V2 §4 example in this design's schema, as the worker receives it after the adapter has filled in lease, ID and `base_rev`:

```json
{"jsonrpc":"2.0","id":41,"method":"window.act","params":{
  "lease":"<opaque>","action_id":"a-01JCD4","base_rev":1871,"deadline_ms":4000,
  "steps":[
    {"target":"s1/g3fa9c1d2/w7",
     "require":{"focus":"s1/g3fa9c1d2/w7","modal":null,"ref":"n31"},
     "intent":{"kind":"key","keys":["F2"]},
     "expect":{"kind":"dialog_present","match":{"title_re":"^Rename"}},"timeout_ms":1500},
    {"intent":{"kind":"text","text":"reports-2026","replace":true},
     "expect":{"kind":"text_equals","ref":"focus","value":"reports-2026"},"timeout_ms":800},
    {"intent":{"kind":"key","keys":["Return"]},
     "expect":{"kind":"all","of":[
        {"kind":"dialog_absent","match":{"title_re":"^Rename"}},
        {"kind":"node_present","match":{"name":"reports-2026"},"within":"s1/g3fa9c1d2/w7"}]},
     "timeout_ms":1500}
  ]}}
```

## 6. Verification and receipts

### 6.1 Three evidence levels

| Level | What it establishes | Recorded as | Can yield `verified`? |
|---|---|---|---|
| 1. Written | The worker handed the input to the route (X request flushed; helper replied) | `attempts[].written` | No |
| 2. Server-acknowledged | The session's own X server consumed it from the intended device (§4.5) | `attempts[].server_ack` | No |
| 3. Effect | A predicate over application-visible state held after dispatch | `effect` | Yes, and only this |

V2: never treat a flush as proof that the application processed input.

### 6.2 What proves each operation

| Intent | Level 2 proof | Level 3 proof: explicit expectation (strong) | Default when `expect` is omitted (weak unless noted) |
|---|---|---|---|
| `key` | Raw key events, each chord, in order, from the route's device | State or tree predicate naming the result (dialog present, node present, focus moved) | `reaction` |
| `text` | Raw key events, count equals key edges sent; for `paste`, also the selection request from the target client | `text_equals` or `text_contains` read back from the node through AT-SPI | Read-back of the focused editable node (strong); `reaction` if there is none |
| `click`, `scroll` | Raw button events from the route's device; pointer at the target point | State, tree or OCR predicate naming the result | `reaction`, measured from the button press |
| `move` | Raw motion; `QueryPointer` equals the target | `pointer_at` (this *is* the effect of a move) | `pointer_at` (strong) |
| `drag` | Raw press, motions, release | Predicate naming the result | `reaction` |
| `invoke` | AT-SPI method reply `true` | Predicate naming the result | `reaction` |
| `focus_window` | Client message sent | `window_focused`, from the focus event | `window_focused` (strong) |
| `close_window` | Client message sent | `window_absent`, from the destroy or unmap event | `window_absent` (strong) |
| `wait` | — | The given predicate | — |

In every bench the filesystem oracle, or a fixture's own log, stays independent of all of this (V2 §3.9).

### 6.3 Deciding `effect.status`

Evaluation is event-driven: each arriving event that touches a predicate's subject triggers a re-evaluation. Tree predicates re-read their subject with one direct call when the subject's application emits an event. OCR and pixel predicates re-evaluate 30 ms (T) after damage in their region. Before declaring anything other than `verified` at the deadline, the engine takes a **final look**: it re-reads the subject directly (an X query; one AT-SPI call; a fresh OCR of the region). A missed event can therefore delay a verdict but cannot by itself produce a wrong one.

| Status | Rule |
|---|---|
| `verified` | The predicate held at some `rev` after `rev.dispatched`. Verification ends at once (early exit). |
| `contradicted` | The subject was observable with fresh evidence and shows a *different* effect: a text read-back that differs both from the expected value and from the value before dispatch; a dialog or modal appeared that the expectation does not match; focus left the target family. Ends verification at once. |
| `not_observed` | The subject was observable up to the deadline, including the final look, and nothing relevant changed. |
| `unknown` | The subject could not be observed (provider degraded and no equivalent predicate; capture failed), or a focus change or grab arrived between `rev.dispatched` and `rev.verified` that makes the evidence ambiguous (V2 "honest uncertainty"). |

When the AT-SPI provider is degraded, a tree predicate is replaced by its OCR equivalent where one exists (`node_present {name}` → `ocr_text_present`), and the receipt says `fallback_from: "tree"`. `text_equals` on a ref has no reliable OCR equivalent and yields `unknown` with reason `provider_degraded`.

### 6.4 The unexpected-state rule

A batch continues past a verified step only if nothing happened that the driver did not predict. If, at verification, the focused window or the modal differs from before dispatch **and** the step's explicit expectation did not name that change (`dialog_present`, `dialog_absent`, `window_present`, `window_absent`, `window_focused`), the batch stops after that step with reason `unexpected_state`. The step itself keeps its `verified` status.

A default `reaction` expectation never predicts a focus change, so a batch built on defaults stops the moment focus moves. This is the control for V2's risk that multi-step actions widen the blast radius of a wrong target.

### 6.5 Receipt

One receipt per `window.act`. It extends V2's receipt v2. Version 2.

**Batch fields**

| Field | Values |
|---|---|
| `version` | `2` |
| `action_id`, `journal_seq` | The driver's ID; the sequence number of the journaled intent |
| `session`, `generation`, `profile`, `profile_hash`, `control_epoch`, `lease_hash` | Identity. `profile_hash` is `null` in v0. |
| `status` | `completed` (every step verified) / `partial` (at least one step verified, then stopped) / `failed` (first executed step not verified) / `rejected` (nothing dispatched) |
| `phase` | Where the batch ended: `validate` / `dispatch` / `verify` / `done` |
| `dispatched` | Boolean kept for compatibility: true if any step wrote input |
| `dispatch_status` | Aggregate: `unknown` if any step is unknown; else `accepted` if any step is accepted; else `not_dispatched` |
| `effect` | The `effect` of the last executed step; `null` if none executed |
| `reason` | `null` or one enumerated reason (§6.6) |
| `stopped_at` | 1-based index of the step that ended the batch; `null` when completed |
| `steps_total`, `steps_verified` | Counts |
| `rev` | `{base, dispatched, verified}`: `dispatched` is the first write, `verified` the last verification |
| `steps` | One entry per step, including steps that never ran |
| `state_delta` | The bounded observation after the last executed step (session scope, since `rev.base`), so the driver needs no follow-up `observe` |
| `route_change` | Present when a route was flipped (§4.7) |
| `needs_human`, `escalate`, `advice` | §7.3, §7.4 |
| `timing_ms` | `{queue, total}` |

**Step fields**

| Field | Values |
|---|---|
| `i`, `kind`, `target` | Index, intent kind, resolved window handle |
| `dispatch_status` | `not_dispatched` / `accepted` / `unknown` |
| `route` | The route that carried the input, or `null` |
| `attempts[]` | Every route tried, in order: `{route, accepted, written, expected, server_ack: {expected, seen, source}, error}` |
| `focus` | `{before, after}` window handles |
| `effect` | `{status, predicate, predicate_class, expect_source: "explicit" or "default", evidence[], fallback_from}`; `null` if the step never dispatched |
| `reason` | `null`, or why this step stopped; `not_reached` for steps after the stopping step |
| `rev` | `{dispatched, verified}` |
| `timing_ms` | `{queue, dispatch, verify}` |

Evidence items are typed: `window` (handle, title, `rev`), `node` (ref, role, name, text, `rev`), `ocr` (text, box, confidence, frame), `frame` (id, path, rect), `tiles` (count, bounding box), `input` (raw events seen), `selection` (requesting window). Every evidence item and the `state_delta` cite the same `rev` counter.

### 6.6 Reasons

V2's set, used as V2 defines them: `preempted`, `stale_rev`, `stale_generation`, `lease_expired`, `permit_expired`, `timeout` (the batch deadline passed), `backend_unsupported`, `provider_degraded`, `queue_full`, `budget_exceeded`, `confirm_required`.

Added (§1.5): `invalid_request`, `precondition_failed`, `target_not_found`, `focus_failed`, `input_unacknowledged`, `effect_not_observed` (a step's verification window expired), `effect_contradicted`, `unexpected_state`, `channel_down`, `worker_fault`, and the step-only `not_reached`.

### 6.7 Three receipts

The values below are illustrative. None is a measurement.

**Success** (the rename of §5.10; abridged to one full step):

```json
{"version":2,"action_id":"a-01JCD4","journal_seq":118,
 "session":"s1","generation":"3fa9c1d2…","profile":"xvfb","profile_hash":null,"control_epoch":1,
 "status":"completed","phase":"done","dispatched":true,"dispatch_status":"accepted",
 "effect":{"status":"verified"},"reason":null,"stopped_at":null,
 "steps_total":3,"steps_verified":3,"rev":{"base":1871,"dispatched":1872,"verified":1890},
 "steps":[
  {"i":1,"kind":"key","target":"s1/g3fa9c1d2/w7","dispatch_status":"accepted","route":"xtest",
   "attempts":[{"route":"xtest","accepted":true,"written":2,"expected":2,
                "server_ack":{"expected":2,"seen":2,"source":"xtest-keyboard"},"error":null}],
   "focus":{"before":"s1/g3fa9c1d2/w7","after":"s1/g3fa9c1d2/w9"},
   "effect":{"status":"verified","predicate":{"kind":"dialog_present","match":{"title_re":"^Rename"}},
             "predicate_class":"state","expect_source":"explicit",
             "evidence":[{"type":"window","handle":"s1/g3fa9c1d2/w9","title":"Rename \"New Folder\"","rev":1876}]},
   "reason":null,"rev":{"dispatched":1872,"verified":1876}},
  {"i":2,"kind":"text","route":"xtest","dispatch_status":"accepted","effect":{"status":"verified","predicate_class":"tree"}},
  {"i":3,"kind":"key","route":"xtest","dispatch_status":"accepted","effect":{"status":"verified","predicate_class":"state"}}],
 "state_delta":{"rev":1890,"state":{"removed":["s1/g3fa9c1d2/w9"],"focus":"s1/g3fa9c1d2/w7","modal":null},
                "tree":{"window":"s1/g3fa9c1d2/w7","delta":{"changed":["n31 table_cell \"reports-2026\" [selected] @212,148,180,22"]}}},
 "needs_human":false,"advice":"none"}
```

**Failure** (the dialog never appears; later steps never dispatch):

```json
{"version":2,"action_id":"a-01JCD5","status":"failed","phase":"verify","dispatched":true,
 "dispatch_status":"accepted","effect":{"status":"not_observed"},"reason":"effect_not_observed",
 "stopped_at":1,"steps_total":3,"steps_verified":0,"rev":{"base":1901,"dispatched":1902,"verified":1902},
 "steps":[
  {"i":1,"kind":"key","dispatch_status":"accepted","route":"xtest",
   "attempts":[{"route":"xtest","accepted":true,"written":2,"expected":2,"server_ack":{"expected":2,"seen":2,"source":"xtest-keyboard"}}],
   "focus":{"before":"s1/g3fa9c1d2/w7","after":"s1/g3fa9c1d2/w7"},
   "effect":{"status":"not_observed","predicate":{"kind":"dialog_present","match":{"title_re":"^Rename"}},
             "predicate_class":"state","expect_source":"explicit",
             "evidence":[{"type":"frame","frame":"f:9c41…","path":"…/driver/frames/9c41….png"}]},
   "reason":"effect_not_observed"},
  {"i":2,"kind":"text","dispatch_status":"not_dispatched","route":null,"attempts":[],"effect":null,"reason":"not_reached"},
  {"i":3,"kind":"key","dispatch_status":"not_dispatched","route":null,"attempts":[],"effect":null,"reason":"not_reached"}],
 "state_delta":{"rev":1902,"unchanged":true},
 "needs_human":false,"advice":"observe_first"}
```

**Partial** (two steps done; the typed text reads back wrong; the confirming `Return` is never sent):

```json
{"version":2,"action_id":"a-01JCD6","status":"partial","phase":"verify","dispatched":true,
 "dispatch_status":"accepted","effect":{"status":"contradicted"},"reason":"effect_contradicted",
 "stopped_at":2,"steps_total":3,"steps_verified":1,"rev":{"base":1910,"dispatched":1911,"verified":1921},
 "steps":[
  {"i":1,"kind":"key","dispatch_status":"accepted","route":"xtest","effect":{"status":"verified","predicate_class":"state"}},
  {"i":2,"kind":"text","dispatch_status":"accepted","route":"xtest",
   "attempts":[{"route":"xtest","accepted":true,"written":26,"expected":26,"server_ack":{"expected":26,"seen":26,"source":"xtest-keyboard"}}],
   "effect":{"status":"contradicted","predicate":{"kind":"text_equals","ref":"focus","value":"reports-2026"},
             "predicate_class":"tree","expect_source":"explicit",
             "evidence":[{"type":"node","ref":"n44","role":"text","text":"reports-226","rev":1921}]},
   "reason":"effect_contradicted"},
  {"i":3,"kind":"key","dispatch_status":"not_dispatched","route":null,"attempts":[],"effect":null,"reason":"not_reached"}],
 "state_delta":{"rev":1921,"state":{"focus":"s1/g3fa9c1d2/w9","modal":"s1/g3fa9c1d2/w9"},
                "tree":{"window":"s1/g3fa9c1d2/w9","delta":{"changed":["n44 text \"\" [focused,editable] text=\"reports-226\" @420,310,260,28"]}}},
 "needs_human":false,"advice":"observe_first"}
```

The partial receipt tells the driver exactly where the session stands: the rename dialog is still open, the field holds the wrong text, nothing was confirmed. One further `act` (replace the text, press `Return`) finishes the task.

### 6.8 Act journal

`STATE/acts/<generation>.jsonl`; one JSON object per line; `ts` in ISO-8601 UTC as in SPEC §3; `flush` and `fsync` on every line. Record types: `worker_start` (session, profile, display, pid, versions), `attach`, `intent` (`journal_seq`, `action_id`, `lease_hash`, `base_rev`, steps, deadline), `receipt` (the full receipt), `control` (observed transition), `route_change`, `fence`, `done`, `worker_stop`. An `intent` always precedes the first write of its batch; a dispatched action with no `intent` line is an integrity failure, not a logging gap (V2 §5.7). The file has one writer. When the broker exists it becomes the writer and this file's records move into its journal unchanged.

## 7. Failure behaviour

### 7.1 Timeouts

| Timer | Value | Basis | On expiry |
|---|---|---|---|
| Step verification window (`timeout_ms`) | default 1,500 ms; 50–5,000 | T | Final look, then `not_observed` or `unknown`; reason `effect_not_observed` |
| Batch `deadline_ms` | default sum of step windows + 500 ms; max 10,000 | T; max is L (V2) | No further step dispatches; reason `timeout` |
| `window.wait` | default 5,000 ms; max 30,000 | T | `satisfied: false`, reason `timeout` |
| `window.observe` | default 2,000 ms | T | Returns what it has, with the slow provider marked |
| Focus set and confirm | 200 ms × 2 attempts | T; count is L | `focus_failed`, nothing dispatched |
| Server acknowledgement | 50 ms (`xtest`); 150 ms + pacing (`evdev`) | T | `unknown`, `input_unacknowledged` |
| Helper request | 100 ms + pacing | T | Socket treated as failed (§4.7) |
| AT-SPI call | 250 ms; degraded after 2 breaches or one > 1 s | T:V2 | §3.8 |
| OCR region | 1,000 ms | T | §3.8 |
| Settle before `state_delta` | 40 ms quiet, 100 ms cap | T; cap T:V2 | `settled: false` |
| Key and button release on abort | ≤30 ms | T:V2 | Fence (§7.4, F1) |
| Lease after disconnect | 5 s | T:V2 | Lease lapses |
| Queue | 1 executing + 8 waiting | L (V2) | `queue_full` |

### 7.2 What the worker retries by itself

A retry happens inside the step, appears in `attempts[]`, and is bounded at 3 attempts per step (L) and by the batch deadline.

| Situation | Why it is safe | Retry |
|---|---|---|
| Focus is wrong before a keyboard step | Nothing has been written | Set and confirm focus, up to 2 attempts |
| Pointer target occluded | Only motion has been sent | Raise the target, re-check once |
| Modifier state dirty at batch start | Nothing has been written | Release tracked keys, re-check once |
| Route failed with proof of non-delivery (`not_dispatched`: X error before any event; helper wrote 0 frames) | Nothing reached the server | Reconnect once where relevant, then next rung |
| AT-SPI provider degraded before the call was sent | Nothing was sent | Next rung, or OCR for reads |
| `move`: acknowledged but `pointer_at` false | Idempotent: an absolute position | Re-issue once |
| `focus_window`: not observed | Idempotent | Re-issue once |
| `text` with `replace: true` **and** a tree read-back on the same node: not observed or contradicted | Converges: select-all and retype replaces whatever is there, and the read-back checks it | Re-issue once on the same route, then once on the next rung |
| Read side: a snapshot timeout, an OCR child crash | Reads have no effect | Retry once, then degrade |

### 7.3 What stops the batch

Everything else stops the batch at that step, releases held keys, and returns the receipt with a fresh `state_delta`. The worker does not retry, and later steps are `not_reached`.

| Stop | `dispatch_status` | `advice` to the driver |
|---|---|---|
| Acknowledged, effect `not_observed`, for any non-idempotent intent (`key`, `click`, appended `text`, `scroll`, `drag`, `invoke`, `close_window`) | `accepted` | `observe_first` |
| Effect `contradicted` | `accepted` | `observe_first` |
| `unexpected_state` after a verified step | `accepted` | `observe_first` |
| Effect `unknown` | `accepted` | `observe_first` |
| `input_unacknowledged` | `unknown` | `do_not_retry` |
| Partial write (`channel_down`) | `accepted`, with exact counts | `do_not_retry` |
| `stale_rev`, `precondition_failed`, `target_not_found`, `focus_failed` | `not_dispatched` | `retry_ok` |
| `queue_full`, `timeout` before dispatch | `not_dispatched` | `retry_ok` |
| `preempted`, `lease_expired`, `permit_expired`, `stale_generation` | as recorded | `reattach` |
| `backend_unsupported`, `confirm_required`, `budget_exceeded` | `not_dispatched` | `stop` |

Why an acknowledged step with no visible effect is never re-sent: the commonest cause is an application that is slow, not one that ignored the input. Re-sending `Return` to a slow dialog confirms the dialog and then whatever is underneath it. The driver, which is shown the state, decides. The cost is one driver turn; the alternative is a duplicate effect, which V2 treats as a STOP-class defect (action 2.4).

`advice` values: `none`; `retry_ok` (nothing reached the application; the same intent may be re-issued against the new state); `observe_first` (something was dispatched; read the state before deciding); `do_not_retry` (delivery is uncertain; reconcile from the state, never re-send blind); `reattach`; `stop`.

### 7.4 When the worker stops and surfaces (fence)

A fenced worker refuses every mutating call with `needs_human: true`, keeps answering `observe`, `action.get` and `system.info`, journals a `fence` record, and shows `state: "fenced"` with the reason in the health document. Only `ark-worker control --state agent --clear-fence` lifts it, with a new control epoch, and V2's rule then applies: no mutation resumes without a fresh observation and lease.

| # | Condition | Why retrying is wrong |
|---|---|---|
| F1 | A release of a tracked key or button could not be acknowledged | A stuck modifier corrupts every later input |
| F2 | Two consecutive steps with `dispatch_status: unknown`, or one followed by a failed channel probe | The worker no longer knows what the session received |
| F3 | The act journal cannot be written or `fsync`ed | No dispatch without a journal intent |
| F4 | The control page is unreadable or malformed | Fail closed |
| F5 | Lost X connection, X server exit, WM exit, or an uncaught worker fault | The generation is over; the worker journals `worker_fault` and exits rather than continuing on a state it cannot trust |
| F6 | The exclusive-grab check fails after start (xorg) | Isolation defect; V2 rule 7 |
| F7 | 5 consecutive batches from one lease with no verified step (T) | A driver that is not converging is surfaced, not left to hammer. The next `act` returns `budget_exceeded`; the adapter tells the model to call `done` with `blocked`. |
| F8 | The lease's `acts` budget is spent | `budget_exceeded` |

F7 and F8 suspend the lease only; F1–F6 fence the session.

### 7.5 Preemption

When the control page stops authorising writes (pause, takeover, or, later, lease or permit expiry):

- The read immediately before a write is the enforcement. At most one primitive that was already written can land (V2 §3.8).
- No further step dispatches. The step in flight keeps being *verified* to its normal deadline; verification is observation, not mutation.
- Held keys and buttons are released. While a batch runs the worker re-reads the page at least every 5 ms (T), so the release happens within the ≤30 ms target (T:V2).
- The receipt carries `reason: preempted` and the epoch under which it ran. An old-epoch batch never resumes.

### 7.6 Crash and restart

- **Worker dies between intent and receipt.** After restart there is a new generation. `action.get` for the old ID returns `unknown` (§5.7). Nothing is replayed. The adapter gets `stale_generation`, re-attaches, and observes before acting.
- **Helper dies.** `evdev` is `down` for the generation; a batch in flight ends with exact `written` counts; the helper's own exit path releases what it held.
- **AT-SPI or OCR child dies or hangs.** One provider is degraded; the session and the action path continue on the other channels.
- **Driver or adapter dies.** The lease lapses after 5 s (T:V2); a batch already executing completes and its receipt is durable for a later `action.get`.
- **Exactly-once is not promised** across a crash between journaling and delivery (V2 §4). The promise is: never dispatched without an intent, never silently replayed, never reported `not_found` when it might have happened.

## 8. Build order and acceptance tests

### 8.1 Build order

Each step ends in a gate; the next step starts when the gate passes. A failing isolation check is a STOP (V2 rule 7); anything else has its fallback written here.

| Step | Build | Gate (§8.3) | If it fails |
|---|---|---|---|
| **W0** | Prerequisites; sessiond changes S1–S3; fixtures | Every row of the note's acceptance table still passes; A0 | S1 fails → `getimage` capture on xvfb. S3 fails → `evdev` stays `down`, continue on xvfb. |
| **W1** | Worker core: lifecycle, WM, X connections, `rev` and state cache, control page, act journal, socket with `session.attach`, `system.info`, `observe` (`state` only) | A1–A4, B1 (state part) | Fix; no fallback, everything depends on it |
| **W2** | Act path on `xtest`: dispatch sequence, server acknowledgement, state and `reaction` predicates, receipts, dedupe, stale checks, stop rules, fence, preemption | C1–C3, C5 (xvfb pair), D1 (scripted), D2–D5, E1, E3, E4, E6, E7 | Isolation defect → STOP. This step makes V2's D0 trace possible. |
| **W3** | Pixels: shadow framebuffer, tiles, "nothing changed", frames, coordinate spaces | B1–B3, B7, B8 | Budget missed → V2's 2.1 rule. A false `unchanged` → fix; not cuttable. |
| **W4** | AT-SPI provider: tree text, refs, tree predicates, breaker, per-application health | B4, B5, D1 with tree predicates | Provider unreliable → tree is off by default; OCR and state carry observation |
| **W5** | `ark-act` adapter and `ark-loop`; first model-driven runs | F1, F2, then F3 | Codex cannot use MCP → the pre-declared one-shot CLI (§5.8). This step makes V2's D1 demo possible. |
| **W6** | `evdev` channel on xorg: helper socket, pacing, grab check, self-test | A2, C4, C5 (mixed pair), E2 | Grab breaks routing or any host delivery → STOP. Key loss → widen pacing up to the spike-proven value. |
| **W7** | OCR provider, OCR predicates, blind escalation to `pixels` | B6 | OCR unreliable on a surface → that surface goes state → pixels |
| **W8** | Fallback ladder, rejection inference, `paste` and `atspi` rungs, route-table persistence, remaining fault injection | E5, E8, all of D and E again | A silent replay or duplicate effect → STOP |
| **W9** | Measurement run and acceptance report in the style of the note | G1–G3 | Rows missed by less than 2× become the budget with ArK's sign-off; by 2× or more the fast path is dropped (V2 action 2.1) |

W6 and W7 do not depend on each other or on W5 and can run in the other lane.

### 8.2 Fixtures

- **`fx_log`**: a GTK3 window with an entry, a button, a label and a list. It writes every input event it receives (key code, button, root coordinates, time, and the *source device name* GDK reports) and every text change to `$HOME/fx-log.jsonl`. This log is the ground truth for delivery, order, exactness and duplicates. Options: `--tag` (unique window title per session), `--reject-xtest` (drop events whose source device name contains `XTEST`: the XTEST-rejection fixture V2 action 2.2 asks for), `--slow-ms N` (act on each input N ms late), `--hang` (block the main loop on `SIGUSR1`, so AT-SPI calls hang), `--popup-after N` (map an unannounced dialog N ms after the next input), `--repaint` (repaint identical pixels at 10 Hz).
- **`fx_canvas`**: a drawing area that renders known strings at known positions and exposes no accessible children.
- **Thunar** with the filesystem as oracle, as in V2's benches.
- **Host observers**, exactly as Spike B used them: a passive evdev reader on the host's physical devices and `xinput test-xi2 --root` on `:0`. The host is observed, never driven.

### 8.3 Acceptance tests

Run by Raj with shell ground truth, as in the note. **A check never accepts the worker's own receipt as its only evidence.** Counts and tolerances are L (chosen sample sizes and pass bounds; 10,000 and 5,000 are V2's scales).

**A. Lifecycle**

| Item | Ground truth | Pass |
|---|---|---|
| A0 sessiond changes | `ls -l SD/fb/Xvfb_screen0`; `SD/input.sock` owner and mode; the session Xorg log's evdev lines | File is 8,297,632 bytes on xvfb (P); socket is owned by the user, mode 0600; log shows the grab option applied to exactly the session's two nodes |
| A1 start on xvfb | `/proc/<pid>/environ` of the worker and each child; `xprop -root _NET_SUPPORTING_WM_CHECK` on the session display; socket exists | Both markers on every process; openbox owns the WM check; one JSON object with `state: "ready"`; stage timings published |
| A2 start on xorg, self-test | `fx_log` in the session, receiving 200 events the harness sends through the worker on the `evdev` route; a second, non-grabbing reader on both session nodes; an independent grab attempt on each node | 200 of 200 events in the fixture log; zero events at the second reader; the independent grab attempt returns "busy"; health document shows `evdev.grab: true` |
| A3 teardown | The note's teardown checks after `ark-sessiond stop --all` | Zero processes with the session markers; no worker socket; no `ArkDrive` devices; registry empty |
| A4 second worker | Start a second worker on the same session | Refused with `worker_already_running`; the first is undisturbed |
| A5 host untouched | Host Xorg pid and `xinput list` on `:0` before and after the whole suite | Same pid; list byte-identical (the note's check) |

**B. Observation**

| Item | Ground truth | Pass |
|---|---|---|
| B1 unchanged, idle | `strace` on the worker during 1,000 `observe(since_rev)` calls on an idle session | 1,000 of 1,000 `unchanged: true`; zero writes to the X sockets, the provider sockets or the helper socket |
| B2 damage but identical | `fx_log --repaint` running; a second X client subscribed to DAMAGE confirms that damage is occurring | 1,000 of 1,000 `unchanged: true` |
| B3 no false unchanged | `fx_log` changes its label at random times and logs each change with a timestamp; 1,000 changes | Zero observations report `unchanged` after a logged change; each change appears in the next observation's delta |
| B4 tree text | The fixture's known widget list | Every widget appears once with the right role, name and extents within ±1 px; refs are identical across 100 observations |
| B5 accessibility break | `fx_log --hang` in session A alongside a healthy second application; then the same with A's AT-SPI bridge killed | Every `observe` returns inside its deadline; the hung application is `degraded`, the other still returns its tree; observation of the hung window falls to OCR; session B is unaffected |
| B6 OCR | `fx_canvas` known strings and positions; `strace` on the OCR child | Every string found with its box within ±4 px; incremental result equals a full re-read after 200 partial repaints; a repeated read of unchanged pixels sends no request to the OCR child |
| B7 frame fidelity | `xwd -root` on the session display at a quiesced moment | A `scale: 1` frame is pixel-identical to the same rectangle of the `xwd` image, on both profiles |
| B8 frame coordinates | `fx_log` button-press coordinates | 200 clicks given in `frame:` space land on the exact intended desktop pixel |

**C. Actuation**

| Item | Ground truth | Pass |
|---|---|---|
| C1 keys, xvfb | `fx_log` in the session; `fx_log` in a sibling session; host observers | 10,000 tagged key events arrive in order with none missing or duplicated; zero in the sibling; zero on the host |
| C2 text, xvfb | `fx_log` text-change log (not AT-SPI) | 100 runs of 100 characters, each exactly equal; 100-character time published against the ≤50 ms target |
| C3 pointer, xvfb | `fx_log` press coordinates | 500 random points, each exact |
| C4 evdev, xorg | As C1–C3, plus the source device name in `fx_log`; a second, non-grabbing reader on the session nodes; a request for key code 99 sent straight to the helper socket | Same pass criteria as C1–C3; every event's source is `kbd` or `ptr`, never an XTEST device; the second reader sees zero of the 10,000 events (the grab excludes every other consumer, §4.8); the code-99 request is refused whole and `fx_log` shows nothing from it. Negative control: with S3 removed in a scratch session that receives pointer motion only, the second reader does see the events. |
| C5 two sessions at once | `fx_log` in each; host observers; `xdotool getmouselocation` on each session display from an independent client | Interleaved runs on xvfb+xvfb and xvfb+xorg: zero cross-delivery over 10,000 tagged events; each pointer moves only in its own session; zero host events |
| C6 F2 | Host F2 still bound to Application Finder; Thunar in the session | Agent F2 opens the rename dialog in its own session only; nothing appears on the host (G1 bench item) |

**D. Batches and verification**

| Item | Ground truth | Pass |
|---|---|---|
| D1 rename in one `act` | Filesystem oracle | A scripted driver issues exactly one `window.act` of three steps; the folder has the new name; receipt `completed`, 3 of 3 verified; 30 of 30 runs |
| D2 partial | `fx_log` | With step 2 made to fail, the log shows no event from step 3; receipt is `partial`, `stopped_at: 2`, step 3 `not_reached` |
| D3 stale `base_rev` | `fx_log` | 1,000 requests whose `base_rev` predates a focus, modal or geometry change: zero input events in the log; every receipt `rejected` with `stale_rev` (V2 action 2.1) |
| D4 dedupe | Filesystem oracle | The same `action_id` sent twice creates one folder; the second reply is the stored receipt |
| D5 uncertainty | `fx_log --popup-after` | A dialog that appears between dispatch and verification never yields a `completed` batch: the receipt is `contradicted`, `unknown` or `unexpected_state`, and no later step is in the log |
| D6 verified means it happened | Every `verified` step in the whole suite, cross-checked against `fx_log` or the oracle | Zero verified steps without the effect. The count of effects that happened but were not verified is published. |

**E. Failure**

| Item | Ground truth | Pass |
|---|---|---|
| E1 preemption | `fx_log`; an independent `XQueryKeymap` client; timestamps of the control write and of the last logged event | After `control --state paused` during a batch: no event from a later step; no key or button left down; receipt `preempted`; closure time published over 200 runs against V2's <50 ms p95 (G1) and ≤10 ms (G2) targets |
| E2 helper killed mid-text (xorg) | `fx_log` | The receipt's `written` count equals the characters in the log; no duplicates afterwards; `evdev` is `down`; no automatic retry |
| E3 worker killed after intent | Journal file; `fx_log` | `action.get` returns `unknown`, never `not_found`; after restart nothing is re-sent |
| E4 slow application | `fx_log --slow-ms 2000` | A click that takes effect after the window expired is in the log exactly once; receipt `not_observed`, `advice: observe_first` |
| E5 XTEST rejection | `fx_log --reject-xtest` | On xvfb: two `not_observed` steps, then `route_change`, then `backend_unsupported` with `escalate: "relaunch_on_xorg"`; the log never shows an accepted event. On xorg: the same fixture accepts the same intents through `evdev` and they verify (V2 action 2.2). |
| E6 stuck key | Independent `XQueryKeymap` client | A batch aborted while a modifier is held leaves nothing down |
| E7 journal order | Parse `STATE/acts/*.jsonl` after 20 `kill -9` runs at random points | Every dispatched batch has an `intent` line before any `fx_log` event of that batch; every line parses |
| E8 no exec in the action path | `strace -f -e trace=execve` on the worker and its children during G1 | Zero `execve` calls |

**F. Drivers**

| Item | Ground truth | Pass |
|---|---|---|
| F1 Codex | Filesystem oracle; token count from the run record | Codex through `ark-act` creates and renames a folder in Thunar, 3 consecutive correct runs; driver turns, tokens and vision calls (frames viewed) published |
| F2 API loop | Same | `ark-loop` through the same adapter, 3 consecutive correct runs; the same figures published |
| F3 pair | Two oracles; host observers; a human typing in a host editor with F2 bound | Two drivers in two sessions at once, both using keyboard and pointer routes; correct, distinct folder names; zero host or sibling delivery (V2's D1 definition) |

**G. Measurements to publish (W9)**

| Item | What | Compared with |
|---|---|---|
| G1 dispatch and verify | ≥5,000 primitive actions at one and two sessions (V2's rule: thirty task runs are not enough for percentiles): request → input written; with focus set; evdev write → event; 100-character text; event-class verdict; receipt durable; local overhead per action | V2 §3.12 rows (all T:V2) |
| G2 observation | Unchanged (fast and damage-identical paths); cached delta; fresh crop and full frame on each profile; OCR-class verdict; `resync_drift` count | V2 §3.12 rows; §3.4 targets |
| G3 turns | Thunar create-and-rename: driver turns, tokens, vision calls, first-route success rate, retries, route changes | Reported, not gated here: V2's ≤50% targets are relative to a passing baseline (P0.2) that does not exist yet |

Reporting follows the note: verdict, table with evidence, defects found and fixed (including harness and spec defects), simplifications, cleanup state.

## 9. Open items

1. **The grab (§4.8) is the one finding that changes someone else's file** (`xorg.conf.tmpl`) and should go to whoever owns S-B2 today. Until C4 passes, no model-driven driver should use the xorg profile's evdev route.
2. **Python and the V2 budgets.** A single-threaded Python loop may not meet the ≤1 ms and ≤3 ms rows. This design does not claim it will. W9 measures; V2's own rule decides.
3. **Session HOME reuse** (§1.3, last row) belongs to sessiond. The worker does not rely on a pristine HOME, but V2's "a warm session is not pristine" risk is real today for a reused session id.
4. **Tunables marked T** (pacing, quiet windows, OCR preprocessing, the blind threshold, the rejection count of 2, the thrash limit of 5) are start values. Each has a gate that will produce a measured replacement.
5. **Not verified at design time:** raw-event source ids for the evdev slaves on Xorg-dummy (gate C4); `codex exec` using an MCP server end to end (gate F1); `libtesseract` C API behaviour through `ctypes` (gate B6). Each has a fallback in §8.1.
