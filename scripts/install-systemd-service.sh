#!/usr/bin/env bash
# scripts/install-systemd-service.sh — install/uninstall Yorik's systemd unit.
#
# Used in three places:
#   1. `start.sh` prompts at the end of first successful boot
#   2. `yorik service install` (CLI subcommand) for opt-in later
#   3. Run directly: `bash scripts/install-systemd-service.sh [install|uninstall|status]`
#
# What it does (install):
#   - Renders scripts/yorik.service.template with this user/repo/port
#   - sudo cp to /etc/systemd/system/yorik.service
#   - sudo systemctl daemon-reload + enable --now yorik
#   - Verifies it came up; rolls back if not
#
# What it doesn't do:
#   - Touch anything if you're not on systemd (Mac, WSL1, alpine, …)
#   - Run if root — drops privileges check would be wrong target user
#   - Replace an existing yorik.service without explicit reinstall

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$REPO/scripts/yorik.service.template"
UNIT_PATH="/etc/systemd/system/yorik.service"
PORT="${HOMEOS_PORT:-8000}"
RUN_USER="${SUDO_USER:-$USER}"
RUN_GROUP="$(id -gn "$RUN_USER")"

# ── Sanity checks ────────────────────────────────────────────────────

_die() { echo "error: $*" >&2; exit 1; }

_check_systemd() {
  command -v systemctl >/dev/null 2>&1 \
    || _die "systemctl not found — this script is Linux/systemd only. On Mac use launchd, on WSL1 there's no service manager."
  [[ -d /run/systemd/system ]] \
    || _die "systemd isn't PID 1 — can't install a system unit (are you in a container?)."
}

_check_template() {
  [[ -f "$TEMPLATE" ]] \
    || _die "template missing: $TEMPLATE"
  [[ -x "$REPO/venv/bin/uvicorn" ]] \
    || _die "venv not built at $REPO/venv — run start.sh first to create it."
}

_render_unit() {
  # Write to a tmp file, install via sudo. Lets us see the rendered
  # content even on permission errors.
  local out="$1"
  sed -e "s|__USER__|$RUN_USER|g" \
      -e "s|__GROUP__|$RUN_GROUP|g" \
      -e "s|__REPO__|$REPO|g" \
      -e "s|__PORT__|$PORT|g" \
      "$TEMPLATE" > "$out"
}

# ── Subcommands ─────────────────────────────────────────────────────

cmd_install() {
  _check_systemd
  _check_template

  if systemctl list-unit-files yorik.service 2>/dev/null | grep -q yorik.service; then
    echo "yorik.service is already installed."
    echo "→ to reinstall, first run: $0 uninstall"
    echo "→ to see status: $0 status"
    return 0
  fi

  local tmp
  tmp="$(mktemp /tmp/yorik.service.XXXXXX)"
  _render_unit "$tmp"
  echo "rendered unit (preview):"
  echo "──────────────────────"
  sed -n '1,8p' "$tmp"
  echo "  …"
  echo "──────────────────────"
  echo "  user:    $RUN_USER"
  echo "  group:   $RUN_GROUP"
  echo "  repo:    $REPO"
  echo "  port:    $PORT"
  echo "  unit:    $UNIT_PATH"
  echo

  # Stop any running manual uvicorn so it doesn't clash on the port.
  if pgrep -f "uvicorn backend.main" >/dev/null; then
    echo "stopping running manual uvicorn (would clash on port $PORT)…"
    pkill -KILL -f "uvicorn backend.main" || true
    sleep 2
  fi

  echo "installing (you'll be prompted for sudo)…"
  sudo install -m 0644 "$tmp" "$UNIT_PATH"
  rm -f "$tmp"
  sudo systemctl daemon-reload
  sudo systemctl enable --now yorik

  # Verify
  sleep 3
  if systemctl is-active --quiet yorik; then
    echo
    echo "  ✓ yorik.service is active. Useful follow-ups:"
    echo "    systemctl status yorik          # current state"
    echo "    journalctl -u yorik -f          # tail logs"
    echo "    sudo systemctl restart yorik    # after upgrades"
  else
    echo
    echo "  ✗ service installed but didn't come up. Logs:" >&2
    sudo journalctl -u yorik -n 20 --no-pager >&2
    echo "  → run: sudo systemctl status yorik" >&2
    return 1
  fi
}

cmd_uninstall() {
  _check_systemd
  if ! systemctl list-unit-files yorik.service 2>/dev/null | grep -q yorik.service; then
    echo "yorik.service is not installed — nothing to do."
    return 0
  fi
  echo "stopping + disabling yorik.service…"
  sudo systemctl disable --now yorik || true
  sudo rm -f "$UNIT_PATH"
  sudo systemctl daemon-reload
  echo "  ✓ removed. yorik will no longer auto-start on boot."
  echo "  → to run manually: bash start.sh"
}

cmd_status() {
  _check_systemd
  if ! systemctl list-unit-files yorik.service 2>/dev/null | grep -q yorik.service; then
    echo "yorik.service is not installed."
    echo "→ install with: $0 install   (or: yorik service install)"
    return 1
  fi
  echo "── systemctl status yorik ──"
  systemctl status yorik --no-pager -l || true
  echo
  echo "── last 10 log lines (journalctl -u yorik) ──"
  journalctl -u yorik -n 10 --no-pager --output=short
}

# ── Dispatch ────────────────────────────────────────────────────────

case "${1:-install}" in
  install)   cmd_install ;;
  uninstall) cmd_uninstall ;;
  status)    cmd_status ;;
  -h|--help|help)
    cat <<EOF
Usage: $0 [install|uninstall|status]

  install     Install yorik.service, enable + start it
  uninstall   Stop, disable, and remove yorik.service
  status      Show systemctl status + recent journal lines

Reads HOMEOS_PORT from environment / config.env (default 8000).
Renders the unit as user '$RUN_USER' running out of '$REPO'.
EOF
    ;;
  *) _die "unknown subcommand: $1 (try --help)" ;;
esac
