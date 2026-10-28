#!/bin/bash
# watchdog.sh - the macOS counterpart of watchdog.vbs. launchd runs it every
# 5 minutes (see install.sh).
#
# A tray that *crashed* is already relaunched by launchd itself (KeepAlive), so
# this only handles one that's alive but hung: its heartbeat file (written by
# the main thread every 30 s as "<pid> <recording>") names a running
# meeting_tray process and is older than STALE_SECS. Killing it makes launchd
# start a fresh one. A tray whose last heartbeat says it was recording is never
# killed (that would lose the meeting).
STALE_SECS=180
DIR="$(cd "$(dirname "$0")/.." && pwd)"
HB="$DIR/tray.heartbeat"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S')  $*" >> "$DIR/watchdog.log"; }

[ -f "$HB" ] || exit 0
read -r pid rec < "$HB" || exit 0          # empty / mid-write heartbeat: skip this round
case "$pid" in ''|*[!0-9]*) exit 0 ;; esac
cmd=$(ps -o command= -p "$pid" 2>/dev/null) || exit 0
[[ "$cmd" == *meeting_tray* ]] || exit 0   # gone, or the pid was reused

age=$(( $(date +%s) - $(stat -f %m "$HB") ))
(( age > STALE_SECS )) || exit 0
if [ "$rec" != "0" ]; then
    log "tray pid $pid heartbeat ${age}s old but was recording - leaving it alone"
    exit 0
fi
log "tray pid $pid heartbeat ${age}s old - killing it; launchd relaunches it"
kill -TERM "$pid" 2>/dev/null
sleep 5
kill -0 "$pid" 2>/dev/null && kill -KILL "$pid"
exit 0
