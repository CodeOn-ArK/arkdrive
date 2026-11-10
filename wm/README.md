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
