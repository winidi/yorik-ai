#!/usr/bin/env bash
# Apply pending Phase E+ migrations to every tenant database.
#
# Why this exists: bootstrap-supabase.sh only migrates the HOST's
# postgres DB. Tenant DBs (yorik_tenant_<name>) sit in the same
# cluster but aren't touched at host startup. After `git pull` brings
# in a new migrations_pg/<n>.sql, host migrates at next FastAPI
# start, but tenants stay on the old schema until you re-create them
# or run this script.
#
# Usage:
#   bash scripts/migrate-tenants.sh             # all tenants
#   bash scripts/migrate-tenants.sh mom dad     # specific ones
#
# Idempotent. Same skip rules as scripts/create-tenant.sh:
#   * 000_ (Phase D legacy SQLite bridge) skipped
#   * 104, 107, 108 (cluster-level — supabase_realtime + service_role
#     + custom GUC grants) marked applied without running
#   * everything else applied via supabase_admin, recorded in the
#     tenant's own schema_migrations table

set -Eeuo pipefail
cd "$(dirname "$0")/.."

if [[ -t 1 ]]; then
  BOLD=$'\033[1m'; RED=$'\033[31m'; GRN=$'\033[32m'
  YEL=$'\033[33m'; CYA=$'\033[36m'; RST=$'\033[0m'
else
  BOLD=""; RED=""; GRN=""; YEL=""; CYA=""; RST=""
fi
say()  { printf "  %s▸%s %s\n" "${CYA}" "${RST}" "$1"; }
ok()   { printf "  %s✓%s %s\n" "${GRN}" "${RST}" "$1"; }
skip() { printf "  %s·%s %s\n" "${YEL}" "${RST}" "$1"; }
fatal() { printf "%s  ✗ %s%s\n" "${RED}" "$1" "${RST}" >&2; exit 1; }

SUPABASE_ENV="infra/supabase/docker/.env"
[[ -f "$SUPABASE_ENV" ]] || fatal "$SUPABASE_ENV missing — run bootstrap-supabase.sh first"
PG_PASSWORD=$(grep -E "^POSTGRES_PASSWORD=" "$SUPABASE_ENV" | head -1 | cut -d= -f2-)
[[ -n "$PG_PASSWORD" ]] || fatal "POSTGRES_PASSWORD not in $SUPABASE_ENV"

if ! docker inspect -f '{{.State.Running}}' supabase-db 2>/dev/null | grep -q true; then
  fatal "supabase-db container not running — bash start.sh first"
fi

psql_admin() {
  docker exec -i -e PGPASSWORD="$PG_PASSWORD" supabase-db \
    psql -U supabase_admin -d "${1:-postgres}" -v ON_ERROR_STOP=1
}
psql_admin_q() {
  docker exec -i -e PGPASSWORD="$PG_PASSWORD" supabase-db \
    psql -U supabase_admin -d "${1:-postgres}" -tA -v ON_ERROR_STOP=1
}

# ─── Discover tenants ─────────────────────────────────────────────────
if [[ $# -gt 0 ]]; then
  TENANTS=("$@")
else
  TENANTS=()
  while IFS= read -r -d '' f; do
    d=$(dirname "$f")
    TENANTS+=("$(basename "$d")")
  done < <(find data/tenants -mindepth 2 -maxdepth 2 -name manifest.env -print0 2>/dev/null)
fi

if [[ ${#TENANTS[@]} -eq 0 ]]; then
  say "no tenants to migrate"
  exit 0
fi

say "migrating ${#TENANTS[@]} tenant(s): ${TENANTS[*]}"

# ─── Per-tenant migration sweep ────────────────────────────────────────
TOTAL_APPLIED=0
TOTAL_SKIPPED=0
for NAME in "${TENANTS[@]}"; do
  # Whitelist guard — same regex create-tenant.sh uses.
  if [[ ! "$NAME" =~ ^[a-z][a-z0-9_]{0,23}$ ]]; then
    skip "$NAME: invalid name, skipping"
    continue
  fi
  DB="yorik_tenant_${NAME}"
  EXISTS=$(psql_admin_q postgres <<SQL 2>/dev/null
SELECT 1 FROM pg_database WHERE datname='$DB';
SQL
)
  if [[ "$EXISTS" != "1" ]]; then
    skip "$NAME: no database $DB, skipping (run create-tenant.sh first)"
    continue
  fi

  # Per-tenant flock to serialise against an in-flight create/drop.
  mkdir -p data/locks
  LOCK_FILE="data/locks/tenant-${NAME}.lock"
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    skip "$NAME: lock held by another operation, skipping"
    exec 9>&-
    continue
  fi

  printf "  ${BOLD}%s${RST}\n" "$NAME"

  # Ensure schema_migrations exists (it should from create-tenant.sh,
  # but a hand-crafted tenant might not have it).
  psql_admin "$DB" >/dev/null <<'SQL'
CREATE TABLE IF NOT EXISTS schema_migrations (
  version    INTEGER PRIMARY KEY,
  name       TEXT NOT NULL,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
);
SQL

  APPLIED=0
  SKIPPED=0
  for sql in migrations_pg/*.sql; do
    base=$(basename "$sql" .sql)
    vint=$(echo "$base" | sed -E 's/^0*([0-9]+).*/\1/')
    name_only=$(echo "$base" | sed -E "s/^${vint}_//")

    # Skip Phase D legacy bridge (it's SQLite → Postgres data import).
    if [[ "$base" =~ ^000_ ]]; then
      continue
    fi

    HAVE=$(psql_admin_q "$DB" <<SQL 2>/dev/null
SELECT 1 FROM schema_migrations WHERE version=$vint;
SQL
)
    if [[ "$HAVE" == "1" ]]; then
      SKIPPED=$((SKIPPED + 1))
      continue
    fi

    # Cluster-level migrations: mark applied without running. Same
    # set create-tenant.sh recognises.
    case "$base" in
      104_phase_e_realtime|\
      107_phase_e_service_role_public|\
      108_phase_e_pgrst_db_schemas_grant)
        psql_admin "$DB" >/dev/null <<SQL
INSERT INTO schema_migrations (version, name) VALUES ($vint, '$name_only')
ON CONFLICT (version) DO NOTHING;
SQL
        APPLIED=$((APPLIED + 1))
        printf "    applying %-40s%s (cluster-level marker)%s\n" "$base" "${YEL}" "${RST}"
        continue ;;
    esac

    printf "    applying %-40s" "$base"
    if psql_admin "$DB" < "$sql" >/dev/null 2>&1; then
      psql_admin "$DB" >/dev/null <<SQL
INSERT INTO schema_migrations (version, name) VALUES ($vint, '$name_only')
ON CONFLICT (version) DO NOTHING;
SQL
      APPLIED=$((APPLIED + 1))
      printf "ok\n"
    else
      printf "%sFAILED%s\n" "${RED}" "${RST}"
      printf "    %s· stopping at first failure for %s — re-run after fixing%s\n" "${YEL}" "$NAME" "${RST}"
      exec 9>&-
      exit 1
    fi
  done

  exec 9>&-
  TOTAL_APPLIED=$((TOTAL_APPLIED + APPLIED))
  TOTAL_SKIPPED=$((TOTAL_SKIPPED + SKIPPED))
  ok "$NAME: $APPLIED applied, $SKIPPED already up-to-date"
done

ok "done — $TOTAL_APPLIED new migration(s) across ${#TENANTS[@]} tenant(s) ($TOTAL_SKIPPED already applied)"
