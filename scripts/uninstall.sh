#!/usr/bin/env bash
# scripts/uninstall.sh — remove Yorik COMPLETELY from this machine.
#
# Stops services, tears down the docker-compose stack (incl. volumes),
# removes the CLI symlink, the systemd unit, the venv, the Whisper
# model cache, config.env, /tmp pidfile + log, and ALL data (local +
# external if you'd relocated it). The repo directory itself is left
# in place — you delete it with `rm -rf` when you're sure.
#
# One big confirmation at the start. No flags. Sudo is requested for
# the systemd unit + container-owned files in data/, if needed.

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; CYAN=$'\033[36m'
BOLD=$'\033[1m'; RESET=$'\033[0m'

say()  { printf "\n${CYAN}[%s]${RESET} %s\n" "$1" "$2"; }
ok()   { printf "  ${GREEN}✓${RESET} %s\n" "$1"; }
skip() { printf "  ${YELLOW}·${RESET} %s\n" "$1"; }
warn() { printf "  ${YELLOW}![${RESET} %s\n" "$1"; }

# ── Inventory what's actually here ───────────────────────────────────

EXTERNAL_ROOT=""
if [[ -f data/storage_root.txt ]]; then
  EXTERNAL_ROOT="$(tr -d '[:space:]' < data/storage_root.txt)"
fi

LOCAL_BIN_LINK="$HOME/.local/bin/yorik"

SYSTEMD_INSTALLED=0
if command -v systemctl >/dev/null 2>&1 \
   && systemctl list-unit-files yorik.service 2>/dev/null | grep -q yorik.service; then
  SYSTEMD_INSTALLED=1
fi

COMPOSE_CMD=""
if [[ -f docker-compose.yml ]]; then
  if command -v docker >/dev/null 2>&1 \
     && docker compose version >/dev/null 2>&1; then
    COMPOSE_CMD="docker compose"
  elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_CMD="docker-compose"
  fi
fi

UVICORN_PID=""
if [[ -f /tmp/homeos-api.pid ]]; then
  UVICORN_PID="$(cat /tmp/homeos-api.pid 2>/dev/null || true)"
fi

# ── Big confirmation ─────────────────────────────────────────────────

printf "\n${BOLD}${RED}━━ Yorik uninstall ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}\n\n"
echo "This will REMOVE Yorik COMPLETELY from this machine."
echo
[[ -n "$UVICORN_PID" ]]    && echo "  • uvicorn (pid $UVICORN_PID)"
[[ -n "$COMPOSE_CMD" ]]    && echo "  • docker compose stack + volumes (immich / paperless / n8n / wa-bridge)"
[[ $SYSTEMD_INSTALLED == 1 ]] && echo "  • systemd unit /etc/systemd/system/yorik.service (needs sudo)"
[[ -L "$LOCAL_BIN_LINK" ]] && echo "  • CLI symlink $LOCAL_BIN_LINK"
[[ -d venv ]]              && echo "  • Python venv $REPO/venv"
[[ -d data ]]              && echo "  • local data $REPO/data (family.db, photos, documents, voices, …)"
[[ -n "$EXTERNAL_ROOT" && -d "$EXTERNAL_ROOT" ]] \
                           && printf "  ${BOLD}${RED}• EXTERNAL data at %s${RESET}\n" "$EXTERNAL_ROOT"
[[ -d "$HOME/.cache/whisper" ]] && echo "  • Whisper model cache ~/.cache/whisper"
[[ -f config.env ]]        && echo "  • config.env (your secrets)"
echo "  • /tmp/homeos-api.pid + /tmp/homeos-api.log"
echo
echo "The repo directory itself ($REPO) stays."
echo "Delete it after: ${BOLD}rm -rf \"$REPO\"${RESET}"
echo
printf "${BOLD}This is irreversible. Make a backup first if your data matters.${RESET}\n\n"
read -r -p "Type ${BOLD}yes${RESET} to proceed: " CONFIRM
if [[ "$CONFIRM" != "yes" ]]; then
  echo "aborted — nothing removed."
  exit 1
fi

# ── Refuse obvious self-foot-shots on EXTERNAL_ROOT ──────────────────

if [[ -n "$EXTERNAL_ROOT" ]]; then
  case "$EXTERNAL_ROOT" in
    ""|"/"|"/home"|"/home/$USER"|"/root"|"/usr"|"/etc"|"/var"|"/tmp"|"/media"|"/mnt"|"/opt")
      warn "external root '$EXTERNAL_ROOT' looks dangerous — refusing to rm it"
      warn "  delete it manually if you really want to: sudo rm -rf '$EXTERNAL_ROOT'"
      EXTERNAL_ROOT=""
      ;;
  esac
fi

# ── Stop running services ────────────────────────────────────────────

say "STOP" "shutting Yorik down"

if [[ $SYSTEMD_INSTALLED == 1 ]]; then
  # The unit template has Restart=on-failure with a 10s backoff. If we
  # only `disable --now` and the stop returns non-zero (stale PIDFile,
  # in-flight start, anything), systemd respawns uvicorn 10s later —
  # right as we're rm-ing data/. That's the "uninstall is flaky" trap.
  #
  # Mask first so the unit is unstartable for the rest of this script,
  # THEN stop + disable. mask replaces the unit file with a /dev/null
  # symlink; the later rm in the PLUMBING section cleans that up.
  sudo systemctl mask yorik 2>/dev/null || true
  sudo systemctl stop yorik 2>/dev/null || true
  sudo systemctl disable yorik 2>/dev/null || true
  sudo systemctl reset-failed yorik 2>/dev/null || true
  ok "systemd service masked, stopped, disabled"
else
  skip "no systemd unit"
fi

if [[ -n "$UVICORN_PID" ]] && kill -0 "$UVICORN_PID" 2>/dev/null; then
  kill "$UVICORN_PID" 2>/dev/null || true
  sleep 2
  if kill -0 "$UVICORN_PID" 2>/dev/null; then
    kill -9 "$UVICORN_PID" 2>/dev/null || true
  fi
  ok "uvicorn (pid $UVICORN_PID) stopped"
fi

# Catch every python process running from THIS install's venv —
# critical for clean uninstall. uvicorn forks multiprocessing-spawn
# workers whose cmdline reads
#   python3 -c "from multiprocessing.spawn import spawn_main; ..."
# (no "uvicorn" string in it). The workers survive their parent's
# death, get reparented to systemd --user, and hold SQLite's
# data/family.db open with multiple FDs forever. A naive
# `pgrep -f uvicorn` misses them, so `rm -rf data/` either fails with
# "Directory not empty" or appears to succeed but leaves the inode
# alive — and on next reconnect the family.db reappears in the
# rebuilt dir. Matching by venv binary path catches the parent AND
# the forked workers regardless of cmdline shape, scoped to THIS
# install (won't touch unrelated python processes).
VENV_PY="$REPO/venv/bin/python3"
if [[ -x "$VENV_PY" ]] && pgrep -f "$VENV_PY" >/dev/null 2>&1; then
  # SIGTERM first so workers can release SQLite locks cleanly.
  pkill -TERM -f "$VENV_PY" 2>/dev/null || true
  sleep 2
  # SIGKILL anything that ignored the polite ask.
  if pgrep -f "$VENV_PY" >/dev/null 2>&1; then
    pkill -KILL -f "$VENV_PY" 2>/dev/null || true
    sleep 1
  fi
  ok "killed stray python processes from $VENV_PY (incl. multiprocessing-spawn workers)"
fi

# Belt-and-suspenders for the rare case the user ran uvicorn from the
# system python instead of the venv. The venv-path match above already
# covers every standard install.
if pgrep -f "uvicorn backend.main" >/dev/null 2>&1; then
  pkill -KILL -f "uvicorn backend.main" 2>/dev/null || true
  ok "killed stray 'uvicorn backend.main' processes"
fi

if [[ -n "$COMPOSE_CMD" ]]; then
  say "DOCKER" "tearing down containers + volumes"
  if $COMPOSE_CMD down -v --remove-orphans 2>/dev/null; then
    ok "$COMPOSE_CMD down -v complete"
  else
    warn "$COMPOSE_CMD down had errors — continuing"
  fi
else
  skip "no docker compose stack"
fi

# ── Remove plumbing ──────────────────────────────────────────────────

say "PLUMBING" "removing CLI symlink + systemd unit + venv"

if [[ -L "$LOCAL_BIN_LINK" ]]; then
  rm -f "$LOCAL_BIN_LINK"
  ok "removed $LOCAL_BIN_LINK"
elif [[ -e "$LOCAL_BIN_LINK" ]]; then
  warn "$LOCAL_BIN_LINK exists but isn't a symlink — leaving alone"
fi

if [[ $SYSTEMD_INSTALLED == 1 ]]; then
  sudo rm -f /etc/systemd/system/yorik.service 2>/dev/null || true
  # unmask clears any systemd-internal "this was masked" state that
  # could otherwise linger after we yank the file out from under it.
  sudo systemctl unmask yorik 2>/dev/null || true
  sudo systemctl daemon-reload 2>/dev/null || true
  sudo systemctl reset-failed yorik 2>/dev/null || true
  ok "removed /etc/systemd/system/yorik.service"
fi

if [[ -d venv ]]; then
  rm -rf venv
  ok "removed $REPO/venv"
fi

# ── Remove data ──────────────────────────────────────────────────────

say "DATA" "removing all Yorik data"

# External first — the local data/ may contain symlinks pointing here.
if [[ -n "$EXTERNAL_ROOT" && -d "$EXTERNAL_ROOT" ]]; then
  if ! rm -rf "$EXTERNAL_ROOT" 2>/dev/null; then
    warn "external root needs sudo (container-owned files); escalating"
    sudo rm -rf "$EXTERNAL_ROOT"
  fi
  ok "removed external data at $EXTERNAL_ROOT"
fi

if [[ -d data ]]; then
  # data/immich/postgres + data/paperless/db are owned by container UIDs
  # (mode 700 on the postgres dir, so even traversing it as the host
  # user is denied). Try without sudo first for the simple case.
  if ! rm -rf data 2>/dev/null; then
    warn "some data dirs owned by container UIDs; escalating to sudo"
    sudo rm -rf data
  fi
  ok "removed $REPO/data"
fi

if [[ -d "$HOME/.cache/whisper" ]]; then
  rm -rf "$HOME/.cache/whisper"
  ok "removed ~/.cache/whisper"
fi

if [[ -f config.env ]]; then
  rm -f config.env
  ok "removed config.env"
fi

rm -f /tmp/homeos-api.pid /tmp/homeos-api.log
ok "removed /tmp/homeos-api.{pid,log}"

# ── Done ─────────────────────────────────────────────────────────────

printf "\n${GREEN}${BOLD}━━ Uninstall complete ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}\n\n"
echo "The repo itself is still at $REPO."
printf "To remove the source too: ${BOLD}rm -rf \"$REPO\"${RESET}\n\n"
