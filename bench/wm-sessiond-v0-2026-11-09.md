# ark-sessiond v0 — Phase 1 first component: session manager

- Plan: ArkDrive Agentic WM, Phase 1 (per `docs/AGENTIC-WM-PLAN-V2.md` §3.1/§3.4/§5.7 shapes, simplified for v0 — see below). Schedule per ArK's Nov 9 directive: continuous work, gates judge done-ness only.
- Date: Nov 9, 2026 (evening PT). Host: ArK2 (arktx).
- Build rule followed: Raj spec (`wm/SPEC.md`, zero open design decisions) → Codex implemented on ArK2 (`codex exec`, detached; main run 51,690 tokens, plus one one-line fix run) → Raj verified with an independent shell acceptance test.
- Code: `wm/` in this repo — `ark-sessiond` (Python 3, stdlib only, one file), `mkdevs.py` (per-session root uinput helper), `ark-xorg-launch.sh`, `xorg.conf.tmpl`, `README.md`, `SPEC.md`. Clone also lives on ArK2 at `~/arkdrive`. Runtime state on ArK2: `~/.local/state/ark-sessiond/` (registry + `journal.jsonl`).
- Recipes reused verbatim from the Phase 0 spikes: private D-Bus + AT-SPI per session with GetAddress discovery (Spike A); Xorg-dummy flags `-sharevts`, `AutoAddGPU=false`, `/dev/dri` bind-mask in a private mount namespace, udev `ID_SEAT=seat-arkdrive` host exclusion, device reattach + enable, `Device Accel Profile -1` (Spike B).

## CLI (as specified; one JSON object on stdout per call)

- `ark-sessiond start --profile xvfb|xorg` → `{session, display, profile, state:"ready", timings}`
- `ark-sessiond list` → live sessions with profile, display, pid, uptime
- `ark-sessiond run --session sN -- <cmd...>` → runs cmd with the session env (DISPLAY, DBUS_SESSION_BUS_ADDRESS, HOME, XDG_RUNTIME_DIR, AT_SPI_BUS_ADDRESS), cwd = session home; child output goes to stderr so stdout stays pure JSON; result `{exit_code, duration_ms, ok}`; sessiond exits with the child's code
- `ark-sessiond stop --session sN` / `stop --all` → full teardown (pid kills + /proc environ marker sweep, Spike A pattern)
- Every start/run/stop appends one JSONL receipt to `journal.jsonl` (`ts, session, action, ok, duration_ms`, + profile / exit_code+cmd / error) — the seed of V2's audit journal.

## Acceptance test (Raj-run, shell ground truth) — VERDICT: PASS

Two runs plus a ground-truth recheck; every item below verified PASS.

| Item | Result | Evidence |
|---|---|---|
| xvfb start ×2 | PASS | s1 `:91`, s2 `:92`, both `state:"ready"` |
| xvfb start timing | — | total 610–624 ms (display 50 ms, bus 2–3 ms, AT-SPI 519–567 ms) |
| run exit codes | PASS | rc 0 both sessions |
| marker isolation | PASS | `marker-s1` only in s1's HOME, `marker-s2` only in s2's HOME (both directions, via `run ... touch`) |
| run env | PASS | `$DISPLAY` inside s1's run = `:91` |
| list | PASS | both sessions, correct ids/displays/pids |
| xorg start | PASS | s3 on `:93` `state:"ready"`, total 1798–1819 ms (devices 1145 ms incl. 1 s udev settle, Xorg 127–147 ms, AT-SPI ~519 ms) |
| xorg devices present | PASS | `xinput list` on `:93` shows `kbd` + `ptr`; `ArkDrive s3` nodes in /sys/class/input |
| xorg devices enabled | PASS | ground-truth recheck: `Device Enabled (120): 1` for BOTH kbd and ptr |
| host exclusion | PASS | all session nodes carry `ID_SEAT=seat-arkdrive`; host `:0` xinput shows zero ArkDrive devices |
| stop --all teardown | PASS | zero processes with the session marker env; zero X locks on session displays; zero `ArkDrive *` input devices; udev rule and `/etc/X11/ark-sessiond-*.conf` removed; registry empty |
| failed-start teardown | PASS | run 1's failed xorg start still removed its udev rule, conf, devices and processes |
| host desktop untouched | PASS | host Xorg pid 2574 alive and `:0` responsive before and after (same pid Spike B recorded) |
| journal | PASS | one line per mutating call, correct shapes, including the ok:false line for the failed start |

### Defects found and fixed during verification

1. **Spec bug (Raj's), found by run 1:** the spec pinned the enabled-check to the literal string `Device Enabled (1): 1`, but the `(1)` is xinput's variable property atom id (real value seen: 120). Xorg had in fact opened and configured both devices correctly. Fix run: Codex changed the regex to `Device Enabled \([0-9]+\):\s*1` (one line); run 2 then passed xorg start end-to-end.
2. **Acceptance-harness bugs (Raj's), not product bugs:** run 1's host check grepped for `Xorg :0`, but the host server runs via `-displayfd` (no `:0` in its cmdline) — fixed to pid + `:0` responsiveness; run 2's `xorg_ptr_enabled` item repeated the same literal-`(1)` grep mistake — disproven by the direct recheck above (`Device Enabled (120): 1`).

## Simplifications vs V2 (also in `wm/README.md`)

(a) One-shot CLI instead of the ark-broker / ark-sessiond daemons, JSON-RPC socket, leases, control page and `rev` counter. (b) Journal is plain append-only JSONL — no hash chain or signed checkpoints (§5.7 deferred). (c) No systemd units or cgroups — detached processes tracked by pid + /proc environ marker sweep. (d) AT-SPI discovery shells out to `gdbus`; D-Bus/X readiness use platform tools (`dbus-daemon`, `xinput`) — sessiond's own code is stdlib-only, its runtime tools are the platform's. (e) Profiles are bare names, not signed immutable profile documents referenced by hash. (f) No warm pool — every start is cold (still ≤0.65 s xvfb / ≤1.9 s xorg measured, inside V2's cold-start targets of 2.5 s / 4 s). (g) The xorg profile creates and attaches input devices but sessiond injects no input — actuation is the worker's job. (h) Single user, no per-slot UIDs.

## Cleanup

ArK2 left clean: no live sessions, registry empty, no session processes/locks/devices, udev rule and staged Xorg confs removed, host desktop (pid 2574) untouched. The `~/arkdrive` clone and `~/.local/state/ark-sessiond/` (journal evidence) remain by design. Nothing committed — the parent agent commits centrally.
