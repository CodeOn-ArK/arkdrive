#!/usr/bin/env python3
"""FIFO ownership of the shared :0 desktop, for the lifetime of a work."""

import argparse
import contextlib
import datetime
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import uuid

POLL_SECONDS = 1.5
OWNER_TOKENS = ("handshake", "brightspace", "reddit", "codex", "qwen", "gwen", "claude")


def now():
    return time.time()


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def derive_owner(leader, pgid):
    try:
        command = Path(f"/proc/{leader}/cmdline").read_bytes().lower().split(b"\0")
        for argument in command:
            for token in OWNER_TOKENS:
                if token.encode() in argument:
                    return token
    except OSError:
        pass
    return f"pgid-{pgid}"


class Lane:
    def __init__(self):
        self.home = Path(os.environ.get("LANEQ_HOME", "~/.local/state/laneq")).expanduser()
        self.queue_dir = self.home / "queue"
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        self.holder_path = self.home / "holder.json"

    @contextlib.contextmanager
    def locked(self):
        with (self.home / "claim.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def write_json(self, path, value):
        fd, temporary = tempfile.mkstemp(prefix=".laneq-", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(value, stream, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def holder(self):
        try:
            return json.loads(self.holder_path.read_text())
        except FileNotFoundError:
            return None

    def queue(self):
        tickets = [json.loads(path.read_text()) for path in self.queue_dir.glob("*.json")]
        return sorted(tickets, key=lambda t: (-t.get("priority", 0), t["created_at"], t["ticket"]))

    def remove_ticket(self, ticket):
        (self.queue_dir / (ticket["ticket"] + ".json")).unlink(missing_ok=True)

    def event(self, name, record):
        entry = {
            "event": name,
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "ticket": record["ticket"],
            "owner": record["owner"],
            "pgid": record["pgid"],
        }
        with (self.home / "events.jsonl").open("a") as stream:
            stream.write(json.dumps(entry, separators=(",", ":")) + "\n")

    def reap(self):
        """Called only under claim.lock; dead pending works cannot block the head."""
        holder = self.holder()
        if holder and not alive(holder["leader_pid"]):
            self.holder_path.unlink(missing_ok=True)
            self.remove_ticket(holder)
            self.event("auto_released_dead", holder)
        for ticket in self.queue():
            if not alive(ticket["leader_pid"]):
                self.remove_ticket(ticket)
                self.event("auto_released_dead", ticket)

    def acquire(self, pgid, leader, owner=None, wait=1200, priority=0):
        deadline = time.monotonic() + wait
        while True:
            with self.locked():
                self.reap()
                holder = self.holder()
                if holder and holder["pgid"] == pgid:
                    holder["last_seen"] = now()
                    self.write_json(self.holder_path, holder)
                    return {"ok": True, "state": "held"}, 0
                queue = self.queue()
                ticket = next((t for t in queue if t["pgid"] == pgid), None)
                if ticket is None:
                    ticket = {
                        "ticket": uuid.uuid4().hex,
                        "owner": owner or os.environ.get("LANEQ_OWNER") or derive_owner(leader, pgid),
                        "pgid": pgid,
                        "leader_pid": leader,
                        "priority": priority,
                        "created_at": time.time_ns(),
                    }
                    self.write_json(self.queue_dir / (ticket["ticket"] + ".json"), ticket)
                    self.event("enqueued", ticket)
                    queue = self.queue()
                # The slot and head are both rechecked while holding the same flock.
                if holder is None and queue[0]["ticket"] == ticket["ticket"]:
                    claimed = now()
                    holder = {key: ticket[key] for key in ("ticket", "owner", "pgid", "leader_pid")}
                    holder.update(claimed_at=claimed, last_seen=claimed)
                    self.write_json(self.holder_path, holder)
                    # Keep the holder's ticket until release; status.queue is pending only.
                    self.event("claimed", holder)
                    return {"ok": True, "state": "claimed"}, 0
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    pending = [t for t in queue if not holder or t["ticket"] != holder["ticket"]]
                    position = next(i + 1 for i, t in enumerate(pending) if t["ticket"] == ticket["ticket"])
                    self.event("lane_busy_timeout", ticket)
                    return {"ok": False, "error": "lane_busy", "holder": holder, "position": position}, 3
            time.sleep(min(POLL_SECONDS, remaining))

    def release(self, pgid):
        with self.locked():
            holder = self.holder()
            if holder and holder["pgid"] == pgid:
                self.holder_path.unlink(missing_ok=True)
                self.remove_ticket(holder)
                self.event("released", holder)
            for ticket in self.queue():
                if ticket["pgid"] == pgid:
                    self.remove_ticket(ticket)
                    self.event("released", ticket)
        return {"ok": True, "state": "released"}

    def status(self):
        with self.locked():
            holder = self.holder()
            queue = [t for t in self.queue() if not holder or t["ticket"] != holder["ticket"]]
            return {"holder": holder, "queue": queue}

    def clear(self):
        with self.locked():
            holder = self.holder()
            records = {t["ticket"]: t for t in self.queue()}
            if holder:
                records[holder["ticket"]] = holder
            self.holder_path.unlink(missing_ok=True)
            for record in records.values():
                self.remove_ticket(record)
                self.event("force_released", record)
        return {"ok": True, "state": "cleared"}


class RunInterrupted(Exception):
    def __init__(self, signum):
        self.signum = signum


def run_work(lane, args):
    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        raise ValueError("run requires a command after --")
    pgid = os.getpgid(0)
    child = None
    interrupted = None
    launching = False

    def on_signal(signum, frame):
        nonlocal interrupted
        interrupted = signum
        # Popen can already have forked before it returns the child object.
        # Defer interruption across that window so cleanup always knows its PID.
        if not launching:
            raise RunInterrupted(signum)

    previous = {s: signal.signal(s, on_signal) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        result, code = lane.acquire(pgid, pgid, args.owner, args.wait, args.priority)
        if code:
            print(json.dumps(result), flush=True)
            return code
        # No new session/group: all descendants inherit this work's ownership.
        launching = True
        try:
            child = subprocess.Popen(command)
        finally:
            launching = False
        if interrupted:
            raise RunInterrupted(interrupted)
        code = child.wait()
        return code if code >= 0 else 128 - code
    except RunInterrupted as exc:
        for signum in previous:
            signal.signal(signum, signal.SIG_IGN)
        if child is not None and child.poll() is None:
            child.send_signal(exc.signum)
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        return 128 + exc.signum
    finally:
        for signum in previous:
            signal.signal(signum, signal.SIG_IGN)
        lane.release(pgid)
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def seconds(value):
    number = float(value)
    if not 0 <= number < float("inf"):
        raise argparse.ArgumentTypeError("must be finite and nonnegative")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    acquire = commands.add_parser("acquire")
    acquire.add_argument("--pgid", type=positive_int, required=True)
    acquire.add_argument("--leader", type=positive_int, required=True)
    acquire.add_argument("--owner")
    acquire.add_argument("--wait", type=seconds, default=1200)
    release = commands.add_parser("release")
    release.add_argument("--pgid", type=positive_int, required=True)
    commands.add_parser("status")
    run = commands.add_parser("run")
    run.add_argument("--owner")
    run.add_argument("--priority", type=int, default=0)
    run.add_argument("--wait", type=seconds, default=1200)
    run.add_argument("command", nargs=argparse.REMAINDER)
    clear = commands.add_parser("clear")
    clear.add_argument("--force", action="store_true", required=True)
    args = parser.parse_args()
    try:
        lane = Lane()
        if args.action == "run":
            return run_work(lane, args)
        if args.action == "acquire":
            result, code = lane.acquire(args.pgid, args.leader, args.owner, args.wait)
        else:
            result = getattr(lane, args.action)(args.pgid) if args.action == "release" else getattr(lane, args.action)()
            code = 0
        print(json.dumps(result), flush=True)
        return code
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), flush=True)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
