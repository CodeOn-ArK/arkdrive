#!/usr/bin/env python3
"""Full hardening regression suite (reference copy, DEFERRED for v1).

One test function per defect fixed across the adversarial hardening
passes. Offline: all desktop/LLM/vision boundaries are stubbed.
Re-pointed at the release executor (../src/cu_executor.py) and the
release lane (../src/cua-lane). The v1 core subset lives in
test_core.py; this full suite is kept, not deleted, and is re-based
progressively after v1. Known stale spots: functions that shell out
to the historical module name, and boundary fixtures written against
pre-receipt internals. Run everything: python3 trio2_reference.py
"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import types

sys.dont_write_bytecode = True
os.environ["CU_DRY_RUN"] = "1"
MODULE_PATH = Path(__file__).resolve().parent.parent / "src" / "cu_executor.py"
spec = importlib.util.spec_from_file_location("cu_executor", MODULE_PATH)
v8 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v8)


@contextlib.contextmanager
def environment(**values):
    previous = {key: os.environ.get(key) for key in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def focus_runner(record):
    """Fake RUNNER: a native editor window is focused."""
    def runner(argv, timeout_s, env_extra=None):
        record.append([str(a) for a in argv])
        if argv[0] == "xdotool" and argv[1] == "getactivewindow":
            return 0, "100\n", ""
        if argv[0] == "xdotool" and argv[1] == "getwindowname":
            return 0, "Editor\n", ""
        if argv[0] == "xprop":
            return 0, 'WM_CLASS(STRING) = "x", "gnome-text-editor"\n', ""
        return 0, "{}", ""
    return runner


def test_taskbar_strip_excluded():
    # Observer issue 6: a change confined to the taskbar strip must not
    # register as progress; a center change must.
    from PIL import Image
    def frame(bottom=None, center=None):
        image = Image.new("L", (960, 540), 128)
        px = image.load()
        if bottom is not None:
            for y in range(540 - 20, 540):
                for x in range(960):
                    px[x, y] = bottom
        if center is not None:
            for y in range(220, 320):
                for x in range(400, 560):
                    px[x, y] = center
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return buf.getvalue()
    base = frame()
    assert v8.region_hash(base) == v8.region_hash(frame(bottom=255)), \
        "taskbar-only change must not change region_hash"
    assert v8.region_hash(base) != v8.region_hash(frame(center=255)), \
        "center change must change region_hash"


def test_spa_sensor_refresh():
    # Observer issue 1: after a mouse_agent step on a browser surface, the
    # loop refreshes CDP text so a DOM change with no title/URL change
    # registers as changed (no false stall).
    progress = os.path.join(tempfile.mkdtemp(prefix="cu-trio2-"), "p.jsonl")
    calls = {"pagetext": 0}
    tools = ["observe", "mouse_agent", "done"]
    seen = []
    def fake_chat(task, steps, last_result, note):
        index = len(seen)
        seen.append(last_result)
        tool = tools[index]
        if tool == "mouse_agent":
            return json.dumps(dict(tool=tool, action="click",
                                   args={"text": "Accept"})), "stop", 0.0
        return json.dumps(dict(tool=tool, result="spa done")), "stop", 0.0
    def fake_tool(raw):
        tool = json.loads(raw)["tool"]
        if tool == "observe":
            return ("Window title: SPA\nWindow id: 100\n"
                    "Window class: chromium\nSurface: browser_cdp\n"
                    "URL: https://spa.test/")
        return '{"ok": true, "via": "cdp", "verified": true}'
    def fake_cdp_text():
        calls["pagetext"] += 1
        return "SPA state B - dialog accepted"
    originals = (v8.call_qwen, v8.run_tool, v8._focus_verify,
                 v8._cdp_text, v8._latest_shot, v8._automation_tab)
    try:
        v8.call_qwen, v8.run_tool = fake_chat, fake_tool
        v8._focus_verify = lambda fresh=False: dict(title="SPA", window_id="100",
                                        window_class="chromium",
                                        surface="browser_cdp")
        v8._cdp_text = fake_cdp_text
        v8._latest_shot = lambda: None
        # Window 100 is the automation window (pass-39 sensor gate).
        v8._automation_tab = lambda: {"window_id": 100, "url": "https://spa.test/"}
        with environment(CU_PROGRESS_LOG=progress, CUA_BIN=None):
            with contextlib.redirect_stdout(io.StringIO()):
                result = v8.run_loop("spa sensor", max_iters=4)
    finally:
        (v8.call_qwen, v8.run_tool, v8._focus_verify,
         v8._cdp_text, v8._latest_shot, v8._automation_tab) = originals
    assert "spa done" in result, "run must complete: " + result
    assert calls["pagetext"] == 1, "exactly one sensor refresh, after the agent step"
    events = [json.loads(line) for line in Path(progress).read_text().splitlines()]
    agent_events = [e for e in events if e["tool"] == "mouse_agent"]
    assert agent_events and agent_events[0]["changed"] is True, \
        "SPA DOM change must register changed=True in progress JSONL"


def test_vision_coordinate_scale():
    # Observer issue 3: vision returns normalized 0-1000; the rung converts
    # to native pixels. Marker ground truth: native (1400, 800) on a
    # 1920x1080 screen == normalized (729, 741).
    captured = {}
    def fake_vision(image, prompt):
        captured["prompt"] = prompt
        captured["image"] = image
        return '{"x": 729, "y": 741}'
    def fake_xdotool(args, surface):
        captured["args"] = dict(args)
        return True, "xdotool", "clicked"
    originals = (v8.vision_text, v8.rung_xdotool, v8._native_grab,
                 v8._screen_size, v8._latest_shot)
    try:
        v8.vision_text = fake_vision
        v8.rung_xdotool = fake_xdotool
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (1920, 1080), "white").save(buf, format="PNG")
        native_png = buf.getvalue()
        v8._native_grab = lambda: native_png
        v8._screen_size = lambda: (1920, 1080)
        v8._latest_shot = lambda: None
        ok, via, detail = v8.rung_vision({"text": "red circle"}, "native")
    finally:
        (v8.vision_text, v8.rung_xdotool, v8._native_grab,
         v8._screen_size, v8._latest_shot) = originals
    # The image handed to the vision server must be size-bounded (its
    # 2048-token context rejects native-res frames with HTTP 400).
    from PIL import Image
    sent = Image.open(io.BytesIO(captured["image"]))
    assert sent.width <= 960, "vision image must be downscaled, got %d" % sent.width
    assert ok and via == "vision", "vision rung must succeed via xdotool"
    assert (captured["args"]["x"], captured["args"]["y"]) == (1400, 800), \
        "normalized (729,741) must convert to native (1400,800), got %s" % (
            (captured["args"].get("x"), captured["args"].get("y")),)
    assert "normalized" in captured["prompt"], \
        "vision prompt must request normalized 0-1000 coordinates"


def test_midtype_timeout_aborts():
    # Observer issue 7: an xdotool type timeout mid-text aborts the action;
    # no later engine receives the full text (no duplicated prefix).
    record = []
    def runner(argv, timeout_s, env_extra=None):
        record.append([str(a) for a in argv])
        if argv[0] == "xdotool" and argv[1] == "getactivewindow":
            return 0, "100\n", ""
        if argv[0] == "xdotool" and argv[1] == "getwindowname":
            return 0, "Editor\n", ""
        if argv[0] == "xprop":
            return 0, 'WM_CLASS(STRING) = "x", "gnome-text-editor"\n', ""
        if argv[0] == "xdotool" and argv[1] == "type":
            chunks = [a for a in record if a[0] == "xdotool" and a[1] == "type"]
            if len(chunks) >= 2:
                raise subprocess.TimeoutExpired(cmd="xdotool type", timeout=1)
            return 0, "", ""
        return 0, "{}", ""
    text = ("abcdefghij" * 25)  # 250 chars -> chunks of 100/100/50
    original = v8.RUNNER
    try:
        v8.RUNNER = runner
        with environment(CU_DRY_RUN=None, YDOTOOL_SOCKET="/nonexistent.sock"):
            result = v8.kbd_agent("type_verify",
                                  {"text": text, "engine": "xdotool"})
    finally:
        v8.RUNNER = original
    assert "ERROR: TIMEOUT kbd_agent" in result, \
        "mid-type timeout must abort the action: %s" % result[:200]
    type_calls = [a for a in record if a[0] == "xdotool" and a[1] == "type"]
    assert len(type_calls) == 2, "exactly two chunk dispatches, got %d" % len(type_calls)
    assert type_calls[0][-1] == text[:100] and type_calls[1][-1] == text[100:200]
    cua_types = [a for a in record if a[0] not in ("xdotool", "xprop")
                 and "type" in a[1:2]]
    assert not cua_types, "no fallback engine may receive the text after abort"
    full = [a for a in record if text in a]
    assert not full, "the full text must never be re-dispatched after a partial type"


class FakeNode:
    def __init__(self, name, role, children=()):
        self.name = name
        self._role = role
        self._children = list(children)
    def getRoleName(self):
        return self._role
    def __iter__(self):
        return iter(self._children)


def test_atspi_focused_app_gating():
    # Observer issue 5 + Codex finding 10: the AT-SPI walk is gated to the
    # focused window's FRAME (not its whole app, not every desktop app);
    # no match -> the rung skips instead of clicking an occluded widget.
    frame_editor = FakeNode("Editor", "frame")
    app_other = FakeNode("Other", "application",
                         [FakeNode("Other App", "frame")])
    app_editor = FakeNode("EditorApp", "application", [frame_editor])
    desktop = [app_other, app_editor]
    assert v8._atspi_focused_frame(desktop, "Editor") is frame_editor
    assert v8._atspi_focused_frame(desktop, "Missing Window") is None
    assert v8._atspi_focused_frame(desktop, "") is None
    # Codex finding 2: ActionTimeout raised inside the walk must propagate,
    # never be swallowed by the per-node exception handler.
    class AlarmNode:
        name = "X"
        def getRoleName(self):
            raise v8.ActionTimeout("deadline")
        def __iter__(self):
            return iter(())
    app_alarm = FakeNode("App", "application", [AlarmNode()])
    try:
        v8._atspi_focused_frame([app_alarm], "Editor")
        raise AssertionError("ActionTimeout must propagate out of the walk")
    except v8.ActionTimeout:
        pass


def test_rung_effect_verification():
    # Observer issue 2, semantics revised by Codex trio-2 pass-22: a
    # clean dispatch with no pixel change is reported honestly as
    # verified="no_visual_change" and STOPS the ladder — re-dispatching
    # a delivered click through another engine double-fires it. The
    # ladder advances only when a rung never dispatched. With a real
    # pixel change, the first rung wins and verified is True.
    record = []
    originals = (v8.RUNNER, v8._grab, v8.rung_xdotool, v8.rung_cua,
                 v8.rung_vision)
    state = {"grab_calls": 0, "change_on_call": None}
    def grab():
        state["grab_calls"] += 1
        if state["change_on_call"] is not None and \
                state["grab_calls"] >= state["change_on_call"]:
            return b"frame-changed" * 32
        return b"frame-same" * 32
    cua_calls = []
    try:
        v8.RUNNER = focus_runner(record)
        v8._grab = grab
        v8.rung_xdotool = lambda a, s: (True, "xdotool", "clicked")
        v8.rung_cua = lambda a, s: cua_calls.append(1) or (True, "cua", "clicked")
        v8.rung_vision = lambda a, s: (False, "vision", "no target")
        with environment(CU_DRY_RUN=None):
            result = json.loads(v8.mouse_agent("click", {"x": 10, "y": 10}))
        assert result["ok"] is True and result["via"] == "xdotool", result
        assert result["verified"] == "no_visual_change", result
        assert not cua_calls, "a delivered click must not be re-dispatched"
        # Grab sequence per rung is before/after; keyboard consumes call 1
        # (before only), xdotool before=2, after=3. Change lands on call 3.
        state["grab_calls"] = 0
        state["change_on_call"] = 3
        with environment(CU_DRY_RUN=None):
            result = json.loads(v8.mouse_agent("click", {"x": 10, "y": 10}))
        assert result["ok"] is True and result["via"] == "xdotool", result
        assert result["verified"] is True, result
    finally:
        (v8.RUNNER, v8._grab, v8.rung_xdotool, v8.rung_cua,
         v8.rung_vision) = originals


def test_trims_and_defaults():
    # Observer issues 4, 9, 13, 14 + redundancy trims.
    assert "atspi" not in v8.LADDERS["vnc"], "dead atspi rung must leave vnc ladder"
    assert "atspi" not in v8.LADDERS["unknown"], "dead atspi rung must leave unknown ladder"
    assert "win_agent" in v8.TOOL_TIMEOUTS_S, "win_agent needs an explicit timeout"
    assert "Batch independent cua actions" not in v8.SYSTEM_PROMPT, \
        "stale v7 batching line must be gone from the prompt"
    with environment(CUA_BIN=None, CUA_CDP_PORT=None):
        assert v8.classify_surface("chromium", "Gmail") == "browser_cdp", \
            "default config must enable the CDP rung"
    with environment(CUA_BIN="/example/cua", CUA_CDP_PORT=None):
        assert v8.classify_surface("chromium", "Gmail") == "browser"
    try:
        import pyatspi  # noqa: F401
        binding = True
    except Exception:
        binding = False
    # Since pass-22 ATSPI_AVAILABLE means binding AND a reachable bus
    # (probed in a subprocess — a bad bus aborts pyatspi at the C level).
    if not binding:
        assert v8.ATSPI_AVAILABLE is False, \
            "ATSPI_AVAILABLE must be False without the pyatspi binding"
    else:
        assert v8.ATSPI_AVAILABLE == v8._probe_atspi(), \
            "ATSPI_AVAILABLE must reflect binding + bus reachability"


def test_focus_memo_dedupes_per_step():
    # Trim: within a step, repeated _focus_verify calls hit the desktop
    # once; any input dispatch invalidates the memo.
    record = []
    original = v8.RUNNER
    token = v8._step_focus.set({})
    try:
        v8.RUNNER = focus_runner(record)
        with environment(CU_DRY_RUN=None):
            first = v8._focus_verify()
            second = v8._focus_verify()
            assert first == second
            probes = [a for a in record if a[1] == "getactivewindow"]
            assert len(probes) == 1, "memo must dedupe focus probes per step"
            v8._call(["xdotool", "key", "Tab"])
            v8._focus_verify()
            probes = [a for a in record if a[1] == "getactivewindow"]
            assert len(probes) == 2, "input dispatch must invalidate the memo"
    finally:
        v8.RUNNER = original
        v8._step_focus.reset(token)


def test_pagetext_cdp_fallback_and_port():
    # Trio-2 bench finding: with cua-lane as the default binary, a browser
    # that was NOT lane-launched has no CDP port; pagetext must fall back
    # to the clipboard path, and rung_cdp must skip (not raise). Also the
    # CDP port derivation must mirror cua-lane (9210 + display number).
    with environment(CUA_CDP_PORT=None, CUA_DISPLAY=":0"):
        assert v8._cdp_port() == 9210
    with environment(CUA_CDP_PORT=None, CUA_DISPLAY=":3"):
        assert v8._cdp_port() == 9213
    with environment(CUA_CDP_PORT="9555", CUA_DISPLAY=":3"):
        assert v8._cdp_port() == 9555
    calls = []
    def fake_cua(*args):
        calls.append(args)
        if args[0] == "cdp-text":
            raise RuntimeError("CDP port 9210 unreachable")
        if args[:2] == ("clip", "get"):
            return {"text": "clipboard page text"}
        return {"ok": True}
    originals = (v8._cua, v8._focus_verify, v8._automation_tab)
    try:
        v8._cua = fake_cua
        v8._focus_verify = lambda fresh=False: dict(title="Page", window_id="100",
                                        window_class="chromium",
                                        surface="browser_cdp")
        # Window 100 IS the automation window in this scenario
        # (pass-38 window-id binding for the CDP channel).
        v8._automation_tab = lambda: {"window_id": 100,
                                      "url": "https://page.test/"}
        with environment(CUA_BIN=None):
            text = v8._pagetext_once()
    finally:
        v8._cua, v8._focus_verify, v8._automation_tab = originals
    assert text == "clipboard page text", \
        "pagetext must fall back to clipboard when CDP is down: %r" % text
    assert ("cdp-text",) in calls and ("key", "ctrl+a") in calls
    # rung_cdp: unreachable port -> clean skip tuple, no exception.
    import types
    sys.modules.setdefault("websocket", types.ModuleType("websocket"))
    original_conn = v8.socket.create_connection
    def dead_conn(*a, **k):
        raise OSError("connection refused")
    try:
        v8.socket.create_connection = dead_conn
        with environment(CUA_BIN=None, CUA_CDP_PORT="9210"):
            ok, via, detail = v8.rung_cdp({"text": "Accept"}, "browser_cdp")
    finally:
        v8.socket.create_connection = original_conn
    assert ok is False and via == "cdp" and "cdp_unreachable" in detail, \
        (ok, via, detail)


def test_codex_partial_type_failure_aborts():
    # Codex finding 4: a NON-timeout failure after a delivered chunk must
    # also abort (no full-text cua fallback -> no duplicated prefix).
    record = []
    def runner(argv, timeout_s, env_extra=None):
        record.append([str(a) for a in argv])
        if argv[0] == "xdotool" and argv[1] == "getactivewindow":
            return 0, "100\n", ""
        if argv[0] == "xdotool" and argv[1] == "getwindowname":
            return 0, "Editor\n", ""
        if argv[0] == "xprop":
            return 0, 'WM_CLASS(STRING) = "x", "gnome-text-editor"\n', ""
        if argv[0] == "xdotool" and argv[1] == "type":
            done = [a for a in record if a[0] == "xdotool" and a[1] == "type"]
            if len(done) >= 2:
                return 1, "", "device busy"
            return 0, "", ""
        return 0, "{}", ""
    text = "abcdefghij" * 25
    original = v8.RUNNER
    try:
        v8.RUNNER = runner
        with environment(CU_DRY_RUN=None, YDOTOOL_SOCKET="/nonexistent.sock"):
            result = v8.kbd_agent("type_verify",
                                  {"text": text, "engine": "xdotool"})
    finally:
        v8.RUNNER = original
    assert "mid-dispatch" in result and "aborting" in result, result[:200]
    types = [a for a in record if a[0] == "xdotool" and a[1] == "type"]
    assert len(types) == 2, types
    assert not [a for a in record if text in a], "full text must not be re-dispatched"


def test_codex_memo_invalidation_paths():
    # Codex finding 5: sh-wrapped key dispatch and FAILED input dispatches
    # must both invalidate the step focus memo.
    record = []
    original = v8.RUNNER
    token = v8._step_focus.set({})
    try:
        v8.RUNNER = focus_runner(record)
        with environment(CU_DRY_RUN=None):
            v8._focus_verify()
            v8._call(["sh", "-c", "xdotool key Alt+Tab"])
            v8._focus_verify()
            probes = [a for a in record if a[1] == "getactivewindow"]
            assert len(probes) == 2, "sh-wrapped input must invalidate the memo"
            def failing(argv, timeout_s, env_extra=None):
                if argv[0] == "xdotool" and argv[1] == "key":
                    return 1, "", "boom"
                return focus_runner(record)(argv, timeout_s, env_extra)
            v8.RUNNER = failing
            try:
                v8._call(["xdotool", "key", "Tab"])
                raise AssertionError("failing dispatch must raise")
            except RuntimeError:
                pass
            v8._focus_verify()
            probes = [a for a in record if a[1] == "getactivewindow"]
            assert len(probes) == 3, "failed input dispatch must invalidate the memo"
    finally:
        v8.RUNNER = original
        v8._step_focus.reset(token)


def test_codex_hash_markers_and_truncation():
    # Codex findings 12 + 13: settle markers are not progress; changes
    # beyond 2000 chars are not hidden by truncation.
    base = "page text"
    assert v8.compute_state_hash("t", "u", base) == \
        v8.compute_state_hash("t", "u", base + " [settled:2]")
    assert v8.compute_state_hash("t", "u", base + " [settled:3]") == \
        v8.compute_state_hash("t", "u", base)
    prefix = "x" * 3000
    assert v8.compute_state_hash("t", "u", prefix + " A") != \
        v8.compute_state_hash("t", "u", prefix + " B")


def test_codex_cdp_text_empty_page():
    # Codex finding 14: cdp-text answering "" is a successful empty read:
    # no clipboard fallback, and the value is "" (not None).
    calls = []
    def fake_cua(*args):
        calls.append(args)
        if args[0] == "cdp-text":
            return {"text": ""}
        return {"ok": True, "text": "stale clipboard"}
    originals = (v8._cua, v8._focus_verify, v8._automation_tab)
    try:
        v8._cua = fake_cua
        v8._focus_verify = lambda fresh=False: dict(title="P", window_id="100",
                                        window_class="chromium",
                                        surface="browser_cdp")
        v8._automation_tab = lambda: {"window_id": 100, "url": ""}
        with environment(CUA_BIN=None):
            assert v8._cdp_text() == ""
            assert v8._pagetext_once() == ""
    finally:
        v8._cua, v8._focus_verify, v8._automation_tab = originals
    assert ("key", "ctrl+a") not in calls, "empty CDP page must not trigger clipboard fallback"


def test_codex_short_image_taskbar_and_settle():
    # Codex finding 18: on short images the center crop must not
    # reintroduce the taskbar strip.
    from PIL import Image
    def frame(bottom):
        image = Image.new("L", (160, 100), 128)
        px = image.load()
        for y in range(96, 100):
            for x in range(160):
                px[x, y] = bottom
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return buf.getvalue()
    assert v8.region_hash(frame(128)) == v8.region_hash(frame(255))
    # Codex finding 6: a click whose effect lands during the settle window
    # verifies on the SAME rung (exactly one dispatch), not via re-click.
    record = []
    grabs = {"n": 0}
    def grab():
        grabs["n"] += 1
        return b"frame-b" * 32 if grabs["n"] >= 3 else b"frame-a" * 32
    dispatches = {"n": 0}
    def rung_x(args, surface):
        dispatches["n"] += 1
        return True, "xdotool", "clicked"
    originals = (v8.RUNNER, v8._grab, v8.rung_xdotool)
    try:
        v8.RUNNER = focus_runner(record)
        v8._grab = grab
        v8.rung_xdotool = rung_x
        with environment(CU_DRY_RUN=None):
            result = json.loads(v8.mouse_agent("click", {"x": 5, "y": 5}))
    finally:
        v8.RUNNER, v8._grab, v8.rung_xdotool = originals
    assert result["ok"] is True and result["verified"] is True, result
    assert dispatches["n"] == 1, "settle re-check must avoid a second dispatch"


def test_codex_clipboard_sentinel():
    # Codex finding 7: if copy is a no-op, readback must NOT verify against
    # the clipboard the paste itself seeded (sentinel clears it first).
    clip = {"value": "unset"}
    typed = {"text": ""}
    def fake_cua(*args):
        if args[:2] == ("clip", "set"):
            clip["value"] = args[2]
            return {"ok": True}
        if args[:2] == ("clip", "get"):
            return {"text": clip["value"]}
        return {"ok": True}
    record = []
    def runner(argv, timeout_s, env_extra=None):
        record.append([str(a) for a in argv])
        if argv[0] == "xdotool" and argv[1] == "getactivewindow":
            return 0, "100\n", ""
        if argv[0] == "xdotool" and argv[1] == "getwindowname":
            return 0, "Editor\n", ""
        if argv[0] == "xprop":
            return 0, 'WM_CLASS(STRING) = "x", "gnome-text-editor"\n', ""
        if argv[0] == "xdotool" and argv[1] == "type":
            typed["text"] += argv[-1]
            return 0, "", ""
        return 0, "{}", ""
    originals = (v8._cua, v8.RUNNER, v8._focus_verify)
    try:
        v8._cua = fake_cua
        v8.RUNNER = runner
        v8._focus_verify = lambda fresh=False: dict(title="Editor", window_id="100",
                                        window_class="gnome-text-editor",
                                        surface="native")
        with environment(CU_DRY_RUN=None, YDOTOOL_SOCKET="/nonexistent.sock"):
            result = json.loads(v8.kbd_agent(
                "type_verify", {"text": "SENTINEL1", "engine": "xdotool",
                                "safe_clipboard": True}))
    finally:
        v8._cua, v8.RUNNER, v8._focus_verify = originals
    assert typed["text"] == "SENTINEL1", typed
    assert result["ok"] is False and result["readback_matches"] is False, \
        "no-op copy must not verify: %s" % result


def test_pass2_fixes():
    # (a) CDP port mirrors the runner env, not inherited DISPLAY.
    with environment(CUA_CDP_PORT=None, CUA_DISPLAY=None, DISPLAY=":3"):
        assert v8._cdp_port() == 9210, v8._cdp_port()
    with environment(CUA_CDP_PORT=None, CUA_DISPLAY=":2", DISPLAY=":3"):
        assert v8._cdp_port() == 9212, v8._cdp_port()
    # (b) Firefox (plain browser surface) never reads via Chromium CDP.
    calls = []
    def fake_cua(*args):
        calls.append(args)
        if args[0] == "cdp-text":
            return {"text": "CHROMIUM PAGE"}
        if args[:2] == ("clip", "get"):
            return {"text": "FIREFOX PAGE"}
        return {"ok": True}
    originals = (v8._cua, v8._focus_verify)
    try:
        v8._cua = fake_cua
        v8._focus_verify = lambda fresh=False: dict(title="FF", window_id="100",
                                        window_class="firefox",
                                        surface="browser")
        with environment(CUA_BIN=None):
            assert v8._pagetext_once() == "FIREFOX PAGE"
    finally:
        v8._cua, v8._focus_verify = originals
    assert ("cdp-text",) not in calls, "Firefox surface must not touch CDP text"
    # (c) AT-SPI title collision: the STATE_ACTIVE frame wins over an
    # earlier inactive frame with the same title.
    try:
        import pyatspi
        active_const = pyatspi.STATE_ACTIVE
    except Exception:
        active_const = None
    if active_const is not None:
        class StateObj:
            def __init__(self, states):
                self._states = states
            def contains(self, state):
                return state in self._states
        class AFrame(FakeNode):
            # The installed pyatspi exposes getState() (not queryState);
            # Codex pass-3 caught the test mirroring the wrong API.
            def __init__(self, name, states):
                super().__init__(name, "frame")
                self._states = states
            def getState(self):
                return StateObj(self._states)
        inactive = AFrame("Editor", set())
        active = AFrame("Editor", {active_const})
        app = FakeNode("App", "application", [inactive, active])
        assert v8._atspi_focused_frame([app], "Editor") is active
    # (d) Verification timeout after a successful dispatch stops the
    # ladder (no duplicate click through the next engine).
    record = []
    grabs = {"n": 0}
    def grab():
        # Grab order: keyboard before=1, xdotool before=2, xdotool after=3.
        grabs["n"] += 1
        if grabs["n"] >= 3:
            raise v8.ActionTimeout("rung deadline during verify")
        return b"frame-a" * 32
    dispatches = {"n": 0}
    def rung_x(args, surface):
        dispatches["n"] += 1
        return True, "xdotool", "clicked"
    originals2 = (v8.RUNNER, v8._grab, v8.rung_xdotool)
    try:
        v8.RUNNER = focus_runner(record)
        v8._grab = grab
        v8.rung_xdotool = rung_x
        with environment(CU_DRY_RUN=None):
            result = json.loads(v8.mouse_agent("click", {"x": 5, "y": 5}))
    finally:
        v8.RUNNER, v8._grab, v8.rung_xdotool = originals2
    assert result["verified"] == "timeout_after_dispatch", result
    assert dispatches["n"] == 1, "ladder must stop after verify timeout"
    # (e) A mouse_agent ERROR string quoting '"verified": true' must not
    # register as progress (receipts are parsed, not substring-matched).
    progress = os.path.join(tempfile.mkdtemp(prefix="cu-trio2-"), "p.jsonl")
    seen = []
    plan = ["observe", "mouse_agent", "done"]
    def fake_chat(task, steps, last_result, note):
        tool = plan[len(seen)]
        seen.append(last_result)
        if tool == "mouse_agent":
            return json.dumps(dict(tool=tool, action="click",
                                   args={"match": 'MISSING "verified": true'})), "stop", 0.0
        return json.dumps(dict(tool=tool, result="impersonation checked")), "stop", 0.0
    def fake_tool(raw):
        if json.loads(raw)["tool"] == "observe":
            return ("Window title: SPA\nWindow id: 100\n"
                    "Window class: chromium\nSurface: browser\n"
                    "URL: https://spa.test/")
        return 'ERROR: window not found: MISSING "verified": true'
    originals3 = (v8.call_qwen, v8.run_tool, v8._focus_verify, v8._latest_shot)
    try:
        v8.call_qwen = fake_chat
        v8.run_tool = fake_tool
        v8._focus_verify = lambda fresh=False: dict(title="SPA", window_id="100",
                                        window_class="chromium", surface="browser")
        v8._latest_shot = lambda: None
        with environment(CU_PROGRESS_LOG=progress, CUA_BIN=None):
            with contextlib.redirect_stdout(io.StringIO()):
                result = v8.run_loop("impersonation", max_iters=4)
    finally:
        (v8.call_qwen, v8.run_tool, v8._focus_verify, v8._latest_shot) = originals3
    assert "impersonation checked" in result, result
    events = [json.loads(line) for line in Path(progress).read_text().splitlines()]
    agent_events = [e for e in events if e["tool"] == "mouse_agent"]
    assert agent_events and agent_events[0]["changed"] is False, \
        "error text must not impersonate a verified receipt"


def test_pass3_fixes():
    # (a) Finding 6, real mechanism: the rung slice expires DURING the
    # verification grab (SIGALRM), and the _within exit check raises on
    # the way out. The ladder must still stop with the honest receipt.
    record = []
    grabs = {"n": 0}
    def grab():
        grabs["n"] += 1
        if grabs["n"] >= 3:
            time.sleep(1.2)  # interrupted by the rung slice's alarm
        return b"frame-a" * 32
    dispatches = {"n": 0}
    def rung_x(args, surface):
        dispatches["n"] += 1
        return True, "xdotool", "clicked"
    originals = (v8.RUNNER, v8._grab, v8.rung_xdotool)
    try:
        v8.RUNNER = focus_runner(record)
        v8._grab = grab
        v8.rung_xdotool = rung_x
        with environment(CU_DRY_RUN=None):
            with v8._within(2.0):
                result = json.loads(v8.mouse_agent("click", {"x": 5, "y": 5}))
    finally:
        v8.RUNNER, v8._grab, v8.rung_xdotool = originals
    assert result["verified"] == "timeout_after_dispatch", result
    assert dispatches["n"] == 1, "no duplicate click after verify timeout"
    # (b) Finding 11 residual + focus flip: if the agent step moved focus
    # from lane Chromium to Firefox, the sensor refresh must not fetch
    # Chromium CDP text for the new surface.
    progress = os.path.join(tempfile.mkdtemp(prefix="cu-trio2-"), "p.jsonl")
    focus_calls = {"n": 0}
    def flip_focus():
        focus_calls["n"] += 1
        if focus_calls["n"] <= 2:
            return dict(title="SPA", window_id="100", window_class="chromium",
                        surface="browser_cdp")
        return dict(title="FF", window_id="200", window_class="firefox",
                    surface="browser")
    cdp_calls = {"n": 0}
    def fake_cdp():
        cdp_calls["n"] += 1
        return "CHROMIUM TEXT"
    plan = ["observe", "mouse_agent", "done"]
    seen = []
    def fake_chat(task, steps, last_result, note):
        tool = plan[len(seen)]
        seen.append(last_result)
        if tool == "mouse_agent":
            return json.dumps(dict(tool=tool, action="click", args={"x": 1, "y": 2})), "stop", 0.0
        return json.dumps(dict(tool=tool, result="flip checked")), "stop", 0.0
    def fake_tool(raw):
        if json.loads(raw)["tool"] == "observe":
            return ("Window title: SPA\nWindow id: 100\n"
                    "Window class: chromium\nSurface: browser_cdp\n"
                    "URL: https://spa.test/")
        return '{"ok": true, "via": "xdotool", "verified": true}'
    originals2 = (v8.call_qwen, v8.run_tool, v8._focus_verify,
                  v8._cdp_text, v8._latest_shot, v8._tab_url)
    try:
        v8.call_qwen, v8.run_tool = fake_chat, fake_tool
        v8._focus_verify = flip_focus
        v8._cdp_text = fake_cdp
        v8._latest_shot = lambda: None
        v8._tab_url = lambda: ""
        with environment(CU_PROGRESS_LOG=progress, CUA_BIN=None):
            with contextlib.redirect_stdout(io.StringIO()):
                result = v8.run_loop("focus flip", max_iters=4)
    finally:
        (v8.call_qwen, v8.run_tool, v8._focus_verify,
         v8._cdp_text, v8._latest_shot, v8._tab_url) = originals2
    assert "flip checked" in result, result
    assert cdp_calls["n"] == 0, "CDP refresh must not run after focus left Chromium"
    # (c) Finding 8 residual: Ctrl+A dispatched via the sh-wrapped keys
    # action must register as a selection in the dry model, so a later
    # paste replaces the field instead of appending.
    token = v8._dry_state.set({"calls": 0, "slept": True, "text": "",
                               "field": "", "clip": "", "selected": False})
    def dry_agent(action, **args):
        return v8.run_tool(json.dumps(
            dict(tool="kbd_agent", action=action, args=args)))
    try:
        with environment(CU_DRY_RUN="1", YDOTOOL_SOCKET="/nonexistent.sock"):
            r1 = json.loads(dry_agent("type_verify", text="OLD",
                                      engine="xdotool"))
            assert r1["ok"] is True, r1
            dry_agent("keys", keys=["ctrl+a"])
            r2 = json.loads(dry_agent("type_verify", text="NEW",
                                      safe_clipboard=True))
            assert r2["ok"] is True and r2["readback_matches"] is True, r2
    finally:
        v8._dry_state.reset(token)


def test_pass4_fixes():
    # (a) Finding 11, cache layer: on browser_cdp the pagetext cache must
    # never serve text when the live page changed under identical
    # title/URL metadata (in-page navigation + metadata lag).
    progress = os.path.join(tempfile.mkdtemp(prefix="cu-trio2-"), "p.jsonl")
    live = {"text": "BODY A"}
    plan = ["observe", "pagetext", "pagetext", "done"]
    seen = []
    def fake_chat(task, steps, last_result, note):
        tool = plan[len(seen)]
        seen.append(last_result)
        return json.dumps(dict(tool=tool, result="cache checked")), "stop", 0.0
    def fake_tool(raw):
        if json.loads(raw)["tool"] == "observe":
            return ("Window title: SPA\nWindow id: 100\n"
                    "Window class: chromium\nSurface: browser_cdp\n"
                    "URL: https://spa.test/")
        return live["text"]
    originals = (v8.call_qwen, v8.run_tool, v8._focus_verify,
                 v8._latest_shot, v8._tab_url)
    try:
        v8.call_qwen, v8.run_tool = fake_chat, fake_tool
        v8._focus_verify = lambda fresh=False: dict(title="SPA", window_id="100",
                                        window_class="chromium",
                                        surface="browser_cdp")
        v8._latest_shot = lambda: None
        v8._tab_url = lambda: ""  # metadata lags: URL never updates
        with environment(CU_PROGRESS_LOG=progress, CUA_BIN=None):
            orig_tool = v8.run_tool
            def flipping_tool(raw):
                out = orig_tool(raw)
                if json.loads(raw)["tool"] == "pagetext":
                    live["text"] = "BODY B"  # in-page nav after first read
                return out
            v8.run_tool = flipping_tool
            with contextlib.redirect_stdout(io.StringIO()):
                result = v8.run_loop("pagetext cache liveness", max_iters=5)
    finally:
        (v8.call_qwen, v8.run_tool, v8._focus_verify,
         v8._latest_shot, v8._tab_url) = originals
    assert "cache checked" in result, result
    pagetext_results = [s for s in seen if s and "BODY" in s]
    assert pagetext_results[-1].startswith("BODY B"), \
        "second pagetext must be a live read, got: " + str(pagetext_results)
    # (b) Firefox isolation edge: the post-step focus re-read FAILS
    # transiently -> stale surface must not trigger the CDP refresh.
    focus_calls = {"n": 0}
    def flaky_focus():
        focus_calls["n"] += 1
        if focus_calls["n"] <= 2:
            return dict(title="SPA", window_id="100", window_class="chromium",
                        surface="browser_cdp")
        raise RuntimeError("getactivewindow transient failure")
    cdp_calls = {"n": 0}
    def fake_cdp():
        cdp_calls["n"] += 1
        return "CHROMIUM TEXT"
    plan2 = ["observe", "mouse_agent", "done"]
    seen2 = []
    def fake_chat2(task, steps, last_result, note):
        tool = plan2[len(seen2)]
        seen2.append(last_result)
        if tool == "mouse_agent":
            return json.dumps(dict(tool=tool, action="click", args={"x": 1, "y": 2})), "stop", 0.0
        return json.dumps(dict(tool=tool, result="flaky checked")), "stop", 0.0
    def fake_tool2(raw):
        if json.loads(raw)["tool"] == "observe":
            return ("Window title: SPA\nWindow id: 100\n"
                    "Window class: chromium\nSurface: browser_cdp\n"
                    "URL: https://spa.test/")
        return '{"ok": true, "via": "xdotool", "verified": true}'
    originals2 = (v8.call_qwen, v8.run_tool, v8._focus_verify,
                  v8._cdp_text, v8._latest_shot, v8._tab_url)
    try:
        v8.call_qwen, v8.run_tool = fake_chat2, fake_tool2
        v8._focus_verify = flaky_focus
        v8._cdp_text = fake_cdp
        v8._latest_shot = lambda: None
        v8._tab_url = lambda: ""
        with environment(CU_PROGRESS_LOG=progress, CUA_BIN=None):
            with contextlib.redirect_stdout(io.StringIO()):
                result = v8.run_loop("flaky focus", max_iters=4)
    finally:
        (v8.call_qwen, v8.run_tool, v8._focus_verify,
         v8._cdp_text, v8._latest_shot, v8._tab_url) = originals2
    assert "flaky checked" in result, result
    assert cdp_calls["n"] == 0, "refresh must not trust a stale surface"
    # (c) Verified click + slice expires during the success log write:
    # the finished receipt must be returned, never a second click.
    record = []
    frames = {"n": 0}
    def grab2():
        frames["n"] += 1
        return (b"frame-a" if frames["n"] % 2 else b"frame-b") * 32
    dispatches = {"n": 0}
    def rung_x(args, surface):
        dispatches["n"] += 1
        return True, "xdotool", "clicked"
    log_calls = {"n": 0}
    def slow_log(*a, **k):
        log_calls["n"] += 1
        if log_calls["n"] >= 2:
            # Only the post-success log write is slow (call 1 is the
            # focus event); SIGALRM interrupts inside it at slice end.
            time.sleep(1.6)
    originals3 = (v8.RUNNER, v8._grab, v8.rung_xdotool, v8.log_progress)
    try:
        v8.RUNNER = focus_runner(record)
        v8._grab = grab2
        v8.rung_xdotool = rung_x
        v8.log_progress = slow_log
        with environment(CU_DRY_RUN=None):
            with v8._within(3.0):
                result = json.loads(v8.mouse_agent("click", {"x": 5, "y": 5}))
    finally:
        v8.RUNNER, v8._grab, v8.rung_xdotool, v8.log_progress = originals3
    assert result["ok"] is True and result["verified"] is True, result
    assert dispatches["n"] == 1, "verified click must never be re-dispatched"
    # (d) AT-SPI: a getState() that blocks past the deadline must abort
    # the traversal (one-shot SIGALRM must not be swallowed). Stub the
    # pyatspi module: _frame_is_active imports it internally, and the
    # fixture must not depend on the binding being installed (Codex
    # trio-2 pass-25 hermeticity).
    class SlowState:
        def contains(self, state):
            time.sleep(0.35)
            return True
    class SlowFrame:
        name = "Slow"
        def getRoleName(self):
            return "frame"
        def getState(self):
            return SlowState()
    import types as _types4
    saved_pyatspi = sys.modules.get("pyatspi")
    sys.modules["pyatspi"] = _types4.SimpleNamespace(STATE_ACTIVE=8)
    began = time.monotonic()
    raised = False
    try:
        with v8._within(0.1):
            v8._atspi_focused_frame([[SlowFrame(), SlowFrame()]], "Slow")
    except v8.ActionTimeout:
        raised = True
    finally:
        if saved_pyatspi is not None:
            sys.modules["pyatspi"] = saved_pyatspi
        else:
            sys.modules.pop("pyatspi", None)
    elapsed = time.monotonic() - began
    assert raised and elapsed < 0.3, (raised, elapsed)
    # (e) Dry keys model: order and repetition through the sh wrapper.
    token = v8._dry_state.set({"calls": 0, "slept": True, "text": "",
                               "field": "OLD", "clip": "NEW",
                               "selected": False})
    def dry_keys(keys):
        return v8.run_tool(json.dumps(
            dict(tool="kbd_agent", action="keys", args={"keys": keys})))
    try:
        with environment(CU_DRY_RUN="1"):
            dry_keys(["ctrl+v", "ctrl+a"])
            state = v8._dry_state.get()
            assert state["field"] == "OLDNEW" and state["selected"] is True, state
            dry_keys(["ctrl+v", "ctrl+v"])
            state = v8._dry_state.get()
            assert state["field"] == "NEWNEW", state
    finally:
        v8._dry_state.reset(token)


def test_pass5_fixes():
    # (a) rung_atspi must propagate deadline exceptions to the ladder's
    # timeout handler instead of converting them to an ordinary failure.
    import types as _types
    record = []
    def boom(desktop, title):
        raise v8.ActionTimeout()
    originals = (v8.RUNNER, v8._atspi_focused_frame, v8._atspi_module,
                 v8._atspi_tried, v8.ATSPI_AVAILABLE)
    try:
        v8.RUNNER = focus_runner(record)
        v8._atspi_focused_frame = boom
        v8._atspi_module = _types.SimpleNamespace(
            Registry=_types.SimpleNamespace(getDesktop=lambda n: []))
        v8._atspi_tried = True
        v8.ATSPI_AVAILABLE = True
        with environment(CU_DRY_RUN=None):
            with v8._within(10):
                try:
                    v8.rung_atspi({"text": "Save"}, "native")
                    raise AssertionError("ActionTimeout was swallowed")
                except v8.ActionTimeout:
                    pass
    finally:
        (v8.RUNNER, v8._atspi_focused_frame, v8._atspi_module,
         v8._atspi_tried, v8.ATSPI_AVAILABLE) = originals
    # (b) A pagetext step's live CDP read must update the loop's URL.
    progress = os.path.join(tempfile.mkdtemp(prefix="cu-trio2-"), "p.jsonl")
    hash_urls = []
    real_hash = v8.compute_state_hash
    def recording_hash(title, url, content, shot=None):
        hash_urls.append(url)
        return real_hash(title, url, content, shot)
    plan = ["observe", "pagetext", "done"]
    seen = []
    def fake_chat(task, steps, last_result, note):
        tool = plan[len(seen)]
        seen.append(last_result)
        return json.dumps(dict(tool=tool, result="url checked")), "stop", 0.0
    def fake_tool(raw):
        if json.loads(raw)["tool"] == "observe":
            return ("Window title: SPA\nWindow id: 100\n"
                    "Window class: chromium\nSurface: browser_cdp\n"
                    "URL: https://spa.test/A")
        v8._last_cdp_url = "https://spa.test/B"  # _cdp_text stash
        v8._cdp_read_url = "https://spa.test/B"  # this step's fresh read
        return "BODY B"
    originals2 = (v8.call_qwen, v8.run_tool, v8._focus_verify,
                  v8._latest_shot, v8._tab_url, v8.compute_state_hash,
                  v8._last_cdp_url)
    try:
        v8.call_qwen, v8.run_tool = fake_chat, fake_tool
        v8._focus_verify = lambda fresh=False: dict(title="SPA", window_id="100",
                                        window_class="chromium",
                                        surface="browser_cdp")
        v8._latest_shot = lambda: None
        v8._tab_url = lambda: ""
        v8.compute_state_hash = recording_hash
        v8._last_cdp_url = ""
        with environment(CU_PROGRESS_LOG=progress, CUA_BIN=None):
            with contextlib.redirect_stdout(io.StringIO()):
                result = v8.run_loop("pagetext url adoption", max_iters=4)
    finally:
        (v8.call_qwen, v8.run_tool, v8._focus_verify,
         v8._latest_shot, v8._tab_url, v8.compute_state_hash,
         v8._last_cdp_url) = originals2
    assert "url checked" in result, result
    assert "https://spa.test/B" in hash_urls, hash_urls


def test_pass6_fixes():
    # (a) A click dispatched by a rung whose runner then times out must
    # be claimed by that rung; verification adjudicates and the ladder
    # must not click again through the next engine.
    dispatches = {"xdotool": 0, "cua": 0}
    def runner(argv, *args, **kw):
        argv = [str(a) for a in argv]
        if "mousemove" in argv and "click" in argv:
            dispatches["xdotool"] += 1
            raise subprocess.TimeoutExpired(argv, 1)
        return focus_runner([])(argv, *args, **kw)
    frames = {"n": 0}
    def grab():
        frames["n"] += 1
        return (b"frame-a" if frames["n"] % 2 else b"frame-b") * 32
    def cua_spy(*a, **k):
        dispatches["cua"] += 1
        return {}
    originals = (v8.RUNNER, v8._grab, v8._cua)
    try:
        v8.RUNNER, v8._grab, v8._cua = runner, grab, cua_spy
        with environment(CU_DRY_RUN=None):
            result = json.loads(agent_click())
    finally:
        v8.RUNNER, v8._grab, v8._cua = originals
    assert result["ok"] is True and result["verified"] is True, result
    assert dispatches == {"xdotool": 1, "cua": 0}, dispatches
    # (b) Same claim inside rung_atspi when its click dispatch times out.
    import types as _types
    class Ext:
        x, y, width, height = 10, 10, 20, 10
    class Comp:
        def getExtents(self, coord):
            return Ext()
    class Node:
        def __init__(self, name, role, children=()):
            self.name, self._role, self._children = name, role, list(children)
        def getRoleName(self):
            return self._role
        def getState(self):
            # Real pyatspi nodes always expose getState; the pass-22
            # visibility gate uses it. Fake widgets are visible+enabled.
            return _types.SimpleNamespace(contains=lambda state: True)
        def queryComponent(self):
            return Comp()
        def __iter__(self):
            return iter(self._children)
    frame = Node("Editor", "frame", [Node("Save", "push button")])
    fake_atspi = _types.SimpleNamespace(
        STATE_VISIBLE=1, STATE_ENABLED=2,
        Registry=_types.SimpleNamespace(getDesktop=lambda n: [[frame]]))
    def fake_call(argv, **kw):
        joined = " ".join(str(a) for a in argv)
        if "getactivewindow" in joined:
            return "123"
        if "getwindowname" in joined:
            return "Editor"
        raise subprocess.TimeoutExpired(argv, 1)
    saved_module = sys.modules.get("pyatspi")
    originals2 = (v8._call, v8._atspi_module, v8._atspi_tried,
                  v8.ATSPI_AVAILABLE)
    try:
        sys.modules["pyatspi"] = fake_atspi
        v8._call = fake_call
        v8._atspi_module, v8._atspi_tried = None, False
        v8.ATSPI_AVAILABLE = True
        with environment(CU_DRY_RUN=None):
            with v8._within(10):
                ok, via, detail = v8.rung_atspi({"text": "Save"}, "native")
    finally:
        if saved_module is not None:
            sys.modules["pyatspi"] = saved_module
        else:
            sys.modules.pop("pyatspi", None)
        (v8._call, v8._atspi_module, v8._atspi_tried,
         v8.ATSPI_AVAILABLE) = originals2
    assert ok is True and "dispatch_timeout_unverified" in detail, (ok, detail)
    # (c) Sticky live URL: observe must not regress an adopted CDP URL
    # back to lagging metadata (hash URLs: A -> B -> B -> B).
    progress = os.path.join(tempfile.mkdtemp(prefix="cu-trio2-"), "p.jsonl")
    hash_urls = []
    real_hash = v8.compute_state_hash
    def recording_hash(title, url, content, shot=None):
        hash_urls.append(url)
        return real_hash(title, url, content, shot)
    plan = ["observe", "pagetext", "observe", "pagetext", "done"]
    seen = []
    def fake_chat(task, steps, last_result, note):
        tool = plan[len(seen)]
        seen.append(last_result)
        return json.dumps(dict(tool=tool, result="sticky checked")), "stop", 0.0
    def fake_tool(raw):
        if json.loads(raw)["tool"] == "observe":
            return ("Window title: SPA\nWindow id: 100\n"
                    "Window class: chromium\nSurface: browser_cdp\n"
                    "URL: https://spa.test/A")
        v8._last_cdp_url = "https://spa.test/B"
        v8._cdp_read_url = "https://spa.test/B"
        return "BODY"
    originals3 = (v8.call_qwen, v8.run_tool, v8._focus_verify,
                  v8._latest_shot, v8._tab_url, v8.compute_state_hash,
                  v8._last_cdp_url)
    try:
        v8.call_qwen, v8.run_tool = fake_chat, fake_tool
        v8._focus_verify = lambda fresh=False: dict(title="SPA", window_id="100",
                                        window_class="chromium",
                                        surface="browser_cdp")
        v8._latest_shot = lambda: None
        v8._tab_url = lambda: "https://spa.test/A"  # metadata lags forever
        v8.compute_state_hash = recording_hash
        v8._last_cdp_url = ""
        with environment(CU_PROGRESS_LOG=progress, CUA_BIN=None):
            with contextlib.redirect_stdout(io.StringIO()):
                result = v8.run_loop("sticky url", max_iters=6)
    finally:
        (v8.call_qwen, v8.run_tool, v8._focus_verify,
         v8._latest_shot, v8._tab_url, v8.compute_state_hash,
         v8._last_cdp_url) = originals3
    assert "sticky checked" in result, result
    first_b = hash_urls.index("https://spa.test/B")
    assert "https://spa.test/A" not in hash_urls[first_b:], hash_urls
    # (d) Cross-browser: a stale Chromium stash must not be adopted for a
    # Firefox clipboard pagetext.
    hash_urls2 = []
    def recording_hash2(title, url, content, shot=None):
        hash_urls2.append(url)
        return real_hash(title, url, content, shot)
    plan2 = ["observe", "pagetext", "done"]
    seen2 = []
    def fake_chat2(task, steps, last_result, note):
        tool = plan2[len(seen2)]
        seen2.append(last_result)
        return json.dumps(dict(tool=tool, result="ff checked")), "stop", 0.0
    def fake_tool2(raw):
        if json.loads(raw)["tool"] == "observe":
            return ("Window title: FF\nWindow id: 200\n"
                    "Window class: firefox\nSurface: browser\n")
        return "FF BODY"
    originals4 = (v8.call_qwen, v8.run_tool, v8._focus_verify,
                  v8._latest_shot, v8._tab_url, v8.compute_state_hash,
                  v8._last_cdp_url)
    try:
        v8.call_qwen, v8.run_tool = fake_chat2, fake_tool2
        v8._focus_verify = lambda fresh=False: dict(title="FF", window_id="200",
                                        window_class="firefox",
                                        surface="browser")
        v8._latest_shot = lambda: None
        v8._tab_url = lambda: ""
        v8.compute_state_hash = recording_hash2
        v8._last_cdp_url = "https://chromium.test/X"
        with environment(CU_PROGRESS_LOG=progress, CUA_BIN=None):
            with contextlib.redirect_stdout(io.StringIO()):
                result = v8.run_loop("cross browser stash", max_iters=4)
    finally:
        (v8.call_qwen, v8.run_tool, v8._focus_verify,
         v8._latest_shot, v8._tab_url, v8.compute_state_hash,
         v8._last_cdp_url) = originals4
    assert "ff checked" in result, result
    assert "https://chromium.test/X" not in hash_urls2, hash_urls2


def agent_click():
    return v8.mouse_agent("click", {"x": 5, "y": 5})


def test_pass7_fixes():
    # (a) AT-SPI claim survival: the click runner blocks past the rung's
    # internal 3s slice; SIGALRM fires inside _call, the claim is
    # recorded, and it must survive the _within exit check.
    import types as _types
    class Ext:
        x, y, width, height = 10, 10, 20, 10
    class Comp:
        def getExtents(self, coord):
            return Ext()
    class Node:
        def __init__(self, name, role, children=()):
            self.name, self._role, self._children = name, role, list(children)
        def getRoleName(self):
            return self._role
        def getState(self):
            # Real pyatspi nodes always expose getState; the pass-22
            # visibility gate uses it. Fake widgets are visible+enabled.
            return _types.SimpleNamespace(contains=lambda state: True)
        def queryComponent(self):
            return Comp()
        def __iter__(self):
            return iter(self._children)
    frame = Node("Editor", "frame", [Node("Save", "push button")])
    fake_atspi = _types.SimpleNamespace(
        STATE_VISIBLE=1, STATE_ENABLED=2,
        Registry=_types.SimpleNamespace(getDesktop=lambda n: [[frame]]))
    def fake_call(argv, **kw):
        joined = " ".join(str(a) for a in argv)
        if "getactivewindow" in joined:
            return "123"
        if "getwindowname" in joined:
            return "Editor"
        if "getdisplaygeometry" in joined:
            return "1920 1080"
        time.sleep(3.4)  # SIGALRM (3s slice) interrupts the dispatch
        return ""
    saved_module = sys.modules.get("pyatspi")
    originals = (v8._call, v8._atspi_module, v8._atspi_tried,
                 v8.ATSPI_AVAILABLE)
    try:
        sys.modules["pyatspi"] = fake_atspi
        v8._call = fake_call
        v8._atspi_module, v8._atspi_tried = None, False
        v8.ATSPI_AVAILABLE = True
        with environment(CU_DRY_RUN=None):
            with v8._within(10):
                ok, via, detail = v8.rung_atspi({"text": "Save"}, "native")
    finally:
        if saved_module is not None:
            sys.modules["pyatspi"] = saved_module
        else:
            sys.modules.pop("pyatspi", None)
        (v8._call, v8._atspi_module, v8._atspi_tried,
         v8.ATSPI_AVAILABLE) = originals
    assert ok is True and "dispatch_timeout_unverified" in detail, (ok, detail)
    # (b) Keyboard rung: Return dispatched, runner times out -> claim.
    originals_b = (v8._call, v8._ensure_focus)
    try:
        v8._ensure_focus = lambda args: None
        def kb_call(argv, **kw):
            raise subprocess.TimeoutExpired(argv, 1)
        v8._call = kb_call
        with environment(CU_DRY_RUN=None):
            with v8._within(10):
                ok, via, detail = v8.rung_keyboard(
                    {"text": "Save", "via_keyboard_ok": True}, "native")
    finally:
        v8._call, v8._ensure_focus = originals_b
    assert ok is True and detail == "dispatch_timeout_unverified", (ok, detail)
    # (c) Sticky retirement: once metadata MOVES to a new value C, it
    # wins over the learned live URL B.
    progress = os.path.join(tempfile.mkdtemp(prefix="cu-trio2-"), "p.jsonl")
    hash_urls = []
    real_hash = v8.compute_state_hash
    def recording_hash(title, url, content, shot=None):
        hash_urls.append(url)
        return real_hash(title, url, content, shot)
    meta = {"url": "https://spa.test/A"}
    plan = ["observe", "pagetext", "observe", "done"]
    seen = []
    def fake_chat(task, steps, last_result, note):
        tool = plan[len(seen)]
        seen.append(last_result)
        if len(seen) == 3:
            meta["url"] = "https://spa.test/C"  # real navigation, metadata current
        return json.dumps(dict(tool=tool, result="retire checked")), "stop", 0.0
    def fake_tool(raw):
        if json.loads(raw)["tool"] == "observe":
            return ("Window title: SPA\nWindow id: 100\n"
                    "Window class: chromium\nSurface: browser_cdp\n"
                    "URL: " + meta["url"])
        v8._last_cdp_url = "https://spa.test/B"
        v8._cdp_read_url = "https://spa.test/B"
        return "BODY"
    originals_c = (v8.call_qwen, v8.run_tool, v8._focus_verify,
                   v8._latest_shot, v8._tab_url, v8.compute_state_hash,
                   v8._last_cdp_url, v8._cdp_read_url, v8._sticky_base)
    try:
        v8.call_qwen, v8.run_tool = fake_chat, fake_tool
        v8._focus_verify = lambda fresh=False: dict(title="SPA", window_id="100",
                                        window_class="chromium",
                                        surface="browser_cdp")
        v8._latest_shot = lambda: None
        v8._tab_url = lambda: meta["url"]
        v8.compute_state_hash = recording_hash
        v8._last_cdp_url, v8._cdp_read_url, v8._sticky_base = "", "", None
        with environment(CU_PROGRESS_LOG=progress, CUA_BIN=None):
            with contextlib.redirect_stdout(io.StringIO()):
                result = v8.run_loop("sticky retirement", max_iters=5)
    finally:
        (v8.call_qwen, v8.run_tool, v8._focus_verify,
         v8._latest_shot, v8._tab_url, v8.compute_state_hash,
         v8._last_cdp_url, v8._cdp_read_url, v8._sticky_base) = originals_c
    assert "retire checked" in result, result
    assert "https://spa.test/C" in hash_urls, hash_urls
    # (d) Freshness restoration: back on Chromium, a fresh CDP read
    # returning the SAME URL B must restore it over Firefox's F.
    hash_urls2 = []
    def recording_hash2(title, url, content, shot=None):
        hash_urls2.append(url)
        return real_hash(title, url, content, shot)
    focus_seq = {"n": 0}
    def two_browser_focus():
        focus_seq["n"] += 1
        if focus_seq["n"] <= 1:
            return dict(title="FF", window_id="200", window_class="firefox",
                        surface="browser")
        return dict(title="SPA", window_id="100", window_class="chromium",
                    surface="browser_cdp")
    def fake_cdp():
        v8._cdp_read_url = "https://spa.test/B"  # same value as the stash
        return "CHROME TEXT"
    plan2 = ["observe", "mouse_agent", "done"]
    seen2 = []
    def fake_chat2(task, steps, last_result, note):
        tool = plan2[len(seen2)]
        seen2.append(last_result)
        if tool == "mouse_agent":
            return json.dumps(dict(tool=tool, action="click", args={"x": 1, "y": 2})), "stop", 0.0
        return json.dumps(dict(tool=tool, result="fresh checked")), "stop", 0.0
    def fake_tool2(raw):
        if json.loads(raw)["tool"] == "observe":
            return ("Window title: FF\nWindow id: 200\n"
                    "Window class: firefox\nSurface: browser\n"
                    "URL: https://ff.test/F")
        return '{"ok": true, "via": "xdotool", "verified": true}'
    originals_d = (v8.call_qwen, v8.run_tool, v8._focus_verify,
                   v8._cdp_text, v8._latest_shot, v8._tab_url,
                   v8.compute_state_hash, v8._last_cdp_url,
                   v8._cdp_read_url, v8._sticky_base, v8._automation_tab)
    try:
        v8.call_qwen, v8.run_tool = fake_chat2, fake_tool2
        v8._focus_verify = two_browser_focus
        v8._cdp_text = fake_cdp
        v8._latest_shot = lambda: None
        v8._tab_url = lambda: ""
        v8.compute_state_hash = recording_hash2
        v8._last_cdp_url = "https://spa.test/B"
        v8._cdp_read_url, v8._sticky_base = "", None
        # Window 100 is the automation window (pass-39 sensor gate).
        v8._automation_tab = lambda: {"window_id": 100, "url": ""}
        with environment(CU_PROGRESS_LOG=progress, CUA_BIN=None):
            with contextlib.redirect_stdout(io.StringIO()):
                result = v8.run_loop("freshness restoration", max_iters=4)
    finally:
        (v8.call_qwen, v8.run_tool, v8._focus_verify,
         v8._cdp_text, v8._latest_shot, v8._tab_url,
         v8.compute_state_hash, v8._last_cdp_url,
         v8._cdp_read_url, v8._sticky_base, v8._automation_tab) = originals_d
    assert "fresh checked" in result, result
    assert hash_urls2[-1] == "https://spa.test/B", hash_urls2


def test_pass8_fixes():
    # (a) Finding 8: a timeout-claimed dispatch whose effect is NOT
    # visible must stop the ladder, not fall through to another click.
    dispatches = {"xdotool": 0, "cua": 0}
    def runner(argv, *a, **kw):
        argv = [str(x) for x in argv]
        if "mousemove" in argv and "click" in argv:
            dispatches["xdotool"] += 1
            raise subprocess.TimeoutExpired(argv, 1)
        return focus_runner([])(argv, *a, **kw)
    def cua_spy(*a, **k):
        dispatches["cua"] += 1
        return {}
    originals = (v8.RUNNER, v8._grab, v8._cua)
    try:
        v8.RUNNER, v8._cua = runner, cua_spy
        v8._grab = lambda: b"frame-a" * 32  # effect never visible
        with environment(CU_DRY_RUN=None):
            result = json.loads(agent_click())
    finally:
        v8.RUNNER, v8._grab, v8._cua = originals
    assert result["ok"] is False, result
    assert result["verified"] == "timeout_dispatch_unverified", result
    assert dispatches == {"xdotool": 1, "cua": 0}, dispatches
    # (b) Finding 4: metadata catching up to the live URL retires the
    # stickiness; a later navigation back must be visible again.
    saved = (v8._last_cdp_url, v8._sticky_base)
    try:
        v8._last_cdp_url, v8._sticky_base = "https://x/B", "https://x/A"
        assert v8._sticky_url("browser_cdp", "https://x/A") == "https://x/B"
        assert v8._sticky_url("browser_cdp", "https://x/B") == "https://x/B"
        assert v8._sticky_base is None, "catch-up must retire the base"
        assert v8._sticky_url("browser_cdp", "https://x/A") == "https://x/A"
    finally:
        v8._last_cdp_url, v8._sticky_base = saved
    # (c) Finding 7: two live adoptions (B then C) keep the METADATA
    # baseline A, so lagging metadata never resurrects A afterwards.
    progress = os.path.join(tempfile.mkdtemp(prefix="cu-trio2-"), "p.jsonl")
    hash_urls = []
    real_hash = v8.compute_state_hash
    def recording_hash(title, url, content, shot=None):
        hash_urls.append(url)
        return real_hash(title, url, content, shot)
    reads = iter(["https://spa.test/B", "https://spa.test/C"])
    plan = ["observe", "pagetext", "pagetext", "observe", "done"]
    seen = []
    def fake_chat(task, steps, last_result, note):
        tool = plan[len(seen)]
        seen.append(last_result)
        return json.dumps(dict(tool=tool, result="base checked")), "stop", 0.0
    def fake_tool(raw):
        if json.loads(raw)["tool"] == "observe":
            return ("Window title: SPA\nWindow id: 100\n"
                    "Window class: chromium\nSurface: browser_cdp\n"
                    "URL: https://spa.test/A")
        live_url = next(reads)
        v8._last_cdp_url = live_url
        v8._cdp_read_url = live_url
        return "BODY"
    originals_c = (v8.call_qwen, v8.run_tool, v8._focus_verify,
                   v8._latest_shot, v8._tab_url, v8.compute_state_hash,
                   v8._last_cdp_url, v8._cdp_read_url, v8._sticky_base)
    try:
        v8.call_qwen, v8.run_tool = fake_chat, fake_tool
        v8._focus_verify = lambda fresh=False: dict(title="SPA", window_id="100",
                                        window_class="chromium",
                                        surface="browser_cdp")
        v8._latest_shot = lambda: None
        v8._tab_url = lambda: "https://spa.test/A"
        v8.compute_state_hash = recording_hash
        v8._last_cdp_url, v8._cdp_read_url, v8._sticky_base = "", "", None
        with environment(CU_PROGRESS_LOG=progress, CUA_BIN=None):
            with contextlib.redirect_stdout(io.StringIO()):
                result = v8.run_loop("adoption baseline", max_iters=6)
    finally:
        (v8.call_qwen, v8.run_tool, v8._focus_verify,
         v8._latest_shot, v8._tab_url, v8.compute_state_hash,
         v8._last_cdp_url, v8._cdp_read_url, v8._sticky_base) = originals_c
    assert "base checked" in result, result
    assert hash_urls[-1] == "https://spa.test/C", hash_urls


def test_pass9_fixes():
    # (a) Verification ERROR after a timeout claim stops the ladder too.
    dispatches = {"xdotool": 0, "cua": 0}
    def runner(argv, *a, **kw):
        argv = [str(x) for x in argv]
        if "mousemove" in argv and "click" in argv:
            dispatches["xdotool"] += 1
            raise subprocess.TimeoutExpired(argv, 1)
        return focus_runner([])(argv, *a, **kw)
    grabs = {"n": 0}
    def grab():
        grabs["n"] += 1
        if grabs["n"] >= 3:  # before-grabs (keyboard, xdotool) succeed
            raise RuntimeError("cua shot failed")
        return b"frame-a" * 32
    def cua_spy(*a, **k):
        dispatches["cua"] += 1
        return {}
    originals = (v8.RUNNER, v8._grab, v8._cua)
    try:
        v8.RUNNER, v8._grab, v8._cua = runner, grab, cua_spy
        with environment(CU_DRY_RUN=None):
            result = json.loads(agent_click())
    finally:
        v8.RUNNER, v8._grab, v8._cua = originals
    assert result["ok"] is False, result
    assert result["verified"] == "timeout_dispatch_unverified", result
    assert dispatches == {"xdotool": 1, "cua": 0}, dispatches
    # (b) Adoption with metadata temporarily unavailable keeps the
    # previous metadata base (no re-base onto the effective URL).
    progress = os.path.join(tempfile.mkdtemp(prefix="cu-trio2-"), "p.jsonl")
    hash_urls = []
    real_hash = v8.compute_state_hash
    def recording_hash(title, url, content, shot=None):
        hash_urls.append(url)
        return real_hash(title, url, content, shot)
    tab_calls = {"n": 0}
    def flaky_tab_url():
        tab_calls["n"] += 1
        if tab_calls["n"] == 5:
            return ""  # transient metadata outage at the 2nd adoption
        return "https://spa.test/A"
    reads = iter(["https://spa.test/B", "https://spa.test/C"])
    plan = ["observe", "pagetext", "pagetext", "observe", "done"]
    seen = []
    def fake_chat(task, steps, last_result, note):
        tool = plan[len(seen)]
        seen.append(last_result)
        return json.dumps(dict(tool=tool, result="outage checked")), "stop", 0.0
    def fake_tool(raw):
        if json.loads(raw)["tool"] == "observe":
            return ("Window title: SPA\nWindow id: 100\n"
                    "Window class: chromium\nSurface: browser_cdp\n"
                    "URL: https://spa.test/A")
        live_url = next(reads)
        v8._last_cdp_url = live_url
        v8._cdp_read_url = live_url
        return "BODY"
    originals_b = (v8.call_qwen, v8.run_tool, v8._focus_verify,
                   v8._latest_shot, v8._tab_url, v8.compute_state_hash,
                   v8._last_cdp_url, v8._cdp_read_url, v8._sticky_base)
    try:
        v8.call_qwen, v8.run_tool = fake_chat, fake_tool
        v8._focus_verify = lambda fresh=False: dict(title="SPA", window_id="100",
                                        window_class="chromium",
                                        surface="browser_cdp")
        v8._latest_shot = lambda: None
        v8._tab_url = flaky_tab_url
        v8.compute_state_hash = recording_hash
        v8._last_cdp_url, v8._cdp_read_url, v8._sticky_base = "", "", None
        with environment(CU_PROGRESS_LOG=progress, CUA_BIN=None):
            with contextlib.redirect_stdout(io.StringIO()):
                result = v8.run_loop("metadata outage", max_iters=6)
    finally:
        (v8.call_qwen, v8.run_tool, v8._focus_verify,
         v8._latest_shot, v8._tab_url, v8.compute_state_hash,
         v8._last_cdp_url, v8._cdp_read_url, v8._sticky_base) = originals_b
    assert "outage checked" in result, result
    assert hash_urls[-1] == "https://spa.test/C", hash_urls
    # (c) A cached observe payload must not resurrect its stale URL
    # after metadata caught up with the live URL.
    hash_urls2 = []
    def recording_hash2(title, url, content, shot=None):
        hash_urls2.append(url)
        return real_hash(title, url, content, shot)
    meta = {"url": "https://spa.test/A"}
    observe_calls = {"n": 0}
    plan2 = ["observe", "pagetext", "observe", "observe", "done"]
    seen2 = []
    def fake_chat2(task, steps, last_result, note):
        tool = plan2[len(seen2)]
        seen2.append(last_result)
        if len(seen2) == 4:
            meta["url"] = "https://spa.test/B"  # metadata catches up later
        return json.dumps(dict(tool=tool, result="cache url checked")), "stop", 0.0
    def fake_tool2(raw):
        tool = json.loads(raw)["tool"]
        if tool == "observe":
            observe_calls["n"] += 1
            return ("Window title: SPA\nWindow id: 100\n"
                    "Window class: chromium\nSurface: browser_cdp\n"
                    "URL: " + meta["url"])
        v8._last_cdp_url = "https://spa.test/B"
        v8._cdp_read_url = "https://spa.test/B"
        return "BODY"
    originals_c = (v8.call_qwen, v8.run_tool, v8._focus_verify,
                   v8._latest_shot, v8._tab_url, v8.compute_state_hash,
                   v8._last_cdp_url, v8._cdp_read_url, v8._sticky_base)
    try:
        v8.call_qwen, v8.run_tool = fake_chat2, fake_tool2
        v8._focus_verify = lambda fresh=False: dict(title="SPA", window_id="100",
                                        window_pid="4242",
                                        window_class="chromium",
                                        surface="browser_cdp")
        v8._latest_shot = lambda: None
        v8._tab_url = lambda: meta["url"]
        v8.compute_state_hash = recording_hash2
        v8._last_cdp_url, v8._cdp_read_url, v8._sticky_base = "", "", None
        with environment(CU_PROGRESS_LOG=progress, CUA_BIN=None):
            with contextlib.redirect_stdout(io.StringIO()):
                result = v8.run_loop("cached observe url", max_iters=6)
    finally:
        (v8.call_qwen, v8.run_tool, v8._focus_verify,
         v8._latest_shot, v8._tab_url, v8.compute_state_hash,
         v8._last_cdp_url, v8._cdp_read_url, v8._sticky_base) = originals_c
    assert "cache url checked" in result, result
    assert observe_calls["n"] == 2, observe_calls  # 2nd observe was cached
    assert hash_urls2[-1] == "https://spa.test/B", hash_urls2


def test_pass10_fixes():
    # (a) First adoption during a metadata outage: the baseline is the
    # still-metadata-sourced current URL; lagging metadata returning
    # later must not regress the effective URL.
    progress = os.path.join(tempfile.mkdtemp(prefix="cu-trio2-"), "p.jsonl")
    hash_urls = []
    real_hash = v8.compute_state_hash
    def recording_hash(title, url, content, shot=None):
        hash_urls.append(url)
        return real_hash(title, url, content, shot)
    tab_calls = {"n": 0}
    def outage_tab_url():
        tab_calls["n"] += 1
        if tab_calls["n"] == 3:
            return ""  # the adoption-time read is the outage
        return "https://spa.test/A"
    plan = ["observe", "pagetext", "observe", "done"]
    seen = []
    def fake_chat(task, steps, last_result, note):
        tool = plan[len(seen)]
        seen.append(last_result)
        return json.dumps(dict(tool=tool, result="first outage checked")), "stop", 0.0
    def fake_tool(raw):
        if json.loads(raw)["tool"] == "observe":
            return ("Window title: SPA\nWindow id: 100\n"
                    "Window class: chromium\nSurface: browser_cdp\n"
                    "URL: https://spa.test/A")
        v8._last_cdp_url = "https://spa.test/B"
        v8._cdp_read_url = "https://spa.test/B"
        return "BODY"
    originals = (v8.call_qwen, v8.run_tool, v8._focus_verify,
                 v8._latest_shot, v8._tab_url, v8.compute_state_hash,
                 v8._last_cdp_url, v8._cdp_read_url, v8._sticky_base)
    try:
        v8.call_qwen, v8.run_tool = fake_chat, fake_tool
        v8._focus_verify = lambda fresh=False: dict(title="SPA", window_id="100",
                                        window_class="chromium",
                                        surface="browser_cdp")
        v8._latest_shot = lambda: None
        v8._tab_url = outage_tab_url
        v8.compute_state_hash = recording_hash
        v8._last_cdp_url, v8._cdp_read_url, v8._sticky_base = "", "", None
        with environment(CU_PROGRESS_LOG=progress, CUA_BIN=None):
            with contextlib.redirect_stdout(io.StringIO()):
                result = v8.run_loop("first adoption outage", max_iters=5)
    finally:
        (v8.call_qwen, v8.run_tool, v8._focus_verify,
         v8._latest_shot, v8._tab_url, v8.compute_state_hash,
         v8._last_cdp_url, v8._cdp_read_url, v8._sticky_base) = originals
    assert "first outage checked" in result, result
    assert hash_urls[-1] == "https://spa.test/B", hash_urls
    # (b) Model feedback: a cached observe must report the EFFECTIVE
    # URL in the text the model reads, not the captured metadata line.
    meta = {"url": "https://spa.test/A"}
    plan2 = ["observe", "pagetext", "observe", "observe", "done"]
    seen2 = []
    def fake_chat2(task, steps, last_result, note):
        tool = plan2[len(seen2)]
        seen2.append(last_result)
        if len(seen2) == 4:
            meta["url"] = "https://spa.test/B"
        return json.dumps(dict(tool=tool, result="feedback checked")), "stop", 0.0
    def fake_tool2(raw):
        if json.loads(raw)["tool"] == "observe":
            return ("Window title: SPA\nWindow id: 100\n"
                    "Window class: chromium\nSurface: browser_cdp\n"
                    "URL: " + meta["url"])
        v8._last_cdp_url = "https://spa.test/B"
        v8._cdp_read_url = "https://spa.test/B"
        return "BODY"
    originals_b = (v8.call_qwen, v8.run_tool, v8._focus_verify,
                   v8._latest_shot, v8._tab_url,
                   v8._last_cdp_url, v8._cdp_read_url, v8._sticky_base)
    try:
        v8.call_qwen, v8.run_tool = fake_chat2, fake_tool2
        v8._focus_verify = lambda fresh=False: dict(title="SPA", window_id="100",
                                        window_class="chromium",
                                        surface="browser_cdp")
        v8._latest_shot = lambda: None
        v8._tab_url = lambda: meta["url"]
        v8._last_cdp_url, v8._cdp_read_url, v8._sticky_base = "", "", None
        with environment(CU_PROGRESS_LOG=progress, CUA_BIN=None):
            with contextlib.redirect_stdout(io.StringIO()):
                result = v8.run_loop("feedback url", max_iters=6)
    finally:
        (v8.call_qwen, v8.run_tool, v8._focus_verify,
         v8._latest_shot, v8._tab_url,
         v8._last_cdp_url, v8._cdp_read_url, v8._sticky_base) = originals_b
    assert "feedback checked" in result, result
    feedback = seen2[-1] or ""
    assert "URL: https://spa.test/B" in feedback, feedback[:200]
    assert "spa.test/A" not in feedback, feedback[:200]


def test_pass11_fixes():
    # Cold start: metadata unavailable from the beginning (observe has
    # no URL at all); the live URL is adopted during the outage; when
    # lagging metadata first becomes readable it must become the
    # baseline, not override the live URL.
    progress = os.path.join(tempfile.mkdtemp(prefix="cu-trio2-"), "p.jsonl")
    hash_urls = []
    real_hash = v8.compute_state_hash
    def recording_hash(title, url, content, shot=None):
        hash_urls.append(url)
        return real_hash(title, url, content, shot)
    tab_calls = {"n": 0}
    def cold_tab_url():
        tab_calls["n"] += 1
        if tab_calls["n"] <= 3:
            return ""  # observe pre, pagetext pre, adoption: all out
        return "https://spa.test/A"
    plan = ["observe", "pagetext", "observe", "done"]
    seen = []
    def fake_chat(task, steps, last_result, note):
        tool = plan[len(seen)]
        seen.append(last_result)
        return json.dumps(dict(tool=tool, result="cold checked")), "stop", 0.0
    def fake_tool(raw):
        if json.loads(raw)["tool"] == "observe":
            return ("Window title: SPA\nWindow id: 100\n"
                    "Window class: chromium\nSurface: browser_cdp\n")
        v8._last_cdp_url = "https://spa.test/B"
        v8._cdp_read_url = "https://spa.test/B"
        return "BODY"
    originals = (v8.call_qwen, v8.run_tool, v8._focus_verify,
                 v8._latest_shot, v8._tab_url, v8.compute_state_hash,
                 v8._last_cdp_url, v8._cdp_read_url, v8._sticky_base)
    try:
        v8.call_qwen, v8.run_tool = fake_chat, fake_tool
        v8._focus_verify = lambda fresh=False: dict(title="SPA", window_id="100",
                                        window_class="chromium",
                                        surface="browser_cdp")
        v8._latest_shot = lambda: None
        v8._tab_url = cold_tab_url
        v8.compute_state_hash = recording_hash
        v8._last_cdp_url, v8._cdp_read_url, v8._sticky_base = "", "", None
        with environment(CU_PROGRESS_LOG=progress, CUA_BIN=None):
            with contextlib.redirect_stdout(io.StringIO()):
                result = v8.run_loop("cold start outage", max_iters=5)
    finally:
        (v8.call_qwen, v8.run_tool, v8._focus_verify,
         v8._latest_shot, v8._tab_url, v8.compute_state_hash,
         v8._last_cdp_url, v8._cdp_read_url, v8._sticky_base) = originals
    assert "cold checked" in result, result
    assert hash_urls[-1] == "https://spa.test/B", hash_urls


def test_pass12_fixes():
    # Matching first live read (live == metadata A) performs no
    # adoption and establishes no baseline; a later genuine navigation
    # to B via metadata must take effect, not be suppressed as "lag".
    progress = os.path.join(tempfile.mkdtemp(prefix="cu-trio2-"), "p.jsonl")
    hash_urls = []
    real_hash = v8.compute_state_hash
    def recording_hash(title, url, content, shot=None):
        hash_urls.append(url)
        return real_hash(title, url, content, shot)
    meta = {"url": "https://spa.test/A"}
    plan = ["observe", "pagetext", "observe", "done"]
    seen = []
    def fake_chat(task, steps, last_result, note):
        tool = plan[len(seen)]
        seen.append(last_result)
        if len(seen) == 3:
            meta["url"] = "https://spa.test/B"  # genuine navigation
        return json.dumps(dict(tool=tool, result="match checked")), "stop", 0.0
    def fake_tool(raw):
        if json.loads(raw)["tool"] == "observe":
            return ("Window title: SPA\nWindow id: 100\n"
                    "Window class: chromium\nSurface: browser_cdp\n"
                    "URL: " + meta["url"])
        v8._last_cdp_url = "https://spa.test/A"  # live agrees with metadata
        v8._cdp_read_url = "https://spa.test/A"
        return "BODY"
    originals = (v8.call_qwen, v8.run_tool, v8._focus_verify,
                 v8._latest_shot, v8._tab_url, v8.compute_state_hash,
                 v8._last_cdp_url, v8._cdp_read_url, v8._sticky_base,
                 v8._live_unbased)
    try:
        v8.call_qwen, v8.run_tool = fake_chat, fake_tool
        v8._focus_verify = lambda fresh=False: dict(title="SPA", window_id="100",
                                        window_class="chromium",
                                        surface="browser_cdp")
        v8._latest_shot = lambda: None
        v8._tab_url = lambda: meta["url"]
        v8.compute_state_hash = recording_hash
        v8._last_cdp_url, v8._cdp_read_url = "", ""
        v8._sticky_base, v8._live_unbased = "", False
        with environment(CU_PROGRESS_LOG=progress, CUA_BIN=None):
            with contextlib.redirect_stdout(io.StringIO()):
                result = v8.run_loop("matching first read", max_iters=5)
    finally:
        (v8.call_qwen, v8.run_tool, v8._focus_verify,
         v8._latest_shot, v8._tab_url, v8.compute_state_hash,
         v8._last_cdp_url, v8._cdp_read_url, v8._sticky_base,
         v8._live_unbased) = originals
    assert "match checked" in result, result
    assert hash_urls[-1] == "https://spa.test/B", hash_urls


def test_pass14_fixes():
    # (a) A verification ERROR after an ordinary successful dispatch
    # stops the ladder (unknown effect), like the timeout-claim case.
    dispatches = {"xdotool": 0, "cua": 0}
    def runner(argv, *a, **kw):
        argv = [str(x) for x in argv]
        if "mousemove" in argv and "click" in argv:
            dispatches["xdotool"] += 1
            return 0, "", ""
        return focus_runner([])(argv, *a, **kw)
    grabs = {"n": 0}
    def grab():
        grabs["n"] += 1
        if grabs["n"] == 3:
            raise RuntimeError("cua shot failed")
        return (b"frame-a" if grabs["n"] % 2 else b"frame-b") * 32
    def cua_spy(*a, **k):
        dispatches["cua"] += 1
        return {}
    originals = (v8.RUNNER, v8._grab, v8._cua)
    try:
        v8.RUNNER, v8._grab, v8._cua = runner, grab, cua_spy
        with environment(CU_DRY_RUN=None):
            result = json.loads(agent_click())
    finally:
        v8.RUNNER, v8._grab, v8._cua = originals
    assert result["ok"] is False, result
    assert result["verified"] == "verification_error", result
    assert dispatches == {"xdotool": 1, "cua": 0}, dispatches
    # (b) Plain-browser (Firefox) pagetext cache must not survive an
    # input step: the DOM can change invisibly to the sensor there.
    progress = os.path.join(tempfile.mkdtemp(prefix="cu-trio2-"), "p.jsonl")
    live = {"text": "BODY A"}
    plan = ["observe", "pagetext", "kbd_agent", "pagetext", "done"]
    seen = []
    def fake_chat(task, steps, last_result, note):
        tool = plan[len(seen)]
        seen.append(last_result)
        if tool == "kbd_agent":
            return json.dumps(dict(tool=tool, action="type", args={"text": "x"})), "stop", 0.0
        return json.dumps(dict(tool=tool, result="ff cache checked")), "stop", 0.0
    def fake_tool(raw):
        tool = json.loads(raw)["tool"]
        if tool == "observe":
            return ("Window title: FF\nWindow id: 200\n"
                    "Window class: firefox\nSurface: browser\n"
                    "URL: https://ff.test/")
        if tool == "pagetext":
            return live["text"]
        return '{"ok": true}'
    originals_b = (v8.call_qwen, v8.run_tool, v8._focus_verify,
                   v8._latest_shot, v8._tab_url)
    try:
        v8.call_qwen = fake_chat
        orig_tool = fake_tool
        def flipping_tool(raw):
            out = orig_tool(raw)
            if json.loads(raw)["tool"] == "kbd_agent":
                live["text"] = "BODY B"  # typing changed the DOM
            return out
        v8.run_tool = flipping_tool
        v8._focus_verify = lambda fresh=False: dict(title="FF", window_id="200",
                                        window_class="firefox",
                                        surface="browser")
        v8._latest_shot = lambda: None
        v8._tab_url = lambda: ""
        with environment(CU_PROGRESS_LOG=progress, CUA_BIN=None):
            with contextlib.redirect_stdout(io.StringIO()):
                result = v8.run_loop("firefox pagetext cache", max_iters=6)
    finally:
        (v8.call_qwen, v8.run_tool, v8._focus_verify,
         v8._latest_shot, v8._tab_url) = originals_b
    assert "ff cache checked" in result, result
    pagetext_results = [s for s in seen if s and "BODY" in s]
    assert pagetext_results[-1].startswith("BODY B"), pagetext_results


def test_pass15_fixes():
    # The plain-browser pagetext invalidation must not depend on the
    # post-step focus probe succeeding: here the probe raises
    # subprocess.TimeoutExpired right after the input step.
    progress = os.path.join(tempfile.mkdtemp(prefix="cu-trio2-"), "p.jsonl")
    live = {"text": "BODY A"}
    probe = {"armed": False, "fired": False}
    def flaky_focus():
        if probe["armed"] and not probe["fired"]:
            probe["fired"] = True
            raise subprocess.TimeoutExpired(["xdotool", "getactivewindow"], 1)
        return dict(title="FF", window_id="200", window_class="firefox",
                    surface="browser")
    plan = ["observe", "pagetext", "kbd_agent", "pagetext", "done"]
    seen = []
    def fake_chat(task, steps, last_result, note):
        tool = plan[len(seen)]
        seen.append(last_result)
        if tool == "kbd_agent":
            return json.dumps(dict(tool=tool, action="keys", args={"keys": ["a"]})), "stop", 0.0
        return json.dumps(dict(tool=tool, result="probe checked")), "stop", 0.0
    def fake_tool(raw):
        tool = json.loads(raw)["tool"]
        if tool == "observe":
            return ("Window title: FF\nWindow id: 200\n"
                    "Window class: firefox\nSurface: browser\n"
                    "URL: https://ff.test/")
        if tool == "pagetext":
            return live["text"]
        return '{"ok": true}'
    originals = (v8.call_qwen, v8.run_tool, v8._focus_verify,
                 v8._latest_shot, v8._tab_url)
    try:
        v8.call_qwen = fake_chat
        def flipping_tool(raw):
            out = fake_tool(raw)
            if json.loads(raw)["tool"] == "kbd_agent":
                live["text"] = "BODY B"
                probe["armed"] = True  # the post-step probe will time out
            return out
        v8.run_tool = flipping_tool
        v8._focus_verify = flaky_focus
        v8._latest_shot = lambda: None
        v8._tab_url = lambda: ""
        with environment(CU_PROGRESS_LOG=progress, CUA_BIN=None):
            with contextlib.redirect_stdout(io.StringIO()):
                result = v8.run_loop("probe timeout invalidation", max_iters=6)
    finally:
        (v8.call_qwen, v8.run_tool, v8._focus_verify,
         v8._latest_shot, v8._tab_url) = originals
    assert "probe checked" in result, result
    pagetext_results = [s for s in seen if s and "BODY" in s]
    assert pagetext_results[-1].startswith("BODY B"), pagetext_results


def test_pass16_fixes():
    # Hybrid config: plain cua binary + CDP port env classifies as
    # browser_cdp but pagetext reads via the clipboard; the cache must
    # still not survive an input step.
    progress = os.path.join(tempfile.mkdtemp(prefix="cu-trio2-"), "p.jsonl")
    live = {"text": "BODY A"}
    plan = ["observe", "pagetext", "kbd_agent", "pagetext", "done"]
    seen = []
    def fake_chat(task, steps, last_result, note):
        tool = plan[len(seen)]
        seen.append(last_result)
        if tool == "kbd_agent":
            return json.dumps(dict(tool=tool, action="keys", args={"keys": ["a"]})), "stop", 0.0
        return json.dumps(dict(tool=tool, result="hybrid checked")), "stop", 0.0
    def fake_tool(raw):
        tool = json.loads(raw)["tool"]
        if tool == "observe":
            return ("Window title: SPA\nWindow id: 100\n"
                    "Window class: chromium\nSurface: browser_cdp\n"
                    "URL: https://spa.test/")
        if tool == "pagetext":
            return live["text"]
        return '{"ok": true}'
    originals = (v8.call_qwen, v8.run_tool, v8._focus_verify,
                 v8._latest_shot, v8._tab_url)
    try:
        v8.call_qwen = fake_chat
        def flipping_tool(raw):
            out = fake_tool(raw)
            if json.loads(raw)["tool"] == "kbd_agent":
                live["text"] = "BODY B"
            return out
        v8.run_tool = flipping_tool
        v8._focus_verify = lambda fresh=False: dict(title="SPA", window_id="100",
                                        window_class="chromium",
                                        surface="browser_cdp")
        v8._latest_shot = lambda: None
        v8._tab_url = lambda: ""
        with environment(CU_PROGRESS_LOG=progress, CUA_BIN="/example/cua",
                         CUA_CDP_PORT="9210"):
            with contextlib.redirect_stdout(io.StringIO()):
                result = v8.run_loop("hybrid config cache", max_iters=6)
    finally:
        (v8.call_qwen, v8.run_tool, v8._focus_verify,
         v8._latest_shot, v8._tab_url) = originals
    assert "hybrid checked" in result, result
    pagetext_results = [s for s in seen if s and "BODY" in s]
    assert pagetext_results[-1].startswith("BODY B"), pagetext_results


def test_pass17_fixes():
    # Two same-title windows: switching focus via win_agent must not
    # serve the first window's pagetext for the second.
    progress = os.path.join(tempfile.mkdtemp(prefix="cu-trio2-"), "p.jsonl")
    window = {"id": "100"}
    texts = {"100": "BODY A", "200": "BODY B"}
    plan = ["observe", "pagetext", "win_agent", "pagetext", "done"]
    seen = []
    def fake_chat(task, steps, last_result, note):
        tool = plan[len(seen)]
        seen.append(last_result)
        if tool == "win_agent":
            return json.dumps(dict(tool=tool, action="focus", args={"match": "200"})), "stop", 0.0
        return json.dumps(dict(tool=tool, result="window checked")), "stop", 0.0
    def fake_tool(raw):
        tool = json.loads(raw)["tool"]
        if tool == "observe":
            return ("Window title: SPA\nWindow id: " + window["id"] + "\n"
                    "Window class: chromium\nSurface: browser_cdp\n"
                    "URL: https://spa.test/")
        if tool == "pagetext":
            return texts[window["id"]]
        return '{"ok": true}'
    originals = (v8.call_qwen, v8.run_tool, v8._focus_verify,
                 v8._latest_shot, v8._tab_url)
    try:
        v8.call_qwen = fake_chat
        def switching_tool(raw):
            out = fake_tool(raw)
            if json.loads(raw)["tool"] == "win_agent":
                window["id"] = "200"
            return out
        v8.run_tool = switching_tool
        v8._focus_verify = lambda fresh=False: dict(
            title="SPA", window_id=window["id"], window_class="chromium",
            surface="browser_cdp")
        v8._latest_shot = lambda: None
        v8._tab_url = lambda: ""
        with environment(CU_PROGRESS_LOG=progress, CUA_BIN="/example/cua",
                         CUA_CDP_PORT="9210"):
            with contextlib.redirect_stdout(io.StringIO()):
                result = v8.run_loop("window switch cache", max_iters=6)
    finally:
        (v8.call_qwen, v8.run_tool, v8._focus_verify,
         v8._latest_shot, v8._tab_url) = originals
    assert "window checked" in result, result
    pagetext_results = [s for s in seen if s and "BODY" in s]
    assert pagetext_results[-1].startswith("BODY B"), pagetext_results


def test_pass18_fixes():
    # (a) A failed pre-tool focus probe after an external window switch
    # must not let stale cached reads through.
    progress = os.path.join(tempfile.mkdtemp(prefix="cu-trio2-"), "p.jsonl")
    window = {"id": "100"}
    probe = {"armed": False, "fired": False}
    texts = {"100": "BODY A", "200": "BODY B"}
    def flaky_focus():
        if probe["armed"] and not probe["fired"]:
            probe["fired"] = True
            raise RuntimeError("getactivewindow failed")
        return dict(title="SPA", window_id=window["id"],
                    window_class="chromium", surface="browser_cdp")
    plan = ["observe", "pagetext", "pagetext", "done"]
    seen = []
    def fake_chat(task, steps, last_result, note):
        tool = plan[len(seen)]
        seen.append(last_result)
        if len(seen) == 2:
            window["id"] = "200"     # external switch after first read
            probe["armed"] = True    # next pre-tool probe fails once
        return json.dumps(dict(tool=tool, result="probe fail checked")), "stop", 0.0
    def fake_tool(raw):
        tool = json.loads(raw)["tool"]
        if tool == "observe":
            return ("Window title: SPA\nWindow id: " + window["id"] + "\n"
                    "Window class: chromium\nSurface: browser_cdp\n"
                    "URL: https://spa.test/")
        return texts[window["id"]]
    originals = (v8.call_qwen, v8.run_tool, v8._focus_verify,
                 v8._latest_shot, v8._tab_url)
    try:
        v8.call_qwen, v8.run_tool = fake_chat, fake_tool
        v8._focus_verify = flaky_focus
        v8._latest_shot = lambda: None
        v8._tab_url = lambda: ""
        with environment(CU_PROGRESS_LOG=progress, CUA_BIN="/example/cua",
                         CUA_CDP_PORT="9210"):
            with contextlib.redirect_stdout(io.StringIO()):
                result = v8.run_loop("pre-tool probe failure", max_iters=5)
    finally:
        (v8.call_qwen, v8.run_tool, v8._focus_verify,
         v8._latest_shot, v8._tab_url) = originals
    assert "probe fail checked" in result, result
    pagetext_results = [s for s in seen if s and "BODY" in s]
    assert pagetext_results[-1].startswith("BODY B"), pagetext_results
    # (b) Repeated successful window switches are progress: the stall
    # monitor must not abort the run.
    window2 = {"id": "100"}
    plan2 = ["observe", "pagetext", "win_agent", "win_agent", "win_agent",
             "win_agent", "done"]
    seen2 = []
    def fake_chat2(task, steps, last_result, note):
        tool = plan2[len(seen2)]
        seen2.append(last_result)
        if tool == "win_agent":
            return json.dumps(dict(tool=tool, action="focus", args={"match": "x"})), "stop", 0.0
        return json.dumps(dict(tool=tool, result="stall checked")), "stop", 0.0
    def fake_tool2(raw):
        tool = json.loads(raw)["tool"]
        if tool == "observe":
            return ("Window title: SPA\nWindow id: " + window2["id"] + "\n"
                    "Window class: chromium\nSurface: browser_cdp\n"
                    "URL: https://spa.test/")
        if tool == "pagetext":
            return "BODY"
        return '{"ok": true}'
    originals2 = (v8.call_qwen, v8.run_tool, v8._focus_verify,
                  v8._latest_shot, v8._tab_url)
    try:
        v8.call_qwen = fake_chat2
        def switching_tool(raw):
            out = fake_tool2(raw)
            if json.loads(raw)["tool"] == "win_agent":
                window2["id"] = str(int(window2["id"]) + 100)
            return out
        v8.run_tool = switching_tool
        v8._focus_verify = lambda fresh=False: dict(
            title="SPA", window_id=window2["id"], window_class="chromium",
            surface="browser_cdp")
        v8._latest_shot = lambda: None
        v8._tab_url = lambda: ""
        with environment(CU_PROGRESS_LOG=progress, CUA_BIN=None):
            with contextlib.redirect_stdout(io.StringIO()):
                result = v8.run_loop("window switch stall", max_iters=8)
    finally:
        (v8.call_qwen, v8.run_tool, v8._focus_verify,
         v8._latest_shot, v8._tab_url) = originals2
    assert "stall checked" in result, result
    assert "STALLED" not in result, result


def test_pass19_fixes():
    # Roundtrip poisoning: a pagetext read taken while the focus probe
    # failed (real window 200, loop still records 100) must not be
    # stored; returning to window 100 must read its own text.
    progress = os.path.join(tempfile.mkdtemp(prefix="cu-trio2-"), "p.jsonl")
    real_window = {"id": "100"}
    probe = {"armed": False, "fired": False}
    texts = {"100": "BODY 100", "200": "BODY 200"}
    def flaky_focus():
        if probe["armed"] and not probe["fired"]:
            probe["fired"] = True
            raise RuntimeError("getactivewindow failed")
        # The loop's view: while the probe is armed it never learns of
        # the switch, so report the recorded window (100) until fired.
        return dict(title="Same", window_id="100",
                    window_class="firefox", surface="browser")
    plan = ["observe", "pagetext", "pagetext", "pagetext", "done"]
    seen = []
    def fake_chat(task, steps, last_result, note):
        tool = plan[len(seen)]
        seen.append(last_result)
        if len(seen) == 3:
            real_window["id"] = "200"  # external switch before read 2
            probe["armed"] = True     # that step's probe fails
        if len(seen) == 4:
            real_window["id"] = "100"  # user returns before the last read
            # Vary an unused field so the identical-call guard does not
            # replay the previous step before the cache path is reached.
            return json.dumps(dict(tool=tool, args={"vary": 1},
                                   result="roundtrip checked")), "stop", 0.0
        return json.dumps(dict(tool=tool, result="roundtrip checked")), "stop", 0.0
    def fake_tool(raw):
        tool = json.loads(raw)["tool"]
        if tool == "observe":
            return ("Window title: Same\nWindow id: 100\n"
                    "Window class: firefox\nSurface: browser\n"
                    "URL: https://ff.test/")
        return texts[real_window["id"]]
    originals = (v8.call_qwen, v8.run_tool, v8._focus_verify,
                 v8._latest_shot, v8._tab_url)
    try:
        v8.call_qwen, v8.run_tool = fake_chat, fake_tool
        v8._focus_verify = flaky_focus
        v8._latest_shot = lambda: None
        v8._tab_url = lambda: ""
        with environment(CU_PROGRESS_LOG=progress, CUA_BIN=None):
            with contextlib.redirect_stdout(io.StringIO()):
                result = v8.run_loop("roundtrip poisoning", max_iters=6)
    finally:
        (v8.call_qwen, v8.run_tool, v8._focus_verify,
         v8._latest_shot, v8._tab_url) = originals
    assert "roundtrip checked" in result, result
    pagetext_results = [s for s in seen if s and "BODY" in s]
    assert pagetext_results[0].startswith("BODY 100"), pagetext_results
    assert pagetext_results[1].startswith("BODY 200"), pagetext_results
    assert pagetext_results[2].startswith("BODY 100"), pagetext_results
    assert "[cached:true]" not in pagetext_results[2], pagetext_results


def test_pass20_fixes():
    # Repeated focus-probe failures are NOT progress: the run must
    # stall-abort, not burn to the iteration cap with changed=True.
    progress = os.path.join(tempfile.mkdtemp(prefix="cu-trio2-"), "p.jsonl")
    def dead_focus():
        raise RuntimeError("getactivewindow failed")
    seen = []
    def fake_chat(task, steps, last_result, note):
        seen.append(last_result)
        return json.dumps(dict(tool="observe",
                               args={"vary": len(seen)})), "stop", 0.0
    originals = (v8.call_qwen, v8.run_tool, v8._focus_verify,
                 v8._latest_shot, v8._tab_url)
    try:
        v8.call_qwen = fake_chat
        v8.run_tool = lambda raw: "Window title: X\nWindow id: 1\nSurface: browser\n"
        v8._focus_verify = dead_focus
        v8._latest_shot = lambda: None
        v8._tab_url = lambda: ""
        with environment(CU_PROGRESS_LOG=progress, CUA_BIN=None):
            with contextlib.redirect_stdout(io.StringIO()):
                result = v8.run_loop("dead probe", max_iters=10)
    finally:
        (v8.call_qwen, v8.run_tool, v8._focus_verify,
         v8._latest_shot, v8._tab_url) = originals
    assert result.startswith("ABORTED_STALLED"), result[:120]


def test_pass22_fixes():
    # Codex pass-22 fresh-scan fixes, rung/call level.
    # (#1) A dispatch that errors AFTER delivery is claimed, never
    # re-fired: rung_cua converts a post-click RuntimeError to a claim.
    original_cua = v8._cua
    try:
        def err_cua(*a, **k):
            raise RuntimeError("screenshot failed after click")
        v8._cua = err_cua
        ok, via, detail = v8.rung_cua({"x": 1, "y": 2}, "native")
        assert ok and "dispatch_error_unverified" in detail, (ok, detail)
        def missing_cua(*a, **k):
            raise FileNotFoundError("no cua")
        v8._cua = missing_cua
        ok, via, detail = v8.rung_cua({"x": 1, "y": 2}, "native")
        assert not ok and "engine_unavailable" in detail, (ok, detail)
    finally:
        v8._cua = original_cua
    # (#1/#2 at the ladder level) an error claim with no pixel change
    # stops the ladder; the next rung is never invoked.
    record = []
    cua_calls = []
    originals = (v8.RUNNER, v8._grab, v8.rung_xdotool, v8.rung_cua)
    try:
        v8.RUNNER = focus_runner(record)
        v8._grab = lambda: b"frame-same" * 32
        v8.rung_xdotool = lambda a, s: (True, "xdotool",
                                        "dispatch_error_unverified: boom")
        v8.rung_cua = lambda a, s: cua_calls.append(1) or (True, "cua", "clicked")
        with environment(CU_DRY_RUN=None):
            result = json.loads(v8.mouse_agent("click", {"x": 10, "y": 10}))
        assert result["ok"] is False, result
        assert result["verified"] == "error_dispatch_unverified", result
        assert not cua_calls, "claimed dispatch must stop the ladder"
    finally:
        (v8.RUNNER, v8._grab, v8.rung_xdotool, v8.rung_cua) = originals
    # (#3) A first-chunk type failure aborts: the chunk may have
    # partially delivered before erroring, so no engine fallback.
    record3 = []
    def runner3(argv, timeout_s, env_extra=None):
        record3.append([str(a) for a in argv])
        if argv[0] == "xdotool" and argv[1] == "getactivewindow":
            return 0, "100\n", ""
        if argv[0] == "xdotool" and argv[1] == "getwindowname":
            return 0, "Editor\n", ""
        if argv[0] == "xprop":
            return 0, 'WM_CLASS(STRING) = "x", "gnome-text-editor"\n', ""
        if argv[0] == "xdotool" and argv[1] == "type":
            return 1, "", "device busy after partial delivery"
        return 0, "{}", ""
    original_runner = v8.RUNNER
    try:
        v8.RUNNER = runner3
        with environment(CU_DRY_RUN=None, YDOTOOL_SOCKET="/nonexistent.sock"):
            result = v8.kbd_agent("type_verify",
                                  {"text": "ABCDE", "engine": "xdotool"})
    finally:
        v8.RUNNER = original_runner
    assert "mid-dispatch" in result, result[:200]
    types = [a for a in record3 if a[:2] == ["xdotool", "type"]]
    assert len(types) == 1, types
    # (#7) A deadline that expired BEFORE dispatch raises
    # PreDispatchTimeout and never invokes the runner.
    invoked = []
    def runner7(argv, timeout_s, env_extra=None):
        invoked.append(argv)
        return 0, "", ""
    original_runner = v8.RUNNER
    token = v8._deadline.set(time.monotonic() - 1)
    try:
        v8.RUNNER = runner7
        with environment(CU_DRY_RUN=None):
            try:
                v8._call(["xdotool", "mousemove", "1", "1"])
                raise AssertionError("expired deadline must raise")
            except v8.PreDispatchTimeout:
                pass
    finally:
        v8.RUNNER = original_runner
        v8._deadline.reset(token)
    assert not invoked, invoked
    # (#7b) _screen_size never swallows the deadline.
    original_call = v8._call
    try:
        def timeout_call(argv, **kw):
            raise v8.ActionTimeout("slice spent")
        v8._call = timeout_call
        try:
            v8._screen_size()
            raise AssertionError("_screen_size must propagate the deadline")
        except v8.ActionTimeout:
            pass
    finally:
        v8._call = original_call
    # (#9) _within re-arms an outer timer that expired during the block
    # instead of dropping it.
    fired = []
    old_handler = signal.signal(
        signal.SIGALRM, lambda s, f: fired.append(1))
    try:
        signal.setitimer(signal.ITIMER_REAL, 0.05)
        with v8._within(0.3):
            time.sleep(0.08)
        time.sleep(0.05)
        assert fired, "the outer alarm must still fire after _within exits"
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
    # (#5) A bad AT-SPI bus address makes the probe report unavailable
    # in a child process instead of aborting it (SIGABRT at the C level).
    child = subprocess.run(
        [sys.executable, "-c",
         "import qwen_executor_ark2_v8 as v8; print(v8.ATSPI_AVAILABLE)"],
        capture_output=True, text=True,
        env={**os.environ,
             "AT_SPI_BUS_ADDRESS": "unix:path=/nonexistent-trio2-bus"},
        cwd=os.path.dirname(os.path.abspath(v8.__file__)), timeout=60)
    assert child.returncode == 0, child.stderr[-300:]
    assert child.stdout.strip() == "False", child.stdout
    # (#6) AT-SPI skips a hidden same-name widget and clicks the visible
    # one.
    import types as _types
    class Ext:
        def __init__(self, x, y):
            self.x, self.y, self.width, self.height = x, y, 20, 10
    class Comp:
        def __init__(self, ext):
            self._ext = ext
        def getExtents(self, coord):
            return self._ext
    class SNode:
        def __init__(self, name, role, children=(), visible=True, ext=None):
            self.name, self._role, self._children = name, role, list(children)
            self._visible, self._ext = visible, ext
        def getRoleName(self):
            return self._role
        def getState(self):
            vis = self._visible
            return _types.SimpleNamespace(
                contains=lambda state: vis if state == 1 else True)
        def queryComponent(self):
            return Comp(self._ext)
        def __iter__(self):
            return iter(self._children)
    hidden = SNode("Save", "push button", visible=False, ext=Ext(10, 10))
    visible_btn = SNode("Save", "push button", visible=True, ext=Ext(200, 100))
    frame = SNode("Editor", "frame", [hidden, visible_btn])
    fake_atspi = _types.SimpleNamespace(
        STATE_VISIBLE=1, STATE_ENABLED=2,
        Registry=_types.SimpleNamespace(getDesktop=lambda n: [[frame]]))
    dispatches = []
    def fake_call22(argv, **kw):
        joined = " ".join(str(a) for a in argv)
        if "getactivewindow" in joined:
            return "123"
        if "getwindowname" in joined:
            return "Editor"
        dispatches.append([str(a) for a in argv])
        return ""
    saved_module = sys.modules.get("pyatspi")
    originals22 = (v8._call, v8._atspi_module, v8._atspi_tried, v8._screen_size,
                   v8.ATSPI_AVAILABLE)
    try:
        sys.modules["pyatspi"] = fake_atspi
        v8._call = fake_call22
        v8._atspi_module, v8._atspi_tried = None, False
        v8.ATSPI_AVAILABLE = True
        v8._screen_size = lambda: (1920, 1080)
        with environment(CU_DRY_RUN=None):
            with v8._within(10):
                ok, via, detail = v8.rung_atspi({"text": "Save"}, "native")
    finally:
        if saved_module is not None:
            sys.modules["pyatspi"] = saved_module
        else:
            sys.modules.pop("pyatspi", None)
        (v8._call, v8._atspi_module, v8._atspi_tried,
         v8._screen_size, v8.ATSPI_AVAILABLE) = originals22
    assert ok, detail
    assert dispatches and dispatches[0][2:4] == ["210", "105"], dispatches
    # (#13) Dry-run field state persists across separate run_tool calls,
    # like a real field: typing OLD then NEW yields OLDNEW.
    v8._dry_state.set(None)
    with environment(CU_DRY_RUN="1"):
        v8.run_tool(json.dumps(dict(
            tool="kbd_agent", action="type_verify",
            args={"text": "OLD", "engine": "xdotool"})))
        v8.run_tool(json.dumps(dict(
            tool="kbd_agent", action="type_verify",
            args={"text": "NEW", "engine": "xdotool"})))
    state = v8._dry_state.get()
    assert state is not None and state["field"] == "OLDNEW", state
    v8._dry_state.set(None)


def test_pass22_loop_fixes():
    # (#4b) observe binds the cua metadata URL only to Chromium focus.
    root = tempfile.mkdtemp(prefix="cu-trio2-meta-")
    with open(os.path.join(root, "state.json"), "w") as stream:
        # Window 101 is the automation window (pass-38 window-id
        # binding): Chromium focus on THAT window gets the URL;
        # Firefox focus never does.
        json.dump({"automation_tab": {"url": "https://chromium.example/",
                                      "window_id": 101}}, stream)
    original_focus = v8._focus_verify
    try:
        with environment(CUA_ROOT=root):
            v8._focus_verify = lambda fresh=False: dict(
                title="FF", window_id="100", window_class="firefox",
                surface="browser")
            out = v8.run_tool(json.dumps({"tool": "observe"}))
            assert "chromium.example" not in out, out
            v8._focus_verify = lambda fresh=False: dict(
                title="GM", window_id="101", window_class="chromium",
                surface="browser_cdp")
            out = v8.run_tool(json.dumps({"tool": "observe"}))
            assert "chromium.example" in out, out
    finally:
        v8._focus_verify = original_focus
    # (#10) A shell step invalidates the pagetext cache: the page body
    # changes under an unchanged title/URL via a shell-driven browser.
    progress = os.path.join(tempfile.mkdtemp(prefix="cu-trio2-"), "p.jsonl")
    body = {"text": "BODY A"}
    seen = []
    plan = ["observe", "pagetext", "shell", "observe", "pagetext", "done"]
    def fake_chat10(task, steps, last_result, note):
        tool = plan[len(seen)]
        seen.append(last_result)
        if tool == "shell":
            return json.dumps(dict(tool="shell",
                                   cmd="cua key F5")), "stop", 0.0
        return json.dumps(dict(tool=tool, result="shell checked")), "stop", 0.0
    def fake_tool10(raw):
        tool = json.loads(raw)["tool"]
        if tool == "observe":
            return ("Window title: Same\nWindow id: 100\n"
                    "Window class: firefox\nSurface: browser\n"
                    "URL: https://ff.test/")
        if tool == "shell":
            body["text"] = "BODY B"
            return "ok"
        return body["text"]
    originals = (v8.call_qwen, v8.run_tool, v8._focus_verify,
                 v8._latest_shot, v8._tab_url)
    try:
        v8.call_qwen, v8.run_tool = fake_chat10, fake_tool10
        v8._focus_verify = lambda fresh=False: dict(title="Same", window_id="100",
                                        window_class="firefox",
                                        surface="browser")
        v8._latest_shot = lambda: None
        v8._tab_url = lambda: ""
        with environment(CU_PROGRESS_LOG=progress, CUA_BIN=None):
            with contextlib.redirect_stdout(io.StringIO()):
                v8.run_loop("shell cache", max_iters=7)
    finally:
        (v8.call_qwen, v8.run_tool, v8._focus_verify,
         v8._latest_shot, v8._tab_url) = originals
    reads = [entry for entry in seen if "BODY" in entry]
    assert reads == ["BODY A", "BODY B"], \
        "shell step must invalidate the cached page text: %s" % reads
    # (#11) A post-action sensor timeout must not erase a verified
    # mouse receipt: the model still receives it, and it counts as
    # progress.
    progress = os.path.join(tempfile.mkdtemp(prefix="cu-trio2-"), "p.jsonl")
    seen11 = []
    plan11 = ["observe", "mouse_agent", "done"]
    receipt = json.dumps({"ok": True, "via": "xdotool", "verified": True})
    def fake_chat11(task, steps, last_result, note):
        tool = plan11[len(seen11)]
        seen11.append(last_result)
        if tool == "mouse_agent":
            return json.dumps(dict(tool=tool, action="click",
                                   args={"x": 1, "y": 2})), "stop", 0.0
        return json.dumps(dict(tool=tool, result="sensor checked")), "stop", 0.0
    def fake_tool11(raw):
        tool = json.loads(raw)["tool"]
        if tool == "observe":
            return ("Window title: Same\nWindow id: 100\n"
                    "Window class: chromium\nSurface: browser_cdp\n"
                    "URL: https://ff.test/")
        return receipt
    real_hash = v8.compute_state_hash
    hash_calls = {"n": 0}
    def flaky_hash(title, url, content, shot=None):
        hash_calls["n"] += 1
        if hash_calls["n"] >= 3:
            raise v8.ActionTimeout("sensor hash blocked")
        return real_hash(title, url, content, shot)
    originals = (v8.call_qwen, v8.run_tool, v8._focus_verify,
                 v8._latest_shot, v8._tab_url, v8.compute_state_hash,
                 v8._cdp_text)
    try:
        v8.call_qwen, v8.run_tool = fake_chat11, fake_tool11
        v8._focus_verify = lambda fresh=False: dict(title="Same", window_id="100",
                                        window_class="chromium",
                                        surface="browser_cdp")
        v8._latest_shot = lambda: None
        v8._tab_url = lambda: ""
        v8.compute_state_hash = flaky_hash
        v8._cdp_text = lambda: "PAGE"
        with environment(CU_PROGRESS_LOG=progress, CUA_BIN=None):
            with contextlib.redirect_stdout(io.StringIO()):
                v8.run_loop("sensor timeout", max_iters=5)
    finally:
        (v8.call_qwen, v8.run_tool, v8._focus_verify, v8._latest_shot,
         v8._tab_url, v8.compute_state_hash, v8._cdp_text) = originals
    assert receipt in seen11[-1], \
        "the verified receipt must survive the sensor timeout: %s" % seen11[-1][:120]
    # (#12) Verified keyboard receipts count as progress: repeated
    # verified edits the page sensor cannot see must not stall-abort.
    progress = os.path.join(tempfile.mkdtemp(prefix="cu-trio2-"), "p.jsonl")
    calls12 = {"n": 0}
    kbd_receipt = json.dumps({"ok": True, "via": "xdotool", "verified": True})
    def fake_chat12(task, steps, last_result, note):
        calls12["n"] += 1
        if calls12["n"] > 5:
            return json.dumps(dict(tool="done", result="edits done")), "stop", 0.0
        return json.dumps(dict(tool="kbd_agent", action="type_verify",
                               args={"text": "v%d" % calls12["n"],
                                     "vary": calls12["n"]})), "stop", 0.0
    originals = (v8.call_qwen, v8.run_tool, v8._focus_verify,
                 v8._latest_shot, v8._tab_url)
    try:
        v8.call_qwen = fake_chat12
        v8.run_tool = lambda raw: kbd_receipt
        v8._focus_verify = lambda fresh=False: dict(title="Same", window_id="100",
                                        window_class="chromium",
                                        surface="browser_cdp")
        v8._latest_shot = lambda: None
        v8._tab_url = lambda: ""
        with environment(CU_PROGRESS_LOG=progress, CUA_BIN=None):
            with contextlib.redirect_stdout(io.StringIO()):
                result = v8.run_loop("kbd progress", max_iters=8)
    finally:
        (v8.call_qwen, v8.run_tool, v8._focus_verify,
         v8._latest_shot, v8._tab_url) = originals
    assert not result.startswith("ABORTED_STALLED"), result[:120]


def test_pass23_fixes():
    # Codex pass-23 verification fixes.
    # (B1) A UnicodeDecodeError (ValueError) after an xdotool click was
    # delivered is claimed, not retried through the next engine.
    original_call = v8._call
    try:
        def decode_err(argv, **kw):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad byte")
        v8._call = decode_err
        ok, via, detail = v8.rung_xdotool({"x": 1, "y": 2}, "native")
        assert ok and "dispatch_error_unverified" in detail, (ok, detail)
    finally:
        v8._call = original_call
    # (B9) PreDispatchTimeout is NOT a claim: with the deadline spent,
    # rung_xdotool propagates it and the runner never runs.
    invoked = []
    original_runner = v8.RUNNER
    token = v8._deadline.set(time.monotonic() - 1)
    try:
        v8.RUNNER = lambda a, t, env_extra=None: invoked.append(a) or (0, "", "")
        with environment(CU_DRY_RUN=None):
            try:
                v8.rung_xdotool({"x": 1, "y": 2}, "native")
                raise AssertionError("PreDispatchTimeout must propagate")
            except v8.PreDispatchTimeout:
                pass
    finally:
        v8.RUNNER = original_runner
        v8._deadline.reset(token)
    assert not invoked, invoked
    # (B11) Bad coordinates are a pre-dispatch skip, never a claim, and
    # no command is invoked.
    cua_calls = []
    original_cua = v8._cua
    try:
        v8._cua = lambda *a, **k: cua_calls.append(a) or {}
        ok, via, detail = v8.rung_cua({"x": "invalid", "y": 1}, "native")
        assert not ok and "bad_coordinates" in detail, (ok, detail)
        assert not cua_calls, cua_calls
        # (B11) The lane's target-lookup failure is a skip, not a claim.
        def lane_miss(*a, **k):
            raise RuntimeError("no clickable element with text: Submit")
        v8._cua = lane_miss
        import socket as _socket
        real_cc = _socket.create_connection
        _socket.create_connection = lambda *a, **k: contextlib.nullcontext()
        try:
            with environment(CUA_BIN="/example/cua-lane"):
                ok, via, detail = v8.rung_cdp({"text": "Submit"}, "browser_cdp")
        finally:
            _socket.create_connection = real_cc
        assert not ok and "target_not_found" in detail, (ok, detail)
    finally:
        v8._cua = original_cua
    # (B12) verify=false must not promote an ambiguous dispatch to ok.
    record = []
    originals = (v8.RUNNER, v8._grab, v8.rung_xdotool)
    try:
        v8.RUNNER = focus_runner(record)
        v8._grab = lambda: b"frame-same" * 32
        v8.rung_xdotool = lambda a, s: (True, "xdotool",
                                        "dispatch_error_unverified: boom")
        with environment(CU_DRY_RUN=None):
            result = json.loads(v8.mouse_agent(
                "click", {"x": 10, "y": 10, "verify": False}))
        assert result["ok"] is False, result
        assert result["verified"] == "error_dispatch_unverified", result
    finally:
        (v8.RUNNER, v8._grab, v8.rung_xdotool) = originals
    # (B6) Lane CDP calls carry the CUA_CDP_HINT pin for the last-read
    # page.
    seen_calls = []
    originals = (v8._call, v8._last_cdp_url)
    try:
        def recording_call(argv, timeout_s=None, env_extra=None):
            seen_calls.append(env_extra)
            return '{"text": "page"}'
        v8._call = recording_call
        v8._last_cdp_url = "https://mail.google.com/mail/u/0/#inbox"
        v8._cua("cdp-text")
        assert seen_calls and seen_calls[0] == {
            "CUA_CDP_HINT": "https://mail.google.com/mail/u/0/#inbox"}, seen_calls
        seen_calls.clear()
        v8._cua("tab")
        assert seen_calls == [None], seen_calls
    finally:
        v8._call, v8._last_cdp_url = originals
    # (B8) rung_atspi refuses to touch the Registry when the probe
    # found AT-SPI unavailable.
    original_avail = v8.ATSPI_AVAILABLE
    try:
        v8.ATSPI_AVAILABLE = False
        with environment(CU_DRY_RUN=None):
            ok, via, detail = v8.rung_atspi({"text": "Save"}, "native")
        assert not ok and "atspi_unavailable" in detail, (ok, detail)
    finally:
        v8.ATSPI_AVAILABLE = original_avail
    # (B3) A paste that errors after delivery aborts; no xdotool retype.
    record3 = []
    def runner3(argv, timeout_s, env_extra=None):
        record3.append([str(a) for a in argv])
        return focus_runner([])(argv, timeout_s, env_extra)
    def paste_err(*a, **k):
        if a[:2] == ("key", "ctrl+v"):
            raise RuntimeError("paste delivered then errored")
        return {"ok": True, "text": ""}
    originals = (v8.RUNNER, v8._cua)
    try:
        v8.RUNNER, v8._cua = runner3, paste_err
        with environment(CU_DRY_RUN=None, YDOTOOL_SOCKET="/nonexistent.sock"):
            result = v8.kbd_agent("type_verify", {
                "text": "ABCDE", "engine": "auto", "safe_clipboard": True})
    finally:
        (v8.RUNNER, v8._cua) = originals
    assert "mid-dispatch" in result, result[:200]
    assert not [a for a in record3 if a[:2] == ["xdotool", "type"]], record3
    # (B5) FileNotFoundError after a delivered chunk aborts (the escape
    # is only for before-any-typing).
    record5 = []
    def runner5(argv, timeout_s, env_extra=None):
        record5.append([str(a) for a in argv])
        if argv[0] == "xdotool" and argv[1] == "type":
            done = [a for a in record5 if a[:2] == ["xdotool", "type"]]
            if len(done) >= 2:
                raise FileNotFoundError("xdotool vanished")
            return 0, "", ""
        return focus_runner([])(argv, timeout_s, env_extra)
    originals = (v8.RUNNER, v8._cua)
    cua_typed = []
    try:
        v8.RUNNER = runner5
        v8._cua = lambda *a, **k: cua_typed.append(a) or {"ok": True}
        with environment(CU_DRY_RUN=None, YDOTOOL_SOCKET="/nonexistent.sock"):
            result = v8.kbd_agent("type_verify", {
                "text": "x" * 150, "engine": "xdotool"})
    finally:
        (v8.RUNNER, v8._cua) = originals
    assert "mid-dispatch" in result, result[:200]
    # No fall-through to the cua TYPE engine. (The fresh before-frame
    # grab legitimately calls _cua("shot") since pass-66 — a read,
    # not a type dispatch.)
    assert not [a for a in cua_typed if a and a[0] == "type"], cua_typed


def test_pass23_loop_fixes():
    # (B7) Switching focus from Chromium to Firefox drops the Chromium
    # URL from subsequent state hashes.
    progress = os.path.join(tempfile.mkdtemp(prefix="cu-trio2-"), "p.jsonl")
    focus = {"current": dict(title="Gmail", window_id="100",
                             window_class="chromium", surface="browser_cdp")}
    seen = []
    plan = ["observe", "pagetext", "observe", "done"]
    def fake_chat(task, steps, last_result, note):
        tool = plan[len(seen)]
        seen.append(last_result)
        if len(seen) == 3:
            focus["current"] = dict(title="FF", window_id="200",
                                    window_class="firefox",
                                    surface="browser")
        return json.dumps(dict(tool=tool, result="url checked")), "stop", 0.0
    def fake_tool(raw):
        tool = json.loads(raw)["tool"]
        if tool == "observe":
            current = focus["current"]
            url_line = ("\nURL: https://chromium.example/"
                        if current["surface"] == "browser_cdp" else "")
            return ("Window title: %s\nWindow id: %s\nWindow class: %s\n"
                    "Surface: %s%s" % (current["title"], current["window_id"],
                                       current["window_class"],
                                       current["surface"], url_line))
        return "FF BODY"
    hash_urls = []
    real_hash = v8.compute_state_hash
    def recording_hash(title, url, content, shot=None):
        hash_urls.append((title, url))
        return real_hash(title, url, content, shot)
    originals = (v8.call_qwen, v8.run_tool, v8._focus_verify,
                 v8._latest_shot, v8._tab_url, v8.compute_state_hash,
                 v8._cdp_text)
    try:
        v8.call_qwen, v8.run_tool = fake_chat, fake_tool
        v8._focus_verify = lambda fresh=False: focus["current"]
        v8._latest_shot = lambda: None
        v8._tab_url = lambda: "https://chromium.example/"
        v8.compute_state_hash = recording_hash
        v8._cdp_text = lambda: "GMAIL BODY"
        with environment(CU_PROGRESS_LOG=progress, CUA_BIN=None):
            with contextlib.redirect_stdout(io.StringIO()):
                v8.run_loop("url switch", max_iters=6)
    finally:
        (v8.call_qwen, v8.run_tool, v8._focus_verify, v8._latest_shot,
         v8._tab_url, v8.compute_state_hash, v8._cdp_text) = originals
    ff_hashes = [url for (title, url) in hash_urls if title == "FF"]
    assert ff_hashes and all(
        "chromium.example" not in url for url in ff_hashes), hash_urls


def test_pass24_fixes():
    # (B9) PreDispatchTimeout propagates out of rung_atspi unchanged.
    import types as _types
    class Ext:
        x, y, width, height = 10, 10, 20, 10
    class Comp:
        def getExtents(self, coord):
            return Ext()
    class Node:
        def __init__(self, name, role, children=()):
            self.name, self._role, self._children = name, role, list(children)
        def getRoleName(self):
            return self._role
        def getState(self):
            return _types.SimpleNamespace(contains=lambda state: True)
        def queryComponent(self):
            return Comp()
        def __iter__(self):
            return iter(self._children)
    frame = Node("Editor", "frame", [Node("Save", "push button")])
    fake_atspi = _types.SimpleNamespace(
        STATE_VISIBLE=1, STATE_ENABLED=2,
        Registry=_types.SimpleNamespace(getDesktop=lambda n: [[frame]]))
    real_call = v8._call
    def fake_call(argv, **kw):
        joined = " ".join(str(a) for a in argv)
        if "getactivewindow" in joined:
            return "123"
        if "getwindowname" in joined:
            return "Editor"
        # Spend the deadline right at the dispatch: the real _call then
        # raises PreDispatchTimeout before invoking any runner.
        v8._deadline.set(time.monotonic() - 1)
        return real_call(argv, **kw)
    saved_module = sys.modules.get("pyatspi")
    originals = (v8._call, v8._atspi_module, v8._atspi_tried,
                 v8.ATSPI_AVAILABLE, v8._screen_size)
    token = v8._deadline.set(time.monotonic() + 100)
    try:
        sys.modules["pyatspi"] = fake_atspi
        v8._call = fake_call
        v8._atspi_module, v8._atspi_tried = None, False
        v8.ATSPI_AVAILABLE = True
        v8._screen_size = lambda: (1920, 1080)
        with environment(CU_DRY_RUN=None):
            try:
                v8.rung_atspi({"text": "Save"}, "native")
                raise AssertionError("PreDispatchTimeout was converted")
            except v8.PreDispatchTimeout:
                pass
    finally:
        v8._deadline.reset(token)
        if saved_module is not None:
            sys.modules["pyatspi"] = saved_module
        else:
            sys.modules.pop("pyatspi", None)
        (v8._call, v8._atspi_module, v8._atspi_tried,
         v8.ATSPI_AVAILABLE, v8._screen_size) = originals
    # (B11-cdp) A CDP protocol failure after mousePressed is a claim,
    # not a target lookup miss.
    original_cua = v8._cua
    import socket as _socket
    real_cc = _socket.create_connection
    try:
        def dispatch_err(*a, **k):
            raise RuntimeError("Input.dispatchMouseEvent: Session not found")
        v8._cua = dispatch_err
        _socket.create_connection = lambda *a, **k: contextlib.nullcontext()
        with environment(CUA_BIN="/example/cua-lane"):
            ok, via, detail = v8.rung_cdp({"text": "Submit"}, "browser_cdp")
        assert ok and "dispatch_error_unverified" in detail, (ok, detail)
    finally:
        v8._cua = original_cua
        _socket.create_connection = real_cc
    # (B6) The lane selector prefers an exact URL match over a sibling
    # whose URL has the hint as a prefix.
    lane_path = str(Path(__file__).resolve().parent.parent / "src" / "cua-lane")
    if os.path.isfile(lane_path):
        child = subprocess.run(
            [sys.executable, "-c", r'''
import importlib.util, io, json, os, sys, types
spec = importlib.util.spec_from_loader("cualane", loader=None)
lane = importlib.util.module_from_spec(spec)
lane.__file__ = str(Path(__file__).resolve().parent.parent / "src" / "cua-lane")
with open(lane.__file__) as stream:
    code = stream.read()
targets = [
    {"type": "page", "url": "https://app.test/page/other",
     "webSocketDebuggerUrl": "ws://B"},
    {"type": "page", "url": "https://app.test/page",
     "webSocketDebuggerUrl": "ws://A"},
]
class Resp:
    def __enter__(self):
        return io.BytesIO(json.dumps(targets).encode())
    def __exit__(self, *a):
        return False
import urllib.request
urllib.request.urlopen = lambda *a, **k: Resp()
picked = []
fake_ws = types.SimpleNamespace(
    create_connection=lambda url, timeout=0: picked.append(url) or
    types.SimpleNamespace())
sys.modules["websocket"] = fake_ws
os.environ["CUA_CDP_HINT"] = "https://app.test/page"
exec(compile(code, lane.__file__, "exec"), lane.__dict__)
lane._CDP()
print(picked)
'''], capture_output=True, text=True, timeout=60)
        assert child.returncode == 0, child.stderr[-300:]
        assert "ws://A" in child.stdout, child.stdout


def test_pass24_loop_fixes():
    # (B7) A focus switch inside an input step (Alt+Tab) drops the old
    # window's URL before the step's own state hash is computed.
    progress = os.path.join(tempfile.mkdtemp(prefix="cu-trio2-"), "p.jsonl")
    focus = {"current": dict(title="Gmail", window_id="100",
                             window_class="chromium", surface="browser_cdp")}
    seen = []
    plan = ["observe", "kbd_agent", "observe", "done"]
    def fake_chat(task, steps, last_result, note):
        tool = plan[len(seen)]
        seen.append(last_result)
        if tool == "kbd_agent":
            return json.dumps(dict(tool=tool, action="keys",
                                   args={"keys": ["alt+Tab"]})), "stop", 0.0
        return json.dumps(dict(tool=tool, result="switch checked")), "stop", 0.0
    def fake_tool(raw):
        tool = json.loads(raw)["tool"]
        if tool == "observe":
            current = focus["current"]
            url_line = ("\nURL: https://chromium.example/"
                        if current["surface"] == "browser_cdp" else "")
            return ("Window title: %s\nWindow id: %s\nWindow class: %s\n"
                    "Surface: %s%s" % (current["title"], current["window_id"],
                                       current["window_class"],
                                       current["surface"], url_line))
        # The Alt+Tab lands: focus is Firefox from here on.
        focus["current"] = dict(title="FF", window_id="200",
                                window_class="firefox", surface="browser")
        return json.dumps({"ok": True, "via": "xdotool", "verified": True})
    hash_urls = []
    real_hash = v8.compute_state_hash
    def recording_hash(title, url, content, shot=None):
        hash_urls.append((title, url))
        return real_hash(title, url, content, shot)
    originals = (v8.call_qwen, v8.run_tool, v8._focus_verify,
                 v8._latest_shot, v8._tab_url, v8.compute_state_hash,
                 v8._cdp_text)
    try:
        v8.call_qwen, v8.run_tool = fake_chat, fake_tool
        v8._focus_verify = lambda fresh=False: focus["current"]
        v8._latest_shot = lambda: None
        v8._tab_url = lambda: "https://chromium.example/"
        v8.compute_state_hash = recording_hash
        v8._cdp_text = lambda: "GMAIL BODY"
        with environment(CU_PROGRESS_LOG=progress, CUA_BIN=None):
            with contextlib.redirect_stdout(io.StringIO()):
                v8.run_loop("alt tab switch", max_iters=6)
    finally:
        (v8.call_qwen, v8.run_tool, v8._focus_verify, v8._latest_shot,
         v8._tab_url, v8.compute_state_hash, v8._cdp_text) = originals
    ff_hashes = [url for (title, url) in hash_urls if title == "FF"]
    assert ff_hashes and all(
        "chromium.example" not in url for url in ff_hashes), hash_urls


def test_pass26_fixes():
    # Codex pass-26 fresh-scan fixes.
    # (N1) A runner OSError after a cua click was delivered is claimed.
    original_call = v8._call
    try:
        def io_err(argv, timeout_s=None, env_extra=None):
            raise OSError(5, "Input/output error")
        v8._call = io_err
        ok, via, detail = v8.rung_cua({"x": 1, "y": 2}, "native")
        assert ok and "dispatch_error_unverified" in detail, (ok, detail)
    finally:
        v8._call = original_call
    # (N4) The default runner kills the whole process group on
    # timeout: a timed-out shell's grandchild must not run later.
    marker = os.path.join(tempfile.mkdtemp(prefix="cu-trio2-"), "grandchild")
    try:
        v8._default_runner(
            ["sh", "-c", "(sleep 0.6; touch %s) & sleep 5" % marker], 0.15)
        raise AssertionError("the foreground sleep must time out")
    except subprocess.TimeoutExpired:
        pass
    time.sleep(0.9)
    assert not os.path.exists(marker), "grandchild survived the timeout"
    # (N5) Type verification uses FRESH frames: typing that changed
    # the screen verifies even when the shot file is stale. (Since
    # pass-66 the before-frame is also a fresh grab, so the grab
    # stub is sequential: pre-type frame, then post-type frame.)
    record = []
    originals = (v8.RUNNER, v8._grab, v8._latest_shot)
    try:
        v8.RUNNER = focus_runner(record)
        _grabs = iter([b"frame-before" * 32, b"frame-after" * 32,
                       b"frame-after" * 32])
        v8._grab = lambda: next(_grabs, b"frame-after" * 32)
        v8._latest_shot = lambda: b"frame-stale" * 32
        with environment(CU_DRY_RUN=None, YDOTOOL_SOCKET="/nonexistent.sock"):
            result = json.loads(v8.kbd_agent(
                "type_verify", {"text": "hello", "engine": "xdotool"}))
        assert result["ok"] is True and result["verified"] is True, result
    finally:
        (v8.RUNNER, v8._grab, v8._latest_shot) = originals
    # (N7) Clipboard SETUP failure (cua unavailable) degrades to the
    # xdotool typing path instead of aborting.
    record7 = []
    def runner7(argv, timeout_s, env_extra=None):
        record7.append([str(a) for a in argv])
        return focus_runner([])(argv, timeout_s, env_extra)
    def cua_missing(*a, **k):
        raise FileNotFoundError("cua binary unavailable")
    originals = (v8.RUNNER, v8._cua)
    try:
        v8.RUNNER, v8._cua = runner7, cua_missing
        with environment(CU_DRY_RUN=None, YDOTOOL_SOCKET="/nonexistent.sock"):
            result = json.loads(v8.kbd_agent("type_verify", {
                "text": "fallback text", "engine": "auto",
                "safe_clipboard": True}))
        assert result["ok"] is True and result["via"] == "xdotool", result
        assert [a for a in record7 if a[:2] == ["xdotool", "type"]], record7
    finally:
        (v8.RUNNER, v8._cua) = originals
    # (N8) A focused dialog is a valid AT-SPI traversal root.
    import types as _types
    class DNode:
        def __init__(self, name, role, children=()):
            self.name, self._role, self._children = name, role, list(children)
        def getRoleName(self):
            return self._role
        def getState(self):
            return _types.SimpleNamespace(
                contains=lambda state: state == 99)
        def __iter__(self):
            return iter(self._children)
    dialog = DNode("Save As", "dialog")
    app = DNode("App", "application", [dialog])
    original_frame_is_active = v8._frame_is_active
    try:
        # Tri-state contract (pass-86): None = state unreadable,
        # which keeps the first-match fallback this scenario models.
        v8._frame_is_active = lambda frame: None
        assert v8._atspi_focused_frame([app], "Save As") is dialog
    finally:
        v8._frame_is_active = original_frame_is_active
    # (N9) A defunct node does not abort the AT-SPI search.
    class Ext:
        x, y, width, height = 10, 10, 20, 10
    class Comp:
        def getExtents(self, coord):
            return Ext()
    class Node:
        def __init__(self, name, role, children=()):
            self.name, self._role, self._children = name, role, list(children)
        def getRoleName(self):
            if self._role == "defunct":
                raise RuntimeError("defunct accessible")
            return self._role
        def getState(self):
            return _types.SimpleNamespace(contains=lambda state: True)
        def queryComponent(self):
            return Comp()
        def __iter__(self):
            return iter(self._children)
    frame = Node("Editor", "frame",
                 [Node("Ghost", "defunct"), Node("Save", "push button")])
    fake_atspi = _types.SimpleNamespace(
        STATE_VISIBLE=1, STATE_ENABLED=2,
        Registry=_types.SimpleNamespace(getDesktop=lambda n: [[frame]]))
    dispatches = []
    def fake_call26(argv, **kw):
        joined = " ".join(str(a) for a in argv)
        if "getactivewindow" in joined:
            return "123"
        if "getwindowname" in joined:
            return "Editor"
        dispatches.append([str(a) for a in argv])
        return ""
    saved_module = sys.modules.get("pyatspi")
    originals = (v8._call, v8._atspi_module, v8._atspi_tried,
                 v8.ATSPI_AVAILABLE, v8._screen_size)
    try:
        sys.modules["pyatspi"] = fake_atspi
        v8._call = fake_call26
        v8._atspi_module, v8._atspi_tried = None, False
        v8.ATSPI_AVAILABLE = True
        v8._screen_size = lambda: (1920, 1080)
        with environment(CU_DRY_RUN=None):
            with v8._within(10):
                ok, via, detail = v8.rung_atspi({"text": "Save"}, "native")
    finally:
        if saved_module is not None:
            sys.modules["pyatspi"] = saved_module
        else:
            sys.modules.pop("pyatspi", None)
        (v8._call, v8._atspi_module, v8._atspi_tried,
         v8.ATSPI_AVAILABLE, v8._screen_size) = originals
    assert ok and dispatches, (ok, detail, dispatches)
    # (N10) Dry-run deletion keys act on the selection like real keys.
    token = v8._dry_state.set({"calls": 0, "slept": True, "text": "",
                               "field": "OLD", "clip": "", "selected": False})
    try:
        with environment(CU_DRY_RUN="1"):
            v8.run_tool(json.dumps(dict(
                tool="kbd_agent", action="keys",
                args={"keys": ["ctrl+a", "BackSpace"]})))
        assert v8._dry_state.get()["field"] == "", v8._dry_state.get()
    finally:
        v8._dry_state.reset(token)
    # (N2) The lane errors when a full-URL hint matches no page.
    lane_path = str(Path(__file__).resolve().parent.parent / "src" / "cua-lane")
    if os.path.isfile(lane_path):
        child = subprocess.run(
            [sys.executable, "-c", r'''
import io, json, os, sys, types
lane_file = str(Path(__file__).resolve().parent.parent / "src" / "cua-lane")
with open(lane_file) as stream:
    code = stream.read()
targets = [{"type": "page", "url": "https://other.test/",
            "webSocketDebuggerUrl": "ws://B"}]
class Resp:
    def __enter__(self):
        return io.BytesIO(json.dumps(targets).encode())
    def __exit__(self, *a):
        return False
import urllib.request
urllib.request.urlopen = lambda *a, **k: Resp()
sys.modules["websocket"] = types.SimpleNamespace(
    create_connection=lambda url, timeout=0: None)
os.environ["CUA_CDP_HINT"] = "https://gone.test/page"
import importlib.util
spec = importlib.util.spec_from_loader("cualane", loader=None)
lane = importlib.util.module_from_spec(spec)
lane.__file__ = lane_file
exec(compile(code, lane_file, "exec"), lane.__dict__)
try:
    lane._CDP()
    print("NO_ERROR")
except RuntimeError as exc:
    print("RAISED:", exc)
'''], capture_output=True, text=True, timeout=60)
        assert "cdp hint matched no page" in child.stdout, child.stdout


def test_pass26_loop_fixes():
    # (N6) An async page change invalidates the clipboard-path pagetext
    # cache via the visual freshness key.
    progress = os.path.join(tempfile.mkdtemp(prefix="cu-trio2-"), "p.jsonl")
    body = {"text": "BODY A"}
    seen = []
    plan = ["observe", "pagetext", "pagetext", "done"]
    def fake_chat(task, steps, last_result, note):
        tool = plan[len(seen)]
        seen.append(last_result)
        if len(seen) == 3:
            body["text"] = "BODY B"  # async flip before the second read
            return json.dumps(dict(tool=tool, args={"vary": 1},
                                   result="async checked")), "stop", 0.0
        return json.dumps(dict(tool=tool, result="async checked")), "stop", 0.0
    def fake_tool(raw):
        tool = json.loads(raw)["tool"]
        if tool == "observe":
            return ("Window title: Same\nWindow id: 100\n"
                    "Window class: firefox\nSurface: browser\n"
                    "URL: https://ff.test/")
        return body["text"]
    grabs = {"n": 0}
    def fake_grab():
        grabs["n"] += 1
        # Call 1: pagetext step 1's serve-check grab (page A; the entry
        # is stored under this serve-time key). Later calls: page B
        # after the async flip.
        return (b"frame-A" if grabs["n"] <= 1 else b"frame-B") * 32
    originals = (v8.call_qwen, v8.run_tool, v8._focus_verify,
                 v8._latest_shot, v8._tab_url, v8._grab)
    try:
        v8.call_qwen, v8.run_tool = fake_chat, fake_tool
        v8._focus_verify = lambda fresh=False: dict(title="Same", window_id="100",
                                        window_class="firefox",
                                        surface="browser")
        v8._latest_shot = lambda: None
        v8._tab_url = lambda: ""
        v8._grab = fake_grab
        with environment(CU_PROGRESS_LOG=progress, CUA_BIN=None):
            with contextlib.redirect_stdout(io.StringIO()):
                v8.run_loop("async pagetext", max_iters=6)
    finally:
        (v8.call_qwen, v8.run_tool, v8._focus_verify,
         v8._latest_shot, v8._tab_url, v8._grab) = originals
    reads = [entry for entry in seen if "BODY" in entry]
    assert reads == ["BODY A", "BODY B"], \
        "async change must invalidate the cached text: %s" % reads
    # (N3) A window switch drops the CDP hint pin.
    progress = os.path.join(tempfile.mkdtemp(prefix="cu-trio2-"), "p.jsonl")
    focus = {"current": dict(title="A", window_id="100",
                             window_class="chromium", surface="browser_cdp")}
    seen3 = []
    plan3 = ["pagetext", "pagetext", "done"]
    def fake_chat3(task, steps, last_result, note):
        tool = plan3[len(seen3)]
        seen3.append(last_result)
        if len(seen3) == 2:
            focus["current"] = dict(title="B", window_id="200",
                                    window_class="chromium",
                                    surface="browser_cdp")
            return json.dumps(dict(tool=tool, args={"vary": 1},
                                   result="switch checked")), "stop", 0.0
        return json.dumps(dict(tool=tool, result="switch checked")), "stop", 0.0
    originals = (v8.call_qwen, v8.run_tool, v8._focus_verify,
                 v8._latest_shot, v8._tab_url, v8._cdp_text, v8._last_cdp_url)
    try:
        v8.call_qwen = fake_chat3
        v8.run_tool = lambda raw: "PAGE"
        v8._focus_verify = lambda fresh=False: focus["current"]
        v8._latest_shot = lambda: None
        v8._tab_url = lambda: ""
        def cdp_read():
            v8._last_cdp_url = "https://a.test/"
            return "PAGE"
        v8._cdp_text = cdp_read
        v8._last_cdp_url = ""
        with environment(CU_PROGRESS_LOG=progress, CUA_BIN=None):
            with contextlib.redirect_stdout(io.StringIO()):
                v8.run_loop("pin switch", max_iters=5)
        assert v8._last_cdp_url == "", \
            "switch must drop the pin: %s" % v8._last_cdp_url
    finally:
        (v8.call_qwen, v8.run_tool, v8._focus_verify, v8._latest_shot,
         v8._tab_url, v8._cdp_text, v8._last_cdp_url) = originals


def test_pass27_fixes():
    # Codex pass-27 verification fixes.
    # (N5) A runner-level TimeoutExpired from the verification grab
    # degrades to the shot file; the typing receipt survives.
    record = []
    originals = (v8.RUNNER, v8._grab, v8._latest_shot)
    try:
        v8.RUNNER = focus_runner(record)
        def expired_grab():
            raise subprocess.TimeoutExpired(["cua", "shot"], 5)
        v8._grab = expired_grab
        v8._latest_shot = lambda: b"frame-before" * 32
        with environment(CU_DRY_RUN=None, YDOTOOL_SOCKET="/nonexistent.sock"):
            result = json.loads(v8.kbd_agent(
                "type_verify", {"text": "hello", "engine": "xdotool"}))
        assert result["ok"] is True, result
    finally:
        (v8.RUNNER, v8._grab, v8._latest_shot) = originals
    # (N7) A runner-level TimeoutExpired on clipboard SETUP also
    # degrades to the xdotool typing path.
    record7 = []
    def runner7(argv, timeout_s, env_extra=None):
        record7.append([str(a) for a in argv])
        return focus_runner([])(argv, timeout_s, env_extra)
    def cua_slow_setup(*a, **k):
        if a[:2] == ("clip", "set"):
            raise subprocess.TimeoutExpired(["cua", "clip"], 5)
        return {"ok": True, "text": ""}
    originals = (v8.RUNNER, v8._cua)
    try:
        v8.RUNNER, v8._cua = runner7, cua_slow_setup
        with environment(CU_DRY_RUN=None, YDOTOOL_SOCKET="/nonexistent.sock"):
            result = json.loads(v8.kbd_agent("type_verify", {
                "text": "fallback text", "engine": "auto",
                "safe_clipboard": True}))
        assert result["ok"] is True and result["via"] == "xdotool", result
    finally:
        (v8.RUNNER, v8._cua) = originals
    # (N9) Children collected before a defunct sibling are still
    # traversed.
    import types as _types
    class Ext:
        x, y, width, height = 10, 10, 20, 10
    class Comp:
        def getExtents(self, coord):
            return Ext()
    class FlakyChildren(list):
        def __iter__(self):
            yield self[0]
            raise RuntimeError("defunct child")
    class Node:
        def __init__(self, name, role, children=()):
            self.name, self._role = name, role
            self._children = FlakyChildren(children) if children else []
        def getRoleName(self):
            return self._role
        def getState(self):
            return _types.SimpleNamespace(contains=lambda state: True)
        def queryComponent(self):
            return Comp()
        def __iter__(self):
            return iter(self._children)
    frame = Node("Editor", "frame", [Node("Save", "push button")])
    fake_atspi = _types.SimpleNamespace(
        STATE_VISIBLE=1, STATE_ENABLED=2,
        Registry=_types.SimpleNamespace(getDesktop=lambda n: [[frame]]))
    dispatches = []
    def fake_call27(argv, **kw):
        joined = " ".join(str(a) for a in argv)
        if "getactivewindow" in joined:
            return "123"
        if "getwindowname" in joined:
            return "Editor"
        dispatches.append([str(a) for a in argv])
        return ""
    saved_module = sys.modules.get("pyatspi")
    originals = (v8._call, v8._atspi_module, v8._atspi_tried,
                 v8.ATSPI_AVAILABLE, v8._screen_size)
    try:
        sys.modules["pyatspi"] = fake_atspi
        v8._call = fake_call27
        v8._atspi_module, v8._atspi_tried = None, False
        v8.ATSPI_AVAILABLE = True
        v8._screen_size = lambda: (1920, 1080)
        with environment(CU_DRY_RUN=None):
            with v8._within(10):
                ok, via, detail = v8.rung_atspi({"text": "Save"}, "native")
    finally:
        if saved_module is not None:
            sys.modules["pyatspi"] = saved_module
        else:
            sys.modules.pop("pyatspi", None)
        (v8._call, v8._atspi_module, v8._atspi_tried,
         v8.ATSPI_AVAILABLE, v8._screen_size) = originals
    assert ok and dispatches, (ok, detail, dispatches)
    # (N2) A full-URL hint does not select a prefix-sibling page.
    lane_path = str(Path(__file__).resolve().parent.parent / "src" / "cua-lane")
    if os.path.isfile(lane_path):
        child = subprocess.run(
            [sys.executable, "-c", r'''
import io, json, os, sys, types
lane_file = str(Path(__file__).resolve().parent.parent / "src" / "cua-lane")
with open(lane_file) as stream:
    code = stream.read()
targets = [{"type": "page", "url": "https://gone.test/page2",
            "webSocketDebuggerUrl": "ws://B"}]
class Resp:
    def __enter__(self):
        return io.BytesIO(json.dumps(targets).encode())
    def __exit__(self, *a):
        return False
import urllib.request
urllib.request.urlopen = lambda *a, **k: Resp()
picked = []
sys.modules["websocket"] = types.SimpleNamespace(
    create_connection=lambda url, timeout=0: picked.append(url))
os.environ["CUA_CDP_HINT"] = "https://gone.test/page"
import importlib.util
spec = importlib.util.spec_from_loader("cualane", loader=None)
lane = importlib.util.module_from_spec(spec)
lane.__file__ = lane_file
exec(compile(code, lane_file, "exec"), lane.__dict__)
try:
    lane._CDP()
    print("PICKED", picked)
except RuntimeError as exc:
    print("RAISED:", exc)
'''], capture_output=True, text=True, timeout=60)
        assert "cdp hint matched no page" in child.stdout, child.stdout


def test_pass27_loop_fixes():
    # (N6) A failing freshness grab never prevents the pagetext read.
    progress = os.path.join(tempfile.mkdtemp(prefix="cu-trio2-"), "p.jsonl")
    seen = []
    plan = ["observe", "pagetext", "done"]
    def fake_chat(task, steps, last_result, note):
        tool = plan[len(seen)]
        seen.append(last_result)
        return json.dumps(dict(tool=tool, result="grabfail checked")), "stop", 0.0
    def fake_tool(raw):
        tool = json.loads(raw)["tool"]
        if tool == "observe":
            return ("Window title: Same\nWindow id: 100\n"
                    "Window class: firefox\nSurface: browser\n"
                    "URL: https://ff.test/")
        return "BODY OK"
    def bad_grab():
        raise OSError("screenshot device busy")
    originals = (v8.call_qwen, v8.run_tool, v8._focus_verify,
                 v8._latest_shot, v8._tab_url, v8._grab)
    try:
        v8.call_qwen, v8.run_tool = fake_chat, fake_tool
        v8._focus_verify = lambda fresh=False: dict(title="Same", window_id="100",
                                        window_class="firefox",
                                        surface="browser")
        v8._latest_shot = lambda: None
        v8._tab_url = lambda: ""
        v8._grab = bad_grab
        with environment(CU_PROGRESS_LOG=progress, CUA_BIN=None):
            with contextlib.redirect_stdout(io.StringIO()):
                result = v8.run_loop("grab failure", max_iters=5)
    finally:
        (v8.call_qwen, v8.run_tool, v8._focus_verify,
         v8._latest_shot, v8._tab_url, v8._grab) = originals
    assert "BODY OK" in seen[-1], seen


def test_pass28_loop_fixes():
    # (N3) A targeted click that switches windows INSIDE the dispatch
    # must not carry the previous window's CDP pin.
    progress = os.path.join(tempfile.mkdtemp(prefix="cu-trio2-"), "p.jsonl")
    active = {"wid": "100"}
    titles = {"100": "Page A - Chromium", "200": "Page B - Chromium"}
    hints = []
    def runner(argv, timeout_s, env_extra=None):
        argv = [str(a) for a in argv]
        joined = " ".join(argv)
        if "getactivewindow" in joined:
            return (0, active["wid"] + "\n", "")
        if "getwindowname" in joined:
            return (0, titles[argv[-1]] + "\n", "")
        if "windowactivate" in joined:
            active["wid"] = argv[-1]
            return (0, "", "")
        if "xprop" in joined:
            return (0, 'WM_CLASS(STRING) = "chromium", "Chromium"\n', "")
        if "cdp-click" in joined:
            hints.append((env_extra or {}).get("CUA_CDP_HINT"))
            return (0, '{"ok": true}', "")
        if "shot" in joined:
            return (0, '{"ok": true}', "")
        return (0, "{}", "")
    plan = ["pagetext", "mouse", "done"]
    seen = []
    def fake_chat(task, steps, last_result, note):
        tool = plan[len(seen)]
        seen.append(last_result)
        if tool == "pagetext":
            return json.dumps({"tool": "pagetext"}), "stop", 0.0
        if tool == "mouse":
            return json.dumps({"tool": "mouse_agent", "action": "click",
                               "args": {"match": "200",
                                        "selector": "#submit",
                                        "vary": len(seen)}}), "stop", 0.0
        return json.dumps(dict(tool="done", result="switched")), "stop", 0.0
    def fake_tool(raw):
        request = json.loads(raw)
        if request["tool"] == "pagetext":
            # Simulate the CDP read that pins the executor to page A.
            v8._last_cdp_url = "https://a.test/"
            return "PAGE A TEXT"
        if request["tool"] == "mouse_agent":
            return v8.mouse_agent(request["action"], request["args"])
        return "{}"
    import socket as _socket
    real_connect = _socket.create_connection
    class _DummySock:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    originals = (v8.call_qwen, v8.run_tool, v8.RUNNER, v8._latest_shot,
                 v8._tab_url)
    try:
        v8.call_qwen, v8.run_tool = fake_chat, fake_tool
        v8.RUNNER = runner
        v8._latest_shot = lambda: b"frame" * 32
        # Final semantics (pass-36): dispatch-class CDP commands
        # require a VERIFIED identity (a read pin or target id). The
        # metadata seed serves read-class commands only, because it
        # describes the automation tab, not necessarily the focused
        # window. After the switch there is no verified identity for
        # B, so the CDP rung must not dispatch at all — and A's pin
        # must never ride into the new window.
        v8._tab_url = lambda: "https://b.test/"
        _socket.create_connection = lambda *a, **k: _DummySock()
        with environment(CU_PROGRESS_LOG=progress, CU_DRY_RUN=None):
            with contextlib.redirect_stdout(io.StringIO()):
                v8.run_loop("targeted switch", max_iters=5)
    finally:
        _socket.create_connection = real_connect
        (v8.call_qwen, v8.run_tool, v8.RUNNER, v8._latest_shot,
         v8._tab_url) = originals
    assert hints == [], hints
    assert v8._last_cdp_url == "" and v8._last_cdp_target_id == ""
    # (N6 mirror) Text read while the page flipped mid-read is not
    # cached under either frame: a later visit to the pre-read page
    # re-reads instead of serving the flipped page's text.
    progress2 = os.path.join(tempfile.mkdtemp(prefix="cu-trio2-"), "p.jsonl")
    frames = [b"frame-A", b"frame-B", b"frame-A", b"frame-A"]
    grab_calls = {"n": 0}
    def fake_grab():
        grab_calls["n"] += 1
        idx = min(grab_calls["n"] - 1, len(frames) - 1)
        return frames[idx] * 32
    reads = []
    page = {"current": "B"}
    plan2 = ["observe", "pagetext", "pagetext", "done"]
    seen2 = []
    def fake_chat2(task, steps, last_result, note):
        tool = plan2[len(seen2)]
        seen2.append(last_result)
        return json.dumps(dict(tool=tool, result="mirror checked")), "stop", 0.0
    def fake_tool2(raw):
        tool = json.loads(raw)["tool"]
        if tool == "observe":
            return ("Window title: Same\nWindow id: 100\n"
                    "Window class: firefox\nSurface: browser\n"
                    "URL: https://ff.test/")
        body = "BODY " + page["current"]
        reads.append(body)
        page["current"] = "A"  # the page settles back to A after read 1
        return body
    originals = (v8.call_qwen, v8.run_tool, v8._focus_verify,
                 v8._latest_shot, v8._tab_url, v8._grab)
    try:
        v8.call_qwen, v8.run_tool = fake_chat2, fake_tool2
        v8._focus_verify = lambda fresh=False: dict(title="Same", window_id="100",
                                        window_class="firefox",
                                        surface="browser")
        v8._latest_shot = lambda: None
        v8._tab_url = lambda: ""
        v8._grab = fake_grab
        with environment(CU_PROGRESS_LOG=progress2, CUA_BIN=None):
            with contextlib.redirect_stdout(io.StringIO()):
                v8.run_loop("mirror race", max_iters=6)
    finally:
        (v8.call_qwen, v8.run_tool, v8._focus_verify,
         v8._latest_shot, v8._tab_url, v8._grab) = originals
    assert reads == ["BODY B", "BODY A"], reads


def test_pass29_loop_fixes():
    # (N6 final) Pagetext is never served from cache: two identical
    # pagetext steps both perform a real read, and neither result is
    # marked cached. (The A->B->A flip during a non-atomic clipboard
    # read defeats any frame-bracketed keying — Codex pass-29.)
    progress = os.path.join(tempfile.mkdtemp(prefix="cu-trio2-"), "p.jsonl")
    tool_calls = []
    plan = ["observe", "pagetext", "pagetext", "done"]
    seen = []
    def fake_chat(task, steps, last_result, note):
        tool = plan[len(seen)]
        seen.append(last_result)
        return json.dumps(dict(tool=tool, result="nocache checked")), "stop", 0.0
    def fake_tool(raw):
        tool = json.loads(raw)["tool"]
        tool_calls.append(tool)
        if tool == "observe":
            return ("Window title: Same\nWindow id: 100\n"
                    "Window class: firefox\nSurface: browser\n"
                    "URL: https://ff.test/")
        return "FRESH TEXT"
    originals = (v8.call_qwen, v8.run_tool, v8._focus_verify,
                 v8._latest_shot, v8._tab_url)
    try:
        v8.call_qwen, v8.run_tool = fake_chat, fake_tool
        v8._focus_verify = lambda fresh=False: dict(title="Same", window_id="100",
                                        window_class="firefox",
                                        surface="browser")
        v8._latest_shot = lambda: None
        v8._tab_url = lambda: ""
        with environment(CU_PROGRESS_LOG=progress, CUA_BIN=None):
            with contextlib.redirect_stdout(io.StringIO()):
                v8.run_loop("no pagetext cache", max_iters=6)
    finally:
        (v8.call_qwen, v8.run_tool, v8._focus_verify,
         v8._latest_shot, v8._tab_url) = originals
    assert tool_calls.count("pagetext") == 2, tool_calls
    assert all("[cached:true]" not in (entry or "") for entry in seen), seen


def test_pass30_fixes():
    # Codex pass-30: (1) a failed settle read propagates its ERROR
    # instead of returning the earlier text as settled; (2) the
    # clipboard pagetext path proves the copy landed (sentinel seed).
    original_once = v8._pagetext_once
    try:
        sequence = iter(["BODY A", "ERROR: pagetext requires a browser surface"])
        v8._pagetext_once = lambda: next(sequence)
        out = v8.run_tool(json.dumps({"tool": "pagetext"}))
        assert out.startswith("ERROR"), out
        sequence = iter(["BODY A", "BODY B", "ERROR: copy failed"])
        v8._pagetext_once = lambda: next(sequence)
        out = v8.run_tool(json.dumps({"tool": "pagetext"}))
        assert out.startswith("ERROR"), out
        sequence = iter(["BODY A", "BODY A"])
        v8._pagetext_once = lambda: next(sequence)
        out = v8.run_tool(json.dumps({"tool": "pagetext"}))
        assert out == "BODY A [settled:2]", out
    finally:
        v8._pagetext_once = original_once
    clipboard = {"value": "USER CLIPBOARD"}
    def make_cua(copy_works):
        def fake_cua(*a):
            if a[:2] == ("clip", "set"):
                clipboard["value"] = a[2]
                return {"ok": True}
            if a[:2] == ("clip", "get"):
                return {"text": clipboard["value"]}
            if a == ("key", "ctrl+c") and copy_works:
                clipboard["value"] = "PAGE TEXT"
            return {"ok": True}
        return fake_cua
    originals = (v8._cua, v8._focus_verify)
    try:
        v8._focus_verify = lambda fresh=False: dict(title="FF", window_id="1",
                                        window_class="firefox",
                                        surface="browser")
        v8._cua = make_cua(copy_works=False)
        out = v8._pagetext_once()
        assert out.startswith("ERROR"), out
        v8._cua = make_cua(copy_works=True)
        out = v8._pagetext_once()
        assert out == "PAGE TEXT", out
        # A FOREIGN caller's sentinel in the clipboard is rejected too
        # (pass-31 cross-talk hardening).
        def foreign_cua(*a):
            if a[:2] == ("clip", "set"):
                clipboard["value"] = a[2]
                return {"ok": True}
            if a[:2] == ("clip", "get"):
                return {"text": "CU-SENTINEL-999-123"}
            return {"ok": True}
        v8._cua = foreign_cua
        out = v8._pagetext_once()
        assert out.startswith("ERROR"), out
    finally:
        (v8._cua, v8._focus_verify) = originals


def test_pass32_fixes():
    # Codex pass-32 fresh-scan fixes.
    # (1) The shell tool kills its whole process group on timeout:
    # a backgrounded descendant must not survive to act afterwards.
    marker = os.path.join(tempfile.mkdtemp(prefix="cu-trio2-"), "marker")
    cmd = "(sleep 1.0; touch %s) & wait" % marker
    with environment(CU_DRY_RUN=None, CU_TIMEOUT_SHELL="0.3"):
        out = v8.run_tool(json.dumps({"tool": "shell", "cmd": cmd}))
    assert "TIMEOUT" in out, out
    time.sleep(1.4)
    assert not os.path.exists(marker), "shell descendant survived timeout"
    # (2) The vision rung never grounds on the stale shot file.
    vision_calls = []
    originals = (v8._native_grab, v8._latest_shot, v8.vision_text)
    try:
        v8._native_grab = lambda: None
        v8._latest_shot = lambda: b"stale-frame" * 32
        def fake_vision(shot, prompt):
            vision_calls.append(True)
            return '{"x": 500, "y": 500}'
        v8.vision_text = fake_vision
        ok, via, detail = v8.rung_vision({"text": "Save"}, "native")
    finally:
        (v8._native_grab, v8._latest_shot, v8.vision_text) = originals
    assert not ok and "native_capture_unavailable" in detail, (ok, detail)
    assert not vision_calls, "vision model must not see a stale frame"
    # (3) Pagetext settle reads re-probe focus freshly: a mid-settle
    # switch to a native app surfaces as an ERROR, not settled text.
    probes = {"n": 0}
    def switching_focus(fresh=False):
        probes["n"] += 1
        if probes["n"] == 1:
            return dict(title="Page", window_id="100",
                        window_class="chromium", surface="browser_cdp")
        return dict(title="Editor", window_id="200",
                    window_class="gnome-text-editor", surface="native")
    originals = (v8._focus_verify, v8._cdp_text)
    try:
        v8._focus_verify = switching_focus
        v8._cdp_text = lambda: "BROWSER BODY"
        out = v8.run_tool(json.dumps({"tool": "pagetext"}))
    finally:
        (v8._focus_verify, v8._cdp_text) = originals
    assert out.startswith("ERROR"), out
    assert probes["n"] >= 2, probes
    # (4) Cold-start CDP commands seed the hint from the automation
    # tab metadata; an established read pin wins over the metadata.
    recorded = []
    def recording_runner(argv, timeout_s, env_extra=None):
        recorded.append(env_extra)
        return (0, '{"text": "x"}', "")
    originals = (v8.RUNNER, v8._tab_url, v8._last_cdp_url)
    try:
        v8.RUNNER = recording_runner
        v8._tab_url = lambda: "https://seed.test/page"
        v8._last_cdp_url = ""
        with environment(CU_DRY_RUN=None):
            with v8._agent_session():
                v8._cua("cdp-text")
                assert recorded[-1] == {"CUA_CDP_HINT": "https://seed.test/page"}, recorded
                v8._last_cdp_url = "https://pinned.test/"
                v8._cua("cdp-text")
                assert recorded[-1] == {"CUA_CDP_HINT": "https://pinned.test/"}, recorded
    finally:
        (v8.RUNNER, v8._tab_url, v8._last_cdp_url) = originals


def test_pass33_fixes():
    # (P2-2 final) Dispatch-class CDP commands refuse to run with no
    # page identity at all; read-class commands may still proceed.
    calls = []
    def recording_runner(argv, timeout_s, env_extra=None):
        calls.append([str(a) for a in argv])
        return (0, '{"text": "x"}', "")
    originals = (v8.RUNNER, v8._tab_url, v8._last_cdp_url)
    try:
        v8.RUNNER = recording_runner
        v8._tab_url = lambda: ""
        v8._last_cdp_url = ""
        with environment(CU_DRY_RUN=None):
            with v8._agent_session():
                try:
                    v8._cua("cdp-click", "#submit")
                    raise AssertionError("identity-less dispatch must refuse")
                except RuntimeError as exc:
                    assert "cdp identity unavailable" in str(exc), exc
                assert not calls, calls
                v8._cua("cdp-text")  # read-class: allowed, establishes pin
                assert calls, "read-class command must proceed"
        # An EMPTY CUA_CDP_HINT env value is no hint (pass-34): it
        # must neither suppress the identity guard nor the pin.
        calls.clear()
        with environment(CU_DRY_RUN=None, CUA_CDP_HINT=""):
            with v8._agent_session():
                try:
                    v8._cua("cdp-click", "#submit")
                    raise AssertionError("empty env hint must not bypass the guard")
                except RuntimeError as exc:
                    assert "cdp identity unavailable" in str(exc), exc
                assert not calls, calls
                v8._last_cdp_url = "https://pinned.test/"
                v8._cua("cdp-click", "#submit")
                assert calls, "pin must be injected despite the empty env value"
    finally:
        (v8.RUNNER, v8._tab_url, v8._last_cdp_url) = originals
    # The lane matches hints exactly for every scheme: an about:blank
    # hint does not fall through to a heuristic background target.
    lane_path = str(Path(__file__).resolve().parent.parent / "src" / "cua-lane")
    if os.path.isfile(lane_path):
        child = subprocess.run(
            [sys.executable, "-c", r'''
import io, json, os, sys, types
lane_file = str(Path(__file__).resolve().parent.parent / "src" / "cua-lane")
with open(lane_file) as stream:
    code = stream.read()
targets = [{"type": "page", "url": "https://background.test/",
            "webSocketDebuggerUrl": "ws://B"}]
class Resp:
    def __enter__(self):
        return io.BytesIO(json.dumps(targets).encode())
    def __exit__(self, *a):
        return False
import urllib.request
urllib.request.urlopen = lambda *a, **k: Resp()
picked = []
sys.modules["websocket"] = types.SimpleNamespace(
    create_connection=lambda url, timeout=0: picked.append(url))
os.environ["CUA_CDP_HINT"] = "about:blank"
import importlib.util
spec = importlib.util.spec_from_loader("cualane", loader=None)
lane = importlib.util.module_from_spec(spec)
lane.__file__ = lane_file
exec(compile(code, lane_file, "exec"), lane.__dict__)
try:
    lane._CDP()
    print("PICKED", picked)
except RuntimeError as exc:
    print("RAISED:", exc)
'''], capture_output=True, text=True, timeout=60)
        assert "cdp hint matched no page" in child.stdout, child.stdout


def test_pass36_fixes():
    # Codex pass-36 fresh-scan fixes.
    # (F1/F2) A cdp-text read pins the target ID; dispatches carry it.
    recorded = []
    def runner_id(argv, timeout_s, env_extra=None):
        recorded.append(env_extra)
        return (0, '{"text": "BODY", "cdp_url": "https://a.test/", '
                   '"cdp_target_id": "T123"}', "")
    originals = (v8.RUNNER, v8._tab_url, v8._last_cdp_url,
                 v8._last_cdp_target_id)
    try:
        v8.RUNNER = runner_id
        v8._tab_url = lambda: ""
        v8._last_cdp_url, v8._last_cdp_target_id = "", ""
        with environment(CU_DRY_RUN=None):
            with v8._agent_session():
                text = v8._cdp_text()
                assert text == "BODY", text
                assert v8._last_cdp_target_id == "T123"
                recorded.clear()
                v8._cua("cdp-click", "#go")
                env = recorded[-1] or {}
                assert env.get("CUA_CDP_TARGET_ID") == "T123", env
                assert env.get("CUA_CDP_HINT") == "https://a.test/", env
    finally:
        (v8.RUNNER, v8._tab_url, v8._last_cdp_url,
         v8._last_cdp_target_id) = originals
    # (F3) A hint-miss clears both pins so the next read re-pins.
    def runner_miss(argv, timeout_s, env_extra=None):
        return (1, "", "cdp hint matched no page: https://gone.test/")
    originals = (v8.RUNNER, v8._tab_url, v8._last_cdp_url,
                 v8._last_cdp_target_id)
    try:
        v8.RUNNER = runner_miss
        v8._tab_url = lambda: ""
        v8._last_cdp_url = "https://gone.test/"
        v8._last_cdp_target_id = "T999"
        with environment(CU_DRY_RUN=None):
            with v8._agent_session():
                try:
                    v8._cua("cdp-text")
                    raise AssertionError("hint miss must raise")
                except RuntimeError:
                    pass
        assert v8._last_cdp_url == "" and v8._last_cdp_target_id == ""
    finally:
        (v8.RUNNER, v8._tab_url, v8._last_cdp_url,
         v8._last_cdp_target_id) = originals
    # (F1) Dispatch-class commands never use the metadata seed.
    calls = []
    def runner_rec(argv, timeout_s, env_extra=None):
        calls.append(env_extra)
        return (0, '{"text": "x"}', "")
    originals = (v8.RUNNER, v8._tab_url, v8._last_cdp_url,
                 v8._last_cdp_target_id, v8._seed_retired)
    try:
        v8.RUNNER = runner_rec
        v8._tab_url = lambda: "https://automation.test/"
        v8._last_cdp_url, v8._last_cdp_target_id = "", ""
        v8._seed_retired = False  # this scenario models a fresh run
        with environment(CU_DRY_RUN=None):
            with v8._agent_session():
                try:
                    v8._cua("cdp-click", "#go")
                    raise AssertionError("metadata-only dispatch must refuse")
                except RuntimeError as exc:
                    assert "cdp identity unavailable" in str(exc), exc
                assert not calls, calls
                v8._cua("cdp-text")
                assert (calls[-1] or {}).get("CUA_CDP_HINT") == \
                    "https://automation.test/", calls
    finally:
        (v8.RUNNER, v8._tab_url, v8._last_cdp_url,
         v8._last_cdp_target_id, v8._seed_retired) = originals
    # (F4) OCR refuses an arbitrarily stale shot file when live
    # capture fails.
    import types as _types
    root = tempfile.mkdtemp(prefix="cu-trio2-")
    stale = os.path.join(root, "shot.jpg")
    with open(stale, "wb") as stream:
        stream.write(b"\xff\xd8\xff stale")
    old = time.time() - 3600
    os.utime(stale, (old, old))
    broken_mss = _types.SimpleNamespace(
        mss=lambda: (_ for _ in ()).throw(RuntimeError("no display")))
    saved_mss = sys.modules.get("mss")
    original_call = v8._call
    try:
        sys.modules["mss"] = broken_mss
        def no_shot(argv, **kw):
            raise RuntimeError("shot unavailable")
        v8._call = no_shot
        with environment(CU_DRY_RUN=None, CUA_ROOT=root):
            with v8._agent_session():
                out = v8.ocr_tool({})
        assert "NO_SCREENSHOT" in out, out
    finally:
        if saved_mss is not None:
            sys.modules["mss"] = saved_mss
        else:
            sys.modules.pop("mss", None)
        v8._call = original_call
    # Lane: CUA_CDP_TARGET_ID selects exactly that target; a missing
    # ID raises the hint-miss error.
    lane_path = str(Path(__file__).resolve().parent.parent / "src" / "cua-lane")
    if os.path.isfile(lane_path):
        child = subprocess.run(
            [sys.executable, "-c", r'''
import io, json, os, sys, types
lane_file = str(Path(__file__).resolve().parent.parent / "src" / "cua-lane")
with open(lane_file) as stream:
    code = stream.read()
targets = [{"type": "page", "id": "AAA", "url": "https://same.test/",
            "webSocketDebuggerUrl": "ws://A"},
           {"type": "page", "id": "BBB", "url": "https://same.test/",
            "webSocketDebuggerUrl": "ws://B"}]
class Resp:
    def __enter__(self):
        return io.BytesIO(json.dumps(targets).encode())
    def __exit__(self, *a):
        return False
import urllib.request
urllib.request.urlopen = lambda *a, **k: Resp()
picked = []
sys.modules["websocket"] = types.SimpleNamespace(
    create_connection=lambda url, timeout=0: picked.append(url))
import importlib.util
spec = importlib.util.spec_from_loader("cualane", loader=None)
lane = importlib.util.module_from_spec(spec)
lane.__file__ = lane_file
exec(compile(code, lane_file, "exec"), lane.__dict__)
os.environ["CUA_CDP_TARGET_ID"] = "BBB"
os.environ["CUA_CDP_HINT"] = "https://same.test/"
lane._CDP()
print("PICKED", picked)
os.environ["CUA_CDP_TARGET_ID"] = "ZZZ"
try:
    lane._CDP()
    print("PICKED2", picked)
except RuntimeError as exc:
    print("RAISED:", exc)
'''], capture_output=True, text=True, timeout=60)
        assert "ws://B" in child.stdout, child.stdout
        assert "cdp hint matched no page" in child.stdout, child.stdout


def test_pass37_fixes():
    # Codex pass-37 fixes.
    # (F1) The observe tool attaches the automation metadata URL only
    # on the browser_cdp surface.
    root = tempfile.mkdtemp(prefix="cu-trio2-")
    with open(os.path.join(root, "state.json"), "w") as stream:
        json.dump({"automation_tab": {"url": "https://automation.test/",
                                      "window_id": 100}}, stream)
    original_focus = v8._focus_verify
    try:
        with environment(CUA_ROOT=root, CU_DRY_RUN=None):
            v8._focus_verify = lambda fresh=False: dict(
                title="Other Chromium", window_id="200",
                window_class="chromium", surface="browser")
            out = v8.run_tool(json.dumps({"tool": "observe"}))
            assert "automation.test" not in out, out
            v8._focus_verify = lambda fresh=False: dict(
                title="Automation", window_id="100",
                window_class="chromium", surface="browser_cdp")
            out = v8.run_tool(json.dumps({"tool": "observe"}))
            assert "https://automation.test/" in out, out
    finally:
        v8._focus_verify = original_focus
    # (F3) A missed metadata seed is poisoned; the recovery read goes
    # unseeded and can re-pin.
    recorded = []
    state = {"fail_seed": True}
    def runner37(argv, timeout_s, env_extra=None):
        recorded.append(env_extra)
        if state["fail_seed"] and env_extra and \
                env_extra.get("CUA_CDP_HINT") == "https://stale.test/":
            return (1, "", "cdp hint matched no page: https://stale.test/")
        return (0, '{"text": "RECOVERED", "cdp_url": "https://c.test/", '
                   '"cdp_target_id": "TC"}', "")
    originals = (v8.RUNNER, v8._tab_url, v8._last_cdp_url,
                 v8._last_cdp_target_id, v8._seed_poison)
    try:
        v8.RUNNER = runner37
        v8._tab_url = lambda: "https://stale.test/"
        v8._last_cdp_url, v8._last_cdp_target_id, v8._seed_poison = "", "", ""
        with environment(CU_DRY_RUN=None):
            with v8._agent_session():
                assert v8._cdp_text() is None  # seeded read misses
                assert v8._seed_poison == "https://stale.test/"
                text = v8._cdp_text()  # recovery: no seed supplied
                assert text == "RECOVERED", text
                assert (recorded[-1] or {}).get("CUA_CDP_HINT") is None, recorded
                assert v8._last_cdp_target_id == "TC"
                assert v8._seed_poison == ""
    finally:
        (v8.RUNNER, v8._tab_url, v8._last_cdp_url,
         v8._last_cdp_target_id, v8._seed_poison) = originals


def test_pass37_loop_fixes():
    # (F2) A title-only change on the same window (a navigation) does
    # not clear the CDP target-ID pin.
    progress = os.path.join(tempfile.mkdtemp(prefix="cu-trio2-"), "p.jsonl")
    titles = ["Page A", "Page B"]
    plan = ["pagetext", "observe", "done"]
    seen = []
    def fake_chat(task, steps, last_result, note):
        tool = plan[len(seen)]
        seen.append(last_result)
        return json.dumps(dict(tool=tool, result="title change")), "stop", 0.0
    def fake_tool(raw):
        tool = json.loads(raw)["tool"]
        if tool == "pagetext":
            v8._last_cdp_url = "https://a.test/"
            v8._last_cdp_target_id = "T1"
            return "TEXT A"
        if tool == "observe":
            title = titles[min(len(seen) - 1, 1)]
            return ("Window title: %s\nWindow id: 100\n"
                    "Window class: chromium\nSurface: browser_cdp\n"
                    "URL: https://a.test/" % title)
        return "{}"
    def focus37(fresh=False):
        idx = 0 if len(seen) <= 1 else 1
        return dict(title=titles[idx], window_id="100",
                    window_class="chromium", surface="browser_cdp")
    originals = (v8.call_qwen, v8.run_tool, v8._focus_verify,
                 v8._latest_shot, v8._tab_url)
    try:
        v8.call_qwen, v8.run_tool = fake_chat, fake_tool
        v8._focus_verify = focus37
        v8._latest_shot = lambda: None
        v8._tab_url = lambda: ""
        with environment(CU_PROGRESS_LOG=progress, CU_DRY_RUN=None):
            with contextlib.redirect_stdout(io.StringIO()):
                v8.run_loop("title change keeps pin", max_iters=5)
    finally:
        (v8.call_qwen, v8.run_tool, v8._focus_verify,
         v8._latest_shot, v8._tab_url) = originals
    assert v8._last_cdp_target_id == "T1", v8._last_cdp_target_id
    assert v8._last_cdp_url == "https://a.test/", v8._last_cdp_url


def test_pass39_loop_fixes():
    # (F1 final) The post-step sensor refresh must not read (or pin)
    # the automation page while a DIFFERENT Chromium window is
    # focused.
    progress = os.path.join(tempfile.mkdtemp(prefix="cu-trio2-"), "p.jsonl")
    refreshes = {"n": 0}
    plan = ["observe", "mouse_agent", "done"]
    seen = []
    def fake_chat(task, steps, last_result, note):
        tool = plan[len(seen)]
        seen.append(last_result)
        if tool == "mouse_agent":
            return json.dumps(dict(tool=tool, action="click",
                                   args={"text": "Delete",
                                         "verify": False})), "stop", 0.0
        return json.dumps(dict(tool=tool, result="gate checked")), "stop", 0.0
    def fake_tool(raw):
        tool = json.loads(raw)["tool"]
        if tool == "observe":
            return ("Window title: Other\nWindow id: 200\n"
                    "Window class: chromium\nSurface: browser_cdp\n"
                    "URL: https://other.test/")
        return '{"ok": true, "via": "xdotool", "verified": true}'
    def fake_cdp():
        refreshes["n"] += 1
        v8._cdp_read_url = "https://automation.test/"
        return "AUTOMATION TEXT"
    originals = (v8.call_qwen, v8.run_tool, v8._focus_verify,
                 v8._cdp_text, v8._latest_shot, v8._automation_tab,
                 v8._tab_url)
    try:
        v8.call_qwen, v8.run_tool = fake_chat, fake_tool
        v8._focus_verify = lambda fresh=False: dict(
            title="Other", window_id="200", window_class="chromium",
            surface="browser_cdp")
        v8._cdp_text = fake_cdp
        v8._latest_shot = lambda: None
        v8._automation_tab = lambda: {"window_id": 100,
                                      "url": "https://automation.test/"}
        v8._tab_url = lambda: "https://automation.test/"
        with environment(CU_PROGRESS_LOG=progress, CUA_BIN=None):
            with contextlib.redirect_stdout(io.StringIO()):
                v8.run_loop("sensor gate", max_iters=4)
    finally:
        (v8.call_qwen, v8.run_tool, v8._focus_verify,
         v8._cdp_text, v8._latest_shot, v8._automation_tab,
         v8._tab_url) = originals
    assert refreshes["n"] == 0, refreshes
    assert v8._last_cdp_target_id == "" and v8._last_cdp_url == ""


def _pid_alive(pid):
    """True only for a genuinely running process: a zombie child of
    this process answers kill(pid, 0) but is dead (pass-43)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        with open("/proc/%d/stat" % pid, "rb") as stream:
            state = stream.read().rsplit(b")", 1)[1].split()[0]
        return state != b"Z"
    except OSError:
        return False


def test_pass41_fixes():
    # (a) G1: the rung slice expiring AFTER a successful dispatch but
    # BEFORE the rung returns must stop the ladder via the
    # pre-registered claim — no second engine may click.
    dispatches = []
    cua_calls = []
    def fake_call(argv, *a, **kw):
        dispatches.append(argv)
        return ""
    real_xdotool = v8.rung_xdotool
    def raising_xdotool(args, surface):
        result = real_xdotool(args, surface)
        raise v8.ActionTimeout()  # slice expires in the return gap
    def spy_cua(args, surface):
        cua_calls.append(args)
        return True, "cua", "clicked"
    originals_a = (v8._call, v8.rung_xdotool, v8.rung_cua, v8._grab,
                   v8._focus_verify)
    try:
        v8._call = fake_call
        v8.rung_xdotool = raising_xdotool
        v8.rung_cua = spy_cua
        v8._grab = lambda: None
        v8._focus_verify = lambda fresh=False: dict(
            title="App", window_id="300", window_class="gedit",
            surface="native")
        import time as _time
        token = v8._deadline.set(_time.monotonic() + 30)
        try:
            with environment(CU_DRY_RUN=None):
                out = v8._mouse_action("click", {"x": 5, "y": 5,
                                                 "surface": "native",
                                                 "verify": False})
        finally:
            v8._deadline.reset(token)
    finally:
        (v8._call, v8.rung_xdotool, v8.rung_cua, v8._grab,
         v8._focus_verify) = originals_a
    receipt = out if isinstance(out, dict) else json.loads(out[out.index("{"):])
    assert len(dispatches) == 1, dispatches
    assert cua_calls == [], "ladder must not advance after the claim"
    assert receipt["verified"] == "timeout_after_dispatch", receipt
    assert receipt["via"] == "xdotool", receipt
    # (b) G4: an explicit CUA_ROOT is authoritative for screenshots.
    import tempfile as _tf
    empty_root = _tf.mkdtemp(prefix="cu-rootA-")
    default_shot = "/tmp/cua/shot.jpg"
    made_default = False
    if not os.path.exists(default_shot):
        os.makedirs("/tmp/cua", exist_ok=True)
        Path(default_shot).write_bytes(b"\xff\xd8fake")
        made_default = True
    try:
        with environment(CUA_ROOT=empty_root):
            assert v8._latest_shot() is None, \
                "must not fall through to another desktop's root"
        with environment(CUA_ROOT=None):
            assert v8._latest_shot() is not None
    finally:
        if made_default:
            os.unlink(default_shot)
    # (c) G3: a setsid-detached descendant of a timed-out command is
    # killed with the tree.
    pid_file = "/tmp/cu-g3-grandchild.pid"
    if os.path.exists(pid_file):
        os.unlink(pid_file)
    try:
        v8._default_runner(
            ["bash", "-c",
             "setsid sleep 60 & echo $! > %s; sleep 60" % pid_file],
            0.5)
        raise AssertionError("runner must time out")
    except subprocess.TimeoutExpired:
        pass
    grandchild = int(Path(pid_file).read_text().strip())
    alive = _pid_alive(grandchild)
    if alive:  # cleanup so the suite never leaks the probe process
        os.kill(grandchild, 9)
    assert not alive, "detached descendant survived the timeout kill"
    # (d) G2 (lane): duplicate-URL targets — the visible/focused one
    # wins the pin, not the first (background) entry.
    import importlib.machinery
    import importlib.util
    lane_path = str(Path(__file__).resolve().parent.parent / "src" / "cua-lane")
    loader = importlib.machinery.SourceFileLoader("cualane41", lane_path)
    spec = importlib.util.spec_from_loader("cualane41", loader)
    lane = importlib.util.module_from_spec(spec)
    loader.exec_module(lane)
    targets = [
        {"type": "page", "id": "BACKGROUND", "url": "https://dup.test/",
         "webSocketDebuggerUrl": "ws://bg"},
        {"type": "page", "id": "VISIBLE", "url": "https://dup.test/",
         "webSocketDebuggerUrl": "ws://fg"},
    ]
    class _Resp:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return json.dumps(targets).encode()
    visibility = {"ws://bg": "hidden|false", "ws://fg": "visible|true"}
    class FakeWS:
        def __init__(self, url):
            self.url = url
            self._pending = None
        def send(self, payload):
            self._pending = json.loads(payload)
        def recv(self):
            expr = self._pending.get("params", {}).get("expression", "")
            if "visibilityState" in expr:
                value = visibility[self.url]
            else:
                value = "page text"
            return json.dumps({"id": self._pending["id"], "result": {
                "result": {"value": value}}})
        def close(self):
            pass
    import urllib.request as _ur
    import importlib as _il
    # Earlier tests leave a stub in sys.modules["websocket"]; the lane
    # needs the real package for its create_connection attribute.
    _ws_stub = sys.modules.pop("websocket", None)
    _ws_mod = _il.import_module("websocket")
    originals_d = (_ur.urlopen, _ws_mod.create_connection)
    try:
        _ur.urlopen = lambda *a, **kw: _Resp()
        _ws_mod.create_connection = lambda url, timeout=None: FakeWS(url)
        with environment(CUA_CDP_HINT="https://dup.test/",
                         CUA_CDP_TARGET_ID=None):
            conn = lane._CDP()
        assert conn.target_id == "VISIBLE", conn.target_id
    finally:
        (_ur.urlopen, _ws_mod.create_connection) = originals_d
        if _ws_stub is not None:
            sys.modules["websocket"] = _ws_stub


def test_pass42_fixes():
    # (a) G1-revoke: a pre-dispatch failure (engine binary missing)
    # revokes the claim — a slice expiry in that rung's return gap
    # must NOT stop the ladder; the next engine dispatches.
    cua_calls = []
    def missing_call(argv, *a, **kw):
        raise FileNotFoundError("xdotool absent")
    real_xdotool = v8.rung_xdotool
    def raising_xdotool(args, surface):
        result = real_xdotool(args, surface)
        assert result[2].startswith("skipped="), result
        raise v8.ActionTimeout()  # slice expires in the return gap
    def spy_cua(args, surface):
        cua_calls.append(args)
        return True, "cua", "clicked"
    originals = (v8._call, v8.rung_xdotool, v8.rung_cua, v8._grab,
                 v8._focus_verify)
    import time as _time
    token = v8._deadline.set(_time.monotonic() + 30)
    try:
        v8._call = missing_call
        v8.rung_xdotool = raising_xdotool
        v8.rung_cua = spy_cua
        v8._grab = lambda: None
        v8._focus_verify = lambda fresh=False: dict(
            title="App", window_id="300", window_class="gedit",
            surface="native")
        with environment(CU_DRY_RUN=None):
            out = v8._mouse_action("click", {"x": 5, "y": 5,
                                             "surface": "native",
                                             "verify": False})
    finally:
        (v8._call, v8.rung_xdotool, v8.rung_cua, v8._grab,
         v8._focus_verify) = originals
        v8._deadline.reset(token)
    receipt = out if isinstance(out, dict) else json.loads(out[out.index("{"):])
    assert cua_calls, "ladder must advance after a revoked claim"
    assert receipt["ok"] is True and receipt["via"] == "cua", receipt
    # (b) G3 rescan: a spawner that keeps creating setsid children
    # during the kill window — every spawned pid must be dead after
    # the runner's timeout cleanup.
    pid_file = "/tmp/cu-g3-spawned.pids"
    if os.path.exists(pid_file):
        os.unlink(pid_file)
    script = ("while :; do setsid sleep 60 & echo $! >> %s; "
              "sleep 0.05; done" % pid_file)
    try:
        v8._default_runner(["bash", "-c", script], 0.6)
        raise AssertionError("runner must time out")
    except subprocess.TimeoutExpired:
        pass
    spawned = [int(x) for x in Path(pid_file).read_text().split()]
    assert len(spawned) >= 3, spawned
    survivors = [pid for pid in spawned if _pid_alive(pid)]
    for pid in survivors:  # never leak probe processes
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
    assert not survivors, survivors


def test_pass43_fixes():
    # G3 (hard form): a no-pause Python spawner creates setsid
    # children as fast as it can for the whole timeout window. The
    # executor's child-subreaper anchor must still collect every one
    # of them after the root dies.
    pid_file = "/tmp/cu-g3-fast.pids"
    if os.path.exists(pid_file):
        os.unlink(pid_file)
    spawner = (
        "import subprocess, time\n"
        "end = time.time() + 30\n"
        "with open(%r, 'a') as f:\n"
        "    while time.time() < end:\n"
        "        p = subprocess.Popen(['sleep', '60'],\n"
        "                             start_new_session=True)\n"
        "        f.write(str(p.pid) + '\\n'); f.flush()\n" % pid_file)
    try:
        v8._default_runner(["python3", "-c", spawner], 0.8)
        raise AssertionError("runner must time out")
    except subprocess.TimeoutExpired:
        pass
    spawned = [int(x) for x in Path(pid_file).read_text().split()]
    assert len(spawned) >= 5, spawned
    survivors = [pid for pid in spawned if _pid_alive(pid)]
    for pid in survivors:  # never leak probe processes
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
    assert not survivors, survivors[:5]


def test_pass44_fixes():
    import sys as _sys
    # (a) R1: a detached process from an EARLIER, completed command
    # must survive a later command's timeout cleanup — even back to
    # back, where start ticks can collide.
    detached = []
    try:
        for _round in range(3):
            rc, out, _err = v8._default_runner(
                ["bash", "-c",
                 "setsid sleep 60 </dev/null >/dev/null 2>&1 & echo $!"],
                5)
            pid = int(out.strip())
            detached.append(pid)
            try:
                v8._default_runner(["sleep", "60"], 0.15)
                raise AssertionError("runner must time out")
            except subprocess.TimeoutExpired:
                pass
            assert _pid_alive(pid), \
                "earlier detached process was swept (round %d)" % _round
    finally:
        for pid in detached:
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
    # (b) R2: cleanup reaping must not steal an unrelated child's
    # exit status. Per the module contract, a Popen this process owns
    # and has not waited on is registered in _ACTIVE_ROOTS (the
    # executor's own spawn sites do this for every command root).
    unrelated = subprocess.Popen(
        [_sys.executable, "-c", "raise SystemExit(23)"])
    v8._ACTIVE_ROOTS.add(unrelated.pid)
    try:
        time.sleep(0.5)  # let it exit; deliberately do not wait() yet
        try:
            v8._default_runner(["sleep", "60"], 0.15)
            raise AssertionError("runner must time out")
        except subprocess.TimeoutExpired:
            pass
        assert unrelated.wait() == 23, unrelated.returncode
    finally:
        v8._ACTIVE_ROOTS.discard(unrelated.pid)


def test_pass45_fixes():
    # G3-zombies: descendants killed while still parented to the
    # command root reparent to this process when the root dies; the
    # cleanup must reap them (by pid, at reap time) instead of
    # leaving zombies behind.
    def zombie_children():
        count = 0
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open("/proc/%s/stat" % entry, "rb") as stream:
                    fields = stream.read().rsplit(b")", 1)[1].split()
                if int(fields[1]) == os.getpid() and fields[0] == b"Z":
                    count += 1
            except OSError:
                continue
        return count
    pid_file = "/tmp/cu-g3-zombies.pids"
    if os.path.exists(pid_file):
        os.unlink(pid_file)
    spawner = (
        "import subprocess, time\n"
        "end = time.time() + 30\n"
        "with open(%r, 'a') as f:\n"
        "    while time.time() < end:\n"
        "        p = subprocess.Popen(['sleep', '60'],\n"
        "                             start_new_session=True)\n"
        "        f.write(str(p.pid) + '\\n'); f.flush()\n" % pid_file)
    before = zombie_children()
    try:
        v8._default_runner(["python3", "-c", spawner], 0.4)
        raise AssertionError("runner must time out")
    except subprocess.TimeoutExpired:
        pass
    spawned = [int(x) for x in Path(pid_file).read_text().split()]
    assert len(spawned) >= 5, spawned
    survivors = [pid for pid in spawned if _pid_alive(pid)]
    for pid in survivors:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
    assert not survivors, survivors[:5]
    assert zombie_children() <= before, \
        "cleanup left %d new zombies" % (zombie_children() - before)


def test_pass46_fixes():
    # A persistent process from an EARLIER command (a daemon) spawns
    # a child and then dies during a LATER command's window: the
    # grandchild reparents here but must be protected by the forest
    # attribution, not swept as the later command's orphan.
    d_file, c_file = "/tmp/cu-p46-daemon.pid", "/tmp/cu-p46-child.pid"
    for path in (d_file, c_file):
        if os.path.exists(path):
            os.unlink(path)
    daemon = (
        "import subprocess, time, os\n"
        "open(%r, 'w').write(str(os.getpid()))\n"
        "time.sleep(0.4)\n"
        "p = subprocess.Popen(['sleep', '60'], start_new_session=True,\n"
        "                     stdin=subprocess.DEVNULL,\n"
        "                     stdout=subprocess.DEVNULL,\n"
        "                     stderr=subprocess.DEVNULL)\n"
        "open(%r, 'w').write(str(p.pid))\n"
        "time.sleep(0.6)\n" % (d_file, c_file))
    rc, _out, _err = v8._default_runner(["python3", "-c", daemon], 10)
    assert rc == 0, (rc, _err)
    child = int(Path(c_file).read_text().strip())
    assert _pid_alive(child)
    try:
        try:
            v8._default_runner(["sleep", "60"], 0.3)
            raise AssertionError("runner must time out")
        except subprocess.TimeoutExpired:
            pass
        assert _pid_alive(child), \
            "earlier daemon's child was swept by a later cleanup"
    finally:
        try:
            os.kill(child, 9)
        except ProcessLookupError:
            pass


def test_pass47_fixes():
    # (a) Item 2: an orphan of the TIMED-OUT command (self-attributed
    # after reparenting here) must not shield its own subtree — its
    # child follows it into the cleanup.
    o_file, c_file = "/tmp/cu-p47-orphan.pid", "/tmp/cu-p47-child.pid"
    for path in (o_file, c_file):
        if os.path.exists(path):
            os.unlink(path)
    orphan = (
        "import subprocess, time, os\n"
        "open(%r, 'w').write(str(os.getpid()))\n"
        "time.sleep(0.35)\n"
        "p = subprocess.Popen(['sleep', '60'], start_new_session=True,\n"
        "                     stdin=subprocess.DEVNULL,\n"
        "                     stdout=subprocess.DEVNULL,\n"
        "                     stderr=subprocess.DEVNULL)\n"
        "open(%r, 'w').write(str(p.pid))\n"
        "time.sleep(30)\n" % (o_file, c_file))
    script_path = "/tmp/cu-p47-orphan.py"
    Path(script_path).write_text(orphan)
    try:
        # The subshell launches the orphan detached and exits at
        # once, so the orphan reparents HERE while the command shell
        # keeps running — the Codex pass-47 item-2 shape.
        v8._default_runner(
            ["bash", "-c",
             "(setsid python3 %s &) ; sleep 60" % script_path],
            1.2)
        raise AssertionError("runner must time out")
    except subprocess.TimeoutExpired:
        pass
    orphan_pid = int(Path(o_file).read_text().strip())
    child_pid = int(Path(c_file).read_text().strip())
    leftovers = [p for p in (orphan_pid, child_pid) if _pid_alive(p)]
    for pid in leftovers:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
    assert not leftovers, leftovers
    # (b) Item 5: a thread-start failure in the forest observer must
    # never propagate into a command run.
    import threading as _threading
    class _RaisingThread:
        def __init__(self, *a, **kw):
            pass
        def start(self):
            raise RuntimeError("can't start new thread")
    saved_thread = _threading.Thread
    saved_handle = v8._FOREST_THREAD
    try:
        _threading.Thread = _RaisingThread
        v8._FOREST_THREAD = None
        v8._forest_start()  # must swallow
        assert v8._FOREST_THREAD is None
        _threading.Thread = saved_thread
        # The next spawn path retries the start (pass-48): the
        # observer must come up once threading works again.
        v8._ensure_subreaper()
        assert v8._FOREST_THREAD is not None
    finally:
        _threading.Thread = saved_thread
        if v8._FOREST_THREAD is None:
            v8._FOREST_THREAD = saved_handle
    rc, out, _err = v8._default_runner(["bash", "-c", "echo ok"], 5)
    assert rc == 0 and "ok" in out, (rc, out, _err)


def test_pass50_fixes():
    # (a) H2: the lane's JSON error envelope must not defeat the
    # pre-dispatch prefix match — a selector miss is a SKIP, so the
    # ladder can fall back to coordinates.
    def envelope_cua(*argv, **kw):
        raise RuntimeError('{"ok": false, "error": '
                           '"selector not found: #missing"}')
    import socket as _socket
    import contextlib as _cl
    @_cl.contextmanager
    def fake_connection(*a, **kw):
        yield None
    originals_a = (v8._cua, v8._last_cdp_url, v8._last_cdp_target_id,
                   _socket.create_connection)
    try:
        v8._cua = envelope_cua
        v8._last_cdp_url = "https://x.test/"
        v8._last_cdp_target_id = ""
        _socket.create_connection = fake_connection
        with environment(CU_DRY_RUN=None, CUA_CDP_HINT=None,
                         CUA_CDP_TARGET_ID=None):
            result = v8.rung_cdp({"text": "Missing"}, "browser_cdp")
    finally:
        (v8._cua, v8._last_cdp_url, v8._last_cdp_target_id,
         _socket.create_connection) = originals_a
    assert result[0] is False and result[2] == "skipped=target_not_found", result
    # (b) H1 (lane): with NO hint, duplicate pages resolve to the
    # visible/focused target, not list order.
    import importlib.machinery
    import importlib.util
    lane_path = str(Path(__file__).resolve().parent.parent / "src" / "cua-lane")
    loader = importlib.machinery.SourceFileLoader("cualane50", lane_path)
    spec = importlib.util.spec_from_loader("cualane50", loader)
    lane = importlib.util.module_from_spec(spec)
    loader.exec_module(lane)
    targets = [
        {"type": "page", "id": "BACKGROUND", "url": "https://a.test/",
         "webSocketDebuggerUrl": "ws://bg"},
        {"type": "page", "id": "VISIBLE", "url": "https://b.test/",
         "webSocketDebuggerUrl": "ws://fg"},
    ]
    class _Resp:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return json.dumps(targets).encode()
    visibility = {"ws://bg": "hidden|false", "ws://fg": "visible|true"}
    class FakeWS:
        def __init__(self, url):
            self.url = url
            self._pending = None
        def send(self, payload):
            self._pending = json.loads(payload)
        def recv(self):
            expr = self._pending.get("params", {}).get("expression", "")
            value = (visibility[self.url]
                     if "visibilityState" in expr else "page text")
            return json.dumps({"id": self._pending["id"], "result": {
                "result": {"value": value}}})
        def close(self):
            pass
    import urllib.request as _ur
    import importlib as _il
    _ws_stub = sys.modules.pop("websocket", None)
    _ws_mod = _il.import_module("websocket")
    originals_b = (_ur.urlopen, _ws_mod.create_connection)
    try:
        _ur.urlopen = lambda *a, **kw: _Resp()
        _ws_mod.create_connection = lambda url, timeout=None: FakeWS(url)
        with environment(CUA_CDP_HINT=None, CUA_CDP_TARGET_ID=None):
            conn = lane._CDP()
        assert conn.target_id == "VISIBLE", conn.target_id
    finally:
        (_ur.urlopen, _ws_mod.create_connection) = originals_b
        if _ws_stub is not None:
            sys.modules["websocket"] = _ws_stub
    # (c) H3: a short-lived detached child of a SUCCESSFUL command
    # must not linger as this process's zombie — the forest observer
    # reaps adopted zombies on its normal polls.
    rc, out, _err = v8._default_runner(
        ["bash", "-c",
         "setsid sleep 0.2 </dev/null >/dev/null 2>&1 & echo $!"], 5)
    assert rc == 0, (rc, _err)
    child = int(out.strip())
    deadline = time.time() + 5
    lingering = True
    while time.time() < deadline:
        try:
            with open("/proc/%d/stat" % child, "rb") as stream:
                fields = stream.read().rsplit(b")", 1)[1].split()
            if int(fields[1]) != os.getpid() or fields[0] != b"Z":
                lingering = False
                break
        except OSError:
            lingering = False
            break
        time.sleep(0.1)
    assert not lingering, "adopted zombie was never reaped"


def test_pass52_fixes():
    # Every v8-owned spawn registers its root: _run_owned (the
    # subprocess.run replacement used by the AT-SPI probe and OCR)
    # must return the child's REAL exit status even with the forest
    # observer running, and the child must be registered in-flight.
    rc, _out, _err = v8._run_owned(["bash", "-c", "exit 23"], 5)
    assert rc == 23, rc
    rc, out, _err = v8._run_owned(["bash", "-c", "echo owned-ok"], 5,
                                  text=True)
    assert rc == 0 and "owned-ok" in out, (rc, out)
    # A fast-exiting root followed by observer polls keeps its
    # status through the public runner too (registration shield).
    for _round in range(3):
        rc, _o, _e = v8._default_runner(["bash", "-c", "exit 23"], 5)
        assert rc == 23, rc
        time.sleep(0.4)  # let the observer poll over the reap window


def test_pass55_fixes():
    # (a) K3: an alarm delivered DURING _within's own setup must
    # unwind cleanly — no leaked deadline or handler for later calls.
    import signal as _signal
    real_setitimer = _signal.setitimer
    fired = {"n": 0}
    def exploding_setitimer(which, seconds, interval=0.0):
        fired["n"] += 1
        if fired["n"] == 1:
            raise v8.ActionTimeout()
        return real_setitimer(which, seconds, interval)
    token = v8._deadline.set(time.monotonic() + 60)
    try:
        _signal.setitimer = exploding_setitimer
        try:
            with v8._within(5):
                pass
            raise AssertionError("setup alarm must propagate")
        except v8.ActionTimeout:
            pass
    finally:
        _signal.setitimer = real_setitimer
    try:
        assert v8._bounded("read", lambda: "success") == "success"
    finally:
        v8._deadline.reset(token)
    # (b) K6: a reused XID (same window id, new pid) must not keep
    # the previous window's class; fresh=True always re-queries.
    focus_state = {"pid": "111", "cls": 'WM_CLASS(STRING) = "gedit", "gedit"'}
    def focus_call(argv, *a, **kw):
        if argv[1] == "getactivewindow":
            return "100"
        if argv[1] == "getwindowname":
            return "Doc"
        if argv[1] == "getwindowpid":
            return focus_state["pid"]
        if argv[0] == "xprop":
            return focus_state["cls"]
        return ""
    originals_b = (v8._call, dict(v8._focus_cache))
    try:
        v8._call = focus_call
        v8._focus_cache.clear()
        first = v8._focus_verify(fresh=True)
        assert "gedit" in first["window_class"], first
        focus_state.update(pid="222",
                           cls='WM_CLASS(STRING) = "vncviewer", "Vncviewer"')
        second = v8._focus_verify()  # same XID, new pid
        assert "vncviewer" in second["window_class"].lower(), second
        focus_state.update(cls='WM_CLASS(STRING) = "chromium", "Chromium"')
        third = v8._focus_verify(fresh=True)  # same pid, fresh re-query
        assert "chromium" in third["window_class"].lower(), third
    finally:
        v8._call = originals_b[0]
        v8._focus_cache.clear()
        v8._focus_cache.update(originals_b[1])
    # (c) K4: tab-affecting chords clear the CDP pins; other keys
    # leave them alone.
    originals_c = (v8._call, v8._ensure_focus, v8._last_cdp_url,
                   v8._last_cdp_target_id)
    try:
        v8._call = lambda argv, *a, **kw: ""
        v8._ensure_focus = lambda args: dict(surface="browser_cdp")
        v8._last_cdp_url, v8._last_cdp_target_id = "https://a.test/", "A"
        v8._kbd_action("keys", {"keys": ["a"]})
        assert v8._last_cdp_target_id == "A"
        v8._kbd_action("keys", {"keys": ["ctrl+t"]})
        assert v8._last_cdp_target_id == "" and v8._last_cdp_url == ""
        v8._last_cdp_url, v8._last_cdp_target_id = "https://a.test/", "A"
        v8._kbd_action("keys", {"keys": ["ctrl+Tab"]})
        assert v8._last_cdp_target_id == ""
    finally:
        (v8._call, v8._ensure_focus, v8._last_cdp_url,
         v8._last_cdp_target_id) = originals_c
    # (d) K2: _kill_tree on an already-reaped root must still sweep
    # its detached orphans, using the start time captured at spawn.
    pid_file = "/tmp/cu-p55-orphan.pid"
    if os.path.exists(pid_file):
        os.unlink(pid_file)
    proc = subprocess.Popen(
        ["bash", "-c",
         "setsid sleep 60 </dev/null >/dev/null 2>&1 & echo $! > %s"
         % pid_file])
    root_start = v8._pid_start(proc.pid)
    proc.communicate(timeout=10)
    orphan = int(Path(pid_file).read_text().strip())
    assert _pid_alive(orphan)
    proc._cu_baseline = set()
    proc._cu_root_start = root_start
    try:
        v8._kill_tree(proc)
        assert not _pid_alive(orphan), "detached orphan survived"
    finally:
        if _pid_alive(orphan):
            os.kill(orphan, 9)
    # (e) K5 (lane): unhinted selection — a FOCUSED Gmail page beats
    # a hidden non-mail page.
    import importlib.machinery
    import importlib.util
    lane_path = str(Path(__file__).resolve().parent.parent / "src" / "cua-lane")
    loader = importlib.machinery.SourceFileLoader("cualane55", lane_path)
    spec = importlib.util.spec_from_loader("cualane55", loader)
    lane = importlib.util.module_from_spec(spec)
    loader.exec_module(lane)
    targets = [
        {"type": "page", "id": "OTHER", "url": "https://other.test/",
         "webSocketDebuggerUrl": "ws://other"},
        {"type": "page", "id": "GMAIL", "url": "https://mail.google.com/mail/u/0/",
         "webSocketDebuggerUrl": "ws://gmail"},
    ]
    class _Resp:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return json.dumps(targets).encode()
    visibility = {"ws://other": "hidden|false", "ws://gmail": "visible|true"}
    class FakeWS:
        def __init__(self, url):
            self.url = url
            self._pending = None
        def send(self, payload):
            self._pending = json.loads(payload)
        def recv(self):
            expr = self._pending.get("params", {}).get("expression", "")
            value = (visibility[self.url]
                     if "visibilityState" in expr else "page text")
            return json.dumps({"id": self._pending["id"], "result": {
                "result": {"value": value}}})
        def close(self):
            pass
    import urllib.request as _ur
    import importlib as _il
    _ws_stub = sys.modules.pop("websocket", None)
    _ws_mod = _il.import_module("websocket")
    originals_e = (_ur.urlopen, _ws_mod.create_connection)
    try:
        _ur.urlopen = lambda *a, **kw: _Resp()
        _ws_mod.create_connection = lambda url, timeout=None: FakeWS(url)
        with environment(CUA_CDP_HINT=None, CUA_CDP_TARGET_ID=None):
            conn = lane._CDP()
        assert conn.target_id == "GMAIL", conn.target_id
    finally:
        (_ur.urlopen, _ws_mod.create_connection) = originals_e
        if _ws_stub is not None:
            sys.modules["websocket"] = _ws_stub


def test_pass56_fixes():
    # (a) K4': pins clear BEFORE the batch — a tab chord followed by
    # a failing key in the same batch must not leave stale pins.
    originals_a = (v8._call, v8._ensure_focus, v8._last_cdp_url,
                   v8._last_cdp_target_id)
    def failing_call(argv, *a, **kw):
        raise RuntimeError("second key failed")
    try:
        v8._call = failing_call
        v8._ensure_focus = lambda args: dict(surface="browser_cdp")
        v8._last_cdp_url, v8._last_cdp_target_id = "https://a.test/", "A"
        try:
            v8._kbd_action("keys", {"keys": ["ctrl+Tab", "ctrl+a"]})
            raise AssertionError("dispatch failure must propagate")
        except RuntimeError:
            pass
        assert v8._last_cdp_target_id == "" and v8._last_cdp_url == ""
    finally:
        (v8._call, v8._ensure_focus, v8._last_cdp_url,
         v8._last_cdp_target_id) = originals_a
    # (b) K6': a failed PID lookup (empty identity) must never
    # satisfy the class cache — the class is re-queried every call.
    focus_state = {"cls": 'WM_CLASS(STRING) = "gedit", "gedit"',
                   "queries": 0}
    def blind_call(argv, *a, **kw):
        if argv[1] == "getactivewindow":
            return "100"
        if argv[1] == "getwindowname":
            return "Doc"
        if argv[1] == "getwindowpid":
            raise RuntimeError("no pid")
        if argv[0] == "xprop":
            focus_state["queries"] += 1
            return focus_state["cls"]
        return ""
    originals_b = (v8._call, dict(v8._focus_cache))
    try:
        v8._call = blind_call
        v8._focus_cache.clear()
        first = v8._focus_verify()
        assert "gedit" in first["window_class"], first
        focus_state["cls"] = 'WM_CLASS(STRING) = "vncviewer", "Vncviewer"'
        second = v8._focus_verify()
        assert "vncviewer" in second["window_class"].lower(), second
        assert focus_state["queries"] == 2, focus_state
    finally:
        v8._call = originals_b[0]
        v8._focus_cache.clear()
        v8._focus_cache.update(originals_b[1])


def test_pass57_fixes():
    # K1-final: a spawn whose Popen object was never bound (alarm in
    # the return-to-assignment gap) is recovered by identity and
    # killed — while an earlier daemon's child in the same scan is
    # protected by its attribution.
    _children, pre_starts, _st = v8._children_map()
    baseline = set(_children.get(os.getpid(), []))
    token = "cu-test-%d" % time.time_ns()
    env = dict(os.environ, CU_SPAWN_TOKEN=token)
    newborn = subprocess.Popen(["sleep", "60"], env=env,
                               start_new_session=True,
                               stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    # A same-window bystander with the IDENTICAL argv but NO token
    # must survive the recovery scan (passes 58-60 guards).
    bystander = subprocess.Popen(["sleep", "60"],
                                 start_new_session=True,
                                 stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
    # An exec-wrapped newborn keeps the token across the exec
    # (pass-60 shape B).
    token2 = "cu-test2-%d" % time.time_ns()
    env2 = dict(os.environ, CU_SPAWN_TOKEN=token2)
    wrapped = subprocess.Popen(["/usr/bin/env", "sleep", "62"], env=env2,
                               start_new_session=True,
                               stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    try:
        assert _pid_alive(newborn.pid)
        v8._kill_unassigned_spawn(baseline, pre_starts, token)
        assert not _pid_alive(newborn.pid), \
            "unassigned newborn was not recovered and killed"
        assert _pid_alive(bystander.pid), \
            "recovery killed a tokenless identical-argv bystander"
        time.sleep(0.3)
        v8._kill_unassigned_spawn(baseline, pre_starts, token2)
        assert not _pid_alive(wrapped.pid), \
            "exec-wrapped newborn was not recovered via its token"
    finally:
        for proc_obj in (newborn, bystander, wrapped):
            try:
                proc_obj.kill()
            except Exception:
                pass
            try:
                proc_obj.wait(timeout=5)
            except Exception:
                pass


def test_pass61_fixes():
    # Token hygiene: a shell printenv must not leak the live
    # spawn token into model-visible output.
    out = v8.run_tool(json.dumps(
        {"tool": "shell", "cmd": "printenv CU_SPAWN_TOKEN"}))
    assert "CU-SPAWN-TOKEN" in out, out
    live = [t for t in v8._SPAWN_TOKENS if t in out]
    assert not live, (live, out)
    # The runner's returned output is scrubbed too.
    rc, so, se = v8._default_runner(
        ["bash", "-c", "printenv CU_SPAWN_TOKEN"], 10)
    assert rc == 0 and "CU-SPAWN-TOKEN" in so, (rc, so)
    assert all(t not in so for t in v8._SPAWN_TOKENS), so


def test_pass63_fixes():
    # T1: a token carrier orphaned by an instantly-exiting root is
    # recovered even though it is NOT a session leader. The shape
    # races the carrier's exec (its /proc environ reads empty
    # mid-exec), so run it several times.
    for attempt in range(8):
        _children, pre_starts, _st = v8._children_map()
        baseline = set(_children.get(os.getpid(), []))
        token = "cu-test63-%d" % time.time_ns()
        env = dict(os.environ, CU_SPAWN_TOKEN=token)
        root = subprocess.Popen(
            ["bash", "-c", "sleep 63 </dev/null >/dev/null 2>&1 & echo $!"],
            env=env, stdout=subprocess.PIPE, text=True,
            start_new_session=True)
        out, _ = root.communicate(timeout=10)
        orphan = int(out.strip())
        assert _pid_alive(orphan)
        try:
            v8._kill_unassigned_spawn(baseline, pre_starts, token)
            assert not _pid_alive(orphan), \
                "token carrier survived recovery (attempt %d)" % attempt
        finally:
            if _pid_alive(orphan):
                os.kill(orphan, 9)
    # T2: the read tool's output is scrubbed too.
    fake = "cu99999-123456789"
    v8._SPAWN_TOKENS.add(fake)
    probe = Path("/tmp/cu-p63-probe.txt")
    probe.write_text("value=" + fake + "\n")
    try:
        out = v8.run_tool(json.dumps(
            {"tool": "read", "file": str(probe)}))
        assert fake not in out and "CU-SPAWN-TOKEN" in out, out
    finally:
        probe.unlink(missing_ok=True)
        v8._SPAWN_TOKENS.discard(fake)
    # T3: tokens are never evicted — the 200th spawn must not
    # un-redact the first token.
    first = "cu-first-1"
    v8._SPAWN_TOKENS.add(first)
    for i in range(200):
        v8._SPAWN_TOKENS.add("cu-fill-%d" % i)
    assert v8._scrub_tokens("x " + first) == "x CU-SPAWN-TOKEN"
    v8._SPAWN_TOKENS.discard(first)
    for i in range(200):
        v8._SPAWN_TOKENS.discard("cu-fill-%d" % i)


def test_pass66_fixes():
    # U1: a root whose _cu_baseline was never attached (alarm in
    # the Popen-to-attach gap) still protects baseline members —
    # _kill_tree recovers the baseline from the token-verified
    # pending-spawn record. Repeated: the recovery races the
    # root's death (its environ empties), so once is not evidence.
    original_pending = v8._PENDING_SPAWN
    try:
        for attempt in range(6):
            bystander = subprocess.Popen(["sleep", "64"],
                                         start_new_session=True,
                                         stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)
            token = "cu-test66-%d" % time.time_ns()
            env = dict(os.environ, CU_SPAWN_TOKEN=token)
            root = subprocess.Popen(["sleep", "64"], env=env,
                                    start_new_session=True,
                                    stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            try:
                assert not hasattr(root, "_cu_baseline")
                v8._PENDING_SPAWN = {"baseline": {bystander.pid},
                                     "token": token}
                v8._kill_tree(root)
                assert not _pid_alive(root.pid), "root survived cleanup"
                assert _pid_alive(bystander.pid), \
                    "baseline member killed (attempt %d)" % attempt
            finally:
                for proc_obj in (root, bystander):
                    try:
                        proc_obj.kill()
                    except Exception:
                        pass
                    try:
                        proc_obj.wait(timeout=5)
                    except Exception:
                        pass
    finally:
        v8._PENDING_SPAWN = original_pending
    # U2: with the seed retired, a read-class CDP call must not
    # consult the metadata seed at all; unretired, it must.
    seed_calls = {"n": 0}
    def counting_tab_url():
        seed_calls["n"] += 1
        return "https://seed.test/"
    seen_env = []
    def recording_call(argv, timeout_s=None, env_extra=None):
        seen_env.append(env_extra)
        return json.dumps({"cdp_url": "https://live.test/",
                           "text": "t", "cdp_target_id": "T1"})
    originals_u2 = (v8._tab_url, v8._call, v8._last_cdp_url,
                    v8._last_cdp_target_id, v8._seed_retired,
                    v8._seed_poison)
    try:
        v8._tab_url = counting_tab_url
        v8._call = recording_call
        v8._last_cdp_url, v8._last_cdp_target_id = "", ""
        v8._seed_poison = ""
        v8._seed_retired = True
        v8._cua("cdp-text")
        assert seed_calls["n"] == 0, "retired seed was consulted"
        assert not seen_env[-1] or \
            "CUA_CDP_HINT" not in (seen_env[-1] or {}), seen_env
        v8._seed_retired = False
        v8._cua("cdp-text")
        assert seed_calls["n"] == 1, "live seed was not consulted"
        assert (seen_env[-1] or {}).get("CUA_CDP_HINT") == \
            "https://seed.test/", seen_env
    finally:
        (v8._tab_url, v8._call, v8._last_cdp_url,
         v8._last_cdp_target_id, v8._seed_retired,
         v8._seed_poison) = originals_u2
    # U3: a no-op type must not verify against a STALE cached
    # before-frame — the before-frame is a fresh grab.
    from PIL import Image
    def png(color):
        buf = io.BytesIO()
        Image.new("RGB", (1920, 1080), color).save(buf, format="PNG")
        return buf.getvalue()
    frame_now, frame_stale = png("white"), png("black")
    originals_u3 = (v8._grab, v8._latest_shot, v8._call,
                    v8._ensure_focus)
    try:
        v8._grab = lambda: frame_now
        v8._latest_shot = lambda: frame_stale
        v8._call = lambda argv, *a, **kw: ""
        v8._ensure_focus = lambda args: dict(
            surface="native", title="Doc", window_id="1")
        receipt = v8._kbd_action("type_verify", {
            "text": "x", "engine": "xdotool"})
        assert receipt["ok"] is True, receipt
        assert not receipt["verified"], \
            "no-op type verified against a stale frame: %r" % (receipt,)
    finally:
        (v8._grab, v8._latest_shot, v8._call,
         v8._ensure_focus) = originals_u3


def test_pass69_fixes():
    # V1: cleanup is uninterruptible — an alarm expiring DURING
    # _kill_tree's scans must not abandon a live root. The alarm
    # still surfaces afterwards (the action did time out).
    import signal as _signal
    root = subprocess.Popen(["sleep", "66"], start_new_session=True,
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    root._cu_baseline = set()
    real_map = v8._children_map
    slowed = {"n": 0}
    def slow_map():
        slowed["n"] += 1
        if slowed["n"] == 1:
            time.sleep(0.3)
        return real_map()
    def boom(signum, frame):
        raise v8.ActionTimeout()
    old_handler = _signal.signal(_signal.SIGALRM, boom)
    try:
        v8._children_map = slow_map
        _signal.setitimer(_signal.ITIMER_REAL, 0.05)
        raised = False
        try:
            v8._kill_tree(root)
        except v8.ActionTimeout:
            raised = True
        _signal.setitimer(_signal.ITIMER_REAL, 0)
        assert raised, "the pending alarm should surface after cleanup"
        assert not _pid_alive(root.pid), \
            "cleanup was interrupted: root left alive"
    finally:
        _signal.setitimer(_signal.ITIMER_REAL, 0)
        _signal.signal(_signal.SIGALRM, old_handler)
        v8._children_map = real_map
        try:
            root.kill()
        except Exception:
            pass
        try:
            root.wait(timeout=5)
        except Exception:
            pass
    # V2: a foreground-tab change under the pin (title moved) drops
    # the pin at read time; the read re-pins the live page.
    focus_state = {"title": "Tab A"}
    seen_pins = []
    pages = {"A": {"cdp_url": "https://a.test/", "text": "BODY A",
                   "cdp_target_id": "TA"},
             "B": {"cdp_url": "https://b.test/", "text": "BODY B",
                   "cdp_target_id": "TB"}}
    def fake_cua(*args):
        seen_pins.append(v8._last_cdp_target_id)
        if v8._last_cdp_target_id == "TA":
            return pages["A"]
        if v8._last_cdp_target_id == "TB":
            return pages["B"]
        # Unpinned: the lane serves the FOREGROUND page.
        return pages["A"] if focus_state["title"] == "Tab A" \
            else pages["B"]
    originals_v2 = (v8._cua, v8._focus_verify, v8._is_automation_window,
                    v8._last_cdp_url, v8._last_cdp_target_id,
                    v8._last_cdp_title, v8._cua_bin)
    try:
        v8._cua = fake_cua
        v8._cua_bin = lambda: "/x/cua-lane"
        v8._focus_verify = lambda fresh=False: dict(
            window_id="7", title=focus_state["title"])
        v8._is_automation_window = lambda wid, pid=None: wid == "7"
        v8._last_cdp_url, v8._last_cdp_target_id = "", ""
        v8._last_cdp_title = ""
        assert v8._cdp_text() == "BODY A"
        assert v8._last_cdp_target_id == "TA"
        assert v8._last_cdp_title == "Tab A"
        focus_state["title"] = "Tab B"  # tab-strip click switched
        text = v8._cdp_text()
        assert seen_pins[-1] == "", \
            "read honored the stale pin after the tab switch"
        assert text == "BODY B" and v8._last_cdp_target_id == "TB", \
            (text, v8._last_cdp_target_id)
    finally:
        (v8._cua, v8._focus_verify, v8._is_automation_window,
         v8._last_cdp_url, v8._last_cdp_target_id,
         v8._last_cdp_title, v8._cua_bin) = originals_v2
    # V3 (lane): a HIDDEN hint match loses to the unique
    # visible+focused page; a visible hint match is kept.
    import importlib.machinery
    import importlib.util
    lane_path = str(Path(__file__).resolve().parent.parent / "src" / "cua-lane")
    loader = importlib.machinery.SourceFileLoader("cualane69", lane_path)
    spec = importlib.util.spec_from_loader("cualane69", loader)
    lane = importlib.util.module_from_spec(spec)
    loader.exec_module(lane)
    targets = [
        {"type": "page", "id": "OLD", "url": "https://old.test/",
         "webSocketDebuggerUrl": "ws://old"},
        {"type": "page", "id": "NEW", "url": "https://new.test/",
         "webSocketDebuggerUrl": "ws://new"},
    ]
    class _Resp:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return json.dumps(targets).encode()
    visibility = {"ws://old": "hidden|false", "ws://new": "visible|true"}
    class FakeWS:
        def __init__(self, url):
            self.url = url
            self._pending = None
        def send(self, payload):
            self._pending = json.loads(payload)
        def recv(self):
            expr = self._pending.get("params", {}).get("expression", "")
            value = (visibility[self.url]
                     if "visibilityState" in expr else "page text")
            return json.dumps({"id": self._pending["id"], "result": {
                "result": {"value": value}}})
        def close(self):
            pass
    import urllib.request as _ur
    import importlib as _il
    _ws_stub = sys.modules.pop("websocket", None)
    _ws_mod = _il.import_module("websocket")
    originals_v3 = (_ur.urlopen, _ws_mod.create_connection)
    try:
        _ur.urlopen = lambda *a, **kw: _Resp()
        _ws_mod.create_connection = lambda url, timeout=None: FakeWS(url)
        with environment(CUA_CDP_HINT="https://old.test/",
                         CUA_CDP_TARGET_ID=None):
            conn = lane._CDP()
        assert conn.target_id == "NEW", conn.target_id
        visibility["ws://old"] = "visible|true"
        visibility["ws://new"] = "hidden|false"
        with environment(CUA_CDP_HINT="https://old.test/",
                         CUA_CDP_TARGET_ID=None):
            conn = lane._CDP()
        assert conn.target_id == "OLD", conn.target_id
    finally:
        (_ur.urlopen, _ws_mod.create_connection) = originals_v3
        if _ws_stub is not None:
            sys.modules["websocket"] = _ws_stub


def test_pass70_fixes():
    # V2': a SAME-TITLE tab switch is caught by the served page's
    # visibility: the pinned page reports hidden while its window
    # is focused, so the read drops the pin and re-reads.
    focus_state = {"title": "Same Title"}
    pages = {"A": {"cdp_url": "https://a.test/", "text": "BODY A",
                   "cdp_target_id": "TA", "cdp_visible": 2},
             "B": {"cdp_url": "https://b.test/", "text": "BODY B",
                   "cdp_target_id": "TB", "cdp_visible": 2}}
    def fake_cua(*args):
        if v8._last_cdp_target_id == "TA":
            return pages["A"]
        if v8._last_cdp_target_id == "TB":
            return pages["B"]
        return pages["A"] if pages["A"]["cdp_visible"] == 2 and \
            v8._last_cdp_url == "" and focus_state.get("fg", "A") == "A" \
            else pages["B"]
    originals = (v8._cua, v8._focus_verify, v8._is_automation_window,
                 v8._last_cdp_url, v8._last_cdp_target_id,
                 v8._last_cdp_title, v8._cua_bin)
    try:
        v8._cua = fake_cua
        v8._cua_bin = lambda: "/x/cua-lane"
        v8._focus_verify = lambda fresh=False: dict(
            window_id="7", title=focus_state["title"])
        v8._is_automation_window = lambda wid, pid=None: wid == "7"
        v8._last_cdp_url, v8._last_cdp_target_id = "", ""
        v8._last_cdp_title = ""
        assert v8._cdp_text() == "BODY A"
        assert v8._last_cdp_target_id == "TA"
        # Tab-strip click to B: same title, A now hidden.
        focus_state["fg"] = "B"
        pages["A"]["cdp_visible"] = 0
        text = v8._cdp_text()
        assert text == "BODY B" and v8._last_cdp_target_id == "TB", \
            (text, v8._last_cdp_target_id)
    finally:
        (v8._cua, v8._focus_verify, v8._is_automation_window,
         v8._last_cdp_url, v8._last_cdp_target_id,
         v8._last_cdp_title, v8._cua_bin) = originals
    # V1': an alarm expiring right at _kill_tree entry must not
    # skip cleanup — repeated trials with the machinery running.
    import signal as _signal
    def boom(signum, frame):
        raise v8.ActionTimeout()
    old_handler = _signal.signal(_signal.SIGALRM, boom)
    try:
        for trial in range(30):
            root = subprocess.Popen(["sleep", "67"],
                                    start_new_session=True,
                                    stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            root._cu_baseline = set()
            _signal.setitimer(_signal.ITIMER_REAL, 0.001)
            try:
                v8._kill_tree(root)
            except v8.ActionTimeout:
                pass
            _signal.setitimer(_signal.ITIMER_REAL, 0)
            alive = _pid_alive(root.pid)
            try:
                root.kill()
            except Exception:
                pass
            try:
                root.wait(timeout=5)
            except Exception:
                pass
            assert not alive, "root survived trial %d" % trial
    finally:
        _signal.setitimer(_signal.ITIMER_REAL, 0)
        _signal.signal(_signal.SIGALRM, old_handler)


def test_pass75_fixes():
    # Dispatch-time pin revalidation: a tab switch performed
    # OUTSIDE kbd_agent (a shell-step xdotool ctrl+Tab) must not
    # let a direct CDP dispatch honor the stale pin.
    focus_state = {"title": "Tab A"}
    dispatched = []
    page_a = {"cdp_url": "https://a.test/", "text": "BODY A",
              "cdp_target_id": "TA", "cdp_visible": 2}
    page_b = {"cdp_url": "https://b.test/", "text": "BODY B",
              "cdp_target_id": "TB", "cdp_visible": 2}
    def lane_call(argv, timeout_s=None, env_extra=None):
        env = env_extra or {}
        if argv[1] == "cdp-text":
            if env.get("CUA_CDP_TARGET_ID") == "TA":
                return json.dumps(page_a)
            if env.get("CUA_CDP_TARGET_ID") == "TB":
                return json.dumps(page_b)
            # Unpinned: the lane serves the foreground page.
            return json.dumps(
                page_a if focus_state["title"] == "Tab A" else page_b)
        if argv[1] == "cdp-click":
            dispatched.append(env.get("CUA_CDP_TARGET_ID"))
            return json.dumps({"clicked": "#submit"})
        return json.dumps({})
    originals = (v8._call, v8._focus_verify, v8._is_automation_window,
                 v8._last_cdp_url, v8._last_cdp_target_id,
                 v8._last_cdp_title, v8._seed_retired, v8._cua_bin)
    try:
        v8._call = lane_call
        v8._cua_bin = lambda: "/x/cua-lane"
        v8._focus_verify = lambda fresh=False: dict(
            window_id="7", title=focus_state["title"])
        v8._is_automation_window = lambda wid, pid=None: wid == "7"
        v8._last_cdp_url, v8._last_cdp_target_id = "", ""
        v8._last_cdp_title = ""
        v8._seed_retired = False
        assert v8._cdp_text() == "BODY A"
        assert v8._last_cdp_target_id == "TA"
        focus_state["title"] = "Tab B"  # shell-step tab switch
        v8._cua("cdp-click", "#submit")
        assert dispatched == ["TB"], \
            "dispatch honored the stale pin: %r" % (dispatched,)
    finally:
        (v8._call, v8._focus_verify, v8._is_automation_window,
         v8._last_cdp_url, v8._last_cdp_target_id,
         v8._last_cdp_title, v8._seed_retired, v8._cua_bin) = originals


def test_pass77_fixes():
    # Wrong-window dispatch: after focus moves to a different
    # window, a CDP dispatch must not honor the previous window's
    # pin — it re-pins the focused window's foreground page.
    focus_state = {"window_id": "7", "title": "Tab A"}
    dispatched = []
    page_a = {"cdp_url": "https://a.test/", "text": "BODY A",
              "cdp_target_id": "TA", "cdp_visible": 2}
    page_b = {"cdp_url": "https://b.test/", "text": "BODY B",
              "cdp_target_id": "TB", "cdp_visible": 2}
    def lane_call(argv, timeout_s=None, env_extra=None):
        env = env_extra or {}
        if argv[1] == "cdp-text":
            if env.get("CUA_CDP_TARGET_ID") == "TA":
                return json.dumps(page_a)
            if env.get("CUA_CDP_TARGET_ID") == "TB":
                return json.dumps(page_b)
            return json.dumps(
                page_a if focus_state["window_id"] == "7" else page_b)
        if argv[1] == "cdp-click":
            dispatched.append(env.get("CUA_CDP_TARGET_ID"))
            return json.dumps({"clicked": "#submit"})
        return json.dumps({})
    originals = (v8._call, v8._focus_verify, v8._is_automation_window,
                 v8._last_cdp_url, v8._last_cdp_target_id,
                 v8._last_cdp_title, v8._seed_retired, v8._cua_bin)
    try:
        v8._call = lane_call
        v8._cua_bin = lambda: "/x/cua-lane"
        v8._focus_verify = lambda fresh=False: dict(
            window_id=focus_state["window_id"],
            title=focus_state["title"])
        v8._is_automation_window = lambda wid, pid=None: wid == "7"
        v8._last_cdp_url, v8._last_cdp_target_id = "", ""
        v8._last_cdp_title = ""
        v8._seed_retired = False
        assert v8._cdp_text() == "BODY A"
        assert v8._last_cdp_target_id == "TA"
        # Focus moves to another window entirely.
        focus_state.update(window_id="9", title="Other Window")
        v8._cua("cdp-click", "#submit")
        assert dispatched == ["TB"], \
            "dispatch honored the wrong window's pin: %r" % (dispatched,)
    finally:
        (v8._call, v8._focus_verify, v8._is_automation_window,
         v8._last_cdp_url, v8._last_cdp_target_id,
         v8._last_cdp_title, v8._seed_retired, v8._cua_bin) = originals


def test_pass78_fixes():
    # (a) wmctrl hex window ids must compare equal to the decimal
    # XID: _same_window and _is_automation_window are numeric.
    assert v8._same_window("0x00000007", 7)
    assert v8._same_window("0x00000007", "7")
    assert not v8._same_window("0x00000009", "7")
    saved_tab = v8._automation_tab
    try:
        v8._automation_tab = lambda: {"window_id": 7}
        assert v8._is_automation_window("0x00000007")
        assert not v8._is_automation_window("0x00000009")
    finally:
        v8._automation_tab = saved_tab
    # (b) Return-to-window-A: a pin re-earned in window B must not
    # survive focus returning to window A.
    focus_state = {"window_id": "7", "title": "Tab A"}
    dispatched = []
    page_a = {"cdp_url": "https://a.test/", "text": "BODY A",
              "cdp_target_id": "TA", "cdp_visible": 2}
    page_b = {"cdp_url": "https://b.test/", "text": "BODY B",
              "cdp_target_id": "TB", "cdp_visible": 2}
    def lane_call(argv, timeout_s=None, env_extra=None):
        env = env_extra or {}
        if argv[1] == "cdp-text":
            if env.get("CUA_CDP_TARGET_ID") == "TA":
                return json.dumps(page_a)
            if env.get("CUA_CDP_TARGET_ID") == "TB":
                return json.dumps(page_b)
            return json.dumps(
                page_a if focus_state["window_id"] == "7" else page_b)
        if argv[1] == "cdp-click":
            dispatched.append(env.get("CUA_CDP_TARGET_ID"))
            return json.dumps({"clicked": "#submit"})
        return json.dumps({})
    originals = (v8._call, v8._focus_verify, v8._is_automation_window,
                 v8._last_cdp_url, v8._last_cdp_target_id,
                 v8._last_cdp_title, v8._last_cdp_window_id,
                 v8._seed_retired, v8._cua_bin)
    try:
        v8._call = lane_call
        v8._cua_bin = lambda: "/x/cua-lane"
        v8._focus_verify = lambda fresh=False: dict(
            window_id=focus_state["window_id"],
            title=focus_state["title"])
        v8._is_automation_window = lambda wid, pid=None: wid == "7"
        v8._last_cdp_url, v8._last_cdp_target_id = "", ""
        v8._last_cdp_title, v8._last_cdp_window_id = "", ""
        v8._seed_retired = False
        assert v8._cdp_text() == "BODY A"
        assert v8._last_cdp_target_id == "TA"
        focus_state.update(window_id="9", title="Other Window")
        v8._cua("cdp-click", "#submit")
        assert dispatched == ["TB"], dispatched
        # Focus returns to window A: the next dispatch must target
        # A's page again, not the still-visible B pin.
        focus_state.update(window_id="7", title="Tab A")
        v8._cua("cdp-click", "#submit")
        assert dispatched == ["TB", "TA"], \
            "return-to-A dispatch kept B's pin: %r" % (dispatched,)
    finally:
        (v8._call, v8._focus_verify, v8._is_automation_window,
         v8._last_cdp_url, v8._last_cdp_target_id,
         v8._last_cdp_title, v8._last_cdp_window_id,
         v8._seed_retired, v8._cua_bin) = originals


def test_pass79_fixes():
    # Foreign browser window: the lane endpoint serves only window
    # A's browser. When an independent window B is focused, A's
    # page reports visible=1 — a dispatch must REFUSE rather than
    # click into the background window.
    focus_state = {"window_id": "7", "title": "Tab A"}
    dispatched = []
    def page_a():
        return {"cdp_url": "https://a.test/", "text": "BODY A",
                "cdp_target_id": "TA",
                "cdp_visible": 2 if focus_state["window_id"] == "7" else 1}
    def lane_call(argv, timeout_s=None, env_extra=None):
        if argv[1] == "cdp-text":
            return json.dumps(page_a())
        if argv[1] == "cdp-click":
            dispatched.append((env_extra or {}).get("CUA_CDP_TARGET_ID"))
            return json.dumps({"clicked": "#submit"})
        return json.dumps({})
    originals = (v8._call, v8._focus_verify, v8._is_automation_window,
                 v8._last_cdp_url, v8._last_cdp_target_id,
                 v8._last_cdp_title, v8._last_cdp_window_id,
                 v8._last_cdp_visible, v8._seed_retired, v8._cua_bin)
    try:
        v8._call = lane_call
        v8._cua_bin = lambda: "/x/cua-lane"
        v8._focus_verify = lambda fresh=False: dict(
            window_id=focus_state["window_id"],
            title=focus_state["title"])
        v8._is_automation_window = lambda wid, pid=None: wid == "7"
        v8._last_cdp_url, v8._last_cdp_target_id = "", ""
        v8._last_cdp_title, v8._last_cdp_window_id = "", ""
        v8._last_cdp_visible = None
        v8._seed_retired = False
        assert v8._cdp_text() == "BODY A"
        assert v8._last_cdp_target_id == "TA"
        v8._cua("cdp-click", "#submit")  # same window: allowed
        assert dispatched == ["TA"], dispatched
        focus_state.update(window_id="9", title="Foreign Window")
        refused = False
        try:
            v8._cua("cdp-click", "#submit")
        except RuntimeError as exc:
            refused = "foreground" in str(exc)
        assert refused, "foreign-window dispatch was not refused"
        assert dispatched == ["TA"], \
            "a click reached the background window: %r" % (dispatched,)
    finally:
        (v8._call, v8._focus_verify, v8._is_automation_window,
         v8._last_cdp_url, v8._last_cdp_target_id,
         v8._last_cdp_title, v8._last_cdp_window_id,
         v8._last_cdp_visible, v8._seed_retired, v8._cua_bin) = originals


def test_pass81_fixes():
    # (#5) A deadline inside the window-PID lookup is a real
    # ActionTimeout, not an empty identity; the focus dict carries
    # window_pid for the observe-cache binding.
    saved_call = v8._call
    saved_cache = dict(v8._focus_cache)
    def call_pid_timeout(argv, timeout_s=None, env_extra=None):
        if argv[:2] == ["xdotool", "getactivewindow"]:
            return "7"
        if argv[:2] == ["xdotool", "getwindowname"]:
            return "Some Window"
        if argv[:2] == ["xdotool", "getwindowpid"]:
            raise v8.ActionTimeout()
        raise AssertionError(argv)
    try:
        v8._call = call_pid_timeout
        try:
            v8._focus_verify(fresh=True)
            raise AssertionError("PID-lookup deadline was swallowed")
        except v8.ActionTimeout:
            pass
    finally:
        v8._call = saved_call
        v8._focus_cache.clear()
        v8._focus_cache.update(saved_cache)
    # (#1) A FAILED dispatch-validation read must not let the stale
    # visibility stamp (2) authorize a click.
    focus_state = {"window_id": "7", "title": "Tab A"}
    dispatched = []
    fail_reads = {"on": False}
    page_a = {"cdp_url": "https://a.test/", "text": "BODY A",
              "cdp_target_id": "TA", "cdp_visible": 2}
    def lane_call(argv, timeout_s=None, env_extra=None):
        if argv[1] == "cdp-text":
            if fail_reads["on"]:
                raise RuntimeError("evaluate failed")
            return json.dumps(page_a)
        if argv[1] == "cdp-click":
            dispatched.append((env_extra or {}).get("CUA_CDP_TARGET_ID"))
            return json.dumps({"clicked": "#submit"})
        return json.dumps({})
    originals = (v8._call, v8._focus_verify, v8._is_automation_window,
                 v8._last_cdp_url, v8._last_cdp_target_id,
                 v8._last_cdp_title, v8._last_cdp_window_id,
                 v8._last_cdp_visible, v8._seed_retired, v8._cua_bin)
    try:
        v8._call = lane_call
        v8._cua_bin = lambda: "/x/cua-lane"
        v8._focus_verify = lambda fresh=False: dict(
            window_id=focus_state["window_id"],
            title=focus_state["title"])
        v8._is_automation_window = lambda wid, pid=None: wid == "7"
        v8._last_cdp_url, v8._last_cdp_target_id = "", ""
        v8._last_cdp_title, v8._last_cdp_window_id = "", ""
        v8._last_cdp_visible = None
        v8._seed_retired = False
        assert v8._cdp_text() == "BODY A"
        assert v8._last_cdp_visible == 2
        fail_reads["on"] = True
        refused = False
        try:
            v8._cua("cdp-click", "#submit")
        except RuntimeError as exc:
            refused = "foreground" in str(exc)
        assert refused, "failed validation still authorized a click"
        assert dispatched == [], dispatched
    finally:
        (v8._call, v8._focus_verify, v8._is_automation_window,
         v8._last_cdp_url, v8._last_cdp_target_id,
         v8._last_cdp_title, v8._last_cdp_window_id,
         v8._last_cdp_visible, v8._seed_retired, v8._cua_bin) = originals


def test_pass84_fixes():
    # The lane's pre-dispatch "selector not visible" rejection
    # (zero-area box model) must revoke the dispatch claim and let
    # the ladder advance, exactly like "selector not found".
    import socket as _socket
    listener = _socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    saved_ws = sys.modules.get("websocket")
    sys.modules.pop("websocket", None)
    originals = (v8._cua, v8._cua_bin, v8._cdp_port)
    token = v8._deadline.set(time.monotonic() + 30)
    try:
        import websocket  # noqa: F401  (real package, not the stub)
        v8._cua_bin = lambda: "/x/cua-lane"
        v8._cdp_port = lambda: port
        def cua_hidden(command, target):
            raise RuntimeError("selector not visible: " + target)
        v8._cua = cua_hidden
        attempted, via, detail = v8.rung_cdp(
            {"selector": "#hidden"}, "browser_cdp")
        assert attempted is False and detail == "skipped=target_not_found", \
            (attempted, via, detail)
    finally:
        v8._deadline.reset(token)
        (v8._cua, v8._cua_bin, v8._cdp_port) = originals
        listener.close()
        if saved_ws is not None:
            sys.modules["websocket"] = saved_ws


def test_pass86_fixes():
    # (#1) Automation-window identity is (XID, PID) when both PIDs
    # are known: a reused XID under a new browser PID is not the
    # automation window.
    saved_tab = v8._automation_tab
    try:
        v8._automation_tab = lambda: {
            "window_id": 100,
            "pid_property": "_NET_WM_PID(CARDINAL) = 111"}
        assert v8._automation_pid() == "111"
        assert v8._is_automation_window("100", "111")
        assert not v8._is_automation_window("100", "222")
        assert v8._is_automation_window("100", None)  # XID-only fallback
        assert not v8._is_automation_window("101", "111")
    finally:
        v8._automation_tab = saved_tab
    # (#2) AT-SPI frame selection: a positively INACTIVE namesake
    # must not be walked; an unreadable-state namesake keeps the
    # legacy fallback; an active namesake wins.
    import types as _types
    fake_pyatspi = _types.SimpleNamespace(STATE_ACTIVE=8)
    class FakeState:
        def __init__(self, active):
            self._active = active
        def contains(self, state):
            return self._active
    class FakeFrame:
        def __init__(self, name, mode):
            self.name = name
            self._mode = mode
        def getRoleName(self):
            return "frame"
        def getState(self):
            if self._mode == "unknown":
                raise RuntimeError("no state")
            return FakeState(self._mode == "active")
        def queryState(self):
            raise RuntimeError("no queryState")
    saved_mod = sys.modules.get("pyatspi")
    try:
        sys.modules["pyatspi"] = fake_pyatspi
        inactive = FakeFrame("Editor", "inactive")
        unknown = FakeFrame("Editor", "unknown")
        active = FakeFrame("Editor", "active")
        assert v8._atspi_focused_frame([[inactive]], "Editor") is None
        assert v8._atspi_focused_frame(
            [[inactive, unknown]], "Editor") is unknown
        assert v8._atspi_focused_frame(
            [[inactive, active]], "Editor") is active
    finally:
        if saved_mod is not None:
            sys.modules["pyatspi"] = saved_mod
        else:
            sys.modules.pop("pyatspi", None)


def test_pass89_fixes():
    # (#1) Deterministic form of the detached-survivor race: the
    # outer deadline (50ms) expires DURING a spawn whose root exits
    # at ~150ms inside a generous runner budget. The caller surfaces
    # ActionTimeout — and the detached child must be dead.
    def sleep10_pids():
        hits = []
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open("/proc/%s/cmdline" % pid, "rb") as fh:
                    if fh.read().replace(b"\0", b" ").strip() == b"sleep 10":
                        hits.append(int(pid))
            except Exception:
                continue
        return hits
    token = v8._deadline.set(time.monotonic() + 30)
    try:
        with environment(CU_DRY_RUN=None):
            surfaced = False
            try:
                with v8._within(0.05):
                    v8._call(["bash", "-c",
                              "setsid sleep 10 </dev/null >/dev/null 2>&1 "
                              "& sleep 0.15"], 5)
            except v8.ActionTimeout:
                surfaced = True
            assert surfaced, "expected the deadline to surface"
    finally:
        v8._deadline.reset(token)
    time.sleep(0.2)
    survivors = sleep10_pids()
    for pid in survivors:
        try:
            os.kill(pid, 9)
        except Exception:
            pass
    assert survivors == [], "detached child survived: %r" % (survivors,)
    # (#2) The PID lookup also propagates the runner-flavor timeout.
    saved_call = v8._call
    saved_cache = dict(v8._focus_cache)
    def call_pid_runner_timeout(argv, timeout_s=None, env_extra=None):
        if argv[:2] == ["xdotool", "getactivewindow"]:
            return "7"
        if argv[:2] == ["xdotool", "getwindowname"]:
            return "Some Window"
        if argv[:2] == ["xdotool", "getwindowpid"]:
            raise subprocess.TimeoutExpired(argv, 0.01)
        raise AssertionError(argv)
    try:
        v8._call = call_pid_runner_timeout
        try:
            v8._focus_verify(fresh=True)
            raise AssertionError("runner-flavor PID timeout swallowed")
        except subprocess.TimeoutExpired:
            pass
    finally:
        v8._call = saved_call
        v8._focus_cache.clear()
        v8._focus_cache.update(saved_cache)


def test_pass91_fixes():
    # (#3) A deadline during the safe-paste ctrl+v dispatch keeps
    # its TIMEOUT classification instead of being wrapped as a
    # partial-paste abort.
    record = []
    def cua_paste_timeout(*argv):
        if argv[0] == "type":
            raise FileNotFoundError("no cua type")  # engage paste path
        if argv[:2] == ("clip", "set"):
            return ""
        if argv[:2] == ("key", "ctrl+v"):
            raise v8.ActionTimeout()
        if argv[:2] == ("clip", "get"):
            return {"text": ""}
        return ""
    originals = (v8.RUNNER, v8._cua, v8._grab, v8._latest_shot)
    try:
        v8.RUNNER = focus_runner(record)
        v8._cua = cua_paste_timeout
        v8._grab = lambda: b"frame" * 64
        v8._latest_shot = lambda: b"frame" * 64
        with environment(CU_DRY_RUN=None,
                         YDOTOOL_SOCKET="/nonexistent.sock"):
            result = v8.kbd_agent("type_verify", {
                "text": "hello", "engine": "auto", "safe_clipboard": True})
        assert "TIMEOUT" in result, result
        assert "paste dispatch failed" not in result, result
    finally:
        (v8.RUNNER, v8._cua, v8._grab, v8._latest_shot) = originals


def test_pass94_fixes():
    # Deferred spawn-cleanup judgment at the tool boundary: a spawn
    # that completed in-budget inside a tool call is tree-killed if
    # the caller's deadline expires later in the SAME call, and left
    # alone when it does not.
    def sleep10_pids():
        hits = []
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open("/proc/%s/cmdline" % pid, "rb") as fh:
                    if fh.read().replace(b"\0", b" ").strip() == b"sleep 10":
                        hits.append(int(pid))
            except Exception:
                continue
        return hits
    def impl_spawn_and_wait(wait_s):
        def impl(_tool_call):
            with environment(CU_DRY_RUN=None):
                v8._call(["bash", "-c",
                          "setsid sleep 10 </dev/null >/dev/null 2>&1 "
                          "& sleep 0.05"], 5)
            time.sleep(wait_s)
            return "done"
        return impl
    saved_impl = v8._run_tool_impl
    call_json = json.dumps({"tool": "shell", "cmd": "true"})
    try:
        # Deadline expires after the in-budget spawn: child dies.
        v8._run_tool_impl = impl_spawn_and_wait(0.35)
        token = v8._deadline.set(time.monotonic() + 0.25)
        try:
            v8.run_tool(call_json)
        finally:
            v8._deadline.reset(token)
        time.sleep(0.2)
        survivors = sleep10_pids()
        assert survivors == [], \
            "boundary sweep missed the child: %r" % (survivors,)
        # Generous deadline: the detached child is intentional.
        v8._run_tool_impl = impl_spawn_and_wait(0.05)
        token = v8._deadline.set(time.monotonic() + 30)
        try:
            v8.run_tool(call_json)
        finally:
            v8._deadline.reset(token)
        time.sleep(0.2)
        survivors = sleep10_pids()
        assert len(survivors) == 1, survivors
        for pid in survivors:
            os.kill(pid, 9)
    finally:
        v8._run_tool_impl = saved_impl
        for pid in sleep10_pids():
            try:
                os.kill(pid, 9)
            except Exception:
                pass


def test_pass98_fixes():
    # (B) A timeout in _cua's pre-dispatch validation (focus probe or
    # validation read) is PRE-dispatch: PreDispatchTimeout, and the
    # runner is never invoked — the rung must not claim an input.
    calls = []
    def recording_runner(argv, timeout_s, env_extra=None):
        calls.append([str(a) for a in argv])
        return (0, "{}", "")
    saved = (v8.RUNNER, v8._focus_verify, v8._cdp_text,
             v8._last_cdp_url, v8._last_cdp_target_id,
             v8._last_cdp_window_id, v8._last_cdp_title,
             v8._last_cdp_visible)
    try:
        v8.RUNNER = recording_runner
        v8._last_cdp_url = "https://pinned.test/"
        v8._last_cdp_target_id = "TID-P98"
        v8._last_cdp_window_id = "777"
        v8._last_cdp_title = "Pinned"
        v8._last_cdp_visible = 2
        v8._focus_verify = lambda *a, **k: {
            "window_id": "777", "window_pid": "4242", "title": "Pinned"}
        def timing_out_text():
            raise TimeoutError("validation read timed out")
        v8._cdp_text = timing_out_text
        with environment(CU_DRY_RUN=None):
            with v8._agent_session():
                try:
                    v8._cua("cdp-clicktext", "Submit")
                    raise AssertionError("validation timeout must raise")
                except v8.PreDispatchTimeout:
                    pass
                except Exception as exc:
                    raise AssertionError(
                        "expected PreDispatchTimeout, got %r" % (exc,))
        assert not calls, calls
    finally:
        (v8.RUNNER, v8._focus_verify, v8._cdp_text,
         v8._last_cdp_url, v8._last_cdp_target_id,
         v8._last_cdp_window_id, v8._last_cdp_title,
         v8._last_cdp_visible) = saved
    # (C) PID-reuse guard: a completed root whose pid now belongs to
    # an unrelated process (different live start time) must not be
    # signaled — nor its group, nor its new descendants.
    import subprocess as _sp
    stranger = _sp.Popen(["sleep", "30"])
    try:
        live_start = v8._pid_start(stranger.pid)
        assert live_start
        v8._ACTIVE_ROOTS.add(stranger.pid)
        # Recorded start in the PAST and the replacement NOT in the
        # baseline: without the pass-99 anchor protection, the
        # subreaper lifetime sweep itself would claim the stranger
        # as an orphan of the dead root (Codex trio-2 pass-99).
        fake = types.SimpleNamespace(
            pid=stranger.pid, _cu_baseline=set(),
            _cu_root_start=max(1, live_start - 5000))
        v8._kill_tree(fake)
        assert stranger.poll() is None, \
            "cleanup signaled a recycled pid's replacement"
    finally:
        v8._ACTIVE_ROOTS.discard(stranger.pid)
        stranger.kill()
        stranger.wait()
    # (D) Lane release refuses a recycled XID (same id/class, new
    # pid) and restores when the identity truly matches.
    import importlib.machinery
    import importlib.util
    lane_path = str(Path(__file__).resolve().parent.parent / "src" / "cua-lane")
    loader = importlib.machinery.SourceFileLoader("cualane98", lane_path)
    spec = importlib.util.spec_from_loader("cualane98", loader)
    lane = importlib.util.module_from_spec(spec)
    loader.exec_module(lane)
    focused = []
    saved_lane = (lane.clients, lane.window_class, lane.window_pid,
                  lane.focus, lane.save_state)
    try:
        lane.clients = lambda: [424242]
        lane.window_class = lambda wid: "WM_CLASS(STRING) = \"app\""
        lane.focus = lambda wid: focused.append(wid)
        lane.save_state = lambda state: None
        args = lane.parser().parse_args(["release"])
        state = {"previous_window": {
            "id": 424242, "title": "T",
            "class": "WM_CLASS(STRING) = \"app\"", "pid": 1111}}
        lane.window_pid = lambda wid: 2222
        try:
            lane.dispatch(args, dict(state))
            raise AssertionError("recycled XID must be refused")
        except RuntimeError:
            pass
        assert not focused, focused
        lane.window_pid = lambda wid: 1111
        out = lane.dispatch(args, dict(state))
        assert out == {"restored": True}, out
        assert focused == [424242], focused
    finally:
        (lane.clients, lane.window_class, lane.window_pid,
         lane.focus, lane.save_state) = saved_lane


def test_pass100_fixes():
    # (1) Forest attribution is (pid, start): a descendant observed
    # under a RECYCLED root (same number, later start) and orphaned
    # onto the executor is protected, not swept as the original
    # command's orphan.
    import subprocess as _sp
    replacement = _sp.Popen(["sleep", "30"])
    orphan = _sp.Popen(["sleep", "30"])
    try:
        r_start = v8._pid_start(replacement.pid)
        o_start = v8._pid_start(orphan.pid)
        assert r_start and o_start
        v8._ACTIVE_ROOTS.add(replacement.pid)
        v8._FOREST_OWNER[orphan.pid] = (replacement.pid, r_start, o_start)
        fake = types.SimpleNamespace(
            pid=replacement.pid, _cu_baseline=set(),
            _cu_root_start=max(1, r_start - 5000))
        v8._kill_tree(fake)
        assert replacement.poll() is None, "replacement was signaled"
        assert orphan.poll() is None, (
            "recycled root's orphaned descendant was swept; "
            "forest record now %r, seeded %r, fake root_start %r"
            % (v8._FOREST_OWNER.get(orphan.pid),
               (replacement.pid, r_start, o_start),
               max(1, r_start - 5000)))
    finally:
        v8._FOREST_OWNER.pop(orphan.pid, None)
        v8._ACTIVE_ROOTS.discard(replacement.pid)
        for proc in (replacement, orphan):
            proc.kill()
            proc.wait()
    # (2) verify with file == against uses the pre-edit snapshot:
    # corruption outside the span fails, a length-changing edit
    # passes.
    import tempfile as _tf
    with _tf.TemporaryDirectory() as td:
        path = os.path.join(td, "f.txt")
        newf = os.path.join(td, "new.txt")
        with open(path, "w") as fh:
            fh.write("l1\nl2\nl3\nl4\nl5\n")
        with open(newf, "w") as fh:
            fh.write("CHANGED\n")
        out = v8._run_tool_impl(json.dumps(
            {"tool": "edit", "file": path, "lines": "2-2",
             "new_file": newf}))
        assert out.startswith("OK"), out
        with open(path, "a") as fh:
            fh.write("CORRUPT\n")
        out = v8._run_tool_impl(json.dumps(
            {"tool": "verify", "file": path, "against": path,
             "lines": "2-2", "new_file": newf}))
        verdict = json.loads(out)
        assert verdict["ok"] is False, verdict
        # Length-changing edit against a fresh snapshot verifies.
        with open(path, "w") as fh:
            fh.write("l1\nl2\nl3\nl4\nl5\n")
        with open(newf, "w") as fh:
            fh.write("X1\nX2\nX3\n")
        out = v8._run_tool_impl(json.dumps(
            {"tool": "edit", "file": path, "lines": "2-2",
             "new_file": newf}))
        assert out.startswith("OK"), out
        out = v8._run_tool_impl(json.dumps(
            {"tool": "verify", "file": path, "against": path,
             "lines": "2-2", "new_file": newf}))
        verdict = json.loads(out)
        assert verdict["ok"] is True, verdict
        v8._snapshots.pop(path, None)
    # (3) The write receipt counts the DELIVERED (truncated) text.
    saved_chat = v8._chat
    try:
        v8._chat = lambda messages, n_predict=0: (
            "abcdefghij " * 700, None, None)
        out = v8._run_tool_impl(json.dumps(
            {"tool": "write", "prompt": "p", "min_words": 1}))
        delivered = out.split("TEXT:\n", 1)[1]
        claimed = int(out.split("WORD_COUNT: ")[1].split(" ")[0])
        assert claimed == len(delivered.split()), (claimed, out[:80])
        assert claimed < 700, claimed
    finally:
        v8._chat = saved_chat


def test_pass101_fixes():
    # (1) Cleanup validates known members by start time each round:
    # a descendant pid recycled between the snapshot and a round is
    # never signaled, and its new children are never walked.
    entry_map = ({100: [200]}, {100: 1000, 200: 2000}, {})
    # Round state: the root is gone, pid 200 now names a DIFFERENT
    # process (start 9999, parented elsewhere) with its own child
    # 300. Only the bare-pid `known` set still points at it.
    round_map = ({1: [200], 200: [300]},
                 {200: 9999, 300: 3000}, {})
    maps = [entry_map, round_map]
    kill_calls = []
    saved = (v8._children_map, v8.os.kill, v8.os.killpg)
    def fake_map():
        return maps.pop(0) if len(maps) > 1 else maps[0]
    try:
        v8._children_map = fake_map
        v8.os.kill = lambda pid, sig: kill_calls.append(pid)
        v8.os.killpg = lambda pid, sig: kill_calls.append(("pg", pid))
        fake = types.SimpleNamespace(
            pid=100, _cu_baseline=set(), _cu_root_start=1000)
        v8._kill_tree(fake)
    finally:
        (v8._children_map, v8.os.kill, v8.os.killpg) = saved
    assert 200 not in kill_calls and 300 not in kill_calls, kill_calls
    # (2)+(3) Path aliases hit the same snapshot; CRLF endings
    # outside the span survive an edit byte-for-byte.
    import tempfile as _tf
    with _tf.TemporaryDirectory() as td:
        path = os.path.join(td, "f.txt")
        with open(path, "wb") as fh:
            fh.write(b"a\r\nb\r\nc\r\n")
        out = v8._run_tool_impl(json.dumps(
            {"tool": "edit", "file": path, "lines": "2-2",
             "new": "X\n"}))
        assert out.startswith("OK"), out
        with open(path, "rb") as fh:
            body = fh.read()
        assert body == b"a\r\nX\nc\r\n", body
        alias = os.path.join(td, ".", "f.txt")
        with open(path, "a") as fh:
            fh.write("CORRUPT\n")
        out = v8._run_tool_impl(json.dumps(
            {"tool": "verify", "file": alias, "against": alias,
             "lines": "2-2", "new_file": path}))
        # new_file here is only a text source; verdict must be False
        # because the snapshot (via the canonical path) sees the
        # corruption the alias tried to hide.
        verdict = json.loads(out)
        assert verdict["ok"] is False, verdict
        v8._snapshots.pop(os.path.realpath(path), None)
    # (4) A dead screenshot path degrades verification, never the
    # dispatch: the xdotool rung still clicks with verify on.
    dispatched = []
    saved2 = (v8._grab, v8._ensure_focus, v8._deadline)
    saved_rungs = {name: v8.__dict__["rung_" + name]
                   for name in ("keyboard", "xdotool", "cua", "atspi",
                                "vision") if "rung_" + name in v8.__dict__}
    try:
        def dead_grab():
            raise RuntimeError("screenshot unavailable")
        v8._grab = dead_grab
        v8._ensure_focus = lambda args: {"surface": "native"}
        v8.__dict__["rung_keyboard"] = lambda a, s: (False, "keyboard", "skip")
        def fake_xdotool(a, s):
            dispatched.append(1)
            return (True, "xdotool", "")
        v8.__dict__["rung_xdotool"] = fake_xdotool
        token = v8._deadline.set(time.monotonic() + 30)
        try:
            with environment(CU_DRY_RUN=None):
                receipt = v8._mouse_action(
                    "click", {"x": 10, "y": 10, "surface": "native"})
        finally:
            v8._deadline.reset(token)
        assert dispatched, receipt
        assert receipt["ok"] is True, receipt
        assert "screenshot_unavailable" in receipt.get("detail", ""), receipt
    finally:
        (v8._grab, v8._ensure_focus, v8._deadline) = saved2
        for name, fn in saved_rungs.items():
            v8.__dict__["rung_" + name] = fn
    # (5) Lane new_tab adopts only the profile-owning Chromium
    # window; an unrelated window appearing first is ignored.
    import importlib.machinery
    import importlib.util
    import subprocess as _sp
    lane_path = str(Path(__file__).resolve().parent.parent / "src" / "cua-lane")
    loader = importlib.machinery.SourceFileLoader("cualane101", lane_path)
    spec = importlib.util.spec_from_loader("cualane101", loader)
    lane = importlib.util.module_from_spec(spec)
    loader.exec_module(lane)
    sleeper = _sp.Popen(["bash", "-c",
                         "exec -a --user-data-dir=/profX sleep 30"])
    saved_lane = (lane.clients, lane.window_class, lane.window_pid,
                  lane.active, lane.save_state, lane.focus,
                  lane.subprocess.Popen, lane.run)
    polls = {"n": 0}
    try:
        lane.window_class = lambda wid: "Chromium"
        lane.window_pid = lambda wid: {222: 999999, 333: sleeper.pid}[wid]
        lane.active = lambda: 111
        lane.save_state = lambda state: None
        lane.focus = lambda wid: None
        lane.run = lambda *a, **k: ""
        lane.subprocess.Popen = lambda *a, **k: None
        def counting_clients():
            polls["n"] += 1
            return [111] if polls["n"] < 2 else [111, 222, 333]
        lane.clients = counting_clients
        # Let the sleeper exec so its /proc cmdline carries the flag.
        for _ in range(40):
            if lane._window_is_automation_browser(333):
                break
            time.sleep(0.05)
        with environment(CUA_PROFILE="/profX"):
            state = {}
            try:
                out = lane.new_tab("https://x.test/", state)
            except AssertionError:
                raise
            except Exception:
                out = None
        assert out is not None and out["created"] is True, out
        assert state["automation_tab"]["window_id"] == 333, state
    finally:
        (lane.clients, lane.window_class, lane.window_pid,
         lane.active, lane.save_state, lane.focus,
         lane.subprocess.Popen, lane.run) = saved_lane
        sleeper.kill()
        sleeper.wait()


def test_pass102_fixes():
    # (1) Mid-sweep root reappearance: the anchor's recycled check
    # is per-round. Mocked maps: entry has a genuine orphan (500,
    # the test's own child slot) and NO root; round maps show the
    # root pid reappearing with a new start and a child (300).
    me = os.getpid()
    v8._ensure_subreaper()
    entry_map = ({me: [500]}, {500: 2000}, {})
    round_map = ({me: [500, 100], 100: [300]},
                 {500: 2000, 100: 9000, 300: 9500}, {})
    maps = [entry_map, round_map]
    kill_calls = []
    saved = (v8._children_map, v8.os.kill, v8.os.killpg)
    def fake_map2():
        return maps.pop(0) if len(maps) > 1 else maps[0]
    try:
        v8._children_map = fake_map2
        v8.os.kill = lambda pid, sig: kill_calls.append(pid)
        v8.os.killpg = lambda pid, sig: kill_calls.append(("pg", pid))
        fake = types.SimpleNamespace(
            pid=100, _cu_baseline=set(), _cu_root_start=1000)
        v8._kill_tree(fake)
    finally:
        (v8._children_map, v8.os.kill, v8.os.killpg) = saved
    assert 500 in kill_calls, kill_calls  # genuine orphan swept
    assert 300 not in kill_calls, kill_calls  # replacement's child safe
    # (2) Lane profile match is an exact cmdline token.
    import importlib.machinery
    import importlib.util
    import subprocess as _sp
    lane_path = str(Path(__file__).resolve().parent.parent / "src" / "cua-lane")
    loader = importlib.machinery.SourceFileLoader("cualane102", lane_path)
    spec = importlib.util.spec_from_loader("cualane102", loader)
    lane = importlib.util.module_from_spec(spec)
    loader.exec_module(lane)
    foreign = _sp.Popen(["bash", "-c",
                         "exec -a --user-data-dir=/profX-foreign sleep 30"])
    exact = _sp.Popen(["bash", "-c",
                       "exec -a --user-data-dir=/profX sleep 30"])
    saved_wp = lane.window_pid
    try:
        lane.window_pid = lambda wid: {1: foreign.pid, 2: exact.pid}[wid]
        with environment(CUA_PROFILE="/profX"):
            for _ in range(40):
                if lane._window_is_automation_browser(2):
                    break
                time.sleep(0.05)
            assert lane._window_is_automation_browser(1) is False
            assert lane._window_is_automation_browser(2) is True
    finally:
        lane.window_pid = saved_wp
        for proc in (foreign, exact):
            proc.kill()
            proc.wait()
    # (3) Loop bookkeeping: dead screenshots must not replace
    # completed click receipts with ERROR results.
    clicks = {"n": 0}
    def loop_runner(argv, *a, **kw):
        argv = [str(x) for x in argv]
        if "click" in argv:
            clicks["n"] += 1
            return 0, "", ""
        return focus_runner([])(argv, *a, **kw)
    def dead_grab2():
        raise RuntimeError("screenshot unavailable")
    plan = ["mouse_agent", "mouse_agent", "mouse_agent", "done"]
    seen_results = []
    def loop_chat(task, steps, last_result, note):
        seen_results.append(last_result)
        tool = plan[len(seen_results) - 1]
        if tool == "done":
            return json.dumps({"tool": "done", "result": "clicked thrice"}), "stop", 0.0
        return json.dumps({"tool": "mouse_agent", "action": "click",
                           "args": {"x": 4 + len(seen_results), "y": 5}}), \
            "stop", 0.0
    progress = os.path.join(tempfile.mkdtemp(prefix="cu-trio2-"), "p.jsonl")
    hash_calls = {"n": 0}
    real_hash = v8.compute_state_hash
    def varying_hash(title, url, content, shot=None):
        # With screenshots dead, every step's sensor state is
        # identical and the loop's stall detector would (rightly)
        # stop the run; model a live desktop by varying the hash.
        hash_calls["n"] += 1
        return real_hash(title, url, content + "#%d" % hash_calls["n"],
                         shot)
    originals = (v8.RUNNER, v8._grab, v8.call_qwen, v8.compute_state_hash)
    try:
        v8.RUNNER, v8._grab, v8.call_qwen = loop_runner, dead_grab2, loop_chat
        v8.compute_state_hash = varying_hash
        with environment(CU_DRY_RUN=None, CU_PROGRESS_LOG=progress):
            with contextlib.redirect_stdout(io.StringIO()):
                result = v8.run_loop("three clicks", max_iters=6)
    finally:
        (v8.RUNNER, v8._grab, v8.call_qwen,
         v8.compute_state_hash) = originals
    assert "clicked thrice" in result, result
    assert clicks["n"] == 3, clicks
    assert not any("screenshot unavailable" in (r or "")
                   for r in seen_results), seen_results
    # (4) Unicode line separators: edit + verify agree with the
    # file's own line segmentation.
    import tempfile as _tf
    with _tf.TemporaryDirectory() as td:
        path = os.path.join(td, "u.txt")
        newf = os.path.join(td, "new.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("a\nb\nc\n")
        with open(newf, "w", encoding="utf-8") as fh:
            fh.write("X\u2028Y\n")
        out = v8._run_tool_impl(json.dumps(
            {"tool": "edit", "file": path, "lines": "2-2",
             "new_file": newf}))
        assert out.startswith("OK"), out
        out = v8._run_tool_impl(json.dumps(
            {"tool": "verify", "file": path, "against": path,
             "lines": "2-2", "new_file": newf}))
        verdict = json.loads(out)
        assert verdict["ok"] is True, verdict
        v8._snapshots.pop(os.path.realpath(path), None)
    # (5) A failed ambiguous click logs changed=false: with a claim
    # marker and no frames at all, the rung event must not claim
    # progress.
    def claim_runner(argv, *a, **kw):
        argv = [str(x) for x in argv]
        if "mousemove" in argv and "click" in argv:
            raise subprocess.TimeoutExpired(argv, 1)
        return focus_runner([])(argv, *a, **kw)
    progress5 = os.path.join(tempfile.mkdtemp(prefix="cu-trio2-"), "p.jsonl")
    originals5 = (v8.RUNNER, v8._grab, v8._cua)
    try:
        v8.RUNNER, v8._grab, v8._cua = claim_runner, dead_grab2, lambda *a, **k: {}
        with environment(CU_DRY_RUN=None, CU_PROGRESS_LOG=progress5):
            result = json.loads(agent_click())
    finally:
        (v8.RUNNER, v8._grab, v8._cua) = originals5
    assert result["ok"] is False, result
    events = [json.loads(line) for line in open(progress5)]
    rung_events = [e for e in events if e["tool"] == "mouse_agent"
                   and "rung=xdotool" in e["note"]]
    assert rung_events, events
    assert rung_events[-1]["changed"] is False, rung_events


def test_pass103_fixes():
    # (1) Baseline protection is identity-aware: a NEW orphan
    # reusing a baseline pid (different start, attributed to this
    # command's root) is swept; the genuine baseline process (same
    # start) stays protected.
    me = os.getpid()
    v8._ensure_subreaper()
    round_map = ({me: [200, 201]}, {200: 2000, 201: 2222}, {})
    kill_calls = []
    saved = (v8._children_map, v8.os.kill, v8.os.killpg)
    try:
        v8._children_map = lambda: round_map
        v8.os.kill = lambda pid, sig: kill_calls.append(pid)
        v8.os.killpg = lambda pid, sig: kill_calls.append(("pg", pid))
        v8._FOREST_OWNER[200] = (100, 1000, 2000)
        fake = types.SimpleNamespace(
            pid=100, _cu_baseline={200: 1111, 201: 2222},
            _cu_root_start=1000)
        v8._kill_tree(fake)
    finally:
        (v8._children_map, v8.os.kill, v8.os.killpg) = saved
        v8._FOREST_OWNER.pop(200, None)
    assert 200 in kill_calls, kill_calls
    assert 201 not in kill_calls, kill_calls
    # (2) _split_lines matches file readlines(newline="") exactly.
    import tempfile as _tf
    for text in ("a\rb\rc\r", "a\r\nb\nc", "x\u2028y\nz", "a\n\nb",
                 "", "no-newline", "a\rb\n", "a\r\n\rb"):
        with _tf.NamedTemporaryFile("w", newline="", delete=False) as fh:
            fh.write(text)
            tmpname = fh.name
        try:
            with open(tmpname, newline="") as fh:
                expected = fh.readlines()
        finally:
            os.unlink(tmpname)
        assert v8._split_lines(text) == expected, (text, expected)
    # (3) An unreadable expectation file confirms nothing and never
    # raises into the receipt path.
    with _tf.NamedTemporaryFile("w", delete=False) as fh:
        fh.write("hello")
        tmpname = fh.name
    try:
        os.chmod(tmpname, 0)
        assert v8._file_expectation(
            {"expect_file": tmpname, "expect_text_in_file": "hello"}
        ) is False
    finally:
        os.chmod(tmpname, 0o600)
        os.unlink(tmpname)
    # (4)+(5) Lane: modifier aliases hit the key guards; a failed
    # spawn clears automation_pending.
    import importlib.machinery
    import importlib.util
    lane_path = str(Path(__file__).resolve().parent.parent / "src" / "cua-lane")
    loader = importlib.machinery.SourceFileLoader("cualane103", lane_path)
    spec = importlib.util.spec_from_loader("cualane103", loader)
    lane = importlib.util.module_from_spec(spec)
    loader.exec_module(lane)
    saved_lane = (lane.input_window, lane.remember, lane.check_focus,
                  lane.xd, lane.save_state, lane.clients, lane.active,
                  lane.subprocess.Popen)
    xd_calls = []
    try:
        lane.input_window = lambda state, wid=None: 999
        lane.remember = lambda state: None
        lane.check_focus = lambda wid: None
        lane.xd = lambda *a, **k: xd_calls.append(a) or ""
        lane.save_state = lambda state: None
        state = {"automation_tab": {"window_id": 999}}
        for combo in ("Control_L+w", "Alt_L+F4", "Control_L+t"):
            args = lane.parser().parse_args(["key", combo])
            try:
                lane.dispatch(args, dict(state))
                raise AssertionError("alias %s must be refused" % combo)
            except ValueError:
                pass
        args = lane.parser().parse_args(["key", "ctrl+a"])
        lane.dispatch(args, dict(state))
        assert xd_calls, "benign combo must dispatch"
        # (5) spawn failure clears the pending flag.
        lane.clients = lambda: [111]
        lane.active = lambda: 111
        def boom(*a, **k):
            raise FileNotFoundError("no chromium")
        lane.subprocess.Popen = boom
        state2 = {}
        try:
            lane.new_tab("https://x.test/", state2)
            raise AssertionError("spawn failure must propagate")
        except FileNotFoundError:
            pass
        assert "automation_pending" not in state2, state2
    finally:
        (lane.input_window, lane.remember, lane.check_focus,
         lane.xd, lane.save_state, lane.clients, lane.active,
         lane.subprocess.Popen) = saved_lane


def test_pass104_fixes():
    # (1) A failed edit leaves the file byte-identical (atomic
    # replace): an unencodable replacement errors without
    # truncating the destination.
    import tempfile as _tf
    with _tf.TemporaryDirectory() as td:
        path = os.path.join(td, "f.txt")
        with open(path, "w") as fh:
            fh.write("a\nb\nc\n")
        out = v8._run_tool_impl(json.dumps(
            {"tool": "edit", "file": path, "lines": "2-2",
             "new": "\ud800\n"}))
        assert out.startswith("ERROR"), out
        with open(path) as fh:
            assert fh.read() == "a\nb\nc\n"
        v8._snapshots.pop(os.path.realpath(path), None)
    # (3) Unassigned-spawn recovery: a recycled baseline pid
    # carrying this attempt's token is recovered; the genuine
    # baseline incarnation (same start) is skipped.
    import subprocess as _sp
    token = "cu-test-p104"
    env = dict(os.environ, CU_SPAWN_TOKEN=token)
    carrier = _sp.Popen(["sleep", "30"], env=env)
    recovered = []
    saved_kt = v8._kill_tree
    try:
        start = v8._pid_start(carrier.pid)
        assert start
        v8._kill_tree = lambda holder: recovered.append(holder.pid)
        v8._kill_unassigned_spawn(
            {carrier.pid: start - 500}, {1: start - 1000}, token)
        assert recovered == [carrier.pid], recovered
        recovered.clear()
        v8._kill_unassigned_spawn(
            {carrier.pid: start}, {1: start - 1000}, token)
        assert recovered == [], recovered
    finally:
        v8._kill_tree = saved_kt
        carrier.kill()
        carrier.wait()
    # (2)+(4) Lane: keysym spellings hit the key guards; the
    # selector center refuses computed-hidden elements.
    import importlib.machinery
    import importlib.util
    lane_path = str(Path(__file__).resolve().parent.parent / "src" / "cua-lane")
    loader = importlib.machinery.SourceFileLoader("cualane104", lane_path)
    spec = importlib.util.spec_from_loader("cualane104", loader)
    lane = importlib.util.module_from_spec(spec)
    loader.exec_module(lane)
    saved_lane = (lane.input_window, lane.remember, lane.check_focus,
                  lane.xd)
    try:
        lane.input_window = lambda state, wid=None: 999
        lane.remember = lambda state: None
        lane.check_focus = lambda wid: None
        lane.xd = lambda *a, **k: ""
        state = {"automation_tab": {"window_id": 999}}
        for combo in ("alt+0xffc1", "ctrl+U0077", "0xffe3+t"):
            args = lane.parser().parse_args(["key", combo])
            try:
                lane.dispatch(args, dict(state))
                raise AssertionError("keysym %s must be refused" % combo)
            except ValueError:
                pass
    finally:
        (lane.input_window, lane.remember, lane.check_focus,
         lane.xd) = saved_lane
    class FakeCDP:
        def __init__(self, style):
            self.style = style
        def cmd(self, method, **kw):
            if method == "DOM.getDocument":
                return {"root": {"nodeId": 1}}
            if method == "DOM.querySelector":
                return {"nodeId": 2}
            if method == "DOM.resolveNode":
                return {"object": {"objectId": "o1"}}
            if method == "Runtime.callFunctionOn":
                if "arguments" in kw:
                    return {"result": {"value": True}}
                return {"result": {"value": self.style}}
            if method == "DOM.scrollIntoViewIfNeeded":
                return {}
            if method == "DOM.getBoxModel":
                return {"model": {"content": [0, 0, 10, 0, 10, 10, 0, 10]}}
            raise AssertionError(method)
    try:
        lane._cdp_center(FakeCDP("block|hidden"), "#x")
        raise AssertionError("hidden element must be refused")
    except RuntimeError as exc:
        assert "not visible" in str(exc), exc
    center = lane._cdp_center(FakeCDP("block|visible"), "#x")
    assert center[0] == 5.0 and center[1] == 5.0, center


def test_pass105_fixes():
    # (1) A successful atomic edit preserves the file's mode.
    import stat as _stat
    import tempfile as _tf
    with _tf.TemporaryDirectory() as td:
        path = os.path.join(td, "f.sh")
        with open(path, "w") as fh:
            fh.write("a\nb\nc\n")
        os.chmod(path, 0o755)
        out = v8._run_tool_impl(json.dumps(
            {"tool": "edit", "file": path, "lines": "2-2",
             "new": "X\n"}))
        assert out.startswith("OK"), out
        mode = _stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o755, oct(mode)
        # (3) Verify rejects inverted, out-of-bounds, and malformed
        # ranges instead of approving.
        newf = os.path.join(td, "new.txt")
        with open(newf, "w") as fh:
            fh.write("X\n")
        for lines, needle in (("2-1", '"ok": false'),
                              ("1-999", '"ok": false'),
                              ("1-1junk", "ERROR")):
            out = v8._run_tool_impl(json.dumps(
                {"tool": "verify", "file": path, "against": path,
                 "lines": lines, "new_file": newf}))
            assert needle in out, (lines, out)
        v8._snapshots.pop(os.path.realpath(path), None)
    # (2) Uppercase hex keysyms hit the lane key guards.
    import importlib.machinery
    import importlib.util
    lane_path = str(Path(__file__).resolve().parent.parent / "src" / "cua-lane")
    loader = importlib.machinery.SourceFileLoader("cualane105", lane_path)
    spec = importlib.util.spec_from_loader("cualane105", loader)
    lane = importlib.util.module_from_spec(spec)
    loader.exec_module(lane)
    saved_lane = (lane.input_window, lane.remember, lane.check_focus,
                  lane.xd)
    try:
        lane.input_window = lambda state, wid=None: 999
        lane.remember = lambda state: None
        lane.check_focus = lambda wid: None
        lane.xd = lambda *a, **k: ""
        state = {"automation_tab": {"window_id": 999}}
        for combo in ("ctrl+0x57", "ctrl+0x54", "0xffeb+0x44"):
            args = lane.parser().parse_args(["key", combo])
            try:
                lane.dispatch(args, dict(state))
                raise AssertionError("hex %s must be refused" % combo)
            except ValueError:
                pass
    finally:
        (lane.input_window, lane.remember, lane.check_focus,
         lane.xd) = saved_lane


def test_pass106_fixes():
    # (1) Edit rejects malformed ranges like verify does.
    import tempfile as _tf
    with _tf.TemporaryDirectory() as td:
        path = os.path.join(td, "f.txt")
        with open(path, "wb") as fh:
            fh.write(b"a\nb\nc\n")
        out = v8._run_tool_impl(json.dumps(
            {"tool": "edit", "file": path, "lines": "2-2junk",
             "new": "X\n"}))
        assert out.startswith("ERROR"), out
        with open(path, "rb") as fh:
            assert fh.read() == b"a\nb\nc\n"
        # (2) A replacement ending in a bare CR is stored verbatim
        # (no LF appended) and verifies exact.
        with open(path, "wb") as fh:
            fh.write(b"a\rb\rc\r")
        newf = os.path.join(td, "new.txt")
        with open(newf, "wb") as fh:
            fh.write(b"X\r")
        out = v8._run_tool_impl(json.dumps(
            {"tool": "edit", "file": path, "lines": "2-2",
             "new_file": newf}))
        assert out.startswith("OK"), out
        with open(path, "rb") as fh:
            assert fh.read() == b"a\rX\rc\r", fh.read()
        out = v8._run_tool_impl(json.dumps(
            {"tool": "verify", "file": path, "against": path,
             "lines": "2-2", "new_file": newf}))
        verdict = json.loads(out)
        assert verdict["ok"] is True, verdict
        v8._snapshots.pop(os.path.realpath(path), None)
    # (3) Lane selector center: a covered element is refused
    # pre-dispatch; a hit-test-passing element returns its center.
    import importlib.machinery
    import importlib.util
    lane_path = str(Path(__file__).resolve().parent.parent / "src" / "cua-lane")
    loader = importlib.machinery.SourceFileLoader("cualane106", lane_path)
    spec = importlib.util.spec_from_loader("cualane106", loader)
    lane = importlib.util.module_from_spec(spec)
    loader.exec_module(lane)
    class FakeCDP2:
        def __init__(self, hit):
            self.hit = hit
        def cmd(self, method, **kw):
            if method == "DOM.getDocument":
                return {"root": {"nodeId": 1}}
            if method == "DOM.querySelector":
                return {"nodeId": 2}
            if method == "DOM.resolveNode":
                return {"object": {"objectId": "o1"}}
            if method == "Runtime.callFunctionOn":
                if "arguments" in kw:
                    return {"result": {"value": self.hit}}
                return {"result": {"value": "block|visible"}}
            if method == "DOM.scrollIntoViewIfNeeded":
                return {}
            if method == "DOM.getBoxModel":
                return {"model": {"content": [0, 0, 10, 0, 10, 10, 0, 10]}}
            raise AssertionError(method)
    try:
        lane._cdp_center(FakeCDP2(False), "#x")
        raise AssertionError("covered element must be refused")
    except RuntimeError as exc:
        assert "covered" in str(exc), exc
    center = lane._cdp_center(FakeCDP2(True), "#x")
    assert center[0] == 5.0 and center[1] == 5.0, center


def test_pass107_fixes():
    # (2) Verify accepts an exact edit whose replacement's
    # trailing CR joins the next line's LF into one CRLF on
    # re-read (joined-text comparison).
    import tempfile as _tf
    with _tf.TemporaryDirectory() as td:
        path = os.path.join(td, "f.txt")
        newf = os.path.join(td, "new.txt")
        with open(path, "wb") as fh:
            fh.write(b"a\nb\n\nc\n")
        with open(newf, "wb") as fh:
            fh.write(b"X\r")
        out = v8._run_tool_impl(json.dumps(
            {"tool": "edit", "file": path, "lines": "2-2",
             "new_file": newf}))
        assert out.startswith("OK"), out
        with open(path, "rb") as fh:
            assert fh.read() == b"a\nX\r\nc\n", fh.read()
        out = v8._run_tool_impl(json.dumps(
            {"tool": "verify", "file": path, "against": path,
             "lines": "2-2", "new_file": newf}))
        verdict = json.loads(out)
        assert verdict["ok"] is True, verdict
        v8._snapshots.pop(os.path.realpath(path), None)
    # (1) Lane clicktext JS hit-tests its match before dispatch.
    import importlib.machinery
    import importlib.util
    lane_path = str(Path(__file__).resolve().parent.parent / "src" / "cua-lane")
    loader = importlib.machinery.SourceFileLoader("cualane107", lane_path)
    spec = importlib.util.spec_from_loader("cualane107", loader)
    lane = importlib.util.module_from_spec(spec)
    loader.exec_module(lane)
    class FakeConn:
        def __init__(self, covered):
            self.covered = covered
            self.events = []
            self.js = ""
        def ev(self, js):
            self.js = js
            return None if self.covered else [60, 40]
        def cmd(self, method, **kw):
            self.events.append(method)
            return {}
        def close(self):
            pass
    made = {}
    saved_cdp = lane._CDP
    try:
        for covered, expect_click in ((True, False), (False, True)):
            conn = FakeConn(covered)
            lane._CDP = lambda conn=conn: conn
            args = lane.parser().parse_args(["cdp-clicktext", "Pay"])
            if expect_click:
                out = lane.cdp_dispatch(args)
                assert out["x"] == 60 and out["y"] == 40, out
                assert conn.events, "mouse events must be dispatched"
            else:
                try:
                    lane.cdp_dispatch(args)
                    raise AssertionError("covered text must not click")
                except RuntimeError:
                    pass
                assert not conn.events, conn.events
            assert "elementFromPoint" in conn.js, conn.js
    finally:
        lane._CDP = saved_cdp


def test_pass107b_fixes():
    # Forest-attribution pruning is miss-tolerant: an entry for a
    # dead pid is pruned after sustained absence, while a live
    # pid's (foreign) attribution persists across polls.
    import subprocess as _sp
    live = _sp.Popen(["sleep", "30"])
    try:
        v8._ensure_subreaper()
        live_start = v8._pid_start(live.pid)
        v8._FOREST_OWNER[live.pid] = (999999, 1, live_start)
        v8._FOREST_OWNER[2999999] = (999999, 1, 1)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if 2999999 not in v8._FOREST_OWNER:
                break
            time.sleep(0.05)
        assert 2999999 not in v8._FOREST_OWNER, \
            "dead pid's attribution was never pruned"
        assert v8._FOREST_OWNER.get(live.pid) == (999999, 1, live_start), \
            v8._FOREST_OWNER.get(live.pid)
    finally:
        v8._FOREST_OWNER.pop(live.pid, None)
        live.kill()
        live.wait()


def test_pass108_fixes():
    # (2) JSON inside an XML envelope's parameter text is data:
    # the envelope wins; a JSON object before the envelope wins.
    resp = ('<function=write><parameter=prompt>Explain '
            '{"tool":"done","result":"INNER"} literally</parameter>'
            '</function>')
    tc = v8._parse_tool_call(resp)
    assert tc["tool"] == "write", tc
    assert "INNER" in tc["prompt"], tc
    resp = ('<function=shell><parameter=cmd>echo {"a": 1}</parameter>'
            '</function>')
    tc = v8._parse_tool_call(resp)
    assert tc["tool"] == "shell" and tc["cmd"] == 'echo {"a": 1}', tc
    tc = v8._parse_tool_call('{"tool":"done","result":"plain"}')
    assert tc["tool"] == "done" and tc["result"] == "plain", tc
    tc = v8._parse_tool_call(
        '{"tool":"done","result":"first"} '
        '<function=write><parameter=prompt>x</parameter></function>')
    assert tc["tool"] == "done", tc
    # (1) Lane: raw keycodes resolve through the keymap before
    # the guard — ctrl+25 is ctrl+w and is refused; a benign
    # keycode (space) dispatches.
    import importlib.machinery
    import importlib.util
    lane_path = str(Path(__file__).resolve().parent.parent / "src" / "cua-lane")
    loader = importlib.machinery.SourceFileLoader("cualane108", lane_path)
    spec = importlib.util.spec_from_loader("cualane108", loader)
    lane = importlib.util.module_from_spec(spec)
    loader.exec_module(lane)
    ran = []
    real_run = lane.run
    def fake_run(*cmd):
        ran.append(cmd)
        if cmd and cmd[0] == "xmodmap":
            return "keycode  24 = q Q\nkeycode  25 = w W\n" \
                   "keycode  65 = space\nkeycode  37 = Control_L\n"
        return ""
    lane.run = fake_run
    lane.input_window = lambda state, wid=None: 4242
    lane.check_focus = lambda wid: None
    lane.remember = lambda state: None
    try:
        for combo in ("ctrl+25", "ctrl+24", "37+25",
                      "ctrl+25junk", "37junk+25junk"):
            lane._KEYCODE_MAP = None
            try:
                lane.dispatch(lane.parser().parse_args(["key", combo]), {})
                raise AssertionError("keycode bypass: " + combo)
            except ValueError:
                pass
            assert not any(c and c[0] == "xdotool" for c in ran), ran
            ran.clear()
        # Hex keysyms are handled by the hex branch, not the
        # keycode branch: 0x61 (a) dispatches; ctrl+0x77 (w) is
        # guarded.
        lane._KEYCODE_MAP = None
        lane.dispatch(lane.parser().parse_args(["key", "0x61"]), {})
        assert any(c and c[0] == "xdotool" for c in ran), ran
        ran.clear()
        lane._KEYCODE_MAP = None
        try:
            lane.dispatch(lane.parser().parse_args(
                ["key", "ctrl+0x77"]), {})
            raise AssertionError("hex keysym bypass: ctrl+0x77")
        except ValueError:
            pass
        assert not any(c and c[0] == "xdotool" for c in ran), ran
        ran.clear()
        # Unicode keysyms of ANY digit count resolve: U77 and
        # U00000077 are both 'w' to libX11.
        for combo in ("ctrl+U77", "ctrl+U00000077"):
            lane._KEYCODE_MAP = None
            try:
                lane.dispatch(lane.parser().parse_args(
                    ["key", combo]), {})
                raise AssertionError("unicode keysym bypass: " + combo)
            except ValueError:
                pass
            assert not any(c and c[0] == "xdotool" for c in ran), ran
            ran.clear()
        # Single digits are digit KEYS, not keycodes: they must
        # dispatch (browser tab shortcuts use ctrl+1..9).
        for combo in ("1", "ctrl+1"):
            lane._KEYCODE_MAP = None
            lane.dispatch(lane.parser().parse_args(["key", combo]), {})
            assert any(c and c[0] == "xdotool" for c in ran), (combo, ran)
            ran.clear()
        lane._KEYCODE_MAP = None
        lane.dispatch(lane.parser().parse_args(["key", "65"]), {})
        assert any(c and c[0] == "xdotool" for c in ran), ran
    finally:
        lane.run = real_run
        lane._KEYCODE_MAP = None


def test_pass112_fixes():
    import importlib.machinery
    import importlib.util
    lane_path = str(Path(__file__).resolve().parent.parent / "src" / "cua-lane")
    loader = importlib.machinery.SourceFileLoader("cualane112", lane_path)
    spec = importlib.util.spec_from_loader("cualane112", loader)
    lane = importlib.util.module_from_spec(spec)
    loader.exec_module(lane)
    # (1) Keycode resolution is shift-level aware: keycode 25
    # maps a (level 0) / W (level 1) — ctrl+25 dispatches the a
    # key, ctrl+shift+25 would send ctrl+shift+w and is refused.
    ran = []
    real_run = lane.run
    def fake_run(*cmd):
        ran.append(cmd)
        if cmd and cmd[0] == "xmodmap":
            return "keycode  25 = a W w W\nkeycode  50 = Shift_L\n"
        return ""
    lane.run = fake_run
    lane.input_window = lambda state, wid=None: 4242
    lane.check_focus = lambda wid: None
    lane.remember = lambda state: None
    try:
        lane._KEYCODE_MAP = None
        lane.dispatch(lane.parser().parse_args(["key", "ctrl+25"]), {})
        assert any(c and c[0] == "xdotool" for c in ran), ran
        ran.clear()
        lane._KEYCODE_MAP = None
        try:
            lane.dispatch(lane.parser().parse_args(
                ["key", "ctrl+shift+25"]), {})
            raise AssertionError("shift-level keycode bypass")
        except ValueError:
            pass
        assert not any(c and c[0] == "xdotool" for c in ran), ran
    finally:
        lane.run = real_run
        lane._KEYCODE_MAP = None
    # (2) CDP probes fail closed: an exceptionDetails-shaped
    # response (no value) refuses the click pre-dispatch.
    class ProbeCDP:
        def __init__(self, fail_at):
            self.fail_at = fail_at
        def cmd(self, method, **kw):
            if method == "DOM.getDocument":
                return {"root": {"nodeId": 1}}
            if method == "DOM.querySelector":
                return {"nodeId": 7}
            if method == "DOM.describeNode":
                return {"node": {"backendNodeId": 7}}
            if method == "DOM.resolveNode":
                return {"object": {"objectId": "o1"}}
            if method == "DOM.scrollIntoViewIfNeeded":
                return {}
            if method == "DOM.getBoxModel":
                return {"model": {"content":
                        [0, 0, 100, 0, 100, 50, 0, 50]}}
            if method == "Runtime.callFunctionOn":
                decl = kw.get("functionDeclaration", "")
                if "getComputedStyle" in decl:
                    if self.fail_at == "style":
                        return {"result": {"exceptionDetails": {}}}
                    return {"result": {"value": "block|visible"}}
                if self.fail_at == "hit":
                    return {"result": {"exceptionDetails": {}}}
                return {"result": {"value": True}}
            return {}
    for fail_at, needle in (("style", "not visible"),
                            ("hit", "covered")):
        try:
            lane._cdp_center(ProbeCDP(fail_at), "#x")
            raise AssertionError("probe failure allowed: " + fail_at)
        except RuntimeError as e:
            assert needle in str(e), (fail_at, e)


def test_pass113_fixes():
    import importlib.machinery
    import importlib.util
    lane_path = str(Path(__file__).resolve().parent.parent / "src" / "cua-lane")
    loader = importlib.machinery.SourceFileLoader("cualane113", lane_path)
    spec = importlib.util.spec_from_loader("cualane113", loader)
    lane = importlib.util.module_from_spec(spec)
    loader.exec_module(lane)
    # (1) The degenerate test uses the BORDER quad: a padded
    # button with zero content size still yields its center.
    class BoxCDP:
        def cmd(self, method, **kw):
            if method == "DOM.getDocument":
                return {"root": {"nodeId": 1}}
            if method == "DOM.querySelector":
                return {"nodeId": 7}
            if method == "DOM.resolveNode":
                return {"object": {"objectId": "o1"}}
            if method == "DOM.scrollIntoViewIfNeeded":
                return {}
            if method == "DOM.getBoxModel":
                return {"model": {
                    "content": [10, 10, 10, 10, 10, 10, 10, 10],
                    "border": [0, 0, 42, 0, 42, 42, 0, 42]}}
            if method == "Runtime.callFunctionOn":
                decl = kw.get("functionDeclaration", "")
                if "getComputedStyle" in decl:
                    return {"result": {"value": "block|visible"}}
                return {"result": {"value": True}}
            return {}
    x, y, _nid = lane._cdp_center(BoxCDP(), "#b")
    assert (x, y) == (21.0, 21.0), (x, y)
    # (2) A generic protocol failure inside the lane's lookup
    # surfaces as a _ReceiptError whose receipt states
    # dispatched=False (receipt redesign: dispatch state is data,
    # never an error-string prefix), and v8's rung branches on the
    # receipt so the ladder can fall back.
    class BoomCDP:
        def cmd(self, method, **kw):
            raise KeyError("result")
        def ev(self, js):
            raise KeyError("result")
        def close(self):
            pass
    saved_cdp = lane._CDP
    try:
        lane._CDP = lambda: BoomCDP()
        args = lane.parser().parse_args(["cdp-click", "#x"])
        try:
            lane.cdp_dispatch(args)
            raise AssertionError("boom must surface")
        except RuntimeError as e:
            receipt = getattr(e, "receipt", None)
            assert receipt and receipt.get("dispatched") is False, e
            assert receipt.get("phase") == "lookup", receipt
    finally:
        lane._CDP = saved_cdp
    import socket as _socket
    with environment(CU_DRY_RUN=None):
        def boom_cua(command, target):
            raise RuntimeError(
                '{"ok": false, "error": "boom", "receipt": '
                '{"version": 1, "phase": "lookup", '
                '"dispatched": false, "effect": "none"}}')
        class _FakeConn:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
        saved_cua, saved_conn = v8._cua, _socket.create_connection
        v8._cua = boom_cua
        _socket.create_connection = lambda *a, **k: _FakeConn()
        try:
            dispatched, via, detail = v8.rung_cdp(
                {"selector": "#x"}, "browser_cdp")
            assert dispatched is False, (dispatched, detail)
            assert "not_dispatched" in detail, detail
        finally:
            v8._cua = saved_cua
            _socket.create_connection = saved_conn


def test_pass114_fixes():
    import importlib.machinery
    import importlib.util
    lane_path = str(Path(__file__).resolve().parent.parent / "src" / "cua-lane")
    loader = importlib.machinery.SourceFileLoader("cualane114", lane_path)
    spec = importlib.util.spec_from_loader("cualane114", loader)
    lane = importlib.util.module_from_spec(spec)
    loader.exec_module(lane)
    # (1) Protocol RuntimeErrors from the CDP layer surface as
    # _ReceiptError with dispatched=False; semantic refusals keep
    # their own message (and also carry a not-dispatched receipt).
    class ProtoCDP:
        def cmd(self, method, **kw):
            raise RuntimeError("CDP protocol boom")
        def ev(self, js):
            raise RuntimeError("CDP protocol boom")
        def close(self):
            pass
    class MissCDP:
        def cmd(self, method, **kw):
            if method == "DOM.getDocument":
                return {"root": {"nodeId": 1}}
            if method == "DOM.querySelector":
                return {"nodeId": 0}
            return {}
        def close(self):
            pass
    saved_cdp = lane._CDP
    args = lane.parser().parse_args(["cdp-click", "#x"])
    try:
        def _ctor_boom():
            raise ConnectionRefusedError("ws setup refused")
        lane._CDP = _ctor_boom
        try:
            lane.cdp_dispatch(args)
            raise AssertionError("ctor failure must surface")
        except RuntimeError as e:
            receipt = getattr(e, "receipt", None)
            assert receipt and receipt.get("dispatched") is False, e
        lane._CDP = lambda: ProtoCDP()
        try:
            lane.cdp_dispatch(args)
            raise AssertionError("protocol error must surface")
        except RuntimeError as e:
            receipt = getattr(e, "receipt", None)
            assert receipt and receipt.get("dispatched") is False, e
        lane._CDP = lambda: MissCDP()
        try:
            lane.cdp_dispatch(args)
            raise AssertionError("selector miss must surface")
        except RuntimeError as e:
            assert str(e).startswith("selector not found"), e
    finally:
        lane._CDP = saved_cdp
    # (2) Observer attribution inherits through adoption: stage1
    # spawns stage2 and exits; adopted stage2 spawns stage3 —
    # stage3's recorded owner must be stage1, not stage2.
    import subprocess as _sp
    import tempfile as _tf
    v8._ensure_subreaper()
    with _tf.TemporaryDirectory() as td:
        flag = os.path.join(td, "go")
        pidfile = os.path.join(td, "pids")
        s3 = os.path.join(td, "stage3.py")
        with open(s3, "w") as fh:
            fh.write("import os, time\n"
                     "open(%r, 'a').write('3 %%d\\n' %% os.getpid())\n"
                     "time.sleep(25)\n" % pidfile)
        s2 = os.path.join(td, "stage2.py")
        with open(s2, "w") as fh:
            fh.write("import os, subprocess, sys, time\n"
                     "while not os.path.exists(%r):\n"
                     "    time.sleep(0.02)\n"
                     "subprocess.Popen([sys.executable, %r])\n"
                     "time.sleep(25)\n" % (flag, s3))
        stage1 = ("import subprocess,sys,time,os;"
                  "p=subprocess.Popen([sys.executable,%r]);"
                  "open(%r,'a').write('2 %%d\\n' %% p.pid);"
                  "open(%r,'w').write('x');"
                  "time.sleep(0.4)" % (s2, pidfile, flag))
        root = _sp.Popen([sys.executable, "-c", stage1])
        try:
            deadline = time.monotonic() + 8
            pids = {}
            while time.monotonic() < deadline:
                if os.path.exists(pidfile):
                    for line in open(pidfile):
                        tag, pid = line.split()
                        pids[int(tag)] = int(pid)
                if 2 in pids and 3 in pids:
                    rec = v8._FOREST_OWNER.get(pids[3])
                    if rec and rec[0] == root.pid:
                        break
                time.sleep(0.05)
            rec = v8._FOREST_OWNER.get(pids.get(3, -1))
            assert rec is not None and rec[0] == root.pid, \
                (pids, rec, root.pid)
        finally:
            for pid in list(v8._FOREST_OWNER):
                pass
            root.kill()
            root.wait()
            for tag, pid in list(pids.items()):
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass
                v8._FOREST_OWNER.pop(pid, None)


def test_pass116_fixes():
    # Scroll's subprocess timeout scales with the repeat count:
    # 1000 repeats at 40 ms need ~43 s, not the 8 s default.
    import importlib.machinery
    import importlib.util
    lane_path = str(Path(__file__).resolve().parent.parent / "src" / "cua-lane")
    loader = importlib.machinery.SourceFileLoader("cualane116", lane_path)
    spec = importlib.util.spec_from_loader("cualane116", loader)
    lane = importlib.util.module_from_spec(spec)
    loader.exec_module(lane)
    calls = []
    real_run = lane.run
    def fake_run(*cmd, **kw):
        calls.append((cmd, kw))
        if "getmouselocation" in cmd:
            return "WINDOW=4242"
        return ""
    lane.run = fake_run
    lane.input_window = lambda state, wid=None: 4242
    lane.check_focus = lambda wid: None
    lane.remember = lambda state: None
    try:
        lane.dispatch(lane.parser().parse_args(
            ["scroll", "down", "1000"]), {})
        click_calls = [kw for cmd, kw in calls
                       if "click" in cmd]
        assert click_calls, calls
        timeout = click_calls[-1].get("timeout", 8)
        assert timeout >= 1000 * 0.04 + 3, timeout
    finally:
        lane.run = real_run


def test_pass117_fixes():
    # SelectionClear during an INCR transfer must not abandon
    # it: the owner keeps serving chunks until the transfer
    # drains, then exits.
    import importlib.machinery
    import importlib.util
    lane_path = str(Path(__file__).resolve().parent.parent / "src" / "cua-lane")
    loader = importlib.machinery.SourceFileLoader("cualane117", lane_path)
    spec = importlib.util.spec_from_loader("cualane117", loader)
    lane = importlib.util.module_from_spec(spec)
    loader.exec_module(lane)
    owner = object.__new__(lane.X11)
    owner.d = 1
    owner.w = 4242
    owner.clip = 999
    owner.incr = 888
    class FakeX:
        def XSetSelectionOwner(self, *a):
            pass
        def XGetSelectionOwner(self, *a):
            return 4242
        def XFlush(self, *a):
            pass
        def XSelectInput(self, *a):
            pass
        def XSendEvent(self, *a):
            pass
    owner.x = FakeX()
    _atoms = {"TARGETS": 100, "UTF8_STRING": 101, "STRING": 102,
              "text/plain;charset=utf-8": 103, "text/plain": 104,
              "ATOM": 105}
    owner.atom = lambda name: _atoms[name]
    changes = []
    def fake_change(win, prop, typ, data, fmt=8):
        changes.append(len(data))
    owner.change = fake_change
    payload = b"x" * 70000
    ev_req = lane.Event()
    ev_req.type = 30
    ev_req.request = lane.Request(30, 0, 1, 1, 0, 777, 999, 101, 555, 0)
    ev_clear = lane.Event()
    ev_clear.type = 29
    def _del_ev():
        ev = lane.Event()
        ev.type = 28
        ev.property = lane.Property(28, 0, 1, 1, 777, 555, 0, 1)
        return ev
    script = [ev_req, ev_clear, _del_ev(), _del_ev(), _del_ev()]
    def fake_event(deadline):
        if script:
            return script.pop(0)
        raise TimeoutError
    owner.event = fake_event
    r_fd, w_fd = os.pipe()
    try:
        owner.serve(payload, w_fd)
    finally:
        os.close(r_fd)
    # Chunks served AFTER the clear: 65536 then the 4464-byte
    # remainder then the terminating empty chunk.
    assert changes[-3:] == [65536, 4464, 0], changes


def test_pass118_fixes():
    # Effective-modifier resolution: with caps:ctrl_modifier,
    # Caps_Lock (and its keycode 66) act as Control — the guards
    # must refuse Caps_Lock+w exactly like ctrl+w.
    import importlib.machinery
    import importlib.util
    lane_path = str(Path(__file__).resolve().parent.parent / "src" / "cua-lane")
    loader = importlib.machinery.SourceFileLoader("cualane118", lane_path)
    spec = importlib.util.spec_from_loader("cualane118", loader)
    lane = importlib.util.module_from_spec(spec)
    loader.exec_module(lane)
    ran = []
    real_run = lane.run
    def fake_run(*cmd):
        ran.append(cmd)
        if cmd[:2] == ("xmodmap", "-pm"):
            return ("control      Control_L (0x25),  Control_R (0x69),"
                    "  Caps_Lock (0x42)\n"
                    "shift        Shift_L (0x32),  Shift_R (0x3e)\n")
        if cmd[:2] == ("xmodmap", "-pke"):
            return "keycode  66 = Caps_Lock\nkeycode  25 = w W\n"
        return ""
    lane.run = fake_run
    lane.input_window = lambda state, wid=None: 4242
    lane.check_focus = lambda wid: None
    lane.remember = lambda state: None
    try:
        for combo in ("Caps_Lock+w", "66+w", "caps_lock+q"):
            lane._KEYCODE_MAP = None
            lane._MOD_BINDINGS = None
            try:
                lane.dispatch(lane.parser().parse_args(
                    ["key", combo]), {})
                raise AssertionError("remapped-modifier bypass: " + combo)
            except ValueError:
                pass
            assert not any(c and c[0] == "xdotool" for c in ran), ran
            ran.clear()
    finally:
        lane.run = real_run
        lane._KEYCODE_MAP = None
        lane._MOD_BINDINGS = None


def test_pass119_fixes():
    # Hex keysyms outside the static map resolve to names via
    # libX11: 0xffe7 is Meta_L; under a map binding Meta_L to
    # Super (mod4), 0xffe7+d must be refused like super+d.
    import importlib.machinery
    import importlib.util
    lane_path = str(Path(__file__).resolve().parent.parent / "src" / "cua-lane")
    loader = importlib.machinery.SourceFileLoader("cualane119", lane_path)
    spec = importlib.util.spec_from_loader("cualane119", loader)
    lane = importlib.util.module_from_spec(spec)
    loader.exec_module(lane)
    ran = []
    real_run = lane.run
    def fake_run(*cmd):
        ran.append(cmd)
        if cmd[:2] == ("xmodmap", "-pm"):
            return ("mod4         Super_L (0x85),  Meta_L (0xcd)\n"
                    "control      Control_L (0x25)\n")
        if cmd[:2] == ("xmodmap", "-pke"):
            return "keycode  25 = w W\n"
        return ""
    lane.run = fake_run
    lane.input_window = lambda state, wid=None: 4242
    lane.check_focus = lambda wid: None
    lane.remember = lambda state: None
    try:
        for combo in ("0xffe7+d", "Meta_L+d"):
            lane._MOD_BINDINGS = None
            try:
                lane.dispatch(lane.parser().parse_args(
                    ["key", combo]), {})
                raise AssertionError("hex modifier bypass: " + combo)
            except ValueError:
                pass
            assert not any(c and c[0] == "xdotool" for c in ran), ran
            ran.clear()
    finally:
        lane.run = real_run
        lane._MOD_BINDINGS = None


def test_pass120_fixes():
    # Bindings are consulted on the RAW keysym name before
    # aliasing: with Super_L bound to Control, Super_L+w and its
    # hex spelling 0xffeb+w are refused like ctrl+w.
    import importlib.machinery
    import importlib.util
    lane_path = str(Path(__file__).resolve().parent.parent / "src" / "cua-lane")
    loader = importlib.machinery.SourceFileLoader("cualane120", lane_path)
    spec = importlib.util.spec_from_loader("cualane120", loader)
    lane = importlib.util.module_from_spec(spec)
    loader.exec_module(lane)
    ran = []
    real_run = lane.run
    def fake_run(*cmd):
        ran.append(cmd)
        if cmd[:2] == ("xmodmap", "-pm"):
            return ("control      Control_L (0x25),  Super_L (0x85)\n"
                    "shift        Shift_L (0x32)\n")
        if cmd[:2] == ("xmodmap", "-pke"):
            return "keycode  25 = w W\n"
        return ""
    lane.run = fake_run
    lane.input_window = lambda state, wid=None: 4242
    lane.check_focus = lambda wid: None
    lane.remember = lambda state: None
    try:
        for combo in ("Super_L+w", "0xffeb+w", "super_l+q"):
            lane._MOD_BINDINGS = None
            try:
                lane.dispatch(lane.parser().parse_args(
                    ["key", combo]), {})
                raise AssertionError("raw-name binding bypass: " + combo)
            except ValueError:
                pass
            assert not any(c and c[0] == "xdotool" for c in ran), ran
            ran.clear()
        # Control: Super_L+d is now ctrl+d — not a guarded combo.
        lane._MOD_BINDINGS = None
        lane.dispatch(lane.parser().parse_args(["key", "Super_L+d"]), {})
        assert any(c and c[0] == "xdotool" for c in ran), ran
    finally:
        lane.run = real_run
        lane._MOD_BINDINGS = None


def test_pass121_fixes():
    # Shorthand tokens and character spellings take the same
    # bindings path: Super_L bound to Control refuses super+w;
    # the letter a bound to Control refuses 0x61+w and U0061+w.
    import importlib.machinery
    import importlib.util
    lane_path = str(Path(__file__).resolve().parent.parent / "src" / "cua-lane")
    loader = importlib.machinery.SourceFileLoader("cualane121", lane_path)
    spec = importlib.util.spec_from_loader("cualane121", loader)
    lane = importlib.util.module_from_spec(spec)
    loader.exec_module(lane)
    ran = []
    real_run = lane.run
    state = {"pm": "control      Control_L (0x25),  Super_L (0x85)\n"}
    def fake_run(*cmd):
        ran.append(cmd)
        if cmd[:2] == ("xmodmap", "-pm"):
            return state["pm"]
        if cmd[:2] == ("xmodmap", "-pke"):
            return "keycode  25 = w W\nkeycode  38 = a A\n"
        return ""
    lane.run = fake_run
    lane.input_window = lambda state_, wid=None: 4242
    lane.check_focus = lambda wid: None
    lane.remember = lambda state_: None
    try:
        for combo in ("super+w", "super+q"):
            lane._MOD_BINDINGS = None
            try:
                lane.dispatch(lane.parser().parse_args(
                    ["key", combo]), {})
                raise AssertionError("shorthand binding bypass: " + combo)
            except ValueError:
                pass
            assert not any(c and c[0] == "xdotool" for c in ran), ran
            ran.clear()
        state["pm"] = "control      Control_L (0x25),  a (0x26)\n"
        for combo in ("0x61+w", "U0061+w", "a+w"):
            lane._MOD_BINDINGS = None
            try:
                lane.dispatch(lane.parser().parse_args(
                    ["key", combo]), {})
                raise AssertionError("char binding bypass: " + combo)
            except ValueError:
                pass
            assert not any(c and c[0] == "xdotool" for c in ran), ran
            ran.clear()
    finally:
        lane.run = real_run
        lane._MOD_BINDINGS = None


def test_pass122_fixes():
    # Facts model: a modifier-BOUND action key keeps both facts.
    # With w bound to Control, ctrl+w is [w->ctrl] + [ctrl] — the
    # w action under a ctrl modifier — and is refused; a bare w
    # (itself now ctrl+w) is refused too. Decoded spellings whose
    # keysym NAME is bound (adiaeresis U00E4, space 0x20) are
    # refused as modifiers.
    import importlib.machinery
    import importlib.util
    lane_path = str(Path(__file__).resolve().parent.parent / "src" / "cua-lane")
    loader = importlib.machinery.SourceFileLoader("cualane122", lane_path)
    spec = importlib.util.spec_from_loader("cualane122", loader)
    lane = importlib.util.module_from_spec(spec)
    loader.exec_module(lane)
    ran = []
    real_run = lane.run
    state = {"pm": "control      Control_L (0x25),  w (0x19)\n"}
    def fake_run(*cmd):
        ran.append(cmd)
        if cmd[:2] == ("xmodmap", "-pm"):
            return state["pm"]
        if cmd[:2] == ("xmodmap", "-pke"):
            return "keycode  25 = w W\n"
        return ""
    lane.run = fake_run
    lane.input_window = lambda state_, wid=None: 4242
    lane.check_focus = lambda wid: None
    lane.remember = lambda state_: None
    def refused(combo):
        lane._MOD_BINDINGS = None
        lane._KEYCODE_MAP = None
        try:
            lane.dispatch(lane.parser().parse_args(["key", combo]), {})
            return False
        except ValueError:
            return True
        finally:
            ran.clear()
    try:
        assert refused("ctrl+w"), "bound w must keep its action fact"
        assert refused("w"), "bare bound w IS ctrl+w"
        assert refused("0x77+q"), "hex w bound to control + q"
        state["pm"] = ("control      Control_L (0x25),"
                       "  adiaeresis (0xe4),  space (0x20)\n")
        assert refused("U00E4+w"), "adiaeresis is a bound modifier"
        assert refused("0x20+w"), "space is a bound modifier"
        # Controls: an unbound letter dispatches; plain ctrl+s
        # stays allowed (not a guarded action).
        state["pm"] = "control      Control_L (0x25)\n"
        lane._MOD_BINDINGS = None
        lane.dispatch(lane.parser().parse_args(["key", "ctrl+s"]), {})
        assert any(c and c[0] == "xdotool" for c in ran), ran
    finally:
        lane.run = real_run
        lane._MOD_BINDINGS = None
        lane._KEYCODE_MAP = None


def test_pass123_fixes():
    # Lane guard completions (findings 1-4).
    import importlib.machinery
    import importlib.util
    lane_path = str(Path(__file__).resolve().parent.parent / "src" / "cua-lane")
    loader = importlib.machinery.SourceFileLoader("cualane123", lane_path)
    spec = importlib.util.spec_from_loader("cualane123", loader)
    lane = importlib.util.module_from_spec(spec)
    loader.exec_module(lane)
    ran = []
    real_run = lane.run
    state = {"pm": "control      Control_L (0x25),  Meta_L (0xcd)\n",
             "pke": "keycode  25 = a W\nkeycode  38 = a A\n"}
    def fake_run(*cmd):
        ran.append(cmd)
        if cmd[:2] == ("xmodmap", "-pm"):
            return state["pm"]
        if cmd[:2] == ("xmodmap", "-pke"):
            return state["pke"]
        return ""
    lane.run = fake_run
    lane.input_window = lambda state_, wid=None: 4242
    lane.check_focus = lambda wid: None
    lane.remember = lambda state_: None
    def refused(combo):
        lane._MOD_BINDINGS = None
        lane._KEYCODE_MAP = None
        try:
            lane.dispatch(lane.parser().parse_args(["key", combo]), {})
            return False
        except ValueError:
            return True
        finally:
            ran.clear()
    try:
        # (1) meta shorthand resolves Meta_L's binding.
        assert refused("meta+w"), "meta shorthand"
        # (2) The physical key's binding survives shift levels:
        # keycode 25 is bound via its level-0 name a.
        state["pm"] = "control      Control_L (0x25),  a (0x19)\n"
        assert refused("shift+25"), "shift-level binding loss"
        # (3) An uppercase token implies Shift for keycodes.
        state["pm"] = "control      Control_L (0x25)\n"
        assert refused("ctrl+A+25"), "implicit shift"
        assert refused("ctrl+0x41+25"), "implicit shift (hex)"
        # (4) Duplicate names: keycode 38's OWN binding (control)
        # wins for the keycode spelling despite a mod4 twin.
        state["pm"] = ("control      a (0x26)\n"
                       "mod4         a (0x27)\n")
        assert refused("38+w"), "per-keycode binding"
    finally:
        lane.run = real_run
        lane._MOD_BINDINGS = None
        lane._KEYCODE_MAP = None
    # (5) Executor: a PreDispatchTimeout in post-action
    # bookkeeping must not erase a completed receipt.
    progress = os.path.join(tempfile.mkdtemp(prefix="cu-trio2-"),
                            "p.jsonl")
    tools = ["kbd_agent", "done"]
    seen = []
    def fake_chat(task, steps, last_result, note):
        index = len(seen)
        seen.append(last_result)
        tool = tools[index]
        if tool == "kbd_agent":
            return json.dumps(dict(tool=tool, action="keys",
                                   args={"keys": ["ctrl", "s"]})), "stop", 0.0
        return json.dumps(dict(tool=tool, result="kept")), "stop", 0.0
    def fake_tool(raw):
        return '{"ok": true, "via": "kbd", "verified": true}'
    focus_calls = {"n": 0}
    def flaky_focus(fresh=False):
        focus_calls["n"] += 1
        if focus_calls["n"] >= 2:
            raise v8.PreDispatchTimeout(
                "deadline expired before dispatch of xdotool")
        return dict(title="Editor", window_id="100",
                    window_class="gnome-text-editor",
                    surface="native")
    originals = (v8.call_qwen, v8.run_tool, v8._focus_verify,
                 v8._cdp_text, v8._latest_shot, v8._automation_tab)
    try:
        v8.call_qwen, v8.run_tool = fake_chat, fake_tool
        v8._focus_verify = flaky_focus
        v8._cdp_text = lambda: None
        v8._latest_shot = lambda: None
        v8._automation_tab = lambda: {}
        with environment(CU_PROGRESS_LOG=progress, CUA_BIN=None):
            with contextlib.redirect_stdout(io.StringIO()):
                result = v8.run_loop("receipt keeps", max_iters=4)
    finally:
        (v8.call_qwen, v8.run_tool, v8._focus_verify,
         v8._cdp_text, v8._latest_shot, v8._automation_tab) = originals
    assert "kept" in result, result
    assert len(seen) >= 2 and '"ok": true' in seen[1], seen
    assert "deadline expired" not in seen[1], seen


def test_pass124_fixes():
    import importlib.machinery
    import importlib.util
    lane_path = str(Path(__file__).resolve().parent.parent / "src" / "cua-lane")
    loader = importlib.machinery.SourceFileLoader("cualane124", lane_path)
    spec = importlib.util.spec_from_loader("cualane124", loader)
    lane = importlib.util.module_from_spec(spec)
    loader.exec_module(lane)
    ran = []
    real_run = lane.run
    state = {"pm": "control      Control_L (0x25)\n",
             "pke": "keycode  25 = w x\nkeycode  10 = 1 exclam\n"
                    "keycode  38 = a A\nkeycode  39 = a A\n"}
    def fake_run(*cmd):
        ran.append(cmd)
        if cmd[:2] == ("xmodmap", "-pm"):
            return state["pm"]
        if cmd[:2] == ("xmodmap", "-pke"):
            return state["pke"]
        return ""
    lane.run = fake_run
    lane.input_window = lambda state_, wid=None: 4242
    lane.check_focus = lambda wid: None
    lane.remember = lambda state_: None
    def refused(combo):
        lane._MOD_BINDINGS = None
        lane._KEYCODE_MAP = None
        try:
            lane.dispatch(lane.parser().parse_args(["key", combo]), {})
            return False
        except ValueError:
            return True
        finally:
            ran.clear()
    def allowed(combo):
        lane._MOD_BINDINGS = None
        lane._KEYCODE_MAP = None
        lane.dispatch(lane.parser().parse_args(["key", combo]), {})
        ok = any(c and c[0] == "xdotool" for c in ran)
        ran.clear()
        return ok
    try:
        # (1) Capitalized named keys do not imply Shift.
        assert refused("Control_L+25"), "capitalized name false shift"
        # (2) Level-1 keysyms (hex Latin-1, named exclam) imply it.
        state["pke"] = ("keycode  25 = a W\nkeycode  10 = 1 exclam\n")
        assert refused("ctrl+0xc4+25"), "Latin-1 uppercase hex"
        assert refused("ctrl+exclam+25"), "named shifted symbol"
        assert refused("ctrl+0x21+25"), "hex shifted symbol"
        # (3) Order: Shift introduced later cannot shift an
        # earlier keycode.
        state["pke"] = "keycode  25 = w x\n"
        assert refused("ctrl+25+A"), "retroactive shift"
        assert allowed("25+A"), "later shift alone is harmless"
        # (4) Per-keycode precision: 38 is control-bound, 39 is
        # super-bound; neither inherits the other's binding.
        state["pm"] = ("control      a (0x26)\n"
                       "mod4         a (0x27)\n")
        state["pke"] = "keycode  38 = a A\nkeycode  39 = a A\n"
        assert allowed("38+d"), "38 must not inherit super"
        assert allowed("39+w"), "39 must not inherit ctrl"
        assert refused("38+w"), "38 keeps its own ctrl binding"
    finally:
        lane.run = real_run
        lane._MOD_BINDINGS = None
        lane._KEYCODE_MAP = None
    # (5) done: an observed deadline expiry in the final probe
    # aborts as DEADLINE instead of reporting success.
    progress = os.path.join(tempfile.mkdtemp(prefix="cu-trio2-"),
                            "p.jsonl")
    def fake_chat(task, steps, last_result, note):
        return json.dumps(dict(tool="done", result="all finished")), \
            "stop", 0.0
    def boom_focus(fresh=False):
        raise v8.ActionTimeout("slice expired")
    originals = (v8.call_qwen, v8._focus_verify)
    try:
        v8.call_qwen = fake_chat
        v8._focus_verify = boom_focus
        with environment(CU_PROGRESS_LOG=progress, CUA_BIN=None):
            with contextlib.redirect_stdout(io.StringIO()):
                result = v8.run_loop("done deadline", max_iters=2)
    finally:
        v8.call_qwen, v8._focus_verify = originals
    assert "DEADLINE" in result, result
    assert "all finished" not in result, result


def test_pass125_fixes():
    # Trio-3 debugger: Codex pass refuted pass-124 fixes 2 and 4 and
    # found a sibling false-positive; all three reproduced against
    # the shipped lane, then fixed. (1) A NAMED Latin-1 uppercase
    # keysym (Adiaeresis) must introduce implicit Shift exactly like
    # its hex spelling — case folding in the level map had merged
    # the adiaeresis/Adiaeresis level pair. (2) A lowercase keysym
    # that also lives at level 0 must NOT introduce Shift merely
    # because another key carries it at level 1. (3) A keycode bound
    # only to a non-canonical modifier (lock) has its OWN binding —
    # the name-union fallback must not fire for it.
    import importlib.machinery
    import importlib.util
    lane_path = str(Path(__file__).resolve().parent.parent / "src" / "cua-lane")
    loader = importlib.machinery.SourceFileLoader("cualane125", lane_path)
    spec = importlib.util.spec_from_loader("cualane125", loader)
    lane = importlib.util.module_from_spec(spec)
    loader.exec_module(lane)
    ran = []
    real_run = lane.run
    state = {"pm": "control      Control_L (0x25)\n",
             "pke": "keycode  25 = a W\n"
                    "keycode  48 = adiaeresis Adiaeresis\n"}
    def fake_run(*cmd):
        ran.append(cmd)
        if cmd[:2] == ("xmodmap", "-pm"):
            return state["pm"]
        if cmd[:2] == ("xmodmap", "-pke"):
            return state["pke"]
        return ""
    lane.run = fake_run
    lane.input_window = lambda state_, wid=None: 4242
    lane.check_focus = lambda wid: None
    lane.remember = lambda state_: None
    def refused(combo):
        lane._MOD_BINDINGS = None
        lane._KEYCODE_MAP = None
        try:
            lane.dispatch(lane.parser().parse_args(["key", combo]), {})
            return False
        except ValueError:
            return True
        finally:
            ran.clear()
    def allowed(combo):
        lane._MOD_BINDINGS = None
        lane._KEYCODE_MAP = None
        lane.dispatch(lane.parser().parse_args(["key", combo]), {})
        ok = any(c and c[0] == "xdotool" for c in ran)
        ran.clear()
        return ok
    try:
        assert refused("ctrl+0xc4+25"), "hex Latin-1 uppercase"
        assert refused("ctrl+Adiaeresis+25"), "named Latin-1 uppercase"
        state["pke"] = ("keycode  25 = a W\nkeycode  38 = a A\n"
                        "keycode  39 = b a\n")
        assert allowed("ctrl+a+25"), "lowercase a needs no shift"
        state["pm"] = ("control      a (0x26)\n"
                       "lock         a (0x38)\n")
        state["pke"] = "keycode  38 = a A\nkeycode  56 = a A\n"
        assert allowed("56+w"), "lock-bound keycode keeps own binding"
        assert refused("38+w"), "control-bound keycode still guarded"
    finally:
        lane.run = real_run
        lane._MOD_BINDINGS = None
        lane._KEYCODE_MAP = None


def main():
    tests = [test_taskbar_strip_excluded, test_spa_sensor_refresh,
             test_vision_coordinate_scale, test_midtype_timeout_aborts,
             test_atspi_focused_app_gating, test_rung_effect_verification,
             test_trims_and_defaults, test_focus_memo_dedupes_per_step,
             test_pagetext_cdp_fallback_and_port,
             test_codex_partial_type_failure_aborts,
             test_codex_memo_invalidation_paths,
             test_codex_hash_markers_and_truncation,
             test_codex_cdp_text_empty_page,
             test_codex_short_image_taskbar_and_settle,
             test_codex_clipboard_sentinel, test_pass2_fixes,
             test_pass3_fixes, test_pass4_fixes, test_pass5_fixes,
             test_pass6_fixes, test_pass7_fixes, test_pass8_fixes,
             test_pass9_fixes, test_pass10_fixes, test_pass11_fixes,
             test_pass12_fixes, test_pass14_fixes, test_pass15_fixes,
             test_pass16_fixes, test_pass17_fixes, test_pass18_fixes,
             test_pass19_fixes, test_pass20_fixes, test_pass22_fixes,
             test_pass22_loop_fixes, test_pass23_fixes,
             test_pass23_loop_fixes, test_pass24_fixes,
             test_pass24_loop_fixes, test_pass26_fixes,
             test_pass26_loop_fixes, test_pass27_fixes,
             test_pass27_loop_fixes, test_pass28_loop_fixes,
             test_pass29_loop_fixes, test_pass30_fixes,
             test_pass32_fixes, test_pass33_fixes,
             test_pass36_fixes, test_pass37_fixes,
             test_pass37_loop_fixes, test_pass39_loop_fixes,
             test_pass41_fixes, test_pass42_fixes, test_pass43_fixes,
             test_pass44_fixes, test_pass45_fixes, test_pass46_fixes,
             test_pass47_fixes, test_pass50_fixes, test_pass52_fixes,
             test_pass55_fixes, test_pass56_fixes, test_pass57_fixes,
             test_pass61_fixes, test_pass63_fixes, test_pass66_fixes,
             test_pass69_fixes, test_pass70_fixes, test_pass75_fixes,
             test_pass77_fixes, test_pass78_fixes, test_pass79_fixes,
             test_pass81_fixes, test_pass84_fixes, test_pass86_fixes,
             test_pass89_fixes, test_pass91_fixes, test_pass94_fixes,
             test_pass98_fixes, test_pass100_fixes, test_pass101_fixes,
             test_pass102_fixes, test_pass103_fixes, test_pass104_fixes,
             test_pass105_fixes, test_pass106_fixes, test_pass107_fixes,
             test_pass107b_fixes, test_pass108_fixes, test_pass112_fixes,
             test_pass113_fixes, test_pass114_fixes, test_pass116_fixes,
             test_pass117_fixes, test_pass118_fixes, test_pass119_fixes,
             test_pass120_fixes, test_pass121_fixes, test_pass122_fixes,
             test_pass123_fixes, test_pass124_fixes, test_pass125_fixes]
    for test in tests:
        # Each test models an independent run: reset run-scoped
        # identity state that a previous scenario may have retired.
        v8._seed_retired = False
        v8._last_cdp_window_id = ""
        v8._last_cdp_visible = None
        test()
        print("PASS", test.__name__)
    print("TRIO2_OK")


if __name__ == "__main__":
    main()
