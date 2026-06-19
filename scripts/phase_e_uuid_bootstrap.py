#!/usr/bin/env python3
"""Phase E §1 — produce a Postgres bootstrap that uses UUIDs for user IDs.

Reads `migrations_pg/000_phase_d_init.sql` (the Phase D bootstrap with
INTEGER PKs) and emits `migrations_pg/100_phase_e_init.sql` with:

  - user_profiles.id BIGSERIAL → UUID PK, FK to auth.users(id) ON DELETE CASCADE
  - every <name>_user_id INTEGER column → UUID
    (except paperless_user_id INTEGER which is Paperless's own integer
    PK; immich_user_id is already TEXT and stays)

Why generate a new file instead of editing 000_phase_d_init.sql in
place: the Phase D bootstrap is referenced by the
phase-d-supabase branch's commit history (pushed to yorik-ai-private).
Phase E ships its own bootstrap; fresh installs apply
000_phase_d_init.sql (SQLite-era PKs) OR 100_phase_e_init.sql
(UUID-native), never both.

Usage:
    ./venv/bin/python3 scripts/phase_e_uuid_bootstrap.py
"""
from __future__ import annotations

import re
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
SRC = PROJECT / "migrations_pg" / "000_phase_d_init.sql"
OUT = PROJECT / "migrations_pg" / "100_phase_e_init.sql"


# Columns whose `INTEGER` we leave alone — they're foreign IDs from
# other systems, not Yorik auth.users references.
SKIP_USER_ID_COLS = {
    "paperless_user_id",   # Paperless's own integer PK
    # immich_user_id is already TEXT in the schema; no INTEGER to flip.
}


_USER_ID_RE = re.compile(
    r"\b(\w+_user_id)\s+INTEGER\b",
    re.IGNORECASE,
)
# user_profiles also has a `user_id INTEGER` member used by other tables; we
# flip every `user_id INTEGER` to UUID too — there's no non-user table that
# has a column called bare `user_id` that means something else.
_BARE_USER_ID_RE = re.compile(
    r"\buser_id\s+INTEGER\b",
    re.IGNORECASE,
)


def _convert(sql: str) -> str:
    """Apply the UUID transformation. Idempotent — re-running yields
    the same output."""

    # 1. user_profiles.id BIGSERIAL → UUID PK + FK to auth.users.
    sql = re.sub(
        r"(CREATE TABLE (?:IF NOT EXISTS )?user_profiles \(\s*)id\s+BIGSERIAL\s+PRIMARY KEY",
        r"\1id              UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE",
        sql,
        flags=re.IGNORECASE,
    )

    # 2. Named *_user_id columns. Skip the Paperless/Immich external refs.
    def replace_user_id_col(m: re.Match) -> str:
        col_name = m.group(1)
        if col_name.lower() in SKIP_USER_ID_COLS:
            return m.group(0)
        return f"{col_name} UUID"

    sql = _USER_ID_RE.sub(replace_user_id_col, sql)
    # 3. Bare `user_id INTEGER` (no prefix). Always becomes UUID.
    sql = _BARE_USER_ID_RE.sub("user_id UUID", sql)
    # 3b. `<col> UUID NOT NULL DEFAULT 1` → `<col> UUID NOT NULL`. The
    # SQLite-era default of `1` (the founder user_id) doesn't map to a
    # UUID. Callers must pass an explicit value at INSERT. The same
    # applies to bare `DEFAULT 1` on any UUID column we just rewrote.
    sql = re.sub(
        r"(\b\w*user_id\s+UUID(?:\s+NOT\s+NULL)?)\s+DEFAULT\s+\d+",
        r"\1",
        sql,
        flags=re.IGNORECASE,
    )
    # 4. Rename the version-0 stamp from 'phase_d_init' → 'phase_e_init'
    # so the historical marker reflects which bootstrap actually ran.
    sql = sql.replace(
        "VALUES (0, 'phase_d_init')",
        "VALUES (0, 'phase_e_init')",
    )
    return sql


def main() -> int:
    src_sql = SRC.read_text()
    out_sql = _convert(src_sql)

    header = """\
-- Yorik Phase E bootstrap — UUID-native user IDs, references auth.users.
--
-- Generated from migrations_pg/000_phase_d_init.sql via
-- scripts/phase_e_uuid_bootstrap.py. The transformations:
--   * user_profiles.id: BIGSERIAL → UUID PK referencing auth.users(id)
--   * every <name>_user_id INTEGER → UUID (except paperless_user_id
--     which is Paperless's own integer PK)
--
-- Apply to a fresh Postgres database where Supabase Auth (GoTrue)
-- already populated the auth schema. Idempotent: every DDL uses
-- IF NOT EXISTS. The schema_migrations stamp at the bottom marks
-- the historical SQLite-era migrations 001-062 as applied too, so
-- the runner doesn't try to re-replay them.
--
-- Phase D bootstrap (000_phase_d_init.sql) still exists for installs
-- that did the SQLite-era cutover; new installs should use this
-- file instead.

"""
    OUT.write_text(header + out_sql)
    print(f"Wrote {OUT} ({OUT.stat().st_size} bytes)")
    # Quick sanity counts.
    user_id_uuid_count = out_sql.lower().count("user_id uuid")
    user_id_int_count = out_sql.lower().count("user_id integer")
    print(f"  *_user_id UUID columns:    {user_id_uuid_count}")
    print(f"  *_user_id INTEGER columns: {user_id_int_count}  (expected: only paperless_user_id)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
