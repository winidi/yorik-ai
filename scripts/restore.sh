#!/usr/bin/env bash
# Yorik restore — decrypt a backup snapshot and swap it in for the
# current data/ directory. Safety: the existing data/ is moved aside
# to data.before-restore-<ts>/ first, so a botched restore can be
# rolled back manually.
#
# Usage:
#   bash scripts/restore.sh /path/to/yorik-<timestamp>.tar.gz.age
#
# Run from the Yorik repo root.

set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <path-to-snapshot.tar.gz.age>" >&2
  exit 2
fi

SNAPSHOT="$1"
if [[ ! -f "$SNAPSHOT" ]]; then
  echo "Snapshot not found: $SNAPSHOT" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

if [[ ! -d "data" ]]; then
  echo "No data/ directory at $REPO_ROOT — is this the Yorik repo root?" >&2
  exit 1
fi

# Ask for the passphrase. Don't echo it.
echo
echo "▶ Restoring from: $SNAPSHOT"
read -rsp "Backup passphrase: " PASSPHRASE
echo
if [[ -z "$PASSPHRASE" ]]; then
  echo "No passphrase provided." >&2
  exit 1
fi

# Confirm before doing anything destructive.
TIMESTAMP="$(date +%Y-%m-%dT%H-%M-%S)"
BACKUP_DIR="data.before-restore-$TIMESTAMP"
echo
echo "This will:"
echo "  1. Stop the Yorik server on :8000 (if running)"
echo "  2. Move current ./data/ → ./$BACKUP_DIR (rollback if needed)"
echo "  3. Decrypt the snapshot and unpack it into ./data/"
echo
read -rp "Proceed? [y/N] " CONFIRM
if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
  echo "Aborted."
  exit 0
fi

# 1. Stop Yorik if running. fuser is fine for local-dev; in production
# the systemd unit would handle this.
echo "→ Stopping Yorik (if running) …"
if command -v fuser >/dev/null 2>&1; then
  fuser -k -9 8000/tcp 2>/dev/null || true
fi
sleep 1

# 2. Decrypt the snapshot FIRST, before touching data/. The snapshot
# often lives inside data/backups/ (the default target). If we moved
# data/ aside first, the decrypt step would fail to find its own
# input file. Read the encrypted bytes now, decrypt to a /tmp file
# the data/ rename can't reach.
if [[ ! -d "venv" ]]; then
  echo "venv/ not found — run install.sh first." >&2
  exit 1
fi

TMP_TAR="$(mktemp --suffix=.tar.gz)"
trap 'rm -f "$TMP_TAR"' EXIT

echo "→ Decrypting snapshot …"
if ! ./venv/bin/python -c "
import sys
from pathlib import Path
from pyrage import passphrase
data = Path('$SNAPSHOT').read_bytes()
try:
    plain = passphrase.decrypt(data, '$PASSPHRASE')
except Exception as e:
    print(f'Decrypt failed: {e}', file=sys.stderr)
    sys.exit(1)
Path('$TMP_TAR').write_bytes(plain)
"; then
  exit 1
fi

# 3. Move current data aside (after the decrypt succeeded, so a bad
# passphrase doesn't disturb the existing install).
echo "→ Moving current data/ to $BACKUP_DIR …"
mv data "$BACKUP_DIR"
mkdir data

echo "→ Extracting into data/ …"
tar -xzf "$TMP_TAR" -C data

# 4. Restore Yorik Postgres if the bundle contains a dump.
#
# For Postgres-backend installs the tarball ships
# data/yorik_postgres.sql.gz (public + yorik schemas) and
# data/yorik_postgres_docs.sql.gz (docs schema with paperless vectors).
# Both must be replayed into the running supabase-db; without this
# the restored Yorik comes up with empty tables — which silently
# defeats the entire point of the backup.
PG_PASSWORD=$(grep -E "^POSTGRES_PASSWORD=" infra/supabase/docker/.env 2>/dev/null \
              | head -1 | cut -d= -f2-)

# Schema-level operations (DROP/CREATE SCHEMA, GRANT) need to run as
# supabase_admin since that role owns the schemas that bootstrap-
# supabase.sh applied. The Yorik FastAPI runtime continues to use
# `postgres` for normal queries; we only need the elevated role for
# the wipe + replay.
psql_admin() {
  docker exec -i -e PGPASSWORD="$PG_PASSWORD" supabase-db \
    psql -U supabase_admin -d postgres -v ON_ERROR_STOP=1 "$@"
}

psql_into_supabase() {
  # stdin → psql in supabase-db. -v ON_ERROR_STOP=1 so a malformed
  # restore fails loudly instead of leaving a half-restored database.
  docker exec -i -e PGPASSWORD="$PG_PASSWORD" supabase-db \
    psql -U postgres -d postgres -v ON_ERROR_STOP=1 "$@"
}

restore_pg_dumps() {
  local main_file="data/yorik_postgres.sql.gz"
  local docs_file="data/yorik_postgres_docs.sql.gz"
  if [[ ! -f "$main_file" && ! -f "$docs_file" ]]; then
    return 0
  fi
  if ! docker inspect -f '{{.State.Running}}' supabase-db 2>/dev/null | grep -q true; then
    echo "⚠ supabase-db not running — Postgres dumps left in data/; replay later." >&2
    return 0
  fi
  if [[ -z "$PG_PASSWORD" ]]; then
    echo "⚠ POSTGRES_PASSWORD not found in infra/supabase/docker/.env — skipping replay" >&2
    return 0
  fi

  # Nuke the public + yorik + docs schemas in dependency-aware order
  # before replaying. pg_dump --clean tries to DROP individual objects,
  # but cross-table FKs on user_profiles.id make that fail with
  # 'cannot drop constraint user_profiles_pkey on table … because
  # other objects depend on it'. Dropping the schemas wholesale with
  # CASCADE sidesteps the dependency dance — the dumps recreate
  # everything anyway. Supabase service schemas (auth/storage/realtime)
  # are untouched.
  # DROP schemas + recreate empty public (so we can install
  # extensions into it before the dump's CREATE TABLE statements
  # reference public.vector / public.uuid_generate_v4 etc).
  # The dump's own DROP statements are gone (pg_dump --clean was
  # incompatible with cross-table FKs); the schemas come back via
  # CREATE TABLE / CREATE FUNCTION inside the dump.
  echo "→ Resetting target schemas (public, yorik, docs) …"
  psql_admin <<'SQL' >/dev/null
DROP SCHEMA IF EXISTS yorik CASCADE;
DROP SCHEMA IF EXISTS docs  CASCADE;
DROP SCHEMA IF EXISTS public CASCADE;
CREATE SCHEMA public;
GRANT ALL ON SCHEMA public TO postgres;
GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;
-- Re-install the cluster-level extensions Yorik tables depend on
-- (pg_dump assumes they're present and references public.vector /
-- public.uuid_generate_v4 directly). Without these the first
-- CREATE TABLE in the dump fails with 'type public.vector does
-- not exist'.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
SQL

  # The pg_dumps were taken as `postgres` and re-recreate objects
  # under that ownership; replay them through psql_admin too so the
  # schema_migrations + helper bootstrap runs with adequate privs,
  # then re-grant the FastAPI runtime role at the end.
  # The dumps contain `CREATE SCHEMA public;` near the top — pre-
  # Phase-D pg_dump emits the public-schema definition even though
  # Postgres auto-creates it. Since we already recreated public
  # (so CREATE EXTENSION vector has somewhere to live), strip that
  # one line on the way into psql; everything else is harmless to
  # re-execute. Same applies to `COMMENT ON SCHEMA public`.
  if [[ -f "$main_file" ]]; then
    echo "→ Restoring Yorik (public + yorik schemas) …"
    if ! zcat "$main_file" \
         | sed -E '/^CREATE SCHEMA public;$/d; /^COMMENT ON SCHEMA public IS/d' \
         | psql_admin >/dev/null; then
      echo "⚠ public+yorik replay failed — Postgres is in an inconsistent state." >&2
      return 1
    fi
    echo "✓ public + yorik schemas restored"
  fi

  if [[ -f "$docs_file" ]]; then
    echo "→ Restoring Yorik (docs schema + paperless vectors) …"
    if ! zcat "$docs_file" | psql_admin >/dev/null; then
      echo "⚠ docs replay failed — paperless search will return empty until re-ingested." >&2
      return 1
    fi
    echo "✓ docs schema restored"
  fi

  # Re-grant runtime access for the `postgres` role the FastAPI pool
  # uses. The pg_dump replayed everything as supabase_admin so the
  # newly-created tables would otherwise be unreadable from FastAPI.
  echo "→ Re-granting FastAPI runtime access …"
  psql_admin <<'SQL' >/dev/null
GRANT USAGE ON SCHEMA public, yorik, docs TO postgres;
GRANT ALL ON ALL TABLES    IN SCHEMA public TO postgres;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO postgres;
GRANT ALL ON ALL TABLES    IN SCHEMA docs   TO postgres;
GRANT ALL ON ALL SEQUENCES IN SCHEMA docs   TO postgres;
GRANT ALL ON ALL TABLES    IN SCHEMA yorik  TO postgres;
GRANT ALL ON ALL FUNCTIONS IN SCHEMA yorik  TO postgres;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO postgres;
ALTER DEFAULT PRIVILEGES IN SCHEMA docs   GRANT ALL ON TABLES TO postgres;
SQL
}

restore_pg_dumps

# Phase F-lite: replay each tenant's Postgres dump. Tenants are stored
# as data/tenants/<name>/postgres.sql.gz with a sibling manifest.env
# that records the original port assignment. We recreate the tenant
# DB shell via scripts/create-tenant.sh (idempotent, applies all
# Phase E migrations + grants) and then replay the bundled dump into
# it. CREATE DATABASE happens via the script's own admin path; the
# replay goes through supabase_admin like the main schema's because
# the dump owns objects under that role.
restore_tenant_dumps() {
  if [[ ! -d "data/tenants" ]]; then
    return 0
  fi
  if ! docker inspect -f '{{.State.Running}}' supabase-db 2>/dev/null | grep -q true; then
    echo "⚠ supabase-db not running — tenant dumps left in data/tenants/" >&2
    return 0
  fi
  if [[ -z "$PG_PASSWORD" ]]; then
    echo "⚠ POSTGRES_PASSWORD missing — skipping tenant replay" >&2
    return 0
  fi
  shopt -s nullglob
  local restored=0
  for tenant_dir in data/tenants/*/; do
    local name
    name=$(basename "$tenant_dir")
    if [[ ! -f "$tenant_dir/manifest.env" ]]; then
      echo "  · $name: manifest missing — skipping"
      continue
    fi
    local dump="$tenant_dir/postgres.sql.gz"
    echo "→ Restoring tenant: $name"
    if [[ ! -f "$dump" ]]; then
      # No dump — fall back to a full create-tenant.sh run so at
      # least the empty tenant shell + migrations come back. Operator
      # would have to re-seed any user content manually.
      if bash scripts/create-tenant.sh "$name" >/dev/null 2>&1; then
        echo "  · $name: manifest-only restore (empty tenant shell created)"
      else
        echo "  ⚠ $name: create-tenant.sh failed and no dump available"
      fi
      continue
    fi

    # Real restore path: create a bare DB, install the minimal
    # pre-dump scaffolding (auth shim + pgvector + pgcrypto — the
    # pg_dump assumes these exist), then replay. We do NOT run
    # create-tenant.sh's migrations first because that would create
    # public/yorik/docs schemas that the dump then tries to recreate
    # (CREATE SCHEMA conflicts, CREATE FUNCTION conflicts, etc.).
    # The dump itself recreates everything migrations would have.
    docker exec -i -e PGPASSWORD="$PG_PASSWORD" supabase-db \
      psql -U supabase_admin -d postgres -v ON_ERROR_STOP=1 >/dev/null <<SQL
DROP DATABASE IF EXISTS "yorik_tenant_$name";
CREATE DATABASE "yorik_tenant_$name";
SQL

    # Install only the cluster-level extensions the dump assumes are
    # already present (pgvector, pgcrypto). All schemas — including
    # auth.users + auth.uid() — come back via the dump itself; we
    # don't pre-install the auth shim because the dump's own CREATE
    # would then collide.
    docker exec -i -e PGPASSWORD="$PG_PASSWORD" supabase-db \
      psql -U supabase_admin -d "yorik_tenant_$name" -v ON_ERROR_STOP=1 >/dev/null <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
SQL

    # Strip only the public-schema CREATE/COMMENT (the schema exists
    # on every fresh DB). yorik/docs/auth all come from the dump
    # itself — _dump_tenant_postgres explicitly includes them.
    if ! zcat "$dump" \
         | sed -E '/^CREATE SCHEMA public;$/d
                   /^COMMENT ON SCHEMA public IS/d' \
         | docker exec -i -e PGPASSWORD="$PG_PASSWORD" supabase-db \
             psql -U supabase_admin -d "yorik_tenant_$name" \
             -v ON_ERROR_STOP=1 >/dev/null; then
      echo "  ⚠ tenant $name replay FAILED — DB shell exists but content is partial"
      continue
    fi

    # Re-grant runtime access to the `postgres` role (the role the
    # tenant Yorik's psycopg pool uses for normal queries). Mirrors
    # create-tenant.sh step 4.
    docker exec -i -e PGPASSWORD="$PG_PASSWORD" supabase-db \
      psql -U supabase_admin -d "yorik_tenant_$name" -v ON_ERROR_STOP=1 >/dev/null <<SQL
GRANT ALL ON DATABASE "yorik_tenant_$name" TO postgres;
GRANT USAGE, CREATE ON SCHEMA public, yorik, docs, auth TO postgres;
GRANT ALL ON ALL TABLES    IN SCHEMA public TO postgres;
GRANT ALL ON ALL TABLES    IN SCHEMA docs   TO postgres;
GRANT ALL ON ALL TABLES    IN SCHEMA auth   TO postgres;
GRANT ALL ON ALL TABLES    IN SCHEMA yorik  TO postgres;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO postgres;
GRANT ALL ON ALL SEQUENCES IN SCHEMA docs   TO postgres;
GRANT ALL ON ALL SEQUENCES IN SCHEMA yorik  TO postgres;
GRANT ALL ON ALL FUNCTIONS IN SCHEMA yorik  TO postgres;
GRANT ALL ON ALL FUNCTIONS IN SCHEMA auth   TO postgres;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO postgres;
ALTER DEFAULT PRIVILEGES IN SCHEMA docs   GRANT ALL ON TABLES TO postgres;
ALTER DEFAULT PRIVILEGES IN SCHEMA auth   GRANT ALL ON TABLES TO postgres;
SQL
    restored=$((restored + 1))
    echo "  ✓ $name restored"
  done
  shopt -u nullglob
  if [[ "$restored" -gt 0 ]]; then
    echo "✓ $restored tenant database(s) restored"
  fi
}

restore_tenant_dumps

# Sanity check: look for the manifest we wrote.
if [[ -f "data/MANIFEST.json" ]]; then
  echo
  echo "✓ Restore complete. Manifest:"
  cat data/MANIFEST.json
else
  echo
  echo "⚠ Restore extracted but MANIFEST.json missing — verify data/ contents."
fi

echo
echo "Your previous data is at $BACKUP_DIR (delete it once you've"
echo "verified the restore worked)."
echo
echo "Restart Yorik:  bash start.sh"
