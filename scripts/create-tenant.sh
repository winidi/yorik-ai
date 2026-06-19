#!/usr/bin/env bash
# Create a new Yorik tenant: a fresh Postgres database inside the
# shared supabase-db, with all migrations applied, ready for a Yorik
# FastAPI process to connect to via YORIK_DB_NAME=<dbname>.
#
# Phase F-lite (database-per-tenant) v1 — the cheap multi-tenant
# pattern that fits 6 households on a 16 GB box:
#   * 1 shared supabase-db Postgres cluster (existing install)
#   * 1 bare Postgres database per tenant (created by this script)
#   * 1 Yorik FastAPI process per tenant (run separately on its own
#     port; this script doesn't manage the runtime)
#   * No per-tenant GoTrue / PostgREST / Realtime — Yorik FastAPI
#     covers the whole API surface. The platform's community-app
#     features (per-app schema, scoped JWT) stay disabled for tenant
#     instances; they're available on the maintainer's own Yorik.
#
# Usage:
#   bash scripts/create-tenant.sh <name>
# Where <name> is a lowercase identifier (e.g. "mom", "dirk", "alex").
# Produces a database called yorik_tenant_<name>.
#
# Side effects:
#   * CREATE DATABASE yorik_tenant_<name> (as supabase_admin)
#   * Initialises auth.users + auth.uid() shim so Phase E migrations
#     that reference them work without a real GoTrue instance
#   * Applies migrations_pg/100_phase_e_init.sql onwards
#   * Records a 'tenant manifest' file under data/tenants/<name>/ with
#     the env vars the operator needs to set when launching the
#     tenant Yorik
#   * Prints a copy-paste runbook at the end
#
# Idempotent: re-running detects an existing database and prints the
# manifest instead of erroring. To wipe a tenant: scripts/drop-tenant.sh

set -Eeuo pipefail
cd "$(dirname "$0")/.."

if [[ -t 1 ]]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GRN=$'\033[32m'
  YEL=$'\033[33m'; CYA=$'\033[36m'; RST=$'\033[0m'
else
  BOLD=""; DIM=""; RED=""; GRN=""; YEL=""; CYA=""; RST=""
fi
say()  { printf "  %s▸%s %s\n" "${CYA}" "${RST}" "$1"; }
ok()   { printf "  %s✓%s %s\n" "${GRN}" "${RST}" "$1"; }
skip() { printf "  %s·%s %s\n" "${YEL}" "${RST}" "$1"; }
fatal() { printf "%s  ✗ %s%s\n" "${RED}" "$1" "${RST}" >&2; exit 1; }

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <tenant-name>" >&2
  exit 2
fi
NAME="$1"
# Whitelist — tenant names become database identifiers + filesystem
# paths. Lowercase ASCII + digits + underscore, max 24 chars. Keeps
# the SQL identifier interpolation below safe without escaping.
if [[ ! "$NAME" =~ ^[a-z][a-z0-9_]{0,23}$ ]]; then
  fatal "tenant name must be lowercase letters + digits + underscore (≤24 chars)"
fi

DB_NAME="yorik_tenant_${NAME}"
SUPABASE_ENV="infra/supabase/docker/.env"

# ─── 0. Tenant-name file lock — serialise create/drop on this name ──
# flock on a per-tenant lock file in data/locks/. Two concurrent
# create-tenant.sh runs against the same name would otherwise hand
# back the same port + race the manifest write; create-vs-drop on
# the same name leaves the upstream cleanup and DB drop interleaved.
# Released automatically on script exit (FD 9 closes).
mkdir -p data/locks
LOCK_FILE="data/locks/tenant-${NAME}.lock"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  fatal "another tenant operation is already in flight for '$NAME' (lock $LOCK_FILE held)"
fi
[[ -f "$SUPABASE_ENV" ]] || \
  fatal "$SUPABASE_ENV missing — run bootstrap-supabase.sh first"

PG_PASSWORD=$(grep -E "^POSTGRES_PASSWORD=" "$SUPABASE_ENV" \
              | head -1 | cut -d= -f2-)
[[ -n "$PG_PASSWORD" ]] || fatal "POSTGRES_PASSWORD not in $SUPABASE_ENV"

# All cluster-level ops (CREATE DATABASE, schema-level grants) go
# through supabase_admin; postgres lacks the privileges. Yorik's
# FastAPI runtime continues to use postgres for normal queries.
psql_admin() {
  docker exec -i -e PGPASSWORD="$PG_PASSWORD" supabase-db \
    psql -U supabase_admin -d "${1:-postgres}" -v ON_ERROR_STOP=1
}
# Tuples-only + unaligned variant for "is X true?" lookups — strips
# header / footer / pipe separators so a one-row SELECT returns just
# the bare value (e.g. "1" instead of " ?column? \n----\n 1 \n…").
# The original psql_admin (with formatting) is still used everywhere
# the result is dumped > /dev/null and only the exit code matters.
psql_admin_q() {
  docker exec -i -e PGPASSWORD="$PG_PASSWORD" supabase-db \
    psql -U supabase_admin -d "${1:-postgres}" -tA -v ON_ERROR_STOP=1
}

if ! docker inspect -f '{{.State.Running}}' supabase-db 2>/dev/null | grep -q true; then
  fatal "supabase-db container not running" \
        "bash start.sh (or: cd infra/supabase/docker && docker compose up -d)"
fi

# ─── 1. CREATE DATABASE (idempotent) ──────────────────────────────────
EXISTS=$(psql_admin_q postgres <<SQL 2>/dev/null
SELECT 1 FROM pg_database WHERE datname='$DB_NAME';
SQL
)

if [[ "$EXISTS" == "1" ]]; then
  skip "database $DB_NAME already exists"
  ALREADY=1
else
  say "creating database $DB_NAME"
  # `CREATE DATABASE` can't run inside the multi-statement script body;
  # send it as its own one-liner.
  echo "CREATE DATABASE \"$DB_NAME\";" | psql_admin postgres >/dev/null
  ok "database $DB_NAME created"
  ALREADY=0
fi

# ─── 2. auth shim — minimal stand-in for GoTrue's auth schema ────────
# The Phase E migrations reference auth.users(id) as a FK target and
# call auth.uid() inside RLS policies. A tenant database doesn't have
# its own GoTrue instance (we share the maintainer's one for the
# maintainer's tenant; everyone else gets local-only auth via Yorik
# FastAPI). Create the structural minimum so the migrations succeed:
#
#   * auth schema
#   * auth.users(id UUID PK) — Yorik writes a row here per local user;
#                              the rest of GoTrue's columns are unused
#   * auth.uid() RETURNS uuid — returns NULL since there's no JWT
#                                context inside FastAPI's
#                                `postgres`-as-superuser connection
#                                (BYPASSRLS anyway). The RLS policies
#                                still parse-check the function exists.
say "installing auth shim in $DB_NAME"
psql_admin "$DB_NAME" <<'SQL' >/dev/null
CREATE SCHEMA IF NOT EXISTS auth;
CREATE TABLE IF NOT EXISTS auth.users (
  id UUID PRIMARY KEY,
  email TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE OR REPLACE FUNCTION auth.uid() RETURNS uuid
  LANGUAGE sql STABLE
  AS $$ SELECT NULL::uuid $$;
SQL
ok "auth shim installed"

# ─── 3. Apply Phase E migrations ──────────────────────────────────────
# Skip migration 000 (the SQLite-import bridge) and any migration that
# can't run inside a tenant DB because it touches cluster-level objects
# the tenant doesn't own (107 = grant on public to service_role,
# 108 = grant SET on a custom GUC). Those only matter when there's a
# real Supabase services stack on top, which tenant DBs don't have.
say "applying Phase E migrations to $DB_NAME"
psql_admin "$DB_NAME" <<'SQL' >/dev/null
CREATE TABLE IF NOT EXISTS schema_migrations (
  version    INTEGER PRIMARY KEY,
  name       TEXT NOT NULL,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
);
SQL

APPLIED=0
SKIPPED_LEGACY=0
SKIPPED_CLUSTER=0
SKIPPED_ALREADY=0
for sql in migrations_pg/*.sql; do
  base=$(basename "$sql" .sql)
  vint=$(echo "$base" | sed -E 's/^0*([0-9]+).*/\1/')
  name_only=$(echo "$base" | sed -E "s/^${vint}_//")

  # Skip Phase D import bridge.
  if [[ "$base" =~ ^000_ ]]; then
    SKIPPED_LEGACY=$((SKIPPED_LEGACY + 1)); continue
  fi
  # Skip cluster-level / Supabase-services migrations: they assume a
  # full Kong/Realtime/PostgREST stack on top of the database. Tenant
  # DBs run bare Postgres under FastAPI, so these are no-ops at best
  # and crash at worst.
  case "$base" in
    104_phase_e_realtime|\
    107_phase_e_service_role_public|\
    108_phase_e_pgrst_db_schemas_grant)
      # Mark as applied so backend/migrations.py's run_pending_pg
      # doesn't try them at FastAPI startup (it has no awareness of
      # the cluster-vs-tenant distinction; without the bookkeeping
      # entry it would re-execute the SQL and crash on missing
      # supabase_realtime / service_role).
      psql_admin "$DB_NAME" <<SQL >/dev/null
INSERT INTO schema_migrations (version, name) VALUES ($vint, '$name_only')
ON CONFLICT (version) DO NOTHING;
SQL
      SKIPPED_CLUSTER=$((SKIPPED_CLUSTER + 1)); continue ;;
  esac
  HAVE=$(psql_admin "$DB_NAME" <<SQL 2>/dev/null
SELECT 1 FROM schema_migrations WHERE version=$vint;
SQL
)
  HAVE=$(printf '%s' "$HAVE" | tr -d '[:space:]' | head -c 1)
  if [[ "$HAVE" == "1" ]]; then
    SKIPPED_ALREADY=$((SKIPPED_ALREADY + 1)); continue
  fi
  printf "    applying %-40s" "$base"
  if psql_admin "$DB_NAME" < "$sql" >/dev/null 2>&1; then
    psql_admin "$DB_NAME" <<SQL >/dev/null
INSERT INTO schema_migrations (version, name) VALUES ($vint, '$name_only')
ON CONFLICT (version) DO NOTHING;
SQL
    APPLIED=$((APPLIED + 1)); printf "ok\n"
  else
    printf "FAILED\n"
    fatal "migration $base failed against $DB_NAME"
  fi
done
ok "$APPLIED new, $SKIPPED_ALREADY already applied, $SKIPPED_LEGACY legacy + $SKIPPED_CLUSTER cluster-level skipped"

# ─── 4. Grant runtime access to `postgres` (FastAPI's pool role) ──────
# Postgres can't accept CURRENT_DATABASE() as a GRANT target — the
# database name has to be a literal identifier. Interpolate from
# $DB_NAME (already whitelisted by the name regex above) and
# double-quote it.
say "granting runtime access to postgres role"
# CREATE ON SCHEMA is the load-bearing one — backend/migrations.py
# CREATE TABLE IF NOT EXISTS schema_migrations on every startup. USAGE
# + ALL ON TABLES only covers existing tables; without CREATE the
# tenant FastAPI dies at boot with 'permission denied for schema
# public'.
psql_admin "$DB_NAME" <<SQL >/dev/null
GRANT ALL ON DATABASE "$DB_NAME" TO postgres;
GRANT USAGE, CREATE ON SCHEMA public, yorik, docs, auth TO postgres;
GRANT ALL ON ALL TABLES    IN SCHEMA public TO postgres;
GRANT ALL ON ALL TABLES    IN SCHEMA docs   TO postgres;
GRANT ALL ON ALL TABLES    IN SCHEMA auth   TO postgres;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO postgres;
GRANT ALL ON ALL SEQUENCES IN SCHEMA docs   TO postgres;
GRANT ALL ON ALL FUNCTIONS IN SCHEMA yorik  TO postgres;
GRANT ALL ON ALL FUNCTIONS IN SCHEMA auth   TO postgres;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO postgres;
ALTER DEFAULT PRIVILEGES IN SCHEMA docs   GRANT ALL ON TABLES TO postgres;
ALTER DEFAULT PRIVILEGES IN SCHEMA auth   GRANT ALL ON TABLES TO postgres;
SQL
ok "runtime grants set"

# ─── 5. Write the tenant manifest ─────────────────────────────────────
TENANT_DIR="data/tenants/$NAME"
mkdir -p "$TENANT_DIR"

# Port allocation — scan existing manifests for HOMEOS_PORT values and
# pick the next-free above the current max. Starts at 8001 (host is
# always 8000). Lets the operator spin up tenants without thinking
# about which ports are free, and gives systemd a stable HOMEOS_PORT
# to bind in the EnvironmentFile.
#
# Resilience: `set -E` plus `find ... -print 2>/dev/null` swallow the
# "no matches" case cleanly — bash's bare-glob would leave the literal
# `*` in place and crash awk on the bogus filename.
MANIFEST_LIST=$(find data/tenants -mindepth 2 -maxdepth 2 -name manifest.env -print 2>/dev/null || true)
if [[ -n "$MANIFEST_LIST" ]]; then
  ALLOCATED_PORT=$(echo "$MANIFEST_LIST" | xargs awk -F= '
    /^HOMEOS_PORT=/ {gsub(/[^0-9]/, "", $2); if ($2+0 > max) max = $2+0}
    END {print (max ? max+1 : 8001)}')
else
  ALLOCATED_PORT=8001
fi
# Guard against the same tenant re-running and getting a fresh port —
# keep its previous one. Read from the manifest we're about to
# overwrite, if any. ALSO covers the restore-from-backup path:
# scripts/restore.sh writes the bundled manifest first, then (for
# manifest-only entries) shells out here; we want to respect the
# bundled HOMEOS_PORT so existing Caddy snippets / invite links keep
# pointing at the right address. Without this, a restored tenant
# silently lands on a fresh port and every URL that referenced the
# old one 502s.
if [[ -f "$TENANT_DIR/manifest.env" ]]; then
  EXISTING_PORT=$(awk -F= '/^HOMEOS_PORT=/ {gsub(/[^0-9]/, "", $2); print $2; exit}' "$TENANT_DIR/manifest.env")
  if [[ -n "$EXISTING_PORT" ]]; then
    ALLOCATED_PORT=$EXISTING_PORT
  fi
fi
ok "allocated port $ALLOCATED_PORT for $NAME"

# Per-tenant bearer token. The host's tenant_bearer_tokens table
# resolves token→tenant_name; every /api/internal/* endpoint that takes
# a `tenant_name` in the body enforces that the body matches the
# token's tenant_name. Generate a fresh token on every run (idempotent
# re-create rotates the bearer); the host's register endpoint replaces
# the previous row in-place.
TENANT_TOKEN=$(./venv/bin/python -c "import secrets; print(secrets.token_urlsafe(48))")
TENANT_TOKEN_FILE="$TENANT_DIR/internal_token"
echo -n "$TENANT_TOKEN" > "$TENANT_TOKEN_FILE"
chmod 600 "$TENANT_TOKEN_FILE"

# Register with the host. The host endpoint requires the LEGACY bearer
# (data/internal_token) — that's the bootstrap token the operator's
# scripts have. Without the legacy file there's no way to register, so
# we ensure it exists before calling (matches what the host startup
# hook does on a fresh install).
LEGACY_TOKEN_FILE="data/internal_token"
if [[ ! -f "$LEGACY_TOKEN_FILE" ]]; then
  ./venv/bin/python -c "
from backend.external_users import get_or_create_internal_token
get_or_create_internal_token()
" >/dev/null
fi
LEGACY_TOKEN=$(cat "$LEGACY_TOKEN_FILE")
HOST_URL="${YORIK_HOST_INTERNAL_URL:-http://127.0.0.1:8000}"
REG_RESP=$(curl -fsS -X POST "$HOST_URL/api/internal/register-tenant-bearer" \
             -H "Authorization: Bearer $LEGACY_TOKEN" \
             -H "Content-Type: application/json" \
             -d "{\"tenant_name\":\"$NAME\",\"token\":\"$TENANT_TOKEN\"}" 2>&1) \
  || REG_RESP=""
if [[ -z "$REG_RESP" ]]; then
  # Host might not be running yet (first-time setup during install).
  # Defer registration: the operator can re-run create-tenant.sh once
  # the host is up, and the script's idempotency will pick up where it
  # left off. Warn rather than fail — the manifest still gets written
  # so systemd can start the tenant when the host is back.
  printf "%s  ⚠ couldn't reach host at %s — tenant bearer not registered; re-run after host is up%s\n" \
    "${YEL}" "$HOST_URL" "${RST}" >&2
else
  ok "tenant bearer registered with host"
fi
cat > "$TENANT_DIR/manifest.env" <<EOF
# Yorik tenant: $NAME
# Generated by scripts/create-tenant.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)
#
# Run this tenant's Yorik FastAPI by sourcing this file before
# launching uvicorn (or via systemd EnvironmentFile=).

YORIK_DB_BACKEND=postgres
YORIK_DB_NAME=$DB_NAME

# Host-side proxy: this tenant doesn't have Immich/Paperless admin
# creds (and shouldn't — host is the only admin). When this tenant
# tries to provision a user, it POSTs to the host Yorik's
# /api/internal/provision endpoint; the host does the admin work and
# returns the per-user creds the tenant stores in its own DB.
YORIK_HOST_INTERNAL_URL=http://127.0.0.1:8000
# Per-tenant bearer — the host indexes this back to the tenant_name,
# so every /api/internal/* call this tenant makes is bound to its
# identity (no cross-tenant impersonation).
YORIK_HOST_INTERNAL_TOKEN_FILE=$(pwd)/data/tenants/$NAME/internal_token
YORIK_IS_TENANT=1

# Auto-allocated port (next-free above existing tenants). Override
# only if you're slotting around a port taken by something else.
HOMEOS_PORT=$ALLOCATED_PORT
HOMEOS_DB_PATH=data/tenants/$NAME/.family.db.stub
HOMEOS_DOCS_DB_PATH=data/tenants/$NAME/.documents.db.stub
HOMEOS_DOCS_DIR=data/tenants/$NAME/documents
EOF
ok "manifest written to $TENANT_DIR/manifest.env"

# ─── 6. Runbook ───────────────────────────────────────────────────────
# ─── 6b. Write Caddy reverse-proxy snippet (idempotent) ──────────────
# Operators with a Caddy front door point its main Caddyfile at
# infra/caddy/tenants/*.caddy via `import` (see infra/caddy/README.md).
# We always emit the snippet — even on installs that don't use Caddy
# it's a tiny harmless file (~150 bytes), and the import-or-not
# decision lives at the operator level, not here.
CADDY_DIR="infra/caddy/tenants"
mkdir -p "$CADDY_DIR"
TENANT_ROOT="${YORIK_TENANT_ROOT:-localhost}"
cat > "$CADDY_DIR/$NAME.caddy" <<EOF
# Yorik tenant: $NAME — generated by scripts/create-tenant.sh
# Reload Caddy after changes: sudo systemctl reload caddy
$NAME.$TENANT_ROOT {
    reverse_proxy 127.0.0.1:$ALLOCATED_PORT
}
EOF
ok "wrote Caddy snippet: $CADDY_DIR/$NAME.caddy ($NAME.$TENANT_ROOT → :$ALLOCATED_PORT)"

# ─── 7. Detect systemd template and emit start instructions ──────────
SYSTEMD_UNIT="/etc/systemd/system/yorik-tenant@.service"
HAS_SYSTEMD_UNIT=0
if [[ -f "$SYSTEMD_UNIT" ]]; then
  HAS_SYSTEMD_UNIT=1
fi

cat <<EOF

${BOLD}Tenant '$NAME' is ready.${RST}

  Database:  $DB_NAME (inside the shared supabase-db cluster)
  Port:      $ALLOCATED_PORT
  Manifest:  $TENANT_DIR/manifest.env

EOF

if [[ "$HAS_SYSTEMD_UNIT" == 1 ]]; then
  cat <<EOF
  ${BOLD}Start this tenant (systemd template installed):${RST}
    sudo systemctl enable --now yorik-tenant@$NAME.service

  ${BOLD}Tail the tenant logs:${RST}
    journalctl -u yorik-tenant@$NAME.service -f

EOF
else
  cat <<EOF
  ${BOLD}Install systemd template (one-time, host operator):${RST}
    sudo cp infra/systemd/yorik-tenant@.service /etc/systemd/system/
    sudo systemctl daemon-reload

  ${BOLD}Then start this tenant:${RST}
    sudo systemctl enable --now yorik-tenant@$NAME.service

  ${BOLD}Or run ad-hoc (no systemd, no auto-restart):${RST}
    set -a; . $TENANT_DIR/manifest.env; set +a
    ./venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port \$HOMEOS_PORT

EOF
fi

cat <<EOF
  ${BOLD}First user setup:${RST}
    The setup wizard at http://localhost:$ALLOCATED_PORT/ provisions the
    admin into auth.users (the shim, local-only — no GoTrue involved).

  ${BOLD}If you're running Caddy:${RST} reload it to pick up the snippet:
    sudo systemctl reload caddy

  ${BOLD}Drop this tenant:${RST}
    bash scripts/drop-tenant.sh $NAME

EOF
