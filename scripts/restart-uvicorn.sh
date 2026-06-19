#!/usr/bin/env bash
# scripts/restart-uvicorn.sh — kill any running uvicorn + start fresh.
#
# Why this exists: --reload doesn't catch every change (new files,
# singleton-cached registries, skill module re-imports), and the
# parent+multiprocessing-worker dance means `pkill -f uvicorn` often
# leaves an orphaned worker still on :8000. This script does it right.
#
# Usage:
#   bash scripts/restart-uvicorn.sh                      # just restart (no --reload, default)
#   bash scripts/restart-uvicorn.sh --reload             # with --reload (auto-restart on file change)
#   HOMEOS_PORT=8001 bash scripts/restart-uvicorn.sh     # different port

set -uo pipefail
cd "$(dirname "$0")/.."

PORT="${HOMEOS_PORT:-8000}"
LOG="${HOMEOS_LOG:-/tmp/homeos-api.log}"
PIDFILE="${HOMEOS_PIDFILE:-/tmp/homeos-api.pid}"
RELOAD_FLAG=""
for arg in "$@"; do
  [[ "$arg" == "--reload" ]] && RELOAD_FLAG="--reload"
  [[ "$arg" == "--no-reload" ]] && RELOAD_FLAG=""
done

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; CYAN=$'\033[36m'; RESET=$'\033[0m'
ok()   { printf "  ${GREEN}✓${RESET} %s\n" "$1"; }
warn() { printf "  ${YELLOW}![${RESET} %s\n" "$1"; }
say()  { printf "${CYAN}▸${RESET} %s\n" "$1"; }

# ── Kill anything currently on :$PORT ────────────────────────────────

say "stopping any uvicorn / venv python holding :$PORT"

# 1. Pidfile-tracked parent
if [[ -f "$PIDFILE" ]]; then
  OLD="$(cat "$PIDFILE" 2>/dev/null || true)"
  if [[ -n "$OLD" ]] && kill -0 "$OLD" 2>/dev/null; then
    pkill -TERM -P "$OLD" 2>/dev/null || true
    kill "$OLD" 2>/dev/null || true
    sleep 2
    if kill -0 "$OLD" 2>/dev/null; then
      pkill -KILL -P "$OLD" 2>/dev/null || true
      kill -9 "$OLD" 2>/dev/null || true
    fi
    ok "stopped pidfile process $OLD"
  fi
  rm -f "$PIDFILE"
fi

# 2. Any uvicorn process for backend.main (catches sibling installs)
if pgrep -f 'uvicorn backend.main' >/dev/null 2>&1; then
  pkill -KILL -f 'uvicorn backend.main' 2>/dev/null || true
  sleep 1
  ok "killed straggler uvicorn(s)"
fi

# 3. Anything still on :$PORT (multiprocessing.spawn orphans etc.)
if command -v ss >/dev/null 2>&1; then
  HOLDER_LINE="$(ss -Hltnp "sport = :$PORT" 2>/dev/null | head -1 || true)"
  if [[ -n "$HOLDER_LINE" ]]; then
    HOLDER_PID="$(printf '%s\n' "$HOLDER_LINE" | sed -nE 's/.*pid=([0-9]+).*/\1/p' | head -1)"
    if [[ -n "$HOLDER_PID" ]]; then
      kill -9 "$HOLDER_PID" 2>/dev/null || true
      sleep 1
      ok "killed port-:$PORT holder pid $HOLDER_PID"
    fi
  fi
fi

# 4. Final pkill of anything from our venv that might be lingering
pkill -KILL -f "$(pwd)/venv/bin/python" 2>/dev/null || true
sleep 2

# ── Sanity check ─────────────────────────────────────────────────────

if command -v ss >/dev/null 2>&1; then
  if ss -Hltn "sport = :$PORT" 2>/dev/null | grep -q .; then
    warn "port :$PORT STILL held — investigate: sudo ss -ltnp 'sport = :$PORT'"
    exit 1
  fi
fi
ok "port :$PORT is free"

# ── Boot fresh ───────────────────────────────────────────────────────

say "starting uvicorn on :$PORT (reload=${RELOAD_FLAG:-off})"

# Source venv + load config.env if present so HOMEOS_* vars are set.
# shellcheck disable=SC1091
source venv/bin/activate
if [[ -f config.env ]]; then
  set -a; source config.env; set +a
fi

# Honour the same YORIK_BIND env var that start.sh respects. Default
# is 0.0.0.0 — matches start.sh so a restart never silently strips
# LAN access from an already-running household. Localhost-only:
# YORIK_BIND=127.0.0.1 bash scripts/restart-uvicorn.sh.
YORIK_BIND="${YORIK_BIND:-0.0.0.0}"

# nohup + disown + redirect — the only way to get a truly detached
# uvicorn that survives this script exiting.
nohup uvicorn backend.main:app --host "$YORIK_BIND" --port "$PORT" $RELOAD_FLAG \
  > "$LOG" 2>&1 < /dev/null &
NEW_PID=$!
echo "$NEW_PID" > "$PIDFILE"
disown -a

# Poll /api/health for up to 30s — uvicorn boot includes pip caches,
# voice models, paperless reconcile (a few seconds total on warm cache).
say "waiting for /api/health"
deadline=$((SECONDS + 30))
while (( SECONDS < deadline )); do
  if curl -sf "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
    ok "uvicorn up: pid $NEW_PID, log $LOG"
    curl -sf "http://127.0.0.1:$PORT/api/health" 2>&1 | head -c 160
    echo
    exit 0
  fi
  sleep 1
done

warn "uvicorn didn't respond within 30s. Tail of log:"
tail -20 "$LOG"
exit 1
