"""read_document — fetch the full extracted text of one indexed doc.

Pair with find_document (which only returns ranked snippets). When the
LLM needs to actually READ a document end-to-end — e.g. to extract
landlord name + contract start date from a Mietvertrag so it can
draft a Mietkündigung — call this with the doc_id.

The text comes from document_chunks (each chunk is a ~500-token slice
of the original; concatenating them in chunk_index order reconstructs
the linear text). Hard cap on output: 40_000 chars (~10k tokens) so a
single fat lease doesn't blow the LLM's context window. The reply
flags `truncated=True` when we hit the cap so the LLM knows.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("yorik.skills.read_document")

MAX_CHARS = 40_000


async def execute(ctx, doc_id: int) -> dict[str, Any]:  # noqa: ARG001
    try:
        doc_id_int = int(doc_id)
    except (TypeError, ValueError):
        raise ValueError(f"doc_id must be an integer, got {doc_id!r}")
    if doc_id_int <= 0:
        raise ValueError(f"doc_id must be positive, got {doc_id_int}")

    from backend.documents import get_document, get_docs_conn, DOCS_DB_PATH

    # Native uploads path: doc_id points at the `documents` table; chunks
    # live in document_chunks. Only commit to this path if the native
    # row HAS chunks — otherwise fall through to Paperless. Zombie rows
    # (write succeeded, indexing crashed) used to win the lookup and
    # the skill returned empty text instead of finding the real doc.
    doc = get_document(doc_id_int)
    if doc:
        doc.pop("path", None)  # never expose on-disk paths to the LLM
        with get_docs_conn(DOCS_DB_PATH) as conn:
            rows = conn.execute(
                "SELECT text FROM document_chunks WHERE doc_id = ? "
                "ORDER BY chunk_index ASC",
                (doc_id_int,),
            ).fetchall()
        if rows:
            return _build_result(doc_id_int, "native",
                                 rows, doc.get("title"), doc.get("mime_type"),
                                 doc.get("bytes"), doc.get("created_at"))

    # Paperless fallback: where 99% of the user's content actually
    # lives. doc_id IS the paperless_doc_id; chunks come from
    # paperless_chunks. Without this the skill returned "not found" for
    # every real document and the LLM kept redirecting to /r/documents
    # as a workaround instead of answering content questions.
    with get_docs_conn(DOCS_DB_PATH) as conn:
        rows = conn.execute(
            "SELECT text FROM paperless_chunks WHERE paperless_doc_id = ? "
            "ORDER BY chunk_index ASC",
            (doc_id_int,),
        ).fetchall()
    if rows:
        title = None
        try:
            from backend.paperless_ingest import _fetch_doc
            meta = _fetch_doc(doc_id_int)
            if meta:
                title = meta.get("title")
        except Exception:
            pass  # title is nice-to-have; text is the load-bearing field
        return _build_result(doc_id_int, "paperless",
                             rows, title, "application/pdf", None, None)

    return {
        "ok":      False,
        "doc_id":  doc_id_int,
        "error":   f"document {doc_id_int} not found",
    }


def _ocr_garble_ratio(text: str) -> float:
    """Heuristic OCR-garble score in [0..1].

    Counts whitespace-separated tokens that look like OCR noise:
      • 1-2 char tokens (real prose has few; OCR letterheads make many),
      • letter+digit mixes ('Ha#deI', 'Ö8B', '8äbklf', 'K4000'),
      • tokens containing special chars not used in real German/English text
        (#, $, %, &, *, \\, <, >, ^, |, ~, `).

    Empirically: clean prose scores < 0.10, printed-letterhead OCR easily
    > 0.30. Used by _build_result to attach a retry-suggesting hint when
    the LLM is reading a doc whose text is mostly unrecoverable noise.
    """
    if not text:
        return 0.0
    tokens = text.split()
    if not tokens:
        return 0.0
    SPECIAL = set("#$%&*+\\<>^|~`")
    bad = 0
    counted = 0
    for raw in tokens:
        t = raw.strip(".,;:!?'\"()[]{}*-_=")
        if not t:
            continue
        counted += 1
        if len(t) <= 2:
            bad += 1
            continue
        if any(c in SPECIAL for c in t):
            bad += 1
            continue
        if any(c.isalpha() for c in t) and any(c.isdigit() for c in t):
            bad += 1
    return bad / counted if counted else 0.0


def _build_result(doc_id: int, source: str, rows,
                  title, mime_type, byte_count, created_at) -> dict:
    full_text = "\n\n".join(r["text"] for r in rows if r["text"])
    truncated = False
    total_chars = len(full_text)
    if total_chars > MAX_CHARS:
        full_text = full_text[:MAX_CHARS]
        truncated = True
    result = {
        "ok":          True,
        "doc_id":      doc_id,
        "source":      source,
        "title":       title,
        "mime_type":   mime_type,
        "bytes":       byte_count,
        "created_at":  created_at,
        "text":        full_text,
        "total_chars": total_chars,
        "truncated":   truncated,
        "chunk_count": len(rows),
    }
    # Garble warning — only when the text is so noisy that values/dates
    # cannot be trusted. Steer the LLM to a sibling doc instead of
    # surrendering ("Die Dokumente sind leider stark beschädigt …").
    garble = _ocr_garble_ratio(full_text)
    if garble > 0.25:
        pct = int(round(garble * 100))
        result["ocr_garble_ratio"] = round(garble, 2)
        result["_llm_hint"] = (
            f"WARNING: text looks OCR-garbled (~{pct}% suspect tokens) — "
            f"amounts and dates from this doc may be unreliable.\n"
            f"Recovery option 1 (preferred for value-seeking): call "
            f"read_document_vision(doc_id={doc_id}, question='<your specific "
            f"question>') — same doc, but the multimodal LLM reads the "
            f"rendered PDF pages directly instead of paperless's OCR text.\n"
            f"Recovery option 2: read_document on a DIFFERENT doc_id from "
            f"the search hits — prefer Vertrag / Kalkulationsblatt / "
            f"Rechnung over Auszahlungsbestätigung."
        )
    return result
