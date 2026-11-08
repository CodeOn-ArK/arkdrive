#!/usr/bin/env bash
# Install cu-executor's dependencies on a Debian/Ubuntu X11 machine.
# Safe to re-run. Does NOT start the model server and does NOT modify
# the executor's configuration -- run tools/check_deps.py afterwards
# to see what state each surface is in.
set -eu

if ! command -v apt-get >/dev/null; then
  echo "install.sh: apt-get not found. This installer targets Debian/Ubuntu;" >&2
  echo "install the equivalents by hand: xdotool wmctrl tesseract-ocr" >&2
  echo "ydotool python3-pyatspi, plus pip packages mss websocket-client Pillow." >&2
  exit 1
fi

SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"

echo "== system packages (X11 input spine, OCR, optional rungs) =="
$SUDO apt-get update -qq
$SUDO apt-get install -y --no-install-recommends \
  xdotool wmctrl tesseract-ocr ydotool python3-pyatspi \
  || echo "WARN: some apt packages failed; check_deps.py will show the gaps."

echo "== python packages =="
python3 -m pip install --user mss websocket-client Pillow \
  || python3 -m pip install --user --break-system-packages \
       mss websocket-client Pillow

echo "== AT-SPI (optional rung) =="
gsettings set org.gnome.desktop.interface toolkit-accessibility true \
  2>/dev/null || echo "WARN: could not set toolkit-accessibility (no gsettings?)"

echo "== ydotool daemon =="
# The ydotool typing rung needs ydotoold running with its socket at
# /tmp/.ydotool_socket. A bare daemon started by hand does NOT survive
# a reboot -- install the user service so it comes back on its own.
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/ydotoold.service" << 'UNIT'
[Unit]
Description=ydotool daemon (cu-executor typing rung)
After=default.target
[Service]
ExecStart=/usr/bin/ydotoold
Restart=on-failure
[Install]
WantedBy=default.target
UNIT
if systemctl --user daemon-reload 2>/dev/null; then
  systemctl --user enable --now ydotoold.service 2>/dev/null \
    && echo "ydotoold user service enabled" \
    || echo "WARN: ydotoold service did not start; the rung stays degraded."
else
  echo "WARN: no user systemd. Start the daemon manually before runs:"
  echo "      ydotoold &   (socket: /tmp/.ydotool_socket)"
  echo "      NOTE: it will not survive a reboot without the service."
fi

echo
echo "Done. Verify with:  python3 tools/check_deps.py"
echo "Point the executor at your model server:"
echo "  export CU_MODEL_SERVER=http://127.0.0.1:8080   # any OpenAI-compatible endpoint"
echo "  export CU_MODEL=<your tool-calling model>"
