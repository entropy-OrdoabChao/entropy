#!/usr/bin/env bash
# turbovec_daemon.sh — start/stop/status/restart the resident retrieval daemon (no systemd; nohup + pidfile).
# usage: scripts/turbovec_daemon.sh {start|stop|status|restart|health} [--no-embed]
#   --no-embed / TURBOVEC_NO_EMBED=1 : FTS-only (torch never imported; safe when the dxg bridge is wedged)
# env: TURBOVEC_PY (default ~/turbovec/venv/bin/python3 for embed mode; /usr/bin/python3 for FTS-only)
#      TURBOVEC_SOCK (/tmp/turbovec.sock) TURBOVEC_TCP ("" | 127.0.0.1:18031) TURBOVEC_ROOT (repo root)
# GPU rule: start the EMBED daemon only when the local-gpu lane answers (`curl -s localhost:8000/v1/models`); the
# script checks this and refuses otherwise (override: TURBOVEC_FORCE=1). It never runs nvidia-smi.
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
set -u
# ROOT = the tree this script lives in (sandbox or live) — resolved from the script path, never from $PWD, so
# `bash /any/where/scripts/turbovec_daemon.sh start` works from any cwd (cron @reboot, relay_room, m2_unfiled).
ROOT="${TURBOVEC_ROOT:-$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)}"
[ -f "$ROOT/pipeline/turbovec_daemon.py" ] || { echo "turbovec_daemon.sh: no pipeline/turbovec_daemon.py under ROOT=$ROOT" >&2; exit 2; }
WAIT_EMBED_S="${TURBOVEC_WAIT_EMBED_S:-180}"   # load ~40 s + cold-index warm-up ~80 s measured 2026-10-24
SOCK="${TURBOVEC_SOCK:-/tmp/turbovec.sock}"
RUN="$ROOT/output/audit"; mkdir -p "$RUN"
PIDFILE="${TURBOVEC_PIDFILE:-$RUN/turbovec_daemon.pid}"
LOG="${TURBOVEC_LOG:-$RUN/turbovec_daemon.log}"
CMD="${1:-status}"; shift || true
for a in "$@"; do [ "$a" = "--no-embed" ] && export TURBOVEC_NO_EMBED=1; done
if [ "${TURBOVEC_NO_EMBED:-}" = "1" ]; then PY="${TURBOVEC_PY:-/usr/bin/python3}"; else PY="${TURBOVEC_PY:-~/turbovec/venv/bin/python3}"; fi
export TURBOVEC_SOCK="$SOCK" TURBOVEC_LOG="$LOG"

alive() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; }
# pid of whatever answers on the socket (stale/missing pidfile case: a daemon started by hand or by another tree)
sock_pid() { do_health 2>/dev/null | sed -n 's/.*"pid": *\([0-9]*\).*/\1/p'; }

do_health() {
  "$PY" - "$SOCK" <<'EOF'
import json, socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.settimeout(3)
try:
    s.connect(sys.argv[1]); s.sendall(b'{"op":"health"}\n'); print(s.makefile().readline().strip())
except Exception as e:
    print(json.dumps({"ok": False, "error": str(e)})); sys.exit(1)
EOF
}

case "$CMD" in
  start)
    if alive; then echo "already running pid $(cat "$PIDFILE")"; exit 0; fi
    SP=$(sock_pid); if [ -n "$SP" ] && kill -0 "$SP" 2>/dev/null; then echo "$SP" >"$PIDFILE"; echo "already serving on $SOCK pid $SP (pidfile repaired)"; do_health; exit 0; fi
    if [ "${TURBOVEC_NO_EMBED:-}" != "1" ] && [ "${TURBOVEC_FORCE:-}" != "1" ]; then
      if ! curl -s -m 3 localhost:8000/v1/models >/dev/null 2>&1; then
        echo "REFUSED: local-gpu lane (localhost:8000) not answering — GPU/dxg health unknown. Use --no-embed or TURBOVEC_FORCE=1." >&2; exit 3
      fi
    fi
    [ -x "$PY" ] || { echo "python not found: $PY" >&2; exit 2; }
    ( cd "$ROOT" && PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" exec nohup "$PY" -u "$ROOT/pipeline/turbovec_daemon.py" >>"$LOG.out" 2>&1 ) &
    echo $! >"$PIDFILE"
    for _ in $(seq 1 40); do [ -S "$SOCK" ] && break; sleep 0.25; done
    echo "started pid $(cat "$PIDFILE") sock=$SOCK no_embed=${TURBOVEC_NO_EMBED:-0} py=$PY log=$LOG root=$ROOT"
    # health loop: FTS-only -> mode=fts_only at once; embed -> wait for load + warm-up (mode=embed) up to WAIT_EMBED_S
    want="embed"; [ "${TURBOVEC_NO_EMBED:-}" = "1" ] && want="fts_only"
    h=""; i=0; SECONDS=0
    while [ "$SECONDS" -lt "$WAIT_EMBED_S" ]; do
      h=$(do_health 2>/dev/null || true)
      case "$h" in *"\"mode\": \"$want\""*|*"\"mode\":\"$want\""*) break ;; *"\"mode\": \"degraded\""*) break ;; esac
      alive || { echo "daemon died during start — see $LOG.out" >&2; echo "$h"; exit 4; }
      sleep 1; i=$SECONDS
    done
    echo "$h"
    case "$h" in *"\"mode\": \"$want\""*|*"\"mode\":\"$want\""*) echo "ready mode=$want after ${i}s"; exit 0 ;;
                 *) echo "NOT READY after ${i}s (wanted mode=$want)" >&2; exit 5 ;; esac ;;
  stop)
    if ! alive; then SP=$(sock_pid); [ -n "$SP" ] && kill -0 "$SP" 2>/dev/null && echo "$SP" >"$PIDFILE"; fi   # stale pidfile, live socket
    if alive; then PID=$(cat "$PIDFILE"); kill "$PID" 2>/dev/null
      for _ in $(seq 1 40); do kill -0 "$PID" 2>/dev/null || break; sleep 0.25; done
      kill -0 "$PID" 2>/dev/null && kill -9 "$PID" 2>/dev/null; echo "stopped pid $PID"
    else echo "not running"; fi
    rm -f "$PIDFILE"; [ -S "$SOCK" ] && rm -f "$SOCK"; exit 0 ;;
  restart) "$0" stop; exec "$0" start "$@" ;;
  status)
    if alive; then echo "running pid $(cat "$PIDFILE") sock=$SOCK"; do_health; else echo "not running"; exit 1; fi ;;
  health) do_health ;;
  *) echo "usage: $0 {start|stop|status|restart|health} [--no-embed]" >&2; exit 64 ;;
esac
