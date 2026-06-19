"""Paperless → sqlite-vec ingestion pipeline.

Listens for Paperless's POST_CONSUME_SCRIPT webhook (one call per
finished document), fetches the OCR'd text + metadata via Paperless's
REST API, chunks + embeds it with the same Ollama-based embedder as the
legacy local-docs pipeline (nomic-embed-text, 768-dim), and upserts into
the `paperless_chunks` + `paperless_vec` tables in `documents.db`.

The result is a vector index that mirrors Paperless's text content. The
LLM gets a `paperless.search_semantic` op that queries this index and
returns top-K passages with citations the user can click.

Failure modes — all best-effort:
  - Paperless unreachable → log + skip; Paperless retries via reindex_all
  - Embedder unreachable → log + skip; same recovery path
  - Doc has zero extracted text (image-only PDF, OCR off) → skip silently

Re-ingesting the same paperless_doc_id replaces all its chunks atomically
so updates in Paperless propagate cleanly.
"""

from __future__ import annotations

import logging
import math
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import requests
import sqlite_vec

from .database import DEFAULT_DOCS_DB_PATH, get_docs_conn, init_docs_db
from .documents import chunk_text, embed, EmbeddingError


def _l2_normalize(vec: List[float]) -> List[float]:
    """Project a vector onto the unit hypersphere. Two effects:
      1. sqlite-vec's L2 distance becomes monotonically equivalent to
         cosine distance (cos = 1 - L2²/2 when both vectors are unit-norm).
      2. distances are bounded in [0, 2] instead of unbounded, so the
         similarity score we surface to users is a meaningful percentage.
    nomic-embed-text via Ollama doesn't normalize by default, so we do it.
    """
    n = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / n for x in vec]

log = logging.getLogger("homeos.paperless_ingest")

DOCS_DB_PATH = os.getenv("HOMEOS_DOCS_DB_PATH", DEFAULT_DOCS_DB_PATH)
PAPERLESS_TIMEOUT = 10


def _pg_backend() -> bool:
    """True iff Yorik is running against Postgres. The ingest, mirror
    accounting, and prune paths branch on this — Postgres writes the
    embedding directly into docs.paperless_chunks.embedding (a
    vector(384) column with an ivfflat index, declared in the Phase D
    bootstrap), instead of going through the SQLite paperless_vec
    virtual table that doesn't exist on the Postgres side."""
    return (os.getenv("YORIK_DB_BACKEND") or "sqlite").lower() == "postgres"


def _qvec_literal(vec: List[float]) -> str:
    """Serialise a float vector into pgvector's text literal shape so it
    can be parameter-bound + cast to ::vector. Mirrors the convention
    used by the existing search() pgvector branch."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


# ─── Paperless API helpers ─────────────────────────────────────────────────

def _paperless_settings() -> Dict[str, str]:
    """Read Paperless base_url + API token from app_settings (auto-populated
    by start.sh first-run) or from the connector credential store."""
    from .database import conn_ctx, DEFAULT_DB_PATH
    from . import credential_store
    creds = credential_store.get("paperless") or {}
    base_url = creds.get("base_url")
    api_key = creds.get("api_key")
    if not (base_url and api_key):
        try:
            with conn_ctx(os.getenv("HOMEOS_DB_PATH", DEFAULT_DB_PATH)) as c:
                for key, dest in (("paperless_base_url", "base_url"),
                                  ("paperless_api_token", "api_key")):
                    row = c.execute(
                        "SELECT value FROM app_settings WHERE key = ?", (key,)
                    ).fetchone()
                    if row and row["value"]:
                        if dest == "base_url" and not base_url: base_url = row["value"]
                        elif dest == "api_key" and not api_key: api_key = row["value"]
        except Exception:  # noqa: BLE001
            pass
    return {"base_url": (base_url or "http://localhost:8010").rstrip("/"),
            "api_key": api_key or ""}


def _fetch_doc(paperless_doc_id: int) -> Optional[Dict[str, Any]]:
    """Returns Paperless doc as a dict (title, content, correspondent, …)
    or None if not reachable / not found."""
    s = _paperless_settings()
    if not s["api_key"]:
        log.warning("paperless_ingest: no API token — skipping doc %s", paperless_doc_id)
        return None
    headers = {"Authorization": f"Token {s['api_key']}", "Accept": "application/json"}
    try:
        r = requests.get(
            f"{s['base_url']}/api/documents/{int(paperless_doc_id)}/",
            headers=headers, timeout=PAPERLESS_TIMEOUT,
        )
        if r.status_code == 404:
            log.warning("paperless_ingest: doc %s not found", paperless_doc_id)
            return None
        r.raise_for_status()
        return r.json()
    except requests.RequestException as exc:
        log.warning("paperless_ingest: fetch failed for %s: %s", paperless_doc_id, exc)
        return None


def _list_all_ids() -> List[int]:
    """Walk the whole Paperless corpus (paginated) and return every doc id.
    Used by reindex_all on first install or after a wipe."""
    s = _paperless_settings()
    if not s["api_key"]:
        return []
    headers = {"Authorization": f"Token {s['api_key']}", "Accept": "application/json"}
    ids: List[int] = []
    next_url = f"{s['base_url']}/api/documents/?page_size=100&fields=id&ordering=id"
    while next_url:
        try:
            r = requests.get(next_url, headers=headers, timeout=PAPERLESS_TIMEOUT)
            r.raise_for_status()
            data = r.json() or {}
            ids.extend(d["id"] for d in (data.get("results") or []))
            next_url = data.get("next")
        except requests.RequestException as exc:
            # Propagate instead of returning a partial list — the outer
            # try/except in reconcile_once treats this as "Paperless
            # unreachable" and early-returns without pruning. Swallowing
            # the failure here would let the prune path mistake a
            # mid-walk pagination error for "those ids no longer exist."
            log.warning("paperless_ingest: paginated list failed: %s", exc)
            raise
    return ids


# ─── Ingestion ─────────────────────────────────────────────────────────────

def _delete_existing_chunks(conn, paperless_doc_id: int) -> None:
    """Remove any prior chunks for this doc. Called before re-ingest so
    updates in Paperless replace cleanly.

    Postgres: a single DELETE on docs.paperless_chunks suffices because
    the embedding is a column on that table.
    SQLite: also wipe the paperless_vec virtual table rows tied to those
    chunk ids — sqlite-vec is a separate table.
    """
    if _pg_backend():
        conn.execute(
            "DELETE FROM paperless_chunks WHERE paperless_doc_id = %s",
            (paperless_doc_id,),
        )
        return
    rows = conn.execute(
        "SELECT id FROM paperless_chunks WHERE paperless_doc_id = ?", (paperless_doc_id,)
    ).fetchall()
    chunk_ids = [r["id"] for r in rows]
    for cid in chunk_ids:
        conn.execute("DELETE FROM paperless_vec WHERE rowid = ?", (cid,))
    conn.execute("DELETE FROM paperless_chunks WHERE paperless_doc_id = ?", (paperless_doc_id,))


def _chunk_preamble(doc: Dict[str, Any]) -> str:
    """Prepended to every chunk's embedded text so the vector captures the
    document's identity, not just body words. Helps with "find the Vodafone
    contract" queries even when the chunk itself doesn't mention 'Vodafone'."""
    parts = []
    if doc.get("title"):
        parts.append(f"Document: {doc['title']}")
    corr = doc.get("correspondent__name") or doc.get("correspondent")
    if corr and isinstance(corr, str):
        parts.append(f"From: {corr}")
    dtype = doc.get("document_type__name") or doc.get("document_type")
    if dtype and isinstance(dtype, str):
        parts.append(f"Type: {dtype}")
    if doc.get("created_date") or doc.get("created"):
        parts.append(f"Date: {(doc.get('created_date') or doc.get('created'))[:10]}")
    return " · ".join(parts) + "\n\n" if parts else ""


def ingest_one(paperless_doc_id: int) -> Dict[str, Any]:
    """Fetch one Paperless doc, chunk + embed + upsert. Returns a summary
    dict. Errors are caught and surfaced via the dict, not raised — keeps
    the webhook endpoint always responding 200."""
    doc = _fetch_doc(paperless_doc_id)
    if not doc:
        return {"ok": False, "id": paperless_doc_id, "error": "not_found_or_unreachable"}

    # Phase B.x: honour yorik-space-<id> marker tag set during upload.
    # Apply the space's Paperless permissions (view+change groups for
    # shared spaces, owner-only for personal) and then strip the marker
    # so it doesn't clutter the UI. Eventually-consistent: this runs on
    # the post-consume webhook, a few seconds after the user uploads.
    _apply_space_marker(paperless_doc_id, doc)

    content = (doc.get("content") or "").strip()
    if not content:
        return {"ok": False, "id": paperless_doc_id, "error": "no_text_content"}

    preamble = _chunk_preamble(doc)
    chunks = chunk_text(content)
    if not chunks:
        return {"ok": False, "id": paperless_doc_id, "error": "chunker_returned_nothing"}

    if _pg_backend():
        # Postgres path: write directly into docs.paperless_chunks with
        # the embedding column. The pool's connection-level
        # `search_path=docs,public` (see database_pg._ensure_pool) means
        # unqualified `paperless_chunks` resolves to docs.paperless_chunks.
        from .database_pg import conn_ctx_pg
        with conn_ctx_pg("docs") as conn:
            _delete_existing_chunks(conn, paperless_doc_id)
            n_ok = 0
            n_fail = 0
            for idx, (body, char_start, char_end) in enumerate(chunks):
                try:
                    vec = _l2_normalize(embed(preamble + body))
                except EmbeddingError as exc:
                    log.warning("paperless_ingest: embed failed for doc %s chunk %s: %s",
                                paperless_doc_id, idx, exc)
                    n_fail += 1
                    continue
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO paperless_chunks "
                        "(paperless_doc_id, chunk_index, text, "
                        " char_start, char_end, embedding) "
                        "VALUES (%s, %s, %s, %s, %s, %s::vector)",
                        (paperless_doc_id, idx, body,
                         char_start, char_end, _qvec_literal(vec)),
                    )
                n_ok += 1
        return {"ok": True, "id": paperless_doc_id, "chunks": n_ok,
                "embed_failures": n_fail, "title": doc.get("title", "")}

    init_docs_db(DOCS_DB_PATH)  # idempotent — ensures vec0 tables exist
    conn = get_docs_conn(DOCS_DB_PATH)
    try:
        _delete_existing_chunks(conn, paperless_doc_id)
        n_ok = 0
        n_fail = 0
        for idx, (body, char_start, char_end) in enumerate(chunks):
            try:
                vec = _l2_normalize(embed(preamble + body))
            except EmbeddingError as exc:
                log.warning("paperless_ingest: embed failed for doc %s chunk %s: %s",
                            paperless_doc_id, idx, exc)
                n_fail += 1
                continue
            cur = conn.execute(
                "INSERT INTO paperless_chunks "
                "(paperless_doc_id, chunk_index, text, char_start, char_end) "
                "VALUES (?, ?, ?, ?, ?)",
                (paperless_doc_id, idx, body, char_start, char_end),
            )
            chunk_id = cur.lastrowid
            conn.execute(
                "INSERT INTO paperless_vec (rowid, embedding) VALUES (?, ?)",
                (chunk_id, sqlite_vec.serialize_float32(vec)),
            )
            n_ok += 1
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "id": paperless_doc_id, "chunks": n_ok, "embed_failures": n_fail,
            "title": doc.get("title", "")}


def reindex_all() -> Dict[str, Any]:
    """Walk the whole Paperless corpus and re-ingest every document.
    Used on first install (backfill any pre-existing Paperless data) or
    after wiping the vector tables. Slow — runs in background-task style
    in the endpoint."""
    ids = _list_all_ids()
    summary = {"total": len(ids), "ok": 0, "skipped": 0, "failed": 0, "details": []}
    for did in ids:
        r = ingest_one(did)
        if r.get("ok"):
            summary["ok"] += 1
        elif r.get("error") in ("no_text_content",):
            summary["skipped"] += 1
        else:
            summary["failed"] += 1
        summary["details"].append(r)
    return summary


def _mirrored_ids() -> set[int]:
    """Set of paperless_doc_id values currently present in the local
    chunk mirror. Empty set if the table doesn't exist yet."""
    if _pg_backend():
        from .database_pg import conn_ctx_pg
        try:
            with conn_ctx_pg("docs") as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT paperless_doc_id FROM paperless_chunks"
                )
                rows = cur.fetchall()
            return {int(r[0]) for r in rows}
        except Exception:  # noqa: BLE001
            return set()
    from backend.documents import DOCS_DB_PATH
    from backend.database import get_docs_conn
    try:
        with get_docs_conn(DOCS_DB_PATH) as conn:
            rows = conn.execute(
                "SELECT DISTINCT paperless_doc_id FROM paperless_chunks"
            ).fetchall()
        return {int(r["paperless_doc_id"]) for r in rows}
    except Exception:  # noqa: BLE001 — table may not exist on first boot
        return set()


# Per-process cache of paperless ids we've already confirmed have no
# extractable text (image-only PDFs that Paperless couldn't OCR, etc.).
# Skipping these on every reconcile pass keeps us from hammering the
# Paperless API + embedder for docs whose answer won't change. Cleared
# on process restart — at most one re-attempt per boot is acceptable.
_no_text_skiplist: set[int] = set()


def reconcile_once() -> Dict[str, Any]:
    """Diff Paperless live ids vs the local mirror — ingest any missing
    AND prune any stale (mirror has it, Paperless doesn't).

    Cheap when there's no drift (one /api/documents/?fields=id call +
    one local SELECT). Runs at startup AND every reconcile interval —
    catches webhook misses, fresh-install backlogs, post-restore gaps,
    and external deletions without anyone needing to hit /reindex-all.

    Pruning rule: ids that disappeared from Paperless's live listing
    (deleted, trashed, or otherwise unreachable from the user's token)
    get their chunks + vectors wiped from paperless_chunks/paperless_vec.
    Without this, semantic search keeps returning hits that resolve to
    a 404 in the UI — confusing and a privacy leak (chunk text outlives
    the source doc). The wider Yorik `documents` table is also swept
    for rows whose on-disk file is gone: those are upload-mirror rows
    orphaned by external deletes or a storage relocation, and they
    surface as broken thumbnails via the duplicate-fix fallback path.

    Failures are bucketed into 'skipped' (no OCR text — permanent for
    that doc until someone re-OCRs it in Paperless) vs 'failed' (retry-
    worthy: network blip, embedder crashed, etc.). Only 'failed' counts
    as a problem in the worker heartbeat."""
    try:
        live = set(_list_all_ids())
    except Exception as exc:  # noqa: BLE001
        log.warning("paperless reconcile: list failed (paperless unreachable?): %s", exc)
        return {"checked": 0, "missing": 0, "ingested": 0, "skipped": 0,
                "failed": 0, "pruned": 0, "orphaned_local": 0, "error": str(exc)}

    mirrored = _mirrored_ids()
    missing = sorted((live - mirrored) - _no_text_skiplist)
    stale = sorted(mirrored - live)

    # Prune stale paperless_chunks/paperless_vec first — fast, no network.
    # Also drop the id from the per-process no-text skiplist so a re-add
    # of the same paperless_doc_id later re-ingests cleanly.
    pruned = 0
    if stale:
        from backend.documents import DOCS_DB_PATH
        from backend.database import get_docs_conn
        try:
            with get_docs_conn(DOCS_DB_PATH) as conn:
                for did in stale:
                    _delete_existing_chunks(conn, did)
                    _no_text_skiplist.discard(did)
                    pruned += 1
                conn.commit()
            log.info("paperless reconcile: pruned %d stale paperless_chunk(s) for ids no longer in Paperless", pruned)
        except Exception as exc:  # noqa: BLE001
            log.warning("paperless reconcile: prune failed: %s", exc)

    # Sweep the wider Yorik `documents` table for rows whose file is
    # gone — they surface as broken thumbnails via the duplicate-fix
    # fallback when Paperless returns empty. Path-existence is the
    # link we have today; a proper paperless_doc_id column on
    # `documents` would let us do explicit cleanup instead.
    orphaned_local = _prune_orphaned_local_documents()

    if not missing:
        return {"checked": len(live), "missing": 0, "ingested": 0,
                "skipped": len(_no_text_skiplist), "failed": 0,
                "pruned": pruned, "orphaned_local": orphaned_local}

    log.info("paperless reconcile: %d doc(s) missing from local mirror, ingesting", len(missing))
    ingested = 0
    skipped = 0
    failed = 0
    for did in missing:
        r = ingest_one(did)
        if r.get("ok"):
            ingested += 1
        elif r.get("error") == "no_text_content":
            skipped += 1
            _no_text_skiplist.add(did)  # don't retry next pass
        else:
            failed += 1
            log.debug("paperless reconcile: ingest %d failed: %s", did, r.get("error"))
    log.info("paperless reconcile: ingested %d / skipped %d (no OCR) / failed %d / pruned %d / orphaned_local %d",
             ingested, skipped, failed, pruned, orphaned_local)
    return {"checked": len(live), "missing": len(missing),
            "ingested": ingested, "skipped": skipped, "failed": failed,
            "pruned": pruned, "orphaned_local": orphaned_local}


def _prune_orphaned_local_documents() -> int:
    """Delete rows from the Yorik `documents` table whose on-disk file
    no longer exists. Best-effort: any error is logged and the row is
    left alone for the next reconcile pass.

    Called from reconcile_once(). Catches two failure modes:
      - external Paperless delete (file got removed via the host)
      - storage relocation that didn't bring the local pile along
        (data/documents/ was a dangling symlink for a moment, files
        gone but rows still in the index)
    Without this, the duplicate-fix fallback path surfaces these as
    broken-thumbnail tiles whenever Paperless live is empty."""
    from pathlib import Path
    from backend.documents import DOCS_DB_PATH, delete_document
    from backend.database import get_docs_conn
    pruned = 0
    try:
        with get_docs_conn(DOCS_DB_PATH) as conn:
            rows = conn.execute("SELECT id, path FROM documents").fetchall()
        orphans = [int(r["id"]) for r in rows if not Path(r["path"]).exists()]
    except Exception as exc:  # noqa: BLE001
        log.warning("paperless reconcile: orphan scan failed: %s", exc)
        return 0
    for did in orphans:
        try:
            if delete_document(did):
                pruned += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("paperless reconcile: orphan delete %d failed: %s", did, exc)
    if pruned:
        log.info("paperless reconcile: pruned %d orphaned local documents row(s) whose files are gone", pruned)
    return pruned


# Reconciler runs at startup, then every RECONCILE_INTERVAL_S. Six hours
# is a good balance: catches webhook misses promptly enough that "I added
# a doc this morning and chat search can't find it" doesn't happen, but
# doesn't hammer Paperless with full-list polls.
RECONCILE_INTERVAL_S = 6 * 3600


async def background_reconciler() -> None:
    """Long-lived background task. Reconciles at startup, then on a
    fixed interval. Heartbeats into the workers registry so the home
    screen surfaces silent failures (Paperless unreachable, ingest
    consistently failing, etc.)."""
    import asyncio
    from backend import workers
    workers.register("paperless_reconciler", kind="scheduler",
                     expected_interval_s=RECONCILE_INTERVAL_S)
    while True:
        try:
            res = await asyncio.to_thread(reconcile_once)
            if res.get("error"):
                workers.heartbeat("paperless_reconciler", "warn",
                                  f"reconcile failed: {res['error'][:60]}")
            elif res["missing"] == 0:
                skip_note = f" ({res.get('skipped', 0)} no-OCR)" if res.get("skipped") else ""
                prune_note = ""
                if res.get("pruned") or res.get("orphaned_local"):
                    prune_note = f", pruned {res.get('pruned',0)}+{res.get('orphaned_local',0)} stale"
                workers.heartbeat("paperless_reconciler", "ok",
                                  f"{res['checked']} docs in sync{skip_note}{prune_note}")
            else:
                # 'failed' is the only retry-worthy category — others
                # are accounted for and not actionable.
                status = "warn" if res.get("failed", 0) > 0 else "ok"
                workers.heartbeat("paperless_reconciler", status,
                                  f"ingested {res['ingested']}/{res['missing']}"
                                  + (f", {res['skipped']} no-OCR" if res.get('skipped') else "")
                                  + (f", {res['failed']} failed" if res.get('failed') else "")
                                  + (f", pruned {res['pruned']}" if res.get('pruned') else "")
                                  + (f", orphaned {res['orphaned_local']}" if res.get('orphaned_local') else ""))
            log.info("task tick: paperless_reconciler checked=%s missing=%s ingested=%s failed=%s",
                     res.get("checked"), res.get("missing"),
                     res.get("ingested"), res.get("failed"),
                     extra={"task": "paperless_reconciler", "status": "ok",
                            "checked": res.get("checked"), "missing": res.get("missing"),
                            "ingested": res.get("ingested"), "failed": res.get("failed")})
        except asyncio.CancelledError:
            workers.report_error("paperless_reconciler", "cancelled")
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("paperless reconciler iteration failed: %s", exc,
                          extra={"task": "paperless_reconciler", "status": "exception"})
            workers.report_error("paperless_reconciler", str(exc)[:80])
        await asyncio.sleep(RECONCILE_INTERVAL_S)


# ─── Recent + count (for the chat "show me a document" / "wie viele" path) ──

def recent_with_count(k: int = 5,
                      creds_override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Most-recent N docs from Paperless PLUS the library total.

    Returns {"hits": [...same shape as search()...], "total": int}.
    Used by SearchDocumentsTool's empty-query path so the LLM can answer
    "wie viele Dokumente habe ich" without a second roundtrip. Failures
    return {"hits": [], "total": 0} so the calling tool doesn't blow up
    when Paperless isn't reachable.
    """
    s = creds_override or _paperless_settings()
    if not s.get("api_key"):
        return {"hits": [], "total": 0}
    try:
        r = requests.get(
            f"{s['base_url']}/api/documents/",
            headers={"Authorization": f"Token {s['api_key']}"},
            params={"ordering": "-created", "page_size": max(1, min(int(k), 30))},
            timeout=PAPERLESS_TIMEOUT,
        )
        r.raise_for_status()
        body = r.json() or {}
    except requests.RequestException as exc:
        log.warning("paperless recent_with_count failed: %s", exc)
        return {"hits": [], "total": 0}
    items = body.get("results", []) or []
    hits = [{
        "doc_id":      d.get("id"),
        "doc_title":   d.get("title") or f"Document {d.get('id')}",
        "doc_mime":    d.get("mime_type") or "application/pdf",
        "chunk_text":  (d.get("content") or "")[:300],
        "chunk_index": 0,
        "distance":    None,
        "source":      "paperless",
    } for d in items]
    return {"hits": hits, "total": int(body.get("count") or 0)}


# ─── Semantic search ───────────────────────────────────────────────────────

def search(query: str, k: int = 8,
           creds_override: Optional[Dict[str, Any]] = None,
           visible_space_ids: Optional[List[int]] = None) -> List[Dict[str, Any]]:
    """Embed the query, return top-K most-similar chunks across the whole
    Paperless mirror, hydrated with Paperless's document metadata via a
    single batched REST call so the LLM has citations to surface.

    creds_override: optional {base_url, api_key} to use for the
    hydration call instead of the global admin token. Wave-3 callers
    pass the per-user Paperless token so each user only sees the docs
    Paperless's own ACL lets them see.

    visible_space_ids: optional list of space IDs the caller's user has
    visibility into (Phase C ACL). When provided, semantic results are
    restricted to chunks whose `paperless_chunks.space_id` is in the
    list. When None, NO space filter is applied (back-compat default;
    callers that don't pass this MUST be on a code path where space
    filtering happens elsewhere — e.g. legacy admin tools / reindex).
    """
    if not query or not query.strip():
        return []
    try:
        qvec = _l2_normalize(embed(query))
    except EmbeddingError as exc:
        log.warning("paperless search: embed failed: %s", exc)
        return []

    import os as _os
    _pg_backend = (_os.getenv("YORIK_DB_BACKEND") or "sqlite").lower() == "postgres"

    init_docs_db(DOCS_DB_PATH)
    conn = get_docs_conn(DOCS_DB_PATH)
    try:
        if _pg_backend:
            # IVFFLAT defaults probes=1 — drops hits silently until the
            # index has thousands of vectors. Bump per-statement so small
            # corpora work; index switches to HNSW post-launch.
            try:
                conn.execute("SET LOCAL ivfflat.probes = 100")
            except Exception:  # noqa: BLE001
                pass
            # pgvector path. `<=>` is cosine distance for vectors that
            # were L2-normalised before storage (which we do — see
            # _l2_normalize at ingest time). `embedding IS NOT NULL`
            # skips rows where the backfill hasn't reached yet.
            qvec_str = "[" + ",".join(repr(float(x)) for x in qvec) + "]"
            if visible_space_ids:
                # NULL space_id rows are legacy chunks ingested before
                # Phase C (no space tag on the Paperless doc). The
                # historical contract was 'visible to the household' —
                # post-Phase-C we keep that compatibility by including
                # NULL alongside the caller's visible spaces, otherwise
                # chat doc-search returns empty for the entire pre-
                # Phase-C corpus.
                placeholders = ",".join("%s" for _ in visible_space_ids)
                sql = (
                    "SELECT pc.id, pc.paperless_doc_id, pc.chunk_index, pc.text, "
                    "       (pc.embedding <=> %s::vector) AS distance "
                    "FROM paperless_chunks pc "
                    f"WHERE pc.embedding IS NOT NULL "
                    f"  AND (pc.space_id IN ({placeholders}) OR pc.space_id IS NULL) "
                    "ORDER BY pc.embedding <=> %s::vector "
                    "LIMIT %s"
                )
                params = (qvec_str, *visible_space_ids, qvec_str, int(k))
            else:
                sql = (
                    "SELECT pc.id, pc.paperless_doc_id, pc.chunk_index, pc.text, "
                    "       (pc.embedding <=> %s::vector) AS distance "
                    "FROM paperless_chunks pc "
                    "WHERE pc.embedding IS NOT NULL "
                    "ORDER BY pc.embedding <=> %s::vector "
                    "LIMIT %s"
                )
                params = (qvec_str, qvec_str, int(k))
            rows = conn.execute(sql, params).fetchall()
        elif visible_space_ids:
            # SQLite + sqlite_vec path. vec0 requires the LIMIT (or
            # `k = ?`) to be evaluated on the virtual table itself, not
            # on a JOIN result. Subquery pattern: ANN-search first, then
            # hydrate from paperless_chunks. Over-fetch from ANN by 5x
            # so the post-JOIN space filter can drop hits outside the
            # caller's visible spaces and still hand back k results.
            ann_limit = max(int(k) * 5, int(k) + 16)
            placeholders = ",".join("?" * len(visible_space_ids))
            rows = conn.execute(f"""
                SELECT pc.id, pc.paperless_doc_id, pc.chunk_index, pc.text, v.distance
                FROM (
                    SELECT rowid, distance
                    FROM paperless_vec
                    WHERE embedding MATCH ?
                    ORDER BY distance
                    LIMIT ?
                ) v
                JOIN paperless_chunks pc ON pc.id = v.rowid
                WHERE pc.space_id IN ({placeholders})
                ORDER BY v.distance
                LIMIT ?
            """, (
                sqlite_vec.serialize_float32(qvec),
                ann_limit,
                *visible_space_ids,
                int(k),
            )).fetchall()
        else:
            rows = conn.execute("""
                SELECT pc.id, pc.paperless_doc_id, pc.chunk_index, pc.text, v.distance
                FROM (
                    SELECT rowid, distance
                    FROM paperless_vec
                    WHERE embedding MATCH ?
                    ORDER BY distance
                    LIMIT ?
                ) v
                JOIN paperless_chunks pc ON pc.id = v.rowid
                ORDER BY v.distance
            """, (sqlite_vec.serialize_float32(qvec), int(k))).fetchall()
    finally:
        conn.close()

    if not rows:
        return []

    # Batch-fetch the document metadata so each result carries a citation.
    # Per-user creds (wave 3) take precedence: pass them in via the
    # `creds_override` arg so Anna's search hydrates against her token
    # and Paperless's own ACL filters the visible docs.
    s = creds_override or _paperless_settings()
    headers = {"Authorization": f"Token {s['api_key']}", "Accept": "application/json"}
    doc_ids = sorted({r["paperless_doc_id"] for r in rows})
    docs_meta: Dict[int, Dict[str, Any]] = {}
    if s["api_key"]:
        try:
            r = requests.get(
                f"{s['base_url']}/api/documents/",
                headers=headers,
                params={"id__in": ",".join(str(i) for i in doc_ids),
                        "page_size": len(doc_ids)},
                timeout=PAPERLESS_TIMEOUT,
            )
            r.raise_for_status()
            for d in (r.json() or {}).get("results", []):
                docs_meta[d["id"]] = d
        except requests.RequestException as exc:
            log.warning("paperless search: doc-meta fetch failed: %s", exc)

    # Phase 12.1: derive visibility from each doc's tag list. visibility_of()
    # consults the resolved tag-id cache, no extra Paperless round-trip.
    from . import paperless_visibility as _pv

    results: List[Dict[str, Any]] = []
    for row in rows:
        meta = docs_meta.get(row["paperless_doc_id"], {})
        # Both query and stored vectors are L2-normalized, so the vec0
        # L2 distance d is related to cosine similarity by cos = 1 - d²/2,
        # bounded in [0, 1] (1 = identical, 0 = orthogonal).
        d = float(row["distance"])
        cos = max(0.0, min(1.0, 1.0 - (d * d) / 2.0))
        results.append({
            "chunk_id": row["id"],
            "paperless_doc_id": row["paperless_doc_id"],
            "chunk_index": row["chunk_index"],
            "text": row["text"],
            "similarity": round(cos, 4),
            "distance": round(d, 4),
            "doc_title": meta.get("title") or f"Document #{row['paperless_doc_id']}",
            "correspondent": meta.get("correspondent__name") or meta.get("correspondent"),
            "doc_date": (meta.get("created_date") or meta.get("created") or "")[:10],
            "doc_url": f"{s['base_url']}/documents/{row['paperless_doc_id']}/" if s["api_key"] else None,
            "preview_url": f"{s['base_url']}/api/documents/{row['paperless_doc_id']}/preview/" if s["api_key"] else None,
            "visibility": _pv.visibility_of(meta.get("tags") or []),
            "match_type": "semantic",
        })
    return results


def _vec_index_count() -> int:
    """How many chunks are currently embedded in the Paperless vec
    table. 0 means the bundled embedder hasn't ingested anything yet
    (or a recent dim-change wiped the table) — useful diagnostic when
    semantic returns nothing on a clean install."""
    if _pg_backend():
        try:
            from .database_pg import conn_ctx_pg
            with conn_ctx_pg("docs") as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM paperless_chunks "
                    "WHERE embedding IS NOT NULL"
                )
                row = cur.fetchone()
            return int(row[0] if row else 0)
        except Exception:  # noqa: BLE001
            return 0
    try:
        from .database import get_docs_conn
        with get_docs_conn(DOCS_DB_PATH) as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM paperless_vec").fetchone()
        return int(row["n"] if row else 0)
    except Exception:  # noqa: BLE001 — table may not exist yet
        return 0


def search_hybrid(query: str, k: int = 8,
                  creds_override: Optional[Dict[str, Any]] = None,
                  rrf_k: int = 60,
                  visible_space_ids: Optional[List[int]] = None) -> Dict[str, Any]:
    """Hybrid search: semantic (embeddings) + FTS (Paperless), fused via
    Reciprocal Rank Fusion (RRF).

    RRF (Cormack et al. 2009) is the de-facto fusion method for combining
    multiple ranked lists: score = sum over sources of 1/(rrf_k + rank).
    Documents appearing in BOTH lists score higher than either source
    alone; documents in only one list still surface naturally — no
    special-casing needed when one source returns nothing.

    Why hybrid beats either source alone:
      - Pure semantic misses exact-string matches (invoice numbers,
        OCR'd serial numbers, oddly-spelled proper nouns).
      - Pure FTS misses paraphrase / cross-language matches ("Rechnung
        Müller" ↔ "invoice Mueller", "Strom" ↔ "electricity bill").
      - Hybrid lifts the best of both — well-established as the modern
        retrieval default before reranking.

    Both legs are best-effort. Return shape:
      {
        "hits":  [...],   # RRF-fused, capped at k; each carries match_type
                          # ("semantic" | "fts" | "hybrid"), rrf_score
        "legs": {
          "semantic": {"count": int, "error": str | None,
                       "vec_count": int},
          "fts":      {"count": int, "error": str | None},
        }
      }
    The legs surface drives the UI's "which engines worked" indicator
    and the LLM's match-type caveats — when only one leg fired, the
    other's error explains why so the user/LLM can act on it.
    """
    import concurrent.futures
    from . import documents as _docs

    legs: Dict[str, Dict[str, Any]] = {
        "semantic": {"count": 0, "error": None, "vec_count": 0},
        "fts":      {"count": 0, "error": None},
    }

    if not query or not query.strip():
        return {"hits": [], "legs": legs}

    # ── Pre-flight diagnostics (cheap; avoids ambiguous empty results) ──
    # Semantic: embedder reachability AND index population. search()
    # swallows EmbeddingError and returns [], so without these probes
    # we couldn't distinguish "embedder down" from "no matches".
    if not _docs.embedder_reachable():
        legs["semantic"]["error"] = (
            f"Embedder not reachable (HOMEOS_EMBED_BACKEND={_docs.EMBED_BACKEND}). "
            "Bundled local model may still be loading; or set HOMEOS_EMBED_BASE_URL "
            "to an external /v1/embeddings endpoint."
        )
    else:
        vec_count = _vec_index_count()
        legs["semantic"]["vec_count"] = vec_count
        if vec_count == 0:
            legs["semantic"]["error"] = (
                "Vector index is empty — Paperless docs haven't been embedded yet. "
                "The reconciler runs every 6h; trigger /api/paperless/reindex-all to "
                "kick it off now."
            )

    # FTS: needs a Paperless API token. search_fts() bails to [] without
    # one — same diagnostic surfaced explicitly here.
    creds = creds_override or _paperless_settings()
    if not creds.get("api_key"):
        legs["fts"]["error"] = (
            "No Paperless API token configured. Visit Settings → Connectors → "
            "Paperless and paste an admin token, or run onboarding to provision "
            "a per-user token."
        )

    # ── Fan out — skip a leg if its pre-flight already errored ──────────
    leg_k = max(k * 2, k + 4)
    sem_hits: List[Dict[str, Any]] = []
    fts_hits: List[Dict[str, Any]] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        sem_fut = ex.submit(search, query, leg_k, creds_override, visible_space_ids) if legs["semantic"]["error"] is None else None
        fts_fut = ex.submit(search_fts, query, leg_k, creds_override) if legs["fts"]["error"] is None else None
        if sem_fut is not None:
            try:
                sem_hits = sem_fut.result()
            except Exception as exc:  # noqa: BLE001
                log.warning("hybrid search: semantic leg failed: %s", exc)
                legs["semantic"]["error"] = f"Semantic search failed: {exc}"
        if fts_fut is not None:
            try:
                fts_hits = fts_fut.result()
            except Exception as exc:  # noqa: BLE001
                log.warning("hybrid search: FTS leg failed: %s", exc)
                legs["fts"]["error"] = f"Paperless keyword search failed: {exc}"

    # Phase C T13: FTS leg hits Paperless directly with the admin token
    # so it returns docs from every workspace. Post-filter against
    # paperless_chunks.space_id so callers with `visible_space_ids` only
    # see docs whose chunks live in their workspaces. Without this,
    # workspace admins / members would see FTS-only hits from other
    # workspaces' Paperless docs (the semantic leg is already filtered).
    if fts_hits and visible_space_ids is not None:
        try:
            doc_ids = [h.get("paperless_doc_id") for h in fts_hits if h.get("paperless_doc_id") is not None]
            if doc_ids:
                init_docs_db(DOCS_DB_PATH)
                vc = get_docs_conn(DOCS_DB_PATH)
                try:
                    placeholders_vs = ",".join("?" * len(visible_space_ids))
                    placeholders_did = ",".join("?" * len(doc_ids))
                    rows = vc.execute(
                        f"SELECT DISTINCT paperless_doc_id FROM paperless_chunks "
                        f"WHERE paperless_doc_id IN ({placeholders_did}) "
                        f"  AND space_id IN ({placeholders_vs})",
                        (*doc_ids, *visible_space_ids),
                    ).fetchall()
                    allowed = {int(r[0]) for r in rows}
                    fts_hits = [h for h in fts_hits if h.get("paperless_doc_id") in allowed]
                finally:
                    vc.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("hybrid search: FTS space filter failed: %s", exc)
            fts_hits = []

    legs["semantic"]["count"] = len(sem_hits)
    legs["fts"]["count"] = len(fts_hits)

    # ── Fuse at the document level — dedupe by paperless_doc_id since
    #    the UI surfaces one card per doc. Semantic leg's best chunk
    #    wins as the snippet (better citation than FTS's first-300-chars).
    rrf_scores: Dict[int, float] = {}
    by_doc: Dict[int, Dict[str, Any]] = {}

    for rank, hit in enumerate(sem_hits):
        did = hit.get("paperless_doc_id")
        if did is None:
            continue
        rrf_scores[did] = rrf_scores.get(did, 0.0) + 1.0 / (rrf_k + rank + 1)
        if did not in by_doc:
            by_doc[did] = dict(hit)  # semantic hits already say match_type='semantic'

    for rank, hit in enumerate(fts_hits):
        did = hit.get("paperless_doc_id")
        if did is None:
            continue
        rrf_scores[did] = rrf_scores.get(did, 0.0) + 1.0 / (rrf_k + rank + 1)
        if did not in by_doc:
            by_doc[did] = dict(hit)  # fts hit, match_type='fts' already set
        else:
            by_doc[did]["match_type"] = "hybrid"  # in both lists

    if not by_doc:
        return {"hits": [], "legs": legs}

    # ── Title-match boost ───────────────────────────────────────────────
    # German embeddings ('nomic-embed-text') don't reliably distinguish
    # legalese doc types (Mietvertrag, Rechnung, Mahnung, KFZ-Versicherung
    # all cluster together because they share formal-letter prose). So
    # the semantic leg routinely returns wrong-type docs at rank 1 even
    # when FTS has the right ones, and RRF's rank-interleaving lets the
    # wrong ones win odd slots.
    # The boost is additive: all query tokens in title → +1.0 (guarantees
    # title matches sort first); partial coverage → mild boost (≤0.05);
    # no token match → 0. Short / common stop-words are dropped before
    # comparison so "ein Mietvertrag" doesn't over-boost on "ein".
    for did, hit in by_doc.items():
        boost = _title_match_score(hit.get("doc_title", ""), query)
        if boost:
            rrf_scores[did] = rrf_scores.get(did, 0.0) + boost

    ranked = sorted(
        by_doc.values(),
        key=lambda h: -rrf_scores.get(h.get("paperless_doc_id"), 0.0),
    )
    for hit in ranked:
        hit["rrf_score"] = round(rrf_scores.get(hit.get("paperless_doc_id"), 0.0), 4)
    return {"hits": ranked[:k], "legs": legs}


_TITLE_STOPWORDS = {
    "der", "die", "das", "den", "dem", "des",
    "ein", "eine", "einer", "einem", "eines", "einen",
    "und", "oder", "von", "zur", "zum", "für", "fur",
    "the", "and", "or", "of", "to", "for", "a", "an",
}


def _title_match_score(title: str, query: str) -> float:
    """Additive RRF boost based on how many query tokens land in title.

    All tokens present → 1.0 (guarantees a top slot).
    Partial coverage   → 0.05 * (matched / total) — mild nudge.
    No tokens / empty  → 0.

    Tokens shorter than 4 chars are dropped along with German/English
    stop words; substring match (not word-boundary) so compound forms
    like 'Mietverträge' still match a 'mietvertrag' query.
    """
    if not title or not query:
        return 0.0
    import re
    t = title.lower()
    tokens = [
        w for w in re.findall(r"\w+", query.lower())
        if len(w) >= 4 and w not in _TITLE_STOPWORDS
    ]
    if not tokens:
        return 0.0
    matched = sum(1 for w in tokens if w in t)
    if matched == 0:
        return 0.0
    if matched == len(tokens):
        return 1.0
    return 0.05 * (matched / len(tokens))


def search_fts(query: str, k: int = 8,
               creds_override: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Embedder-free fallback: ask Paperless's own full-text search.

    Used by find_document when semantic search returns [] (e.g. because
    the embedder is unreachable, or no chunks are indexed yet). Paperless
    has a tested, mature FTS over all OCR'd document content — not as
    smart as embeddings ('Rechnung Müller' won't match 'invoice Mueller'
    via translation) but always returns something useful if the document
    is in Paperless at all.

    Hit shape mirrors search() above so find_document doesn't need to
    branch in two places. match_type='fts' distinguishes these from
    semantic hits."""
    if not query or not query.strip():
        return []
    s = creds_override or _paperless_settings()
    if not s.get("api_key"):
        return []
    headers = {"Authorization": f"Token {s['api_key']}", "Accept": "application/json"}
    base = s["base_url"].rstrip("/")
    try:
        r = requests.get(
            f"{base}/api/documents/",
            headers=headers,
            params={"query": query, "page_size": int(k)},
            timeout=PAPERLESS_TIMEOUT,
        )
        r.raise_for_status()
        body = r.json() or {}
    except requests.RequestException as exc:
        log.warning("paperless FTS fallback failed: %s", exc)
        return []

    from . import paperless_visibility as _pv

    hits: List[Dict[str, Any]] = []
    for d in (body.get("results") or [])[:k]:
        text = (d.get("content") or "")[:400]
        hits.append({
            "chunk_id": None,
            "paperless_doc_id": d.get("id"),
            "chunk_index": 0,
            "text": text,
            "similarity": None,
            "distance": None,
            "doc_title": d.get("title") or f"Document #{d.get('id')}",
            "correspondent": d.get("correspondent__name") or d.get("correspondent"),
            "doc_date": (d.get("created_date") or d.get("created") or "")[:10],
            "doc_url": f"{base}/documents/{d.get('id')}/",
            "preview_url": f"{base}/api/documents/{d.get('id')}/preview/",
            "visibility": _pv.visibility_of(d.get("tags") or []),
            "match_type": "fts",
        })
    return hits


def _apply_space_marker(paperless_doc_id: int, doc: Dict[str, Any]) -> None:
    """If the doc carries a `yorik-space-<id>` tag, call
    paperless_provisioning.set_document_space() to set the right
    view_groups/change_groups (shared space) or owner (personal), then
    strip the marker tag. No-op when no marker present.

    Errors are logged + swallowed — the ingest pipeline runs in the
    background and shouldn't crash because permission application
    flaked. The drift detector + a manual /spaces/{id} fix-up will
    catch this on the next tick.
    """
    try:
        tag_names = [
            (t.get("name") if isinstance(t, dict) else None)
            for t in (doc.get("tag_names") or doc.get("tags") or [])
        ]
        tag_names = [t for t in tag_names if t]
        # Tags can also surface as bare strings via paperless's serialiser
        # — handle both shapes defensively.
        if not tag_names:
            tag_names = [
                t for t in (doc.get("tag_names") or doc.get("tags") or [])
                if isinstance(t, str)
            ]
        marker_prefix = "yorik-space-"
        marker = next((t for t in tag_names if t.startswith(marker_prefix)), None)
        if not marker:
            return
        try:
            space_id = int(marker[len(marker_prefix):])
        except ValueError:
            log.warning("paperless_ingest: bad space marker tag %r on doc %s",
                        marker, paperless_doc_id)
            return
        from . import paperless_provisioning as _pp
        ok = _pp.set_document_space(paperless_doc_id, space_id)
        if not ok:
            log.warning("paperless_ingest: set_document_space failed for doc=%s space=%s",
                        paperless_doc_id, space_id)
            return
        # Strip the marker tag now that permissions are applied.
        _strip_paperless_tag(paperless_doc_id, marker)
    except Exception as exc:  # noqa: BLE001
        log.warning("paperless_ingest: space marker application failed: %s", exc)


def _strip_paperless_tag(paperless_doc_id: int, tag_name: str) -> None:
    """Remove one tag (by name) from a Paperless document. Uses the
    admin token from connectors.paperless._settings — same source
    paperless_provisioning uses."""
    import requests as _req
    from .connectors.paperless import _settings as _p_settings
    s = _p_settings()
    if not s.get("api_key"):
        return
    base = (s.get("base_url") or "http://localhost:8010").rstrip("/")
    H = {"Authorization": f"Token {s['api_key']}", "Accept": "application/json"}
    # Find the tag id.
    r = _req.get(f"{base}/api/tags/", headers=H, params={"name": tag_name}, timeout=6)
    if not r.ok:
        return
    tag_id = None
    for t in (r.json() or {}).get("results") or []:
        if (t.get("name") or "") == tag_name:
            tag_id = int(t["id"]); break
    if tag_id is None:
        return
    # Fetch current tag ids on the doc; remove the marker; PATCH back.
    r = _req.get(f"{base}/api/documents/{paperless_doc_id}/", headers=H, timeout=6)
    if not r.ok:
        return
    current = [int(x) for x in (r.json().get("tags") or [])]
    if tag_id not in current:
        return
    new_tags = [x for x in current if x != tag_id]
    r = _req.patch(
        f"{base}/api/documents/{paperless_doc_id}/",
        headers={**H, "Content-Type": "application/json"},
        json={"tags": new_tags},
        timeout=6,
    )
    if not r.ok:
        log.warning("paperless_ingest: failed to strip marker tag %s from doc %s: HTTP %s",
                    tag_name, paperless_doc_id, r.status_code)
