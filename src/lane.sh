#!/usr/bin/env bash
set -u

usage() {
    printf 'Usage: %s <1|2|3> <start|stop|status|claim [owner]|release|env>\n' "$0" >&2
    printf '       %s <1|2|3> run "<task prompt>"\n' "$0" >&2
    exit 1
}

[[ $# -ge 2 && $# -le 3 ]] || usage
N=$1
case "$N" in
    1) DISP=:11; VNC=5911 ;;
    2) DISP=:12; VNC=5912 ;;
    3) DISP=:13; VNC=5913 ;;
    *) usage ;;
esac
shift
ACTION=$1
BASE=${CU_LANE_BASE:-"$HOME/portal-lanes"}
PROFILE="$BASE/profiles/lane$N"
CUAROOT="/tmp/cua-lane$N"
LOGS="$BASE/logs"
LOCKS="$BASE/locks"
ENVFILE="$BASE/lane$N.env"
WMFILE="$BASE/lane$N.wm.pid"
LOCKDIR="$LOCKS/lane$N.lock"
SOCKET="/tmp/.X11-unix/X${DISP#:}"

write_env() {
    {
        printf 'export DISPLAY=%q\n' "$DISP"
        printf 'export CUA_DISPLAY=%q\n' "$DISP"
        printf 'export CUA_ROOT=%q\n' "$CUAROOT"
        printf 'export CUA_PROFILE=%q\n' "$PROFILE"
        printf 'export CUA_BIN=%q\n' "$BASE/bin/cua-lane"
        printf 'export PATH=%q:%q:"$PATH"\n' "$BASE/bin" "$HOME/bin"
    } > "$ENVFILE" || return 1
    printf '%s\n' "$ENVFILE"
}

wm_alive() {
    WM_PID=
    [[ -f "$WMFILE" ]] || return 1
    IFS= read -r WM_PID < "$WMFILE"
    [[ "$WM_PID" =~ ^[1-9][0-9]*$ ]] || return 1
    kill -0 "$WM_PID" 2>/dev/null
}

lane_status() {
    local xvnc=no wm=no chromium=no owner=free
    if [[ -S "$SOCKET" ]] && pgrep -f -- "Xvnc $DISP " > /dev/null; then
        xvnc=yes
    fi
    if wm_alive; then
        wm="yes (pid $WM_PID)"
    fi
    if pgrep -f -- "user-data-dir=$PROFILE" > /dev/null; then
        chromium=yes
    fi
    if [[ -d "$LOCKDIR" ]]; then
        owner=$(cat "$LOCKDIR/owner" 2>/dev/null) || owner=unknown
    fi
    printf 'lane%s display=%s xvnc=%s xfwm4=%s chromium=%s claimed-by=%s\n' \
        "$N" "$DISP" "$xvnc" "$wm" "$chromium" "$owner"
}

case "$ACTION" in
    env)
        [[ $# -eq 1 ]] || usage
        write_env
        ;;
    start)
        [[ $# -eq 1 ]] || usage
        write_env || exit 1
        mkdir -p "$LOGS" "$PROFILE" || exit 1
        if [[ ! -S "$SOCKET" ]]; then
            setsid nohup Xvnc "$DISP" -geometry 1600x1000 -depth 24 \
                -rfbport "$VNC" -localhost -SecurityTypes None -AlwaysShared \
                > "$LOGS/xvnc-lane$N.log" 2>&1 < /dev/null &
            for ((attempt = 0; attempt < 100; attempt++)); do
                [[ -S "$SOCKET" ]] && break
                sleep 0.1
            done
            if [[ ! -S "$SOCKET" ]]; then
                printf 'lane%s: Xvnc socket did not appear within 10 seconds\n' "$N" >&2
                lane_status
                exit 1
            fi
        fi
        if ! wm_alive; then
            setsid nohup env DISPLAY="$DISP" xfwm4 \
                > "$LOGS/xfwm4-lane$N.log" 2>&1 < /dev/null &
            printf '%s\n' "$!" > "$WMFILE" || exit 1
        fi
        lane_status
        ;;
    stop)
        [[ $# -eq 1 ]] || usage
        pkill -f -- "user-data-dir=$PROFILE" || :
        if wm_alive; then
            kill "$WM_PID" || :
        fi
        rm -f -- "$WMFILE"
        pkill -f -- "Xvnc $DISP " || :
        lane_status
        ;;
    status)
        [[ $# -eq 1 ]] || usage
        lane_status
        ;;
    claim)
        mkdir -p "$LOCKS" || exit 1
        if ! mkdir "$LOCKDIR" 2>/dev/null; then
            if [[ -d "$LOCKDIR" ]]; then
                owner=$(cat "$LOCKDIR/owner" 2>/dev/null) || owner=unknown
                printf 'lane%s already claimed by %s\n' "$N" "$owner" >&2
            else
                printf 'lane%s: could not create %s\n' "$N" "$LOCKDIR" >&2
            fi
            exit 1
        fi
        if ! printf '%s %s\n' "${2:-${USER:-owner}}" "$(date -Is)" > "$LOCKDIR/owner"; then
            rmdir -- "$LOCKDIR" 2>/dev/null
            exit 1
        fi
        printf 'claimed lane%s\n' "$N"
        ;;
    release)
        [[ $# -eq 1 ]] || usage
        if [[ -d "$LOCKDIR" ]]; then
            rm -rf -- "$LOCKDIR" || exit 1
            printf 'released lane%s\n' "$N"
        else
            printf 'lane%s was not claimed\n' "$N"
        fi
        ;;
    run)
        [[ $# -eq 2 ]] || usage
        if [[ ! -f "$ENVFILE" ]]; then
            write_env || exit 1
        fi
        source "$ENVFILE" || exit 1
        DL="${EXEC_DEADLINE_S:-480}"
        DL="${DL%%.*}"
        [[ "$DL" =~ ^[0-9]+$ ]] || DL=480
        EXEC=${CU_LANE_EXECUTOR:-"$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/cu_executor.py"}
        exec timeout --signal=TERM --kill-after=15 "$((DL + 120))" python3 "$EXEC" "$2"
        ;;
    *) usage ;;
esac
