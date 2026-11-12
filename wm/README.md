# ark-sessiond v0

A one-shot Python 3 session manager with `xvfb` and `xorg` profiles. The
Python code uses only the standard library. Commands print one JSON object
to stdout; commands launched with `run` send both output streams to stderr.

From `~/arkdrive/wm`:

```sh
./ark-sessiond start --profile xvfb
./ark-sessiond run --session s1 -- /bin/true
./ark-sessiond list
./ark-sessiond stop --session s1
./ark-sessiond stop --all
```

Use the session id returned by `start`. `run` returns the child's exit code;
an unknown session returns exit code 2. Start failures return exit code 1
after cleaning up the partial session.

State lives in `~/.local/state/ark-sessiond`: `sessions.json` is the locked
registry, `journal.jsonl` records start/run/stop operations, and `run/<sid>/`
contains the session home, private runtime directory (0700), and logs.
Sessions have their own display, D-Bus and AT-SPI buses. All session
processes carry `ARK_SESSIOND` and `ARK_SESSION_ID` markers for cleanup.
Display allocation uses the first free number in 91–120.

Runtime tools: `Xvfb`, `dbus-daemon`, `gdbus`,
`/usr/libexec/at-spi-bus-launcher`, and `/usr/libexec/at-spi2-registryd`.
The `xorg` profile additionally uses passwordless `sudo -n`, `python3`,
`udevadm`, `Xorg` with the dummy and evdev drivers, `xinput`, `unshare`,
`mount`, and `/dev/uinput`. It creates a keyboard and pointer per session,
installs the host-exclusion udev rule, starts Xorg in a private mount
namespace, and attaches/enables both devices. Stop removes the session's
Xorg config and removes the udev rule after the last xorg session stops.

The section 10 smoke test uses only `xvfb`: start, run `/bin/true`, list,
stop all, then `cat ~/.local/state/ark-sessiond/journal.jsonl`.

## Simplifications

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

## WM v1 stage WM-B

Sessiond now persists incarnation ids, genealogy, task/result mailboxes and
budget ledgers. `stop --session sN` reaps its subtree, parent gate first and
children reaped first, even with the controller dead. Prefer
`stop --incarnation ID` for automation: `sN` can be reused after stop.

`./ark-wmctl serve` runs the stdlib owner-local controller;
`./ark-wmctl call registry '{}'` reads its registry/caps API. Spawn requires
a ready AGENT parent worker and a delegate capability from
`capability.issue`. Default admission is two sessions; structural caps are
depth three and fan-out four. The controller never acquires a worker lease.

See [the WM-B bench note](../bench/wm2-controller-2026-11-11.md) for wire
shapes, config, recovery, artifact exchange and Raj's outstanding live gates.
Run `PYTHONDONTWRITEBYTECODE=1 python3 tests/run_wm_b_headless.py full` for the
full headless suite.

## WM v1 stage WM-C

`./ark-present --host-display :121` opens the presenter on an explicitly
chosen local human/test host display. `:0` is refused before opening X. The
host needs python-xlib, XRender and the existing system X11/XKB libraries;
no new Python package is required. Session workers still target :91–:120.

Select a surface, then use **Takeover** for human input. **Resume** releases
holds, requires a fresh captured frame, and returns automation admission for
a new driver lease and full settled session observe. Selection alone sends
no native input. Drag a child title to move it, right-drag to resize;
Collapse changes presentation only. Deeper descendants appear as attention
chips; select a chip to re-root. Parent dialogs disable child hit testing.

Human input goes directly to the worker, with generation/layout bindings,
a one-second presenter heartbeat deadline and controller process identity
checks. Lost authority closes the helper gate and releases holds, leaving
the session paused. Recover with another explicit Takeover, then Resume.
Keystroke content never enters the human journal path.

Run `PYTHONDONTWRITEBYTECODE=1 python3 tests/run_wm_c_headless.py full` for
the complete headless suite. See [the WM-C bench note](../bench/wm3-presenter-2026-11-11.md)
for the wire protocol, state machine and Raj's unrun live gates. The next
section documents WM-D promotion.

## WM v1 stage WM-D

`ark-wmctl call promote` accepts `source_incarnation`, a worker `window`
handle, `app_class`, `request_id`, and the source's delegate `capability`.
Optional `reservation` uses the ordinary spawn ledger; `transfer_clipboard`
defaults to false. The reply includes `promotion_id`, and on success the
child incarnation and checkpoint manifest path. Retry with the same request
id and parameters. The RPC is synchronous; a client timeout does not imply
failure or authorize a new request identity.

`app_class` is the exact observed native WM_CLASS: `gnome-text-editor` (a
saved file) or `Thunar` (a directory). Desktop-file ids such as
`org.gnome.TextEditor` are unsupported and return `no_recipe`.
They require an unambiguous path exposed by title/tree and corroborated by
the window title, beneath the source session's private HOME. Detectable
unsaved state is refused before any promotion intent or gate is created.
Unsupported classes return `no_recipe`. These two exports only snapshot
saved filesystem content; neither stops the source app, so failures before
retirement leave that app running without a restart.

The controller gates automated input to the source window/app family,
waits for settled worker sensing, and retains an immutable manifest plus
hashed file/directory snapshot in `STATE/exchange/promotions/ID/`. It uses
normal stage B spawn, starts the destination worker, copies documents into
`CHILD_HOME/Promotions/ID/`, optionally sets clipboard, reopens, verifies the
window and filesystem, then closes only the original source window.
Other source apps and the source session keep running. Human takeover of
the source is unavailable during this transaction. No undo, selection,
login, or unsaved buffer is transferred.

All steps are journaled before execution. Recovery reuses a complete
checkpoint, reconciles uncertain launches/clipboard/close from observation,
and never resends those uncertain actions. An incomplete checkpoint fails
safely. Failed children are reaped through sessiond; budget reservations
follow stage B's existing no-refund policy. Checkpoints remain after either
outcome. Native retire uses `wmctrl`; selected clipboard transfer also
requires `xclip`. New controller/recipe code uses the Python standard
library, with the existing worker/runtime providing desktop sensing.

See [the WM-D bench note](../bench/wm4-promotion-2026-11-11.md) for headless
results and the Debian VM live gates.
