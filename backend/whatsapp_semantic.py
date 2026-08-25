"""Semantic search over WhatsApp messages (pgvector, local embedder).

Sits alongside the keyword search in whatsapp.py:
  keyword   = high precision, exact-word matches ("show me the contract")
  semantic  = high recall, meaning matches ("anything about the agreement")

The draft generator fuses both — union of top-K from each, dedup by
msg_id — and feeds the combined set to the LLM as "possibly relevant
context".

Storage: ``docs.wa_chunks`` (one row per indexed message, with the
chat_jid/msg_id needed to hydrate it back into the conversation) and
its ``embedding vector`` column (migration 127). Both query and stored
vectors are L2-normalised, so ``<=>`` (cosine distance) orders hits
correctly and ``1 - distance`` is the cosine similarity.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Optional

from .database import get_conn
from .database_pg import conn_ctx_pg
from .documents import EMBED_DIM, EmbeddingError, embed, ollama_reachable

log = logging.getLogger("yorik.whatsapp.semantic")

# Don't bother embedding ultra-short messages — "ok", "thumbs", "lol"
# carry no semantic signal and would dilute the index.
MIN_TEXT_CHARS = 12


def _l2_normalize(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / n for x in vec]


def _vec_literal(vec: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def ensure_schema() -> None:
    """The table and its embedding column come from migrations_pg/.
    Kept so older call sites keep working."""
    return None


def index_message(msg_id: str, chat_jid: str, text: Optional[str],
                  ts: int, push_name: Optional[str] = None,
                  from_me: bool = False) -> bool:
    """Embed + upsert one message. Returns True if indexed, False if
    skipped (too short, no embedder, already indexed)."""
    if not text or len(text.strip()) < MIN_TEXT_CHARS:
        return False
    text = text.strip()

    with conn_ctx_pg("docs") as conn:
        existing = conn.execute(
            "SELECT id FROM wa_chunks WHERE chat_jid = %s AND msg_id = %s "
            "AND embedding IS NOT NULL",
            (chat_jid, msg_id),
        ).fetchone()
    if existing:
        return False

    try:
        vec = _l2_normalize(embed(text))
    except EmbeddingError as e:
        log.warning("embed failed for %s/%s: %s", chat_jid, msg_id, e)
        return False

    with conn_ctx_pg("docs") as conn:
        conn.execute(
            "INSERT INTO wa_chunks (chat_jid, msg_id, text, timestamp, push_name, from_me, embedding) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s::vector) "
            "ON CONFLICT (chat_jid, msg_id) DO UPDATE SET "
            "  text = EXCLUDED.text, embedding = EXCLUDED.embedding, "
            "  push_name = EXCLUDED.push_name, timestamp = EXCLUDED.timestamp",
            (chat_jid, msg_id, text, int(ts), push_name, 1 if from_me else 0, _vec_literal(vec)),
        )
    return True


def search(query_text: str, k: int = 5,
           exclude_chat_jid: Optional[str] = None) -> list[dict[str, Any]]:
    """Semantic search across all WA messages. Returns up to ``k`` results,
    each {chat_jid, msg_id, text, push_name, timestamp, from_me, distance,
    chat_name}. ``distance`` is cosine distance; lower = closer."""
    if not query_text or len(query_text.strip()) < 3:
        return []
    try:
        qvec = _l2_normalize(embed(query_text))
    except EmbeddingError as e:
        log.debug("query embed failed: %s", e)
        return []

    fetch_k = int(k) + (3 if exclude_chat_jid else 0)
    qlit = _vec_literal(qvec)
    with conn_ctx_pg("docs") as conn:
        with conn.cursor() as cur:
            # ivfflat probes default to 1, which silently drops rows on
            # small corpora; raise it for this statement.
            cur.execute("SET LOCAL ivfflat.probes = 100")
            cur.execute(
                "SELECT chat_jid, msg_id, text, push_name, timestamp, from_me, "
                "       (embedding <=> %s::vector) AS distance "
                "FROM wa_chunks "
                "WHERE embedding IS NOT NULL "
                "ORDER BY embedding <=> %s::vector "
                "LIMIT %s",
                (qlit, qlit, fetch_k),
            )
            rows = list(cur.fetchall())

    if not rows:
        return []

    # Hydrate chat names so the LLM context can say "from chat with
    # Lena Hoffmann" instead of a raw JID.
    jids = list({r["chat_jid"] for r in rows})
    placeholders = ",".join("?" * len(jids))
    with get_conn() as fam:
        name_rows = fam.execute(
            f"SELECT jid, name FROM wa_chats WHERE jid IN ({placeholders})", jids
        ).fetchall()
    names = {r["jid"]: r["name"] for r in name_rows}

    out: list[dict[str, Any]] = []
    for r in rows:
        if exclude_chat_jid and r["chat_jid"] == exclude_chat_jid:
            continue
        out.append({
            "chat_jid":  r["chat_jid"],
            "msg_id":    r["msg_id"],
            "text":      r["text"],
            "push_name": r["push_name"],
            "timestamp": r["timestamp"],
            "from_me":   bool(r["from_me"]),
            "distance":  float(r["distance"]),
            "chat_name": names.get(r["chat_jid"]) or r["chat_jid"].split("@")[0],
        })
        if len(out) >= k:
            break
    return out


def backfill(limit: Optional[int] = None) -> dict[str, Any]:
    """Index every wa_message that isn't yet embedded. Returns
    {indexed, skipped, errors, total}. Safe to re-run."""
    if not ollama_reachable():
        return {"error": "embedder unreachable — start it or check HOMEOS_EMBED_* settings"}

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

    with conn_ctx_pg("docs") as conn:
        indexed = {
            (r["chat_jid"], r["msg_id"])
            for r in conn.execute(
                "SELECT chat_jid, msg_id FROM wa_chunks WHERE embedding IS NOT NULL"
            ).fetchall()
        }

    n_indexed = n_skipped = n_errors = 0
    for c in candidates:
        if (c["chat_jid"], c["msg_id"]) in indexed:
            n_skipped += 1
            continue
        try:
            ok = index_message(
                msg_id=c["msg_id"], chat_jid=c["chat_jid"], text=c["body"],
                ts=c["timestamp"], push_name=c["push_name"], from_me=bool(c["from_me"]),
            )
            if ok:
                n_indexed += 1
            else:
                n_skipped += 1
        except Exception as e:  # noqa: BLE001
            log.exception("backfill failed for %s/%s: %s", c["chat_jid"], c["msg_id"], e)
            n_errors += 1
    return {"total": len(candidates), "indexed": n_indexed,
            "skipped": n_skipped, "errors": n_errors}


def index_stats() -> dict[str, Any]:
    """Number of embedded messages + embedder reachability, for the UI badge."""
    try:
        with conn_ctx_pg("docs") as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM wa_chunks WHERE embedding IS NOT NULL"
            ).fetchone()
        n = int(row["n"] if row else 0)
        return {"indexed_messages": n, "embedder_reachable": ollama_reachable(),
                "embed_dim": EMBED_DIM}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e), "embedder_reachable": ollama_reachable()}
