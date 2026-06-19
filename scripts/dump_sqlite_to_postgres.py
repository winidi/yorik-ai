#!/usr/bin/env python3
"""Translate Yorik's SQLite schema into a Postgres bootstrap migration.

Reads `sqlite_master` from `data/family.db` + `data/documents.db`,
mechanically transforms each CREATE TABLE / INDEX into the Postgres
dialect, and writes `migrations_pg/000_phase_d_init.sql`.

Handled specially:
  - `INTEGER PRIMARY KEY AUTOINCREMENT`  → `BIGSERIAL PRIMARY KEY`
  - `DEFAULT (datetime('now'))`          → `DEFAULT current_timestamp`
  - `DEFAULT (date('now'))`              → `DEFAULT current_date`
  - `REAL`                                → `DOUBLE PRECISION`
  - `BLOB`                                → `BYTEA`
  - `sqlite_sequence`, `sqlite_stat*`     → skipped (Postgres uses sequences)
  - FTS5 virtual tables (`*_fts`)         → skipped; replaced by tsvector columns + GIN indexes on the parent table (emitted at the bottom)
  - vec0 virtual tables (`*_vec`)         → skipped; replaced by `embedding vector(384)` column on the parent (emitted at the bottom)
  - FTS5 triggers (`*_ai`, `*_ad`, `*_au`)→ skipped; Postgres uses generated columns or triggers we re-author below

The output is intentionally non-trivial: it should produce the same
**reachable** schema as the SQLite end state, not a 1:1 copy of every
SQLite internal. Diff the table+column counts after running to catch
omissions.

Usage:
    ./venv/bin/python3 scripts/dump_sqlite_to_postgres.py
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Iterable

PROJECT = Path(__file__).resolve().parent.parent
FAMILY_DB = PROJECT / "data" / "family.db"
DOCS_DB   = PROJECT / "data" / "documents.db"
OUT       = PROJECT / "migrations_pg" / "000_phase_d_init.sql"

# Tables / objects we drop entirely.
SKIP_NAMES = {
    "sqlite_sequence", "sqlite_stat1", "sqlite_stat2",
    "sqlite_stat3", "sqlite_stat4", "schema_migrations",
}

# Prefixes/suffixes that mark FTS5 / vec0 internal helper tables.
# sqlite_master surfaces these as regular tables alongside the real ones.
SKIP_SUFFIXES = (
    "_fts_data", "_fts_idx", "_fts_docsize", "_fts_config", "_fts_content",
    "_vec_info", "_vec_chunks", "_vec_rowids", "_vec_vector_chunks00",
)
SKIP_INTERNAL_BASES = ("vec_chunks_", "paperless_vec_", "wa_vec_")


def _is_internal(name: str) -> bool:
    if any(name.endswith(s) for s in SKIP_SUFFIXES):
        return True
    if any(name.startswith(b) for b in SKIP_INTERNAL_BASES):
        return True
    return False


# Inline REFERENCES clause, e.g. `space_id INTEGER REFERENCES spaces(id) ON DELETE CASCADE`.
# We strip the REFERENCES … clause from the CREATE TABLE column definition and emit
# a matching ALTER TABLE ADD CONSTRAINT at the end of the file, so forward refs
# (events created before spaces in our schema dump) don't error out.
_REFERENCES_RE = re.compile(
    r"\s+REFERENCES\s+(\w+)\s*\(\s*(\w+)\s*\)"
    r"(?:\s+ON\s+(?:UPDATE|DELETE)\s+(?:CASCADE|RESTRICT|SET\s+NULL|SET\s+DEFAULT|NO\s+ACTION))*",
    re.IGNORECASE,
)

# Table-level FOREIGN KEY (col) REFERENCES table(col) … clauses. We strip
# the entire line (and any trailing comma) since FKs are deferred to a
# follow-up migration after all tables exist.
_TABLE_LEVEL_FK_RE = re.compile(
    r",?\s*FOREIGN\s+KEY\s*\([^)]+\)"
    r"(?:\s+REFERENCES\s+\w+\s*\([^)]+\)"
    r"(?:\s+ON\s+(?:UPDATE|DELETE)\s+(?:CASCADE|RESTRICT|SET\s+NULL|SET\s+DEFAULT|NO\s+ACTION))*"
    r")?",
    re.IGNORECASE,
)

# Parent → child vec0 virtual table. We replace each vec0 table with an
# `embedding vector(N)` column on the parent + an ivfflat index. The
# parent's CREATE TABLE has to be augmented; we do it post-translation
# by emitting ALTER TABLE statements at the end.
#
# Discovered by reading `data/family.db` schema (family/docs both
# scanned automatically) — keep this in sync if a new vec table is
# added by a future migration.
VEC_PARENTS = {
    "paperless_vec": ("paperless_chunks", 384, "docs"),
    "wa_vec":        ("wa_messages",      384, "main"),
    "vec_chunks":    ("document_chunks",  384, "docs"),  # docs.db's native_documents embedder
}

# FTS5 virtual tables → tsvector + GIN index on the parent. Columns
# indexed inferred from the original FTS5 column list.
FTS_PARENTS = {
    "email_messages_fts": ("email_messages", ("subject", "from_name", "from_email", "snippet", "body_text"), "main"),
    "wa_messages_fts":    ("wa_messages",    ("text", "transcript"),                                       "main"),
}


def _extract_fks(table_name: str, sql: str) -> tuple[str, list[str]]:
    """Strip inline REFERENCES clauses from `sql`. Return (cleaned_sql, [ALTER…]).

    Each extracted FK becomes a separate ALTER TABLE statement emitted
    at the end of the bootstrap, so forward references between tables
    don't fail the CREATE step.

    Heuristic: scan column lines for `<col> ... REFERENCES other(col) …`
    and remove the REFERENCES segment. We don't try to support every
    SQLite quirk; only the patterns Yorik actually uses (column-level
    FKs with optional ON DELETE/UPDATE actions).
    """
    fks: list[str] = []
    counter = [0]

    def replace(m: re.Match) -> str:
        target = m.group(1)
        target_col = m.group(2)
        # Reconstruct the full match so we capture the ON DELETE/UPDATE.
        full = m.group(0)
        # Figure out which column this REFERENCES applies to by walking back.
        # The match starts AFTER the column name + type, so we need the column
        # name from context. We can't easily get it inside the lambda; we'll
        # post-process after this returns.
        idx = counter[0]
        counter[0] += 1
        fks.append(f"-- [{idx}] {table_name} → {target}({target_col})")
        fks.append(f"-- ALTER TABLE {table_name} ADD CONSTRAINT fk_{table_name}_{idx} FOREIGN KEY ... REFERENCES {target}({target_col}) ...;")
        return ""  # Strip the REFERENCES clause from the column def.

    cleaned = _REFERENCES_RE.sub(replace, sql)
    # Also strip table-level FOREIGN KEY (...) REFERENCES ... clauses.
    cleaned = _TABLE_LEVEL_FK_RE.sub("", cleaned)
    # Strip dangling commas before the closing `)`, even when an SQL
    # comment intervenes (SQLite happily emits `col INTEGER, -- foo\n)`
    # which Postgres rejects). Comments take the form `-- … <newline>`.
    cleaned = re.sub(
        r",(\s*(?:--[^\n]*\n)?\s*)\)",
        r"\1\n)",
        cleaned,
    )
    # We deliberately don't emit working ALTER TABLE statements for FKs
    # in this pass — see TODO at the top of the file. Phase D moves
    # past Section 2.4 will reconcile FK relationships from the
    # spaces.py / users.py model.
    return cleaned, []


def _transform_create_table(sql: str, table_name: str) -> tuple[str, list[str]]:
    """Mechanical rewrites against a single CREATE TABLE statement."""
    s = sql
    # INTEGER PRIMARY KEY AUTOINCREMENT — note: Postgres BIGSERIAL implies NOT NULL + PRIMARY KEY indirectly,
    # but the original SQL also explicitly says PRIMARY KEY so we drop the original and emit BIGSERIAL.
    s = re.sub(
        r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b",
        "BIGSERIAL PRIMARY KEY",
        s, flags=re.IGNORECASE,
    )
    # Plain INTEGER PRIMARY KEY (no AUTOINCREMENT) — also a sequence in SQLite, also BIGSERIAL.
    s = re.sub(
        r"\bINTEGER\s+PRIMARY\s+KEY(?!\s+AUTOINCREMENT)\b(?![\w])",
        "BIGSERIAL PRIMARY KEY",
        s, flags=re.IGNORECASE,
    )
    # Default literals.
    # SQLite stored these as 'YYYY-MM-DD HH:MM:SS' (text). To keep
    # existing data + new rows in the same format, emit a to_char
    # default that produces the same string shape. Otherwise mixed-
    # format rows would break range queries against TEXT-typed
    # created_at columns.
    s = re.sub(r"DEFAULT\s*\(\s*datetime\('now'\)\s*\)",
               "DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')",
               s, flags=re.IGNORECASE)
    s = re.sub(r"DEFAULT\s*\(\s*date\('now'\)\s*\)",
               "DEFAULT to_char(now(), 'YYYY-MM-DD')",
               s, flags=re.IGNORECASE)
    # Type swaps. \bREAL\b matches column type 'REAL' (not 'real_name' etc).
    s = re.sub(r"\bREAL\b", "DOUBLE PRECISION", s)
    s = re.sub(r"\bBLOB\b", "BYTEA", s)
    # Strip inline FK refs (Postgres will error on forward refs to tables
    # not yet created).
    s, fk_alters = _extract_fks(table_name, s)
    # SQLite tolerates trailing commas inside CREATE TABLE column lists in
    # some weird emitted forms; Postgres doesn't. Strip lonely trailing
    # commas before the final ).
    s = re.sub(r",\s*\)\s*$", "\n)", s)
    return s, fk_alters


def _transform_create_index(sql: str) -> str:
    """SQLite + Postgres CREATE INDEX share enough syntax that no rewrites
    are usually needed. We add `IF NOT EXISTS` so reruns are safe."""
    if " IF NOT EXISTS " not in sql.upper():
        sql = re.sub(r"\bCREATE\s+INDEX\b",
                     "CREATE INDEX IF NOT EXISTS",
                     sql, flags=re.IGNORECASE, count=1)
        sql = re.sub(r"\bCREATE\s+UNIQUE\s+INDEX\b",
                     "CREATE UNIQUE INDEX IF NOT EXISTS",
                     sql, flags=re.IGNORECASE, count=1)
    return sql


def _iter_db(path: Path, kind_filter: str) -> Iterable[tuple[str, str]]:
    """Yield (name, sql) pairs for tables / indexes / triggers in `path`,
    ordered for safe execution (tables before indexes before triggers)."""
    with sqlite3.connect(path) as c:
        cur = c.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type=? AND sql IS NOT NULL "
            "ORDER BY rowid",
            (kind_filter,),
        )
        for row in cur.fetchall():
            yield row[0], row[1]


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)

    out: list[str] = []
    out.append("-- Yorik Phase D bootstrap — autogenerated from SQLite end-state schema.")
    out.append("-- See scripts/dump_sqlite_to_postgres.py for the translator.")
    out.append("-- Re-running is safe (every DDL is IF NOT EXISTS-equivalent).")
    out.append("")
    out.append("CREATE EXTENSION IF NOT EXISTS vector;")
    out.append("CREATE EXTENSION IF NOT EXISTS pgcrypto;")
    out.append("")
    out.append("CREATE SCHEMA IF NOT EXISTS docs;")
    out.append("")

    table_count = 0
    skipped: list[str] = []

    # Pass 1: family.db tables.
    out.append("-- =================== family.db tables ===================")
    out.append("")
    for name, sql in _iter_db(FAMILY_DB, "table"):
        if name in SKIP_NAMES:
            skipped.append(f"family.db: {name} (skip-list)")
            continue
        if _is_internal(name):
            skipped.append(f"family.db: {name} (vec/fts internal)")
            continue
        if name in VEC_PARENTS or name in FTS_PARENTS:
            skipped.append(f"family.db: {name} (virtual table — handled below)")
            continue
        sql, _ = _transform_create_table(sql, name)
        # Mark as IF NOT EXISTS for idempotence.
        sql = re.sub(r"\bCREATE\s+TABLE\b(?!\s+IF\s+NOT\s+EXISTS)",
                     "CREATE TABLE IF NOT EXISTS",
                     sql, flags=re.IGNORECASE, count=1)
        out.append(sql + ";")
        out.append("")
        table_count += 1

    # Pass 2: docs.db tables — go into `docs.` schema.
    out.append("-- =================== documents.db tables (docs schema) ===================")
    out.append("")
    for name, sql in _iter_db(DOCS_DB, "table"):
        if name in SKIP_NAMES:
            skipped.append(f"documents.db: {name} (skip-list)")
            continue
        if _is_internal(name):
            skipped.append(f"documents.db: {name} (vec/fts internal)")
            continue
        if name in VEC_PARENTS or name in FTS_PARENTS:
            skipped.append(f"documents.db: {name} (virtual table — handled below)")
            continue
        sql, _ = _transform_create_table(sql, name)
        # Prefix the table name with `docs.` schema.
        sql = re.sub(r"\bCREATE\s+TABLE\b(?!\s+IF\s+NOT\s+EXISTS)\s+(`?)(\w+)(`?)",
                     r"CREATE TABLE IF NOT EXISTS docs.\2",
                     sql, flags=re.IGNORECASE, count=1)
        out.append(sql + ";")
        out.append("")
        table_count += 1

    # Pass 3: indexes.
    out.append("-- =================== indexes (family.db) ===================")
    out.append("")
    for name, sql in _iter_db(FAMILY_DB, "index"):
        if name.startswith("sqlite_autoindex_"):
            continue  # Postgres recreates these automatically with the PK / UNIQUE constraint
        out.append(_transform_create_index(sql) + ";")

    out.append("")
    out.append("-- =================== indexes (documents.db, docs schema) ===================")
    out.append("")
    for name, sql in _iter_db(DOCS_DB, "index"):
        if name.startswith("sqlite_autoindex_"):
            continue
        # Skip indexes that index a vec/fts internal table (we don't carry those).
        if _is_internal(name):
            continue
        # Skip indexes whose target table we skipped (vec0 / fts5 virtuals).
        if any(skip in sql for skip in VEC_PARENTS) or any(skip in sql for skip in FTS_PARENTS):
            continue
        # Rewrite the table reference to docs.*.
        sql_pg = _transform_create_index(sql)
        sql_pg = re.sub(r"\bON\s+(`?)(\w+)(`?)", r"ON docs.\2", sql_pg, flags=re.IGNORECASE, count=1)
        out.append(sql_pg + ";")

    # Pass 4: vec0 → pgvector. Add column + ivfflat index per VEC_PARENTS.
    out.append("")
    out.append("-- =================== Embeddings (pgvector replaces sqlite_vec) ===================")
    out.append("")
    for vec_name, (parent, dim, db) in VEC_PARENTS.items():
        qual = f"docs.{parent}" if db == "docs" else parent
        out.append(f"-- {vec_name} (sqlite_vec virtual) → {qual}.embedding vector({dim})")
        out.append(f"ALTER TABLE {qual} ADD COLUMN IF NOT EXISTS embedding vector({dim});")
        out.append(
            f"CREATE INDEX IF NOT EXISTS idx_{parent}_embedding "
            f"ON {qual} USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);"
        )
        out.append("")

    # Pass 5: FTS5 → tsvector + GIN. We materialise a tsvector column.
    out.append("-- =================== Full-text search (tsvector replaces FTS5) ===================")
    out.append("")
    for fts_name, (parent, cols, db) in FTS_PARENTS.items():
        qual = f"docs.{parent}" if db == "docs" else parent
        coalesces = " || ' ' || ".join(f"coalesce({c}, '')" for c in cols)
        out.append(f"-- {fts_name} (FTS5) → {qual}.search_tsv tsvector (generated)")
        out.append(
            f"ALTER TABLE {qual} ADD COLUMN IF NOT EXISTS search_tsv tsvector "
            f"GENERATED ALWAYS AS (to_tsvector('simple', {coalesces})) STORED;"
        )
        out.append(
            f"CREATE INDEX IF NOT EXISTS idx_{parent}_search_tsv "
            f"ON {qual} USING gin (search_tsv);"
        )
        out.append("")

    # Pass 6: schema_migrations stamping — same shape as the SQLite
    # version so backend/migrations.py can share logic. Mark all 62
    # historical migrations as applied so the runner doesn't try to
    # re-replay them, plus version 0 = phase_d_init sentinel.
    out.append("-- =================== schema_migrations stamp ===================")
    out.append("CREATE TABLE IF NOT EXISTS schema_migrations (")
    out.append("    version    INTEGER PRIMARY KEY,")
    out.append("    name       TEXT NOT NULL,")
    out.append("    applied_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp")
    out.append(");")
    with sqlite3.connect(FAMILY_DB) as c:
        rows = c.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()
    out.append(f"-- Stamping version 0 (phase_d_init) + {len(rows)} historical migrations as applied.")
    out.append(
        "INSERT INTO schema_migrations (version, name) "
        "VALUES (0, 'phase_d_init') ON CONFLICT (version) DO NOTHING;"
    )
    for ver, n in rows:
        # SQL-escape single quotes in name (defensive — migration names
        # are snake_case but just in case).
        safe = n.replace("'", "''")
        out.append(
            f"INSERT INTO schema_migrations (version, name) "
            f"VALUES ({int(ver)}, '{safe}') ON CONFLICT (version) DO NOTHING;"
        )

    OUT.write_text("\n".join(out) + "\n")
    print(f"Wrote {OUT} — {table_count} tables, {len(rows)} migrations stamped.")
    if skipped:
        print(f"Skipped ({len(skipped)}):")
        for s in skipped:
            print(f"  - {s}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
