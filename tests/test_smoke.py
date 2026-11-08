#!/usr/bin/env python3
"""Offline smoke checks for the release executor; no browser, desktop input, or model server required."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time

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


def agent(tool, action, **args):
    return v8.run_tool(json.dumps(dict(tool=tool, action=action, args=args)))


def main():
    with tempfile.TemporaryDirectory(prefix="cu-v8-smoke-") as directory:
        progress = str(Path(directory) / "progress.jsonl")
        with environment(CU_DRY_RUN="1", CU_DRY_FAIL_FIRST=None, CU_DRY_SLEEP_S=None,
                         CU_PROGRESS_LOG=progress, CUA_CDP_PORT=None, CUA_BIN=None,
                         CU_TIMEOUT_KBD_AGENT="5"):
            # (a) Both non-destructive typing and explicitly safe paste succeed.
            # Dry state is per-run since trio-2 pass-22 (fields keep their
            # content across calls, like real fields), so each scenario
            # below resets it to model a fresh desktop.
            result = json.loads(agent("kbd_agent", "type_verify", text="smoke text"))
            assert result["ok"] is True, "type_verify must succeed in dry mode"
            v8._dry_state.set(None)
            result = json.loads(agent("kbd_agent", "type_verify", text="paste text", safe_clipboard=True))
            assert result["ok"] and result["readback_matches"], "safe paste must verify clipboard readback"

            # (b) The whole agent deadline interrupts even a runner ignoring its timeout.
            v8._dry_state.set(None)
            with environment(CU_TIMEOUT_KBD_AGENT="0.2", CU_DRY_SLEEP_S="1"):
                began = time.monotonic()
                result = agent("kbd_agent", "type_verify", text="deadline")
                elapsed = time.monotonic() - began
                assert "ERROR: TIMEOUT kbd_agent" in result, "sleeping runner must return structured TIMEOUT"
                assert elapsed < 0.8, "agent must honor its 0.2s deadline rather than wait for the runner"

            # (c) Failure of the first dry call must still allow a later coordinate rung.
            v8._dry_state.set(None)
            with environment(CU_DRY_FAIL_FIRST="1"):
                result = json.loads(agent("mouse_agent", "click", x=100, y=80, verify=True))
                assert result["ok"] is True, "click ladder must recover after first-call failure"
                assert len(result["rungs_tried"]) >= 2, "click must try at least two ladder rungs"
                assert result["via"] == "xdotool", "later xdotool coordinate rung must succeed"
                assert result["verified"] == "dry", "dry verification must never grab the desktop"

            # (d) Progress output is JSONL with the complete event schema.
            isolated = str(Path(directory) / "events.jsonl")
            with environment(CU_PROGRESS_LOG=isolated):
                for step in (1, 2, 3):
                    v8.log_progress(step, "smoke", 1.5, "abcdef012345", True, False, "test")
            events = [json.loads(line) for line in Path(isolated).read_text().splitlines()]
            assert [event["step"] for event in events] == [1, 2, 3], "progress steps must be monotonic"
            fields = {"ts", "run_id", "step", "tool", "ms", "state_hash", "changed", "wasted", "note"}
            assert all(set(event) == fields for event in events), "every progress event must have all fields"

            # (e) Surface classification is independent of the current desktop.
            # Trio-2: the default CUA_BIN is the cua-lane binary, so Chromium
            # classifies browser_cdp by default; plain cua opts out.
            assert v8.classify_surface("chromium", "Gmail") == "browser_cdp", "default lane binary enables CDP surface"
            with environment(CUA_BIN="/example/cua"):
                assert v8.classify_surface("chromium", "Gmail") == "browser", "plain cua binary is browser (no CDP)"
            assert v8.classify_surface("vncviewer", "TigerVNC") == "vnc", "VNC must override other surface rules"
            assert v8.classify_surface("gnome-text-editor", "Notes") == "native", "editor must be native"
            assert v8.classify_surface("", "") == "unknown", "empty surface must be unknown"
            with environment(CUA_CDP_PORT="9222"):
                assert v8.classify_surface("chromium", "Gmail") == "browser_cdp", "CDP port enables CDP surface"
            with environment(CUA_BIN="/example/cua-lane"):
                assert v8.classify_surface("chrome", "Gmail") == "browser_cdp", "lane binary enables CDP surface"

            # (f) Region hashing is pure, including its raw-byte fallback.
            first, second = b"frame-a" * 32, b"frame-b" * 32
            assert v8.region_hash(first) == v8.region_hash(first), "identical frames must hash identically"
            assert v8.region_hash(first) != v8.region_hash(second), "different frame bytes must change the hash"
            assert v8.compute_state_hash("title", "url", "a  b\nc") == v8.compute_state_hash(
                "title", "url", "a b c"), "state hash must normalize whitespace"

            # SPEC section 9: switch after two unchanged steps, then abort after two more.
            monitor = v8.ProgressMonitor()
            note, stalled = monitor.update(False, "native")
            assert not note and not stalled, "first unchanged step must not switch or abort"
            note, stalled = monitor.update(False, "native")
            assert "SWITCH" in note and "xdotool" in note and not stalled, "second unchanged step must name the ladder"
            assert monitor.update(False, "native") == ("", False), "switch must grant another strategy attempt"
            assert monitor.update(False, "native")[1], "two more unchanged steps must abort"

            # Focus polls have an overall deadline even when activation returns success.
            with environment(CU_TIMEOUT_WIN_AGENT="0.2"):
                result = agent("win_agent", "focus", match="999")
                assert "ERROR: TIMEOUT win_agent" in result, "unconfirmed focus must return TIMEOUT"

            # Exercise the monitor inside run_loop without any live LLM or real tools.
            original_chat, original_tool, original_shot = v8.call_qwen, v8.run_tool, v8._latest_shot
            notes = []
            def fake_chat(task, steps, last_result, note):
                notes.append(note)
                tc = dict(tool="kbd_agent", action="keys", args={"keys": ["Tab"], "sequence": len(notes)})
                return json.dumps(tc), "stop", 0.0
            try:
                v8.call_qwen = fake_chat
                v8.run_tool = lambda tc: '{"ok": true}'
                v8._latest_shot = lambda: None
                with contextlib.redirect_stdout(io.StringIO()):
                    result = v8.run_loop("monitor smoke", max_iters=6)
                assert result.startswith("ABORTED_STALLED"), "run_loop must stall after switch and two further no-change steps"
                assert len(notes) == 5 and "SWITCH" in notes[3], "run_loop must put switch into the next model prompt"
                assert "summary steps=5" in result and "atspi=" in result, "abort must include structured run summary"
            finally:
                v8.call_qwen, v8.run_tool, v8._latest_shot = original_chat, original_tool, original_shot
            # Repeated reads reuse state caches; refreshed native pixels invalidate observe.
            original_chat, original_tool = v8.call_qwen, v8.run_tool
            original_focus, original_shot = v8._focus_verify, v8._latest_shot
            original_tab_url = v8._tab_url
            v8._tab_url = lambda: ""  # hermetic: no real desktop metadata
            try:
                for surface, refreshed in (("native", False), ("native", True), ("browser", False)):
                    calls, last_results = [], []
                    tools = ["observe", "observe", "done"] if surface == "native" else [
                        "observe", "pagetext", "pagetext", "done"]
                    frame = [b"frame-a" * 32]
                    def cache_chat(task, steps, last_result, note):
                        index = len(last_results)
                        last_results.append(last_result)
                        if refreshed and index == 1:
                            frame[0] = b"frame-b" * 32
                        return json.dumps(dict(tool=tools[index], result="cache checked")), "stop", 0.0
                    def cache_tool(raw):
                        tool = json.loads(raw)["tool"]
                        calls.append(tool)
                        return ("Window title: Cache test\nWindow id: 100\nSurface: " + surface +
                                "\nURL: https://example.test/") if tool == "observe" else "Page text"
                    v8.call_qwen, v8.run_tool = cache_chat, cache_tool
                    v8._focus_verify = lambda: dict(title="Cache test", window_id="100", window_pid="4242", surface=surface)
                    v8._latest_shot = lambda: frame[0]
                    with contextlib.redirect_stdout(io.StringIO()):
                        result = v8.run_loop("cache smoke", max_iters=len(tools))
                    assert "cache checked" in result, "cache scenario must reach done"
                    if surface == "native":
                        assert calls.count("observe") == (2 if refreshed else 1), "observe cache must track refreshed pixels"
                        if not refreshed:
                            assert "[cached:true]" in last_results[-1], "repeated read must advertise its cache hit"
                    else:
                        # Pagetext is never cached (trio-2 pass-29): the
                        # clipboard read is non-atomic, so every pagetext
                        # step performs a fresh read.
                        assert calls.count("pagetext") == 2, "pagetext must re-read every time"
                        assert "[cached:true]" not in last_results[-1], "pagetext must never serve from cache"
            finally:
                v8.call_qwen, v8.run_tool = original_chat, original_tool
                v8._focus_verify, v8._latest_shot = original_focus, original_shot
                v8._tab_url = original_tab_url
    # (h) OCR dry mode never needs a screenshot or the OCR binary.
    with environment(CU_DRY_RUN="1"):
        result = json.loads(v8.run_tool(json.dumps(dict(tool="ocr", args={}))))
        assert result["engine"] == "dry", "OCR must use the dry engine"

        # (i) OCR has its own bounded tool timeout.
        assert "ocr" in v8.TOOL_TIMEOUTS_S, "OCR must have a tool timeout"

        # (j) Explicit ydotool typing works with the dry runner.
        result = json.loads(agent("kbd_agent", "type_verify", text="ydotool smoke", engine="ydotool"))
        assert result["ok"] is True, "ydotool type_verify must succeed in dry mode"
    print("SMOKE_OK")


if __name__ == "__main__":
    main()
