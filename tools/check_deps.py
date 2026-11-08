#!/usr/bin/env python3
"""Dependency checker for cu-executor.

Reports, per surface (browser / native / vnc), whether this machine can
actually run it: ready / degraded (some fallback rungs missing) /
missing (a hard requirement is absent). Read-only: it installs nothing
and starts nothing. Run tools/check_deps.py --help for options.

Exit status: 0 if at least the native surface is ready or degraded,
1 if no surface is usable at all.
"""
import argparse
import importlib.util
import os
import shutil
import socket
import sys
import urllib.request

OK, DEGRADED, MISSING = "ready", "degraded", "missing"
RANK = {OK: 0, DEGRADED: 1, MISSING: 2}


def worst(*levels):
    return max(levels, key=lambda lv: RANK[lv])


class Report:
    def __init__(self):
        self.lines = []
        self.surfaces = {}

    def check(self, label, present, level_if_absent, note=""):
        mark = "ok " if present else "-- "
        self.lines.append("  [%s] %-34s %s" % (mark, label, note))
        return OK if present else level_if_absent


def tcp_reachable(url, timeout=2.0):
    try:
        req = urllib.request.Request(url.rstrip("/") + "/v1/models",
                                     method="GET")
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true",
                    help="machine-readable output")
    args = ap.parse_args()
    rep = Report()
    L = rep.lines

    L.append("python %s" % sys.version.split()[0])
    py_ok = sys.version_info >= (3, 10)
    rep.check("python >= 3.10", py_ok, MISSING,
              "" if py_ok else "executor needs 3.10+")

    L.append("pip modules:")
    mods = {}
    for mod, label in (("mss", "mss (screenshots)"),
                       ("websocket", "websocket-client (CDP)"),
                       ("PIL", "Pillow (region hashing)"),
                       ("pyatspi", "pyatspi (AT-SPI rung, optional)")):
        mods[mod] = importlib.util.find_spec(mod) is not None
        rep.check(label, mods[mod],
                  DEGRADED if mod == "pyatspi" else MISSING)

    L.append("system tools:")
    tools = {}
    for tool in ("xdotool", "wmctrl", "tesseract", "ydotool", "xterm"):
        tools[tool] = shutil.which(tool) is not None
        rep.check(tool, tools[tool], DEGRADED if tool == "xterm" else MISSING)

    ydo_socket = os.environ.get("YDOTOOL_SOCKET", "/tmp/.ydotool_socket")
    daemon_up = os.path.exists(ydo_socket)
    rep.check("ydotoold daemon (socket %s)" % ydo_socket, daemon_up,
              DEGRADED,
              "" if daemon_up else
              "ydotool rung unavailable until the daemon runs; it does "
              "NOT survive a reboot unless a user service starts it")

    atspi_on = False
    try:
        out = os.popen("gsettings get org.gnome.desktop.interface "
                       "toolkit-accessibility 2>/dev/null").read().strip()
        atspi_on = out == "true"
    except Exception:
        pass
    rep.check("toolkit-accessibility gsettings", atspi_on, DEGRADED,
              "" if atspi_on else "AT-SPI rung needs it set to true")

    chromium = any(shutil.which(b) for b in
                   ("chromium", "chromium-browser", "google-chrome"))
    rep.check("chromium / chrome", chromium, MISSING,
              "browser surface only")
    viewer = any(shutil.which(b) for b in
                 ("tigervncviewer", "vncviewer", "xtigervncviewer"))
    rep.check("VNC viewer (TigerVNC)", viewer, MISSING, "vnc surface only")

    server = (os.environ.get("CU_MODEL_SERVER")
              or os.environ.get("QWEN_SERVER") or "http://127.0.0.1:8080")
    model_up = tcp_reachable(server)
    rep.check("model endpoint %s" % server, model_up, DEGRADED,
              "" if model_up else
              "no model server answering; runs will fail at the LLM call")

    native = worst(OK if (tools["xdotool"] and tools["wmctrl"]
                          and tools["tesseract"] and mods["mss"]
                          and mods["PIL"]) else MISSING,
                   OK if (tools["ydotool"] and daemon_up) else DEGRADED,
                   OK if (mods["pyatspi"] and atspi_on) else DEGRADED,
                   OK if model_up else DEGRADED)
    browser = worst(OK if (chromium and mods["websocket"]) else MISSING,
                    native if native != MISSING else DEGRADED)
    vnc = worst(native, OK if viewer else MISSING)
    rep.surfaces = {"browser": browser, "native": native, "vnc": vnc}

    L.append("")
    for name in ("browser", "native", "vnc"):
        L.append("surface %-8s %s" % (name, rep.surfaces[name]))
    if args.json:
        import json
        print(json.dumps({"surfaces": rep.surfaces,
                          "checks": "\n".join(rep.lines)}, indent=2))
    else:
        print("\n".join(rep.lines))
    return 0 if RANK[rep.surfaces["native"]] <= 1 else 1


if __name__ == "__main__":
    sys.exit(main())
