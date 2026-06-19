#!/usr/bin/env python3
"""Phase D Section 4.3 — embed every paperless_chunks / wa_messages row
in the Postgres docs schema whose `embedding vector(384)` is NULL.

Reads:
  - docs.paperless_chunks (id, text)
  - wa_messages (id, text || transcript)

Calls `backend.documents.embed` (Yorik's bundled embedder) on each text,
L2-normalises (matches the existing ingest contract — search uses
cosine distance on L2-normalised vectors), batches via `executemany`
into the `embedding` column. Idempotent: skips already-embedded rows
so a partial run can be resumed without duplicating work.

Usage:
    YORIK_DB_PASSWORD=... ./venv/bin/python3 scripts/embed_backfill.py
    YORIK_DB_PASSWORD=... ./venv/bin/python3 scripts/embed_backfill.py --target yorik_test --batch 64

Progress reported every BATCH rows; full log to stdout.
"""
from __future__ import annotations

import argparse
import logging
import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.documents import embed  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("embed_backfill")

PROJECT = Path(__file__).resolve().parent.parent


def _l2_normalize(vec):
    """Same normalisation paperless_ingest applies before storing.
    Without this, search's cosine-distance assumption breaks for
    Postgres-side queries against vectors persisted by this script."""
    s = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / s for v in vec]


def _vec_literal(vec) -> str:
    """pgvector accepts both binary and text input. We use text:
    `'[0.1,0.2,…]'::vector`. Keeps the dependency on pgvector-python
    optional; psycopg2 / psycopg3 don't need a custom adapter here."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def _backfill(conn, table_qual: str, text_expr: str, source_label: str,
              batch_size: int, pk_col: str = "id") -> int:
    """Embed every row in `table_qual` where embedding IS NULL.

    `text_expr` is the SQL expression evaluating to the text to embed
    (e.g. `text` for paperless_chunks, `coalesce(text,'') || ' ' ||
    coalesce(transcript,'')` for wa_messages). Skips rows whose text
    is blank — embedding empty strings would only add noise.

    `pk_col` is the table's primary-key column name. Most tables use
    `id` but wa_messages uses `msg_id` (TEXT). The UPDATE WHERE clause
    + SELECT both honour this so the script doesn't assume `id`."""
    cur = conn.cursor()
    cur.execute(
        f"SELECT COUNT(*) AS n FROM {table_qual} "
        f"WHERE embedding IS NULL AND length(trim({text_expr})) > 0"
    )
    todo = int(cur.fetchone()["n"])
    log.info("==> %s: %d rows to embed", source_label, todo)
    if todo == 0:
        return 0

    cur.execute(
        f"SELECT {pk_col} AS pk, {text_expr} AS body FROM {table_qual} "
        f"WHERE embedding IS NULL AND length(trim({text_expr})) > 0 "
        f"ORDER BY {pk_col}"
    )
    started = time.monotonic()
    done = 0
    batch: list[tuple] = []
    for row in cur:
        try:
            vec = _l2_normalize(embed(row["body"]))
        except Exception as exc:  # noqa: BLE001
            log.warning("  embed fail for id=%s: %s", row["id"], exc)
            continue
        batch.append((_vec_literal(vec), row["pk"]))
        if len(batch) >= batch_size:
            with conn.cursor() as wcur:
                wcur.executemany(
                    f"UPDATE {table_qual} SET embedding = %s::vector WHERE {pk_col} = %s",
                    batch,
                )
            conn.commit()
            done += len(batch)
            elapsed = time.monotonic() - started
            rate = done / elapsed if elapsed > 0 else 0
            eta = (todo - done) / rate if rate > 0 else 0
            log.info("  %s: %d/%d (%.1f/s, ETA %ds)",
                     source_label, done, todo, rate, int(eta))
            batch = []
    if batch:
        with conn.cursor() as wcur:
            wcur.executemany(
                f"UPDATE {table_qual} SET embedding = %s::vector WHERE id = %s",
                batch,
            )
        conn.commit()
        done += len(batch)

    elapsed = time.monotonic() - started
    log.info("==> %s done: %d rows in %.1fs (%.1f/s)",
             source_label, done, elapsed, done / elapsed if elapsed else 0)
    return done


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default="yorik_test",
                        help="Postgres database name")
    parser.add_argument("--batch", type=int, default=64,
                        help="rows per UPDATE batch (default 64)")
    args = parser.parse_args()

    if not os.getenv("YORIK_DB_PASSWORD") and not os.getenv("YORIK_DB_URL"):
        env_path = PROJECT / "infra/supabase/docker/.env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("POSTGRES_PASSWORD="):
                    os.environ["YORIK_DB_PASSWORD"] = line.split("=", 1)[1]
                    break
    os.environ["YORIK_DB_NAME"] = args.target

    from backend.database_pg import conn_ctx_pg, close_all_pools  # noqa: E402

    total = 0
    try:
        # paperless_chunks lives in the docs schema.
        with conn_ctx_pg("docs") as pg:
            total += _backfill(pg, "docs.paperless_chunks", "text",
                               "paperless_chunks", args.batch)
        # wa_messages lives in the main schema. Its PK is msg_id (not id) —
        # `_backfill` aliases it as `id` in the SELECT and is wired to UPDATE
        # WHERE msg_id = … through the `pk_col` parameter.
        with conn_ctx_pg("main") as pg:
            total += _backfill(
                pg, "public.wa_messages",
                "coalesce(text, '') || ' ' || coalesce(transcript, '')",
                "wa_messages", args.batch,
                pk_col="msg_id",
            )
        log.info("\nTotal: %d rows embedded into yorik_test (%s)", total, args.target)
        return 0
    finally:
        close_all_pools()


if __name__ == "__main__":
    raise SystemExit(main())
