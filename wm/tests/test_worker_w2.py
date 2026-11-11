#!/usr/bin/python3
"""Headless W2 contracts, real journal/fsync and deterministic fake X server.

No X display, socket listener, session or input device is opened. Fake server
state/logs are independent of worker receipts, and time advances explicitly.
"""
import copy
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import signal
import struct
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch
from Xlib import XK

sys.dont_write_bytecode = True
WM = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("ark_worker_w2", str(WM / "ark-worker"))
spec = importlib.util.spec_from_loader(loader.name, loader)
w = importlib.util.module_from_spec(spec)
loader.exec_module(w)
from worker_act import Ack, ActError, Engine, chord, predicate_schema
from worker_xtest import XTestTransport, decode_raw
GEN = "0123456789abcdef" * 2


class FakeX:
    """Independent input recorder; it does not consult receipt/effect fields."""
    def __init__(self, owner):
        self.w = owner
        self.sources = {"keyboard": 5, "pointer": 4}
        self.raw, self.raw_seq = [], 0
        self.position = {"x": 2, "y": 2}
        self.child = 10
        self.mask = 0
        self.down, self.buttons = set(), set()
        self.log, self.bursts, self.buffer = [], [], []
        self.ack = True
        self.release_ack = True
        self.hook = None
        self.query_log = []
        self.test_presses = 0
        self.test = False
        self.focus_works = True
        self.close_works = True
        self.refresh_hook = None
        self.initialised = True
        self.delay = 0
        self.on_write = None

    def emit(self, event, slave=True):
        kind, detail, *xy = event
        source = 5 if kind.startswith("key") else 4
        self.raw_seq += 1
        self.raw.append((self.raw_seq, kind, detail, source if slave else 2, source))

    def write(self, event):
        # Oracle: the on-disk fsynced intent and arm MUST exist at every write.
        if not self.test:
            rows = self.w.rows()
            diagnostic_release = event[0].endswith("up") and any(v["type"] == "release" and v["action_id"] == "channel_selftest" for v in rows)
            if not diagnostic_release:
                assert any(v["type"] == "intent" for v in rows)
                assert any(v["type"] == "step_armed" for v in rows)
        self.buffer.append(event)
        self.log.append(event)
        kind, detail, *xy = event
        if kind == "key_down":
            self.down.add(detail)
            if self.test:
                self.test_presses += 1
        elif kind == "key_up" and self.release_ack:
            self.down.discard(detail)
        elif kind == "button_down":
            self.buttons.add(detail)
        elif kind == "button_up" and self.release_ack:
            self.buttons.discard(detail)
        elif kind == "motion":
            self.position = {"x": xy[0], "y": xy[1]}
            self.w.cache.set("pointer", dict(self.position))
        if self.ack and (not kind.endswith("up") or self.release_ack):
            self.emit(event, False)  # master duplicate arrives first
            self.emit(event, True)
        if self.on_write:
            self.on_write(event)

    def flush(self):
        if self.buffer:
            self.bursts.append(list(self.buffer))
            self.buffer.clear()
        if self.hook:
            self.hook()

    def keycode(self, name):
        return {"Control_L": 37, "Shift_L": 50, "Alt_L": 64, "Super_L": 133,
                "a": 38, "b": 56, "F2": 68, "Return": 36, "Tab": 23}.get(name)

    def character(self, char):
        return [50, 38] if char == "A" else [38] if char == "a" else [56] if char == "b" else None

    def select_all(self, handle):
        return ["Control_L", "a"]

    def inter_key_delay_us(self, handle):
        return self.delay

    def pointer(self, cb):
        self.query_log.append("pointer")
        cb(dict(self.position, child=self.child, mask=self.mask))

    def keymap(self, cb):
        self.query_log.append("keymap")
        cb(set(self.down))

    def refresh(self, predicate, target, cb):
        self.query_log.append("refresh")
        if self.refresh_hook:
            self.refresh_hook()
        cb({"complete": True})

    def barrier(self, cb):
        cb({})

    def belongs(self, child, win):
        return child == win["xid"]

    def wm(self, kind, wid):
        self.log.append((kind, wid))
        if kind == "focus_window" and self.focus_works:
            self.w.cache.set("focus", self.w.cache.handle(wid))
        if kind == "close_window" and self.close_works:
            self.w.cache.remove(wid)
        if kind == "raise_window":
            self.child = wid
        self.w.cache.commit()

    def test_window(self, keyboard):
        self.test = True
        self.test_presses = 0
        return 123

    def end_test(self, test, old_focus):
        self.test = False


class DeferredMetadata:
    """Server properties without PropertyNotify; real sample/snapshot code.

    Replies take separate pump turns, including descendants queued by replies.
    Title replies are slower than the first barrier and capture request-time
    state, so a barrier alone cannot stand in for a completed metadata sample.
    """
    def __init__(self, harness):
        self.h = harness
        self.title = "Fixture"
        self.title_delay = 0.012
        self.pending, self.log = [], []
        xs = self.x = w.XState.__new__(w.XState)
        xs.w, xs.root, xs.ev = harness.w, NS(id=1), Mock()
        xs.ev.display.info.pixmap_formats = [NS(depth=24, bits_per_pixel=32)]
        xs.atoms = {name: i + 100 for i, name in enumerate(w.ATOM_NAMES)}
        xs.clients, xs.refreshing, xs.again, xs.incarnations = {10}, set(), set(), {}
        xs.selected_windows, xs.uncovered = {10}, set()
        xs.focus_xid = xs.input_focus = 10
        xs.ax = self
        transport = XTestTransport.__new__(XTestTransport)
        transport.w, transport.x = harness.w, xs
        transport.pointer = harness.t.pointer
        self.transport = transport

    def ask(self, cls, callback, **args):
        title = cls is w.request.GetProperty and args["property"] == self.x.atoms["_NET_WM_NAME"]
        delay = self.title_delay if title else 0.001
        self.log.append((self.h.now, cls, args))
        self.pending.append((self.h.now + delay, callback, self.reply(cls, args)))

    def reply(self, cls, args):
        if cls is w.request.GetWindowAttributes:
            return {"override_redirect": False, "win_class": w.X.InputOutput, "map_state": w.X.IsViewable}
        if cls is w.request.GetGeometry:
            return {"width": 640, "height": 480, "depth": 24} if args["drawable"] is self.x.root else {"width": 500, "height": 400}
        if cls is w.request.TranslateCoords:
            return {"x": 0, "y": 0}
        if cls is w.request.GetInputFocus:
            return {"focus": 10}
        if cls is w.request.QueryTree:
            return {"children": list(self.x.clients), "parent": 1}
        if cls is w.request.QueryPointer:
            return {"root_x": self.h.t.position["x"], "root_y": self.h.t.position["y"]}
        if cls is w.request.GetProperty:
            atom = args["property"]
            wid = getattr(args["window"], "id", args["window"])
            title = self.title if wid == 10 else "Unrelated"
            value = (8, title.encode()) if atom == self.x.atoms["_NET_WM_NAME"] else (
                (8, b"fixture\0Fixture\0") if atom == self.x.atoms["WM_CLASS"] else (
                (32, list(self.x.clients)) if atom == self.x.atoms["_NET_CLIENT_LIST"] else (
                (32, [10]) if atom == self.x.atoms["_NET_ACTIVE_WINDOW"] else (32, []))))
            return {"property_type": 1, "value": value}
        raise AssertionError(cls)

    def pump(self):
        pending, self.pending = self.pending, []
        for due, callback, data in pending:
            if self.h.now >= due:
                callback(data)
            else:
                self.pending.append((due, callback, data))
        self.x.derive()


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix=".worker-w2-", dir=WM.parent)
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        (self.path / "acts").mkdir()
        self.w = owner = w.Worker.__new__(w.Worker)
        owner.state, owner.sd = self.path, self.path
        owner.sid, owner.generation = "s1", GEN
        owner.record = {"profile": "xvfb", "display": 91}
        owner.setup_action_profile()
        owner.cache = w.StateCache("s1", GEN)
        owner.screen = {"w": 640, "h": 480}
        owner.cache.control = {"state": "agent", "epoch": 1}
        owner.cache.upsert(10, {"xid": 10, "family": "x:10", "title": "Fixture", "class": "Fixture", "mapped": True,
                               "geometry": {"x": 0, "y": 0, "w": 500, "h": 400}, "type": "normal", "is_modal": False,
                               "transient_for": None})
        self.handle = owner.cache.handle(10)
        owner.cache.set("focus", self.handle)
        owner.cache.set("stacking", [10])
        owner.cache.set("pointer", {"x": 2, "y": 2})
        owner.cache.baseline()
        owner.counters = dict.fromkeys(("batches", "steps", "verified", "not_observed", "contradicted", "unknown", "retries", "route_changes", "resync_drift", "false_closes"), 0)
        owner.journal = w.Journal(self.path / "acts" / (GEN + ".jsonl"), "s1", GEN)
        self.addCleanup(owner.journal.close)
        owner.rows = self.rows
        self.now = 100.0
        self.coverage = True
        owner.covered = lambda: self.coverage
        owner.polls = []
        owner.poll_control = lambda force=False: owner.polls.append(self.now)
        owner.timings = {}
        owner.lease = "lease"
        owner.lease_peer = self.peer = NS()
        owner.lease_until = None
        self.replies = []
        owner.respond = lambda peer, ident, result: self.replies.append((ident, copy.deepcopy(result)))
        self.t = FakeX(owner)
        owner.act = self.e = Engine(owner, self.t, clock=lambda: self.now)
        self.e.channel = {"state": "ok", "keyboard": "ok", "pointer": "ok"}
        self.e.attach("lease", self.peer, {})
        self.e.observed(self.peer, {}, {"full": True, "settled": True, "rev": 1})

    def rows(self):
        return [json.loads(line) for line in self.w.journal.path.read_text().splitlines()]

    def params(self, steps=None, **extra):
        return {"session": "s1", "generation": GEN, "lease": "lease", "control_epoch": 1,
                "action_id": "a", "base_rev": 1, "steps": steps or [self.move()], **extra}

    def move(self, x=20, y=20, **extra):
        return {"intent": {"kind": "move", "at": {"point": {"space": "desktop", "x": x, "y": y}}}, "timeout_ms": 50, **extra}

    def key(self, **extra):
        return {"intent": {"kind": "key", "keys": ["F2"]},
                "expect": {"kind": "window_present", "match": {"title": "Later"}}, "timeout_ms": 50, **extra}

    def submit(self, params=None, ident=1):
        self.e.submit(self.peer, ident, params or self.params())

    def tick(self):
        if hasattr(self, "metadata"):
            self.metadata.pump()
        self.w.cache.commit()
        self.e.tick()
        self.now += 0.001

    def drain(self, limit=30000):
        for _ in range(limit):
            if not self.e.active and not self.e.queue and not self.e.selftest:
                return
            self.tick()
        self.fail("action state machine did not finish")

    def result(self):
        return self.replies[-1][1]

    def run_action(self, p=None):
        self.submit(p)
        self.drain()
        return self.result()

    def later(self):
        self.w.cache.upsert(11, {"xid": 11, "family": "x:10", "title": "Later", "class": "Fixture", "mapped": True,
                                "geometry": {"x": 100, "y": 100, "w": 100, "h": 100}, "type": "normal", "is_modal": False,
                                "transient_for": None})
        self.w.cache.set("stacking", [10, 11])
        self.w.cache.commit()


class DialogMetadata(DeferredMetadata):
    """Real snapshot/derive path: active client arrives before its metadata."""
    def __init__(self, harness):
        super().__init__(harness)
        self.due = None
        self.appeared = False
        self.saw_unresolved_focus = False

    def reply(self, cls, args):
        wid = getattr(args.get("window"), "id", args.get("window"))
        if self.appeared and cls is w.request.GetInputFocus:
            return {"focus": 11}
        if cls is w.request.GetProperty:
            atom = args["property"]
            value = None
            if self.appeared and atom == self.x.atoms["_NET_ACTIVE_WINDOW"]:
                value = [11]
            elif wid == 11:
                if atom == self.x.atoms["_NET_WM_NAME"]:
                    return {"property_type": 1, "value": (8, b"Popup")}
                value = {self.x.atoms["WM_TRANSIENT_FOR"]: [10],
                         self.x.atoms["_NET_WM_STATE"]: [self.x.atoms["_NET_WM_STATE_MODAL"]],
                         self.x.atoms["_NET_WM_WINDOW_TYPE"]: [self.x.atoms["_NET_WM_WINDOW_TYPE_DIALOG"]]}.get(atom)
            if value is not None:
                return {"property_type": 1, "value": (32, value)}
        return super().reply(cls, args)

    def pump(self):
        if self.due is not None and self.h.now >= self.due and not self.appeared:
            self.appeared = True
            self.x.clients.add(11)
            self.x.focus_xid = self.x.input_focus = 11
            self.x.refresh(11)
        super().pump()
        if self.appeared and self.h.w.cache.focus is None and self.x.refreshing:
            self.saw_unresolved_focus = True


class ChurningDialogMetadata(DialogMetadata):
    """Mapped GTK-like client: classifiers readable, full metadata unfinished.

    Configure events advance revisions every pump turn. Frame ancestry replies
    remain outstanding beyond the step deadline, so a full snapshot cannot
    register the popup. This also prevents quiet whole-session coverage.
    """
    def __init__(self, harness):
        super().__init__(harness)
        self.x.ancestors = {}
        self.h.t.events = lambda events: None
        self.configures = 0
        self.popup_title = b"Popup"
        self.unrelated_change = False

    def ask(self, cls, callback, **args):
        if cls is w.request.QueryTree and args["window"] == 11:
            self.log.append((self.h.now, cls, args))
            self.pending.append((self.h.now + 5, callback, {"parent": 1}))
        else:
            super().ask(cls, callback, **args)

    def reply(self, cls, args):
        wid = getattr(args.get("window"), "id", args.get("window"))
        if cls is w.request.GetProperty and args["property"] == self.x.atoms["_NET_WM_NAME"]:
            if wid == 11 or wid == 12 and self.appeared and self.unrelated_change:
                title = self.popup_title if wid == 11 else b"Changed"
                return {"property_type": 1, "value": (8, title)}
        if cls is w.request.QueryTree and args["window"] is self.x.root:
            # A WM frame/check window must never count as a foreign client.
            return {"children": [*self.x.clients, 99], "parent": 1}
        if cls is w.request.GetGeometry and args["drawable"] == 11:
            return {"width": 100 + self.configures, "height": 100}
        return super().reply(cls, args)

    def pump(self):
        super().pump()
        if self.appeared:
            self.configures += 1
            self.x.events([NS(type=w.X.ConfigureNotify, window=11)])


class FixRound4(Harness):
    def click(self):
        return {"target": self.handle, "intent": {"kind": "click", "at": {"x": 20, "y": 30}},
                "expect": {"kind": "dialog_present", "match": {"title": "Popup"}}, "timeout_ms": 1000}

    def popup(self):
        self.w.cache.upsert(11, {"xid": 11, "family": "x:10", "title": "Popup", "class": "Fixture", "mapped": True,
                                "geometry": {"x": 100, "y": 100, "w": 100, "h": 100}, "type": "dialog",
                                "is_modal": True, "transient_for": 10})
        self.w.cache.set("focus", self.w.cache.handle(11))
        self.w.cache.set("modal", [self.w.cache.handle(11)])
        self.w.cache.set("stacking", [10, 11])
        self.w.cache.commit()

    def observe(self, **params):
        # Exercise production observe -> deferred QueryPointer -> pending ->
        # observed -> engine reconciliation, using only fake X and real journal.
        self.peer.waits = []
        self.w.peers = [self.peer]
        self.w.x = NS(root=1, uncovered=set(), ax=NS(ask=lambda cls, cb, **kw:
                      cb({"root_x": self.t.position["x"], "root_y": self.t.position["y"]})))
        self.w.observe(self.peer, 99, {"lease": "lease", **params})
        self.w.cache.commit()
        self.w.pending()
        return self.result()

    def assert_one_click(self):
        self.assertEqual([ev for ev in self.t.log if ev[0].startswith("button")],
                         [("button_down", 1), ("button_up", 1)])

    def test_dialog_arrival_modal_and_focus_verify(self):
        self.t.on_write = lambda ev: self.popup() if ev[0] == "button_up" else None
        r = self.run_action(self.params([self.click()]))
        self.assertEqual((r["status"], r["effect"]["status"]), ("success", "verified"))
        self.assertFalse(self.e.pending_effects)
        self.assert_one_click()

    def test_delayed_dialog_focus_before_metadata_is_not_contradiction(self):
        self.metadata = DialogMetadata(self)
        self.t.refresh = self.metadata.transport.refresh
        self.w.covered = lambda: not (self.metadata.pending or self.metadata.x.refreshing)
        def arm(ev):
            if ev[0] == "button_down":
                self.metadata.due = self.now + .3
        self.t.on_write = arm
        r = self.run_action(self.params([self.click()]))
        self.assertTrue(self.metadata.saw_unresolved_focus)
        self.assertEqual((r["status"], r["effect"]["status"]), ("success", "verified"))
        self.assertEqual(r["steps"][0]["focus"]["after"], self.w.cache.handle(11))
        self.assertEqual(r["steps"][0]["modal"]["after"], [self.w.cache.handle(11)])
        self.assert_one_click()

    def test_dialog_and_unrelated_new_window_still_contradicted(self):
        def change(ev):
            if ev[0] == "button_up":
                self.popup()
                self.w.cache.upsert(12, dict(self.w.cache.windows[10], xid=12, title="Unrelated"))
                self.w.cache.commit()
        self.t.on_write = change
        r = self.run_action(self.params([self.click()]))
        self.assertEqual((r["reason"], r["effect"]["status"]), ("effect_contradicted", "contradicted"))
        self.assert_one_click()

    def test_dialog_and_unrelated_window_title_change_still_contradicted(self):
        self.w.cache.upsert(12, dict(self.w.cache.windows[10], xid=12, title="Unrelated"))
        self.w.cache.baseline()
        def change(ev):
            if ev[0] == "button_up":
                self.popup()
                self.w.cache.upsert(12, dict(self.w.cache.windows[12], title="Changed"))
                self.w.cache.commit()
        self.t.on_write = change
        r = self.run_action(self.params([self.click()]))
        self.assertEqual((r["reason"], r["effect"]["status"]), ("effect_contradicted", "contradicted"))
        self.assert_one_click()

    def test_contradicted_observe_then_normal_act_same_lease(self):
        def change():
            self.later()
            self.w.cache.upsert(12, dict(self.w.cache.windows[10], xid=12, title="Unrelated"))
            self.w.cache.set("focus", self.w.cache.handle(12))
            self.w.cache.commit()
        self.t.hook = change
        original = self.run_action(self.params([self.key()]))
        self.assertEqual(original["effect"]["status"], "contradicted")
        self.assert_reconciles_and_moves(original)

    def test_unknown_observe_then_normal_act_same_lease(self):
        def change():
            self.later()
            self.w.cache.set("focus", self.w.cache.handle(11))
            self.w.cache.commit()
            self.w.cache.set("focus", self.handle)
            self.w.cache.commit()
        self.t.hook = change
        original = self.run_action(self.params([self.key()]))
        self.assertEqual(original["effect"]["status"], "unknown")
        self.assert_reconciles_and_moves(original)

    def assert_reconciles_and_moves(self, original):
        self.t.hook = None
        blocked = self.run_action(self.params(action_id="blocked", base_rev=self.w.cache.rev))
        self.assertEqual((blocked["reason"], blocked["advice"]), ("awaiting_reconciliation", "observe_first"))
        writes = list(self.t.log)
        observed = self.observe()
        self.assertTrue(observed["full"] and observed["settled"])
        self.assertFalse(self.e.pending_effects)
        self.assertEqual(self.t.log, writes)
        self.assertEqual(self.e.get(GEN, "a")["receipt"], original)
        self.assertEqual([row["type"] for row in self.rows()].count("reconciliation"), 1)
        r = self.run_action(self.params(action_id="resumed", base_rev=observed["rev"]))
        self.assertEqual(r["status"], "success", r.get("reason"))
        self.assertIsNone(r["reason"])
        self.assertFalse(self.e.lease_state["lease"]["suspended"])

    def test_observe_without_effect_proof_acknowledges_outcome(self):
        original = self.run_action(self.params([self.key()]))
        self.observe()
        r = self.run_action(self.params(action_id="acknowledged", base_rev=self.w.cache.rev))
        self.assertEqual((r["status"], r["reason"]), ("success", None))
        self.assertEqual(self.e.get(GEN, "a")["receipt"], original)
        self.assertFalse(self.e.lease_state["lease"]["suspended"])

    def test_reconciled_dialog_reports_modal_blocker_instead_of_budget(self):
        def change(ev):
            if ev[0] == "button_up":
                self.popup()
                self.w.cache.upsert(12, dict(self.w.cache.windows[10], xid=12, title="Unrelated"))
                self.w.cache.commit()
        self.t.on_write = change
        original = self.run_action(self.params([self.click()]))
        self.assertEqual(original["effect"]["status"], "contradicted")
        self.t.on_write = None
        observed = self.observe()
        self.assertFalse(self.e.pending_effects)
        writes = list(self.t.log)
        r = self.run_action(self.params([self.move(target=self.handle)], action_id="after-modal", base_rev=observed["rev"]))
        self.assertEqual((r["reason"], r["advice"]), ("modal_still_present", "stop"))
        self.assertEqual(self.t.log, writes)
        self.assertFalse(self.e.lease_state["lease"]["suspended"])

    def test_scoped_or_uncovered_observation_cannot_clear_pending(self):
        self.run_action(self.params([self.key()]))
        self.later()
        for params, extra in (({"scope": {"kind": "window", "window": self.handle}}, {}),
                              ({}, {"resync_required": True}), ({}, {"settled": False})):
            with self.subTest(params=params, extra=extra):
                self.w.observed(self.peer, params, {"full": True, "settled": True, "rev": self.w.cache.rev, **extra})
                self.assertTrue(self.e.pending_effects)
        self.observe()
        self.assertFalse(self.e.pending_effects)


class FixRound6(Harness):
    observe = FixRound4.observe

    def setUp(self):
        super().setUp()
        self.w.stopping = False
        self.w.versions = lambda: {"fake_x": True}

    def batch(self):
        return [self.move(id="m1"), self.key(id="k2", timeout_ms=100),
                self.key(id="t3", intent={"kind": "text", "text": "a"})]

    def stopped(self):
        original = self.run_action(self.params(self.batch()))
        self.assertEqual((original["status"], original["reason"]), ("partial", "timeout"))
        self.assertEqual([s["effect"]["status"] for s in original["steps"]],
                         ["verified", "not_observed", "not_observed"])
        self.assertEqual(original["unexecuted_steps"], ["t3"])
        self.assertEqual(original["steps"][1]["dispatch_status"], "accepted")
        self.assertTrue(all(a["server_ack"]["seen"] == a["server_ack"]["expected"]
                            for a in original["steps"][1]["attempts"]))
        return original

    def blocked(self):
        writes = list(self.t.log)
        r = self.run_action(self.params(action_id="blocked", generation=self.w.generation,
                                        lease=self.w.lease, base_rev=self.w.cache.rev))
        self.assertEqual((r["phase"], r["reason"]), ("validate", "awaiting_reconciliation"))
        self.assertEqual(writes, self.t.log)

    def admitted(self):
        r = self.run_action(self.params(action_id="resumed", generation=self.w.generation,
                                        lease=self.w.lease, base_rev=self.w.cache.rev))
        self.assertEqual((r["status"], r["reason"]), ("success", None))

    def restart(self):
        # New generation, cache and lease; retain the session's journal directory.
        self.w.journal.close()
        self.w.generation = "fedcba9876543210" * 2 if self.w.generation == GEN else "a" * 32
        self.w.journal = w.Journal(self.path / "acts" / (self.w.generation + ".jsonl"),
                                   self.w.sid, self.w.generation)
        self.addCleanup(self.w.journal.close)
        self.w.cache = w.StateCache(self.w.sid, self.w.generation)
        self.w.cache.control = {"state": "agent", "epoch": 1}
        self.w.cache.upsert(10, {"xid": 10, "family": "x:10", "title": "Fixture", "mapped": True,
                                 "geometry": {"x": 0, "y": 0, "w": 500, "h": 400},
                                 "type": "normal", "is_modal": False})
        self.w.cache.set("focus", self.w.cache.handle(10))
        self.w.cache.baseline()
        self.w.lease, self.w.lease_peer = None, None
        self.t = FakeX(self.w)
        self.w.act = self.e = Engine(self.w, self.t, clock=lambda: self.now)
        self.e.channel = {"state": "ok", "keyboard": "ok", "pointer": "ok"}
        self.w.message(self.peer, json.dumps({"jsonrpc": "2.0", "id": 90, "method": "session.attach",
                                             "params": {"client": "codex", "name": "round6"}}))
        self.assertIn("lease", self.result())

    def test_not_observed_batch_full_observe_reconciles_without_rewriting_receipt(self):
        original = self.stopped()
        self.blocked()
        writes = list(self.t.log)
        observed = self.observe()
        self.assertTrue(observed["full"] and observed["settled"])
        self.assertFalse(self.e.evaluate(original["steps"][1]["expect"]))
        self.assertEqual(self.t.log, writes)
        self.assertEqual(self.e.get(GEN, "a")["receipt"], original)
        rows = [row for row in self.rows() if row["type"] == "reconciled"]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["action_id"], rows[0]["action_generation"], rows[0]["rev"]),
                         ("a", GEN, observed["rev"]))
        self.admitted()

    def test_unchanged_delta_does_not_reconcile(self):
        self.stopped()
        observed = self.observe(since_rev=self.w.cache.rev)
        self.assertEqual((observed["full"], observed["settled"], observed["unchanged"]),
                         (False, True, True))
        self.blocked()
        self.assertFalse(any(row["type"] == "reconciled" for row in self.rows()))
        self.observe()
        self.admitted()

    def test_contradicted_false_postcondition_full_observe_reconciles(self):
        def change():
            self.w.cache.upsert(12, dict(self.w.cache.windows[10], xid=12, title="Unrelated"))
            self.w.cache.commit()
        self.t.hook = change
        original = self.run_action(self.params([self.key()]))
        self.assertEqual(original["effect"]["status"], "contradicted")
        self.assertFalse(self.e.evaluate(original["effect"]["predicate"]))
        self.t.hook = None
        self.blocked()
        self.observe()
        self.admitted()
        self.assertEqual(self.e.get(GEN, "a")["receipt"], original)

    def test_info_names_pending_action_and_returns_to_ready(self):
        self.stopped()
        self.w.message(self.peer, '{"jsonrpc":"2.0","id":88,"method":"system.info"}')
        info = self.result()
        self.assertEqual((info["worker"]["state"], info["worker"]["fence_reason"],
                          info["worker"]["pending_action_id"]),
                         ("awaiting_reconciliation", "awaiting_reconciliation", "a"))
        self.assertTrue(info["awaiting_reconciliation"])
        self.assertTrue(info["pending_effects"])
        self.observe()
        self.w.message(self.peer, '{"jsonrpc":"2.0","id":89,"method":"system.info"}')
        info = self.result()
        self.assertEqual((info["worker"]["state"], info["worker"]["fence_reason"],
                          info["worker"]["pending_action_id"]), ("ready", None, None))
        self.assertFalse(info["awaiting_reconciliation"])
        self.assertFalse(info["pending_effects"])

    def test_only_current_holder_full_settled_session_sample_reconciles(self):
        self.stopped()
        full = {"full": True, "settled": True, "rev": self.w.cache.rev}
        cases = [(NS(), {}, full), (self.peer, {"lease": "old"}, full),
                 (self.peer, {}, dict(full, full=False)), (self.peer, {}, dict(full, settled=False)),
                 (self.peer, {}, dict(full, resync_required=True)),
                 (self.peer, {"scope": {"kind": "window", "window": self.handle}}, full)]
        for peer, params, result in cases:
            with self.subTest(params=params, result=result):
                self.w.observed(peer, params, result)
                self.blocked()
        self.observe()
        self.admitted()

    def test_sigkill_after_ack_recovers_unknown_and_new_holder_reconciles(self):
        # Kill a real child running only the headless engine after the middle
        # key's XI2 ack, before its verification/receipt. No display is opened.
        pid = os.fork()
        if pid == 0:
            try:
                self.submit(self.params(self.batch()))
                for _ in range(1000):
                    self.tick()
                    action = self.e.actions[(GEN, "a")]
                    if action["receipt"]["steps"][1]["dispatch_status"] == "accepted":
                        os.kill(os.getpid(), signal.SIGKILL)
            finally:
                os._exit(2)
        _, status = os.waitpid(pid, 0)
        self.assertTrue(os.WIFSIGNALED(status))
        self.assertEqual(os.WTERMSIG(status), signal.SIGKILL)
        self.assertFalse(any(row["type"] == "receipt" for row in self.rows()))
        self.restart()
        self.assertEqual(self.w.health()["worker"]["pending_action_generation"], GEN)
        self.blocked()
        self.w.message(self.peer, json.dumps({"jsonrpc": "2.0", "id": 91, "method": "action.get",
                                             "params": {"generation": GEN, "action_id": "a"}}))
        original = self.result()["receipt"]
        self.assertEqual((original["status"], original["last_armed"]), ("unknown", "k2"))
        self.assertFalse(self.t.log)
        self.observe(lease=self.w.lease)
        self.assertEqual(w.Journal.action(self.path / "acts" / (GEN + ".jsonl"), "a")["receipt"], original)
        self.assertFalse(self.t.log)
        self.admitted()
        self.restart()
        self.assertFalse(self.e.pending_effects)  # acknowledgment survives another restart
        self.assertEqual(self.w.health()["worker"]["state"], "ready")

    def test_final_unresolved_receipt_survives_restart_until_full_observe(self):
        original = self.stopped()
        self.restart()
        self.blocked()
        self.observe(lease=self.w.lease, since_rev=self.w.cache.rev)
        self.blocked()
        self.observe(lease=self.w.lease)
        self.admitted()
        self.assertEqual(w.Journal.action(self.path / "acts" / (GEN + ".jsonl"), "a")["receipt"], original)

    def test_journal_failure_keeps_acknowledgment_fenced(self):
        self.stopped()
        with patch.object(self.w.journal, "append", side_effect=OSError("disk full")):
            with self.assertRaises(ActError):
                self.observe()
        self.assertTrue(self.e.pending_effects)
        self.assertTrue(self.e.fence_reason.startswith("F3"))

    def test_full_observe_acknowledges_only_oldest_action_in_its_generation(self):
        self.stopped()
        # Synthetic queue: normal stop-on-first-unverified prevents a second
        # unresolved action. Reused IDs across generations must remain distinct.
        second = dict(self.e.pending_effects[0], generation="b" * 32)
        self.e.pending_effects.append(second)
        self.observe()
        self.assertEqual(self.e.pending_effects, [second])
        self.blocked()
        self.observe()
        self.admitted()
        rows = [row for row in self.rows() if row["type"] == "reconciled"]
        self.assertEqual([(row["action_id"], row["action_generation"]) for row in rows],
                         [("a", GEN), ("a", "b" * 32)])

    def test_reconciled_final_action_does_not_return_on_restart(self):
        self.stopped()
        self.observe()
        self.restart()
        self.assertFalse(self.e.pending_effects)
        self.observe(lease=self.w.lease)
        self.admitted()


class FixRound5(Harness):
    click = FixRound4.click
    popup = FixRound4.popup
    assert_one_click = FixRound4.assert_one_click

    def churning_popup(self):
        self.metadata = ChurningDialogMetadata(self)
        self.t.refresh = self.metadata.transport.refresh
        self.w.covered = lambda: not (self.metadata.pending or self.metadata.x.refreshing)
        def arm(ev):
            if ev[0] == "button_down":
                self.metadata.due = self.now + .3
        self.t.on_write = arm
        step = self.click()
        step["timeout_ms"] = 4000
        return step

    def test_mapped_dialog_verifies_direct_classifiers_during_geometry_churn(self):
        r = self.run_action(self.params([self.churning_popup()]))
        self.assertTrue(self.metadata.saw_unresolved_focus)
        self.assertGreater(self.metadata.configures, 10)
        self.assertNotIn(11, self.w.cache.windows)  # No full metadata commit.
        self.assertFalse(self.w.covered())
        self.assertEqual((r["status"], r["effect"]["status"]), ("success", "verified"))
        self.assertLess(r["steps"][0]["timing_ms"]["total"], 600)
        self.assertIsNotNone(r["steps"][0]["rev"]["verified"])
        proof = r["steps"][0]["effect"]["evidence"][0]["comparison"]["classifiers"]
        popup = next(v for v in proof if v["xid"] == 11)
        self.assertEqual((popup["title"], popup["type"], popup["transient_for"]), ("Popup", "dialog", 10))
        self.assertTrue(popup["mapped"] and popup["is_modal"])
        self.assert_one_click()

    def test_window_appearance_also_verifies_direct_title_class_type(self):
        step = self.churning_popup()
        step["expect"] = {"kind": "window_present", "match": {"title": "Popup", "class": "Fixture", "type": "dialog"}}
        r = self.run_action(self.params([step]))
        self.assertNotIn(11, self.w.cache.windows)
        self.assertEqual((r["status"], r["effect"]["status"]), ("success", "verified"))
        self.assertLess(r["steps"][0]["timing_ms"]["total"], 600)
        self.assert_one_click()

    def test_complete_sample_with_null_focus_then_registered_dialog_verifies(self):
        started = self.now
        gaps = []
        def refresh():
            if self.now >= started + .6:
                self.popup()
            elif self.now >= started + .3:
                self.w.cache.set("focus", None)
                self.w.cache.commit()
                gaps.append(self.now)
        self.t.refresh_hook = refresh
        r = self.run_action(self.params([self.click()]))
        self.assertTrue(gaps)
        self.assertEqual((r["status"], r["effect"]["status"]), ("success", "verified"))
        self.assertGreaterEqual(r["steps"][0]["timing_ms"]["total"], 600)
        self.assertEqual(r["steps"][0]["focus"]["after"], self.w.cache.handle(11))
        self.assert_one_click()

    def test_null_focus_without_appearance_uses_full_four_second_window(self):
        def change(ev):
            if ev[0] == "button_up":
                self.w.cache.set("focus", None)
                self.w.cache.commit()
        self.t.on_write = change
        step = self.click()
        step["timeout_ms"] = 4000
        r = self.run_action(self.params([step]))
        self.assertEqual((r["reason"], r["effect"]["status"]), ("timeout", "not_observed"))
        self.assertGreaterEqual(r["steps"][0]["timing_ms"]["total"], 4000)
        self.assertLess(r["steps"][0]["timing_ms"]["total"], 4050)
        self.assertIsNone(r["steps"][0]["rev"]["verified"])
        self.assert_one_click()

    def test_direct_dialog_and_unrelated_title_change_still_contradicted(self):
        self.w.cache.upsert(12, dict(self.w.cache.windows[10], xid=12, title="Unrelated",
                                     app="Fixture", instance="fixture", leader=None))
        self.w.cache.set("stacking", [10, 12])
        self.w.cache.baseline()
        step = self.churning_popup()
        self.metadata.x.clients.add(12)
        self.metadata.unrelated_change = True
        r = self.run_action(self.params([step]))
        self.assertNotIn(11, self.w.cache.windows)
        self.assertEqual(self.w.cache.windows[12]["title"], "Changed")
        self.assertEqual((r["reason"], r["effect"]["status"]), ("effect_contradicted", "contradicted"))
        self.assert_one_click()

    def test_direct_different_arrival_during_churn_still_contradicted(self):
        step = self.churning_popup()
        self.metadata.popup_title = b"Different"
        r = self.run_action(self.params([step]))
        self.assertNotIn(11, self.w.cache.windows)
        self.assertEqual((r["reason"], r["effect"]["status"]), ("effect_contradicted", "contradicted"))
        self.assertLess(r["steps"][0]["timing_ms"]["total"], 600)
        self.assert_one_click()


class FixRound2(Harness):
    def click(self, **extra):
        return self.key(intent={"kind": "click", "at": {"x": 20, "y": 30}}, timeout_ms=5000, **extra)

    def use_metadata(self):
        self.metadata = DeferredMetadata(self)
        self.t.refresh = self.metadata.transport.refresh
        self.w.covered = lambda: not (self.metadata.pending or self.metadata.x.refreshing or self.metadata.x.uncovered)

    def assert_one_click(self, receipt):
        self.assertEqual([ev for ev in self.t.log if ev[0].startswith("button")],
                         [("button_down", 1), ("button_up", 1)])
        # These are motion, down and up, not three click-macro dispatches.
        self.assertEqual(len(receipt["steps"][0]["attempts"]), 3)
        self.assertTrue(all(t["server_ack"]["seen"] == t["server_ack"]["expected"]
                            for t in receipt["steps"][0]["attempts"]))
        self.assertEqual(self.w.counters["retries"], 0)

    def test_third_step_stall_preserves_verified_prefix_and_stop_id(self):
        steps = [self.move(id="first"), self.move(30, id="second"),
                 self.key(id="third"), self.move(40, id="fourth")]
        r = self.run_action(self.params(steps))
        self.assertEqual(r["status"], "partial")
        self.assertEqual((r["stopped_step"], r["stopped_at"]), ("third", 3))
        self.assertEqual(r["completed_steps"], ["first", "second"])
        for s in r["steps"][:2]:
            self.assertEqual(s["disposition"], "executed")
            self.assertEqual(s["effect"]["status"], "verified")
            self.assertIsNone(s["reason"])
        self.assertEqual(r["steps"][2]["reason"], "timeout")
        self.assertEqual(r["steps"][2]["effect"]["status"], "not_observed")
        self.assertEqual(r["steps"][3]["disposition"], "unexecuted")
        self.assertEqual(r["steps"][3]["reason"], "not_reached")
        self.assertNotIn(("motion", 0, 40, 20), self.t.log)
        self.assertEqual(r, self.rows()[-1]["receipt"])

    def test_nested_read_timeout_stays_on_third_step(self):
        # Exercise the outer catch independently: a read can also fail while
        # handling the first exception. It must still name the active step.
        verify = self.e.verify
        def failed_read(a, r, p, until):
            if r["id"] == "third":
                yield
                raise ActError("timeout")
            return (yield from verify(a, r, p, until))
        with patch.object(self.e, "verify", side_effect=failed_read):
            r = self.run_action(self.params([self.move(id="first"), self.move(30, id="second"),
                                             self.key(id="third"), self.move(40, id="fourth")]))
        self.assertEqual((r["stopped_step"], r["stopped_at"]), ("third", 3))
        self.assertEqual(r["completed_steps"], ["first", "second"])
        self.assertEqual([s["disposition"] for s in r["steps"]], ["executed", "executed", "executed", "unexecuted"])
        self.assertEqual([s["reason"] for s in r["steps"]], [None, None, "timeout", "not_reached"])
        self.assertEqual(r["effect"], r["steps"][2]["effect"])
        self.assertEqual(r, self.rows()[-1]["receipt"])

    def test_click_missing_title_notification_verifies_fresh_inside_timeout(self):
        self.use_metadata()
        def application(event):
            if event == ("button_up", 1):
                self.metadata.title = "Later"  # No cache update or X event.
                self.assertEqual(self.w.cache.windows[10]["title"], "Fixture")
        self.t.on_write = application
        r = self.run_action(self.params([self.click()]))
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["steps"][0]["effect"]["status"], "verified")
        self.assertLess(r["steps"][0]["timing_ms"]["total"], 200)
        self.assertEqual(self.w.cache.windows[10]["title"], "Later")
        self.assert_one_click(r)

    def test_click_absent_effect_never_replays_during_full_timeout(self):
        self.use_metadata()
        r = self.run_action(self.params([self.click()]))
        self.assertEqual(r["reason"], "timeout")
        self.assertEqual(r["effect"]["status"], "not_observed")
        self.assertGreaterEqual(r["steps"][0]["timing_ms"]["total"], 5000)
        self.assertLess(r["steps"][0]["timing_ms"]["total"], 5050)
        self.assertGreater(len([v for v in self.metadata.log if v[1] is w.request.GetProperty]), 20)
        self.assert_one_click(r)

    def test_click_final_look_waits_for_current_title_reply(self):
        self.use_metadata()
        started, refresh = self.now, self.t.refresh
        def final_effect(predicate, target, cb):
            if self.now >= started + 0.2:
                self.metadata.title = "Later"
            return refresh(predicate, target, cb)
        self.t.refresh = final_effect
        step = self.click()
        step["timeout_ms"] = 200
        r = self.run_action(self.params([step]))
        self.assertEqual(r["status"], "success")
        self.assertGreaterEqual(r["steps"][0]["timing_ms"]["total"], 200)
        self.assertLess(r["steps"][0]["timing_ms"]["total"], 250)
        self.assert_one_click(r)

    def test_refresh_drains_own_snapshot_before_callback(self):
        self.use_metadata()
        # An old event snapshot cannot substitute for this sample's own
        # requests. It need not gate the independent targeted read either.
        self.metadata.x.refresh(10)
        self.metadata.title = "Later"
        completed = []
        self.t.refresh({"kind": "window_present", "window": self.handle}, self.handle,
                       lambda _: completed.append((self.now, self.w.cache.windows[10]["title"],
                                                    bool(self.metadata.x.refreshing or self.metadata.pending))))
        started = self.now
        for _ in range(100):
            if completed:
                break
            self.tick()
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0][1:], ("Later", False))
        self.assertGreaterEqual(completed[0][0] - started, 0.012)
        self.assertFalse(self.t.log)

    def test_stalled_final_refresh_is_unknown_and_does_not_replay_click(self):
        self.use_metadata()
        self.metadata.title_delay = float("inf")
        step = self.click()
        step["timeout_ms"] = 200
        r = self.run_action(self.params([step]))
        self.assertEqual(r["effect"]["status"], "unknown")
        self.assertEqual(r["reason"], "provider_degraded")
        self.assertEqual((r["stopped_step"], r["stopped_at"]), ("1", 1))
        self.assertLess(r["steps"][0]["timing_ms"]["total"], 300)
        self.assert_one_click(r)
        queries = len(self.metadata.log)
        for _ in range(20):
            self.tick()
        self.assertEqual(len(self.metadata.log), queries)  # No orphan barrier loop.


class FixRound3(Harness):
    click = FixRound2.click
    use_metadata = FixRound2.use_metadata
    assert_one_click = FixRound2.assert_one_click

    def many_windows(self, named):
        for wid in range(11, 160):
            self.w.cache.upsert(wid, dict(self.w.cache.windows[10], xid=wid, title="Unrelated",
                                         geometry={"x": 510, "y": 410, "w": 10, "h": 10}))
        self.w.cache.set("stacking", list(self.w.cache.windows))
        self.w.cache.baseline()
        self.use_metadata()
        self.metadata.x.clients.update(range(11, 160))
        original = self.metadata.ask
        def ask(cls, callback, **args):
            # Unrelated event refreshes cannot complete within the step window.
            if cls is w.request.GetGeometry and args["drawable"] in range(11, 160):
                self.metadata.log.append((self.now, cls, args))
                self.metadata.pending.append((float("inf"), callback, self.metadata.reply(cls, args)))
            else:
                original(cls, callback, **args)
        self.metadata.ask = ask
        self.sample_logs = []
        refresh = self.t.refresh
        def sampled(predicate, target, cb):
            start = len(self.metadata.log)
            def done(result):
                self.sample_logs.append(self.metadata.log[start:])
                cb(result)
            return refresh(predicate, target, done)
        self.t.refresh = sampled
        def application(event):
            if event == ("button_down", 1):
                # The real fixture renamed ~45 ms after press, with no notify.
                self.metadata.pending.append((self.now + 0.045,
                                              lambda _: setattr(self.metadata, "title", "Later"), None))
            if event == ("button_up", 1):
                for wid in range(11, 160):
                    self.metadata.x.refresh(wid)
        self.t.on_write = application
        step = self.click()
        if named:
            step["expect"]["window"] = self.handle
        return step

    def test_title_with_150_windows_ignores_unrelated_metadata_drain(self):
        step = self.many_windows(named=True)
        r = self.run_action(self.params([step]))
        self.assertEqual(r["status"], "success")
        self.assertLess(r["steps"][0]["timing_ms"]["total"], 200)
        self.assertFalse(self.w.covered())  # Unrelated metadata is still blocked.
        self.assert_one_click(r)
        queried = {getattr(a.get("window", a.get("drawable", a.get("src_wid"))), "id",
                           a.get("window", a.get("drawable", a.get("src_wid"))))
                   for _, _, a in self.sample_logs[0]}
        self.assertLessEqual(queried, {None, 1, 10})

    def test_match_probes_only_classifiers_then_snapshots_matching_windows(self):
        step = self.many_windows(named=False)
        step["expect"]["match"].update({"class": "Fixture", "type": "normal"})
        r = self.run_action(self.params([step]))
        self.assertEqual(r["status"], "success")
        self.assertLess(r["steps"][0]["timing_ms"]["total"], 200)
        self.assert_one_click(r)
        allowed = {self.metadata.x.atoms[n] for n in
                   ("_NET_WM_NAME", "WM_NAME", "WM_CLASS", "_NET_WM_WINDOW_TYPE")}
        for _, cls, args in self.sample_logs[0]:
            wid = args.get("window", args.get("drawable", args.get("src_wid")))
            if wid in range(11, 160):
                self.assertIn(cls, (w.request.GetProperty, w.request.GetWindowAttributes))
                if cls is w.request.GetProperty:
                    self.assertIn(args["property"], allowed)

    def test_sample_waits_for_own_recursive_ancestry_but_not_other_refresh(self):
        self.use_metadata()
        self.metadata.x.ancestors = {}
        original = self.metadata.ask
        def ask(cls, callback, **args):
            if cls is w.request.QueryTree and args["window"] in (10, 20):
                parent = 20 if args["window"] == 10 else 1
                self.metadata.pending.append((self.now + 0.018, callback, {"parent": parent}))
            else:
                original(cls, callback, **args)
        self.metadata.ask = ask
        self.metadata.title = "Later"
        completed = []
        self.t.refresh({"kind": "window_present", "window": self.handle}, self.handle,
                       lambda result: completed.append((self.now, result)))
        started = self.now
        for _ in range(50):
            self.tick()
            if completed:
                break
        self.assertEqual(len(completed), 1)
        self.assertTrue(completed[0][1]["complete"])
        self.assertGreaterEqual(completed[0][0] - started, 0.036)
        self.assertEqual(self.metadata.x.ancestors[10], (20,))


class RestartAdoption(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix=".worker-restart-", dir=WM.parent)
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.record = {"session": "s1", "session_dir": str(self.path), "display": 91, "profile": "xvfb",
                       "home": str(self.path / "home"), "runtime_dir": str(self.path / "runtime"),
                       "bus_address": "unix:path=fake", "atspi_address": "unix:path=fake"}
        (self.path / "home").mkdir()
        (self.path / "sessions.json").write_text('{"s1":{}}')
        (self.path / "openbox.json").write_text(json.dumps({"wm_pid": 321, "wm_starttime": "old-wm"}))
        w.initialise_control(self.path / "control", GEN)
        with (self.path / "control").open("r+b") as stream:
            stream.write(w.encode_control(GEN, epoch=7, state=w.STATES["paused"]))
        self.present, self.live = True, True
        self.stamp = "old-wm"
        self.selection, self.supporting, self.redirect = True, True, True
        self.pid_queries = []
        self.cmdline = b"/usr/bin/openbox\0--sm-disable\0--config-file\0" + os.fsencode(WM / "openbox-rc.xml") + b"\0"
        self.env = os.fsencode(f"ARK_SESSIOND={self.path}\0ARK_SESSION_ID=s1\0DISPLAY=:91\0")
        self.spawns = []

    def run_start(self):
        real_bytes = Path.read_bytes
        def read_bytes(path):
            if str(path) == "/proc/321/cmdline":
                return self.cmdline
            if str(path) == "/proc/321/environ":
                return self.env
            return real_bytes(path)
        def identity(pid):
            return self.stamp if pid == 321 and self.live else "new-wm" if pid == 322 else None
        def launch(args, **kwargs):
            self.spawns.append(args)
            if args[0] == "/usr/bin/openbox":
                return NS(pid=322)
            generation = args[args.index("--_generation") + 1]
            owner = self.owner = w.Worker.__new__(w.Worker)
            owner.state, owner.sd, owner.sid = self.path, self.path, "s1"
            owner.record, owner.generation = self.record, generation
            owner.openbox = None
            owner.timings, owner.elapsed = {}, lambda: 1
            owner.cache = w.StateCache("s1", generation)
            xs = self.x = w.XState.__new__(w.XState)
            xs.w, xs.phase, xs.root = owner, "versions", Mock(id=1)
            xs.atoms = {name: i + 100 for i, name in enumerate(w.ATOM_NAMES)}
            xs.wm_ready, xs.initialised = False, False
            xs.refreshing = set()
            xs.inp_ax = NS(pending=[])
            xs.reconcile = Mock()
            xs.derive = Mock()
            def ask(cls, callback, **kw):
                if cls is w.request.GetSelectionOwner:
                    data = {"owner": 50 if self.present and self.selection else 0}
                elif cls is w.request.GetWindowAttributes:
                    data = {"all_event_masks": w.X.SubstructureRedirectMask if self.present and self.redirect else 0}
                elif cls is w.request.GetProperty:
                    if kw["property"] == xs.atoms["_NET_WM_PID"]:
                        self.pid_queries.append(kw["window"])
                        value = []  # Real Openbox check/selection owners.
                    else:
                        value = [50] if self.present and self.supporting or owner.openbox else []
                    data = {"property_type": 1, "value": (32, value)}
                else:
                    raise AssertionError(cls)
                callback(data)
            xs.ax = NS(pending=[], ask=ask)
            result = {"generation": generation}
            try:
                with patch.object(w, "Engine", return_value=NS(t=NS(initialised=False))), \
                        patch.object(w, "XTestTransport", return_value=NS(initialised=False)):
                    xs.advance()
                self.assertTrue(xs.initialised)
                self.assertEqual(xs.phase, "channel_setup")
                xs.reconcile.assert_called_once_with()
                result["state"] = "ready"
            except RuntimeError as exc:
                result.update(state="failed", error=str(exc))
            os.write(kwargs["pass_fds"][0], json.dumps(result).encode())
            return Mock(pid=444)
        with patch.object(w, "session_record", return_value=self.record), \
                patch.object(w, "worker_metadata", return_value=None), \
                patch.object(w, "proc_identity", side_effect=identity), \
                patch.object(Path, "read_bytes", read_bytes), \
                patch.object(w.subprocess, "Popen", side_effect=launch), \
                patch.object(w.os, "killpg"), patch.object(w, "wait_pid", return_value=True):
            return w.start(self.path, "s1")

    def test_recorded_live_wm_adopted_in_new_generation_with_control_preserved(self):
        result = self.run_start()
        self.assertEqual(result["state"], "ready")
        self.assertNotEqual(result["generation"], GEN)
        self.assertIsInstance(self.owner.openbox, w.AdoptedOpenbox)
        self.assertEqual(self.owner.openbox.pid, 321)
        self.assertEqual(len(self.spawns), 1)  # Daemon only; no second WM.
        self.assertEqual(self.pid_queries, [])
        control = w.decode_control((self.path / "control").read_bytes(), result["generation"])
        self.assertEqual((control["state"], control["epoch"]), (w.STATES["paused"], 8))

    def test_dead_recorded_pid_with_wm_present_refused(self):
        self.live = False
        self.assertEqual(self.run_start()["error"], "wm_already_running")
        self.assertEqual(len(self.spawns), 1)

    def test_stale_starttime_refused_even_with_recorded_pid_alive(self):
        self.stamp = "reused-pid"
        self.assertEqual(self.run_start()["error"], "wm_already_running")
        self.assertEqual(len(self.spawns), 1)

    def test_redirect_mask_alone_with_valid_record_adopted(self):
        self.selection = self.supporting = False
        self.assertEqual(self.run_start()["state"], "ready")
        self.assertIsInstance(self.owner.openbox, w.AdoptedOpenbox)
        self.assertEqual(len(self.spawns), 1)
        self.assertEqual(self.pid_queries, [])

    def test_redirect_mask_alone_without_valid_record_refused(self):
        self.selection = self.supporting = False
        self.stamp = "reused-pid"
        self.assertEqual(self.run_start()["error"], "wm_already_running")
        (self.path / "openbox.json").unlink()
        self.assertEqual(self.run_start()["error"], "wm_already_running")
        self.assertEqual(self.pid_queries, [])

    def test_different_cmdline_or_session_refused(self):
        original_cmdline, original_env = self.cmdline, self.env
        for cmdline, env in ((b"/usr/bin/foreign-wm\0", original_env),
                             (original_cmdline + b"--extra\0", original_env),
                             (original_cmdline, original_env.replace(b"DISPLAY=:91", b"DISPLAY=:92")),
                             (original_cmdline, original_env.replace(b"ARK_SESSION_ID=s1", b"ARK_SESSION_ID=s2")),
                             (original_cmdline, original_env.replace(os.fsencode(self.path), b"/foreign")),
                             (original_cmdline, b"DISPLAY=:91\0")):
            with self.subTest(cmdline=cmdline, env=env):
                self.cmdline, self.env = cmdline, env
                self.assertEqual(self.run_start()["error"], "wm_already_running")

    def test_no_wm_spawns_and_records_openbox_as_before(self):
        self.present = self.live = False  # A clean stop left no live WM.
        self.assertEqual(self.run_start()["state"], "ready")
        self.assertEqual(len(self.spawns), 2)
        self.assertEqual(self.spawns[1][0], "/usr/bin/openbox")
        self.assertEqual(json.loads((self.path / "openbox.json").read_text()),
                         {"wm_pid": 322, "wm_starttime": "new-wm"})

    def test_first_start_without_prior_identity_spawns(self):
        (self.path / "openbox.json").unlink()
        (self.path / "control").unlink()
        self.present = False
        result = self.run_start()
        self.assertEqual(result["state"], "ready")
        self.assertEqual(len(self.spawns), 2)
        control = w.decode_control((self.path / "control").read_bytes(), result["generation"])
        self.assertEqual((control["state"], control["epoch"]), (w.STATES["agent"], 1))

    def test_present_wm_without_record_refused(self):
        (self.path / "openbox.json").unlink()
        self.assertEqual(self.run_start()["error"], "wm_already_running")
        self.assertEqual(len(self.spawns), 1)

    def test_worker_metadata_record_can_supply_previous_wm_identity(self):
        (self.path / "openbox.json").rename(self.path / "worker.json")
        self.assertEqual(self.run_start()["state"], "ready")
        self.assertEqual(len(self.spawns), 1)

    def test_adopted_lifecycle_checks_identity_before_signalling_nonchild(self):
        own = w.AdoptedOpenbox(321, "old-wm")
        with patch.object(w, "proc_identity", return_value="old-wm") as identity, \
                patch.object(w.os, "kill") as kill, patch.object(w.os, "waitpid") as reap:
            self.assertIsNone(own.poll())
            own.terminate()
            own.kill()
            self.assertEqual([c.args for c in kill.call_args_list],
                             [(321, w.signal.SIGTERM), (321, w.signal.SIGKILL)])
            identity.return_value = "reused-pid"
            self.assertEqual(own.poll(), 0)
            own.terminate()
            self.assertEqual(own.wait(), 0)
            self.assertEqual(kill.call_count, 2)
            reap.assert_not_called()


class Validation(Harness):
    def test_batch_schema_limits_before_intent(self):
        for mutate in (lambda p: p.update(steps=[]), lambda p: p.update(steps=[self.move()] * 9),
                       lambda p: p.update(deadline_ms=10001), lambda p: p.update(base_rev=True),
                       lambda p: p.update(extra=1), lambda p: p["steps"][0].update(timeout_ms=49),
                       lambda p: p["steps"][0].update(extra=True),
                       lambda p: p["steps"][0]["intent"].update(count=2)):
            p = self.params()
            mutate(p)
            with self.assertRaises(ActError):
                self.submit(p)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.t.log, [])

    def test_default_deadline_and_duplicate_ids(self):
        p, _ = self.e.normalise(self.params())
        self.assertEqual(p["deadline_ms"], 550)
        with self.assertRaises(ActError):
            self.e.normalise(self.params([self.move(id="same"), self.move(id="same")]))
        with self.assertRaises(ActError):
            self.e.normalise(self.params([self.move(timeout_ms=5000)] * 2))

    def test_lease_generation_and_epoch(self):
        for field, value in (("lease", "bad"), ("generation", "f" * 32)):
            with self.assertRaises(w.RPCError):
                self.submit(self.params(**{field: value}))
        r = self.run_action(self.params(control_epoch=2))
        self.assertEqual(r["dispatch_status"], "not_dispatched")
        self.assertEqual(r["reason"], "preempted")
        self.assertFalse(self.rows())

    def test_full_observation_required(self):
        self.e.lease_state["lease"]["observed"] = None
        r = self.run_action()
        self.assertEqual(r["reason"], "precondition_failed")
        self.assertFalse(self.t.log)

    def test_pending_dedupe_and_conflict(self):
        p = self.params()
        self.submit(p)
        self.submit(copy.deepcopy(p), 2)
        self.assertEqual(self.result()["status"], "pending")
        self.assertEqual(len(self.e.queue), 1)
        changed = self.params([self.move(21)])
        with self.assertRaisesRegex(ActError, "action_id_conflict"):
            self.submit(changed)
        self.drain()
        before = list(self.t.log)
        r = copy.deepcopy(self.result())
        self.submit(p)
        self.assertEqual(self.result(), r)
        self.assertEqual(self.t.log, before)
        self.assertEqual([v["type"] for v in self.rows()].count("intent"), 1)

    def test_canonical_key_order_dedupe(self):
        p = self.params()
        r = self.run_action(p)
        self.submit(dict(reversed(list(p.items()))))
        self.assertEqual(self.result(), r)

    def test_queue_full_and_expired_never_dispatch(self):
        self.submit(self.params(action_id="active"))
        self.tick()
        for i in range(8):
            self.submit(self.params(action_id=f"q{i}", deadline_ms=1))
        self.submit(self.params(action_id="overflow"))
        self.assertEqual(self.result()["reason"], "queue_full")
        self.now += 0.1
        self.drain()
        for i in range(8):
            result = self.e.get(GEN, f"q{i}")["receipt"]
            self.assertEqual(result["reason"], "timeout")
            self.assertFalse(result["dispatched"])
        self.assertEqual(sum(ev[0] == "motion" for ev in self.t.log), 1)


class Dispatch(Harness):
    def test_targetless_move_over_desktop_has_no_pending_effect(self):
        self.t.child = 0
        step = self.move(600, 450)
        step["intent"]["at"]["point"]["at_rev"] = 1
        r = self.run_action(self.params([step]))
        sr = r["steps"][0]
        self.assertEqual(r["status"], "success")
        self.assertEqual(sr["dispatch_status"], "accepted")
        self.assertEqual(sr["effect"]["status"], "verified")
        self.assertEqual(sr["expect"], {"kind": "pointer_at", "point": {"x": 600, "y": 450}})
        self.assertEqual(sr["expect_source"], "default")
        self.assertIsNone(sr["target_resolved"])
        self.assertIsNone(sr["target"])
        self.assertNotIn("geometry_guard", sr)
        ack = sr["attempts"][0]["server_ack"]
        self.assertEqual((ack["seen"], ack["expected"]), (1, 1))
        self.assertEqual(self.t.log, [("motion", 0, 600, 450)])
        self.assertFalse(r["pending_effects"])
        self.assertFalse(self.e.pending_effects)
        # A later action must still pass admission and verification.
        r = self.run_action(self.params([self.move(610, 450)], action_id="next", base_rev=self.w.cache.rev))
        self.assertEqual(r["status"], "success")
        self.assertFalse(self.e.pending_effects)

    def test_targetless_move_over_nonfocused_window_does_not_raise(self):
        self.later()
        other = self.w.cache.handle(11)
        self.t.child = 11
        r = self.run_action(self.params([self.move(150, 150)], base_rev=self.w.cache.rev))
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["steps"][0]["target_resolved"], other)
        self.assertEqual(r["steps"][0]["effect"]["status"], "verified")
        self.assertEqual(self.t.log, [("motion", 0, 150, 150)])
        self.assertEqual(self.w.cache.focus, self.handle)
        self.assertFalse(self.e.pending_effects)

    def test_targetless_pointer_resolution_uses_topmost_mapped_geometry(self):
        self.later()
        other = self.w.cache.handle(11)
        for kind in ("move", "click", "scroll", "drag"):
            with self.subTest(kind=kind):
                intent = {"kind": kind, "from" if kind == "drag" else "at": {"x": 150, "y": 150}}
                step = {"intent": intent, "require": {}}
                self.assertEqual(self.e.resolve(step), (other, {"x": 150, "y": 150}))
                self.w.cache.set("stacking", [11, 10])
                self.assertEqual(self.e.resolve(step)[0], self.handle)
                self.w.cache.set("stacking", [10, 11])
                for target in (self.handle, {"window": self.handle}):
                    self.assertEqual(self.e.resolve(dict(step, target=target))[0], self.handle)
                self.w.cache.upsert(11, dict(self.w.cache.windows[11], mapped=False))
                self.assertEqual(self.e.resolve(step)[0], self.handle)
                intent["from" if kind == "drag" else "at"] = {"x": 600, "y": 450}
                self.assertIsNone(self.e.resolve(step)[0])
                self.w.cache.upsert(11, dict(self.w.cache.windows[11], mapped=True))

    def test_pointer_hit_test_uses_cached_root_frame_stacking(self):
        self.later()
        xs = w.XState.__new__(w.XState)
        xs.w, xs.root = self.w, NS(id=1)
        xs.focus_xid = xs.input_focus = 10
        xs.ancestors = {10: (100,), 11: (110,)}
        xs.refresh = Mock()
        for children, expected in (([110, 100], self.handle), ([100, 110], self.w.cache.handle(11))):
            xs.root_tree({"children": children})
            xs.derive()
            step = {"intent": {"kind": "move", "at": {"x": 150, "y": 150}}, "require": {}}
            self.assertEqual(self.e.resolve(step)[0], expected)

    def test_explicit_click_still_raises_once_then_fails_occluded(self):
        self.later()
        self.t.child = 11
        original = self.t.wm
        def covered(kind, wid):
            original(kind, wid)
            self.t.child = 11  # Cover remains above the explicit target.
        self.t.wm = covered
        step = self.key(target=self.handle, intent={"kind": "click", "at": {"x": 150, "y": 150}},
                        expect={"kind": "window_present", "match": {"title": "After click"}})
        r = self.run_action(self.params([step], base_rev=self.w.cache.rev))
        self.assertEqual(r["reason"], "precondition_failed")
        self.assertEqual(r["steps"][0]["detail"], "occluded")
        self.assertEqual(r["steps"][0]["effect"]["status"], "unknown")
        self.assertEqual(r["steps"][0]["target_resolved"], self.handle)
        self.assertEqual(self.t.log, [("motion", 0, 150, 150), ("raise_window", 10)])
        self.assertEqual(self.w.counters["retries"], 1)

    def test_targetless_key_keeps_focused_window(self):
        self.t.hook = self.later
        r = self.run_action(self.params([self.key()]))
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["steps"][0]["target_resolved"], self.handle)
        self.assertEqual(self.t.log, [("key_down", 68), ("key_up", 68)])

    def test_targetless_desktop_move_coordinate_miss_reissues_once(self):
        self.t.child = 0
        motions = []
        def miss_once(event):
            if event[0] == "motion":
                motions.append(event)
                if len(motions) == 1:
                    self.t.position = {"x": 2, "y": 2}
                    self.w.cache.set("pointer", dict(self.t.position))
        self.t.on_write = miss_once
        r = self.run_action(self.params([self.move(600, 450)]))
        self.assertEqual(r["status"], "success")
        self.assertEqual(self.t.log, [("motion", 0, 600, 450)] * 2)
        self.assertEqual(self.w.counters["retries"], 1)
        self.assertFalse(self.e.pending_effects)

    def test_drag_unresolved_destination_never_positions_or_presses(self):
        s = self.key(intent={"kind": "drag", "from": {"point": {"x": 20, "y": 20}}, "to": {"ref": "n1"}})
        r = self.run_action(self.params([s]))
        self.assertEqual(r["reason"], "provider_degraded")
        self.assertFalse(r["dispatched"])
        self.assertFalse(self.t.log)

    def test_target_disappearing_during_focus_is_not_worker_fault(self):
        self.w.cache.focus = None
        self.t.focus_works = False
        original = self.t.wm
        def destroy(kind, wid):
            original(kind, wid)
            self.w.cache.remove(wid)
        self.t.wm = destroy
        r = self.run_action(self.params([self.key(target=self.handle, timeout_ms=500)]))
        self.assertEqual(r["reason"], "target_not_found")
        self.assertFalse(r["dispatched"])
        self.assertIsNone(self.e.fence_reason)

    def test_intent_and_arm_fsync_before_transport(self):
        fsync = os.fsync
        calls = []
        with patch.object(w.os, "fsync", side_effect=lambda fd: (calls.append(fd), fsync(fd))[1]):
            r = self.run_action()
        self.assertEqual(r["status"], "success")
        self.assertEqual([v["type"] for v in self.rows()], ["intent", "step_armed", "receipt"])
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.t.position, {"x": 20, "y": 20})
        self.assertEqual(self.t.down, set())

    def test_stop_first_unverified_suffix_not_reached(self):
        r = self.run_action(self.params([self.move(), self.key(), self.move(100)]))
        self.assertEqual(r["status"], "partial")
        self.assertEqual(r["stopped_at"], 2)
        self.assertEqual(r["effect"]["status"], "not_observed")
        self.assertEqual(r["steps"][2]["reason"], "not_reached")
        self.assertEqual(r["steps"][2]["stop_reason"], "prior_step_stopped")
        self.assertNotIn(("motion", 0, 100, 20), self.t.log)
        self.assertEqual(r["advice"], "observe_first")
        self.assertIn("refresh", self.t.query_log)

    def test_key_chord_order_and_server_ack(self):
        self.t.hook = self.later
        step = self.key(intent={"kind": "key", "keys": ["ctrl", "a"]})
        r = self.run_action(self.params([step]))
        self.assertEqual(r["status"], "success")
        self.assertEqual(self.t.log, [("key_down", 37), ("key_down", 38), ("key_up", 38), ("key_up", 37)])
        ack = r["steps"][0]["attempts"][0]["server_ack"]
        self.assertEqual((ack["expected"], ack["seen"]), (4, 4))
        self.assertEqual(self.t.down, set())

    def test_key_chord_invalid_and_missing_keysym(self):
        for keys in (["a", "b"], ["Control_L"], ["a", "Control_L"], ["ctrl+a"], ["ctrl", "ctrl", "a"]):
            with self.assertRaises(ActError):
                chord(keys)
        r = self.run_action(self.params([self.key(intent={"kind": "key", "keys": ["NotAKeysym"]})]))
        self.assertEqual(r["reason"], "invalid_request")
        self.assertFalse(r["dispatched"])
        self.assertFalse(self.t.log)

    def test_text_chunks_with_gate_and_select_all(self):
        calls = 0
        def effect():
            nonlocal calls
            calls += 1
            if calls >= 4:
                self.later()
        self.t.hook = effect
        step = self.key(intent={"kind": "text", "text": "a" * 65, "replace": True})
        r = self.run_action(self.params([step]))
        self.assertEqual(r["status"], "success")
        self.assertEqual([len(v) for v in self.t.bursts], [4, 64, 64, 2])
        self.assertGreater(len(self.w.polls), 130)
        self.assertEqual(self.t.down, set())

    def test_unmappable_text_no_prefix_and_no_focus_or_motion(self):
        r = self.run_action(self.params([self.key(intent={"kind": "text", "text": "aa😀aa", "replace": True})]))
        self.assertEqual((r["reason"], r["steps"][0]["detail"]), ("backend_unsupported", "unmappable_text"))
        self.assertFalse(r["dispatched"])
        self.assertFalse(self.t.log)
        self.assertNotIn("step_armed", [v["type"] for v in self.rows()])

    def test_text_controls_require_opt_in(self):
        for c in ("\n", "\t"):
            with self.assertRaises(ActError):
                self.submit(self.params([self.key(intent={"kind": "text", "text": c})]))
        self.assertFalse(self.rows())

    def test_missing_expect_and_reaction_do_not_dispatch(self):
        for kind in ("key", "text", "click"):
            s = self.key()
            s.pop("expect")
            s["intent"] = {"kind": kind, **({"keys": ["F2"]} if kind == "key" else {"text": "a"} if kind == "text" else {"at": {"point": {"x": 2, "y": 2}}})}
            r = self.run_action(self.params([s], action_id=kind))
            self.assertEqual(r["reason"], "invalid_expectation")
        with self.assertRaises(ActError):
            self.submit(self.params([self.key(expect={"kind": "reaction"})]))
        self.assertFalse(self.t.log)

    def test_focus_fix_cached_and_two_attempts(self):
        self.w.cache.focus = None
        self.t.hook = self.later
        r = self.run_action(self.params([self.key(target=self.handle)]))
        self.assertEqual(r["status"], "success")
        self.assertEqual(self.t.log[0], ("focus_window", 10))
        # Fresh harness conditions with failed confirmation.
        self.e.pending_effects.clear()
        self.w.cache.remove(11)
        self.w.cache.commit()
        self.w.cache.focus = None
        self.t.focus_works = False
        self.t.log.clear()
        self.t.hook = None
        p = self.params([self.key(timeout_ms=500)], action_id="fail-focus", base_rev=self.w.cache.rev)
        # name the intended target because no cached focus exists
        p["steps"][0]["target"] = self.handle
        r = self.run_action(p)
        self.assertEqual(r["reason"], "focus_failed")
        self.assertFalse(r["dispatched"])
        self.assertEqual(self.t.log, [("focus_window", 10), ("focus_window", 10)])

    def test_pointer_position_raise_once_before_click(self):
        self.t.child = 999
        self.t.hook = lambda: None
        step = self.key(intent={"kind": "click", "at": {"point": {"x": 20, "y": 30}}, "count": 3}, timeout_ms=500)
        # Change the expected state only after button submission.
        self.t.on_write = lambda ev: self.later() if ev[0] == "button_down" else None
        r = self.run_action(self.params([step]))
        self.assertEqual(r["status"], "success")
        self.assertEqual([ev[0] for ev in self.t.log], ["motion", "raise_window", "button_down", "button_up", "button_down", "button_up", "button_down", "button_up"])
        self.assertEqual(self.t.buttons, set())
        self.assertGreaterEqual(self.now - 100, .2)

    def test_scroll_buttons_and_drag_sampling(self):
        self.t.on_write = lambda ev: self.later() if ev[0] == "button_down" else None
        step = self.key(intent={"kind": "scroll", "at": {"point": {"x": 20, "y": 20}}, "dy": -2, "dx": 1})
        r = self.run_action(self.params([step]))
        self.assertEqual(r["status"], "success")
        self.assertEqual([d for k, d, *xy in self.t.log if k == "button_down"], [4, 4, 7])
        self.t.log.clear()
        self.w.cache.remove(11)
        self.w.cache.commit()
        step = self.key(intent={"kind": "drag", "from": {"point": {"x": 20, "y": 20}}, "to": {"x": 80, "y": 80}}, timeout_ms=200)
        r = self.run_action(self.params([step], action_id="drag", base_rev=self.w.cache.rev))
        self.assertEqual(r["status"], "success")
        motions = [ev for ev in self.t.log if ev[0] == "motion"]
        self.assertGreaterEqual(len(motions), 10)
        self.assertEqual(motions[-1], ("motion", 0, 80, 80))
        self.assertFalse(self.t.buttons)

    def test_wm_close_and_focus_defaults(self):
        s = {"target": self.handle, "intent": {"kind": "close_window"}, "timeout_ms": 50}
        r = self.run_action(self.params([s]))
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["steps"][0]["route"], "wm")
        self.assertNotIn(10, self.w.cache.windows)

    def test_wait_dispatches_nothing(self):
        s = {"intent": {"kind": "wait"}, "expect": {"kind": "window_present", "window": self.handle}}
        r = self.run_action(self.params([s]))
        self.assertEqual(r["status"], "success")
        self.assertFalse(r["dispatched"])
        self.assertFalse(self.t.log)
        self.assertEqual([v["type"] for v in self.rows()], ["intent", "receipt"])

    def test_invoke_and_refs_undispatched(self):
        for i, s in enumerate((self.key(target={"window": self.handle, "ref": "n1"}),
                               self.key(require={"ref": "n1"}), self.key(intent={"kind": "invoke", "ref": "n1", "action": "press"}))):
            r = self.run_action(self.params([s], action_id=str(i)))
            self.assertEqual(r["reason"], "provider_degraded")
            self.assertFalse(r["dispatched"])
        self.assertFalse(self.t.log)


class FreshnessAndPredicates(Harness):
    def test_verified_move_stops_on_unexpected_focus(self):
        def change(ev):
            if ev[0] == "motion":
                self.later()
                self.w.cache.set("focus", self.w.cache.handle(11))
                self.w.cache.commit()
        self.t.on_write = change
        r = self.run_action(self.params([self.move(), self.move(30)]))
        self.assertEqual(r["reason"], "unexpected_state")
        self.assertEqual(r["steps"][0]["effect"]["status"], "verified")
        self.assertEqual(r["steps"][1]["reason"], "not_reached")

    def test_focus_leave_and_return_is_honest_unknown(self):
        def change():
            self.later()
            self.w.cache.set("focus", self.w.cache.handle(11))
            self.w.cache.commit()
            self.w.cache.set("focus", self.handle)
            self.w.cache.commit()
        self.t.hook = change
        r = self.run_action(self.params([self.key()]))
        self.assertEqual(r["effect"]["status"], "unknown")
        self.assertEqual(r["reason"], "focus_lost")

    def test_stale_vs_false_precondition(self):
        self.w.cache.facets["focus"] = 2
        self.w.cache.rev = 2
        r = self.run_action()
        self.assertEqual(r["reason"], "stale_rev")
        r = self.run_action(self.params([self.move(require={"focus": "other"})], action_id="false", base_rev=2))
        self.assertEqual(r["reason"], "precondition_failed")
        self.assertFalse(self.t.log)

    def test_point_geometry_default_guard_and_unrelated_rev(self):
        self.w.cache.rev = 3
        self.w.cache.facets["geometry[" + self.handle + "]"] = 3
        r = self.run_action()
        self.assertEqual(r["reason"], "stale_rev")
        self.w.cache.facets["geometry[" + self.handle + "]"] = 1
        r = self.run_action(self.params(action_id="unrelated"))
        self.assertEqual(r["status"], "success")

    def test_already_satisfied_exact_move_and_focus(self):
        for ident, step in (("move", self.move(2, 2)), ("focus", {"target": self.handle, "intent": {"kind": "focus_window"}})):
            r = self.run_action(self.params([step], action_id=ident))
            self.assertEqual(r["status"], "success")
            self.assertFalse(r["dispatched"])
            self.assertEqual(r["steps"][0]["disposition"], "satisfied_without_dispatch")
        self.assertFalse(self.t.log)

    def test_unrelated_already_true_rejected(self):
        step = self.key(expect={"kind": "window_present", "window": self.handle})
        r = self.run_action(self.params([step]))
        self.assertEqual(r["reason"], "invalid_expectation")
        self.assertFalse(self.t.log)

    def test_provider_predicate_unknown_after_submission(self):
        for kind in ("text_equals", "node_present", "ocr_text", "frame_matches"):
            r = self.run_action(self.params([self.key(expect={"kind": kind})], action_id=kind))
            if kind in ("text_equals", "node_present"):
                self.assertEqual(r["status"], "failed")
                self.assertEqual(r["dispatch_status"], "not_dispatched")
                self.assertEqual(r["reason"], "provider_degraded")
            elif kind == "ocr_text":
                self.assertEqual(r["status"], "unknown")
                self.assertEqual(r["dispatch_status"], "accepted")
            else:
                self.assertEqual(r["reason"], "awaiting_reconciliation")
        self.assertEqual(len([ev for ev in self.t.log if ev[0] == "key_down"]), 1)

    def test_composite_three_valued_and_flat_limit(self):
        present = {"kind": "window_present", "window": self.handle}
        missing = {"kind": "node_present"}
        self.assertTrue(self.e.evaluate({"kind": "any", "of": [present, missing]}))
        self.assertIsNone(self.e.evaluate({"kind": "all", "of": [present, missing]}))
        with self.assertRaises(ActError):
            predicate_schema({"kind": "all", "of": [present, {"kind": "any", "of": [present, present]}]})

    def test_dialog_needs_x_evidence_and_exact_classifier(self):
        self.later()
        p = {"kind": "dialog_present", "match": {"title": "Later"}}
        self.assertFalse(self.e.evaluate(p))
        self.w.cache.windows[11]["transient_for"] = 10
        self.assertTrue(self.e.evaluate(p))
        self.assertIsNone(self.e.evaluate({"kind": "dialog_present", "match": {"type": "dialog"}}))
        self.assertIsNone(self.e.evaluate(dict(p, role="rename")))

    def test_final_look_can_find_missed_event(self):
        self.t.refresh_hook = self.later
        r = self.run_action(self.params([self.key()]))
        self.assertEqual(r["status"], "success")
        self.assertIn("refresh", self.t.query_log)
        self.assertEqual(len([ev for ev in self.t.log if ev[0] == "key_down"]), 1)

    def test_unexpected_modal_stops_and_wrong_predicate_cannot_authorise(self):
        def popup():
            self.later()
            win = self.w.cache.windows[11]
            win.update(title="Unexpected", type="dialog", is_modal=True, transient_for=10)
            self.w.cache.set("focus", win["handle"])
            self.w.cache.set("modal", [win["handle"]])
        self.t.hook = popup
        r = self.run_action(self.params([self.key(), self.move(100)]))
        self.assertIn(r["effect"]["status"], ("contradicted", "unknown"))
        self.assertEqual(r["steps"][1]["reason"], "not_reached")
        self.assertEqual(len(self.t.log), 2)


class Failures(Harness):
    def test_torn_journal_tail_cannot_be_cleared_by_writable_disk(self):
        self.submit()
        with self.w.journal.path.open("a") as stream:
            stream.write('{"type":"step_armed"')
        self.e.fence("F3:journal_unavailable", journal=False)
        self.e.control_changed({"state": "paused", "epoch": 1}, {"state": "agent", "epoch": 2, "flags": 1})
        self.assertTrue(self.e.fence_reason.startswith("F3"))
        found = w.Journal.action(self.w.journal.path, "a")
        self.assertEqual(found["receipt"]["effect"]["status"], "unknown")
        self.assertFalse(found["history_complete"])
        self.assertFalse(self.t.log)

    def test_intent_fsync_failure_keeps_id_reserved_after_storage_repair(self):
        p = self.params()
        with patch.object(w.os, "fsync", side_effect=OSError("disk full")):
            with self.assertRaises(ActError):
                self.submit(p)
        self.submit(p)
        self.assertEqual(self.result()["status"], "pending")
        self.assertFalse(self.e.queue)
        self.assertEqual(self.e.get(GEN, "a")["receipt"]["reason"], "journal_unavailable")
        self.assertFalse(self.t.log)

    def test_partial_transport_write_remains_unknown_and_releases(self):
        def broken(ev):
            if ev[0] == "key_down":
                raise OSError("write extent unavailable")
        self.t.on_write = broken
        r = self.run_action(self.params([self.key()]))
        self.assertEqual(r["dispatch_status"], "unknown")
        self.assertFalse(self.t.down)
        self.assertTrue(r["pending_effects"])
        self.assertEqual(r["steps"][0]["attempts"][0]["error"], "write extent unavailable")

    def test_step_deadline_stops_slow_macro_without_replay(self):
        self.t.delay = 10000
        r = self.run_action(self.params([self.key(intent={"kind": "text", "text": "a" * 64})]))
        self.assertEqual(r["reason"], "timeout")
        typed = sum(ev[0] == "key_down" for ev in self.t.log)
        self.assertGreater(typed, 0)
        self.assertLess(typed, 64)
        self.assertFalse(self.t.down)
        self.assertTrue(r["pending_effects"])

    def test_control_malformed_f4_and_explicit_clear_requires_new_attach(self):
        previous = self.w.cache.control
        bad = {"state": "paused", "epoch": None, "reason": "invalid_control_size"}
        self.e.control_changed(previous, bad)
        self.assertTrue(self.e.fence_reason.startswith("F4"))
        self.e.control_changed(bad, {"state": "agent", "epoch": 2, "flags": 0})
        self.assertTrue(self.e.fence_reason.startswith("F4"))
        self.e.control_changed({"state": "agent", "epoch": 2}, {"state": "agent", "epoch": 3, "flags": 1})
        self.assertIsNone(self.e.fence_reason)
        self.assertIsNone(self.w.lease)

    def test_focus_change_between_text_chunks_stops_before_second_chunk(self):
        def steal():
            self.later()
            self.w.cache.set("focus", self.w.cache.handle(11))
        self.t.hook = steal
        r = self.run_action(self.params([self.key(intent={"kind": "text", "text": "a" * 65})]))
        self.assertEqual(r["reason"], "unexpected_state")
        self.assertEqual(len([ev for ev in self.t.log if ev[0] == "key_down"]), 32)
        self.assertFalse(self.t.down)

    def test_delayed_effect_resolves_without_replaying_input(self):
        self.run_action(self.params([self.key()]))
        initial_log = list(self.t.log)
        self.later()
        self.e.reconcile()
        result = self.e.get(GEN, "a")
        self.assertEqual(result["receipt"]["effect"]["status"], "not_observed")
        self.assertEqual(result["reconciliation"][0]["effect"], "verified")
        self.assertEqual(self.t.log, initial_log)

    def test_explicit_state_predicate_does_not_accept_weak_any_branch(self):
        p = {"kind": "any", "of": [{"kind": "pointer_at", "point": {"x": 20, "y": 20}},
                                      {"kind": "window_present", "match": {"title": "Later"}}]}
        r = self.run_action(self.params([self.key(expect=p)]))
        self.assertEqual(r["reason"], "invalid_expectation")
        self.assertFalse(self.t.log)

    def test_preexisting_state_with_wrong_pointer_cannot_verify_key(self):
        p = {"kind": "all", "of": [{"kind": "pointer_at", "point": {"x": 20, "y": 20}},
                                      {"kind": "window_present", "window": self.handle}]}
        r = self.run_action(self.params([self.key(expect=p)]))
        self.assertEqual(r["reason"], "invalid_expectation")
        self.assertFalse(self.t.log)

    def test_preempt_mid_text_releases_and_never_runs_suffix(self):
        def pause(ev):
            if ev == ("key_down", 50):
                self.w.cache.control = {"state": "paused", "epoch": 2}
        self.t.on_write = pause
        s = self.key(intent={"kind": "text", "text": "A" * 64})
        r = self.run_action(self.params([s, self.move(300)]))
        self.assertEqual(r["reason"], "preempted")
        self.assertEqual(self.t.down, set())
        self.assertEqual(self.t.log[:2], [("key_down", 50), ("key_up", 50)])
        self.assertNotIn(("motion", 0, 300, 20), self.t.log)
        self.assertTrue(r["steps"][0]["cleanup"]["confirmed"])
        self.assertGreaterEqual(self.now - 100, .05)

    def test_f3_intent_failure_zero_input(self):
        with patch.object(w.os, "fsync", side_effect=OSError("disk full")):
            with self.assertRaises(ActError):
                self.submit()
        self.assertTrue(self.e.fence_reason.startswith("F3"))
        self.assertFalse(self.t.log)

    def test_f3_arm_failure_zero_input(self):
        self.submit()
        with patch.object(w.os, "fsync", side_effect=OSError("disk full")):
            self.drain()
        self.assertTrue(self.e.fence_reason.startswith("F3"))
        self.assertFalse(self.t.log)
        self.assertEqual(self.result()["status"], "pending")

    def test_f1_unconfirmed_release_fences(self):
        self.t.release_ack = False
        self.t.on_write = lambda ev: self.w.cache.control.update(state="paused", epoch=2) if ev == ("key_down", 37) else None
        r = self.run_action(self.params([self.key(intent={"kind": "key", "keys": ["Control_L", "a"]})]))
        self.assertTrue(self.e.fence_reason.startswith("F1"))
        self.assertTrue(r["needs_human"])
        self.assertFalse(r["steps"][0]["cleanup"]["confirmed"])

    def test_f2_unknown_delivery_and_failed_probe(self):
        self.t.ack = False
        r = self.run_action(self.params([self.key()]))
        self.assertTrue(self.e.fence_reason.startswith("F2"))
        self.assertEqual(r["dispatch_status"], "unknown")
        self.assertEqual(r["advice"], "do_not_retry")
        self.assertTrue(r["pending_effects"])

    def test_f7_five_no_progress_batches_suspend_only_lease(self):
        for i in range(5):
            r = self.run_action(self.params([self.key(require={"focus": "bad"})], action_id=str(i)))
            self.assertEqual(r["reason"], "precondition_failed")
        self.assertTrue(self.e.lease_state["lease"]["suspended"])
        self.assertIsNone(self.e.fence_reason)
        r = self.run_action(self.params(action_id="six"))
        self.assertEqual(r["reason"], "budget_exceeded")
        self.assertFalse(self.t.log)

    def test_f8_acts_budget(self):
        self.e.lease_state["lease"]["acts"] = 1
        self.run_action()
        r = self.run_action(self.params(action_id="next"))
        self.assertEqual(r["reason"], "budget_exceeded")
        self.assertEqual(self.w.counters["route_changes"], 0)

    def test_unresolved_blocks_until_quiescence_proof_and_fresh_observe(self):
        original = self.run_action(self.params([self.key()]))
        self.assertEqual(original["reason"], "timeout")
        self.assertTrue(self.e.pending_effects)
        r = self.run_action(self.params(action_id="blocked"))
        self.assertEqual(r["reason"], "awaiting_reconciliation")
        self.e.reconcile()
        self.assertTrue(self.e.pending_effects)  # old state cannot prove cancellation
        self.later()
        self.e.reconcile()
        self.assertFalse(self.e.pending_effects)
        self.assertEqual(self.e.get(GEN, "a")["receipt"], original)  # immutable
        self.w.observed(self.peer, {}, {"full": True, "settled": True, "rev": self.w.cache.rev})
        r = self.run_action(self.params(action_id="same-epoch", base_rev=self.w.cache.rev))
        self.assertEqual(r["status"], "success")
        previous = self.w.cache.control
        self.w.cache.control = {"state": "agent", "epoch": 2}
        self.e.control_changed(previous, self.w.cache.control)
        self.w.lease, self.w.lease_peer = "new", self.peer
        self.e.attach("new", self.peer, {})
        self.e.observed(self.peer, {}, {"full": True, "settled": True, "rev": self.w.cache.rev})
        r = self.run_action(self.params(action_id="resumed", lease="new", control_epoch=2, base_rev=self.w.cache.rev))
        self.assertEqual(r["status"], "success")

    def test_channel_down_undispatched(self):
        self.e.channel["state"] = "down"
        r = self.run_action()
        self.assertEqual(r["reason"], "channel_down")
        self.assertFalse(r["dispatched"])

    def test_receipt_durability_failure_is_pending(self):
        self.submit()
        append = self.w.journal.append
        def fail(kind, **fields):
            if kind == "receipt":
                raise OSError("disk full")
            append(kind, **fields)
        with patch.object(self.w.journal, "append", side_effect=fail):
            self.drain()
            self.assertEqual(self.e.get(GEN, "a")["status"], "pending")
        self.assertEqual(self.result()["status"], "pending")
        self.assertEqual(self.e.get(GEN, "a")["status"], "final")
        self.assertTrue(self.e.fence_reason.startswith("F3"))


class ReceiptAndSelftest(Harness):
    def test_paused_start_defers_selftest_until_explicit_new_epoch(self):
        self.w.cache.control = {"state": "paused", "epoch": 2}
        self.e.start_selftest()
        self.assertIsNone(self.e.selftest)
        self.assertTrue(self.e.selftest_pending)
        self.assertFalse(self.t.log)
        previous = self.w.cache.control
        self.w.cache.control = {"state": "agent", "epoch": 3}
        self.e.control_changed(previous, self.w.cache.control)
        self.drain()
        self.assertEqual(self.e.channel["state"], "ok")
        self.assertEqual(len(self.t.log), 100)

    def test_partial_receipt_complete_and_stopping_effect(self):
        r = self.run_action(self.params([self.move(), self.key(), self.move(30)]))
        self.assertEqual(r["status"], "partial")
        self.assertEqual(r["completed_steps"], ["1"])
        self.assertEqual(r["unexecuted_steps"], ["3"])
        self.assertEqual(r["effect"], r["steps"][1]["effect"])
        self.assertEqual(r["rev"]["final"], r["state_delta"]["rev"])
        self.assertEqual(r, self.rows()[-1]["receipt"])

    def test_counter_values_reflect_real_outcomes(self):
        self.run_action(self.params([self.move(), self.key()]))
        self.assertEqual(self.w.counters["batches"], 1)
        self.assertEqual(self.w.counters["steps"], 2)
        self.assertEqual(self.w.counters["verified"], 1)
        self.assertEqual(self.w.counters["not_observed"], 1)
        self.assertEqual(self.w.counters["route_changes"], 0)

    def test_graceful_stop_releases_without_resuming_batch(self):
        self.submit(self.params([self.key(intent={"kind": "text", "text": "A" * 64})]))
        # Pause the fake raw stream while Shift is held, then request stop.
        self.t.ack = False
        self.tick()
        self.assertIn(50, self.e.holds["xtest"]["key"])
        self.t.ack = True
        for _ in range(40):
            if self.e.stopping_tick():
                break
            self.now += .001
        self.assertFalse(self.t.down)
        self.assertTrue(self.e.shutdown)
        self.assertFalse(any(row["type"] == "receipt" for row in self.rows()))

    def test_selftest_preemption_releases_shift(self):
        self.w.cache.windows.clear()
        self.t.on_write = lambda ev: self.w.cache.control.update(state="paused", epoch=2) if ev == ("key_down", 50) else None
        self.e.start_selftest()
        self.drain()
        self.assertFalse(self.t.down)
        self.assertEqual(self.e.channel["state"], "down")

    def test_receipt_field_completeness_all_outcomes(self):
        required = set("version phase session generation window action_id request_hash journal_seq profile_hash control_epoch actor_id lease_hash build_id boot_id development_flags rev status dispatched dispatch_status effect reason steps completed_steps stopped_step unexecuted_steps pending_effects state_delta timing_ms advice".split())
        step_fields = set("id target_requested target_resolved require intent_redacted expect disposition route attempts focus modal rev effect bindings reason timing_ms cleanup predicate_class expect_source".split())
        variants = [self.params(), self.params([self.key()], action_id="failed"),
                    self.params([self.key(expect={"kind": "ocr_text"})], action_id="unknown")]
        statuses = []
        for p in variants:
            self.e.pending_effects.clear()
            p["base_rev"] = self.w.cache.rev
            r = self.run_action(p)
            statuses.append(r["status"])
            self.assertLessEqual(required, r.keys())
            self.assertLessEqual(step_fields, r["steps"][0].keys())
            self.assertEqual(set(r["rev"]), {"base", "dispatched", "verified", "final"})
            self.assertEqual(r, self.rows()[-1]["receipt"])
        self.assertEqual(statuses, ["success", "failed", "unknown"])

    def test_action_get_crash_boundary_never_not_found(self):
        self.w.journal.append("intent", action_id="crash", request_hash="hash", journal_seq=1)
        self.w.journal.append("step_armed", action_id="crash", step_id="one")
        result = w.Journal.action(self.w.journal.path, "crash")
        self.assertEqual(result["receipt"]["status"], "unknown")
        self.assertEqual(result["receipt"]["last_armed"], "one")
        self.assertEqual(w.Journal.action(self.w.journal.path, "absent")["status"], "not_found")
        self.assertFalse(self.t.log)

    def test_selftest_pointer_only_when_client_exists(self):
        origin = dict(self.t.position)
        self.e.start_selftest()
        self.drain()
        self.assertEqual(len(self.t.log), 100)
        self.assertTrue(all(ev[0] == "motion" for ev in self.t.log))
        self.assertEqual(self.t.position, origin)
        self.assertEqual(self.e.channel, {"state": "ok", "keyboard": "untested", "pointer": "ok"})

    def test_selftest_empty_session_200_events(self):
        self.w.cache.windows.clear()
        self.e.start_selftest()
        self.drain()
        self.assertEqual(len(self.t.log), 200)
        self.assertEqual(self.t.test_presses, 50)
        self.assertEqual(self.t.down, set())
        self.assertEqual(self.t.position, {"x": 2, "y": 2})
        self.assertEqual(self.e.channel, {"state": "ok", "keyboard": "ok", "pointer": "ok"})

    def test_selftest_ack_failure_down(self):
        self.t.ack = False
        self.e.start_selftest()
        self.drain()
        self.assertEqual(self.e.channel["state"], "down")


class RawTransport(unittest.TestCase):
    def test_transport_bootstrap_selects_raw_and_discovers_only_enabled_slaves(self):
        requests = []
        x = NS(ev=Mock(), ax=NS(ask=lambda cls, cb, **kw: requests.append((cls, cb, kw))), root=Mock())
        x.ev.display.info.min_keycode, x.ev.display.info.max_keycode = 8, 255
        x.ev.display.get_extension_major.return_value = 131
        t = XTestTransport(NS(x=x))
        self.assertEqual([c for c, cb, kw in requests], [w.request.GetKeyboardMapping, w.xinput.XIQueryDevice])
        self.assertEqual(requests[0][2], {"first_keycode": 8, "count": 248})
        devices = [{"name": "Virtual core XTEST keyboard", "deviceid": 5, "use": w.xinput.SlaveKeyboard, "enabled": True},
                   {"name": "Virtual core XTEST pointer", "deviceid": 4, "use": w.xinput.SlavePointer, "enabled": True},
                   {"name": "Virtual core XTEST keyboard", "deviceid": 2, "use": w.xinput.MasterKeyboard, "enabled": True}]
        t.devices({"devices": devices})
        self.assertEqual(t.sources, {"keyboard": 5, "pointer": 4})
        selected = x.root.xinput_select_events.call_args.args[0]
        self.assertEqual(selected[0][0], w.xinput.AllDevices)
        self.assertTrue(selected[0][1] & w.xinput.RawKeyPressMask)

    def test_slave_only_master_duplicates_and_order(self):
        ack = Ack([("key_down", 50), ("key_up", 50)], {"keyboard": 5}, 0)
        ack.feed([(1, "key_down", 50, 2, 5), (2, "key_down", 50, 7, 7),
                  (3, "key_up", 50, 5, 5), (4, "key_down", 50, 5, 5),
                  (5, "key_down", 50, 2, 5), (6, "key_up", 50, 5, 5)])
        self.assertTrue(ack.complete)
        self.assertEqual(ack.seen, 2)

    def test_raw_wire_bytes_and_ignored_extension(self):
        payload = struct.pack("=HIIHHI", 5, 123, 50, 5, 0, 0) + bytes(4)
        raw = struct.pack("=BBHIH", 35, 131, 42, 0, 13) + payload
        event = w.ge.GenericEvent(binarydata=raw)
        self.assertEqual(decode_raw(event, 131), ("key_down", 50, 5, 5))
        self.assertIsNone(decode_raw(event, 132))
        event._data["data"] = b"short"
        self.assertIsNone(decode_raw(event, 131))

    def test_server_keyboard_mapping_shift_levels(self):
        t = XTestTransport.__new__(XTestTransport)
        t.x = NS(ev=NS(display=NS(info=NS(min_keycode=37))))
        t.keysyms, t.characters = {}, {}
        t.devices_ready = True
        t.mapping({"keysyms": [[XK.XK_Control_L, 0], [ord("a"), ord("A")], [0x20ac, 0], [XK.XK_Shift_L, 0]]})
        self.assertEqual(t.keycode("Control_L"), 37)
        self.assertEqual(t.character("a"), [38])
        self.assertEqual(t.character("A"), [40, 38])
        self.assertIsNone(t.character("😀"))
        self.assertEqual(t.character("€"), [39])


if __name__ == "__main__":
    unittest.main(verbosity=2)
