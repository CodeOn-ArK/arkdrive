#!/usr/bin/python3
"""Headless W1 contract checks. No display or input connection is opened.

Run: PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 wm/tests/test_worker_w1.py
Live gates, including socket credential enforcement, remain independent.
"""

import array
import importlib.machinery
import importlib.util
import io
import json
import mmap
import os
from pathlib import Path
import selectors
import signal
import struct
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

sys.dont_write_bytecode = True
WM = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("ark_worker_w1", str(WM / "ark-worker"))
spec = importlib.util.spec_from_loader(loader.name, loader)
w = importlib.util.module_from_spec(spec)
loader.exec_module(w)
GEN = "0123456789abcdef" * 2
OTHER = "fedcba9876543210" * 2


def window(title="Main", mapped=True, geometry=None, **extras):
    return {"family": "pid:42", "app": "Fixture", "title": title,
            "class": "Fixture", "instance": "fixture", "xid": 10,
            "geometry": geometry or {"x": 3, "y": 4, "w": 100, "h": 80},
            "mapped": mapped, "type": "normal", "transient_for": None,
            "leader": None, "is_modal": False, **extras}


class SandboxCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".worker-w1-", dir=WM.parent)
        self.path = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)


class ControlTests(SandboxCase):
    def test_layout_round_trip(self):
        blob = w.encode_control(GEN, 7, w.STATES["human"], 1, bytes(range(16)))
        self.assertEqual(len(blob), 64)
        self.assertEqual(blob[:8], b"ARKC\x01\0\0\0")
        self.assertEqual(struct.unpack_from("<Q", blob, 24)[0], 7)
        value = w.decode_control(blob, GEN)
        self.assertEqual((value["epoch"], value["state"], value["flags"]), (7, 2, 1))
        self.assertEqual(value["lease_hash"], bytes(range(16)).hex())

    def test_malformed_and_stale_close(self):
        for blob in (b"", bytes(64), w.encode_control(GEN, 0), w.encode_control(GEN, 1, 9)):
            with self.assertRaises(ValueError):
                w.decode_control(blob, GEN)
        with self.assertRaisesRegex(ValueError, "stale_generation"):
            w.decode_control(w.encode_control(OTHER), GEN)

    def test_torn_read_order_on_real_shared_mapping(self):
        path = self.path / "control"
        w.initialise_control(path, GEN)
        snapshots = []
        with path.open("r+b") as stream, mmap.mmap(stream.fileno(), 64) as page:
            def store(page, offset, value, bits):
                w.control_store(page, offset, value, bits)
                snapshots.append((offset, w.decode_control(page[:], GEN)))
            w.transition_control(page, w.STATES["paused"], 2, store)
            self.assertEqual([p for p, _ in snapshots], [32, 24])
            self.assertEqual([(s["state"], s["epoch"]) for _, s in snapshots], [(3, 1), (3, 2)])
            snapshots.clear()
            w.transition_control(page, w.STATES["agent"], 3, store)
            self.assertEqual([p for p, _ in snapshots], [24, 32])
            self.assertEqual([(s["state"], s["epoch"]) for _, s in snapshots], [(3, 3), (0, 3)])
            # At neither intermediate stage can an old-epoch AGENT batch run.
            self.assertFalse(any(s["state"] == 0 and s["epoch"] == 2 for _, s in snapshots))

    def test_restart_preserves_pause_and_rebinds_generation(self):
        path = self.path / "control"
        path.write_bytes(w.encode_control(GEN, 9, w.STATES["human"]))
        w.initialise_control(path, OTHER)
        value = w.decode_control(path.read_bytes(), OTHER)
        self.assertEqual((value["state"], value["epoch"]), (2, 10))
        self.assertEqual(path.stat().st_size, 64)


class JournalTests(SandboxCase):
    def test_append_flush_fsync_and_honest_action_scan(self):
        path = self.path / "acts.jsonl"
        journal = w.Journal(path, "s1", GEN)
        self.addCleanup(journal.close)
        real_fsync = os.fsync
        with patch.object(w.os, "fsync", wraps=real_fsync) as fsync:
            journal.append("worker_start", title="Unicode α")
            journal.append("attach", client="codex")
            self.assertEqual(fsync.call_count, 2)
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual([r["type"] for r in rows], ["worker_start", "attach"])
        self.assertTrue(all(r["ts"].endswith("Z") for r in rows))
        self.assertEqual(w.Journal.action(path, "x")["status"], "not_found")
        journal.append("intent", action_id="x")
        self.assertEqual(w.Journal.action(path, "x")["receipt"]["dispatch_status"], "unknown")
        journal.append("receipt", action_id="x", receipt={"reason": "example"})
        self.assertEqual(w.Journal.action(path, "x")["receipt"], {"reason": "example"})
        with self.assertRaisesRegex(w.RPCError, "history_expired"):
            w.Journal.action(self.path / "missing", "x")

    def test_fsync_failure_propagates(self):
        journal = w.Journal(self.path / "acts", "s1", GEN)
        self.addCleanup(journal.close)
        with patch.object(w.os, "fsync", side_effect=OSError("disk failure")):
            with self.assertRaisesRegex(OSError, "disk failure"):
                journal.append("done")

    def test_corrupt_history_never_reports_not_found(self):
        path = self.path / "broken.jsonl"
        path.write_text('{"type":"intent",')
        with self.assertRaisesRegex(w.RPCError, "journal_unavailable"):
            w.Journal.action(path, "missing")


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.cache = w.StateCache("s1", GEN)

    def test_map_unmap_focus_geometry_and_destroy_revisions(self):
        c = self.cache
        c.upsert(10, window())
        c.commit(True)
        handle = c.handle(10)
        self.assertEqual(handle, "s1/g01234567/w1")
        self.assertEqual(c.rev, 2)
        self.assertTrue(c.observe({"kind": "session"}, 2)["unchanged"])
        c.set("focus", handle)
        c.commit(True)
        self.assertEqual(c.facets["focus"], 3)
        self.assertFalse(c.observe({"kind": "session"}, 2)["unchanged"])
        c.upsert(10, window(geometry={"x": 20, "y": 30, "w": 101, "h": 81}))
        c.commit(True)
        self.assertEqual(c.facets["geometry[" + handle + "]"], 4)
        c.upsert(10, window(mapped=False))
        c.commit(True)
        self.assertFalse(c.observe({"kind": "session"}, 4)["state"]["windows"][0]["mapped"])
        c.remove(10)
        c.commit(True)
        self.assertEqual(c.observe({"kind": "session"}, 5)["state"]["removed"], [handle])
        c.upsert(10, window())
        c.commit(True)
        self.assertNotEqual(c.handle(10), handle)

    def test_unrelated_event_rev_does_not_change_facets(self):
        self.cache.commit(True)
        self.assertEqual(self.cache.rev, 2)
        self.assertTrue(self.cache.observe({"kind": "session"}, 1)["unchanged"])

    def test_dirty_unknown_future_and_generation(self):
        c = self.cache
        self.assertIsNone(c.observe({"kind": "session"}, 1, False)["unchanged"])
        for since in (None, 0, 99):
            result = c.observe({"kind": "session"}, since)
            self.assertFalse(result["unchanged"])
            self.assertTrue(result["full"])
        with self.assertRaisesRegex(w.RPCError, "stale_generation"):
            c.selected({"kind": "window", "window": "s1/g99999999/w1"})

    def test_control_and_baseline(self):
        c = self.cache
        c.set("control", {"state": "agent", "epoch": 1})
        c.baseline()
        self.assertEqual(c.rev, 1)
        c.set("control", {"state": "paused", "epoch": 2})
        c.commit()
        self.assertEqual(c.facets["control"], 2)
        self.assertFalse(c.observe({"kind": "session"}, 1)["unchanged"])

    def test_destroyed_window_scope_retains_removal_evidence(self):
        c = self.cache
        c.upsert(10, window())
        c.commit()
        handle = c.handle(10)
        since = c.rev
        c.remove(10)
        c.commit()
        result = c.observe({"kind": "window", "window": handle}, since)
        self.assertEqual(result["state"]["windows"], [])
        self.assertEqual(result["state"]["removed"], [handle])


class FakeSelector:
    def modify(self, *args):
        pass

    def unregister(self, *args):
        pass


class RPCTests(SandboxCase):
    def setUp(self):
        super().setUp()
        self.worker = worker = w.Worker.__new__(w.Worker)
        worker.state, worker.sd = self.path, self.path
        (self.path / "acts").mkdir()
        worker.sid, worker.generation = "s1", GEN
        worker.record = {"profile": "xvfb", "display": 91}
        worker.cache = w.StateCache("s1", GEN)
        worker.cache.control = {"state": "agent", "epoch": 1}
        worker.cache.upsert(10, window())
        worker.cache.baseline()
        worker.journal = w.Journal(self.path / "acts" / (GEN + ".jsonl"), "s1", GEN)
        self.addCleanup(worker.journal.close)
        worker.x = NS(initialised=True, ax=NS(pending=[], ask=Mock()), refreshing=set(), uncovered=set(), versions={})
        worker.watchdog = w.time.monotonic() + 100
        worker.selector = FakeSelector()
        worker.ingest = Mock()
        worker.lease = worker.lease_peer = worker.lease_until = None
        worker.screen = {"w": 640, "h": 480}
        worker.stopping = False
        worker.openbox_version = "test"
        worker.counters = dict.fromkeys(("batches", "steps", "verified", "not_observed", "contradicted",
                                        "unknown", "retries", "route_changes", "resync_drift", "false_closes"), 0)
        self.peer, self.other = w.Peer(Mock()), w.Peer(Mock())
        worker.peers = {self.peer, self.other}

    def call(self, method, params=None, peer=None):
        peer = peer or self.peer
        self.worker.message(peer, w.json_bytes({"jsonrpc": "2.0", "id": 7,
                                             "method": method, "params": params or {}}))
        if not peer.outgoing:
            return None
        size = struct.unpack("!I", peer.outgoing[:4])[0]
        value = json.loads(peer.outgoing[4:size + 4])
        del peer.outgoing[:size + 4]
        return value

    def attach(self):
        return self.call("session.attach", {"client": "codex", "name": "test"})["result"]["lease"]

    def test_attach_connection_binding_done_and_journal(self):
        token = self.attach()
        self.assertEqual(len(token), 32)
        self.assertEqual(self.call("session.attach", {"client": "api", "name": "other"}, self.other)
                         ["error"]["data"]["reason"], "lease_held")
        self.assertEqual(self.call("window.observe", {"lease": token, "since_rev": 1}, self.other)
                         ["error"]["data"]["reason"], "invalid_lease")
        result = self.call("session.done", {"lease": token, "status": "success", "summary": "done"})
        self.assertEqual(result["result"], {})
        self.assertIsNone(self.worker.lease)
        self.assertIn("result", self.call("session.attach", {"client": "api", "name": "next"}, self.other))
        self.assertEqual([json.loads(s)["type"] for s in self.worker.journal.path.read_text().splitlines()],
                         ["attach", "done", "attach"])

    def test_disconnect_grace_and_new_token(self):
        first = self.attach()
        with patch.object(w.time, "monotonic", return_value=100):
            self.worker.disconnect(self.peer)
            self.assertEqual(self.worker.lease_until, 105)
        with patch.object(w.time, "monotonic", return_value=104.999):
            self.assertEqual(self.call("session.attach", {"client": "api", "name": "other"}, self.other)
                             ["error"]["data"]["reason"], "lease_held")
        with patch.object(w.time, "monotonic", return_value=105):
            second = self.call("session.attach", {"client": "api", "name": "other"}, self.other)["result"]["lease"]
        self.assertNotEqual(first, second)

    def test_1000_unchanged_calls_never_query_x(self):
        token = self.attach()
        for _ in range(1000):
            value = self.call("window.observe", {"lease": token, "since_rev": 1})["result"]
            self.assertTrue(value["unchanged"])
            self.assertEqual(value["channels_used"], ["state"])
        self.worker.x.ax.ask.assert_not_called()

    def test_full_observe_pointer_is_deferred(self):
        self.worker.x.root = 1
        token = self.attach()
        self.assertIsNone(self.call("window.observe", {"lease": token}))
        cls, callback = self.worker.x.ax.ask.call_args.args
        self.assertIs(cls, w.request.QueryPointer)
        callback({"root_x": 9, "root_y": 11})
        self.worker.cache.commit()
        self.worker.pending()
        size = struct.unpack("!I", self.peer.outgoing[:4])[0]
        value = json.loads(self.peer.outgoing[4:size + 4])["result"]
        self.assertEqual(value["state"]["pointer"], {"x": 9, "y": 11})
        self.assertTrue(value["full"])

    def test_unavailable_requested_channels_are_not_fabricated(self):
        token = self.attach()
        value = self.call("window.observe", {"lease": token, "since_rev": 1,
                         "channels": ["state", "tree", "ocr", "pixels"]})["result"]
        self.assertIsNone(value["unchanged"])
        for name in ("tree", "ocr", "pixels"):
            self.assertNotIn(name, value)
            self.assertFalse(value["coverage"][name]["complete"])
            self.assertEqual(value["providers"][name]["state"], "down" if name == "tree" else "unavailable")

    def test_wait_event_completion_and_timeout(self):
        token = self.attach()
        params = {"lease": token, "expect": {"kind": "window_present", "match": {"title": "Later"}}}
        self.assertIsNone(self.call("window.wait", params))
        self.worker.cache.upsert(20, window("Later", xid=20))
        self.worker.cache.commit(True)
        self.worker.pending()
        self.assertTrue(json.loads(self.peer.outgoing[4:])["result"]["satisfied"])
        self.peer.outgoing.clear()
        self.assertIsNone(self.call("window.wait", {"lease": token,
            "expect": {"kind": "window_absent", "match": {"title": "Later"}}}))
        self.peer.waits[0]["deadline"] = 0
        self.worker.pending()
        result = json.loads(self.peer.outgoing[4:])["result"]
        self.assertFalse(result["satisfied"])
        self.assertEqual(result["reason"], "timeout")

    def test_wait_missing_providers_returns_unknown(self):
        token = self.attach()
        for kind in ("node_present", "text_equals", "ocr_text_absent", "region_changed"):
            result = self.call("window.wait", {"lease": token, "expect": {"kind": kind}})["result"]
            self.assertIsNone(result["satisfied"])
            self.assertEqual(result["reason"], "provider_degraded")

    def test_dialog_focus_and_regex_predicates(self):
        c = self.worker.cache
        handle = c.handle(10)
        c.set("focus", handle)
        c.upsert(20, window("Rename exact", xid=20, type="dialog", transient_for=10, is_modal=True))
        c.commit(True)
        self.assertTrue(self.worker.predicate({"kind": "window_focused", "window": handle}))
        self.assertTrue(self.worker.predicate({"kind": "window_present", "match": {"title_re": "^R(ename).*exact$"}}))
        self.assertTrue(self.worker.predicate({"kind": "dialog_present", "parent": handle,
                                              "match": {"title": "Rename exact"}}))
        self.assertIsNone(self.worker.predicate({"kind": "dialog_present", "match": {"type": "dialog"}}))

    def test_malformed_act_rejects_without_journal_intent(self):
        before = self.worker.journal.path.read_bytes()
        result = self.call("window.act", {"action_id": "w2"})
        self.assertEqual(result["error"]["data"], {"reason": "invalid_params"})
        self.assertEqual(before, self.worker.journal.path.read_bytes())
        self.assertEqual(self.call("action.get", {"action_id": "w2"})["result"]["status"], "not_found")

    def test_honest_health_and_all_counter_keys(self):
        health = self.call("system.info")["result"]
        self.assertEqual(len(health["counters"]), 10)
        self.assertTrue(all(v == 0 for v in health["counters"].values()))
        self.assertEqual(health["channels"]["xtest"]["state"], "untested")
        self.assertEqual(health["channels"]["evdev"]["reason"], "backend_unsupported")
        self.worker.record["profile"] = "xorg"
        self.assertEqual(self.worker.channels()["evdev"]["reason"], "not_implemented")
        self.assertEqual(health["capture"]["mode"], "unavailable")

    def test_invalid_requests_do_not_retire_worker(self):
        token = self.attach()
        for method, params in (
                ("window.observe", {"lease": token, "scope": 3}),
                ("window.observe", {"lease": token, "channels": [3]}),
                ("window.observe", {"lease": token, "since_rev": True}),
                ("window.observe", {"lease": token, "unknown": True}),
                ("window.wait", {"lease": token, "expect": {"kind": "window_present", "match": {"title_re": "["}}}),
                ("system.info", {"deadline_ms": 0}),
                ("action.get", {"generation": "../bad", "action_id": "x"}),
                ("session.done", {"lease": token, "status": "success", "summary": "a\0b"})):
            self.assertIn("error", self.call(method, params))
        self.assertIn("result", self.call("system.info"))

    def test_fragmented_and_coalesced_frame_decoder(self):
        payload = w.frame({"jsonrpc": "2.0", "id": 1, "method": "system.info"})
        self.peer.sock.recv.side_effect = [payload[:2], payload[2:] + payload]
        self.worker.io(self.peer, selectors.EVENT_READ)
        self.assertFalse(self.peer.outgoing)
        self.worker.io(self.peer, selectors.EVENT_READ)
        replies = []
        while self.peer.outgoing:
            size = struct.unpack("!I", self.peer.outgoing[:4])[0]
            replies.append(json.loads(self.peer.outgoing[4:size + 4]))
            del self.peer.outgoing[:size + 4]
        self.assertEqual(len(replies), 2)
        self.assertTrue(all(r["jsonrpc"] == "2.0" for r in replies))

    def test_oversized_frame_closes_peer(self):
        self.peer.sock.recv.return_value = struct.pack("!I", w.MAX_FRAME + 1)
        self.worker.io(self.peer, selectors.EVENT_READ)
        self.assertNotIn(self.peer, self.worker.peers)

    def test_uid_rejection(self):
        sock = Mock()
        sock.getsockopt.return_value = struct.pack("3i", 100, os.getuid() + 1, 100)
        self.worker.listener = Mock()
        self.worker.listener.accept.return_value = (sock, None)
        self.worker.accept()
        sock.close.assert_called_once()

    def test_control_poll_journals_transition_once(self):
        worker = self.worker
        path = self.path / "control"
        w.initialise_control(path, GEN)
        with path.open("rb") as stream, mmap.mmap(stream.fileno(), 64, access=mmap.ACCESS_READ) as page:
            worker.control_page, worker.control_raw, worker.control_due = page, None, 0
            worker.control_file = stream
            info = os.fstat(stream.fileno())
            worker.control_identity = (info.st_dev, info.st_ino)
            worker.cache.control = None
            worker.poll_control(True)
            with path.open("r+b") as output, mmap.mmap(output.fileno(), 64) as mutable:
                w.transition_control(mutable, w.STATES["human"], 2)
            worker.poll_control(True)
            worker.cache.commit()
            worker.poll_control(True)
        rows = [json.loads(s) for s in worker.journal.path.read_text().splitlines()]
        self.assertEqual([r["type"] for r in rows], ["control"])
        self.assertEqual(rows[0]["state"], "human")
        self.assertEqual(worker.cache.facets["control"], 2)

    def test_truncated_control_is_closed_without_mmap_sigbus(self):
        worker = self.worker
        path = self.path / "control"
        w.initialise_control(path, GEN)
        with path.open("rb") as stream, mmap.mmap(stream.fileno(), 64, access=mmap.ACCESS_READ) as page:
            worker.control_file, worker.control_page = stream, page
            info = os.fstat(stream.fileno())
            worker.control_identity = (info.st_dev, info.st_ino)
            worker.control_raw, worker.control_due = None, 0
            worker.poll_control(True)
            path.write_bytes(b"bad")
            worker.poll_control(True)
            self.assertEqual(worker.cache.control["state"], "paused")
            self.assertIsNone(worker.cache.control["epoch"])


class XEventTests(unittest.TestCase):
    def test_generic_and_unknown_events_ignored_core_motion_still_works(self):
        cache = w.StateCache("s1", GEN)
        xs = w.XState.__new__(w.XState)
        xs.w, xs.root = NS(cache=cache), NS(id=1)
        xs.focus_xid = xs.input_focus = 0
        # Real python-xlib 0.33 generic envelopes, including an unparsed raw
        # payload and a non-XI extension. No display constructor is involved.
        events = [w.ge.GenericEvent(binarydata=struct.pack(
            "=BBHIH22s", w.ge.GenericEventCode, extension, 1, 0, kind, bytes(22)))
            for extension, kind in ((131, w.xinput.RawMotion), (132, 999))]
        self.assertIsInstance(events[0].data, bytes)
        events += [NS(type=w.ge.GenericEventCode, evtype=w.xinput.Motion,
                      data=NS(root_x=900, root_y=800)), NS(type=w.X.MappingNotify)]
        xs.events(events)
        self.assertIsNone(cache.pointer)
        self.assertFalse(cache.windows)
        xs.events([NS(type=w.X.MotionNotify, root_x=12, root_y=34)])
        self.assertEqual(cache.pointer, {"x": 12, "y": 34})

    def test_property_wire_shape(self):
        self.assertEqual(w.prop_values({"property_type": 33, "value": (32, array.array("I", [10, 20]))}),
                         array.array("I", [10, 20]))
        self.assertEqual(w.prop_values(None), [])

    def test_synthetic_x_event_stream_map_focus_geometry_unmap_destroy(self):
        cache = w.StateCache("s1", GEN)
        worker = NS(cache=cache, x_errors=[])
        xs = w.XState.__new__(w.XState)
        xs.w, xs.root = worker, NS(id=1)
        xs.atoms = {name: i + 100 for i, name in enumerate(w.ATOM_NAMES)}
        xs.clients, xs.refreshing, xs.again, xs.incarnations = {10}, set(), set(), {}
        xs.selected_windows, xs.uncovered = set(), set()
        xs.focus_xid, xs.input_focus = 0, 0
        xs.ev = Mock()
        source = {"mapped": True, "x": 3, "focus": 0}
        def ask(cls, callback, **args):
            if cls is w.request.GetWindowAttributes:
                data = {"override_redirect": False, "win_class": w.X.InputOutput,
                        "map_state": w.X.IsViewable if source["mapped"] else w.X.IsUnmapped}
            elif cls is w.request.GetGeometry:
                data = {"width": 100, "height": 80}
            elif cls is w.request.TranslateCoords:
                data = {"x": source["x"], "y": 4}
            elif cls is w.request.GetInputFocus:
                data = {"focus": source["focus"]}
            elif cls is w.request.QueryTree:
                data = {"children": [10]}
            else:
                atom = args["property"]
                value = (8, b"Main") if atom == xs.atoms["_NET_WM_NAME"] else (
                    (8, b"fixture\0Fixture\0") if atom == xs.atoms["WM_CLASS"] else (
                    (32, [source["focus"]]) if atom == xs.atoms["_NET_ACTIVE_WINDOW"] else (32, [])))
                data = {"property_type": 1, "value": value}
            callback(data)
        xs.ax = NS(ask=ask)
        def event(kind):
            xs.events([NS(type=kind, window=10)])
            cache.commit(True)
        event(w.X.MapNotify)
        self.assertEqual(cache.rev, 2)
        handle = cache.handle(10)
        self.assertEqual(cache.windows[10]["geometry"]["x"], 3)
        source["focus"] = 10
        event(w.X.FocusIn)
        self.assertEqual(cache.focus, handle)
        self.assertEqual(cache.facets["focus"], 3)
        source["x"] = 44
        event(w.X.ConfigureNotify)
        self.assertEqual(cache.windows[10]["geometry"]["x"], 44)
        self.assertEqual(cache.facets["geometry[" + handle + "]"], 4)
        source["mapped"] = False
        xs.clients.clear()  # WM may remove the client before UnmapNotify.
        event(w.X.UnmapNotify)
        self.assertFalse(cache.windows[10]["mapped"])
        event(w.X.DestroyNotify)
        self.assertIn(handle, cache.observe({"kind": "session"}, 5)["state"]["removed"])
        self.assertTrue(cache.observe({"kind": "session"}, cache.rev)["unchanged"])

    def test_extension_queries_explicit_22_before_any_xi_use(self):
        # The constructor is driven with fake deferred connections, not X.
        ev, inp = Mock(), Mock()
        ev.display.info.pixmap_formats = [NS(depth=24, bits_per_pixel=32)]
        root = Mock()
        root.id = 1
        ev.screen.return_value.root = root
        requests = []
        class Deferred:
            def __init__(self, connection, fatal):
                self.pending = []
                self.on_error = Mock()
            def ask(self, cls, cb, **kwargs):
                requests.append((cls, kwargs))
                self.pending.append(1)
        with patch.object(w.display, "Display", side_effect=[ev, inp]), patch.object(w, "AsyncX", Deferred):
            w.XState(NS(record={"display": 91}, x_errors=[]))
        query = [kw for cls, kw in requests if cls is w.xinput.XIQueryVersion]
        self.assertEqual(len(query), 1)
        self.assertEqual((query[0]["major_version"], query[0]["minor_version"]), (2, 2))
        root.xinput_select_events.assert_not_called()
        self.assertFalse(any("Create" in cls.__name__ for cls, _ in requests))

    def test_host_display_rejected_before_x_connection(self):
        with patch.object(w.display, "Display") as connect:
            with self.assertRaisesRegex(RuntimeError, "display_out_of_range"):
                w.XState(NS(record={"display": 0}))
            connect.assert_not_called()

    def test_environment_is_registry_only(self):
        record = {"session": "s1", "display": 91, "home": "/session/home", "runtime_dir": "/session/runtime",
                  "bus_address": "unix:path=/session/bus", "atspi_address": "unix:path=/session/atspi"}
        with patch.dict(os.environ, {"DISPLAY": ":0", "HOME": "/host", "DBUS_SESSION_BUS_ADDRESS": "host", "XAUTHORITY": "/host/auth"}):
            env = w.session_env(record, Path("/session/state"))
        self.assertEqual(env["DISPLAY"], ":91")
        self.assertEqual(env["HOME"], "/session/home")
        self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"], record["bus_address"])
        self.assertNotIn("XAUTHORITY", env)
        self.assertEqual(env["ARK_SESSION_ID"], "s1")

    def test_low_xi_version_refused(self):
        xs = w.XState.__new__(w.XState)
        xs.versions = {}
        with self.assertRaisesRegex(RuntimeError, "missing_extension"):
            xs.version("XInputExtension", {"major_version": 2, "minor_version": 0})

    def test_expected_badwindow_consumed_other_x_error_faults(self):
        xs = w.XState.__new__(w.XState)
        xs.w = NS(x_errors=[])
        bad_window = w.error.BadWindow.__new__(w.error.BadWindow)
        self.assertEqual(xs.window_select_error(bad_window, None), 1)
        self.assertFalse(xs.w.x_errors)
        bad_access = w.error.BadAccess.__new__(w.error.BadAccess)
        self.assertEqual(xs.window_select_error(bad_access, None), 1)
        self.assertEqual(xs.w.x_errors, [bad_access])


class XIdentityTests(SandboxCase):
    def setUp(self):
        super().setUp()
        sd = self.path / "run/s1"
        for name in ("home", "runtime"):
            (sd / name).mkdir(parents=True)
        self.record = {"session": "s1", "display": 91, "profile": "xorg", "pid_x": 12345,
                       "session_dir": str(sd), "home": str(sd / "home"),
                       "runtime_dir": str(sd / "runtime"),
                       "bus_address": "unix:path=session-bus", "atspi_address": "unix:path=atspi"}
        self.cmdline = b"/usr/lib/xorg/Xorg\0:91\0-config\0ark-sessiond-91.conf\0"
        self.lock_pid, self.starttime, self.socket_present = "     12345\n", "987654", True
        self.environ_reads = []
        real_read_text, real_read_bytes, real_stat = Path.read_text, Path.read_bytes, Path.stat
        def read_text(path, *args, **kwargs):
            if path == Path("/proc/12345/stat"):
                # /proc stat field 22 is starttime; root ownership does not
                # prevent reading it. Include spaces/parentheses in comm.
                return "12345 (Xorg (session)) S " + "0 " * 18 + self.starttime + " 0"
            if path == Path("/tmp/.X91-lock"):
                return self.lock_pid
            return real_read_text(path, *args, **kwargs)
        def read_bytes(path, *args, **kwargs):
            if path == Path("/proc/12345/cmdline"):
                return self.cmdline
            if path == Path("/proc/12345/environ"):
                self.environ_reads.append(path)
                raise PermissionError("root-owned process environ")
            return real_read_bytes(path, *args, **kwargs)
        def stat(path, *args, **kwargs):
            if path == Path("/tmp/.X11-unix/X91"):
                # A root-owned socket; no same-UID requirement is valid here.
                return NS(st_mode=0o140777 if self.socket_present else 0o100644, st_uid=0)
            return real_stat(path, *args, **kwargs)
        for name, replacement in (("read_text", read_text), ("read_bytes", read_bytes), ("stat", stat)):
            patcher = patch.object(Path, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_root_owned_xorg_and_user_xvfb_bind_without_environ(self):
        for profile, cmdline in (("xorg", self.cmdline), ("xvfb", b"Xvfb\0:91\0-screen\0" + b"0\0")):
            with self.subTest(profile=profile):
                self.record["profile"], self.cmdline = profile, cmdline
                record = w.session_record({"s1": self.record}, "s1", self.path)
                self.assertEqual(record["_x_starttime"], self.starttime)
                # Reach the first connection only after a successful recheck.
                with patch.object(w.display, "Display", side_effect=RuntimeError("connection reached")) as connect:
                    with self.assertRaisesRegex(RuntimeError, "connection reached"):
                        w.XState(NS(record=record))
                    connect.assert_called_once_with(":91")
        self.assertFalse(self.environ_reads)

    def test_wrong_process_display_lock_or_missing_socket_rejected(self):
        original = self.cmdline
        for cmdline, lock_pid, present, reason in (
                (original.replace(b":91", b":92"), "12345", True, "x_identity_mismatch"),
                (original.replace(b"Xorg", b"other"), "12345", True, "x_identity_mismatch"),
                (original, "54321", True, "x_identity_mismatch"),
                (original, "malformed", True, "x_identity_mismatch"),
                (original, "12345", False, "x_socket_missing")):
            with self.subTest(cmdline=cmdline, lock_pid=lock_pid, socket=present):
                self.cmdline, self.lock_pid, self.socket_present = cmdline, lock_pid, present
                with self.assertRaisesRegex(RuntimeError, reason):
                    w.session_record({"s1": self.record}, "s1", self.path)

    def test_dead_pid_and_changed_starttime_refused_before_connect(self):
        with patch.object(w, "proc_identity", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "x_server_dead"):
                w.session_record({"s1": self.record}, "s1", self.path)
        record = w.session_record({"s1": self.record}, "s1", self.path)
        for identity in (None, "987655"):
            with patch.object(w, "proc_identity", return_value=identity), patch.object(w.display, "Display") as connect:
                with self.assertRaisesRegex(RuntimeError, "x_identity_mismatch"):
                    w.XState(NS(record=record))
                connect.assert_not_called()

    def test_display_binding_rechecked_before_connect(self):
        record = w.session_record({"s1": self.record}, "s1", self.path)
        self.lock_pid = "54321"
        with patch.object(w.display, "Display") as connect:
            with self.assertRaisesRegex(RuntimeError, "x_identity_mismatch"):
                w.XState(NS(record=record))
            connect.assert_not_called()


class LifecycleTests(SandboxCase):
    def test_launcher_failed_reply_eof_and_bad_json_stop_owned_process_group(self):
        record = {"session": "s1", "session_dir": str(self.path), "display": 91,
                  "home": str(self.path), "runtime_dir": str(self.path),
                  "bus_address": "unix:path=bus", "atspi_address": "unix:path=atspi"}
        (self.path / "sessions.json").write_text('{"s1":{}}')
        evidence = self.path / "child-exit"
        # A throwaway daemon/child group replaces the worker; no X or WM.
        # On TERM the daemon records whether its child ALSO received the
        # group signal, and reaps it. Even a failed assertion leaves no orphan.
        script = """
import os, signal, subprocess, sys
from pathlib import Path
child = subprocess.Popen([sys.executable, '-c',
    "import os, signal; os.write(1, b'R'); signal.pause()"], stdout=subprocess.PIPE)
assert child.stdout.read(1) == b'R'
child.stdout.close()
def stopped(signum, frame):
    try:
        code = child.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()
        code = 'survived group TERM'
    Path(sys.argv[2]).write_text(str(code))
    sys.exit(0)
signal.signal(signal.SIGTERM, stopped)
os.write(int(sys.argv[1]), sys.argv[3].encode())
os.close(int(sys.argv[1]))
signal.pause()
"""
        spawn = w.subprocess.Popen
        for payload, error in (('{"state":"failed","error":"injected"}\n', None),
                               ("", "worker_fault"), ("{broken", "Expecting property")):
            with self.subTest(payload=payload):
                evidence.unlink(missing_ok=True)
                daemons = []
                def launch(args, **kwargs):
                    daemon = spawn([sys.executable, "-c", script,
                                    str(kwargs["pass_fds"][0]), str(evidence), payload], **kwargs)
                    daemons.append(daemon)
                    return daemon
                try:
                    with patch.object(w, "session_record", return_value=record), \
                            patch.object(w, "worker_metadata", return_value=None), \
                            patch.object(w.subprocess, "Popen", side_effect=launch):
                        if error:
                            with self.assertRaisesRegex((RuntimeError, ValueError), error):
                                w.start(self.path, "s1")
                        else:
                            self.assertEqual(w.start(self.path, "s1")["state"], "failed")
                    self.assertEqual(evidence.read_text(), str(-signal.SIGTERM))
                    self.assertIsNotNone(daemons[0].returncode)
                finally:
                    for daemon in daemons:
                        if daemon.poll() is None:
                            os.killpg(daemon.pid, signal.SIGTERM)
                            daemon.wait(timeout=3)

    def test_failed_start_reaps_spawned_child_before_failure_notification(self):
        (self.path / "acts").mkdir()
        w.initialise_control(self.path / "control", GEN)
        record = {"session": "s1", "session_dir": str(self.path)}
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
        try:
            # Real harmless children, never Openbox or an X connection. The
            # second child ignores TERM, exercising bounded KILL/reap cleanup.
            for ignore_term in (False, True):
                with self.subTest(ignore_term=ignore_term):
                    worker = w.Worker(self.path, record, GEN, None)
                    fds = [os.pipe(), os.pipe()]
                    x = Mock(initialised=False)
                    x.ev.fileno.return_value, x.inp.fileno.return_value = fds[0][0], fds[1][0]
                    x.ax.pending, x.inp_ax.pending = [], []
                    script = ("import os, signal; "
                              "signal.signal(signal.SIGTERM, signal.SIG_IGN); " if ignore_term else
                              "import os, signal; ") + "os.write(1, b'R'); signal.pause()"
                    def spawn():
                        worker.openbox = w.subprocess.Popen([sys.executable, "-c", script],
                            cwd=self.path, stdout=w.subprocess.PIPE, stderr=w.subprocess.DEVNULL)
                        self.assertEqual(worker.openbox.stdout.read(1), b"R")
                        # A failed X transport must not block child teardown.
                        x.ev.fileno.side_effect = OSError("broken X transport")
                    x.advance.side_effect = spawn
                    notified = []
                    def notify(value):
                        self.assertIsNotNone(worker.openbox.returncode)
                        with self.assertRaises(ChildProcessError):
                            os.waitpid(worker.openbox.pid, os.WNOHANG)
                        notified.append(value)
                    real_wait_pid = w.wait_pid
                    worker.ready_fd = 999  # notify is replaced; no such FD is used.
                    try:
                        with patch.object(w, "XState", return_value=x), \
                                patch.object(worker, "ingest", side_effect=[None, RuntimeError("worker_fault: X lost")]), \
                                patch.object(worker, "notify", side_effect=notify), \
                                patch.object(w, "wait_pid", side_effect=lambda pid, seconds: real_wait_pid(pid, 0.05)), \
                                patch.object(w.sys, "stderr", io.StringIO()):
                            self.assertEqual(worker.run(), 1)
                        self.assertEqual(notified[0]["error"], "worker_fault: X lost")
                        self.assertEqual(worker.openbox.returncode, -signal.SIGKILL if ignore_term else -signal.SIGTERM)
                        self.assertTrue(worker.control_file.closed)
                    finally:
                        if worker.openbox:
                            if worker.openbox.poll() is None:
                                worker.openbox.kill()
                            worker.openbox.wait()
                            worker.openbox.stdout.close()
                        for pair in fds:
                            for fd in pair:
                                os.close(fd)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)

    def test_x_fault_journaled_and_local_handles_closed(self):
        (self.path / "acts").mkdir()
        w.initialise_control(self.path / "control", GEN)
        record = {"session": "s1", "session_dir": str(self.path)}
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
        try:
            worker = w.Worker(self.path, record, GEN, None)
            with patch.object(w, "XState", side_effect=RuntimeError("X connection lost")), patch.object(w.sys, "stderr", io.StringIO()):
                self.assertEqual(worker.run(), 1)
            self.assertTrue(worker.control_file.closed)
            self.assertTrue(worker.control_page.closed)
            records = [json.loads(s) for s in (self.path / "acts" / (GEN + ".jsonl")).read_text().splitlines()]
            self.assertEqual([r["type"] for r in records], ["worker_stop"])
            self.assertEqual(records[0]["reason"], "worker_fault")
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)

    def test_second_start_refused_without_generation_or_child(self):
        state = self.path
        (state / "sessions.json").write_text('{"s1":{}}')
        record = {"session_dir": str(self.path)}
        with patch.object(w, "session_record", return_value=record), patch.object(w, "worker_metadata", return_value={"pid": 123}), patch.object(w.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(RuntimeError, "worker_already_running"):
                w.start(state, "s1")
            spawn.assert_not_called()
            self.assertFalse((state / "acts").exists())

    def test_stop_dead_worker_removes_stale_endpoint_only(self):
        (self.path / "driver").mkdir()
        (self.path / "driver/act.sock").touch()
        (self.path / "worker.json").write_text("{}")
        records = {"s1": {"session_dir": str(self.path)}}
        text = json.dumps(records)
        (self.path / "sessions.json").write_text(text)
        args = NS(_daemon=False, command="stop", session="s1")
        with patch.object(w, "STATE", self.path), patch.object(w, "worker_metadata", return_value=None):
            result, code = w.command(args)
        self.assertEqual(code, 0)
        self.assertEqual(result["state"], "stopped")
        self.assertFalse((self.path / "worker.json").exists())
        self.assertFalse((self.path / "driver/act.sock").exists())
        self.assertEqual((self.path / "sessions.json").read_text(), text)

    def test_pinned_openbox_config(self):
        import xml.etree.ElementTree as ET
        text = (WM / "openbox-rc.xml").read_text()
        root = ET.fromstring(text)
        ns = {"o": "http://openbox.org/3.4/rc"}
        self.assertFalse(root.findall(".//o:keybind", ns))
        self.assertNotIn("F2", text)
        self.assertEqual(root.find("o:focus/o:followMouse", ns).text, "no")
        self.assertEqual(root.find("o:desktops/o:number", ns).text, "1")
        self.assertEqual(root.find("o:theme/o:animateIconify", ns).text, "no")


if __name__ == "__main__":
    unittest.main(verbosity=2)
