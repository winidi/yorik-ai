"""Phase E §6 — per-app Postgres schema install/uninstall lifecycle.

For manifest v2 apps, "install" means:
  1. CREATE SCHEMA app_<id>
  2. Run schema.sql inside that schema (sets search_path first)
  3. Run policies.sql (RLS for the app's owned tables)
  4. For each permissions.reads entry, create a projection VIEW
     in app_<id> that selects the granted columns from public.<table>.
     RLS on the underlying table still applies — the view projects
     columns, it doesn't bypass.
  5. ALTER PUBLICATION supabase_realtime ADD TABLE for each
     realtime_subscriptions entry that names an owned table.
  6. Insert a row into installed_apps with the manifest snapshot.

"Uninstall" reverses in opposite order:
  1. ALTER PUBLICATION supabase_realtime DROP TABLE for owned tables
  2. DROP SCHEMA app_<id> CASCADE
  3. UPDATE installed_apps SET uninstalled_at = now()

Per-app Postgres roles (granting USAGE on app_<id> only) and per-app
JWTs are added in §6.6 — the table has nullable columns for them.

v1 manifests (no `manifest_version: 2`) skip this entire module and
keep using the SQLite path in app_loader.load_app.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from .database_pg import conn_ctx_pg

log = logging.getLogger("homeos.app_lifecycle")

_SCHEMA_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
# Postgres identifiers may start with letter OR underscore; we use this
# for table/column/view names. Schema names stay stricter (above) to
# rule out names like `_internal` for app schemas.
_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def owned_schema_for(manifest: Dict[str, Any]) -> str:
    """Compute the Postgres schema name for an app.

    Explicit `owned_schema` in the manifest wins. Otherwise default to
    `app_<id>` with hyphens and dots converted to underscores so
    namespaced ids like `acme.notes` become `app_acme_notes`.
    """
    if sch := manifest.get("owned_schema"):
        return sch
    aid = manifest["id"]
    safe = re.sub(r"[.\-]+", "_", aid).lower()
    return f"app_{safe}"


def _ident(name: str) -> str:
    """Quote a Postgres identifier safely after whitelist validation.

    We never interpolate user input into SQL — this just defends
    against bugs that pass non-identifier strings.
    """
    if not _IDENT_RE.match(name):
        raise ValueError(f"unsafe identifier: {name!r}")
    return f'"{name}"'


def install_app_schema(
    *,
    manifest: Dict[str, Any],
    schema_sql: str,
    policies_sql: Optional[str],
    granted_by_user_id: Optional[str] = None,
    source_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Install the per-app Postgres schema.

    Returns the installed_apps row as a dict. Idempotent only at the
    coarse "drop + create" level — call uninstall_app_schema first if
    you want a clean reinstall.

    Raises ValueError if validation fails (e.g. policies.sql missing
    for a v2 manifest with owned_tables that need RLS).
    """
    if manifest.get("manifest_version") != 2:
        raise ValueError("install_app_schema only applies to manifest v2 apps")

    app_id = manifest["id"]
    sch = owned_schema_for(manifest)
    if not _SCHEMA_NAME_RE.match(sch):
        raise ValueError(f"owned_schema {sch!r} is not a safe Postgres identifier")

    owned_tables: List[str] = manifest.get("owned_tables") or []
    for t in owned_tables:
        if not _IDENT_RE.match(t):
            raise ValueError(f"owned_tables entry {t!r} is not a safe identifier")

    if owned_tables and not policies_sql:
        # The masterplan is explicit: an app that ships owned_tables
        # must ship policies.sql alongside. Without RLS, a JWT-scoped
        # client could read every other user's notes.
        raise ValueError(
            f"app {app_id!r}: policies.sql missing — required when owned_tables is non-empty"
        )

    perms = manifest.get("permissions") or {}
    reads: List[Dict[str, Any]] = perms.get("reads") or []
    realtime_subs: List[str] = perms.get("realtime_subscriptions") or []

    # Per-app Postgres role — scoped credentials when the app's
    # iframe talks to Supabase directly (supabase-js → PostgREST).
    # Without this, an app uses the user's JWT and sees everything
    # the user can; with this, an app sees only its own schema +
    # projection views. NOLOGIN because PostgREST switches into the
    # role via SET ROLE; we never log in as it directly.
    from . import app_jwt as _appjwt
    role = _appjwt.role_name_for(app_id)

    with conn_ctx_pg("main") as conn:
        with conn.cursor() as cur:
            # 0) (Re)create the role idempotently. DROP first so a
            #    reinstall starts with no stale grants.
            cur.execute(
                f"DO $$ BEGIN "
                f"  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN "
                f"    EXECUTE 'REASSIGN OWNED BY {_ident(role)} TO postgres'; "
                f"    EXECUTE 'DROP OWNED BY {_ident(role)}'; "
                f"    EXECUTE 'DROP ROLE {_ident(role)}'; "
                f"  END IF; "
                f"END $$"
            )
            cur.execute(f"CREATE ROLE {_ident(role)} NOLOGIN")
            # authenticator (the role PostgREST logs in as) needs the
            # ability to SET ROLE into the app role. Otherwise the JWT
            # validates but the role switch fails.
            cur.execute(f"GRANT {_ident(role)} TO authenticator")
            # postgres needs membership too, otherwise REASSIGN OWNED +
            # DROP OWNED at uninstall fail with "permission denied to
            # reassign objects" — Supabase's `postgres` role is not
            # a true superuser (BYPASSRLS only), so it can't reassign
            # across role boundaries without explicit membership.
            cur.execute(f"GRANT {_ident(role)} TO postgres")

            # 1) Create the schema. Drop first to make reinstall sane.
            cur.execute(f"DROP SCHEMA IF EXISTS {_ident(sch)} CASCADE")
            cur.execute(f"CREATE SCHEMA {_ident(sch)}")
            # `authenticated` keeps USAGE so the user can read their
            # own app data via their personal JWT (useful for Yorik UI
            # surfacing installed-app contents). The app role gets
            # USAGE too — that's how the app reads via its own JWT.
            cur.execute(
                f"GRANT USAGE ON SCHEMA {_ident(sch)} TO authenticated, anon, {_ident(role)}"
            )
            # 2) Run schema.sql with search_path scoped to the app schema
            #    so CREATE TABLE foo lands in app_<id>.foo without the
            #    app author needing to remember the prefix.
            cur.execute(f"SET LOCAL search_path = {_ident(sch)}, public")
            if schema_sql.strip():
                cur.execute(schema_sql)
            # 3) Apply policies.sql — RLS policies for owned_tables.
            if policies_sql and policies_sql.strip():
                cur.execute(policies_sql)
                # GRANT row-level access to authenticated; RLS gates rows.
                for t in owned_tables:
                    cur.execute(
                        f"GRANT SELECT, INSERT, UPDATE, DELETE ON "
                        f"{_ident(sch)}.{_ident(t)} TO authenticated, {_ident(role)}"
                    )
            # 4) Projection views for permissions.reads.
            cur.execute("RESET search_path")
            for entry in reads:
                table = entry["table"]
                cols = entry.get("columns")
                if not _IDENT_RE.match(table):
                    raise ValueError(f"reads.table {table!r} unsafe")
                if cols:
                    for c in cols:
                        if not _IDENT_RE.match(c):
                            raise ValueError(f"reads.columns entry {c!r} unsafe")
                    col_list = ", ".join(_ident(c) for c in cols)
                else:
                    col_list = "*"
                view_name = f"_yorik_{table}"
                if not _IDENT_RE.match(view_name):
                    raise ValueError(f"view name {view_name!r} unsafe")
                cur.execute(
                    f"CREATE OR REPLACE VIEW {_ident(sch)}.{_ident(view_name)} AS "
                    f"SELECT {col_list} FROM public.{_ident(table)}"
                )
                cur.execute(
                    f"GRANT SELECT ON {_ident(sch)}.{_ident(view_name)} "
                    f"TO authenticated, {_ident(role)}"
                )
            # 5) Realtime publication membership. Apps subscribe to
            #    `app_<id>.<table>`; ALTER PUBLICATION takes a fully
            #    qualified relation. Errors swallowed when already
            #    in the publication, matching 104_phase_e_realtime.
            for t in realtime_subs:
                # Only manage subscriptions to OWNED tables here —
                # Yorik core tables are already in the publication
                # via migration 104; the read-permission check on
                # the consent screen gates whether the user can see
                # those changes.
                if t in owned_tables and _IDENT_RE.match(t):
                    try:
                        cur.execute(
                            f"ALTER PUBLICATION supabase_realtime "
                            f"ADD TABLE {_ident(sch)}.{_ident(t)}"
                        )
                    except Exception as exc:  # noqa: BLE001 — psycopg DuplicateObject etc
                        if "is already member" in str(exc) or "duplicate" in str(exc).lower():
                            conn.rollback()
                            cur = conn.cursor()
                        else:
                            raise
            # 6) Write the ledger row.
            cur.execute(
                """
                INSERT INTO installed_apps
                  (app_id, version, owned_schema,
                   manifest_snapshot, granted_permissions,
                   granted_by_user_id, source_dir, app_role_name)
                VALUES
                  (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)
                ON CONFLICT (app_id) WHERE uninstalled_at IS NULL
                DO UPDATE SET
                  version = EXCLUDED.version,
                  owned_schema = EXCLUDED.owned_schema,
                  manifest_snapshot = EXCLUDED.manifest_snapshot,
                  granted_permissions = EXCLUDED.granted_permissions,
                  granted_by_user_id = EXCLUDED.granted_by_user_id,
                  source_dir = EXCLUDED.source_dir,
                  app_role_name = EXCLUDED.app_role_name,
                  granted_at = now()
                RETURNING id, app_id, owned_schema, granted_at
                """,
                (
                    app_id,
                    manifest.get("version", "0.0"),
                    sch,
                    json.dumps(manifest),
                    json.dumps(perms),
                    granted_by_user_id,
                    source_dir,
                    role,
                ),
            )
            row = cur.fetchone()
            conn.commit()
        # Refresh PostgREST exposure so supabase-js can hit the new
        # schema. Runs on a separate cursor (its own transaction) so
        # a NOTIFY failure doesn't roll back the install.
        try:
            _resync_postgrest_schemas(conn)
            conn.commit()
        except Exception:  # noqa: BLE001
            log.exception("install: PGRST schema resync failed (non-fatal)")
            conn.rollback()
    log.info(
        "app installed (schema=%s, reads=%d, owned_tables=%d): %s",
        sch, len(reads), len(owned_tables), app_id,
    )
    return {
        "id": row[0], "app_id": row[1], "owned_schema": row[2], "granted_at": row[3],
    }


def uninstall_app_schema(*, app_id: str, keep_data: bool = False) -> bool:
    """Reverse install_app_schema.

    Returns True if a row was found and marked uninstalled. The schema
    is dropped CASCADE unless `keep_data=True`, in which case the row
    is still marked uninstalled but the schema remains for manual
    backup (the user gets a downloadable .sql in §6.5).
    """
    with conn_ctx_pg("main") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, owned_schema, app_role_name, "
                "       (manifest_snapshot->'owned_tables') "
                "FROM installed_apps "
                "WHERE app_id = %s AND uninstalled_at IS NULL "
                "ORDER BY id DESC LIMIT 1",
                (app_id,),
            )
            row = cur.fetchone()
            if not row:
                return False
            install_id, sch, app_role, owned_tables_json = row
            owned_tables = owned_tables_json or []

            # Drop publication entries first — they prevent the schema
            # drop otherwise.
            if not keep_data:
                for t in owned_tables:
                    if isinstance(t, str) and _IDENT_RE.match(t):
                        try:
                            cur.execute(
                                f"ALTER PUBLICATION supabase_realtime "
                                f"DROP TABLE {_ident(sch)}.{_ident(t)}"
                            )
                        except Exception:  # noqa: BLE001
                            conn.rollback()
                            cur = conn.cursor()

                cur.execute(f"DROP SCHEMA IF EXISTS {_ident(sch)} CASCADE")

                # Drop the per-app role. REASSIGN OWNED + DROP OWNED
                # clear any leftover ownership on objects we didn't
                # explicitly DROP (defence — DROP SCHEMA CASCADE
                # should have caught everything in the owned schema,
                # but a future GRANT on a public.* table would
                # otherwise wedge the role drop).
                if app_role and _IDENT_RE.match(app_role):
                    try:
                        cur.execute(
                            f"REASSIGN OWNED BY {_ident(app_role)} TO postgres"
                        )
                        cur.execute(f"DROP OWNED BY {_ident(app_role)}")
                        cur.execute(f"DROP ROLE {_ident(app_role)}")
                    except Exception:  # noqa: BLE001
                        log.exception(
                            "failed to drop app role %s — leaving for manual cleanup",
                            app_role,
                        )
                        conn.rollback()
                        cur = conn.cursor()

            cur.execute(
                "UPDATE installed_apps SET uninstalled_at = now() WHERE id = %s",
                (install_id,),
            )
            conn.commit()
        # Refresh PostgREST exposure so the dropped schema disappears
        # from /rest/v1/* on the next request.
        try:
            _resync_postgrest_schemas(conn)
            conn.commit()
        except Exception:  # noqa: BLE001
            log.exception("uninstall: PGRST schema resync failed (non-fatal)")
            conn.rollback()
    log.info("app uninstalled (schema=%s, keep_data=%s): %s", sch, keep_data, app_id)
    return True


# ─── PostgREST schema exposure (Phase E §13 follow-up #36) ────────────
#
# PostgREST's db-schemas list governs which schemas are reachable via
# /rest/v1/* — apps can't read their own data through supabase-js
# until their schema is in that list. Setting it via env var would
# require a container restart per install; instead we use PostgREST's
# in-database config (db-config=true) by stashing the comma-separated
# list as a per-role GUC on the authenticator role, then sending the
# two reload NOTIFY signals. PostgREST 11+ supports both.

_PGRST_BASE_SCHEMAS = ("public", "storage", "graphql_public")


def _resync_postgrest_schemas(conn) -> None:
    """Rebuild PGRST db-schemas from the live ledger and hot-reload.

    Idempotent — recomputes from scratch every call so install +
    uninstall code paths converge on the same answer. Skipped silently
    if the authenticator role doesn't exist (test harnesses with a
    fresh DB).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT owned_schema FROM installed_apps "
            "WHERE uninstalled_at IS NULL "
            "  AND owned_schema IS NOT NULL "
            "ORDER BY owned_schema"
        )
        app_schemas = [r[0] for r in cur.fetchall()]
        schemas = list(_PGRST_BASE_SCHEMAS) + app_schemas
        # Postgres won't accept arbitrary SQL here; the schema names
        # were validated by _SCHEMA_NAME_RE at install time. Build the
        # GUC value as a literal — the ALTER ROLE statement takes a
        # string value, no further interpolation.
        value = ",".join(schemas)
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'authenticator'")
        if cur.fetchone() is None:
            return
        # `ALTER ROLE ... SET` doesn't accept a parameter for the GUC
        # value (Postgres treats it as a literal at parse time). Quote
        # manually — `value` is a comma-joined list of identifiers that
        # already passed _SCHEMA_NAME_RE at install time, so no escapes
        # are possible. The current_database() ensures we tag the
        # setting to the DB Yorik is connected to.
        cur.execute("SELECT current_database()")
        dbname = cur.fetchone()[0]
        cur.execute(
            f"ALTER ROLE authenticator IN DATABASE {_ident(dbname)} "
            f"SET pgrst.db_schemas = '{value}'"
        )
        # Two reloads: config picks up the schemas list change, schema
        # picks up the table definitions in the newly-exposed schema.
        cur.execute("NOTIFY pgrst, 'reload config'")
        cur.execute("NOTIFY pgrst, 'reload schema'")


def get_installed_app(app_id: str) -> Optional[Dict[str, Any]]:
    """Return the active install row for app_id or None."""
    with conn_ctx_pg("main") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT app_id, version, owned_schema, manifest_snapshot, "
                "       granted_permissions, granted_by_user_id, granted_at, "
                "       source_dir "
                "FROM installed_apps "
                "WHERE app_id = %s AND uninstalled_at IS NULL",
                (app_id,),
            )
            row = cur.fetchone()
    if not row:
        return None
    return {
        "app_id": row[0], "version": row[1], "owned_schema": row[2],
        "manifest": row[3], "granted_permissions": row[4],
        "granted_by_user_id": row[5], "granted_at": row[6],
        "source_dir": row[7],
    }


def list_active_v2_installs() -> List[Dict[str, Any]]:
    """All active v2 installs (uninstalled_at IS NULL).

    The boot loader uses this to re-register apps that aren't on disk
    under apps/ — in-place installs from arbitrary source_dirs.
    """
    with conn_ctx_pg("main") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT app_id, owned_schema, manifest_snapshot, source_dir "
                "FROM installed_apps "
                "WHERE uninstalled_at IS NULL "
                "  AND (manifest_snapshot->>'manifest_version')::int = 2 "
                "ORDER BY granted_at"
            )
            rows = cur.fetchall()
    return [
        {
            "app_id": r[0], "owned_schema": r[1],
            "manifest": r[2], "source_dir": r[3],
        }
        for r in rows
    ]
