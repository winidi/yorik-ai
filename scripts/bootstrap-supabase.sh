#!/usr/bin/env bash
# Bring up the bundled Supabase stack + apply migrations_pg/*.sql.
#
# Idempotent. Called by start.sh (and install.sh) when
# YORIK_DB_BACKEND=postgres. Safe to re-run: brings up missing
# containers, leaves running ones alone; applies only migrations not
# already recorded in schema_migrations.
#
# Why this exists separately: start.sh's PHASE 5 (database) runs
# `python -m backend.database`, which needs a live Postgres on :5435
# to talk to. Pre-Phase-D start.sh just opened a SQLite file — there
# was nothing to bring up. With Postgres, the stack has to be in
# place BEFORE backend.database is invoked.

set -Eeuo pipefail

cd "$(dirname "$0")/.."

# ─── colours (mirror start.sh) ────────────────────────────────────────
if [[ -t 1 ]]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; GRN=$'\033[32m'
  YEL=$'\033[33m'; CYA=$'\033[36m'; RST=$'\033[0m'
else
  BOLD=""; DIM=""; GRN=""; YEL=""; CYA=""; RST=""
fi
say()  { printf "  %s▸%s %s\n" "${CYA}" "${RST}" "$1"; }
ok()   { printf "  %s✓%s %s\n" "${GRN}" "${RST}" "$1"; }
skip() { printf "  %s·%s %s\n" "${YEL}" "${RST}" "$1"; }
warn() { printf "  %s⚠%s %s\n" "${YEL}" "${RST}" "$1"; }
fatal() { printf "%s  ✗ %s%s\n" $'\033[31m' "$1" "${RST}" >&2; exit 1; }

SUPABASE_DIR="infra/supabase/docker"

# ─── Auto-clone Supabase if missing ──────────────────────────────────
# infra/supabase/ is gitignored (the supabase repo is too big to vendor),
# so first-install boxes won't have it. Clone it shallowly the first
# time bootstrap runs. The Yorik-specific compose override lives at
# infra/supabase-overlay/docker-compose.yorik.yml (git-tracked) and
# gets copied in alongside.
if [[ ! -d "$SUPABASE_DIR" ]]; then
  say "first-install: cloning supabase/supabase --depth 1 (this takes ~30s, downloads ~500MB)"
  if ! git clone --depth 1 https://github.com/supabase/supabase.git \
       infra/supabase >/dev/null 2>&1; then
    fatal "supabase clone failed — check network, then re-run start.sh"
  fi
  ok "supabase cloned"
fi

# Yorik-specific compose override (publishes Postgres on host :5435).
# Tracked at infra/supabase-overlay/; copy into the supabase dir on
# every bootstrap so a re-cloned supabase doesn't lose it.
YORIK_OVERLAY="infra/supabase-overlay/docker-compose.yorik.yml"
if [[ -f "$YORIK_OVERLAY" ]]; then
  cp -f "$YORIK_OVERLAY" "$SUPABASE_DIR/docker-compose.yorik.yml"
fi

# ─── Auto-generate Supabase .env on first install ────────────────────
# generate-keys.sh stamps fresh JWT_SECRET, ANON_KEY, SERVICE_ROLE_KEY,
# POSTGRES_PASSWORD, etc. into .env. Without --update-env it just
# prints them and exits 1. We catch that case here so the operator
# never has to know about the dance.
if [[ ! -f "$SUPABASE_DIR/.env" ]]; then
  say "first-install: generating Supabase .env (fresh secrets)"
  cp "$SUPABASE_DIR/.env.example" "$SUPABASE_DIR/.env"
  if [[ -x "$SUPABASE_DIR/utils/generate-keys.sh" ]]; then
    (cd "$SUPABASE_DIR" && bash utils/generate-keys.sh --update-env >/dev/null 2>&1) \
      || warn "generate-keys.sh failed; .env left at example defaults"
  fi
  # Override Kong's default 8000 to 8400 so Yorik can bind 8000 (the
  # host's normal FastAPI port). Same shift for HTTPS (8443→8453).
  # POSTGRES_PORT (and Supavisor's 5432/6543 host bindings) stay at
  # the upstream defaults; install.sh pre-flight has already
  # verified every required port is free, so a clash fails loudly
  # before we get here instead of after a half-up stack.
  sed -i \
      -e 's/^KONG_HTTP_PORT=8000$/KONG_HTTP_PORT=8400/' \
      -e 's/^KONG_HTTPS_PORT=8443$/KONG_HTTPS_PORT=8453/' \
      "$SUPABASE_DIR/.env"
  ok "Supabase .env stamped (Kong on 8400, Postgres on 5435)"
fi

# Read .env. We can't `source` the file directly — docker compose env
# files allow unquoted values with spaces (e.g.
# STUDIO_DEFAULT_ORGANIZATION=Default Organization) which bash misreads
# as commands. Use a narrow grep+cut so we only pluck the values we
# actually need without paying for the others.
env_value() {
  local key="$1"
  local default="${2:-}"
  local v
  v=$(grep -E "^${key}=" "$SUPABASE_DIR/.env" 2>/dev/null \
      | head -1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//')
  echo "${v:-$default}"
}
POSTGRES_PORT=$(env_value POSTGRES_PORT_EXT 5435)
POSTGRES_DB=$(env_value POSTGRES_DB postgres)
KONG_HTTP_PORT=$(env_value KONG_HTTP_PORT 8400)
ANON_KEY=$(env_value ANON_KEY x)

# Docker command — sg-wrap if the calling shell doesn't have the
# docker group active yet (install.sh sets DOCKER_PREFIX accordingly;
# bare invocations from a normal shell get the empty prefix).
docker_cmd() { ${DOCKER_PREFIX:-} docker "$@"; }
# docker-compose.yorik.yml is the Yorik-specific override (exposes
# Postgres on host :5435 so backend.database can connect with
# psycopg). Without it, supabase-db only listens on the internal
# Docker network and FastAPI gets "Connection refused" on 5435.
dc() {
  docker_cmd compose \
    -f "$SUPABASE_DIR/docker-compose.yml" \
    -f "$SUPABASE_DIR/docker-compose.yorik.yml" \
    "$@"
}

# ─── 1. bring up the stack ────────────────────────────────────────────
say "Supabase: docker compose up -d"
if dc ps --status=running 2>/dev/null | grep -q supabase-db; then
  skip "supabase-db already running"
else
  dc up -d >/dev/null
fi

# ─── 2. wait for Postgres ready ───────────────────────────────────────
say "waiting for Postgres :$POSTGRES_PORT (up to 90s)"
for i in $(seq 1 90); do
  if docker_cmd exec supabase-db pg_isready -U postgres -d "$POSTGRES_DB" \
       >/dev/null 2>&1; then
    ok "Postgres healthy"
    break
  fi
  sleep 1
done
if ! docker_cmd exec supabase-db pg_isready -U postgres -d "$POSTGRES_DB" \
     >/dev/null 2>&1; then
  fatal "Postgres didn't become ready in 90s" \
        "check: docker logs supabase-db"
fi

# Wait for the auth + rest services too — they need to apply their own
# migrations before we layer Yorik's on top. Realtime/storage join
# the publication after they boot; missing them at this stage causes
# Yorik startup to log spurious "relation does not exist" warnings.
say "waiting for GoTrue auth + PostgREST"
# Kong sits in front of every Supabase upstream and rejects requests
# without an `apikey` header at 401 even when the upstream is healthy.
# Without the header on the auth probe, the loop ran the full 60s
# every boot (and bootstrap fell through with a false "up" anyway).
# Including the ANON_KEY on both probes makes the loop exit within
# a second once the services are actually ready.
for i in $(seq 1 60); do
  AUTH_OK=$(curl -fsS --max-time 1 "http://localhost:${KONG_HTTP_PORT}/auth/v1/health" \
            -H "apikey: ${ANON_KEY}" >/dev/null 2>&1 && echo y || echo n)
  REST_OK=$(curl -fsS --max-time 1 "http://localhost:${KONG_HTTP_PORT}/rest/v1/" \
            -H "apikey: ${ANON_KEY}" >/dev/null 2>&1 && echo y || echo n)
  [[ "$AUTH_OK" == "y" && "$REST_OK" == "y" ]] && break
  sleep 1
done
ok "Supabase services up"

# ─── 3. apply Yorik migrations ────────────────────────────────────────
say "applying Yorik migrations from migrations_pg/"

# Share the same schema_migrations table backend/migrations.py uses
# at runtime. Without this, FastAPI's startup-time migration runner
# (run_pending_pg) doesn't know what the bootstrap already applied,
# tries to re-run as `postgres` (the FastAPI pool's role), and crashes
# because `postgres` lacks the higher privileges needed to write into
# the `docs` schema that supabase_admin owns.
docker_cmd exec -i supabase-db psql -U supabase_admin -d "$POSTGRES_DB" \
  -v ON_ERROR_STOP=1 >/dev/null <<'SQL'
CREATE TABLE IF NOT EXISTS schema_migrations (
  version    INTEGER PRIMARY KEY,
  name       TEXT NOT NULL,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
);
-- Make sure backend/migrations.py (running as `postgres`) can read it.
GRANT SELECT, INSERT, UPDATE, DELETE ON schema_migrations TO postgres;
SQL

# Pre-Phase-E migrations (000_*) were the Phase D import path for
# carrying an existing SQLite database into Postgres. They define
# user_profiles.id as BIGSERIAL and create dozens of Yorik-core tables
# with integer foreign keys. Phase E (100_*) is the UUID-native reset
# — it expects to be the canonical schema. Because each migration
# uses CREATE TABLE IF NOT EXISTS, running 000 before 100 leaves the
# BIGSERIAL columns in place and Phase E RLS helpers fail with
# 'operator does not exist: bigint = uuid'.
#
# Skip 000_* on fresh installs: 100_* is sufficient and authoritative
# for the Phase E schema. Existing Phase D installs that need an
# in-place upgrade have a separate (manual) port.
APPLIED_COUNT=0
SKIPPED_COUNT=0
LEGACY_SKIPPED=0
for sql in migrations_pg/*.sql; do
  version=$(basename "$sql" .sql)
  if [[ "$version" =~ ^000_ ]]; then
    LEGACY_SKIPPED=$((LEGACY_SKIPPED + 1))
    continue
  fi
  # backend/migrations.py uses INTEGER version PKs; filenames are
  # zero-padded ints. Strip the leading zeros so `100_phase_e_init`
  # becomes 100 (matches what backend/migrations.py:discover() reads).
  version_int=$(echo "$version" | sed -E 's/^0*([0-9]+).*/\1/')
  name_only=$(echo "$version" | sed -E "s/^${version_int}_//")

  EXISTS=$(docker_cmd exec -i supabase-db psql -U supabase_admin -d "$POSTGRES_DB" \
           -tA -c "SELECT 1 FROM schema_migrations WHERE version=$version_int" \
           2>/dev/null)
  if [[ "$EXISTS" == "1" ]]; then
    SKIPPED_COUNT=$((SKIPPED_COUNT + 1))
    continue
  fi
  printf "    applying %s ... " "$version"
  # supabase_admin has the higher privileges needed for cluster-level
  # operations like GRANT SET ON PARAMETER (108_phase_e_pgrst_db_schemas_grant.sql).
  if docker_cmd exec -i supabase-db psql -U supabase_admin -d "$POSTGRES_DB" \
       -v ON_ERROR_STOP=1 -q < "$sql" >/dev/null 2>&1; then
    docker_cmd exec -i supabase-db psql -U supabase_admin -d "$POSTGRES_DB" -q \
      -c "INSERT INTO schema_migrations (version, name) VALUES ($version_int, '$name_only') ON CONFLICT (version) DO NOTHING" >/dev/null
    APPLIED_COUNT=$((APPLIED_COUNT + 1))
    printf "ok\n"
  else
    printf "FAILED\n"
    fatal "migration $version failed" \
          "docker exec supabase-db psql -U supabase_admin -d $POSTGRES_DB < migrations_pg/${version}.sql"
  fi
done

ok "$APPLIED_COUNT new, $SKIPPED_COUNT already applied, $LEGACY_SKIPPED legacy (pre-Phase-E) skipped"
