# ArkDrive worker — reconciled build spec (worker v0)

- Date: Nov 9, 2026. Reconciled by Raj from the dual-model round:
  - **[G]** `ARK-WORKER-ACT-SPEC.md` — GPT-6.1 Sol, High (contract, receipts, reconciliation).
  - **[F]** `WORKER-DESIGN-FABLE.md` — Fable 5.2 (claude-fable-5-2), High (implementation shape, mechanisms, gates).
- Authority: `AGENTIC-WM-PLAN-V2.md` (V2) including the final continuous-work
  directive (no calendar staging, no midnight pause; gates judge done-ness).
- Rule of reading: where this spec names a section of [G] or [F] as adopted,
  that section is part of this spec unchanged. Where the two designs
  conflicted, §2 below is the decision and the reason. Everything not
  mentioned follows [G] for contract semantics and [F] for mechanism.
- Labels: **M** measured, **T** target (unmeasured), **L** contract limit —
  as in both sources. No number in either source design is a result.

## 1. What is being built

One deterministic `ark-worker` per `ark-sessiond` session. The worker owns
that session's WM, sensing and input on a driver's behalf, performs no model
calls, and answers four driver tools through one adapter (`observe`, `act`,
`wait`, `done`). A driver supplies intent plus an explicit postcondition per
step; the worker resolves targets, dispatches, evaluates the predicate, and
returns a receipt and the resulting state delta. One ordered mutation queue
per session; different sessions run independently.

Deliverables (all under `wm/`, extending the built sessiond v0):

| Piece | Form |
|---|---|
| `ark-worker` | Python 3, single-threaded `selectors` loop, python-xlib (two connections: `ev` events/queries, `inp` XTEST), Pillow. [F §2.1, §2.4] |
| `worker_atspi.py` | Provider child, pyatspi; all AT-SPI calls and listeners. Restartable. [F §2.1] |
| `worker_ocr.py` | Provider child, `libtesseract.so.5` via ctypes, loaded once. Restartable. [F §2.1] |
| `openbox` + pinned rc | Session WM, child of the worker; zero key bindings (no F2), click-to-focus. [F §2.1] |
| `mkdevs.py` (extended) | Existing root uinput helper plus the gated event socket (§4). Still launched by sessiond. |
| `ark-act` | Driver adapter: MCP stdio server, stdlib only; pre-declared one-shot CLI fallback if gate F1 fails. [F §5.8] |
| `ark-loop` | Minimal API-model loop over `ark-act`, using the `CU_MODEL_PROVIDER` presets. [F §5.8] |
| `fixtures/` | `fx_log`, `fx_canvas` ground-truth fixtures. [F §8.2] |
| Profiles | Versioned profile JSON, canonicalised and SHA-256 hashed, per [G] "Development profile data", restricted to the fields v0 uses (§5). |

Not built in v0: the broker as a separate process, leases with MAC, warm
pool, hash-chained journal, signed profiles, CDP provider (route and channel
reserved, returning `backend_unsupported`), Xpra/console, systemd units,
credential proxy. The wire format, method names and journal envelope are the
production contract so the broker can take over the worker's socket role
later without the adapter changing [F §5.1].

## 2. Reconciled decisions (where [G] and [F] differed)

- **R1 — Actuation order: physical-first [F], including for text.**
  AT-SPI finds and verifies (resolve a ref to bounds, read back effects);
  the session's physical channel acts. Text order: keys → paste → AT-SPI
  set-text. Click: physical → AT-SPI action on a ref. `invoke` is the
  semantic intent in both designs and stays AT-SPI. Reasons [F §1.5.1]:
  D1/G1 require physical keyboard and pointer paths; a semantic click
  measured ~0.2 s (M:V2) against a ≤3 ms dispatch target (T:V2); a blocking
  AT-SPI action is a plausible cause of the historical Thunar a11y outage.
  This deviates from a plain reading of V2 §3.6 ("AT-SPI handles exposed
  controls") and is recorded as the one contract change in this spec.
  [G]'s per-operation ladder is otherwise adopted, with CDP rungs removed
  (R3) and route eligibility rules unchanged: fallback only after definite
  non-dispatch [G, "Capability ladder"].
- **R2 — No automatic re-issue of a submitted text replace.** [F §7.2]
  proposed one convergent retry of `text, replace:true` on a fresh same-node
  read-back. Rejected: [G] and V2's never-replay rule win — a read-back
  channel can itself be the degraded one. If reconciliation proves the
  effect, the step completes without a second dispatch [G, retry matrix];
  otherwise the batch stops and the driver decides from fresh state.
- **R3 — CDP is reserved, not built.** [G] specified the Chromium page
  provider now; [F] reserves it. v0's acceptance target is native (Thunar);
  the page provider lands after the core gates pass, following [G]'s CDP
  rules (registered endpoint only, no arbitrary Runtime evaluation, native
  dialogs stay on AT-SPI/X).
- **R4 — Broker collapsed into the worker for v0 [F §1.4], with [G]'s
  semantics.** The worker holds the driver socket and issues `journal_seq`;
  the lease is a connection-bound token checked by peer UID. Kept from [G]:
  the attach → fresh-observation → ready handshake before any act is
  admitted; the single authoritative action journal
  (`STATE/acts/<generation>.jsonl` [F §6.8] — one writer, the worker; when
  the broker exists it becomes the writer and the records move unchanged);
  intent dedupe by `(generation, action_id)`; the
  `awaiting_reconciliation` state and the `visual_review` handshake
  (§6). Recorded in every manifest: `development_same_uid=true`,
  `helper_identity=root`, `profile_signature=unsigned`,
  `audit_mode=jsonl` [G].
- **R5 — Control page: [F]'s 64-byte file for v0**, written by
  `ark-worker control`, read by worker and helper before every write/frame,
  with [G]'s epoch fencing and fail-closed semantics. [G]'s native
  `ark_gate.c` shared page is deferred to the broker milestone; the file
  page's write-order protocol [F §2.6] is the v0 mechanism.
- **R6 — Explicit `expect` on every step [G]; `reaction` never verifies a
  mutation [G]; unexpected-state stop [F §6.4] also applies.** The only
  default expectations are the strong ones both designs agree on:
  `move` → `pointer_at`, `focus_window` → `window_focused`,
  `close_window` → `window_absent`, `text` → whole-field `text_equals`
  read-back (computed expectation [G]). Any other step without an explicit
  application-state predicate is `invalid_expectation` before dispatch.
  Predicates use [G]'s closed set and strength table, with [F]'s `match`
  object (`title`, `title_re`, `class`, `type`) as the dialog/window
  matcher — constrained by [G]'s rule that `dialog_present` additionally
  requires the profile's dialog classifier (§5). Cross-step bindings use
  [G]'s `{from_step, name}` mechanism, exported only by verified steps.
- **R7 — Server acknowledgement = [F]'s XI2 raw-event check** (expected
  source device, in order) on both routes, as evidence level 2. It proves
  routing only and never yields `verified` [both]. The evdev helper's reply
  [F §4.4] carries exact `frames_written`; a lost helper ack means delivery
  unknown [G].
- **R8 — Capture: mmap first on xvfb, GetImage fallback [F §3.2, S1];**
  xorg uses GetImage of the dirty bounding box in v0. [G]'s MIT-SHM shadow
  is the S-CAP spike's question, decided by measurement, not assumed.
  Frame, tile, hash, mask and budget rules: [G, "Pixels, OCR and bounded
  output"] with [F §3.4]'s lazy-hash and settle-before-reporting rules.
- **R9 — Helper protocol: [F §4.4] frames + [G]'s bind ceremony and chunk
  dedupe.** Sessiond launches the helper dormant with
  `--event-socket --broker-uid` [G]; the worker binds once per device
  lifetime after SO_PEERCRED, PID/starttime, device-identity and
  closed-gate verification [G]. Requests carry `seq`; same `seq` + same
  payload returns the stored reply and never rewrites; changed payload is
  an error [G]. The helper refuses key code 99 outright and the worker
  rejects `Sys_Req`/`Print` chords on the evdev route [F §4.8].
- **R10 — Runtime choices from [F]'s design-time checks (P):** python-xlib
  with XI 2.2 negotiated explicitly and `DamageQueryVersion` before
  `DamageCreate`; pyatspi 2.58.1 child; tesseract via ctypes (tesserocr is
  not verified installed); evdev key codes derived from the session's own
  XKB mapping, never a fixed offset (both designs agree; [F]'s "X keycode
  minus 8" shortcut is NOT adopted — [G]'s compiled-from-keymap rule is).
- **R11 — Fences: [F §7.4] F1–F8 adopted wholesale**, plus [G]'s rule that
  an unresolved effect fences mutation admission (not the whole worker)
  until reconciliation or retirement. Fence and reconciliation are
  different states in `system.info`.
- **R12 — Reason codes: the union** of V2's set, [G]'s additions and
  [F §6.6]'s additions. Where names collide in meaning, [G]'s name wins
  (`precondition_failed` covers [F]'s uses; `effect_not_observed` is a
  step outcome, the batch `reason` for a verification expiry is `timeout`
  per [G]'s table — [F]'s receipt examples are illustrative only).

## 3. Invariants (every gate enforces these)

From [G] "Invariants" and [F] §0, merged:

- Driver → worker → session backend. Mutations only via the worker's
  socket, carrying generation, control epoch and durable `journal_seq`.
- One `rev` per generation, owned by the worker; one action executes per
  session; at most 8 queued (L).
- The control page is the only mutation authority; worker and helper check
  it immediately before each mutating write, on every route including
  paste. Gate closure permits only release of tracked holds, journaled,
  never counted as completing the intent.
- Every step begins with verified targeting and ends with its explicit
  predicate. Flush, helper ack and raw-event ack do not prove effect.
- A batch is sequential and non-transactional. No rollback. Stop on the
  first unverified step. Never replay an unknown or merely unobserved
  action.
- Never inject on the host display; never host ydotool; never reopen a
  global bus or clipboard; no route ever leaves the session — exhausting
  the ladder is a stop with `escalate`, never a host fallback.
- The worker makes zero model calls and zero fork/exec in the action path
  (checked by `strace`, gate E8 [F]).

## 4. Sessiond changes (v0 prerequisites) — [F §2.3], adopted

| # | Change | If absent |
|---|---|---|
| S1 | Xvfb started with `-fbdir <SD>/fb` (dir created first) | xvfb capture falls back to GetImage; slower budget label |
| S2 | `mkdevs.py` started with `--event-socket <SD>/input.sock --owner-uid <uid> --control <SD>/control` | evdev channel `down`; xorg acts via XTEST only |
| S3 | `xorg.conf.tmpl`: `Option "GrabDevice" "true"` in both InputDevice sections | evdev `down` with `no_exclusive_grab` |

Plus [G]'s bind-time requirements on the same helper: dormant until bound,
exactly one bind per device lifetime, assigned-node identity loss or grab
loss retires the generation, live host sentinel required before input
admission (sentinel mechanics per Spike B / [G]).

## 5. Profiles — [G], v0 subset

Closed-schema JSON, canonicalised (sorted keys), SHA-256 `profile_hash`
(not null — [G] over [F §1.4e]; the hash is cheap and the route table and
receipts key on it). v0 uses: `schema, backend, resolution
{1920×1080×24} (T), keymap us, locale C.UTF-8, wm openbox (no bindings),
applications, input {inter_key_delay_ms 0, double_click_gap_ms 80,
pointer_tolerance_px 1} (T), providers` timers. The `thunar` entry is
[G]'s exactly: `surface "controls"`, `text_mode "plain"`, shortcuts
(rename F2, select_all Ctrl+a, paste Ctrl+v, accept Return, cancel
Escape), the canonical role map, and the rename-dialog classifier (newly
mapped transient of the Thunar family; exactly one enabled editable text
field whose initial value equals the item's pre-dispatch name; enabled
buttons named exactly `Rename` and `Cancel`). If the classifier does not
match the installed Thunar, the run records a profile incompatibility and
the route is unavailable — the worker never guesses a title or field.
Unknown executable hashes get metadata/pixel/physical capability only.
Route hints key on `(profile_hash, app_build, toolkit, surface_role,
operation)` [G]; flips persist in `STATE/route-table.json` [F §4.6].

## 6. API, receipts, failure rules

- Transport, types, `window.observe` / `window.wait` / `window.act`
  schemas, guard dependencies, predicate strength table, rename example:
  [G] "Driver API and exact schema" through "Predicate language", with
  [F §5.4]'s additional intents `focus_window`, `close_window`, `wait`
  and [F §5.3]'s `ignore_regions` on observe. `at` targets may also be
  `{text, nth, within}` [F §5.4] — multiple matches without `nth` are
  `target_not_found` with candidates, never a guess.
- Receipts: [G]'s receipt v2 field set and outcome table are normative,
  plus per-attempt `server_ack {expected, seen, source}` [F], and
  `predicate_class` + `expect_source` per step [F], and batch `advice`
  [F §7.3]. Batch status vocabulary is [G]'s
  (`success|failed|partial|unknown`, pre-dispatch rejection is a failed
  receipt with `dispatch_status: not_dispatched` — [F]'s separate
  `completed`/`rejected` names are not used).
- Journal, dedupe, `action.get`, durable retention, late reconciliation
  records: [G] "Receipts, durability and partial completion".
- Timers and bounds: [G]'s table (V2 values), with [F §7.1]'s additions
  (focus 200 ms × 2, ack windows 50/150 ms, settle 40 ms/100 ms cap).
- Retry matrix: [G]'s, amended by R2. [F §7.2]'s pre-dispatch retries
  (focus fix, occlusion raise, dirty-modifier release, reconnect once)
  are all pre-dispatch or idempotent and stand.
- Synthetic-input rejection inference: [F §4.7] (2 consecutive
  acknowledged, focused, zero-reaction `xtest` steps flip the entry;
  xvfb then stops with `backend_unsupported` + `escalate:
  relaunch_on_xorg`). A fixture/policy classification [G] also marks an
  entry rejected without production probing.

## 7. Build order and gates

Numbering [F §8.1]; gate letters [F §8.3] with [G]'s negative controls
folded in. Codex implements; Raj verifies from an independent shell
harness with ground truth outside the worker (fixture logs, filesystem
oracle, host/sibling observers). A check never accepts the worker's own
receipt as its only evidence. Isolation failure at any gate is a STOP.

| Step | Build | Gates |
|---|---|---|
| W0 | Prerequisites (install openbox); sessiond S1–S3; fixtures `fx_log`, `fx_canvas` | sessiond v0 acceptance still passes; A0, A5 |
| W1 | Worker core: lifecycle, WM, X connections, `rev`/state cache, control page, act journal, socket, `session.attach`, `system.info`, `observe` (state only) | A1–A4, B1 (state part) |
| W2 | XTEST act path: dispatch sequence [F §4.2], raw-event ack, state predicates + strong defaults, receipts, dedupe, stale checks, stop rules, fence, preemption | C1–C3, C5 (xvfb pair), D2–D5, E1, E3, E4, E6, E7 |
| W3 | Pixels: shadow framebuffer, tiles, cheap unchanged, frames, coordinate spaces | B1–B3, B7, B8 |
| W4 | AT-SPI provider: tree text (`lines/1` [F §3.5]), refs, tree predicates, breaker, per-app health | B4, B5, D1 (tree predicates) |
| W5 | `ark-act` adapter + `ark-loop`; first model-driven runs | F1, F2, then F3; D1 scripted→model |
| W6 | evdev channel: helper socket, pacing, grab proof, self-test [F §4.9] | A2, C4, C5 (mixed pair), E2 |
| W7 | OCR provider and predicates; blind escalation to pixels | B6 |
| W8 | Fallback ladder, rejection inference, paste + atspi rungs, route persistence, remaining fault injection | E5, E8, all of D and E again; [G] negative controls (delayed effect, partial text, cross-session single-instance, early stop at every step position) |
| W9 | Measurement run and acceptance report in the sessiond note's style | G1–G3 [F]; [G] qualification table published, not inferred |

C6 (host F2 untouched) runs with W2 and again at W5. The paired Thunar
D1/F3 runs are the Phase 1 milestone: two sessions, two drivers,
create + rename, filesystem oracle, host untouched.

## 8. Open items carried, not decided here

- [F §9]: sessiond HOME reuse across recycled session ids; the session
  event nodes' 0666 mode (S-B2 should remove it); tunables marked T get
  measured replacements at their gates.
- [G]: S-CAP (MIT-SHM) qualification; Cycles GPU qualification stays
  blocked on VRAM until a session can free ≥~1 GiB.
- R1 is the one deliberate deviation from V2's wording. If ArK reverses
  it, only §2 R1 and the route table change; nothing else in this spec
  depends on it.
