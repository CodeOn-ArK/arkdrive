# ark-sessiond v0 — implementation spec (implement EXACTLY this; decide nothing)

Create these files in `~/arkdrive/wm/` on this machine (ArK2). Do not modify
anything outside `wm/`. Proven recipes you must reuse live in `~/wm-spike-a/`
and `~/wm-spike-b/` (read them if any detail below is unclear; where this
spec and a spike script differ, this spec wins).

Files:
1. `ark-sessiond` — Python 3, standard library ONLY, executable (`chmod +x`).
2. `mkdevs.py` — Python 3, stdlib only (ctypes uinput, adapted from
   `~/wm-spike-b/mkdevs.py` but per-session, see §4).
3. `ark-xorg-launch.sh` — bash, same logic as `~/wm-spike-b/launch-xorg.sh`.
4. `xorg.conf.tmpl` — byte copy of `~/wm-spike-b/xorg.conf.tmpl`.
5. `README.md` — usage summary + the simplifications list from §8 verbatim.

## 1. State layout

- STATE = `~/.local/state/ark-sessiond` (os.path.expanduser).
- Registry: `STATE/sessions.json` (JSON object: sid -> record).
- Journal: `STATE/journal.jsonl`.
- Lock file: `STATE/.lock`; every registry read/write happens under an
  exclusive `fcntl.flock` on it.
- Per session: `STATE/run/<sid>/` containing `home/`, `runtime/`
  (mode 0700), `logs/`.
- Marker env on EVERY process sessiond spawns for a session (including via
  sudo, using `sudo -n env VAR=... cmd`): `ARK_SESSIOND=<STATE absolute path>`
  and `ARK_SESSION_ID=<sid>`.
- Session record fields: `session, profile, display (int), pid_x, pid_dbus,
  bus_address, atspi_address, pid_atspi_launcher, pid_registryd, home,
  runtime_dir, session_dir, created_at (epoch float)`; xorg adds `vt (int),
  devices {"kbd": node, "ptr": node}, mkdevs_pid (int)`.

## 2. CLI (argparse; JSON object on stdout, exactly one per call)

- `ark-sessiond start --profile xvfb|xorg`
  -> `{"session": "s1", "display": ":91", "profile": "xvfb",
      "state": "ready", "timings": {"display_ms": int, "bus_ms": int,
      "atspi_ms": int, "total_ms": int}}` (xorg timings add `devices_ms`,
      `xorg_ms`). Session ids: `s` + (max existing numeric suffix + 1),
      starting at `s1`. Display: smallest d in 91..120 with no
      `/tmp/.X<d>-lock`, no `/tmp/.X11-unix/X<d>`, and not used by a live
      registry entry.
- `ark-sessiond list`
  -> `{"sessions": [{"session", "profile", "display" (":91"), "pid",
      "uptime_s" (int), "state" ("ready" if pid_x alive else "dead")}]}`,
      sorted by numeric suffix.
- `ark-sessiond run --session sN -- <cmd...>`
  runs cmd with cwd = session home and env overridden: `DISPLAY`,
  `DBUS_SESSION_BUS_ADDRESS`, `HOME`, `XDG_RUNTIME_DIR`, `ARK_SESSIOND`,
  `ARK_SESSION_ID`, and `AT_SPI_BUS_ADDRESS` if an address is stored.
  The child's stdout AND stderr both go to sessiond's stderr (stdout stays
  pure JSON). Result: `{"session", "exit_code", "duration_ms", "ok"}`
  where ok = (exit_code == 0). sessiond exits with the child's exit code.
  Unknown session: stdout `{"error": "unknown session", "session": "sN"}`,
  exit 2.
- `ark-sessiond stop --session sN` -> `{"session": "sN", "state": "stopped",
  "ok": true}`. `ark-sessiond stop --all` -> `{"stopped": ["s1", ...],
  "ok": true}`. Unknown session on stop: same error shape as run, exit 2.

## 3. Journal

Every start/run/stop appends ONE line to `STATE/journal.jsonl`:
`{"ts": <ISO-8601 UTC, e.g. 2026-11-09T21:40:00Z>, "session": sid|null,
"action": "start"|"run"|"stop", "ok": bool, "duration_ms": int}` plus
extras: start adds `"profile"`; run adds `"exit_code"` and `"cmd"` (list);
failures add `"error"` (string). Write with flush + os.fsync.

## 4. mkdevs.py (root helper, one session)

Args: `--fifo PATH --devices-json PATH --name-prefix "ArkDrive s1"`.
On startup: write own pid to `<devices-json>.pid`; create the fifo if
missing; create TWO uinput devices named `<prefix> kbd` and `<prefix> ptr`
using the ctypes code from `~/wm-spike-b/mkdevs.py` `make_dev` (kbd: EV_KEY
+ EV_SYN, KEYBIT codes 1..248; ptr: EV_KEY + EV_REL + EV_SYN, REL_X/REL_Y/
REL_HWHEEL/REL_WHEEL, BTN_LEFT/BTN_RIGHT/BTN_MIDDLE); locate each node by
scanning `/sys/class/input/event*/device/name`; chmod node 0666; write
devices-json `{"kbd": node, "ptr": node}`. Then loop reading fifo lines
(reopen on EOF) until a line `quit`: then UI_DEV_DESTROY both devices,
close fds, remove devices-json, exit 0.

## 5. ark-xorg-launch.sh

Args: `PIDFILE DISPLAY CONFNAME VT`. Body identical in logic to
`~/wm-spike-b/launch-xorg.sh`: write `$$` to PIDFILE, then
`exec unshare -m bash -c '...'` that bind-mounts /dev/null over
/dev/dri/card0, card1, renderD128, renderD129 (when present) and execs
`Xorg "$DISPLAY" -config "$CONFNAME" -noreset -nolisten tcp -novtswitch
-sharevts "$VT"`.

## 6. start, profile xvfb (in order; time each stage)

1. Create dirs (runtime 0700).
2. Spawn `Xvfb :<d> -screen 0 1920x1080x24 -nolisten tcp`, env: markers +
   session HOME + XDG_RUNTIME_DIR; `start_new_session=True`; stdout/stderr
   to `logs/xvfb.log`. Readiness: poll every 0.05 s up to 5 s — connect an
   AF_UNIX stream socket to `/tmp/.X11-unix/X<d>`; success = ready; if the
   process exits first, fail.
3. Run `dbus-daemon --session --fork --print-address=1 --print-pid=1` with
   env markers + HOME + XDG_RUNTIME_DIR + DISPLAY; parse address (line 1)
   and pid (line 2) from stdout.
4. Spawn `/usr/libexec/at-spi-bus-launcher` (env + DBUS_SESSION_BUS_ADDRESS,
   log to `logs/atspi-launcher.log`), record pid; sleep 0.5 s; spawn
   `/usr/libexec/at-spi2-registryd` (same env, `logs/registryd.log`),
   record pid.
5. AT-SPI address: poll every 0.25 s up to 6 s running
   `gdbus call --session --dest org.a11y.Bus --object-path /org/a11y/bus
   --method org.a11y.Bus.GetAddress` with the session env; extract with
   regex `unix:[^"',)]+`. (Discovery MUST use this GetAddress path, per
   Spike A: the AT_SPI_BUS X root property is unreliable with 2+ sessions.)
6. Save registry, journal, print result JSON.

## 7. start, profile xorg (extra steps before §6 steps 3–5; X server differs)

1. Preflight: if any `/sys/class/input/event*/device/name` starts with
   `ArkDrive ` and does not belong to a live registry session, fail with
   error `stale device: <name>`.
2. udev rule: if `/etc/udev/rules.d/49-arkdrive-sessiond.rules` is missing
   or differs, install via sudo tee with EXACTLY this content (one line):
   `SUBSYSTEM=="input", ATTR{name}=="ArkDrive *", ENV{ID_INPUT}="0", ENV{LIBINPUT_IGNORE_DEVICE}="1", ENV{ID_SEAT}="seat-arkdrive", TAG+="seat"`
   then `sudo -n udevadm control --reload-rules`.
3. mkfifo `session_dir/cmd.fifo`. Launch helper:
   `sudo -n env ARK_SESSIOND=<STATE> ARK_SESSION_ID=<sid> python3
   <wm>/mkdevs.py --fifo <fifo> --devices-json <session_dir>/devices.json
   --name-prefix "ArkDrive <sid>"`, detached, output to `logs/mkdevs.log`.
   Read helper pid from `devices.json.pid`; poll up to 10 s for
   devices.json. Then sleep 1.0 s (udev settle) and
   `sudo -n chmod 666 <kbd> <ptr>` (re-assert after settle).
   Verify for each node: `udevadm info -q property -n <node>` output
   contains `ID_SEAT=seat-arkdrive`; else fail.
4. Render `xorg.conf.tmpl` with @KBD@/@PTR@ replaced by the nodes into
   `session_dir/xorg.conf`; `sudo -n cp` it to
   `/etc/X11/ark-sessiond-<d>.conf`. VT number = display - 84 (91 -> vt7).
5. Launch: `sudo -n env ARK_SESSIOND=<STATE> ARK_SESSION_ID=<sid> bash
   <wm>/ark-xorg-launch.sh <session_dir>/xorg.pid :<d>
   ark-sessiond-<d>.conf vt<vt>`, detached, output to `logs/xorg-launch.log`.
   pid_x = contents of xorg.pid (poll up to 5 s for the file). Readiness:
   same socket poll as §6.2; fail if pid dies.
6. Device attach (as the normal user, DISPLAY=:<d>): run each of
   `xinput reattach kbd "Virtual core keyboard"`,
   `xinput reattach ptr "Virtual core pointer"`, `xinput enable kbd`,
   `xinput enable ptr`, `xinput set-prop ptr "Device Accel Profile" -1`;
   then verify `xinput list-props kbd` and `xinput list-props ptr` each
   contain a line matching `Device Enabled (1): 1`. Retry the whole attach
   block up to 5 times with 0.3 s gaps; fail if still not enabled.
   (Config-section devices start as floating slaves; both reattach AND
   enable are required — Spike B finding 4.)
7. Then §6 steps 3–5 (buses + AT-SPI) unchanged.
Any failure anywhere in start: tear down the partial session using the
§8 stop machinery, journal with ok=false and the error, print
`{"session": sid, "state": "failed", "error": "<msg>"}` and exit 1.

## 8. stop machinery (used by stop and by failed-start teardown)

For one session, in order:
1. xorg only: write `quit\n` to the fifo if a reader exists (open with
   O_WRONLY|O_NONBLOCK; ignore ENXIO), wait up to 3 s for mkdevs_pid exit.
2. SIGTERM each recorded pid (pid_x, pid_dbus, pid_atspi_launcher,
   pid_registryd, mkdevs_pid if still alive); wait up to 1 s.
3. Marker sweep (Spike A pattern): scan `/proc/<pid>/environ` of every
   process; any process (except sessiond itself) whose env contains BOTH
   `ARK_SESSIOND=<STATE>` and `ARK_SESSION_ID=<sid>` gets SIGTERM; wait 1 s;
   rescan and SIGKILL survivors.
4. xorg only: `sudo -n rm -f /etc/X11/ark-sessiond-<d>.conf`; if no xorg
   sessions remain in the registry afterwards, also
   `sudo -n rm -f /etc/udev/rules.d/49-arkdrive-sessiond.rules` and
   `sudo -n udevadm control --reload-rules`.
5. If the X server pid is dead: remove `/tmp/.X<d>-lock` and
   `/tmp/.X11-unix/X<d>` if present.
6. Remove the registry entry; journal the stop.

## 9. README simplifications list (include verbatim in README.md)

v0 simplifications vs the v2 plan: (a) one-shot CLI instead of the
ark-broker/ark-sessiond daemons, JSON-RPC socket, leases, control page and
rev counter; (b) journal is a plain append-only JSONL file — no hash chain,
no signed checkpoints (plan §5.7 deferred); (c) no systemd units or
cgroups — detached processes tracked by pid + /proc environ marker sweep;
(d) AT-SPI discovery shells out to gdbus; D-Bus and X readiness use
dbus-daemon/xinput subprocesses — the program's own code is stdlib-only,
its runtime tools are the platform's; (e) profiles are bare names, not
signed immutable profile documents referenced by hash; (f) no warm pool —
every start is cold; (g) the xorg profile creates and attaches input
devices but sessiond injects no input (actuation is the worker's job);
(h) single user, no per-slot UIDs.

## 10. Smoke test (run it, show me the output)

From `~/arkdrive/wm`: `./ark-sessiond start --profile xvfb`, then
`./ark-sessiond run --session <sid> -- /bin/true`, `./ark-sessiond list`,
`./ark-sessiond stop --all`, and `cat` the journal. Do NOT test the xorg
profile (it is verified separately). Report the smoke output verbatim.
