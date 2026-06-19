"""Semantic search over WhatsApp messages (sqlite-vec, nomic-embed-text).

Sits alongside the existing FTS5 keyword search in whatsapp.py:
  FTS5      = high precision, exact-word matches ("show me the contract")
  Semantic  = high recall, meaning matches ("anything about the agreement")

The draft generator fuses both — picks the union of top-K results from
each, dedups by msg_id, and feeds the combined set to qwen3 as
"possibly relevant context." That way the LLM sees genuine intent
matches AND keyword pivots, without getting drowned in noise.

Storage lives in data/documents.db (same SQLite file as Paperless's
vec index) under two new tables:

  wa_chunks  — one row per indexed WA message; carries chat_jid/msg_id
               so we can hydrate back into the conversation context
  wa_vec     — sqlite-vec virtual table (EMBED_DIM=768) on rowid

Why a separate DB from family.db: heavy reindex jobs and ANN searches
should never lock the calendar/tasks pipeline. Same rationale that
already split documents.db out in Wave 4.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import sqlite_vec

from .database import get_conn, get_docs_conn, init_docs_db, DEFAULT_DOCS_DB_PATH
from .documents import embed, EmbeddingError, EMBED_DIM, ollama_reachable

log = logging.getLogger("yorik.whatsapp.semantic")

DOCS_DB_PATH = os.getenv("HOMEOS_DOCS_DB_PATH", DEFAULT_DOCS_DB_PATH)

# Don't bother embedding ultra-short messages — "ok", "thumbs", "lol" carry
# no semantic signal and would dilute the index without helping retrieval.
MIN_TEXT_CHARS = 12


def ensure_schema() -> None:
    """Add wa_chunks + wa_vec to documents.db. Idempotent."""
    init_docs_db(DOCS_DB_PATH)
    conn = get_docs_conn(DOCS_DB_PATH)
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS wa_chunks (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_jid      TEXT NOT NULL,
                msg_id        TEXT NOT NULL,
                text          TEXT NOT NULL,
                timestamp     INTEGER NOT NULL,
                push_name     TEXT,
                from_me       INTEGER NOT NULL DEFAULT 0,
                UNIQUE (chat_jid, msg_id)
            );
            CREATE INDEX IF NOT EXISTS ix_wa_chunks_chat ON wa_chunks(chat_jid, timestamp DESC);
        """)
        # wa_vec dim follows the global EMBED_DIM. _ensure_vec_table
        # handles the drop+recreate when the embedder model changes.
        from .database import _ensure_vec_table
        _ensure_vec_table(conn, "wa_vec", EMBED_DIM)
        conn.commit()
    finally:
        conn.close()


def index_message(msg_id: str, chat_jid: str, text: Optional[str],
                  ts: int, push_name: Optional[str] = None,
                  from_me: bool = False) -> bool:
    """Embed + insert one message. Returns True if indexed, False if
    skipped (too short, no embedder, already indexed)."""
    if not text or len(text.strip()) < MIN_TEXT_CHARS:
        return False
    text = text.strip()

    # First-message-ever guard: index_all + stats both call ensure_schema
    # before touching wa_chunks, but index_message (the per-message hot
    # path) skipped it — so fresh installs would OperationalError on the
    # first incoming WA text message until someone ran the bulk indexer.
    # ensure_schema is idempotent (CREATE IF NOT EXISTS); cheap to call.
    ensure_schema()

    conn = get_docs_conn(DOCS_DB_PATH)
    try:
        # Skip if already indexed — embeddings are deterministic per
        # input but the API call costs ~50-200 ms, no point re-running.
        existing = conn.execute(
            "SELECT id FROM wa_chunks WHERE chat_jid=? AND msg_id=?",
            (chat_jid, msg_id),
        ).fetchone()
        if existing:
            return False
    finally:
        conn.close()

    try:
        vec = embed(text)
    except EmbeddingError as e:
        log.warning("embed failed for %s/%s: %s", chat_jid, msg_id, e)
        return False

    conn = get_docs_conn(DOCS_DB_PATH)
    try:
        cur = conn.execute(
            "INSERT INTO wa_chunks (chat_jid, msg_id, text, timestamp, push_name, from_me) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (chat_jid, msg_id, text, ts, push_name, 1 if from_me else 0),
        )
        chunk_id = cur.lastrowid
        conn.execute(
            "INSERT INTO wa_vec (rowid, embedding) VALUES (?, ?)",
            (chunk_id, sqlite_vec.serialize_float32(vec)),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def search(query_text: str, k: int = 5,
           exclude_chat_jid: Optional[str] = None) -> list[dict[str, Any]]:
    """Semantic search across all WA messages. Returns up to `k` results,
    each {chat_jid, msg_id, text, push_name, timestamp, distance,
    chat_name}. Distance is cosine; lower = closer."""
    if not query_text or len(query_text.strip()) < 3:
        return []
    try:
        qvec = embed(query_text)
    except EmbeddingError as e:
        log.debug("query embed failed: %s", e)
        return []

    conn = get_docs_conn(DOCS_DB_PATH)
    try:
        # vec0 requires LIMIT inside the MATCH subquery — same pattern
        # paperless_ingest uses. Over-fetch a bit so we still have k
        # results after the exclude_chat filter.
        fetch_k = k + (3 if exclude_chat_jid else 0)
        rows = conn.execute("""
            SELECT c.chat_jid, c.msg_id, c.text, c.push_name, c.timestamp,
                   c.from_me, v.distance
            FROM (
                SELECT rowid, distance
                FROM wa_vec
                WHERE embedding MATCH ?
                ORDER BY distance
                LIMIT ?
            ) v
            JOIN wa_chunks c ON c.id = v.rowid
            ORDER BY v.distance
        """, (sqlite_vec.serialize_float32(qvec), int(fetch_k))).fetchall()
    finally:
        conn.close()

    if not rows:
        return []

    # Hydrate chat names from family.db so the LLM context can say
    # "from chat with Lena Hoffmann" instead of a raw JID.
    jids = list({r["chat_jid"] for r in rows})
    placeholders = ",".join("?" * len(jids))
    with get_conn() as fam:
        name_rows = fam.execute(
            f"SELECT jid, name FROM wa_chats WHERE jid IN ({placeholders})", jids
        ).fetchall()
    names = {r["jid"]: r["name"] for r in name_rows}

    out = []
    for r in rows:
        if exclude_chat_jid and r["chat_jid"] == exclude_chat_jid:
            continue
        out.append({
            "chat_jid": r["chat_jid"],
            "msg_id":   r["msg_id"],
            "text":     r["text"],
            "push_name": r["push_name"],
            "timestamp": r["timestamp"],
            "from_me":  bool(r["from_me"]),
            "distance": float(r["distance"]),
            "chat_name": names.get(r["chat_jid"]) or r["chat_jid"].split("@")[0],
        })
        if len(out) >= k:
            break
    return out


def backfill(limit: Optional[int] = None) -> dict[str, Any]:
    """Index every wa_message that isn't yet in wa_chunks. Returns
    {indexed, skipped, errors, total}. Safe to re-run."""
    if not ollama_reachable():
        return {"error": "Ollama (embedder) unreachable — start it or set HOMEOS_OLLAMA_BASE_URL"}
    ensure_schema()

    # Find candidate messages from family.db. Prefer text; fall back to
    # transcript so voice notes get indexed once Whisper has run.
    with get_conn() as conn:
        q = (
            "SELECT msg_id, chat_jid, COALESCE(text, transcript) AS body, "
            "       push_name, from_me, timestamp "
            "FROM wa_messages "
            "WHERE (text IS NOT NULL OR transcript IS NOT NULL) "
            "ORDER BY timestamp DESC"
        )
        if limit:
            q += f" LIMIT {int(limit)}"
        candidates = conn.execute(q).fetchall()

    # Skip ones already in wa_chunks — fast set check.
    conn = get_docs_conn(DOCS_DB_PATH)
    try:
        indexed = {(r["chat_jid"], r["msg_id"]) for r in conn.execute(
            "SELECT chat_jid, msg_id FROM wa_chunks"
        ).fetchall()}
    finally:
        conn.close()

    n_indexed = n_skipped = n_errors = 0
    for c in candidates:
        if (c["chat_jid"], c["msg_id"]) in indexed:
            n_skipped += 1
            continue
        try:
            ok = index_message(
                msg_id=c["msg_id"],
                chat_jid=c["chat_jid"],
                text=c["body"],
                ts=c["timestamp"],
                push_name=c["push_name"],
                from_me=bool(c["from_me"]),
            )
            if ok:
                n_indexed += 1
            else:
                n_skipped += 1
        except Exception as e:
            log.exception("backfill failed for %s/%s: %s", c["chat_jid"], c["msg_id"], e)
            n_errors += 1
    return {"total": len(candidates), "indexed": n_indexed,
            "skipped": n_skipped, "errors": n_errors}


def index_stats() -> dict[str, Any]:
    """Quick health stats — number of indexed messages + reachable
    embedder status. Used by the UI badge."""
    try:
        ensure_schema()
        conn = get_docs_conn(DOCS_DB_PATH)
        try:
            n = conn.execute("SELECT COUNT(*) FROM wa_chunks").fetchone()[0]
        finally:
            conn.close()
        return {"indexed_messages": n, "embedder_reachable": ollama_reachable(),
                "embed_dim": EMBED_DIM}
    except Exception as e:
        return {"error": str(e), "embedder_reachable": ollama_reachable()}
