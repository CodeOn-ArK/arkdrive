# laneq

`laneq` serializes whole works on ArK2's shared `:0` desktop. The `cua`
shim acquires ownership on the first write and keeps it across subsequent
calls and long LLM pauses. Pending works queue by priority descending, then
creation time ascending (FIFO at the same priority). Priority defaults to 0;
it never preempts the current holder.

Only the group leader's existence determines whether a work is alive:
`os.kill(leader_pid, 0)`. There is no heartbeat expiry. An acquire refreshes
`last_seen` for the existing holder. The next acquire removes a dead holder
and dead pending tickets before checking the queue head. A zombie PID counts
as existing until its parent reaps it, as required by this liveness check.

## Install

Run on ArK2 from an environment allowed to write `~/bin`:

```sh
python3 ~/arkdrive/tools/laneq/install.py
sha256sum ~/bin/cua.real ~/bin/cua.bak-laneq
```

The installer moves the original `~/bin/cua` to `~/bin/cua.real`, keeps a
byte-identical copy at `~/bin/cua.bak-laneq`, verifies both SHA-256 hashes,
and installs `laneq.py` as `~/bin/laneq` and `cua_shim.py` as `~/bin/cua`.
It refuses conflicting existing backups and supports rerunning against
the installed shim. It prints the original and both backup hashes as JSON.
`--bin-dir PATH` supports an isolated test installation.

**This build session could not install into `~/bin`: its managed filesystem
policy permits writes only in the workspace and `/tmp`. Installation and
real-desktop verification remain pending.**

For recovery, `LANEQ_OFF=1 cua ...` bypasses coordination for that invocation.
To restore the original launcher permanently, copy `~/bin/cua.real` over
`~/bin/cua`. Keep both backups.

## Use

Legacy drivers keep invoking `~/bin/cua` as before. Its process group is the
work identity; the process with PID equal to the PGID is the group leader.
The first write automatically registers/acquires the lane. Ownership lasts
until an explicit release or until the leader disappears and a later acquire
cleans it up. Exiting a single `cua` call does not release a live driver.

```sh
laneq status
laneq acquire --pgid 1234 --leader 1234 --owner handshake --wait 1200
laneq release --pgid 1234
laneq run --owner research --priority 0 --wait 1200 -- python3 desktop_job.py
laneq clear --force
```

`run` uses its own existing process group and launches its child in that same
group. It releases on child exit and returns the child's exit code (128 plus
signal number for signal termination). SIGTERM/SIGINT to `run` cancels a
pending ticket or forwards the signal to its child, waits for exit, and then
releases. A direct child ignoring the signal is killed after 3 seconds.
Child stdout/stdin/stderr pass through; successful `run` adds no stdout.

**Independent works must have independent process groups.** Interactive
shell job control normally provides them. When launching multiple works
from a script or another environment that shares a group, use `setsid`:

```sh
setsid laneq run --owner job-a -- python3 desktop_job.py
```

Drivers in the same process group deliberately share one ownership record.
For direct `acquire`, pass the actual group and its leader; `run` and the
shim determine these automatically. Avoid detached desktop descendants that
outlive the command supplied to `run`.

### Read/write split

These exact command prefixes pass directly to real cua without queuing:

| Call | Access |
| --- | --- |
| `shot` | Read only |
| `win list` | Read only |
| `tab list` | Read only |
| `clip get` | Read only |
| Everything else, including empty/unknown/incomplete commands | Write |

Observers can take screenshots while a work holds the lane. A write runs
`laneq acquire` first, captures its output, then uses `os.execvpe` for real
cua on success. Arguments, stdin, stdout, stderr, real exit codes and signals
pass through. Acquisition adds no successful stdout. On timeout the shim
returns the acquisition's single JSON object and exit code 3 without invoking
real cua:

```json
{"ok": false, "error": "lane_busy", "holder": {"ticket": "...", "owner": "handshake", "pgid": 1234, "leader_pid": 1234, "claimed_at": 0, "last_seen": 0}, "position": 1}
```

Queue positions are 1-based among pending tickets; they exclude the holder.
A timed-out direct acquire leaves its ticket queued, so retries retain their
place. `release --pgid N` also cancels pending tickets for N and is idempotent.
A timed-out `run` cancels its ticket as part of its exit cleanup.

### Non-cua works and scope

**xdotool and any other desktop work that bypasses cua must be wrapped in
`laneq run -- command ...`.** Coordination works only when all writers
participate. Use `LANEQ_OFF` only as an intentional escape hatch; any set
value, including the empty string, bypasses the shim.

Portal lanes (`~/portal-lanes`, displays `:11`–`:13`) and `wm/` ark-sessiond
sessions are isolated and exempt by design. Their tools and pipeline scripts
are outside this change.

## State, environment, and audit

`LANEQ_HOME` defaults to `~/.local/state/laneq`. It contains:

- `queue/<ticket>.json`: group, leader, owner, priority and `created_at`
  (Unix nanoseconds). The holder's ticket stays on disk until release.
- `holder.json`: `ticket`, `owner`, `pgid`, `leader_pid`, `claimed_at`,
  `last_seen` (the last two are Unix seconds).
- `claim.lock`: exclusive `flock` protecting all queue/holder mutations and
  consistent status reads. Claims recheck both slot and queue head inside
  the lock and write holder JSON with a temporary file plus `os.replace`.
- `events.jsonl`: append-only `enqueued`, `claimed`, `released`,
  `auto_released_dead`, `lane_busy_timeout`, `force_released` records. Each
  has `event`, UTC `ts`, `ticket`, `owner`, `pgid`. Force-clear audits each
  removed work. This file is retained by clear.

`status` prints `{"holder": object_or_null, "queue": [pending_tickets]}`;
it does not reap state. Acquisition polls at intervals up to 1.5 seconds.
Explicit `--owner` takes precedence over `LANEQ_OWNER`, then owner derivation
from `/proc/<leader>/cmdline` (first recognized `handshake`, `brightspace`,
`reddit`, `codex`, `qwen`, `gwen`, `claude` token); fallback is `pgid-N`.

The shim locates `laneq` alongside itself and uses `LANEQ_CUA_REAL` or
`~/bin/cua.real` for real cua. `LANEQ_WAIT_S` sets its acquisition timeout;
the default is 1200 seconds. `run` and direct `acquire` also default to 1200
seconds and accept `--wait 0` for an immediate attempt.

`clear --force` is manual recovery: it removes the holder and all tickets;
it does not stop the processes themselves. Stop stale writers before using
it. Still-running acquisition requests can enqueue again on their next poll.

## Headless tests

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tools/laneq/tests -v
```

Every test uses an isolated temporary state directory and a temporary bin
directory installed from a stub cua. Shim tests explicitly set
`LANEQ_CUA_REAL` to that stub. Group leaders use `start_new_session=True`
(setsid). No test invokes the actual `~/bin/cua`, X11, or desktop input.
The benchmark note records results and outstanding live gates.
