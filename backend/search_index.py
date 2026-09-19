"""One semantic index over everything the household search covers.

Plan: docs/plans/2026-09-19-suche-ueberall.md. Table `search_chunks`
(migration 141) holds pieces of text with their embedding, keyed by
source and row. It knows nothing about who may see a row: the search
(backend/search_routes.py) joins the source table with the same
visibility rule the app uses, so a changed share needs no rebuild.

Paperless documents and Immich photos keep their own indexes.

Embedder: the bundled MiniLM by default. YORIK_SEARCH_EMBED_URL points
the index at an OpenAI-shaped /v1/embeddings server instead — the
bundled option is Qwen3-Embedding-4B in a llama.cpp container on the
CPU (scripts/install-search-embedder.sh), which separates hits from
noise far better. Every chunk records the model that wrote it; after a
switch the sweep re-embeds, and a search only compares like with like.

The indexer is a background sweep: rows that never change (email,
WhatsApp, finished recordings) are indexed once, small tables that do
change (tasks, contacts, events, drafts) are compared by hash, rows
that are gone lose their chunks.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional

from .database import get_conn

log = logging.getLogger("yorik.search_index")

CHUNK_CHARS = 600          # the embedder cuts at 128 tokens
MIN_TEXT_CHARS = 12        # "ok", "danke" carry no meaning worth a vector
BATCH_ROWS = 200
SWEEP_INTERVAL_S = int(os.getenv("YORIK_SEARCH_INDEX_INTERVAL_S", "300"))
# Rows per source and sweep, so the first run over a big mailbox yields
# to the rest of the box; the sweep loops until nothing is left.
MAX_ROWS_PER_PASS = 2000

EMBED_URL = os.getenv("YORIK_SEARCH_EMBED_URL", "").rstrip("/")
EMBED_MODEL = os.getenv("YORIK_SEARCH_EMBED_MODEL", "qwen3-embedding-4b")
# Qwen3-Embedding wants the task spelled out on the query side only.
QUERY_PREFIX = os.getenv(
    "YORIK_SEARCH_EMBED_QUERY_PREFIX",
    "Instruct: Given a search query, retrieve relevant passages that answer the query\nQuery: ",
).replace("\\n", "\n") if EMBED_URL else ""
EMBED_BATCH = 32


def model_tag() -> str:
    """Names the embedder in search_chunks.model."""
    if EMBED_URL:
        return EMBED_MODEL
    from .embedders import local as _local
    return _local.MODEL_NAME.split("/")[-1]


def max_distance() -> float:
    """Cosine distance up to which a hit by meaning is shown. Measured
    on German household texts: MiniLM puts real hits below 0.5 and noise
    from 0.57; Qwen3-Embedding-4B real hits up to 0.61, noise above."""
    override = os.getenv("YORIK_SEARCH_MAX_DISTANCE")
    if override:
        return float(override)
    return 0.61 if EMBED_URL else 0.55


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _clean(*parts: Any) -> str:
    text = "\n".join(str(p).strip() for p in parts if p and str(p).strip())
    return re.sub(r"[ \t]+", " ", text).strip()


def _strip_html(html: Optional[str]) -> str:
    if not html:
        return ""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def chunk_text(text: str, max_chunks: int) -> list[str]:
    """Pieces of about CHUNK_CHARS, cut at paragraph, sentence or word
    ends. The first piece carries the head of the row (subject, title)."""
    text = (text or "").strip()
    if len(text) < MIN_TEXT_CHARS:
        return []
    out: list[str] = []
    rest = text
    while rest and len(out) < max_chunks:
        if len(rest) <= CHUNK_CHARS:
            out.append(rest)
            break
        window = rest[:CHUNK_CHARS]
        cut = max(window.rfind("\n"), window.rfind(". "), window.rfind("? "), window.rfind("! "))
        if cut < CHUNK_CHARS // 2:
            cut = window.rfind(" ")
        if cut < CHUNK_CHARS // 2:
            cut = CHUNK_CHARS - 1
        out.append(rest[:cut + 1].strip())
        rest = rest[cut + 1:].strip()
    return [c for c in out if c]


# ─── sources ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Source:
    name: str
    table: str
    columns: str                      # SELECT list, must include id
    text: Callable[[Any], str]
    where: str = "TRUE"               # rows worth indexing
    immutable: bool = False           # index once vs. compare by hash
    max_chunks: int = 2


def _recording_text(r) -> str:
    parts: list[Any] = [r["title"]]
    try:
        report = json.loads(r["report_json"] or "{}")
        parts.append(report.get("summary"))
        for key in ("decisions", "tasks", "highlights", "open_questions"):
            for item in report.get(key) or []:
                parts.append(item if isinstance(item, str) else
                             " ".join(str(v) for v in item.values() if isinstance(v, str)))
    except (ValueError, AttributeError):
        pass
    with get_conn() as conn:
        segs = conn.execute(
            "SELECT speaker_label, text FROM recording_segments WHERE recording_id = ? ORDER BY seq",
            (r["id"],),
        ).fetchall()
    parts.extend(f"{s['speaker_label'] or ''}: {s['text']}" for s in segs if s["text"])
    return _clean(*parts)


SOURCES: dict[str, Source] = {s.name: s for s in (
    Source("email", "email_messages", "id, subject, from_name, from_email, snippet, body_text",
           lambda r: _clean(r["subject"], r["from_name"] or r["from_email"], r["body_text"] or r["snippet"]),
           where="COALESCE(is_draft, 0) = 0", immutable=True, max_chunks=3),
    Source("whatsapp", "wa_messages", "id, text, transcript, push_name",
           lambda r: _clean(r["text"] or r["transcript"]),
           where="COALESCE(text, transcript, '') <> ''", immutable=True, max_chunks=2),
    Source("tasks", "tasks", "id, title, notes, person, category",
           lambda r: _clean(r["title"], r["notes"], r["person"], r["category"])),
    Source("contacts", "contacts",
           "id, display_name, aliases, legal_name, relation, role, notes, tags",
           lambda r: _clean(r["display_name"], r["aliases"], r["legal_name"], r["relation"],
                            r["role"], r["notes"], r["tags"]),
           where="status = 'active' AND merged_into_id IS NULL", max_chunks=1),
    Source("events", "events", "id, title, notes, location, person, category",
           lambda r: _clean(r["title"], r["notes"], r["location"], r["person"], r["category"])),
    Source("recordings", "recordings", "id, title, report_json",
           _recording_text, where="status = 'done'", immutable=True, max_chunks=60),
    Source("drafts", "compose_drafts", "id, subject, recipient, body_html",
           lambda r: _clean(r["subject"], r["recipient"], _strip_html(r["body_html"])), max_chunks=4),
)}


# ─── embedding ───────────────────────────────────────────────────────

def _l2(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / n for x in vec]


def vec_literal(vec: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def _embed_external(texts: list[str]) -> list[list[float]]:
    import requests
    out: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH):
        r = requests.post(f"{EMBED_URL}/embeddings",
                          json={"model": EMBED_MODEL, "input": texts[i:i + EMBED_BATCH]}, timeout=120)
        r.raise_for_status()
        data = sorted(r.json()["data"], key=lambda d: d.get("index", 0))
        out.extend(_l2(d["embedding"]) for d in data)
    return out


def embed_many(texts: list[str]) -> list[list[float]]:
    """Normalised vectors for a batch of stored texts. Module-level so
    tests swap it. With an external embedder configured there is no
    fallback: two models in one index would not compare."""
    if EMBED_URL:
        return _embed_external(texts)
    from . import documents as _docs
    if _docs.EMBED_BACKEND in ("auto", "local") and not _docs.EMBED_BASE_URL:
        from .embedders import local as _local
        return [_l2(v) for v in _local.embed_batch(texts)]
    return [_l2(_docs.embed(t)) for t in texts]


def embed_query(text: str) -> Optional[str]:
    """The query as a pgvector literal, or None when no embedder works
    (the search then stays keyword-only)."""
    try:
        return vec_literal(embed_many([QUERY_PREFIX + text])[0])
    except Exception as exc:  # noqa: BLE001
        log.debug("query embed failed: %s", exc)
        return None


# ─── indexing ────────────────────────────────────────────────────────

def _write_rows(src: Source, rows: list[Any]) -> int:
    """Chunk, embed and store a batch of source rows. A row without
    usable text gets one empty chunk, so it is not picked up again."""
    pending: list[tuple[int, int, str, str]] = []     # row_id, chunk_no, text, hash
    for r in rows:
        text = src.text(r)
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
        chunks = chunk_text(text, src.max_chunks) or [""]
        pending.extend((int(r["id"]), i, c, digest) for i, c in enumerate(chunks))
    to_embed = [p[2] for p in pending if p[2]]
    vectors = iter(embed_many(to_embed)) if to_embed else iter(())
    now, model = _now(), model_tag()
    with get_conn() as conn:
        for row_id in {p[0] for p in pending}:
            conn.execute("DELETE FROM search_chunks WHERE source = ? AND row_id = ?", (src.name, row_id))
        for row_id, chunk_no, text, digest in pending:
            vec = vec_literal(next(vectors)) if text else None
            conn.execute(
                "INSERT INTO search_chunks (source, row_id, chunk_no, text, content_hash, embedding, model, indexed_at) "
                "VALUES (?, ?, ?, ?, ?, ?::vector, ?, ?)",
                (src.name, row_id, chunk_no, text, digest, vec, model, now),
            )
        conn.commit()
    return len(rows)


def _index_immutable(src: Source) -> int:
    done = 0
    while done < MAX_ROWS_PER_PASS:
        with get_conn() as conn:
            rows = conn.execute(
                f"SELECT {src.columns} FROM {src.table} t WHERE ({src.where}) AND NOT EXISTS "
                f"(SELECT 1 FROM search_chunks sc WHERE sc.source = ? AND sc.row_id = t.id) "
                f"ORDER BY t.id DESC LIMIT ?",
                (src.name, BATCH_ROWS),
            ).fetchall()
        if not rows:
            break
        done += _write_rows(src, rows)
    return done


def _index_mutable(src: Source) -> int:
    with get_conn() as conn:
        rows = conn.execute(f"SELECT {src.columns} FROM {src.table} t WHERE ({src.where})").fetchall()
        known = {int(r["row_id"]): r["content_hash"] for r in conn.execute(
            "SELECT DISTINCT row_id, content_hash FROM search_chunks WHERE source = ?", (src.name,),
        ).fetchall()}
    changed = [r for r in rows
               if known.get(int(r["id"])) != hashlib.sha1(src.text(r).encode("utf-8")).hexdigest()]
    done = 0
    for i in range(0, len(changed), BATCH_ROWS):
        done += _write_rows(src, changed[i:i + BATCH_ROWS])
    return done


def _drop_gone(src: Source) -> int:
    """Chunks of rows that no longer exist, and chunks another embedder
    wrote (they come back with the current one in the same sweep)."""
    with get_conn() as conn:
        cur = conn.execute(
            f"DELETE FROM search_chunks sc WHERE sc.source = ? AND (sc.model <> ? OR NOT EXISTS "
            f"(SELECT 1 FROM {src.table} t WHERE t.id = sc.row_id AND ({src.where})))",
            (src.name, model_tag()),
        )
        conn.commit()
        return cur.rowcount or 0


def sweep() -> dict[str, int]:
    """One pass over every source. Returns rows indexed per source."""
    out: dict[str, int] = {}
    for src in SOURCES.values():
        try:
            _drop_gone(src)
            out[src.name] = _index_immutable(src) if src.immutable else _index_mutable(src)
        except Exception as exc:  # noqa: BLE001 — one source must not stop the others
            log.warning("search index: %s failed: %s", src.name, exc)
            out[src.name] = -1
    return out


def stats() -> dict[str, int]:
    with get_conn() as conn:
        return {r["source"]: int(r["n"]) for r in conn.execute(
            "SELECT source, COUNT(DISTINCT row_id) AS n FROM search_chunks GROUP BY source").fetchall()}


_scheduler_task = None


def start_scheduler(loop: asyncio.AbstractEventLoop) -> None:
    from . import workers
    global _scheduler_task
    workers.register("search-index", kind="indexer", expected_interval_s=SWEEP_INTERVAL_S)

    async def _loop():
        await asyncio.sleep(45)     # let the start-up settle first
        while True:
            busy = False
            try:
                result = await asyncio.get_running_loop().run_in_executor(None, sweep)
                indexed = sum(n for n in result.values() if n > 0)
                busy = any(n >= MAX_ROWS_PER_PASS for n in result.values())
                failed = [k for k, n in result.items() if n < 0]
                workers.heartbeat("search-index", "warn" if failed else "ok",
                                  f"{indexed} rows indexed" + (f", failed: {', '.join(failed)}" if failed else ""))
            except Exception as exc:  # noqa: BLE001
                log.warning("search index: sweep failed: %s", exc)
            await asyncio.sleep(5 if busy else SWEEP_INTERVAL_S)

    _scheduler_task = loop.create_task(_loop(), name="search-index")
