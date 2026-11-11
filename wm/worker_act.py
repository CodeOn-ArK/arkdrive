"""W2 action state machine. One owner loop; transport methods never block.

Generators yield monotonic wake times. The owner keeps draining X/control/RPC
between yields. No action is replayed from the journal, including armed ones.
"""
import copy
import hashlib
import json
import re
import time
from collections import deque
from pathlib import Path

from worker_routes import RouteTable, ladder


class ActError(Exception):
    def __init__(self, reason, detail=None):
        self.reason, self.detail = reason, detail
        super().__init__(reason)


class TreePending(ActError):
    """A recoverable window has no decidable snapshot yet; admission may wait."""
    def __init__(self, window):
        self.window = window
        super().__init__("provider_degraded")


class OCRPending(ActError):
    """A read-only OCR flight may be awaited inside the caller's deadline."""
    def __init__(self):
        super().__init__('provider_degraded')


MODIFIERS = {"ctrl": "Control_L", "control": "Control_L", "shift": "Shift_L",
             "alt": "Alt_L", "super": "Super_L"}
MODS = {"Control_L", "Control_R", "Shift_L", "Shift_R", "Alt_L", "Alt_R", "Super_L", "Super_R"}
STATE = {"window_present", "window_absent", "dialog_present", "dialog_absent", "window_focused", "pointer_at", "focus_is"}
PROVIDERS = {"node_present", "node_absent", "text_equals", "text_contains", "node_state",
             "item_present", "item_absent", "window_geometry", "scroll_value", "ocr_text",
             "ocr_text_present", "ocr_text_absent", "frame_matches", "visual_review",
             "region_changed", "region_stable", "tree", "ocr", "pixels"}
KINDS = {"key", "text", "click", "move", "scroll", "drag", "focus_window", "close_window", "invoke", "wait"}
TIMINGS = ("queue", "focus", "dispatch", "application_wait", "provider", "capture", "verify", "durable", "total")


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def closed(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise ActError("invalid_params")


def integer(value, lo, hi):
    if type(value) is not int or not lo <= value <= hi:
        raise ActError("invalid_params")
    return value


def string(value):
    if not isinstance(value, str) or not value or "\0" in value:
        raise ActError("invalid_params")
    return value


def chord(keys):
    if not isinstance(keys, list) or not 1 <= len(keys) <= 4:
        raise ActError("invalid_request")
    # The wire is one keysym per member; aliases use design's chord spelling.
    result = [MODIFIERS.get(k.lower(), k) if isinstance(k, str) else "" for k in keys]
    if any(not k or "+" in k for k in result) or len(set(result)) != len(result):
        raise ActError("invalid_request")
    if any(k not in MODS for k in result[:-1]) or result[-1] in MODS:
        raise ActError("invalid_request")
    return result


def predicate_schema(p, nested=False):
    if not isinstance(p, dict) or not isinstance(p.get("kind"), str):
        raise ActError("invalid_expectation")
    k = p["kind"]
    if k in ("all", "any"):
        closed(p, ("kind", "of"), ("of",))
        if nested or not isinstance(p["of"], list) or not 2 <= len(p["of"]) <= 4:
            raise ActError("invalid_expectation")
        for item in p["of"]:
            predicate_schema(item, True)
    elif k in STATE:
        fields = {"kind", "window", "handle", "match", "parent", "role", "bind"}
        if k == "pointer_at":
            fields = {"kind", "point", "tolerance_px"}
        if k == "focus_is":
            fields.add("ref")
        closed(p, fields)
        for name in ("window", "handle", "parent"):
            if name in p:
                if isinstance(p[name], dict):
                    closed(p[name], ("from_step", "name"), ("from_step", "name"))
                    string(p[name]["from_step"])
                    string(p[name]["name"])
                else:
                    string(p[name])
        if k == "pointer_at" and "point" not in p:
            raise ActError("invalid_expectation")
        if "tolerance_px" in p:
            integer(p["tolerance_px"], 0, 1)
        if "bind" in p:
            if not isinstance(p["bind"], dict) or len(p["bind"]) > 8:
                raise ActError("invalid_params")
        if "match" in p:
            closed(p["match"], ("title", "title_re", "class", "type"))
            if any(not isinstance(v, str) or len(v) > 512 for v in p["match"].values()):
                raise ActError("invalid_params")
            if "title_re" in p["match"]:
                try:
                    re.compile(p["match"]["title_re"])
                except re.error:
                    raise ActError("invalid_params")
    elif k in PROVIDERS:
        closed(p, ("kind", "ref", "match", "value", "state", "within", "container", "name", "role",
                   "window", "selector", "rect", "axis", "crop", "text", "re", "region", "mode", "hash", "quiet_ms"))
        if "selector" in p:
            from worker_tree import selector_schema
            selector_schema(p["selector"])
        if k in ('ocr_text', 'ocr_text_present', 'ocr_text_absent'):
            if 'text' in p and (not isinstance(p['text'], str) or not p['text']):
                raise ActError('invalid_expectation')
            if 're' in p:
                if 'text' in p or not isinstance(p['re'], str) or not p['re'] or len(p['re']) > 512:
                    raise ActError('invalid_expectation')
                try:
                    re.compile(p['re'])
                except re.error:
                    raise ActError('invalid_expectation')
        if k == "node_state" and (p.get("state") not in
                ("selected", "checked", "expanded", "focused", "enabled", "editable", "disabled") or
                "value" in p and type(p["value"]) is not bool):
            raise ActError("invalid_expectation")
    else:
        # Reaction and generic pixel change are evidence, never proof.
        raise ActError("invalid_expectation")


def leaves(p):
    return p["of"] if p["kind"] in ("all", "any") else [p]


def predicate_class(p):
    classes = {"state" if v["kind"] in STATE else "tree" if v["kind"] in
               ("text_equals", "text_contains", "node_present", "node_absent", "node_state", "item_present", "item_absent", "scroll_value")
               else "ocr" if v["kind"].startswith("ocr") else "pixels" for v in leaves(p)}
    return next(iter(classes)) if len(classes) == 1 else "composite"


def contains_ref(value):
    if isinstance(value, dict):
        return any(k in ("ref", "selector", "from_step", "selected_ref", "editable_ref", "text_equals", "modal_role")
                   or contains_ref(v) for k, v in value.items())
    if isinstance(value, list):
        return any(contains_ref(v) for v in value)
    return False


class Ack:
    """Consume only slave copies from the selected XTEST source, in order."""
    def __init__(self, expected, sources, cursor):
        self.expected, self.sources, self.cursor = list(expected), sources, cursor
        self.seen = 0

    def feed(self, events):
        for seq, kind, detail, device, source in events:
            if seq <= self.cursor:
                continue
            self.cursor = seq
            if self.seen >= len(self.expected):
                continue
            want_kind, want_detail = self.expected[self.seen]
            wanted_source = self.sources.get("keyboard" if want_kind.startswith("key") else "pointer")
            if device == source == wanted_source and (kind, detail) == (want_kind, want_detail):
                self.seen += 1

    @property
    def complete(self):
        return self.seen == len(self.expected)

    def receipt(self):
        return {"expected": len(self.expected), "seen": self.seen, "source": dict(self.sources)}


class Engine:
    def __init__(self, worker, transport, clock=time.monotonic):
        self.w, self.t, self.clock = worker, transport, clock
        self.queue, self.active = deque(), None
        self.actions = {}
        self.seq = 0
        self.evdev = None
        from worker_paste import Clipboard
        self.clipboard = Clipboard(worker)
        self.routes = RouteTable(worker.state / "route-table.json", worker.generation)
        self.route = "xtest"
        self.fence_reason = None
        self.pending_effects = []
        self.holds = {route: {"key": set(), "button": set()} for route in ("xtest", "evdev")}
        self.unknown_streak = 0
        self.lease_state = {}
        self.channel = {"state": "untested", "keyboard": "untested", "pointer": "untested"}
        self.selftest = None
        self.selftest_pending = False
        self.shutdown = False
        self.stop_runner = None
        self.durable_ms = 0
        try:
            self.boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        except OSError:
            self.boot_id = None
        digest = hashlib.sha256()
        for name in ("ark-worker", "worker_act.py", "worker_xtest.py", "worker_evdev.py", "worker_keymap.py", "worker_routes.py", "worker_paste.py", "input_protocol.py", "worker_pixels.py", "worker_tree.py", "worker_atspi.py", "openbox-rc.xml"):
            digest.update(name.encode() + Path(__file__).with_name(name).read_bytes())
        self.build_id = digest.hexdigest()
        try:
            self.pending_effects = self.w.journal.unresolved()
        except (OSError, ValueError):
            self.fence("F3:journal_unavailable", journal=False)

    def durable(self, kind, **fields):
        started = self.clock()
        try:
            self.w.journal.append(kind, **fields)
        except (OSError, ValueError):
            self.fence("F3:journal_unavailable", journal=False)
            raise ActError("journal_unavailable")
        finally:
            self.durable_ms += (self.clock() - started) * 1000

    def fence(self, reason, journal=True):
        if self.fence_reason:
            return
        self.fence_reason = reason
        if reason == 'F1:release_unconfirmed' and self.evdev and any(self.holds['evdev'].values()):
            self.evdev.health['fenced'] = reason
        if journal:
            try:
                self.durable("fence", reason=reason)
            except ActError:
                self.fence_reason = "F3:journal_unavailable"

    def validate_journal(self):
        try:
            self.w.journal.validate()
        except (OSError, ValueError):
            self.fence_reason = "F3:journal_unavailable"
            raise ActError("journal_unavailable")

    def control_changed(self, previous, current):
        if current.get("epoch") is None:
            self.fence("F4:control_unavailable")
        # Explicit --clear-fence uses a dedicated bit and a new epoch.
        if previous and current.get("epoch") != previous.get("epoch"):
            if current.get("flags", 0) & 1 and current["state"] == "agent":
                try:
                    self.validate_journal()
                    self.durable("fence_clear", previous=self.fence_reason, control_epoch=current["epoch"])
                    if not any(v for holds in self.holds.values() for v in holds.values()):
                        self.fence_reason = None
                        if self.evdev:
                            self.evdev.health.pop('fenced', None)
                except ActError:
                    pass
            # New authority always needs a new attach and full observation.
            self.w.lease = self.w.lease_peer = self.w.lease_until = None
            if current["state"] == "agent" and self.selftest_pending and self.selftest is None and not self.fence_reason:
                self.start_selftest()

    def attach(self, token, peer, budget):
        self.lease_state[token] = {"epoch": (self.w.cache.control or {}).get("epoch"),
                                   "observed": None, "acts": budget.get("acts"), "used": 0,
                                   "misses": 0, "suspended": False, "peer": peer}

    def observed(self, peer, params, result):
        token = self.w.lease
        state = self.lease_state.get(token)
        tree = result.get("tree", {})
        if state and peer is self.w.lease_peer and params.get('lease', token) == token:
            # A delivered healthy interval rearms fallback even when recovery
            # reset the breaker's counters and no pixels/tree nodes changed.
            if (tree.get('window') and tree.get('status') == 'usable' and
                    not self.w.ocr_tree_unusable(self.w.ocr_tree_state(tree['window'], result.get('providers')))):
                state.get('ocr_trees', {}).pop(tree['window'], None)
        if (state and peer is self.w.lease_peer and params.get('lease', token) == token
                and 'ocr' in result):
            from worker_pixels import intersect
            handle = result['ocr']['window']
            rect, _ = self.w.pixels.region(params)
            region = intersect(rect, self.w.pixels.window(handle)['geometry'])
            state.setdefault('ocr_trees', {})[handle] = (self.w.ocr_tree_state(handle, result.get('providers')), region)
        if state and peer is self.w.lease_peer and params.get("lease", token) == token and "rev" in tree:
            baselines = state.setdefault("trees", {})
            pages = state.setdefault("tree_pages", {})
            h, rev = tree["window"], tree["rev"]
            if "delta" in tree:
                baselines[h] = rev
            elif tree.get("full"):
                continuation = params.get("cursor")
                prior = pages.get(h)
                contiguous = continuation is None or prior == (rev, continuation)
                if contiguous:
                    if tree.get("truncated"):
                        baselines.pop(h, None)
                        pages[h] = (rev, tree["cursor"])
                    else:
                        baselines[h] = rev
                        pages.pop(h, None)
        if (state and peer is self.w.lease_peer and result.get("full") and result.get("settled")
                and params.get("lease", token) == token
                and not result.get("resync_required")
                and params.get("scope", {}).get("kind", "session") == "session"):
            state["observed"] = result["rev"]
            self.reconcile(observed_rev=result["rev"])

    def reconciliation_reason(self):
        if self.pending_effects:
            return "effect_unresolved" if any(v.get("observed_unresolved") for v in self.pending_effects) else "awaiting_reconciliation"
        return None

    def gate(self, action):
        self.w.poll_control(force=True)
        c = self.w.cache.control or {}
        if c.get("epoch") is None:
            self.fence("F4:control_unavailable")
        if self.fence_reason:
            raise ActError("worker_fault", self.fence_reason)
        if self.lease_state.get(action["params"].get("lease"), {}).get("done"):
            raise ActError("preempted")
        if self.shutdown or c.get("state") != "agent" or c.get("epoch") != action["params"]["control_epoch"]:
            raise ActError("preempted")
        if self.clock() >= min(action["deadline"], action.get("step_deadline", action["deadline"])):
            raise ActError("timeout")

    def normalise(self, params):
        closed(params, ("session", "generation", "lease", "control_epoch", "action_id", "base_rev", "deadline_ms", "steps"),
               ("session", "generation", "lease", "control_epoch", "action_id", "base_rev", "steps"))
        for key in ("session", "generation", "lease", "action_id"):
            string(params[key])
        integer(params["control_epoch"], 1, 2**64 - 1)
        integer(params["base_rev"], 1, 2**63 - 1)
        steps = params["steps"]
        if not isinstance(steps, list) or not 1 <= len(steps) <= 8:
            raise ActError("invalid_params")
        p = copy.deepcopy(params)
        ids = set()
        for i, s in enumerate(p["steps"]):
            closed(s, ("id", "target", "require", "intent", "expect", "timeout_ms", "risk", "allow_stale_pixels"), ("intent",))
            s.setdefault("id", str(i + 1))
            string(s["id"])
            if s["id"] in ids:
                raise ActError("invalid_params")
            ids.add(s["id"])
            integer(s.setdefault("timeout_ms", 1500), 50, 5000)
            if "allow_stale_pixels" in s and type(s["allow_stale_pixels"]) is not bool:
                raise ActError("invalid_params")
            s.setdefault("require", {})
            closed(s["require"], ("focus", "modal", "ref", "window", "geometry", "pointer", "modal_role", "selected_ref", "editable_ref", "text_equals"))
            for f in ("focus", "window"):
                if f in s["require"] and s["require"][f] is not None:
                    string(s["require"][f])
            if "modal" in s["require"] and not (s["require"]["modal"] is None or isinstance(s["require"]["modal"], (str, dict))):
                raise ActError("invalid_params")
            if isinstance(s["require"].get("modal"), dict):
                predicate_schema({"kind": "window_present", "match": s["require"]["modal"]})
            if "target" in s:
                if isinstance(s["target"], str):
                    string(s["target"])
                else:
                    closed(s["target"], ("window", "ref", "selector", "point", "from_step", "name"))
                    if "selector" in s["target"]:
                        from worker_tree import selector_schema
                        selector_schema(s["target"]["selector"])
            intent = s["intent"]
            if not isinstance(intent, dict) or not isinstance(intent.get("kind"), str) or intent["kind"] not in KINDS:
                raise ActError("invalid_request")
            k = intent["kind"]
            fields = {"key": ("keys",), "text": ("text", "replace", "allow_control", "via"),
                      "click": ("at", "button", "count", "modifiers"), "move": ("at",),
                      "scroll": ("at", "dy", "dx", "axis", "ticks", "modifiers"),
                      "drag": ("from", "to", "button", "duration_ms", "modifiers"),
                      "focus_window": ("window",), "close_window": ("window",),
                      "invoke": ("ref", "action"), "wait": ()}[k]
            closed(intent, ("kind", "route", *fields))
            if intent.get("route", "auto") not in ("auto", "input", "xtest", "evdev", "wm", "atspi", "clipboard", "cdp"):
                raise ActError("invalid_request")
            if k == "key":
                intent["keys"] = chord(intent.get("keys"))
            if k == "text":
                value = string(intent.get("text"))
                if len(value) > 2000 or len(value.encode()) > 4096:
                    raise ActError("invalid_request")
                for f in ("replace", "allow_control"):
                    if type(intent.setdefault(f, False)) is not bool:
                        raise ActError("invalid_request")
                if any(c in value for c in "\n\t") and not intent["allow_control"]:
                    raise ActError("invalid_request")
                if intent.get("via", "auto") not in ("auto", "keys", "paste"):
                    raise ActError("invalid_request")
            if "modifiers" in intent:
                mods = intent["modifiers"]
                if not isinstance(mods, list) or len(mods) > 3 or any(m not in ("Shift_L", "Control_L", "Alt_L", "Super_L") for m in mods) or len(set(mods)) != len(mods):
                    raise ActError("invalid_request")
            if k in ("click", "drag") and intent.setdefault("button", "left") not in ("left", "middle", "right"):
                raise ActError("invalid_request")
            if k == "click":
                integer(intent.setdefault("count", 1), 1, 3)
            if k == "drag":
                integer(intent.setdefault("duration_ms", 64), 50, 1000)
                if "to" not in intent:
                    raise ActError("invalid_request")
            if k == "scroll":
                if "ticks" in intent:
                    if intent.get("axis") not in ("vertical", "horizontal"):
                        raise ActError("invalid_request")
                    integer(intent["ticks"], -20, 20)
                    intent["dy" if intent["axis"] == "vertical" else "dx"] = intent["ticks"]
                for f in ("dy", "dx"):
                    integer(intent.setdefault(f, 0), -20, 20)
                if not (intent["dy"] or intent["dx"]):
                    raise ActError("invalid_request")
            if "expect" in s:
                predicate_schema(s["expect"])
        total = sum(s["timeout_ms"] for s in p["steps"]) + 500
        integer(p.setdefault("deadline_ms", total), 1, 10000)
        # NUL/nonfinite/recursive values are rejected before journal creation.
        encoded = canonical(p)
        if "\\u0000" in encoded:
            raise ActError("invalid_params")
        return p, hashlib.sha256(encoded.encode()).hexdigest()

    def make_receipt(self, p, request_hash):
        def step(s, i):
            return {"id": s["id"], "i": i, "kind": s["intent"]["kind"],
                    "target_requested": s.get("target"), "target_resolved": None, "target": None,
                    "require": s["require"], "intent_redacted": s["intent"], "expect": s.get("expect"),
                    "disposition": "unexecuted", "dispatch_status": "not_dispatched", "route": None,
                    "attempts": [], "focus": {"before": None, "after": None}, "modal": {"before": [], "after": []},
                    "rev": {"base": p["base_rev"], "dispatched": None, "verified": None},
                    "effect": self.effect("not_observed", s.get("expect")), "bindings": {},
                    "reason": "not_reached", "stop_reason": "prior_step_stopped", "predicate_class": None,
                    "expect_source": "explicit" if "expect" in s else "default",
                    "timing_ms": dict.fromkeys(TIMINGS, 0),
                    "cleanup": {"released": [], "unreleased": [], "confirmed": True}}
        return {"version": 2, "phase": "validate", "session": self.w.sid, "generation": self.w.generation,
                "window": None, "action_id": p["action_id"], "request_hash": request_hash,
                "journal_seq": None, "profile": self.w.record["profile"], "profile_hash": self.w.profile_hash,
                "control_epoch": p["control_epoch"], "actor_id": "driver", "lease_hash": hashlib.sha256(p["lease"].encode()).hexdigest()[:32],
                "build_id": self.build_id, "boot_id": self.boot_id, "development_flags": self.w.development_flags,
                "rev": {"base": p["base_rev"], "dispatched": None, "verified": None, "final": self.w.cache.rev},
                "status": "failed", "dispatched": False, "dispatch_status": "not_dispatched",
                "effect": self.effect("not_observed", None), "reason": None,
                "steps": [step(s, i + 1) for i, s in enumerate(p["steps"])], "completed_steps": [],
                "stopped_step": None, "stopped_at": None, "steps_total": len(p["steps"]), "steps_verified": 0,
                "unexecuted_steps": [s["id"] for s in p["steps"]], "pending_effects": [], "state_delta": None,
                "needs_human": False, "escalate": None, "advice": "none", "timing_ms": dict.fromkeys(TIMINGS, 0)}

    @staticmethod
    def effect(status, p, evidence=None):
        k = p.get("kind") if p else None
        return {"status": status, "predicate": p, "evidence": evidence or [],
                "assurance": "none" if status != "verified" else "position" if k == "pointer_at" else
                "visual_state" if any(e.get('kind') == 'ocr' for e in evidence or []) else "application_state"}

    def submit(self, peer, ident, params):
        p, h = self.normalise(params)
        key = (p["generation"], p["action_id"])
        prior = self.actions.get(key)
        if prior:
            if prior["hash"] != h:
                raise ActError("action_id_conflict")
            self.w.respond(peer, ident, prior["receipt"] if prior.get("final") else self.pending(prior))
            return
        # Authorisation precedes intent; dedupe retrieval itself never dispatches.
        self.w.require_lease(peer, p)
        tree = getattr(self.w, "tree", None)
        if tree:
            tree.admission(p["steps"])
        receipt = self.make_receipt(p, h)
        a = {"params": p, "hash": h, "receipt": receipt, "peer": peer, "ident": ident,
             "received": self.clock(), "deadline": self.clock() + p["deadline_ms"] / 1000,
             "preempted": False, "armed": [], "final": False, "durable_start": self.durable_ms}
        state = self.lease_state.get(p["lease"], {})
        reason = None
        if self.fence_reason:
            reason = "journal_unavailable" if self.fence_reason.startswith("F3") else "worker_fault"
        elif self.pending_effects:
            reason = self.reconciliation_reason()
        elif state.get("suspended") or (state.get("acts") is not None and state.get("used", 0) >= state["acts"]):
            state["suspended"] = True
            reason = "budget_exceeded"
        elif state.get("epoch") != p["control_epoch"] or (self.w.cache.control or {}).get("epoch") != p["control_epoch"]:
            reason = "preempted"
        elif state.get("observed") is None:
            reason = "precondition_failed"
        elif p["base_rev"] < state["observed"] or p["base_rev"] > self.w.cache.rev:
            reason = "stale_rev"
        elif len(self.queue) >= 8:
            reason = "queue_full"
        if reason:
            receipt.update(reason=reason, advice=self.advice(reason, "not_dispatched"), needs_human=bool(self.fence_reason))
            receipt["pending_effects"] = copy.deepcopy(self.pending_effects)
            self.w.respond(peer, ident, receipt)
            return
        self.seq += 1
        receipt["journal_seq"] = self.seq
        # Reserve the ID even if fsync fails after a complete intent write.
        # Repairing storage must never turn that ID into a new enqueue.
        self.actions[key] = a
        a["intent_record"] = {"action_id": p["action_id"], "request_hash": h, "journal_seq": self.seq,
                              "profile": self.w.record["profile"], "profile_hash": receipt["profile_hash"],
                              "build_id": self.build_id, "boot_id": self.boot_id, "development_flags": self.w.development_flags,
                              "lease_hash": receipt["lease_hash"], "control_epoch": p["control_epoch"],
                              "request": {k: v for k, v in p.items() if k != "lease"}}
        try:
            self.durable("intent", **a["intent_record"])
            a["intent_durable"] = True
        except ActError:
            receipt.update(reason="journal_unavailable", advice="stop", needs_human=True)
            a["outcome_known"] = True
            raise
        self.queue.append(a)
        state["used"] += 1
        self.w.counters["batches"] += 1

    def pending(self, a):
        return {"status": "pending", "action_id": a["params"]["action_id"], "request_hash": a["hash"],
                "journal_seq": a["receipt"]["journal_seq"], "last_armed": a["armed"][-1] if a["armed"] else None,
                "pending_effects": copy.deepcopy(self.pending_effects)}

    def get(self, generation, action_id):
        a = self.actions.get((generation, action_id))
        if not a:
            return None
        if not a["final"] and a.get("outcome_known"):
            try:
                self.validate_journal()
                if not a.get("intent_durable"):
                    self.durable("intent", **a["intent_record"])
                    a["intent_durable"] = True
                self.durable("receipt", action_id=action_id, receipt=a["receipt"])
                a["final"] = True
            except ActError:
                pass
        return {"status": "final", "receipt": a["receipt"], "reconciliation": a.get("reconciliations", [])} if a["final"] else self.pending(a)

    def tick(self):
        if self.evdev:
            self.evdev.tick()
        if self.selftest:
            try:
                next(self.selftest)
            except StopIteration:
                self.selftest = None
            return
        if self.active is None and self.queue:
            self.active = self.queue.popleft()
            self.active["runner"] = self.run(self.active)
        if self.active:
            try:
                next(self.active["runner"])
            except StopIteration:
                self.active = None

    def stopping_tick(self):
        """Release-only shutdown. Interrupted actions stay durable/unknown."""
        self.shutdown = True
        if self.evdev and self.evdev.stop_refresh():
            return False
        if self.evdev and self.evdev.inflight:
            # Finish reading the outstanding reply before release_all uses
            # this stream. The owner closes the helper gate first on xorg.
            runner = self.selftest or (self.active and self.active.get("runner"))
            if runner:
                try:
                    next(runner)
                except StopIteration:
                    self.selftest = None
                    self.active = None
                return False
        if self.stop_runner is None:
            a = self.active or {"params": {"action_id": "worker_stop"}, "acks": []}
            r = a["receipt"]["steps"][0] if self.active else {
                "id": "stop", "attempts": [], "rev": {"dispatched": None}, "dispatch_status": "not_dispatched"}
            self.stop_runner = self.release(a, r)
        try:
            next(self.stop_runner)
        except StopIteration:
            return True
        return False

    def pause(self, seconds, a=None, enforce=True):
        until = self.clock() + seconds
        while self.clock() < until:
            if a and enforce:
                self.gate(a)
            yield

    def query(self, name, *args, deadline=None):
        box = []
        cancel = getattr(self.t, name)(*args, lambda result: box.append(result))
        try:
            while not box:
                if deadline is not None and self.clock() >= deadline:
                    raise ActError("timeout")
                yield
        finally:
            if callable(cancel):
                cancel()
        return box[0]

    def window(self, handle):
        if handle is not None:
            if not isinstance(handle, str):
                raise ActError("invalid_request")
            if not handle.startswith(f"{self.w.sid}/g{self.w.generation[:8]}/"):
                raise ActError("stale_generation")
        return next((v for v in self.w.cache.windows.values() if v["handle"] == handle and v["mapped"]), None)

    def point(self, raw, base=None):
        if not isinstance(raw, dict):
            raise ActError("invalid_request")
        if "point" in raw:
            closed(raw, ("point",))
            raw = raw["point"]
        if 'text' in raw:
            return self.text_point(raw, base)
        if contains_ref(raw) or "text" in raw:
            raise ActError("provider_degraded")
        closed(raw, ("space", "x", "y", "at_rev"), ("x", "y"))
        x, y = raw["x"], raw["y"]
        if type(x) is not int or type(y) is not int:
            raise ActError("invalid_request")
        space = raw.get("space", "desktop")
        if space != "desktop" and getattr(self.w, "pixels", None):
            x, y = self.w.pixels.desktop_point(raw, base)
        elif space != "desktop":
            if not isinstance(space, str) or not space.startswith("window:"):
                raise ActError("transform_stale")
            win = self.window(space[7:])
            if not win:
                raise ActError("target_not_found")
            if self.w.cache.facets.get("geometry[" + win["handle"] + "]", 1) > raw.get("at_rev", 0):
                raise ActError("stale_rev")
            x, y = x + win["geometry"]["x"], y + win["geometry"]["y"]
        if not 0 <= x < self.w.screen["w"] or not 0 <= y < self.w.screen["h"]:
            raise ActError("invalid_request")
        return {"x": x, "y": y}

    def text_point(self, raw, base=None):
        closed(raw, ('text', 'nth', 'within'), ('text',))
        text = string(raw['text'])
        if 'nth' in raw:
            integer(raw['nth'], 0, 2**31-1)
        handle = raw.get('within', self.w.cache.focus)
        win = self.window(handle)
        if win is None:
            raise ActError('target_not_found')
        tree, ocr = getattr(self.w, 'tree', None), getattr(self.w, 'ocr', None)
        matches = []
        if tree and tree.healthy(handle):
            snap = tree.snapshot(handle, completed=True)
            matches = [dict(text=n['name'], box=n['bounds'], ref=n['ref'])
                       for n in snap['nodes'].values() if n.get('name') == text and
                       n.get('showing', True) and n.get('visible', True)]
        if not matches and ocr:
            lines = ocr.read(handle)
            if lines is None:
                if ocr.state != 'down':
                    raise OCRPending()
                raise ActError('provider_degraded')
            matches = [line for line in lines if text in line['text']]
        nth = raw.get('nth', 0)
        if not matches or ('nth' not in raw and len(matches) > 1) or nth >= len(matches):
            raise ActError('target_not_found', dict(candidates=matches[:5]))
        x, y, width, height = matches[nth]['box']
        if tree and matches[nth].get('ref') and 'disabled' in tree.cache.windows[handle]['nodes'][matches[nth]['ref']].get('states', []):
            raise ActError('precondition_failed')
        g = win['geometry']
        right, bottom = min(x+width, g['x']+g['w']), min(y+height, g['y']+g['h'])
        x, y = max(x, g['x']), max(y, g['y'])
        if right <= x or bottom <= y:
            raise ActError('target_not_found')
        if base is not None and self.w.cache.facets.get('tree['+handle+']', 1) > base and tree and matches[nth].get('ref'):
            raise ActError('stale_rev', 'tree_changed')
        return self.point(dict(x=x+(right-x)//2, y=y+(bottom-y)//2), base)

    def resolve(self, s, base=None):
        target, intent = s.get("target"), s["intent"]
        tree = getattr(self.w, "tree", None)
        semantic = contains_ref(target) or intent["kind"] == "invoke"
        if (semantic or contains_ref(s["require"])) and tree is None:
            raise ActError("provider_degraded")
        pointer = intent["kind"] in ("move", "click", "scroll", "drag")
        handle = target if isinstance(target, str) else (target or {}).get("window")
        handle = intent.get("window", handle or (None if pointer else self.w.cache.focus))
        win = self.window(handle)
        if not win and intent["kind"] != "wait" and not (pointer and handle is None):
            raise ActError("target_not_found")
        at = intent.get("from") if intent["kind"] == "drag" else intent.get("at")
        at = at or ((target or {}).get("point") if isinstance(target, dict) else None)
        if isinstance(at, dict) and 'text' in at and 'within' not in at and handle:
            at = dict(at, within=handle)
        if isinstance(at, dict) and 'text' in at and at.get('within'):
            if handle and handle != at['within']:
                raise ActError('invalid_request')
            handle = at['within']
        point = self.point(at, base) if at is not None else None
        if semantic:
            if not isinstance(target, dict):
                target = {}
            subject = target.get("ref", target.get("selector", intent.get("ref")))
            node = tree.resolve(handle, subject, base, completed=True)
            if "disabled" in node.get("states", []):
                raise ActError("precondition_failed")
            s["_node"] = dict(node)
            handle = handle or self.w.cache.focus
            x, y, width, height = node["bounds"]
            g = self.window(handle)["geometry"]
            right, bottom = min(x+width, g["x"]+g["w"]), min(y+height, g["y"]+g["h"])
            x, y = max(x, g["x"]), max(y, g["y"])
            width, height = right-x, bottom-y
            if width <= 0 or height <= 0:
                raise ActError("target_not_found")
            point = self.point({"x": x + width//2, "y": y + height//2}, base) if intent["kind"] != "invoke" else None
        if pointer and point is None:
            raise ActError("invalid_request")
        if pointer and handle is None:
            cache = self.w.cache
            order = {wid: i for i, wid in enumerate(cache.stacking)}
            for win in sorted(cache.windows.values(), key=lambda v: order.get(v["xid"], -1), reverse=True):
                g = win["geometry"]
                if win["mapped"] and g["x"] <= point["x"] < g["x"] + g["w"] and g["y"] <= point["y"] < g["y"] + g["h"]:
                    handle = win["handle"]
                    break
        return handle, point

    def guards(self, a, s, base, handle, point):
        cache, req = self.w.cache, s["require"]
        tree = getattr(self.w, "tree", None)
        if tree and contains_ref(req):
            tree.requirements(req, handle, base, completed=True)
        if s.get("_node"):
            current = tree.resolve(handle, s["_node"]["ref"], base, completed=True)
            if any(current.get(k) != s["_node"].get(k) for k in ("key", "role", "name")):
                raise ActError("stale_rev", "tree_identity")
            if "disabled" in current.get("states", []):
                raise ActError("precondition_failed")
        if not 1 <= base <= cache.rev:
            raise ActError("stale_rev")
        facets = ["focus", "modal"]
        if point:
            facets.append("stacking")
            if handle is not None:
                facets.append("geometry[" + handle + "]")
            for win in cache.windows.values():
                g = win["geometry"]
                if win["mapped"] and g["x"] <= point["x"] < g["x"] + g["w"] and g["y"] <= point["y"] < g["y"] + g["h"]:
                    facets.append("geometry[" + win["handle"] + "]")
            if getattr(self.w, "pixels", None) and not s.get("allow_stale_pixels", False):
                facets.extend(self.w.pixels.facet(t) for t in self.w.pixels.tiles(
                    dict(x=point["x"], y=point["y"], w=1, h=1)))
            for value in (s.get("target"), s["intent"].get("at"), s["intent"].get("from")):
                if isinstance(value, dict):
                    raw = value.get("point", value)
                    if 'text' in raw:
                        fresh = self.point(dict(raw, within=raw.get('within', handle)), base)
                        if fresh != point:
                            raise ActError('stale_rev', 'text_target_changed')
                    if "at_rev" in raw:
                        integer(raw["at_rev"], 1, cache.rev)
                        if handle is not None and cache.facets.get("geometry[" + handle + "]", 1) > raw["at_rev"]:
                            raise ActError("stale_rev")
        if "window" in req:
            facets.append("geometry[" + req["window"] + "]")
        if "geometry" in req:
            closed(req["geometry"], ("window", "rect"), ("window", "rect"))
            facets.append("geometry[" + req["geometry"]["window"] + "]")
        if "pointer" in req:
            facets.append("pointer")
        if any(cache.facets.get(f, 1) > base for f in facets):
            raise ActError("stale_rev")
        if "focus" in req and req["focus"] != cache.focus:
            raise ActError("precondition_failed")
        if "modal" in req and req["modal"] != "any":
            actual = cache.modal
            wanted = req["modal"]
            if wanted is None:
                valid = not actual
            elif isinstance(wanted, dict):
                valid = any(self.matches(self.window(h), wanted) for h in actual)
            else:
                valid = wanted in actual
            if not valid:
                raise ActError("precondition_failed")
        if "window" in req and not self.window(req["window"]):
            raise ActError("precondition_failed")
        if "geometry" in req:
            win = self.window(req["geometry"]["window"])
            if not win or win["geometry"] != req["geometry"]["rect"]:
                raise ActError("precondition_failed")
        if "pointer" in req and cache.pointer != req["pointer"]:
            raise ActError("precondition_failed")
        if cache.modal and handle not in cache.modal and s["intent"]["kind"] != "wait":
            raise ActError("modal_still_present", "unexpected_modal")

    @staticmethod
    def matches(win, match):
        return bool(win) and all(bool(re.search(v, win.get("title", ""))) if k == "title_re" else win.get(k) == v
                                 for k, v in match.items())

    def verification_state(self, sampled):
        cache = self.w.cache
        if not isinstance(sampled, dict) or "classifiers" not in sampled:
            return cache.windows, cache.focus, set(cache.modal)
        # Direct appearance evidence is deliberately separate from the full
        # cache: no geometry or frame data is invented for a new client.
        windows = dict(cache.windows)
        for wid, fields in sampled["classifiers"].items():
            old = windows.get(wid, {})
            windows[wid] = dict(old, **fields, handle=old.get("handle", f"xid:{wid}"),
                                family=old.get("family", f"x:{wid}"))
        for _ in range(len(windows)):
            changed = False
            for wid, win in list(windows.items()):
                parent = windows.get(win.get("transient_for"))
                if parent and parent["family"] != win["family"]:
                    windows[wid] = dict(win, family=parent["family"])
                    changed = True
            if not changed:
                break
        focus = windows.get(sampled.get("focus_xid"))
        modal = {v["handle"] for v in windows.values() if v["mapped"] and focus and
                 v["family"] == focus["family"] and
                 (v.get("is_modal") or (v.get("transient_for") and v["type"] == "dialog"))}
        return windows, focus["handle"] if focus else None, modal

    def effective_predicate(self, p):
        """Only name presence/absence has an OCR equivalent. Never infer refs."""
        if p['kind'] in ('all', 'any'):
            return dict(p, of=[self.effective_predicate(v) for v in p['of']])
        tree, ocr = getattr(self.w, 'tree', None), getattr(self.w, 'ocr', None)
        if not tree or not ocr or p['kind'] not in ('node_present', 'node_absent') or 'ref' in p:
            return p
        window = p.get('window') or (p.get('within') if p.get('within') != 'modal' else
                                    next(iter(self.w.cache.modal), None)) or tree.predicate_window(p)
        if tree.readiness(window) != 'degraded':
            return p
        match = p.get('selector', p.get('match', {'name': p['name']} if 'name' in p else {}))
        if (not isinstance(match, dict) or set(match)-{'provider', 'name'} or not isinstance(match.get('name'), str) or not match['name'] or
                p.get('role') or p.get('container') or
                p.get('within') and p['within'] != 'modal' and not any(
                    w['mapped'] and w['handle'] == p['within'] for w in self.w.cache.windows.values())):
            return p
        return dict(kind='ocr_text_absent' if p['kind'] == 'node_absent' else 'ocr_text_present',
                    text=match['name'], window=window)

    def evaluated_effect(self, status, p, sampled=False):
        effective = self.effective_predicate(p) if p else p
        effect = self.effect(status, effective, self.evidence(effective, sampled) if status == 'verified' and effective else [])
        if effective != p:
            effect['fallback_from'] = 'tree'
        return effect

    def evaluate(self, p, sampled=False):
        p = self.effective_predicate(p)
        k = p["kind"]
        if k in ("all", "any"):
            results = [self.evaluate(v, sampled) for v in p["of"]]
            if k == "all":
                return False if False in results else None if None in results else True
            return True if True in results else None if None in results else False
        tree = getattr(self.w, "tree", None)
        if k in ('ocr_text', 'ocr_text_present', 'ocr_text_absent') and getattr(self.w, 'ocr', None):
            return self.w.ocr.evaluate(p)
        if tree and k in ("node_present", "node_absent", "text_equals", "text_contains", "node_state", "item_present", "item_absent", "scroll_value"):
            return tree.evaluate(p)
        if tree and k == "focus_is" and "ref" in p:
            return tree.evaluate(dict(p, kind="node_state", state="focused"))
        if k not in STATE or (k == "focus_is" and "ref" in p):
            return None
        if not sampled and not self.w.covered():
            return None
        if k == "pointer_at":
            return self.w.cache.pointer == self.point(p["point"])
        windows, focus, _ = self.verification_state(sampled)
        wins = [v for v in windows.values() if v["mapped"]]
        handle = p.get("window", p.get("handle"))
        if contains_ref(handle):
            return None
        if handle:
            wins = [v for v in wins if v["handle"] == handle]
        match = p.get("match", {})
        wins = [v for v in wins if self.matches(v, match)]
        if k.startswith("dialog"):
            if contains_ref(p.get("parent")):
                return None
            if (p.get("role") and tree is None) or (not p.get("role") and not handle and not any(f in match for f in ("title", "title_re", "class"))):
                return None  # No semantic profile classifier before W4.
            wins = [v for v in wins if v.get("is_modal") or v.get("transient_for") or v["type"] == "dialog"]
            if p.get("parent"):
                parent = next((v for v in windows.values() if v["handle"] == p["parent"] and v["mapped"]), None)
                if not parent:
                    return None
                wins = [v for v in wins if v["family"] == parent["family"]]
            if p.get("role") and tree:
                classified, uncertain = [], False
                for win in wins:
                    result = tree.classify(win["handle"], p["role"],
                                           getattr(self, "dialog_initial", None) if k == "dialog_present" else None)
                    if result is None:
                        uncertain = True
                        tree.ensure(win["handle"])
                    elif result:
                        classified.append(win)
                if not classified and uncertain:
                    return None
                wins = classified
        if k in ("window_focused", "focus_is"):
            return any(v["handle"] == focus for v in wins)
        return not wins if k.endswith("absent") else bool(wins)

    def expectation(self, s, handle, point):
        k = s["intent"]["kind"]
        p = s.get("expect")
        if p is None and k == "text" and s.get("_node") and getattr(self.w, "tree", None):
            p = {"kind": "text_equals", "window": handle, "ref": s["_node"]["ref"],
                 "value": self.w.tree.computed_text(s["_node"], s["intent"])}
        if p is None:
            p = {"kind": "pointer_at", "point": point} if k == "move" else (
                {"kind": "window_focused", "window": handle} if k == "focus_window" else (
                {"kind": "window_absent", "window": handle} if k == "close_window" else None))
            if p is None:
                raise ActError("invalid_expectation")
        predicate_schema(p)
        ls = leaves(p)
        if k == "move" and (p["kind"] != "pointer_at" or self.point(p["point"]) != point):
            raise ActError("invalid_expectation")
        if k == "focus_window" and (p["kind"] not in ("window_focused", "focus_is") or p.get("window") != handle):
            raise ActError("invalid_expectation")
        if k not in ("wait", "move", "focus_window") and all(v["kind"] in ("pointer_at", "window_focused", "focus_is") for v in ls):
            raise ActError("invalid_expectation")
        if k not in ("wait", "move", "focus_window") and p["kind"] == "any" and any(v["kind"] in ("pointer_at", "window_focused", "focus_is") for v in ls):
            raise ActError("invalid_expectation")
        return p

    def initially_satisfied(self, p, kind):
        if kind not in ("wait", "move", "focus_window") and p["kind"] == "all":
            strong = [v for v in p["of"] if v["kind"] not in ("pointer_at", "window_focused", "focus_is")]
            # Position/focus evidence cannot turn a preexisting application
            # state into proof that this mutation caused the desired effect.
            return self.evaluate(strong[0] if len(strong) == 1 else {"kind": "all", "of": strong})
        return self.evaluate(p)

    def arm(self, a, r):
        if r["id"] not in a["armed"]:
            self.durable("step_armed", action_id=a["params"]["action_id"], journal_seq=a["receipt"]["journal_seq"], step_id=r["id"])
            a["armed"].append(r["id"])

    def send(self, a, r, events, cleanup=False):
        if not cleanup:
            self.gate(a)
            while not self.w.covered():
                self.gate(a)
                yield
            while not self.w.settle_pixels():
                self.gate(a)
                yield
            if r.get("target_resolved"):
                yield from self.route_snapshot(a, r, r["target_resolved"])
            # Targeted macros may span multiple flush/ack groups. A focus or
            # modal transition between groups stops them before more input.
            release_only = events and all(ev[0] in ("key_up", "button_up") for ev in events)
            guard = r.get("geometry_guard")
            if guard and not release_only and self.w.cache.facets.get(guard["facet"], 1) > guard["rev"]:
                raise ActError("stale_rev")
            if "macro_focus" in r and not release_only and (self.w.cache.focus != r["macro_focus"] or self.w.cache.modal != r["macro_modal"] or
                    any(self.w.cache.facets.get(f, 1) > r["macro_rev"] for f in ("focus", "modal"))):
                raise ActError("unexpected_state")
            self.arm(a, r)
        if self.route == "evdev" and not cleanup:
            return (yield from self.send_evdev(a, r, events))
        ack = Ack([(k, d) for k, d, *xy in events], self.t.sources, self.t.raw_seq)
        start = self.clock()
        attempt = {"route": "xtest", "started_ns": int(start * 1e9), "ended_ns": None,
                   "dispatch_status": "not_dispatched", "accepted": False, "written_prefix": {"events": 0},
                   "written": 0, "expected": len(events), "server_ack": ack.receipt(),
                   "provider_result": None, "error": None, "reason": None}
        r["attempts"].append(attempt)
        a.setdefault("acks", []).append((r["id"], ack))
        r["route"] = "xtest"
        # Mark a possible extent BEFORE invoking the transport. Exceptions
        # after entry cannot establish that its buffered prefix was empty.
        attempt.update(dispatch_status="unknown", accepted=None)
        previous_dispatch_rev = r["rev"]["dispatched"]
        try:
            for event in events:
                self.w.poll_control(force=True)
                if not cleanup:
                    self.gate(a)
                k, detail, *xy = event
                # Track before write, so a write exception still releases it.
                if k == "key_down":
                    self.holds["xtest"]["key"].add(detail)
                elif k == "button_down":
                    self.holds["xtest"]["button"].add(detail)
                r["rev"]["dispatched"] = r["rev"]["dispatched"] or self.w.cache.rev
                self.t.write(event)
                attempt["written"] += 1
                attempt["written_prefix"]["events"] += 1
            self.t.flush()
        except ActError as exc:
            # Gate closure can happen between buffered primitives. Flush only
            # that submitted prefix, never add another input primitive.
            self.t.flush()
            ack.expected = ack.expected[:attempt["written"]]
            ack.feed(self.t.raw)
            attempt.update(ended_ns=int(self.clock() * 1e9), server_ack=ack.receipt(), reason=exc.reason)
            if not attempt["written"]:
                attempt.update(dispatch_status="not_dispatched", accepted=False)
                r["rev"]["dispatched"] = previous_dispatch_rev
            else:
                r["rev"]["dispatched"] = r["rev"]["dispatched"] or self.w.cache.rev
                if ack.complete:
                    attempt.update(dispatch_status="accepted", accepted=True)
            raise
        except (OSError, RuntimeError) as exc:
            attempt["error"] = str(exc)
            attempt["reason"] = "channel_down"
            r["dispatch_status"] = "unknown"
            raise ActError("channel_down")
        if events:
            r["rev"]["dispatched"] = r["rev"]["dispatched"] or self.w.cache.rev
        if "timing_ms" in r:
            r["timing_ms"]["dispatch"] += (self.clock() - start) * 1000
        due = self.clock() + (0.025 if cleanup else 0.05)
        while True:
            ack.feed(self.t.raw)
            if ack.complete or self.clock() >= due:
                break
            if not cleanup:
                try:
                    self.gate(a)
                except ActError as exc:
                    if exc.reason == "preempted":
                        a["preempted"] = True
                        yield from self.release(a, r)
                    elif exc.reason != "timeout":
                        raise
            yield
        attempt.update(ended_ns=int(self.clock() * 1e9), server_ack=ack.receipt(),
                       dispatch_status="accepted" if ack.complete else "unknown", accepted=True if ack.complete else None)
        r["dispatch_status"] = self.aggregate(r["attempts"])
        if not ack.complete:
            attempt["reason"] = "input_unacknowledged"
            if cleanup:
                return False
            raise ActError("input_unacknowledged")
        for k, detail, *xy in events:
            if k == "key_up":
                self.holds["xtest"]["key"].discard(detail)
            elif k == "button_up":
                self.holds["xtest"]["button"].discard(detail)
        return True

    def wait_evdev(self, a, diagnostic=False):
        while self.evdev.dirty and self.evdev.health['state'] != 'down':
            if diagnostic:
                if self.shutdown or (self.w.cache.control or {}).get('state') != 'agent':
                    raise ActError('preempted')
            else:
                self.gate(a)
            yield
        if self.evdev.health['state'] == 'down':
            raise ActError('channel_down')

    def send_evdev(self, a, r, events, diagnostic=False):
        point = yield from self.query("pointer", deadline=self.clock() + 0.2)
        yield from self.wait_evdev(a, diagnostic)
        epoch = self.evdev.map_epoch
        if r.get('evdev_intent_epoch', epoch) != epoch:
            raise ActError('provider_degraded')
        plan = self.evdev.plan(events, point)
        holds = self.holds["evdev"]
        # Validate the complete macro before sending any of its frames.
        offset = 0
        while offset < len(plan):
            yield from self.wait_evdev(a, diagnostic)
            if epoch != self.evdev.map_epoch:
                # A prefix may already have run. Cleanup, never replay/replan
                # a macro across a changed table.
                raise ActError('provider_degraded')
            dev = plan[offset][0]
            end = offset
            while end < len(plan) and end-offset < 64 and plan[end][0] == dev:
                end += 1
            group = plan[offset:end]
            if not diagnostic:
                self.gate(a)
            elif self.fence_reason:
                raise ActError('worker_fault', self.fence_reason)
            elif self.shutdown or (self.w.cache.control or {}).get("state") != "agent":
                raise ActError("preempted")
            expected = [event for _, _, raw in group for event in raw]
            prior_holds = {kind: set(codes) for kind, codes in holds.items()}
            prior_wire_holds = set(self.evdev.held_codes[dev])
            for device, frame, _ in group:
                self.evdev.held_codes[device].update(code for typ, code, value in frame if typ == 1 and value == 1)
            for kind, detail in expected:
                if kind in ("key_down", "button_down"):
                    holds["key" if kind == "key_down" else "button"].add(detail)
            gap = 2000 if dev == "kbd" else 0
            result = yield from self.evdev.send_frames(dev, [frame for _, frame, _ in group], expected, gap, map_epoch=epoch)
            if result['dispatch_status'] == 'not_dispatched':
                # A proven zero-write refusal (including a different worker
                # generation's pin) adds no holds. Preserve earlier groups'
                # holds, but do not fence a new worker for a rejected press.
                for kind, codes in prior_holds.items():
                    holds[kind].clear()
                    holds[kind].update(codes)
                self.evdev.held_codes[dev].clear()
                self.evdev.held_codes[dev].update(prior_wire_holds)
            if dev == 'kbd':
                r['evdev_map_epoch'] = epoch
            if not diagnostic and self.evdev.last_ack is not None:
                a.setdefault("acks", []).append((r["id"], self.evdev.last_ack))
            r["route"] = "evdev"
            r["attempts"].append(result)
            r["written"] = r.get("written", 0) + result["frames_written"]
            r["dispatch_status"] = self.aggregate(r["attempts"])
            if result["dispatch_status"] != "not_dispatched":
                r["rev"]["dispatched"] = r["rev"]["dispatched"] or self.w.cache.rev
            if result["reason"]:
                # Includes grab_lost/channel_down, even at zero frames: stop
                # this macro and the batch without another route or replay.
                raise ActError(result["reason"])
            for kind, detail in expected:
                if kind in ('key_down', 'button_down'):
                    holds['key' if kind == 'key_down' else 'button'].add(detail)
                elif kind in ("key_up", "button_up"):
                    holds["key" if kind == "key_up" else "button"].discard(detail)
            for device, frame, _ in group:
                for typ, code, value in frame:
                    if typ == 1:
                        if value:
                            self.evdev.held_codes[device].add(code)
                        else:
                            self.evdev.held_codes[device].discard(code)
            offset = end
            if dev == "kbd" and offset < len(plan) and plan[offset][0] == dev:
                yield from self.pause(gap/1e6, a if not diagnostic else None)
        return True

    def release_evdev(self, a, r):
        holds = self.holds["evdev"]
        if not any(holds.values()):
            return True
        events = [("key_up", c) for c in sorted(holds["key"])] + [("button_up", c) for c in sorted(holds["button"])]
        snapshot = tuple(events)
        previous = a.get('evdev_release')
        if previous and not previous['cleanup']['confirmed'] and previous['holds'] == snapshot:
            r['cleanup'] = previous['cleanup']
            return previous['cleanup']['confirmed']
        # Verification and finalization can both request cleanup. One attempt
        # per unchanged hold set/action, with one durable outcome, even on F1.
        try:
            self.durable('release_armed', action_id=a['params']['action_id'], route='evdev', events=events,
                         wire_holds={dev: sorted(codes) for dev, codes in self.evdev.held_codes.items()})
        except ActError:
            pass  # Release remains possible after F3.
        self.evdev.health['release_pending'] = True
        # A replacement's empty helper-local held set cannot prove release of
        # the dead helper's presses. Always replay our recorded wire holds
        # once a supervised transport has been retired.
        ok = False
        if self.evdev.health['state'] != 'down' or self.evdev.instance is None:
            ok = yield from self.evdev.release_all()
        if not ok:
            ok = yield from self.evdev.recover_release()
        # The helper reply proves writes, not X consumption. Confirm release
        # independently on this session's X connection, even after gate closure.
        confirmed = False
        deadline = self.clock() + 0.15
        while ok and self.clock() < deadline:
            try:
                keys = yield from self.query("keymap", deadline=deadline)
                pointer = yield from self.query("pointer", deadline=deadline)
                mask = sum(1 << (7+b) for b in holds["button"])
                if not set(keys) & holds["key"] and not pointer["mask"] & mask:
                    confirmed = True
                    break
            except ActError:
                break
            yield
        r["cleanup"] = {"released": events if confirmed else [], "unreleased": [] if confirmed else events, "confirmed": confirmed}
        a['evdev_release'] = {'holds': snapshot, 'cleanup': r['cleanup']}
        self.durable_cleanup(a, events, route='evdev', confirmed=confirmed, helper_instance=self.evdev.instance)
        self.evdev.health.pop('release_pending', None)
        if confirmed:
            holds["key"].clear()
            holds["button"].clear()
            for codes in self.evdev.held_codes.values():
                codes.clear()
        else:
            self.fence("F1:release_unconfirmed")
        return confirmed

    def release(self, a, r):
        evdev_ok = yield from (self.release_evdev(a, r) if self.evdev else self.noop_release())
        holds = self.holds["xtest"]
        events = [("key_up", v) for v in sorted(holds["key"], reverse=True)] + [("button_up", v) for v in sorted(holds["button"], reverse=True)]
        if not events:
            return evdev_ok
        # Preserve holds until a release is confirmed. QueryKeymap provides a
        # separate fallback for keyboard release; buttons require raw ack.
        held_keys = set(holds["key"])
        self.durable_cleanup(a, events)
        cleanup_receipt = {'id': r['id'], 'attempts': [], 'rev': {'dispatched': None},
                           'dispatch_status': 'not_dispatched'}
        try:
            ok = yield from self.send(a, cleanup_receipt, events, cleanup=True)
        except ActError:
            ok = False
        if not ok and held_keys and not any(k.startswith("button") for k, d in events):
            try:
                down = yield from self.query("keymap", deadline=self.clock() + 0.005)
                ok = not (set(down) & held_keys)
            except ActError:
                ok = False
        r["cleanup"] = {"released": events if ok else [], "unreleased": [] if ok else events, "confirmed": ok}
        r['cleanup']['attempts'] = cleanup_receipt['attempts']
        if not ok:
            holds["key"].update(held_keys)
            holds["button"].update(d for k, d in events if k == "button_up")
            self.fence("F1:release_unconfirmed")
        return ok

    @staticmethod
    def noop_release():
        if False:
            yield
        return True

    def durable_cleanup(self, a, events, **fields):
        # Release must remain possible even after F3; journal best effort only.
        try:
            self.durable("release", action_id=a["params"]["action_id"], events=events, **fields)
        except ActError:
            pass

    @staticmethod
    def aggregate(attempts):
        statuses = [v["dispatch_status"] for v in attempts]
        return "unknown" if "unknown" in statuses else "accepted" if "accepted" in statuses else "not_dispatched"

    def focus(self, a, r, handle):
        if self.w.cache.focus == handle:
            return
        for attempt in range(2):
            if attempt:
                self.w.counters["retries"] += 1
            self.gate(a)
            self.arm(a, r)
            r.setdefault("targeting_attempts", []).append({"route": "wm", "operation": "focus_window", "window": handle,
                                                          "started_ns": int(self.clock() * 1e9)})
            win = self.window(handle)
            if win is None:
                raise ActError("target_not_found")
            self.t.wm("focus_window", win["xid"])
            until = min(a["deadline"], self.clock() + 0.2)
            while self.clock() < until:
                self.gate(a)
                if self.w.cache.focus == handle and self.w.covered():
                    return
                yield
        raise ActError("focus_failed")

    def position(self, a, r, point, handle):
        yield from self.send(a, r, [("motion", 0, point["x"], point["y"])])
        for attempt in range(2):
            data = yield from self.query("pointer", deadline=min(a["deadline"], self.clock() + 0.1))
            if r.get("kind") == "move" and {"x": data["x"], "y": data["y"]} != point:
                return  # Verify, then at most one idempotent re-issue.
            if handle is None:
                if {"x": data["x"], "y": data["y"]} == point:
                    return
                raise ActError("precondition_failed")
            if {"x": data["x"], "y": data["y"]} == point and self.t.belongs(data["child"], self.window(handle)):
                return
            if attempt == 0:
                self.gate(a)
                win = self.window(handle)
                if win is None:
                    raise ActError("target_not_found")
                self.t.wm("raise_window", win["xid"])
                self.w.counters["retries"] += 1
                # Round trip on ev orders raise before the second hit test.
            else:
                raise ActError("precondition_failed", "occluded")

    def mapped_keys(self, names):
        codes = [self.t.keycode(v) for v in names]
        if any(v is None for v in codes):
            raise ActError("invalid_request", "unmapped_keysym")
        return codes

    @staticmethod
    def key_events(codes):
        return [("key_down", c) for c in codes] + [("key_up", c) for c in reversed(codes)]

    def current_tree_ref(self, a, handle, ref, base):
        """Earlier macro input can invalidate the tree we must recheck."""
        tree = self.w.tree
        until = min(a["deadline"], a["step_deadline"])
        while True:
            try:
                self.gate(a)
            except ActError as exc:
                if exc.reason == "timeout" and self.clock() >= until:
                    raise ActError("provider_degraded") from exc
                raise
            try:
                return tree.resolve(handle, ref, base)
            except ActError as exc:
                if exc.reason != "provider_degraded" or not tree.can_wait(handle) or self.clock() >= until:
                    raise
            yield

    def route_context(self, s, handle):
        intent, kind = s['intent'], s['intent']['kind']
        win = self.window(handle) or {}
        tree = getattr(self.w, 'tree', None)
        raw = getattr(tree, 'raw', {}).get(handle, {})
        nodes = getattr(getattr(tree, 'cache', None), 'windows', {}).get(handle, {}).get('nodes', {})
        node = s.get('_node')
        if kind == 'text' and node is None:
            focused = [n for n in nodes.values() if 'focused' in n.get('states', []) and
                       'editable' in n.get('states', []) and 'disabled' not in n.get('states', [])]
            if len(focused) == 1:
                node = s['_fallback_node'] = dict(focused[0])
        app = win.get('app_build') or win.get('app') or win.get('class') or 'unknown'
        identity = dict(profile_hash=self.w.profile_hash, app_build=app,
                        toolkit=raw.get('toolkit', win.get('toolkit', 'unknown')),
                        surface_role=(node or {}).get('role', raw.get('role', win.get('type', 'unknown'))),
                        operation=kind, input_class='keyboard' if kind in ('key', 'text') else 'pointer')
        routes = ladder(self.w.record['profile'], kind, intent.get('via', 'auto'),
                        all(self.t.character(c) is not None for c in intent.get('text', '')),
                        len(intent.get('text', '')), node is not None and 'editable' in node.get('states', []),
                        isinstance(s.get('target'), dict) and 'ref' in s['target'] and
                        bool((node or {}).get('actions')))
        requested = intent.get('route', 'auto')
        if requested not in ('auto', 'input'):
            routes = [route for route in routes if route == ('paste' if requested == 'clipboard' else requested)]
        apps = self.w.profile_data.get('applications', {})
        policy = apps.get(app, apps.get(win.get('class'), {})) if isinstance(apps, dict) else {}
        try:
            key = self.routes.entry(identity, routes, policy.get('synthetic_input') == 'rejected')
            # A new generation's successful physical selftest is recovery proof.
            for route in routes:
                state = self.routes.entries[key]['states'][route]
                healthy = (route == 'xtest' and self.channel['state'] == 'ok' or
                           route == 'evdev' and self.evdev and self.evdev.health['state'] == 'up' or
                           route == 'paste' and self.clipboard.health['state'] in ('ok', 'unavailable') or
                           route == 'atspi' and tree and tree.healthy(handle))
                if state['state'] == 'down' and state.get('generation') != self.w.generation and healthy:
                    self.routes.flip(key, route, 'ok')
        except OSError as exc:
            raise ActError('worker_fault', 'route_table_unavailable') from exc
        return key, routes

    def route_flip(self, key, route, state):
        try:
            self.routes.flip(key, route, state)
        except OSError as exc:
            raise ActError('worker_fault', 'route_table_unavailable') from exc

    def family_content(self, member):
        # Override-redirect WM focus/check windows can inherit the focused
        # client's family. A noninteractive, invisible 1x1 placeholder is not
        # application content. Absence of a tree alone never excludes a window.
        g, h = member['geometry'], member['handle']
        tree = getattr(self.w, 'tree', None)
        nodes = getattr(getattr(tree, 'cache', None), 'windows', {}).get(h, {}).get('nodes')
        raw = getattr(tree, 'raw', {}).get(h)
        semantic = bool(nodes or raw and raw.get('status') != 'unmatched')
        offscreen = (g['x'] + g['w'] <= 0 or g['y'] + g['h'] <= 0 or
                     g['x'] >= self.w.screen['w'] or g['y'] >= self.w.screen['h'])
        return not (member['type'] == 'popup' and g['w'] == g['h'] == 1 and offscreen and
                    not member.get('transient_for') and not member.get('is_modal') and
                    self.w.cache.focus != h and not semantic)

    def family_stamp(self, handle):
        cache = self.w.cache
        win = self.window(handle)
        if not win:
            return None
        family = win['family']
        members = [v for v in cache.windows.values() if v['family'] == family and self.family_content(v)]
        stamp = {}
        pixels = getattr(self.w, 'pixels', None)
        for member in members:
            h = member['handle']
            stamp[h] = (copy.deepcopy(member), cache.last_window.get(member['xid'], 0))
            for name in ('geometry['+h+']', 'tree['+h+']'):
                stamp[name] = cache.facets.get(name, 1)
            if pixels:
                g = member['geometry']
                for tile in pixels.tiles(dict(x=g['x'], y=g['y'], w=g['w'], h=g['h'])):
                    name = pixels.facet(tile)
                    stamp[name] = cache.facets.get(name, 1)
        for h, old in cache.tombstones.items():
            if old.get('family') == family and self.family_content(old):
                stamp['removed:'+h] = cache.removed.get(h)
        return stamp

    def route_snapshot(self, a, r, handle):
        if '_family_before' in r:
            return
        # Reuse W4's publication pump, including a finite set of already
        # building family snapshots. Do not chase the dirty flag or successors:
        # completed publications remain usable while a follow-up builds.
        tree = getattr(self.w, 'tree', None)
        if tree:
            win = self.window(handle)
            family = {v['handle'] for v in self.w.cache.windows.values()
                      if win and v['family'] == win['family'] and self.family_content(v)}
            publications = [t for t in tree.pending.values()
                            if t['op'] == 'snapshot' and t['window'] in family]
            if publications:
                tree.tick()
            while any(not t['done'] and not t['breached'] for t in publications):
                self.gate(a)
                yield
        # W3 already hashes only known dirty tiles. Finish that work before
        # stamping, rather than treating verification's cache catch-up as input
        # reaction. Never mark tiles or force an extra capture for this stamp.
        while not self.w.covered() or not self.w.settle_pixels():
            self.gate(a)
            yield
        self.gate(a)
        r['_family_before'] = self.family_stamp(handle)
        r['_focus_dispatch'] = self.w.cache.focus == handle

    def learn_route(self, r, handle):
        key, route = r.get('_route_key'), r.get('route')
        if key is None or route is None:
            return
        r['_route_learned'] = True
        attempts = [v for v in r['attempts'] if v['route'] == route]
        acknowledged = bool(attempts) and all(
            v['dispatch_status'] == 'accepted' and v.get('server_ack') is not None and
            v['server_ack']['expected'] > 0 and v['server_ack']['seen'] == v['server_ack']['expected']
            for v in attempts)
        try:
            flipped = self.routes.result(key, route, verified=r['effect']['status'] == 'verified',
                acknowledged=acknowledged, focused_before=r.get('_focus_dispatch', False),
                focused_after=self.w.cache.focus == handle,
                no_change=r.get('_family_before') is not None and r['_family_before'] == self.family_stamp(handle),
                effect=r['effect']['status'], recover=r['dispatch_status'] != 'unknown')
        except OSError as exc:
            raise ActError('worker_fault', 'route_table_unavailable') from exc
        if flipped:
            r['route_change'] = {'from': 'xtest', 'to': self.routes.next(key, r['_routes']),
                                 'reason': 'synthetic_input_rejected_suspect'}
            self.w.counters['route_changes'] += 1

    def route_exhausted(self, key, r, kind):
        if kind == 'invoke':
            raise ActError('provider_degraded')
        if self.routes.state(key, 'xtest') == 'rejected':
            r['escalate'] = 'relaunch_on_xorg' if self.w.record['profile'] == 'xvfb' else 'restart_session'
            raise ActError('backend_unsupported', 'xtest_rejected')
        raise ActError('backend_unsupported')

    @staticmethod
    def rung_attempt(route, groups, error, started, ended):
        if len(groups) == 1:
            result = groups[0]
            result['route'] = route
            return result
        statuses = [g['dispatch_status'] for g in groups]
        acked = [g['server_ack'] for g in groups if g.get('server_ack')]
        status = 'unknown' if 'unknown' in statuses else 'accepted' if 'accepted' in statuses else 'not_dispatched'
        return dict(route=route, started_ns=started, ended_ns=ended, dispatch_status=status,
                    accepted=True if status == 'accepted' else False if status == 'not_dispatched' else None,
                    written=sum(g.get('written', 0) for g in groups), expected=sum(g.get('expected', 0) for g in groups),
                    written_prefix=dict(events=sum(g.get('written', 0) for g in groups)),
                    server_ack=dict(expected=sum(v['expected'] for v in acked), seen=sum(v['seen'] for v in acked),
                                    source=acked[-1]['source']) if acked else None,
                    frames_written=sum(g.get('frames_written', 0) for g in groups),
                    provider_result=None, error=next((g['error'] for g in groups if g.get('error')), None),
                    reason=error.reason if error else None, input_groups=groups)

    def dispatch(self, a, s, r, handle, point):
        kind = s['intent']['kind']
        if kind == 'wait':
            return
        key, routes = self.route_context(s, handle)
        r['_route_key'], r['_routes'] = key, routes
        if (self.w.record['profile'] == 'xorg' and kind in ('key', 'text', 'click', 'scroll', 'move', 'drag') and
                self.routes.state(key, 'xtest') == 'rejected' and
                (not self.evdev or self.evdev.health['state'] == 'down')):
            self.route_exhausted(key, r, kind)
        initial = self.routes.next(key, routes)
        if routes and initial != routes[0]:
            r.setdefault('route_change', {'from': routes[0], 'to': initial, 'reason': 'learned_route'})
        if initial is None:
            self.route_exhausted(key, r, kind)
        for route in routes[routes.index(initial):]:
            if self.routes.state(key, route) in ('down', 'rejected'):
                continue
            if r.get('_rung_count', 0) >= 3:
                break
            r['_rung_count'] = r.get('_rung_count', 0) + 1
            offset, started, error = len(r['attempts']), int(self.clock()*1e9), None
            r['route'] = route
            try:
                yield from self.dispatch_rung(a, s, r, handle, point, route)
            except ActError as exc:
                error = exc
            finally:
                groups = r['attempts'][offset:]
                attempt = self.rung_attempt(route, groups, error, started, int(self.clock()*1e9))
                r['attempts'][offset:] = [attempt]
                if route == 'paste':
                    attempt.update(clipboard_overwritten=r.get('clipboard_overwritten', False),
                                   selection_requested=r.get('selection_requested', False))
                    self.clipboard.attempt = attempt
                    attempt['input_route'] = self.route
                r['route'] = route
                r['dispatch_status'] = self.aggregate(r['attempts'])
            if error is None:
                return
            status = attempt['dispatch_status']
            if status == 'unknown':
                if error.reason == 'input_unacknowledged':
                    self.route_flip(key, route, 'suspect')
                raise error
            if error.reason in ('channel_down', 'backend_unsupported', 'provider_degraded'):
                self.route_flip(key, route, 'down')
            # Only proof of zero delivery permits a non-idempotent replay.
            # Unknown and acknowledged absence of effect never change rungs.
            if (status != 'not_dispatched' or r['dispatch_status'] != 'not_dispatched' or
                    error.reason not in ('channel_down', 'backend_unsupported', 'provider_degraded')):
                raise error
            next_route = self.routes.next(key, routes[routes.index(route)+1:])
            if next_route is None or r['_rung_count'] >= 3:
                # Keep ordinary channel failures when no rejection escalation applies.
                if route != 'atspi' and self.routes.state(key, 'xtest') == 'rejected':
                    self.route_exhausted(key, r, kind)
                raise error
            r['route_change'] = {'from': route, 'to': next_route, 'reason': error.reason}
            self.w.counters['route_changes'] += 1
        self.route_exhausted(key, r, kind)

    def dispatch_rung(self, a, s, r, handle, point, route):
        intent, k = s['intent'], s['intent']['kind']
        self.route = route if route in ('evdev', 'xtest') else 'xtest'
        if route == 'paste':
            key = r['_route_key']
            physical = ['evdev', 'xtest'] if self.w.record['profile'] == 'xorg' else ['xtest']
            self.route = next((v for v in physical if
                              (v == 'xtest' and self.channel['state'] == 'ok' or
                               v == 'evdev' and self.evdev and self.evdev.health['state'] == 'up')), None)
            if self.route is None:
                raise ActError('backend_unsupported', 'paste_input_unavailable')
        if self.route == 'evdev':
            if not self.evdev or self.evdev.health['state'] != 'up':
                raise ActError('channel_down')
            self.evdev.guard(intent)
            yield from self.wait_evdev(a)
            r['evdev_intent_epoch'] = self.evdev.map_epoch
            if not a.get('evdev_grab_checked'):
                a['evdev_grab_checked'] = True
                if not (yield from self.evdev.grab_check()):
                    raise ActError('channel_down')
        # Resolve the entire macro before positioning/focusing/arming it.
        end = self.point(intent["to"], r["rev"]["base"]) if k == "drag" else None
        if end is not None:
            self.guards(a, s, r["rev"]["base"], handle, end)
        codes = self.mapped_keys(intent["keys"]) if k == "key" else []
        mods = self.mapped_keys(intent.get("modifiers", []))
        text, insertion_keys = None, None
        if k == "text" and route not in ("paste", "atspi"):
            text = [self.t.character(c) for c in intent["text"]]
            if any(v is None for v in text):
                raise ActError("backend_unsupported", "unmappable_text")
        if k == "text" and route != "atspi":
            select_all = self.mapped_keys(self.t.select_all(handle)) if intent["replace"] else []
            if (s.get("_node") and "focused" not in s["_node"].get("states", []) and not intent["replace"]):
                insertion_keys = (self.mapped_keys(["Control_L", "Home"]), self.mapped_keys(["Right"]),
                                  self.mapped_keys(["Shift_L", "Right"]))
        paste_codes = []
        if route == 'paste':
            try:
                paste_codes = self.mapped_keys(['Control_L', 'v'])
            except ActError as exc:
                raise ActError('backend_unsupported', 'unmappable_paste_shortcut') from exc
        if route != "atspi" and self.route == "evdev":
            all_codes = codes + mods + paste_codes
            if route == "paste":
                all_codes += select_all
            if k == "text" and route != "paste":
                all_codes += select_all + [c for group in text for c in group]
                if insertion_keys:
                    all_codes += [c for group in insertion_keys for c in group]
            if any(self.evdev.keycodes.get(c) in (None, 99) for c in all_codes):
                raise ActError("invalid_request", "unmapped_evdev_key")
        if k == "wait":
            return
        if route not in ("atspi", "wm") and self.route == "xtest" and self.channel["state"] != "ok":
            raise ActError("channel_down")
        if point and handle is not None:
            facet = "geometry[" + handle + "]"
            r.setdefault("geometry_guard", {"facet": facet, "rev": self.w.cache.facets.get(facet, 1)})
        if s.get("_node"):
            yield from self.current_tree_ref(a, handle, s["_node"]["ref"], r["rev"]["base"])
            r["tree_guard"] = (handle, s["_node"]["ref"])
        if route == "atspi":
            node = s.get("_node") or s.get("_fallback_node")
            if node is None or k == "text" and "editable" not in node.get("states", []):
                raise ActError("backend_unsupported")
            node = yield from self.current_tree_ref(a, handle, node["ref"], r["rev"]["base"])
            if k == "text" and "editable" not in node.get("states", []):
                raise ActError("backend_unsupported")
            if k == 'text' and not s.get('_node') and 'focused' not in node.get('states', []):
                raise ActError('precondition_failed')
            yield from self.focus(a, r, handle)
            yield from self.route_snapshot(a, r, handle)
            fields = dict(text=intent["text"], focused_required=not bool(s.get('_node'))) if k == "text" else dict(action=intent.get("action"))
            self.gate(a)
            self.arm(a, r)
            attempt = {"route": "atspi", "dispatch_status": "not_dispatched", "accepted": None,
                       "written": 0, "expected": 1, "written_prefix": {}, "server_ack": None,
                       "provider_result": None, "error": None, "reason": None,
                       "started_ns": int(self.clock()*1e9), "ended_ns": None}
            task = self.w.tree.request("set_text" if k == "text" else "invoke", handle, key=node["key"],
                                       role=node["role"], name=node.get("name", ""), **fields,
                                       control=str(self.w.sd / "control"), generation=self.w.generation,
                                       epoch=a["params"]["control_epoch"], deadline=min(a["deadline"], a["step_deadline"]))
            r["attempts"].append(attempt)
            if task is None:
                raise ActError("provider_degraded")
            r["route"] = "atspi"
            r["rev"]["dispatched"] = self.w.cache.rev
            attempt["dispatch_status"] = r["dispatch_status"] = "unknown"
            while not task["done"] and not task["breached"]:
                self.gate(a)
                yield
            attempt["ended_ns"] = int(self.clock()*1e9)
            if task.get("error") or task["breached"]:
                attempt["error"] = task.get("error") or "provider_deadline"
                if task.get("error") in ("stale_rev", "invalid_request", "backend_unsupported", "preempted", "precondition_failed"):
                    attempt.update(dispatch_status="not_dispatched", accepted=False)
                    r["dispatch_status"] = "not_dispatched"
                    raise ActError(task["error"])
                raise ActError("provider_degraded")
            attempt["provider_result"] = task["result"]
            accepted = task["result"].get("accepted", False)
            attempt.update(dispatch_status="accepted" if accepted else "not_dispatched", accepted=accepted, written=int(accepted))
            r["dispatch_status"] = attempt["dispatch_status"]
            if not accepted:
                raise ActError("effect_not_observed")
            return
        if k in ("key", "text"):
            focus_started = self.clock()
            yield from self.focus(a, r, handle)
            r["timing_ms"]["focus"] += (self.clock() - focus_started) * 1000
            r["focus"]["confirmed"] = self.w.cache.focus
            r["macro_focus"], r["macro_modal"] = self.w.cache.focus, list(self.w.cache.modal)
            r["macro_rev"] = self.w.cache.rev
            if k == "text" and s.get("_node"):
                if "editable" not in s["_node"].get("states", []):
                    raise ActError("precondition_failed")
                if "focused" not in s["_node"].get("states", []):
                    yield from self.position(a, r, point, handle)
                    yield from self.current_tree_ref(a, handle, s["_node"]["ref"], r["rev"]["base"])
                    yield from self.send(a, r, [("button_down", 1), ("button_up", 1)])
                    if not intent["replace"]:
                        node = s["_node"]
                        start, end = sorted(node.get("selection", [node.get("caret", 0)] * 2))
                        home, right, select_right = insertion_keys
                        yield from self.send(a, r, self.key_events(home))
                        for _ in range(start):
                            yield from self.send(a, r, self.key_events(right))
                        for _ in range(end-start):
                            yield from self.send(a, r, self.key_events(select_right))
        if point and k in ("click", "scroll", "drag"):
            if s.get("_node"):
                yield from self.focus(a, r, handle)
                r["focus"]["confirmed"] = self.w.cache.focus
            r["macro_focus"], r["macro_modal"] = self.w.cache.focus, list(self.w.cache.modal)
            r["macro_rev"] = self.w.cache.rev
            yield from self.position(a, r, point, handle)
        if route == 'paste':
            self.gate(a)
            self.arm(a, r)
            yield from self.clipboard.own(intent['text'], handle, lambda: self.gate(a), r)
            if intent['replace']:
                yield from self.send(a, r, self.key_events(self.mapped_keys(self.t.select_all(handle))))
            yield from self.send(a, r, self.key_events(paste_codes))
            return
        if k in ("focus_window", "close_window"):
            yield from self.route_snapshot(a, r, handle)
            self.gate(a)
            self.arm(a, r)
            start = self.clock()
            attempt = {"route": "wm", "started_ns": int(start * 1e9), "ended_ns": int(start * 1e9),
                       "dispatch_status": "unknown", "accepted": None, "written_prefix": {"messages": 0},
                       "written": 0, "expected": 1, "server_ack": {"expected": 0, "seen": 0, "source": "wm"},
                       "provider_result": None, "error": None, "reason": None}
            r["route"] = "wm"
            r["attempts"].append(attempt)
            r["rev"]["dispatched"] = self.w.cache.rev
            self.t.wm(k, self.window(handle)["xid"])
            attempt.update(dispatch_status="accepted", accepted=True, written=1, written_prefix={"messages": 1})
            r["dispatch_status"] = "accepted"
        elif k == "key":
            yield from self.send(a, r, self.key_events(codes))
        elif k == "text":
            if select_all:
                yield from self.send(a, r, self.key_events(select_all))
            for offset in range(0, len(text), 32):
                self.gate(a)
                group = text[offset:offset + 32]
                delay = self.t.inter_key_delay_us(handle) / 1e6
                if delay:
                    for char_codes in group:
                        yield from self.send(a, r, self.key_events(char_codes))
                        yield from self.pause(delay, a)
                else:
                    yield from self.send(a, r, [ev for cc in group for ev in self.key_events(cc)])
        elif k == "move":
            yield from self.position(a, r, point, handle)
        else:
            if mods:
                yield from self.send(a, r, [("key_down", c) for c in mods])
            button = {"left": 1, "middle": 2, "right": 3}[intent.get("button", "left")]
            if k == "click":
                yield from self.pause(0.01, a)
                if s.get("_node"):
                    yield from self.current_tree_ref(a, handle, s["_node"]["ref"], r["rev"]["base"])
                for n in range(intent["count"]):
                    yield from self.send(a, r, [("button_down", button)])
                    yield from self.pause(0.015, a)
                    yield from self.send(a, r, [("button_up", button)])
                    if n + 1 < intent["count"]:
                        yield from self.pause(0.06, a)
            elif k == "scroll":
                events = []
                for value, neg, pos in ((intent["dy"], 4, 5), (intent["dx"], 6, 7)):
                    b = neg if value < 0 else pos
                    for _ in range(abs(value)):
                        events.extend((("button_down", b), ("button_up", b)))
                yield from self.send(a, r, events)
            elif k == "drag":
                yield from self.send(a, r, [("button_down", button)])
                count = max(9, (intent["duration_ms"] + 7) // 8)
                for n in range(1, count + 1):
                    self.gate(a)
                    x = round(point["x"] + (end["x"] - point["x"]) * n / count)
                    y = round(point["y"] + (end["y"] - point["y"]) * n / count)
                    yield from self.send(a, r, [("motion", 0, x, y)])
                    yield from self.pause(intent["duration_ms"] / count / 1000, a)
                yield from self.send(a, r, [("button_up", button)])
            if mods:
                yield from self.send(a, r, [("key_up", c) for c in reversed(mods)])

    def predicted(self, p):
        return any(v["kind"] in ("dialog_present", "dialog_absent", "window_present", "window_absent", "window_focused", "focus_is") for v in leaves(p))

    def transition_expected(self, r, p, sampled=False):
        cache = self.w.cache
        windows, new_focus, new_modal = self.verification_state(sampled)
        old_focus, old_modal = r["focus"].get("confirmed", r["focus"]["before"]), set(r["modal"]["before"])
        dialog_effect = any(v["kind"] in ("dialog_present", "dialog_absent") for v in leaves(p))
        appearance = any(v["kind"] in ("window_present", "dialog_present") for v in leaves(p))
        tree = getattr(self.w, "tree", None)
        if tree and any(v["kind"].startswith("dialog") and v.get("role") and
                        self.evaluate(v, sampled) is None for v in leaves(p)):
            return None
        if old_focus == new_focus and old_modal == new_modal and not dialog_effect and not appearance:
            return True
        # Focus may name an as-yet unsampled client (derive then yields None).
        # Pending metadata is uncertainty, never evidence of a foreign change.
        def names(handle, present):
            win = next((v for v in windows.values() if v["handle"] == handle and v["mapped"]),
                       cache.tombstones.get(handle))
            for v in leaves(p):
                k = v["kind"]
                if k not in STATE or k == "pointer_at":
                    continue
                if present and k.endswith("absent") or not present and not k.endswith("absent"):
                    continue
                subject = v.get("window", v.get("handle"))
                if subject and subject != handle:
                    continue
                if not subject and not v.get("match") and not v.get("role"):
                    continue
                if not win or not self.matches(win, v.get("match", {})):
                    continue
                if k.startswith("dialog") and v.get("role") and tree and present and not tree.classify(
                        handle, v["role"], getattr(self, "dialog_initial", None)):
                    continue
                if k.startswith("dialog") and not (win.get("transient_for") or win.get("is_modal") or win["type"] == "dialog"):
                    continue
                if k.startswith("dialog") and v.get("parent"):
                    parent = next((w for w in windows.values() if w["handle"] == v["parent"] and w["mapped"]), None)
                    if not parent or win["family"] != parent["family"]:
                        continue
                if present or self.evaluate(v, sampled) is True:
                    return True
            return False
        if dialog_effect or appearance:
            before = r.get("windows_before", {})
            now = {win["handle"]: win for win in windows.values() if win["mapped"]}
            # Family is derived metadata (including popup focus fallback), not
            # an independent application mutation. Ignore only that field.
            def state(win):
                return {k: v for k, v in win.items() if k != "family"} if win else None
            for handle in before.keys() | now.keys():
                if state(before.get(handle)) == state(now.get(handle)) or handle == r["target"]:
                    continue
                if not names(handle, handle in now):
                    return False
        # Null focus is the gap between losing the old client and resolving a
        # new one. It supplies no positive evidence against an appearance.
        if not sampled and not self.w.covered():
            return None
        # A named disappearing modal permits focus to return to its parent.
        removed = old_modal - new_modal
        removed_named = bool(removed) and all(names(h, False) for h in removed)
        # Focusing the step's target or its family is an explained transition,
        # including when a pure tree expectation cannot name X state.
        target = next((w for w in windows.values() if w["handle"] == r["target"] and w["mapped"]), None)
        focused = next((w for w in windows.values() if w["handle"] == new_focus and w["mapped"]), None)
        target_focus = new_focus is not None and (new_focus == r["target"] or
                       bool(target and focused and target["family"] == focused["family"]))
        focus_ok = ((appearance and new_focus is None) or old_focus == new_focus or
                    target_focus or names(new_focus, True) or names(old_focus, False) or removed_named)
        # A newly modal target is explained, but a new modal child must still
        # be named: the opening step targets its parent, including D5 popups.
        return (focus_ok and all(h == r["target"] or names(h, True) for h in new_modal - old_modal)
                and all(names(h, False) for h in removed))

    def evidence(self, p, sampled=False):
        p = self.effective_predicate(p)
        if predicate_class(p) == 'ocr' and getattr(self.w, 'ocr', None):
            return self.w.ocr.evidence(p)
        if predicate_class(p) == "composite":
            return [e for leaf in leaves(p) if self.evaluate(leaf, sampled) is True
                    for e in self.evidence(leaf, sampled)]
        comparison = {"predicate": p, "result": True}
        if isinstance(sampled, dict) and "classifiers" in sampled:
            comparison["classifiers"] = list(sampled["classifiers"].values())
        tree_evidence = predicate_class(p) == "tree"
        return [{"kind": "tree" if tree_evidence else "state", "provider": "atspi" if tree_evidence else "x11", "rev": self.w.cache.rev, "sample_ns": int(self.clock() * 1e9),
                 "scope": {"kind": "session"}, "hash": None, "ref": None, "coverage": "complete", "redactions": [],
                 "comparison": comparison}]

    def tree_expectation(self, p):
        p = self.effective_predicate(p)
        return any((predicate_class(leaf) == 'tree' or
                    predicate_class(leaf) == 'ocr' and getattr(self.w, 'ocr', None) is not None) or
                   (leaf["kind"] == "focus_is" and "ref" in leaf) or
                   (leaf["kind"].startswith("dialog_") and leaf.get("role")) for leaf in leaves(p))

    def tree_undecided(self, p):
        p = self.effective_predicate(p)
        ocr = getattr(self.w, 'ocr', None)
        ocr_wait = any(predicate_class(leaf) == 'ocr' for leaf in leaves(p))
        if ocr_wait and (ocr is None or ocr.state == 'down'):
            return False
        tree = getattr(self.w, "tree", None)
        if tree is None:
            return ocr_wait and ocr.flight is not None
        windows = []
        for leaf in leaves(p):
            if predicate_class(leaf) == "tree" or (leaf["kind"] == "focus_is" and "ref" in leaf):
                windows.append(tree.predicate_window(leaf))
            elif leaf["kind"].startswith("dialog_") and leaf.get("role"):
                windows.extend(win["handle"] for win in self.w.cache.windows.values()
                               if win["mapped"] and self.matches(win, leaf.get("match", {})) and
                               (not leaf.get("window") or win["handle"] == leaf["window"]) and
                               (win.get("is_modal") or win.get("transient_for") or win["type"] == "dialog"))
        return (bool(windows) or ocr_wait) and all(tree.can_wait(h) for h in windows) and (not ocr_wait or ocr.flight is not None)

    def verification_verdict(self, p, sampled, dispatched):
        """Admission/wait reads stay independent of post-input freshness."""
        p = self.effective_predicate(p)
        if p['kind'] in ('all', 'any'):
            results = [self.verification_verdict(v, sampled, dispatched) for v in p['of']]
            values, contrary = zip(*results)
            if p['kind'] == 'all':
                value = False if False in values else None if None in values else True
                return value, any(contrary)
            value = True if True in values else None if None in values else False
            return value, all(contrary)
        tree = getattr(self.w, 'tree', None)
        if tree and dispatched is not None:
            if predicate_class(p) == 'tree':
                return tree.effect_verdict(p, dispatched)
            if p['kind'] == 'focus_is' and 'ref' in p:
                return tree.effect_verdict(dict(p, kind='node_state', state='focused'), dispatched)
        return self.evaluate(p, sampled), False

    def verify(self, a, r, p, until):
        final = False
        sampled = False
        sampled_rev = None
        a["verification_sampled"] = False
        # PropertyNotify is a hint, not proof that the cache is current. Sample
        # after the initial event window, then at most ten times a second while
        # waiting. A lost notification must not cost the full step timeout.
        refresh_at = min(until, self.clock() + 0.04)
        while True:
            if sampled_rev != self.w.cache.rev:
                sampled = False
            try:
                self.gate(a)
            except ActError as exc:
                if exc.reason == "preempted":
                    a["preempted"] = True
                    yield from self.release(a, r)
                elif exc.reason != "timeout":
                    # Verification remains read-only under a fence too.
                    yield from self.release(a, r)
            if not self.w.settle_pixels():
                yield
                continue
            value, contrary = self.verification_verdict(p, sampled, r['rev']['dispatched'])
            old_focus = r["focus"].get("confirmed", r["focus"]["before"])
            dispatch_rev = r["rev"]["dispatched"]
            focus_gap = dispatch_rev is not None and self.w.cache.focus == old_focus and self.w.cache.facets.get("focus", 1) > dispatch_rev
            if focus_gap and p["kind"] != "pointer_at":
                return "unknown", "focus_lost"
            if value is True and p["kind"] == "pointer_at":
                a["verification_sampled"] = sampled
                r["unexpected_transient"] = focus_gap
                self.w.cache.commit(True)
                r["rev"]["verified"] = self.w.cache.rev
                return "verified", None
            transition = self.transition_expected(r, p, sampled)
            if transition is False or contrary:
                return "contradicted", "effect_contradicted"
            if value is True and transition is True:
                a["verification_sampled"] = sampled
                self.w.cache.commit(True)
                r["rev"]["verified"] = self.w.cache.rev
                return "verified", None
            if value is None and self.w.covered() and not self.tree_undecided(p):
                return "unknown", "provider_degraded"
            if final:
                return ("unknown", "provider_degraded") if value is None or transition is None else ("not_observed", "timeout")
            if self.clock() >= refresh_at:
                # Keep a final server sample even after earlier fallback reads.
                # Only this bounded read-only look may outlive the step window.
                final = self.clock() >= until
                due = self.clock() + 0.05
                try:
                    sample = yield from self.query("refresh", p, r["target"], deadline=due)
                    sampled = sample if sample.get("complete", False) else False
                    sampled_rev = self.w.cache.rev
                    if sampled:
                        # Consume this completed read before another event turn
                        # can expire it merely by advancing a geometry facet.
                        refresh_at = min(until, self.clock() + 0.1)
                        continue
                except ActError:
                    if final:
                        # Incomplete fresh evidence is not conclusive absence.
                        return "unknown", "provider_degraded"
                refresh_at = min(until, self.clock() + 0.1)
            yield

    def bound(self, value, bindings, field=None):
        if isinstance(value, dict) and set(value) == {"from_step", "name"}:
            item = bindings.get(value["from_step"], {}).get(value["name"])
            if item is None:
                raise ActError("invalid_request", "unverified_binding")
            if field == "target":
                return copy.deepcopy(item)
            return item.get("ref", item.get("window")) if isinstance(item, dict) else item
        if isinstance(value, dict):
            return {k: self.bound(v, bindings, k) for k, v in value.items()}
        if isinstance(value, list):
            return [self.bound(v, bindings, field) for v in value]
        return value

    def exports(self, p):
        tree = getattr(self.w, "tree", None)
        if not tree:
            return {}
        output = {}
        for leaf in leaves(p):
            if leaf["kind"] != "dialog_present" or not leaf.get("bind"):
                continue
            parent = self.window(leaf.get("parent"))
            candidates = [win for win in self.w.cache.windows.values() if win["mapped"] and
                          (not parent or win["family"] == parent["family"]) and
                          (win.get("is_modal") or win.get("transient_for") or win.get("type") == "dialog") and
                          self.matches(win, leaf.get("match", {}))]
            matches = [(win, tree.classify(win["handle"], leaf.get("role", "dialog"),
                        getattr(self, "dialog_initial", None), remember=True, completed=True)) for win in candidates]
            matches = [(win, result) for win, result in matches if result]
            if len(matches) != 1:
                raise ActError("invalid_request", "ambiguous_dialog_binding")
            win, result = matches[0]
            for name, selector in leaf["bind"].items():
                if selector == "dialog":
                    output[name] = {"window": win["handle"]}
                else:
                    # The adopted rename classifier supplies the unique editable
                    # text field (GTK exposes it as either text or entry).
                    if selector == {"role": "text"} and "entry" in result:
                        ref = result["entry"]
                    else:
                        ref = tree.resolve(win["handle"], dict(provider="atspi", **selector), completed=True)["ref"]
                    output[name] = {"window": win["handle"], "ref": ref}
        return output

    def admit_tree(self, a, read):
        """Wait for recoverable, undecided admission reads within this step."""
        waiting = False
        while True:
            if waiting:
                try:
                    self.gate(a)
                except ActError as exc:
                    if exc.reason == "timeout":
                        raise ActError("provider_degraded") from exc
                    raise
            try:
                return read()
            except TreePending as exc:
                if not self.w.tree.can_wait(exc.window):
                    raise ActError("provider_degraded") from exc
                waiting = True
            except OCRPending:
                if not getattr(self.w, 'ocr', None) or self.w.ocr.state == 'down':
                    raise ActError('provider_degraded')
                waiting = True
            yield

    def run(self, a):
        p, receipt = a["params"], a["receipt"]
        state = self.lease_state.get(p["lease"], {})
        base = p["base_rev"]
        stop_reason, stopping = None, None
        receipt["timing_ms"]["queue"] = (self.clock() - a["received"]) * 1000
        try:
            self.gate(a)
            if self.pending_effects:
                raise ActError("awaiting_reconciliation")
            dirty = yield from self.query("pointer", deadline=min(a["deadline"], self.clock() + 0.1))
            if dirty.get("mask", 0) & 255:
                r = receipt["steps"][0]
                yield from self.release(a, r)
                self.w.counters["retries"] += 1
                dirty = yield from self.query("pointer", deadline=min(a["deadline"], self.clock() + 0.1))
                if dirty.get("mask", 0) & 255:
                    raise ActError("precondition_failed", "modifier_state_dirty")
            bindings = {}
            self.dialog_initial = None
            for s, r in zip(p["steps"], receipt["steps"]):
                stopping = r
                started = self.clock()
                r["disposition"], r["reason"], r["stop_reason"] = "failed", None, None
                r["rev"]["base"] = base
                r["focus"]["before"] = self.w.cache.focus
                r["modal"]["before"] = list(self.w.cache.modal)
                self.w.counters["steps"] += 1
                try:
                    until = min(a["deadline"], self.clock() + s["timeout_ms"] / 1000)
                    a["step_deadline"] = until
                    self.gate(a)
                    while not self.w.covered():
                        self.gate(a)
                        yield
                    if s.get("risk"):
                        raise ActError("confirm_required")
                    while not self.w.settle_pixels():
                        self.gate(a)
                        yield
                    s = self.bound(s, bindings)
                    handle, point = yield from self.admit_tree(a, lambda: self.resolve(s, base))
                    if getattr(self.w, "tree", None) and "selected_ref" in s["require"]:
                        selected = yield from self.admit_tree(a, lambda: self.w.tree.resolve(
                            handle, s["require"]["selected_ref"], base, completed=True))
                        self.dialog_initial = selected.get("name") or selected.get("text")
                    yield from self.admit_tree(a, lambda: self.guards(a, s, base, handle, point))
                    r["target_resolved"] = r["target"] = handle
                    expect = self.expectation(s, handle, point)
                    r["expect"] = expect
                    if any(v["kind"] in ("dialog_present", "dialog_absent", "window_present") for v in leaves(expect)):
                        r["windows_before"] = copy.deepcopy({win["handle"]: win for win in self.w.cache.windows.values() if win["mapped"]})
                    r["predicate_class"] = predicate_class(expect)
                    k = s["intent"]["kind"]
                    before = yield from self.admit_tree(a, lambda: self.initially_satisfied(expect, k))
                    if k != "wait" and self.tree_expectation(expect):
                        while before is None and self.clock() < until:
                            if not self.tree_undecided(expect):
                                raise ActError("provider_degraded")
                            self.gate(a)
                            yield
                            if self.clock() >= until:
                                break
                            before = yield from self.admit_tree(a, lambda: self.initially_satisfied(expect, k))
                        if before is None:
                            raise ActError("provider_degraded")
                    if before is True and k != "wait":
                        if k in ("move", "focus_window") or (k == "text" and s.get("_node") and
                                s["intent"]["replace"] and expect.get("kind") == "text_equals" and
                                expect.get("ref") == s["_node"]["ref"] and
                                expect.get("value") == s["intent"]["text"]):
                            r.update(disposition="satisfied_without_dispatch", reason="precondition_already_satisfied")
                            r["rev"]["verified"] = self.w.cache.rev
                            r["effect"] = self.evaluated_effect('verified', expect)
                        else:
                            raise ActError("invalid_expectation", "precondition_already_satisfied")
                    else:
                        receipt["phase"] = "dispatch"
                        yield from self.admit_tree(a, lambda: self.guards(a, s, base, handle, point))
                        dispatch_started = self.clock()
                        yield from self.dispatch(a, s, r, handle, point)
                        r["timing_ms"]["application_wait"] = (self.clock() - dispatch_started) * 1000
                        receipt["phase"] = "verify"
                        verify_started = self.clock()
                        outcome, reason = yield from self.verify(a, r, expect, until)
                        # Only absolute move and focus may reissue once, after
                        # definite acceptance and conclusive absence of effect.
                        if outcome == "not_observed" and k in ("move", "focus_window") and self.clock() < a["deadline"] and not a["preempted"]:
                            self.w.counters["retries"] += 1
                            a["step_deadline"] = min(a["deadline"], self.clock() + s["timeout_ms"] / 1000)
                            yield from self.dispatch(a, s, r, handle, point)
                            outcome, reason = yield from self.verify(a, r, expect, min(a["deadline"], self.clock() + s["timeout_ms"] / 1000))
                        r["disposition"] = "executed" if r["attempts"] or k == "wait" else "failed"
                        r["effect"] = self.evaluated_effect(outcome, expect, a.get('verification_sampled', False))
                        r["reason"] = reason
                        r["timing_ms"]["verify"] = (self.clock() - verify_started) * 1000
                    effective = self.effective_predicate(expect)
                    if effective != expect:
                        r['effect'].update(predicate=effective, fallback_from='tree')
                    r['predicate_class'] = predicate_class(effective)
                    r["effect"].update(predicate_class=r["predicate_class"], expect_source=r["expect_source"])
                    r["focus"]["after"], r["modal"]["after"] = self.w.cache.focus, list(self.w.cache.modal)
                    self.learn_route(r, handle)
                    if a["preempted"]:
                        raise ActError("preempted")
                    if r["effect"]["status"] != "verified":
                        stop_reason = r["reason"]
                        break
                    if r.get("unexpected_transient") or not self.transition_expected(r, expect, a.get("verification_sampled", False)):
                        stop_reason = r["reason"] = "unexpected_state"
                        break
                    if getattr(self.w, "tree", None):
                        r["bindings"] = yield from self.admit_tree(a, lambda: self.exports(expect))
                        bindings[s["id"]] = r["bindings"]
                    base = r["rev"]["verified"]
                except ActError as exc:
                    r["reason"], r["detail"] = exc.reason, exc.detail
                    r["dispatch_status"] = self.aggregate(r["attempts"])
                    submitted = r["dispatch_status"] != "not_dispatched"
                    if submitted:
                        # A preempted in-flight step retains read-only
                        # verification to its original normal deadline.
                        if exc.reason in ("preempted", "input_unacknowledged", "channel_down", "timeout", "unexpected_state"):
                            a["preempted"] = a["preempted"] or exc.reason == "preempted"
                            yield from self.release(a, r)
                            outcome, _ = yield from self.verify(a, r, r["expect"], until)
                        else:
                            outcome = "unknown"
                        r["effect"] = self.evaluated_effect(outcome, r["expect"])
                        r["disposition"] = "executed"
                    else:
                        r["effect"] = self.evaluated_effect('not_observed', r['expect'])
                    if exc.reason == 'input_unacknowledged' and r.get('_route_key'):
                        self.route_flip(r['_route_key'], r['route'], 'suspect')
                    stop_reason = exc.reason
                    break
                finally:
                    self.clipboard.attempt = self.clipboard.step_evidence = None
                    if not r.get('_route_learned') and r.get('_route_key'):
                        self.learn_route(r, r.get('target_resolved'))
                    a.pop("step_deadline", None)
                    r["focus"]["after"], r["modal"]["after"] = self.w.cache.focus, list(self.w.cache.modal)
                    r["timing_ms"]["total"] = (self.clock() - started) * 1000
        except ActError as exc:
            stop_reason = exc.reason
            # Exceptions during a step's error handling still belong to that
            # step. Only failures before the loop are attributed to step one.
            if stopping is None:
                stopping = receipt["steps"][0]
            if stopping["effect"]["status"] != "verified":
                stopping["disposition"] = "executed" if stopping["dispatch_status"] != "not_dispatched" else "failed"
            stopping.update(reason=exc.reason, detail=exc.detail, stop_reason=None)
        r = stopping or receipt["steps"][-1]
        yield from self.release(a, r)
        # F2: unknown delivery plus an independent, gated pointer probe. Do
        # not send probe input after takeover/deadline/fence.
        if any(v["dispatch_status"] == "unknown" for v in receipt["steps"]):
            self.unknown_streak += 1
            if self.unknown_streak >= 2:
                self.fence("F2:delivery_unknown")
            else:
                try:
                    self.gate(a)
                    origin = yield from self.query("pointer", deadline=self.clock() + 0.05)
                    probe = {"id": r["id"], "route": None, "attempts": [], "rev": {"dispatched": None}, "dispatch_status": "not_dispatched"}
                    r['channel_probe'] = probe['attempts']
                    x, y = origin["x"], origin["y"]
                    if (self.route == 'evdev' and self.evdev.health['state'] == 'down' and
                            r.get('reason') != 'input_unacknowledged'):
                        # Preserve the independent F2 liveness check after a
                        # helper failure; incomplete raw ack probes its own route.
                        self.route = 'xtest'
                    yield from self.send(a, probe, [("motion", 0, x + (1 if x + 1 < self.w.screen["w"] else -1), y), ("motion", 0, x, y)])
                    r["channel_probe"] = probe["attempts"]
                except ActError as exc:
                    if exc.reason in ("input_unacknowledged", "channel_down"):
                        if self.route == "evdev" and self.evdev:
                            self.evdev.down("input_unacknowledged")
                            if r.get("_route_key"):
                                self.route_flip(r["_route_key"], r["route"], "down")
                        else:
                            self.channel["state"] = "down"
                            if r.get("_route_key"):
                                self.route_flip(r["_route_key"], r["route"], "down")
                        self.fence("F2:probe_failed")
        else:
            self.unknown_streak = 0
        # Settle only on rev-bearing events; raw acknowledgements themselves
        # do not postpone the quiet window indefinitely.
        cap, quiet, rev = self.clock() + 0.1, self.clock() + 0.04, self.w.cache.rev
        while self.clock() < min(cap, quiet):
            if self.w.cache.rev != rev:
                rev, quiet = self.w.cache.rev, self.clock() + 0.04
            yield
        while not self.w.settle_pixels():
            yield
        pixels = getattr(self.w, "pixels", None)
        if pixels:
            pixels.receipt_frames(receipt)
        receipt["state_delta"] = self.w.cache.observe({"kind": "session"}, p["base_rev"], self.w.covered())
        receipt["state_delta"]["settled"] = self.clock() >= quiet
        self.finish(a, stop_reason, stopping)

    def finish(self, a, reason, stopping):
        r, p = a["receipt"], a["params"]
        steps = r["steps"]
        completed = [s["id"] for s in steps if s["effect"]["status"] == "verified"]
        aggregate = self.aggregate(steps)
        stopped = stopping if reason else None
        r.update(completed_steps=completed, steps_verified=len(completed), reason=reason,
                 stopped_step=stopped["id"] if stopped else None, stopped_at=stopped["i"] if stopped else None,
                 unexecuted_steps=[s["id"] for s in steps if s["disposition"] == "unexecuted"],
                 dispatch_status=aggregate,
                 dispatched=any(any(t.get("written", 0) for t in s["attempts"]) for s in steps),
                 effect=copy.deepcopy((stopped or steps[-1])["effect"]))
        r["status"] = "success" if not reason else "partial" if completed else "unknown" if r["effect"]["status"] == "unknown" else "failed"
        if not reason:
            r["phase"] = "complete"
        r["rev"].update(dispatched=next((s["rev"]["dispatched"] for s in steps if s["rev"]["dispatched"] is not None), None),
                         verified=next((s["rev"]["verified"] for s in reversed(steps) if s["rev"]["verified"] is not None), None), final=self.w.cache.rev)
        handles = {s["target_resolved"] for s in steps if s["target_resolved"]}
        r["window"] = next(iter(handles)) if len(handles) == 1 else None
        r["advice"] = self.advice(reason, aggregate)
        r["needs_human"] = bool(self.fence_reason)
        unresolved = [s for s in steps if s["dispatch_status"] == "unknown" or
                      s["dispatch_status"] == "accepted" and s["effect"]["status"] != "verified"]
        for s in unresolved:
            self.pending_effects.append({"action_id": p["action_id"], "step_id": s["id"], "expect": s["expect"],
                                         "epoch": p["control_epoch"], "delivery_quiescent": s["dispatch_status"] == "accepted" and s["cleanup"]["confirmed"],
                                         "rev": self.w.cache.rev})
        for s in steps:
            s.pop("windows_before", None)
            for field in ('_route_key', '_routes', '_rung_count', '_family_before', '_focus_dispatch', '_route_learned'):
                s.pop(field, None)
            if s['effect'].get('fallback_from') == 'tree':
                s['predicate_class'] = predicate_class(s['effect']['predicate'])
            s["effect"].update(predicate_class=s["predicate_class"], expect_source=s["expect_source"])
            if s["disposition"] != "unexecuted":
                self.w.counters[s["effect"]["status"]] += 1
                if s["effect"]["status"] == "verified" and s["kind"] in ("key", "text"):
                    if s.get("route") == "evdev" and self.evdev:
                        if (not self.evdev.dirty and self.evdev.health['state'] != 'down' and
                                s.get('evdev_map_epoch') == self.evdev.map_epoch):
                            self.evdev.health["keyboard"] = "tested"
                    else:
                        self.channel["keyboard"] = "ok"
            s.pop('evdev_map_epoch', None)
            s.pop('evdev_intent_epoch', None)
        r["pending_effects"] = copy.deepcopy(self.pending_effects)
        r["effect"] = copy.deepcopy((stopped or steps[-1])["effect"])
        state = self.lease_state.get(p["lease"])
        if state:
            state["misses"] = 0 if completed else state["misses"] + 1
            if state["misses"] >= 5 or state["acts"] is not None and state["used"] >= state["acts"]:
                state["suspended"] = True
                try:
                    self.durable("lease_suspend", lease_hash=r["lease_hash"], reason="F7:no_progress" if state["misses"] >= 5 else "F8:acts_budget")
                except ActError:
                    pass
        r["timing_ms"]["total"] = (self.clock() - a["received"]) * 1000
        for name in TIMINGS:
            if name not in ("queue", "total", "durable"):
                r["timing_ms"][name] = sum(s["timing_ms"][name] for s in steps)
        r["timing_ms"]["durable"] = self.durable_ms - a.get("durable_start", 0)
        durable_start = self.clock()
        a["outcome_known"] = True
        try:
            self.durable("receipt", action_id=p["action_id"], receipt=r)
        except ActError:
            self.w.respond(a["peer"], a["ident"], self.pending(a))
            return
        # Immutable stored receipt matches exactly the durable JSON record.
        self.last_durable_ms = (self.clock() - durable_start) * 1000
        a["final"] = True
        self.w.respond(a["peer"], a["ident"], r)

    @staticmethod
    def advice(reason, dispatch):
        if reason is None:
            return "none"
        if reason in ("preempted", "stale_generation", "lease_expired", "permit_expired"):
            return "reattach"
        if reason in ("input_unacknowledged", "channel_down") or dispatch == "unknown":
            return "do_not_retry"
        if reason in ("effect_unresolved", "modal_still_present"):
            return "stop"
        if dispatch == "accepted" or reason == "awaiting_reconciliation":
            return "observe_first"
        if reason in ("backend_unsupported", "confirm_required", "budget_exceeded", "worker_fault", "journal_unavailable", "provider_degraded"):
            return "stop"
        return "retry_ok"

    def reconcile(self, observed_rev=None):
        # A current holder's full settled sample acknowledges the oldest
        # unresolved action, even if its original postcondition remains false.
        # Keep independent positive proof as supplemental history (D5).
        group = list(self.pending_effects)
        oldest = None
        if observed_rev is not None:
            if not group:
                return
            oldest = (group[0].get("generation", self.w.generation), group[0]["action_id"])
            group = [p for p in group if (p.get("generation", self.w.generation), p["action_id"]) == oldest]
            if oldest[0] == self.w.generation and any(observed_rev < p["rev"] for p in group):
                return
        for pending in group:
            generation = pending.get("generation", self.w.generation)
            action = self.actions.get((generation, pending["action_id"]))
            if action:
                acks = [ack for sid, ack in action.get("acks", []) if sid == pending["step_id"]]
                for ack in acks:
                    ack.feed(self.t.raw)
                step = next(s for s in action["receipt"]["steps"] if s["id"] == pending["step_id"])
                if acks and all(ack.complete for ack in acks) and step["cleanup"]["confirmed"]:
                    pending["delivery_quiescent"] = True
            proven = False
            if (generation == self.w.generation and pending["delivery_quiescent"]
                    and self.w.cache.rev >= pending["rev"]):
                try:
                    proven = self.evaluate(pending["expect"]) is True
                except ActError:
                    # A restarted provider or stale selector cannot reconcile
                    # an old mutation. Keep it unresolved without replaying.
                    pass
            if proven:
                self.durable("reconciliation", action_id=pending["action_id"], step_id=pending["step_id"], rev=self.w.cache.rev, effect="verified")
                if action:
                    action.setdefault("reconciliations", []).append({"step_id": pending["step_id"], "rev": self.w.cache.rev, "effect": "verified"})
                self.pending_effects.remove(pending)
            elif observed_rev is None:
                # Observation has been followed, but cannot prove completion
                # or cancel possibly delayed input. Name that blocker honestly.
                pending["observed_unresolved"] = True
        if oldest is not None:
            self.durable("reconciled", action_id=oldest[1], action_generation=oldest[0], rev=observed_rev)
            self.pending_effects[:] = [p for p in self.pending_effects if p not in group]

    def start_selftest(self):
        if (self.w.cache.control or {}).get("state") != "agent":
            self.channel = {"state": "down", "keyboard": "untested", "pointer": "untested", "reason": "selftest_pending_control"}
            self.selftest_pending = True
            self.selftest = None
            return
        self.selftest_pending = False
        self.selftest = self.channels_selftest() if self.evdev else self.channel_selftest()

    def channels_selftest(self):
        started = self.clock()
        yield from self.evdev_selftest()
        self.route = "xtest"
        yield from self.channel_selftest()
        self.w.timings["selftest_ms"] = (self.clock() - started)*1000
        if self.channel["state"] != "ok" and self.evdev.health["state"] != "up" and not self.selftest_pending:
            raise RuntimeError("all_input_channels_down")

    def evdev_selftest(self):
        deadline = self.clock() + 5
        while not (yield from self.evdev_selftest_attempt()):
            if self.clock() >= deadline:
                self.evdev.down('session_settlement_timeout')
                return
            yield

    def evdev_selftest_attempt(self):
        keyboard = not any(v["mapped"] and v["type"] != "popup" for v in self.w.cache.windows.values())
        test = None
        old_focus = self.w.cache.focus
        a = {"params": {"action_id": "channel_selftest"}}
        r = {"id": "selftest", "attempts": [], "rev": {"dispatched": None}, "dispatch_status": "not_dispatched"}
        try:
            if not (yield from self.evdev.startup()):
                return True
            origin = yield from self.query("pointer", deadline=self.clock() + 0.2)
            test = self.t.test_window(keyboard)
            yield from self.query("barrier", deadline=self.clock() + 0.2)
            events = []
            r['evdev_intent_epoch'] = self.evdev.map_epoch
            if keyboard:
                shift = self.t.keycode("Shift_L")
                if shift is None:
                    raise ActError("channel_down")
                events = [(kind, shift) for _ in range(50) for kind in ("key_down", "key_up")]
            x, y = origin["x"], origin["y"]
            delta = 1 if x+1 < self.w.screen["w"] else -1
            events += [("motion", 0, x+delta if n%2 == 0 else x, y) for n in range(100)]
            self.durable("channel_test", route="evdev", events=len(events), keyboard=keyboard)
            yield from self.send_evdev(a, r, events, diagnostic=True)
            # Core events may follow the final raw event; allow a round trip.
            until = self.clock() + 0.15
            while keyboard and self.t.test_presses < 50 and self.clock() < until:
                yield
            pointer = yield from self.query("pointer", deadline=self.clock() + 0.2)
            yield from self.wait_evdev(a, diagnostic=True)
            if pointer["x"] != x or pointer["y"] != y or keyboard and self.t.test_presses != 50:
                raise ActError("selftest_delivery_failed")
            self.evdev.health.update(state="up", keyboard="tested" if keyboard and
                                     r.get('evdev_map_epoch') == self.evdev.map_epoch else "untested", last_error=None)
        except ActError as exc:
            if exc.reason == 'provider_degraded':
                # Only the private diagnostic surface may restart after a
                # changed table. Application actions are never replayed.
                return False
            self.evdev.down(exc.reason)
        finally:
            yield from self.release_evdev(a, r)
            self.t.end_test(test, old_focus)
        return True

    def channel_selftest(self):
        start = self.clock()
        keyboard = not any(v["mapped"] and v["type"] != "popup" for v in self.w.cache.windows.values())
        self.channel = {"state": "down", "keyboard": "untested", "pointer": "down"}
        try:
            self.w.poll_control(force=True)
            if self.fence_reason:
                raise ActError('worker_fault', self.fence_reason)
            if (self.w.cache.control or {}).get("state") != "agent":
                raise ActError("preempted")
            origin = yield from self.query("pointer", deadline=self.clock() + 0.2)
            old_focus = self.w.cache.focus
            test = self.t.test_window(keyboard)
            yield from self.query("barrier", deadline=self.clock() + 0.2)
            events = []
            if keyboard:
                shift = self.t.keycode("Shift_L")
                if shift is None:
                    raise ActError("channel_down")
                for _ in range(50):
                    events.extend((("key_down", shift), ("key_up", shift)))
            x, y = origin["x"], origin["y"]
            for n in range(100):
                events.append(("motion", 0, x + (1 if x + 1 < self.w.screen["w"] else -1) if n % 2 == 0 else x, y))
            ack = Ack([(k, d) for k, d, *xy in events], self.t.sources, self.t.raw_seq)
            # Startup self-test is a separate journaled diagnostic, never an
            # application action; no keyboard test if any client exists.
            self.durable("channel_test", events=len(events), keyboard=keyboard)
            for ev in events:
                self.w.poll_control(force=True)
                if (self.w.cache.control or {}).get("state") != "agent":
                    raise ActError("preempted")
                if ev[0] == "key_down":
                    self.holds["xtest"]["key"].add(ev[1])
                self.t.write(ev)
            self.t.flush()
            until = self.clock() + 0.2
            while self.clock() < until:
                ack.feed(self.t.raw)
                if ack.complete and (not keyboard or self.t.test_presses == 50):
                    break
                yield
            pointer = yield from self.query("pointer", deadline=self.clock() + 0.2)
            good_pointer = pointer["x"] == x and pointer["y"] == y
            good_keyboard = not keyboard or self.t.test_presses == 50
            self.channel = {"state": "ok" if ack.complete and good_pointer and good_keyboard else "down",
                            "keyboard": "ok" if keyboard and good_keyboard and ack.complete else "down" if keyboard else "untested",
                            "pointer": "ok" if ack.complete and good_pointer else "down"}
            if ack.complete:
                self.holds["xtest"]["key"].clear()
            self.t.end_test(test, old_focus)
        except ActError as exc:
            self.channel["state"] = "down"
            self.selftest_pending = exc.reason == "preempted"
            self.t.end_test(None, None)
        if self.holds["xtest"]["key"]:
            a = {"params": {"action_id": "channel_selftest"}}
            r = {"id": "selftest", "attempts": [], "rev": {"dispatched": None}, "dispatch_status": "not_dispatched"}
            yield from self.release(a, r)
        self.w.timings["selftest_ms"] = (self.clock() - start) * 1000


def recover_receipt(intent, armed):
    """Full v2 crash receipt, with explicitly unavailable post-crash state.

    No fresh worker/generation observation can establish an old action's
    delivery. This function only describes durable history; it never replays.
    """
    from types import SimpleNamespace
    p = copy.deepcopy(intent.get("request", {}))
    p.setdefault("session", intent.get("session"))
    p.setdefault("generation", intent.get("generation"))
    p.setdefault("action_id", intent.get("action_id"))
    p.setdefault("base_rev", intent.get("base_rev", 1))
    p.setdefault("control_epoch", intent.get("control_epoch"))
    p["lease"] = "redacted"
    p.setdefault("steps", [])
    for i, s in enumerate(p["steps"]):
        s.setdefault("id", str(i + 1))
        s.setdefault("require", {})
    e = Engine.__new__(Engine)
    e.w = SimpleNamespace(sid=p["session"], generation=p["generation"], record={"profile": intent.get("profile")},
                           profile_hash=intent.get("profile_hash"), development_flags=intent.get("development_flags", {}),
                           cache=SimpleNamespace(rev=None))
    e.build_id, e.boot_id = intent.get("build_id"), intent.get("boot_id")
    r = e.make_receipt(p, intent.get("request_hash"))
    r.update(status="unknown", phase="reconcile", dispatch_status="unknown", dispatched=False,
             effect=e.effect("unknown", None), reason="worker_fault", advice="do_not_retry",
             journal_seq=intent.get("journal_seq"), lease_hash=intent.get("lease_hash"),
             last_armed=armed[-1] if armed else None,
             state_delta={"rev": None, "state": None, "resync_required": True, "reason": "stale_generation"})
    for s in r["steps"]:
        if s["id"] in armed:
            s.update(disposition="executed", dispatch_status="unknown", reason="worker_fault", effect=e.effect("unknown", s["expect"]))
            r["pending_effects"].append({"action_id": p["action_id"], "step_id": s["id"], "delivery_quiescent": False})
    return r
