#!/bin/bash
# Spike B session Xorg launcher (runs as root via sudo).
# args: PIDFILE DISPLAY CONFNAME VT
# Masks the host's DRM nodes inside a private mount namespace so the session
# server's platform probe finds no GPU to fight over (with -sharevts the
# first server keeps DRM master and a second server's modeset probe dies
# with drmSetMaster EBUSY). The dummy driver is software-only anyway.
echo $$ > "$1"
exec unshare -m bash -c '
  for d in /dev/dri/card0 /dev/dri/card1 /dev/dri/renderD128 /dev/dri/renderD129; do
    [ -e "$d" ] && mount --bind /dev/null "$d" 2>/dev/null
  done
  exec Xorg "$1" -config "$2" -noreset -nolisten tcp -novtswitch -sharevts "$3"
' _ "$2" "$3" "$4"
