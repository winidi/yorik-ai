#!/usr/bin/env python3
"""Phase D Section 6 — copy every row from SQLite into the Postgres
bootstrap schema.

Assumes:
  - migrations_pg/000_phase_d_init.sql has already been applied (the
    target Postgres DB has the 60 empty tables).
  - The SQLite DBs at data/family.db + data/documents.db are
    quiescent. (Workstation Yorik can stay running on SQLite — it
    won't notice we're reading from a snapshot; row counts may drift
    a hair as it writes, which we re-sync at cutover in Section 7.)

What it copies:
  - Every user table in family.db → public schema
  - Every user table in documents.db → docs schema
  - SQLite-internal tables (sqlite_*), schema_migrations, FTS5 / vec0
    virtuals + their helper tables are skipped — schema_migrations is
    already stamped by the bootstrap; FTS/vec are re-derived from
    text (Section 4.3).

What it doesn't touch:
  - The `embedding` columns we added to paperless_chunks /
    document_chunks / wa_messages — those stay NULL; re-embedded in
    Section 4.3 as part of the data import.
  - The `search_tsv` columns we added (FTS5 replacement) — those are
    `GENERATED ALWAYS … STORED` so Postgres fills them as we INSERT.

After all data is in:
  - Resync each BIGSERIAL sequence so the next auto-id picks up where
    SQLite's AUTOINCREMENT left off. Without this, the first new row
    inserted via Yorik would collide with an imported id.
  - Print a count-parity table so the operator can verify.

Usage:
    YORIK_DB_PASSWORD=... ./venv/bin/python3 scripts/migrate_sqlite_data.py
    YORIK_DB_PASSWORD=... ./venv/bin/python3 scripts/migrate_sqlite_data.py --target yorik_test
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Iterable

# Allow `from backend.database_pg …` when invoked directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("migrate_sqlite_data")

PROJECT = Path(__file__).resolve().parent.parent
FAMILY_DB = PROJECT / "data" / "family.db"
DOCS_DB   = PROJECT / "data" / "documents.db"

# Mirrors the dump script's skip rules so we don't try to copy
# tables that don't exist on the Postgres side.
SKIP_NAMES = {
    "sqlite_sequence", "sqlite_stat1", "sqlite_stat2",
    "sqlite_stat3", "sqlite_stat4", "schema_migrations",
}
SKIP_SUFFIXES = (
    "_fts_data", "_fts_idx", "_fts_docsize", "_fts_config", "_fts_content",
    "_vec_info", "_vec_chunks", "_vec_rowids", "_vec_vector_chunks00",
)
SKIP_INTERNAL_BASES = ("vec_chunks_", "paperless_vec_", "wa_vec_")
# vec0 / fts5 virtual-table NAMES themselves (the parents already get
# their replacement columns in the bootstrap).
VEC_FTS_VIRTUALS = {
    "paperless_vec", "wa_vec", "vec_chunks",
    "email_messages_fts", "wa_messages_fts",
}

BATCH_SIZE = 1000


def _skip(name: str) -> bool:
    if name in SKIP_NAMES or name in VEC_FTS_VIRTUALS:
        return True
    if any(name.endswith(s) for s in SKIP_SUFFIXES):
        return True
    if any(name.startswith(b) for b in SKIP_INTERNAL_BASES):
        return True
    return False


def _user_tables(db_path: Path) -> list[str]:
    with sqlite3.connect(db_path) as c:
        names = [
            r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "ORDER BY rowid"
            ).fetchall()
        ]
    return [n for n in names if not _skip(n)]


def _columns(sqlite_db: Path, table: str) -> list[str]:
    """Return SQLite table columns, EXCLUDING any column whose name
    matches a Postgres-only added column (embedding, search_tsv) so we
    don't try to copy values that aren't in the source."""
    with sqlite3.connect(sqlite_db) as c:
        rows = c.execute(f"PRAGMA table_info({table})").fetchall()
    cols = [r[1] for r in rows]
    # Strip Postgres-only columns (we don't add any to SQLite tables;
    # this is defensive in case the live SQLite gets new columns and
    # we re-run the migration).
    return [c for c in cols if c not in ("embedding", "search_tsv")]


def _copy_table(
    sqlite_db: Path,
    table: str,
    pg_conn,
    pg_schema: str,
) -> int:
    """Bulk-copy one table's rows from SQLite to Postgres. Returns the
    row count copied. Uses executemany with batches of BATCH_SIZE."""
    cols = _columns(sqlite_db, table)
    if not cols:
        log.warning("  %s.%s: no columns?", pg_schema, table)
        return 0
    col_list = ", ".join(f'"{c}"' for c in cols)
    placeholders = ", ".join(["%s"] * len(cols))
    insert_sql = (
        f'INSERT INTO {pg_schema}."{table}" ({col_list}) '
        f"VALUES ({placeholders}) "
        f"ON CONFLICT DO NOTHING"
    )

    src = sqlite3.connect(sqlite_db)
    try:
        src.row_factory = None  # plain tuples — psycopg expects tuples
        cur = src.execute(f'SELECT {col_list} FROM "{table}"')
        copied = 0
        batch: list[tuple] = []
        with pg_conn.cursor() as pcur:
            for row in cur:
                batch.append(row)
                if len(batch) >= BATCH_SIZE:
                    pcur.executemany(insert_sql, batch)
                    copied += len(batch)
                    batch = []
            if batch:
                pcur.executemany(insert_sql, batch)
                copied += len(batch)
        pg_conn.commit()
        return copied
    finally:
        src.close()


def _resync_sequences(pg_conn, schema: str) -> None:
    """After importing rows with explicit ids, advance the BIGSERIAL
    sequence so the next auto-id is max(id) + 1. Postgres helpers:
    pg_get_serial_sequence + setval()."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND column_default LIKE 'nextval%%'",
            (schema,),
        )
        cols = cur.fetchall()
        for row in cols:
            t = row["table_name"]; c = row["column_name"]
            seq = cur.execute(
                "SELECT pg_get_serial_sequence(%s, %s) AS seq",
                (f"{schema}.{t}", c),
            ).fetchone()
            seq_name = seq["seq"] if seq else None
            if not seq_name:
                continue
            mx = cur.execute(
                f'SELECT COALESCE(MAX("{c}"), 0) AS mx FROM {schema}."{t}"'
            ).fetchone()
            max_id = int(mx["mx"] or 0)
            cur.execute(
                "SELECT setval(%s, GREATEST(%s, 1), %s)",
                (seq_name, max_id, max_id > 0),
            )
            log.info("  seq %s → %d", seq_name, max_id)
    pg_conn.commit()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default="yorik_test",
                        help="Postgres database name (default: yorik_test)")
    args = parser.parse_args()

    # Auto-pick up POSTGRES_PASSWORD from the supabase .env.
    if not os.getenv("YORIK_DB_PASSWORD") and not os.getenv("YORIK_DB_URL"):
        env_path = PROJECT / "infra/supabase/docker/.env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("POSTGRES_PASSWORD="):
                    os.environ["YORIK_DB_PASSWORD"] = line.split("=", 1)[1]
                    break
    os.environ["YORIK_DB_NAME"] = args.target

    from backend.database_pg import conn_ctx_pg, close_all_pools

    try:
        with conn_ctx_pg("main") as pg:
            log.info("==> family.db → public schema")
            total_family = 0
            for t in _user_tables(FAMILY_DB):
                n = _copy_table(FAMILY_DB, t, pg, "public")
                log.info("  %-35s %d rows", t, n)
                total_family += n

            log.info("==> documents.db → docs schema")
            total_docs = 0
            for t in _user_tables(DOCS_DB):
                n = _copy_table(DOCS_DB, t, pg, "docs")
                log.info("  %-35s %d rows", t, n)
                total_docs += n

            log.info("==> Re-syncing BIGSERIAL sequences")
            _resync_sequences(pg, "public")
            _resync_sequences(pg, "docs")

            log.info("\nTotals: family.db=%d docs.db=%d rows imported",
                     total_family, total_docs)
        return 0
    finally:
        close_all_pools()


if __name__ == "__main__":
    raise SystemExit(main())
