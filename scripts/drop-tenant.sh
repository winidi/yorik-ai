#!/usr/bin/env bash
# Drop a Yorik tenant: its Postgres database and the manifest under
# data/tenants/<name>/. Destructive — the database is removed without
# a backup pass. Take a snapshot first if you care:
#
#   docker exec -i -e PGPASSWORD="$PG_PASSWORD" supabase-db \
#     pg_dump -U postgres -d yorik_tenant_<name> \
#     | gzip > tenant-<name>-$(date +%Y%m%d).sql.gz
#
# Pair-script with scripts/create-tenant.sh.

set -Eeuo pipefail
cd "$(dirname "$0")/.."

if [[ -t 1 ]]; then
  RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; RST=$'\033[0m'
else
  RED=""; GRN=""; YEL=""; RST=""
fi
fatal() { printf "%s  ✗ %s%s\n" "${RED}" "$1" "${RST}" >&2; exit 1; }

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <tenant-name>" >&2
  exit 2
fi
NAME="$1"
if [[ ! "$NAME" =~ ^[a-z][a-z0-9_]{0,23}$ ]]; then
  fatal "tenant name must be lowercase letters + digits + underscore (≤24 chars)"
fi

DB_NAME="yorik_tenant_${NAME}"
TENANT_DIR="data/tenants/$NAME"

SUPABASE_ENV="infra/supabase/docker/.env"
[[ -f "$SUPABASE_ENV" ]] || fatal "$SUPABASE_ENV missing"
PG_PASSWORD=$(grep -E "^POSTGRES_PASSWORD=" "$SUPABASE_ENV" | head -1 | cut -d= -f2-)

mkdir -p data/locks
LOCK_FILE="data/locks/tenant-${NAME}.lock"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  fatal "another tenant operation is already in flight for '$NAME' (lock $LOCK_FILE held)"
fi

# Confirm before dropping — accidental wipes are nasty.
read -rp "${YEL}This will DROP DATABASE $DB_NAME, stop the systemd unit, soft-delete the tenant's Immich+Paperless users, and remove $TENANT_DIR. Proceed? [y/N] ${RST}" CONFIRM
[[ "$CONFIRM" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }

# ─── 1. Stop + disable the tenant systemd unit (if any) ──────────────────
# We do this before the upstream cleanup so the tenant FastAPI can't
# observe / re-create users mid-teardown. Best-effort: if systemctl
# isn't around or the unit was never installed, fall through quietly.
# `is-enabled` returns 0 for enabled / 1 for not — `2>/dev/null || true`
# eats the "unit not found" stderr so the operator doesn't see noise
# on fresh drops where the systemd template never got installed.
SYSTEMD_UNIT="yorik-tenant@$NAME.service"
if command -v systemctl >/dev/null 2>&1; then
  # `is-enabled` is the only reliable existence test for a template
  # instance — `list-unit-files` returns "0 unit files listed" with
  # exit 0 for instances (you'd have to query the template by name,
  # not the instance), which made the previous && chain skip the
  # whole block. is-enabled prints "enabled" / exit 0 for our case
  # and "disabled" / exit 1 otherwise; we just need exit 0.
  IS_ENABLED=$(systemctl is-enabled "$SYSTEMD_UNIT" 2>/dev/null || true)
  if [[ "$IS_ENABLED" == "enabled" || "$IS_ENABLED" == "active" ]]; then
    printf "${YEL}  ▸${RST} stopping + disabling %s …\n" "$SYSTEMD_UNIT"
    # No sudo — yorik.service runs with NoNewPrivileges=true which
    # blocks setuid. systemctl talks to systemd over D-Bus and asks
    # polkit; infra/polkit/50-yorik-tenant.rules grants the yorik user
    # the right to manage yorik-tenant@* units. install.sh installs
    # that rule; without it (manual install, container, etc.) this
    # falls back to a clear error rather than the cryptic sudo refusal.
    systemctl disable --now "$SYSTEMD_UNIT" 2>&1 | sed 's/^/    /' || \
      printf "${YEL}    · systemctl disable failed — check polkit rule at /etc/polkit-1/rules.d/50-yorik-tenant.rules%s\n" "${RST}"
    # Defensive symlink cleanup: `systemctl disable` on a template
    # instance occasionally races the `--now` and leaves the
    # multi-user.target.wants/ symlink dangling. Modern systemd
    # (>=255) handles this cleanly via D-Bus; older versions might
    # not. The symlink itself can only be removed by root, but
    # systemctl reenable+disable through D-Bus is idempotent and
    # achieves the same end state via polkit-authorised calls.
    SYMLINK="/etc/systemd/system/multi-user.target.wants/$SYSTEMD_UNIT"
    if [[ -L "$SYMLINK" ]]; then
      # First-try the D-Bus path (polkit handles auth).
      systemctl reenable "$SYSTEMD_UNIT" 2>/dev/null \
        && systemctl disable "$SYSTEMD_UNIT" 2>/dev/null || true
      # If the symlink survives, the operator can clean it up post-drop
      # with `sudo rm`. Leaving it doesn't break the next boot (the
      # tenant DB is already gone; systemd will log a failed-start and
      # move on) but it IS noise. Tell the operator.
      if [[ -L "$SYMLINK" ]]; then
        printf "${YEL}    · symlink %s persists — sudo rm -f to silence boot-time noise%s\n" \
          "$SYMLINK" "${RST}"
      fi
    fi
  fi
fi

# ─── 2. Soft-delete upstream Immich + Paperless users ────────────────────
# Tenants have ONE upstream Immich account per Yorik user, namespaced
# `<tenant>+<localpart>@<domain>` so we can find + delete them from the
# host without touching the tenant DB (which may already be gone if a
# previous drop crashed). Call the host's /api/internal/tenant/drop —
# the host has the admin tokens; the tenant doesn't (and shouldn't).
#
# Soft-failure: if the host endpoint is unreachable (host Yorik not
# running) we WARN and continue. Skipping leaves orphaned upstream
# users that the operator has to clean by hand later — annoying but
# safer than aborting the DB drop and leaving the tenant Postgres
# behind.
INTERNAL_TOKEN_FILE="data/internal_token"
HOST_URL="${YORIK_HOST_INTERNAL_URL:-http://127.0.0.1:8000}"
if [[ -r "$INTERNAL_TOKEN_FILE" ]]; then
  TOK=$(tr -d '\n' < "$INTERNAL_TOKEN_FILE")
  printf "${YEL}  ▸${RST} soft-deleting upstream Immich + Paperless users …\n"
  RESP=$(curl -fsS -X POST "$HOST_URL/api/internal/tenant/drop" \
           -H "Authorization: Bearer $TOK" \
           -H "Content-Type: application/json" \
           -d "{\"tenant_name\":\"$NAME\"}" 2>&1) || RESP=""
  if [[ -n "$RESP" ]]; then
    # Python is already a hard dep (venv) — use it for the JSON pretty.
    printf "%s\n" "$RESP" | ./venv/bin/python -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    for svc in ('immich', 'paperless'):
        s = d.get(svc, {})
        deleted = s.get('deleted', 0)
        errs = s.get('errors', [])
        skipped = s.get('skipped', 0) == -1
        if skipped:
            print(f'    · {svc}: skipped — {errs[0] if errs else \"unknown\"}')
        elif errs:
            print(f'    · {svc}: deleted {deleted}, {len(errs)} error(s):')
            for e in errs[:5]:
                print(f'        {e}')
        else:
            print(f'    ✓ {svc}: deleted {deleted}')
except Exception as e:
    print(f'    · could not parse drop response: {e}')
"
  else
    printf "%s    · host /api/internal/tenant/drop unreachable — upstream cleanup skipped%s\n" "${YEL}" "${RST}"
  fi
else
  printf "%s    · no host internal_token — upstream cleanup skipped (orphan users will remain)%s\n" "${YEL}" "${RST}"
fi

# ─── 3. Drop the tenant's Postgres database ──────────────────────────────
# Postgres refuses DROP DATABASE while any session is connected. Kick
# whoever is still on (FastAPI tenant pool, lingering psql) before
# trying the drop — otherwise the script fails with "database is being
# accessed by other users".
docker exec -i -e PGPASSWORD="$PG_PASSWORD" supabase-db \
  psql -U supabase_admin -d postgres -v ON_ERROR_STOP=1 >/dev/null <<SQL
SELECT pg_terminate_backend(pid)
  FROM pg_stat_activity
 WHERE datname = '$DB_NAME' AND pid <> pg_backend_pid();
DROP DATABASE IF EXISTS "$DB_NAME";
SQL

rm -rf "$TENANT_DIR"

# Unregister the tenant's bearer from the host's tenant_bearer_tokens
# registry. Without this, the bearer string remains valid until the
# table is manually trimmed, and an attacker who captured it (via a
# stale backup or hostile snapshot) could still authenticate as the
# deleted tenant. Legacy bearer auths this endpoint; we capture the
# actual HTTP status + body on failure so the operator can tell
# "host is down" from "wrong token" from "endpoint changed shape."
LEGACY_TOKEN_FILE="data/internal_token"
HOST_URL="${YORIK_HOST_INTERNAL_URL:-http://127.0.0.1:8000}"
if [[ ! -r "$LEGACY_TOKEN_FILE" ]]; then
  printf "%s    · skip bearer unregister: %s missing (operator scripts can't auth without it)%s\n" \
    "${YEL}" "$LEGACY_TOKEN_FILE" "${RST}"
else
  LEGACY_TOKEN=$(cat "$LEGACY_TOKEN_FILE")
  UNREG_RESP=$(curl -sS -o /tmp/yorik-unreg-body.$$ -w "%{http_code}" \
                 -X POST "$HOST_URL/api/internal/unregister-tenant-bearer" \
                 -H "Authorization: Bearer $LEGACY_TOKEN" \
                 -H "Content-Type: application/json" \
                 -d "{\"tenant_name\":\"$NAME\"}" 2>&1) || UNREG_RESP="000"
  if [[ "$UNREG_RESP" == "200" ]]; then
    : # quiet success
  elif [[ "$UNREG_RESP" == "000" ]]; then
    printf "%s    · couldn't reach host at %s — bearer remains registered (run drop-tenant again after host is back)%s\n" \
      "${YEL}" "$HOST_URL" "${RST}"
  else
    UNREG_BODY=$(head -c 200 /tmp/yorik-unreg-body.$$ 2>/dev/null)
    printf "%s    · unregister failed: HTTP %s %s — bearer may remain in host's tenant_bearer_tokens (manually clear with DELETE FROM tenant_bearer_tokens WHERE tenant_name='%s')%s\n" \
      "${YEL}" "$UNREG_RESP" "$UNREG_BODY" "$NAME" "${RST}"
  fi
  rm -f /tmp/yorik-unreg-body.$$
fi

# Remove the Caddy snippet too so a subsequent Caddy reload drops the
# route. Until that reload runs, requests to <tenant>.<domain> still
# answer with a 502 because there's nothing on the port anymore.
CADDY_SNIPPET="infra/caddy/tenants/$NAME.caddy"
if [[ -f "$CADDY_SNIPPET" ]]; then
  rm -f "$CADDY_SNIPPET"
  printf "%s  ▸%s removed %s — reload Caddy: sudo systemctl reload caddy\n" \
    "${YEL}" "${RST}" "$CADDY_SNIPPET"
fi

printf "%s  ✓ tenant %s dropped%s\n" "${GRN}" "$NAME" "${RST}"
