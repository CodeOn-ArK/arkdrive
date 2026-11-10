"""Headless integration tests. Every cua invocation targets our installed stub."""

import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

SOURCE = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


class LaneTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="laneq-test-")
        self.root = Path(self.temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "stub.jsonl"
        stub = self.bin / "cua"
        stub.write_text(
            f"#!{PYTHON}\n"
            "import json, os, sys\n"
            "with open(os.environ['STUB_LOG'], 'a') as f:\n"
            "    f.write(json.dumps({'argv': sys.argv[1:], 'pgid': os.getpgid(0)}) + '\\n')\n"
            "print(json.dumps({'ok': True, 'stub': True, 'argv': sys.argv[1:]}))\n"
            "sys.exit(int(os.environ.get('STUB_EXIT', '0')))\n"
        )
        stub.chmod(0o755)
        self.original_bytes = stub.read_bytes()
        self.env = dict(os.environ)
        for key in list(self.env):
            if key.startswith("LANEQ_"):
                del self.env[key]
        self.env.update(LANEQ_HOME=str(self.root / "state"), STUB_LOG=str(self.log),
                        LANEQ_CUA_REAL=str(self.bin / "cua.real"), LANEQ_WAIT_S="10")
        self.processes = []
        self.install_result = self.install()

    def tearDown(self):
        for process, group in reversed(self.processes):
            if process.poll() is None:
                try:
                    if group:
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                except ProcessLookupError:
                    pass
            process.communicate(timeout=5)
        self.temporary.cleanup()

    def install(self):
        result = subprocess.run([PYTHON, str(SOURCE / "install.py"), "--bin-dir", str(self.bin)],
                                env=self.env, capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def spawn(self, arguments, group=False, env=None):
        process = subprocess.Popen(arguments, env=env or self.env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=group)
        self.processes.append((process, group))
        return process

    def leader(self):
        return self.spawn(["sleep", "120"], group=True)

    def cli(self, *arguments, code=0):
        result = subprocess.run([str(self.bin / "laneq"), *map(str, arguments)], env=self.env,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, code, result.stderr + result.stdout)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        return json.loads(result.stdout)

    def acquire(self, leader, wait=0, **kwargs):
        return self.cli("acquire", "--pgid", leader.pid, "--leader", leader.pid,
                        "--wait", wait, **kwargs)

    def waiting(self, leader):
        return self.spawn([str(self.bin / "laneq"), "acquire", "--pgid", str(leader.pid),
                           "--leader", str(leader.pid), "--wait", "12"])

    def eventually(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.04)
        self.fail("condition did not become true before timeout")

    def status(self):
        return self.cli("status")

    def release(self, leader):
        return self.cli("release", "--pgid", leader.pid)

    def result(self, process, code=0):
        stdout, stderr = process.communicate(timeout=15)
        self.assertEqual(process.returncode, code, stderr + stdout)
        self.assertEqual(len(stdout.splitlines()), 1)
        return json.loads(stdout)

    def events(self):
        return [json.loads(line) for line in (self.root / "state/events.jsonl").read_text().splitlines()]

    def test_status_shape(self):
        self.assertEqual(self.status(), {"holder": None, "queue": []})
        leader = self.leader()
        self.acquire(leader)
        status = self.status()
        self.assertEqual(set(status["holder"]),
                         {"ticket", "owner", "pgid", "leader_pid", "claimed_at", "last_seen"})
        self.assertEqual(status["queue"], [])

    def test_fast_path_refresh_and_idempotent_release(self):
        leader = self.leader()
        self.assertEqual(self.acquire(leader), {"ok": True, "state": "claimed"})
        before = self.status()["holder"]
        self.assertEqual(self.acquire(leader), {"ok": True, "state": "held"})
        after = self.status()["holder"]
        self.assertEqual(before["ticket"], after["ticket"])
        self.assertGreater(after["last_seen"], before["last_seen"])
        self.release(leader)
        self.release(leader)
        self.assertEqual(self.status(), {"holder": None, "queue": []})
        self.assertEqual([e["event"] for e in self.events()], ["enqueued", "claimed", "released"])

    def test_fifo_two_waiters_block_then_claim_in_order(self):
        first, second, third = self.leader(), self.leader(), self.leader()
        self.acquire(first)
        second_acquire = self.waiting(second)
        self.eventually(lambda: len(self.status()["queue"]) == 1)
        third_acquire = self.waiting(third)
        self.eventually(lambda: len(self.status()["queue"]) == 2)
        self.assertEqual([t["pgid"] for t in self.status()["queue"]], [second.pid, third.pid])
        self.assertIsNone(second_acquire.poll())
        self.assertIsNone(third_acquire.poll())
        self.release(first)
        self.assertEqual(self.result(second_acquire)["state"], "claimed")
        self.assertIsNone(third_acquire.poll())
        self.release(second)
        self.assertEqual(self.result(third_acquire)["state"], "claimed")
        claimed = [e["pgid"] for e in self.events() if e["event"] == "claimed"]
        self.assertEqual(claimed, [first.pid, second.pid, third.pid])

    def test_dead_holder_auto_releases(self):
        first, second = self.leader(), self.leader()
        self.acquire(first)
        waiter = self.waiting(second)
        self.eventually(lambda: len(self.status()["queue"]) == 1)
        first.kill()
        first.wait()
        self.assertEqual(self.result(waiter)["state"], "claimed")
        self.assertEqual(self.status()["holder"]["pgid"], second.pid)
        self.assertIn("auto_released_dead", [e["event"] for e in self.events()])

    def test_live_leader_never_expires_from_old_last_seen(self):
        leader, second = self.leader(), self.leader()
        self.acquire(leader)
        path = self.root / "state/holder.json"
        record = json.loads(path.read_text())
        record["last_seen"] = 0
        path.write_text(json.dumps(record))
        result = self.acquire(second, code=3)
        self.assertEqual(result["holder"]["pgid"], leader.pid)

    def test_timeout_shape_ticket_dedup_and_audit(self):
        first, second = self.leader(), self.leader()
        self.acquire(first)
        started = time.monotonic()
        result = self.acquire(second, wait=0.2, code=3)
        self.assertGreaterEqual(time.monotonic() - started, 0.2)
        self.assertEqual(set(result), {"ok", "error", "holder", "position"})
        self.assertEqual((result["ok"], result["error"], result["position"]), (False, "lane_busy", 1))
        self.acquire(second, code=3)
        self.assertEqual(len(self.status()["queue"]), 1)
        for event in self.events():
            self.assertEqual(set(event), {"event", "ts", "ticket", "owner", "pgid"})
        self.assertEqual(self.events()[-1]["event"], "lane_busy_timeout")

    def test_release_can_cancel_pending_ticket(self):
        first, second = self.leader(), self.leader()
        self.acquire(first)
        self.acquire(second, code=3)
        self.release(second)
        self.assertEqual(self.status()["queue"], [])
        self.assertEqual(self.status()["holder"]["pgid"], first.pid)

    def test_dead_pending_ticket_does_not_block_queue(self):
        first, dead, next_leader = self.leader(), self.leader(), self.leader()
        self.acquire(first)
        self.acquire(dead, code=3)
        dead.kill()
        dead.wait()
        self.release(first)
        self.assertEqual(self.acquire(next_leader)["state"], "claimed")
        self.assertEqual(self.status()["queue"], [])

    def work(self, command, *options, env=None):
        return self.spawn([str(self.bin / "laneq"), "run", *options, "--", *command],
                          group=True, env=env)

    def test_priority_jumps_queue_without_preempting_holder(self):
        holder = self.leader()
        self.acquire(holder)
        low = self.work(["true"], "--owner", "low", "--priority", "0")
        self.eventually(lambda: len(self.status()["queue"]) == 1)
        high = self.work(["true"], "--owner", "high", "--priority", "5")
        self.eventually(lambda: len(self.status()["queue"]) == 2)
        self.assertEqual([t["owner"] for t in self.status()["queue"]], ["high", "low"])
        self.assertEqual(self.status()["holder"]["pgid"], holder.pid)
        self.release(holder)
        for process in (high, low):
            stdout, stderr = process.communicate(timeout=12)
            self.assertEqual((process.returncode, stdout, stderr), (0, "", ""))
        self.assertEqual([e["owner"] for e in self.events() if e["event"] == "claimed"][1:], ["high", "low"])

    def test_run_serializes_two_two_second_sleeps(self):
        markers = self.root / "markers"
        script = ("import os,sys,time; "
                  "f=open(sys.argv[1],'a',buffering=1); "
                  "f.write(sys.argv[2]+' start '+str(time.monotonic())+' '+str(os.getpgid(0))+'\\n'); "
                  "time.sleep(2); f.write(sys.argv[2]+' end '+str(time.monotonic())+'\\n')")
        started = time.monotonic()
        first = self.work([PYTHON, "-c", script, str(markers), "one"])
        self.eventually(lambda: markers.exists())
        second = self.work([PYTHON, "-c", script, str(markers), "two"])
        for process in (first, second):
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual((process.returncode, stdout, stderr), (0, "", ""))
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 4.0)
        lines = [line.split() for line in markers.read_text().splitlines()]
        self.assertEqual([line[:2] for line in lines],
                         [["one", "start"], ["one", "end"], ["two", "start"], ["two", "end"]])
        self.assertEqual(int(lines[0][3]), first.pid)
        self.assertEqual(int(lines[2][3]), second.pid)
        self.assertGreaterEqual(float(lines[2][2]), float(lines[1][2]))
        print(f"run serialization evidence: {elapsed:.3f}s; " + "; ".join(markers.read_text().splitlines()),
              file=sys.stderr)
        self.assertEqual(self.status(), {"holder": None, "queue": []})

    def test_run_preserves_child_exit_code(self):
        process = self.work([PYTHON, "-c", "import sys;sys.exit(17)"])
        stdout, stderr = process.communicate(timeout=8)
        self.assertEqual((process.returncode, stdout, stderr), (17, "", ""))
        self.assertIsNone(self.status()["holder"])

    def test_run_signal_releases_and_stops_child(self):
        for signum in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=signum):
                child_file = self.root / f"child-{signum}"
                script = "import os,pathlib,sys,time;pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(120)"
                process = self.work([PYTHON, "-c", script, str(child_file)])
                self.eventually(child_file.exists)
                child_pid = int(child_file.read_text())
                process.send_signal(signum)
                process.communicate(timeout=8)
                self.assertEqual(process.returncode, 128 + signum)
                self.assertEqual(self.status(), {"holder": None, "queue": []})
                with self.assertRaises(ProcessLookupError):
                    os.kill(child_pid, 0)

    def test_run_signal_while_queued_cancels_ticket(self):
        holder = self.leader()
        self.acquire(holder)
        process = self.work(["true"])
        self.eventually(lambda: len(self.status()["queue"]) == 1)
        process.terminate()
        process.communicate(timeout=8)
        self.assertEqual(process.returncode, 143)
        self.assertEqual(self.status()["queue"], [])
        self.assertEqual(self.status()["holder"]["pgid"], holder.pid)

    def test_run_terminates_child_that_ignores_term_before_releasing(self):
        child_file = self.root / "ignoring-child"
        script = ("import os,pathlib,signal,sys,time;"
                  "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                  "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(120)")
        process = self.work([PYTHON, "-c", script, str(child_file)])
        self.eventually(child_file.exists)
        child_pid = int(child_file.read_text())
        process.terminate()
        time.sleep(0.1)
        self.assertEqual(self.status()["holder"]["pgid"], process.pid)
        process.communicate(timeout=8)
        self.assertEqual(process.returncode, 143)
        self.assertEqual(self.status(), {"holder": None, "queue": []})
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)

    def test_clear_force_audit_and_empty_state(self):
        first, second = self.leader(), self.leader()
        self.acquire(first)
        self.acquire(second, code=3)
        self.cli("clear", "--force")
        self.assertEqual(self.status(), {"holder": None, "queue": []})
        self.assertEqual(len([e for e in self.events() if e["event"] == "force_released"]), 2)

    def test_owner_explicit_environment_and_proc_derivation(self):
        first, second = self.leader(), self.leader()
        self.cli("acquire", "--pgid", first.pid, "--leader", first.pid, "--owner", "explicit")
        self.assertEqual(self.status()["holder"]["owner"], "explicit")
        self.release(first)
        self.env["LANEQ_OWNER"] = "environment"
        self.acquire(second)
        self.assertEqual(self.status()["holder"]["owner"], "environment")
        self.release(second)
        del self.env["LANEQ_OWNER"]
        third = self.spawn([PYTHON, "-c", "import time;time.sleep(120)", "handshake"], group=True)
        # Wait until exec has replaced the initial Python subprocess trampoline.
        self.eventually(lambda: b"handshake" in Path(f"/proc/{third.pid}/cmdline").read_bytes())
        self.acquire(third)
        self.assertEqual(self.status()["holder"]["owner"], "handshake")

    def shim(self, *arguments, env=None):
        return self.spawn([str(self.bin / "cua"), *arguments], group=True, env=env)

    def test_shim_write_waits_for_other_group(self):
        holder = self.leader()
        self.acquire(holder)
        write = self.shim("click", "10", "20")
        self.eventually(lambda: len(self.status()["queue"]) == 1)
        self.assertIsNone(write.poll())
        self.assertFalse(self.log.exists())
        self.release(holder)
        result = self.result(write)
        self.assertEqual(result, {"ok": True, "stub": True, "argv": ["click", "10", "20"]})
        self.assertEqual(self.status()["holder"]["pgid"], write.pid)

    def test_shim_read_only_calls_bypass_live_holder(self):
        holder = self.leader()
        self.acquire(holder)
        for arguments in (("shot",), ("win", "list"), ("tab", "list"), ("clip", "get")):
            with self.subTest(arguments=arguments):
                started = time.monotonic()
                result = self.result(self.shim(*arguments))
                self.assertLess(time.monotonic() - started, 1.5)
                self.assertEqual(result["argv"], list(arguments))
                self.assertEqual(self.status()["holder"]["pgid"], holder.pid)
                self.assertEqual(self.status()["queue"], [])

    def test_shim_off_bypasses_all_calls_including_empty_value(self):
        holder = self.leader()
        self.acquire(holder)
        env = dict(self.env, LANEQ_OFF="")
        result = self.result(self.shim("paste", "verbatim $() `ticks`\n雪", env=env))
        self.assertEqual(result["argv"], ["paste", "verbatim $() `ticks`\n雪"])
        self.assertEqual(self.status()["queue"], [])

    def test_shim_timeout_is_single_json_and_does_not_invoke_stub(self):
        holder = self.leader()
        self.acquire(holder)
        env = dict(self.env, LANEQ_WAIT_S="0.15")
        result = self.result(self.shim("key", "a", env=env), code=3)
        self.assertEqual((result["error"], result["position"]), ("lane_busy", 1))
        self.assertFalse(self.log.exists())

    def test_shim_unknown_and_incomplete_arguments_are_writes(self):
        holder = self.leader()
        self.acquire(holder)
        env = dict(self.env, LANEQ_WAIT_S="0")
        for arguments in ((), ("win",), ("tab",), ("clip",), ("unknown",), ("--bad", "shot")):
            with self.subTest(arguments=arguments):
                result = self.result(self.shim(*arguments, env=env), code=3)
                self.assertEqual(result["error"], "lane_busy")
        self.assertFalse(self.log.exists())

    def test_shim_preserves_real_exit_code_and_stdin(self):
        # Exit code comes from exec, rather than the acquisition subprocess.
        env = dict(self.env, STUB_EXIT="19")
        self.assertTrue(self.result(self.shim("click", "1", "2", env=env), code=19)["stub"])
        stdin_stub = self.root / "stdin-stub"
        stdin_stub.write_text(f"#!{PYTHON}\nimport json,sys\nprint(json.dumps({{'input':sys.stdin.read()}}))\n")
        stdin_stub.chmod(0o755)
        env = dict(self.env, LANEQ_CUA_REAL=str(stdin_stub))
        process = subprocess.run([str(self.bin / "cua"), "paste"], input="literal\n雪\n", text=True,
                                 env=env, capture_output=True, start_new_session=True, timeout=8)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout), {"input": "literal\n雪\n"})

    def test_installer_byte_identical_backups_and_idempotence(self):
        checksum = hashlib.sha256(self.original_bytes).hexdigest()
        self.assertEqual(self.install_result, {"ok": True, "original_cua_sha256": checksum,
                                              "cua_real_sha256": checksum, "cua_backup_sha256": checksum})
        for name in ("cua.real", "cua.bak-laneq"):
            self.assertEqual((self.bin / name).read_bytes(), self.original_bytes)
        self.assertEqual(self.install(), self.install_result)
        self.assertEqual((self.bin / "cua").read_bytes(), (SOURCE / "cua_shim.py").read_bytes())

    def test_installer_refuses_conflicting_backup(self):
        (self.bin / "cua.bak-laneq").write_bytes(b"conflict")
        before = (self.bin / "cua").read_bytes()
        result = subprocess.run([PYTHON, str(SOURCE / "install.py"), "--bin-dir", str(self.bin)],
                                env=self.env, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.bin / "cua").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
