"""Document corpus + RAG retrieval.

Pipeline:
    upload → extract_text → chunk → embed (bundled sentence-transformers,
    or external /v1/embeddings if HOMEOS_EMBED_BASE_URL is set) →
    persist chunk text in document_chunks + vector in vec_chunks (sqlite-vec)

Search:
    query → embed → sqlite-vec ANN → join chunks + documents → role filter →
    top-k results, ready for the LLM to cite. Higher-level
    `paperless_ingest.search_hybrid()` fuses these with FTS via RRF.

All document data lives in data/documents.db (NOT family.db) on purpose.
Heavy reindex jobs never touch the operational DB. The vector index can
be wiped + rebuilt without affecting personal data.

License of every dep:  sqlite-vec MIT, pypdf BSD-3, python-docx MIT,
sentence-transformers Apache 2.0. Safe for commercial use.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
import sqlite_vec

from .database import DEFAULT_DOCS_DB_PATH, get_docs_conn, init_docs_db

log = logging.getLogger("homeos.documents")

DOCS_DB_PATH = os.getenv("HOMEOS_DOCS_DB_PATH", DEFAULT_DOCS_DB_PATH)
DOCS_DIR = Path(os.getenv("HOMEOS_DOCS_DIR", "data/documents"))
EMBED_MODEL = os.getenv("HOMEOS_EMBED_MODEL", "nomic-embed-text-v1.5")
# External embedder endpoint (OpenAI-compatible /v1/embeddings).
# Empty string disables the external path entirely. The bundled local
# sentence-transformers embedder is the default so semantic search
# works on day 1 without any external endpoint.
EMBED_BASE_URL = os.getenv("HOMEOS_EMBED_BASE_URL", "").rstrip("/")
# Backend selection:
#   auto     — try external first, fall back to bundled local (default)
#   external — only the configured EMBED_BASE_URL; no local fallback
#   local    — only the bundled sentence-transformers, skip external
#   off      — embed() always raises; hybrid search degrades to FTS only
EMBED_BACKEND = os.getenv("HOMEOS_EMBED_BACKEND", "auto").lower()
CHUNK_SIZE = int(os.getenv("HOMEOS_CHUNK_SIZE", "500"))          # rough tokens (~4 chars/tok for English)
CHUNK_OVERLAP = int(os.getenv("HOMEOS_CHUNK_OVERLAP", "50"))


def _resolve_embed_dim() -> int:
    """Output dimension of the embedder. The local model's dim wins
    because vec0 tables must be sized at table-creation time and the
    local model is the always-available default. Power users running an
    external embedder with a different dim must set HOMEOS_EMBED_DIM
    explicitly so the vec tables match."""
    override = os.getenv("HOMEOS_EMBED_DIM")
    if override:
        return int(override)
    if EMBED_BACKEND == "off":
        return 384  # arbitrary — never used, but the schema needs a number
    try:
        from .embedders.local import dimension
        return dimension()
    except Exception as exc:  # noqa: BLE001
        log.warning("local embedder dim probe failed (%s); defaulting to 384", exc)
        return 384


EMBED_DIM = _resolve_embed_dim()


# ─── extraction ────────────────────────────────────────────────────────────

SUPPORTED_MIME = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/plain",
    "text/markdown",
    "text/x-markdown",
    "application/octet-stream",  # fallback by extension
}


def detect_mime(path: Path) -> str:
    ext = path.suffix.lower()
    return {
        ".pdf":  "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".md":   "text/markdown",
        ".markdown": "text/markdown",
        ".txt":  "text/plain",
    }.get(ext, "application/octet-stream")


def extract_text(path: Path, mime: Optional[str] = None) -> str:
    """Best-effort text extraction. Returns a single string; chunking happens next."""
    mime = mime or detect_mime(path)
    if mime == "application/pdf":
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        pages = []
        for page in reader.pages:
            try:
                pages.append(page.extract_text() or "")
            except Exception as exc:  # noqa: BLE001
                log.warning("pdf page extract failed: %s", exc)
        return "\n\n".join(pages)
    if mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        import docx
        doc = docx.Document(str(path))
        return "\n\n".join(p.text for p in doc.paragraphs if p.text)
    # text/markdown/plain or unknown: read as utf-8 with fallback
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="latin-1", errors="replace")


# ─── chunking ──────────────────────────────────────────────────────────────

# Sliding window over paragraphs. Pack paragraphs into chunks of ~CHUNK_SIZE
# tokens (estimated as chars / 4), with CHUNK_OVERLAP token tail kept in the
# next chunk so semantics aren't cut mid-thought.

_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n+")


def _approx_tokens(s: str) -> int:
    return max(1, len(s) // 4)


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> List[Tuple[str, int, int]]:
    """Returns list of (chunk_text, char_start, char_end).

    Strategy:
      1. Split into paragraphs.
      2. Greedily pack paragraphs into a chunk until ~chunk_size tokens.
      3. When closing a chunk, keep the trailing ~overlap tokens as the
         start of the next chunk so context bleeds across boundaries.
      4. Very large single paragraphs are split on sentence boundaries; if
         a single sentence is still too long, hard-split on character count.
    """
    if not text or not text.strip():
        return []
    target_chars = chunk_size * 4
    overlap_chars = overlap * 4
    paragraphs = [p.strip() for p in _PARAGRAPH_SPLIT.split(text) if p.strip()]
    if not paragraphs:
        return []

    chunks: List[Tuple[str, int, int]] = []
    cur: List[str] = []
    cur_len = 0
    cursor = 0  # global char offset

    def flush(end_offset: int) -> None:
        nonlocal cur, cur_len
        if not cur:
            return
        body = "\n\n".join(cur)
        start = end_offset - len(body)
        chunks.append((body, start, end_offset))
        # Build the overlap tail for the next chunk
        if overlap_chars > 0 and len(body) > overlap_chars:
            tail = body[-overlap_chars:]
            # snap to next sentence boundary
            m = re.search(r"[.!?]\s", tail)
            if m:
                tail = tail[m.end():]
            cur = [tail] if tail else []
            cur_len = len(tail) // 4
        else:
            cur = []
            cur_len = 0

    for para in paragraphs:
        # Locate the paragraph in the original text starting from cursor.
        idx = text.find(para, cursor)
        if idx < 0:
            idx = cursor
        end = idx + len(para)
        cursor = end

        para_tokens = _approx_tokens(para)
        # If the paragraph alone exceeds chunk_size, hard-split it.
        if para_tokens > chunk_size:
            # Flush whatever's pending first.
            if cur:
                flush(idx)
            # Split this long paragraph into pieces of target_chars.
            piece_start = 0
            while piece_start < len(para):
                piece_end = min(piece_start + target_chars, len(para))
                # Snap to a space if possible
                if piece_end < len(para):
                    space = para.rfind(" ", piece_start, piece_end)
                    if space > piece_start + target_chars // 2:
                        piece_end = space
                piece = para[piece_start:piece_end].strip()
                if piece:
                    abs_start = idx + piece_start
                    chunks.append((piece, abs_start, abs_start + len(piece)))
                piece_start = piece_end
            continue

        if cur_len + para_tokens > chunk_size and cur:
            flush(idx)

        cur.append(para)
        cur_len += para_tokens

    if cur:
        flush(cursor)
    return chunks


# ─── embedding ─────────────────────────────────────────────────────────────

class EmbeddingError(Exception):
    pass


def _embed_external(text: str) -> List[float]:
    """Call the configured OpenAI-compatible /v1/embeddings endpoint."""
    r = requests.post(
        f"{EMBED_BASE_URL}/embeddings",
        json={"model": EMBED_MODEL, "input": text},
        timeout=30,
    )
    if not r.ok:
        raise EmbeddingError(f"external embeddings HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    emb = ((data.get("data") or [{}])[0]).get("embedding")
    if not (emb and isinstance(emb, list)):
        raise EmbeddingError(f"unexpected /v1/embeddings response shape: {data}")
    if len(emb) != EMBED_DIM:
        raise EmbeddingError(
            f"external embedder returned dim {len(emb)}, expected {EMBED_DIM}. "
            f"Set HOMEOS_EMBED_DIM={len(emb)} (and reindex) or switch model."
        )
    return [float(x) for x in emb]


def _embed_local(text: str) -> List[float]:
    """Use the bundled sentence-transformers model."""
    from .embedders import local as _local
    return _local.embed(text)


def embed(text: str) -> List[float]:
    """Compute a sentence embedding via the configured embedder.

    Backend selection (HOMEOS_EMBED_BACKEND):
      auto (default) — external first if EMBED_BASE_URL set, else local;
                       on external failure, fall back to local
      external       — only EMBED_BASE_URL; no local fallback
      local          — only the bundled sentence-transformers
      off            — raises EmbeddingError unconditionally

    Raises EmbeddingError on failure — callers are best-effort (the
    document just doesn't get its vector index; search degrades to FTS).
    """
    if not text or not text.strip():
        return [0.0] * EMBED_DIM
    if EMBED_BACKEND == "off":
        raise EmbeddingError("embedder disabled (HOMEOS_EMBED_BACKEND=off)")

    last_error: Optional[str] = None

    # External attempt — only when configured and not explicitly opted out.
    try_external = EMBED_BACKEND in ("auto", "external") and bool(EMBED_BASE_URL)
    if try_external:
        try:
            return _embed_external(text)
        except (requests.RequestException, EmbeddingError) as exc:
            last_error = f"external embedder failed: {exc}"
            if EMBED_BACKEND == "external":
                raise EmbeddingError(last_error)
            log.debug("%s; falling back to local embedder", last_error)

    # Local fallback — always available once sentence-transformers is installed.
    if EMBED_BACKEND in ("auto", "local"):
        try:
            return _embed_local(text)
        except Exception as exc:  # noqa: BLE001
            local_err = f"local embedder failed: {exc}"
            raise EmbeddingError(
                f"{last_error}; {local_err}" if last_error else local_err
            ) from exc

    raise EmbeddingError(last_error or "no embedder configured")


def embedder_reachable() -> bool:
    """True iff at least one embedder backend can serve a request.

    Used by /api/health and start.sh banners. We treat 'local' as always
    reachable (the model loads or it doesn't — `is_available` covers
    that), and probe `/v1/models` for external."""
    if EMBED_BACKEND == "off":
        return False
    if EMBED_BACKEND in ("auto", "local"):
        from .embedders import local as _local
        if _local.is_available():
            return True
        if EMBED_BACKEND == "local":
            return False
    if EMBED_BACKEND in ("auto", "external") and EMBED_BASE_URL:
        try:
            r = requests.get(f"{EMBED_BASE_URL}/models", timeout=2)
            return r.ok
        except requests.RequestException:
            return False
    return False


# Backwards-compat alias for callers still using the old name.
ollama_reachable = embedder_reachable


# ─── CRUD + indexing ───────────────────────────────────────────────────────

def _ensure_storage() -> None:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    init_docs_db(DOCS_DB_PATH)


def add_document(
    title: str,
    src_path: Path,
    mime_type: Optional[str] = None,
    tags: Optional[List[str]] = None,
    allowed_roles: str = "admin",
    owner_user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Copy the upload into DOCS_DIR, write the row, return metadata.

    Indexing (text extraction + embedding) is a separate call — `index_document(id)`.
    Splitting the two means a failing embed doesn't block the upload itself.
    """
    _ensure_storage()
    mime = mime_type or detect_mime(src_path)
    bytes_count = src_path.stat().st_size
    with get_docs_conn(DOCS_DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO documents (title, path, mime_type, bytes, tags, allowed_roles, owner_user_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (title, "", mime, bytes_count, ",".join(tags or []), allowed_roles, owner_user_id),
        )
        doc_id = cur.lastrowid
        # Place the file under DOCS_DIR/<doc_id>/<filename>
        dest_dir = DOCS_DIR / str(doc_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src_path.name
        if dest.exists():
            dest.unlink()
        # If src and dest are different paths, copy bytes; otherwise leave.
        with open(src_path, "rb") as f_in, open(dest, "wb") as f_out:
            f_out.write(f_in.read())
        conn.execute("UPDATE documents SET path = ? WHERE id = ?", (str(dest), doc_id))
        conn.commit()
        row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    return dict(row)


def list_documents(role: Optional[str] = None) -> List[Dict[str, Any]]:
    """List all documents the given role may read."""
    with get_docs_conn(DOCS_DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, title, mime_type, bytes, tags, allowed_roles, chunk_count, created_at, indexed_at "
            "FROM documents ORDER BY created_at DESC"
        ).fetchall()
    out = [dict(r) for r in rows]
    if role and role not in ("platform_admin", "admin"):
        out = [d for d in out if role in (d["allowed_roles"] or "").split(",")]
    return out


def recent(k: int = 5, role: Optional[str] = None) -> List[Dict[str, Any]]:
    """Most-recently-added documents for the role. Returns a flat list
    shaped like search() hits (doc_id, doc_title, doc_mime, snippet) so
    SearchDocumentsTool can render the same UI cards. Used when the
    user wants to browse without a query — "show me a document",
    "irgendein Dokument", etc.

    Snippet is the first chunk's text when available, otherwise empty.
    """
    docs = list_documents(role=role)
    if not docs:
        return []
    docs = docs[:max(1, min(int(k), 20))]
    # Pull a sample chunk for each so the UI card has a preview line.
    out: List[Dict[str, Any]] = []
    with get_docs_conn(DOCS_DB_PATH) as conn:
        for d in docs:
            row = conn.execute(
                "SELECT text FROM document_chunks WHERE doc_id = ? "
                "ORDER BY chunk_index ASC LIMIT 1",
                (d["id"],),
            ).fetchone()
            snippet = (row["text"] if row else "") or ""
            out.append({
                "doc_id":      d["id"],
                "doc_title":   d.get("title") or f"Document {d['id']}",
                "doc_mime":    d.get("mime_type"),
                "chunk_text":  snippet,
                "chunk_index": 0,
                "distance":    None,
            })
    return out


def get_document(doc_id: int) -> Optional[Dict[str, Any]]:
    with get_docs_conn(DOCS_DB_PATH) as conn:
        row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    return dict(row) if row else None


def delete_document(doc_id: int) -> bool:
    """Remove the document row, its chunks, its vectors, and the file on disk."""
    if (os.getenv("YORIK_DB_BACKEND") or "sqlite").lower() == "postgres":
        # Postgres: no separate vec_chunks table — embedding is a column
        # on document_chunks. FK ON DELETE CASCADE handles the chunks.
        from .database_pg import conn_ctx_pg
        with conn_ctx_pg("docs") as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT path FROM documents WHERE id = %s", (doc_id,))
                row = cur.fetchone()
                if not row:
                    return False
                doc_path = row[0] if not isinstance(row, dict) else row.get("path")
                cur.execute("DELETE FROM documents WHERE id = %s", (doc_id,))
        try:
            p = Path(doc_path) if doc_path else None
            if p and p.exists():
                p.unlink()
            if p and p.parent.exists() and p.parent.parent == DOCS_DIR and not any(p.parent.iterdir()):
                p.parent.rmdir()
        except OSError as exc:
            log.warning("cleanup failed for doc %s: %s", doc_id, exc)
        return True

    with get_docs_conn(DOCS_DB_PATH) as conn:
        row = conn.execute("SELECT path FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if not row:
            return False
        # Delete chunk vectors first (vec table doesn't cascade)
        chunk_ids = [r["id"] for r in conn.execute("SELECT id FROM document_chunks WHERE doc_id = ?", (doc_id,))]
        if chunk_ids:
            placeholders = ",".join("?" * len(chunk_ids))
            conn.execute(f"DELETE FROM vec_chunks WHERE rowid IN ({placeholders})", chunk_ids)
        # ON DELETE CASCADE wipes document_chunks
        conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        conn.commit()
    # Best-effort file cleanup — never fatal.
    try:
        p = Path(row["path"])
        if p.exists():
            p.unlink()
        if p.parent.exists() and p.parent.parent == DOCS_DIR and not any(p.parent.iterdir()):
            p.parent.rmdir()
    except OSError as exc:
        log.warning("cleanup failed for doc %s: %s", doc_id, exc)
    return True


def index_document(doc_id: int) -> Dict[str, Any]:
    """Extract → chunk → embed → persist. Replaces any existing chunks for this doc.

    Returns {ok, chunk_count, embed_failed_count, error?}. Doesn't raise.
    """
    meta = get_document(doc_id)
    if not meta:
        return {"ok": False, "error": f"document {doc_id} not found"}
    path = Path(meta["path"])
    if not path.exists():
        return {"ok": False, "error": f"file missing on disk: {path}"}

    try:
        text = extract_text(path, meta.get("mime_type"))
    except Exception as exc:  # noqa: BLE001
        log.warning("extraction failed for doc %s: %s", doc_id, exc, exc_info=True)
        return {"ok": False, "error": f"text extraction failed: {exc}"}

    chunks = chunk_text(text)
    if not chunks:
        with get_docs_conn(DOCS_DB_PATH) as conn:
            conn.execute(
                "UPDATE documents SET chunk_count = 0, indexed_at = datetime('now') WHERE id = ?",
                (doc_id,),
            )
            conn.commit()
        return {"ok": True, "chunk_count": 0, "embed_failed_count": 0, "note": "no extractable text"}

    failed = 0
    if (os.getenv("YORIK_DB_BACKEND") or "sqlite").lower() == "postgres":
        # Postgres path: pgvector column on docs.document_chunks. No
        # separate vec_chunks virtual table. Mirrors paperless_ingest's
        # _pg_backend branch.
        import math as _m
        from .database_pg import conn_ctx_pg
        def _l2(vec):
            n = _m.sqrt(sum(x * x for x in vec)) or 1.0
            return [x / n for x in vec]
        def _qvec(vec):
            return "[" + ",".join(repr(float(x)) for x in vec) + "]"
        with conn_ctx_pg("docs") as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM document_chunks WHERE doc_id = %s", (doc_id,))
                for i, (body, start, end) in enumerate(chunks):
                    # Postgres TEXT rejects NUL (0x00) bytes; PDFs that
                    # came out of OCR sometimes carry them. SQLite is
                    # lenient — strip here so the Postgres path matches.
                    body = body.replace("\x00", "")
                    try:
                        vec = _l2(embed(body))
                    except EmbeddingError as exc:
                        log.warning("embed failed for doc=%s chunk=%s: %s", doc_id, i, exc)
                        failed += 1
                        cur.execute(
                            "INSERT INTO document_chunks (doc_id, chunk_index, text, char_start, char_end) "
                            "VALUES (%s, %s, %s, %s, %s)",
                            (doc_id, i, body, start, end),
                        )
                        continue
                    cur.execute(
                        "INSERT INTO document_chunks (doc_id, chunk_index, text, char_start, char_end, embedding) "
                        "VALUES (%s, %s, %s, %s, %s, %s::vector)",
                        (doc_id, i, body, start, end, _qvec(vec)),
                    )
                cur.execute(
                    "UPDATE documents SET chunk_count = %s, indexed_at = to_char(now(), 'YYYY-MM-DD HH24:MI:SS') WHERE id = %s",
                    (len(chunks) - failed, doc_id),
                )
        return {"ok": True, "chunk_count": len(chunks) - failed, "embed_failed_count": failed}

    with get_docs_conn(DOCS_DB_PATH) as conn:
        # Wipe existing chunks + vectors for this doc.
        old_ids = [r["id"] for r in conn.execute("SELECT id FROM document_chunks WHERE doc_id = ?", (doc_id,))]
        if old_ids:
            ph = ",".join("?" * len(old_ids))
            conn.execute(f"DELETE FROM vec_chunks WHERE rowid IN ({ph})", old_ids)
            conn.execute("DELETE FROM document_chunks WHERE doc_id = ?", (doc_id,))
        # Insert chunks + their embeddings.
        for i, (body, start, end) in enumerate(chunks):
            cur = conn.execute(
                "INSERT INTO document_chunks (doc_id, chunk_index, text, char_start, char_end) "
                "VALUES (?, ?, ?, ?, ?)",
                (doc_id, i, body, start, end),
            )
            chunk_id = cur.lastrowid
            try:
                vec = embed(body)
            except EmbeddingError as exc:
                log.warning("embed failed for doc=%s chunk=%s: %s", doc_id, i, exc)
                failed += 1
                continue
            conn.execute(
                "INSERT INTO vec_chunks(rowid, embedding) VALUES (?, ?)",
                (chunk_id, sqlite_vec.serialize_float32(vec)),
            )
        conn.execute(
            "UPDATE documents SET chunk_count = ?, indexed_at = datetime('now') WHERE id = ?",
            (len(chunks) - failed, doc_id),
        )
        conn.commit()

    return {"ok": True, "chunk_count": len(chunks) - failed, "embed_failed_count": failed}


# ─── search ────────────────────────────────────────────────────────────────

def search(query: str, k: int = 5, role: Optional[str] = None) -> List[Dict[str, Any]]:
    """Semantic search across all chunks the role may read.

    Returns up to `k` results, each with the chunk text, its document
    metadata, and the cosine-distance score (lower = better match).
    """
    if not query or not query.strip():
        return []
    try:
        qvec = embed(query)
    except EmbeddingError as exc:
        log.warning("query embed failed: %s", exc)
        return [{"error": str(exc), "ok": False}]

    # Over-fetch slightly so the role filter has room to drop hits.
    over_k = max(k * 3, k + 3)
    if (os.getenv("YORIK_DB_BACKEND") or "sqlite").lower() == "postgres":
        # Postgres: pgvector cosine distance on docs.document_chunks.embedding.
        import math as _m
        n = _m.sqrt(sum(x * x for x in qvec)) or 1.0
        qvec_norm = [x / n for x in qvec]
        qlit = "[" + ",".join(repr(float(x)) for x in qvec_norm) + "]"
        from .database_pg import conn_ctx_pg
        with conn_ctx_pg("docs") as conn:
            with conn.cursor() as cur:
                # IVFFLAT defaults probes=1 which silently drops most rows
                # until the index has thousands of vectors. On a fresh
                # install with one chunk, ORDER BY returns nothing even
                # though WHERE embedding IS NOT NULL matches it. Bump
                # probes to the full cluster count so small corpora work.
                cur.execute("SET LOCAL ivfflat.probes = 100")
                cur.execute(
                    "SELECT dc.id AS chunk_id, "
                    "       (dc.embedding <=> %s::vector) AS distance, "
                    "       dc.doc_id, dc.chunk_index, dc.text AS chunk_text, "
                    "       dc.char_start, dc.char_end, "
                    "       d.title AS doc_title, d.mime_type AS doc_mime, "
                    "       d.allowed_roles AS doc_allowed_roles "
                    "FROM document_chunks dc "
                    "JOIN documents d ON d.id = dc.doc_id "
                    "WHERE dc.embedding IS NOT NULL "
                    "ORDER BY dc.embedding <=> %s::vector "
                    "LIMIT %s",
                    (qlit, qlit, over_k),
                )
                # Pool's row_factory already returns HybridRow (dict-like
                # via row['col'] and tuple-like via row[0]). Don't re-wrap.
                rows = list(cur.fetchall())
    else:
        qblob = sqlite_vec.serialize_float32(qvec)
        with get_docs_conn(DOCS_DB_PATH) as conn:
            rows = conn.execute(
                """
                SELECT
                  vc.rowid          AS chunk_id,
                  vc.distance       AS distance,
                  dc.doc_id         AS doc_id,
                  dc.chunk_index    AS chunk_index,
                  dc.text           AS chunk_text,
                  dc.char_start     AS char_start,
                  dc.char_end       AS char_end,
                  d.title           AS doc_title,
                  d.mime_type       AS doc_mime,
                  d.allowed_roles   AS doc_allowed_roles
                FROM vec_chunks vc
                JOIN document_chunks dc ON dc.id = vc.rowid
                JOIN documents d        ON d.id  = dc.doc_id
                WHERE vc.embedding MATCH ? AND k = ?
                ORDER BY vc.distance
                """,
                (qblob, over_k),
            ).fetchall()

    out: List[Dict[str, Any]] = []
    for r in rows:
        if role and role not in ("platform_admin", "admin"):
            allowed = (r["doc_allowed_roles"] or "").split(",")
            if role not in [x.strip() for x in allowed]:
                continue
        out.append({
            "chunk_id":   r["chunk_id"],
            "doc_id":     r["doc_id"],
            "doc_title":  r["doc_title"],
            "doc_mime":   r["doc_mime"],
            "chunk_index": r["chunk_index"],
            "chunk_text": r["chunk_text"],
            "char_start": r["char_start"],
            "char_end":   r["char_end"],
            "distance":   round(float(r["distance"]), 4),
        })
        if len(out) >= k:
            break
    return out
