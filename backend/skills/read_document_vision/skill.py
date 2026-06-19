"""read_document_vision — render PDF pages and let the multimodal LLM read them.

Companion to read_document. Use this when paperless's OCR garbled the
text (read_document returns a high garble ratio) or when the question
needs spatial reasoning the flat OCR text destroys (table rows, columns,
form field positions).

Pipeline:
  1. Resolve PDF source (paperless via /api/documents/{id}/download/, or
     a native upload via documents.path on disk).
  2. Render the requested pages to PNG at 150 dpi via pdftoppm (poppler).
  3. Encode each page as base64 and build an OpenAI multimodal message.
  4. Call the same llm endpoint Yorik uses for everything else — it's
     wired with a Qwen3.5-VL mmproj (see project memory).
  5. Return the model's reply as plain text so the caller can quote it.

Cost: ~2 s per page at 150 dpi on the local 9B + BF16 mmproj setup.
Per-image vision token cost is ~1280 against ctx — pages are capped at
15 to stay comfortable inside the 65k context window.
"""

from __future__ import annotations

import base64
import glob
import logging
import os
import subprocess
import tempfile
from typing import Any, Optional

log = logging.getLogger("yorik.skills.read_document_vision")

MAX_PAGES = 15
RENDER_DPI = 150
LLM_TIMEOUT_S = 240


async def execute(
    ctx,
    doc_id: int,
    pages: str = "1-10",
    question: str = "",
) -> dict[str, Any]:
    try:
        doc_id_int = int(doc_id)
    except (TypeError, ValueError):
        raise ValueError(f"doc_id must be an integer, got {doc_id!r}")
    if doc_id_int <= 0:
        raise ValueError(f"doc_id must be positive, got {doc_id_int}")

    first, last = _parse_page_range(pages)
    if last - first + 1 > MAX_PAGES:
        last = first + MAX_PAGES - 1  # silently cap rather than error

    # 1. Find the PDF on disk or fetch from paperless.
    pdf_path, source, title = _resolve_pdf(doc_id_int)
    if not pdf_path:
        return {
            "ok":    False,
            "doc_id": doc_id_int,
            "error": f"document {doc_id_int} not found (checked native uploads + paperless)",
        }

    # 2. Render to PNGs in a tempdir we clean up afterwards.
    with tempfile.TemporaryDirectory(prefix="ydv-") as tmpdir:
        png_paths = _render_pages(pdf_path, tmpdir, first, last)
        if not png_paths:
            return {
                "ok":     False,
                "doc_id": doc_id_int,
                "error":  f"could not render any pages from {pdf_path} "
                          f"(pdftoppm missing? page range out of bounds?)",
            }

        # 3. Build the multimodal message.
        prompt = _build_prompt(question)
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for p in png_paths:
            try:
                b = base64.b64encode(open(p, "rb").read()).decode()
            except OSError as exc:
                log.warning("read_document_vision: failed to read %s: %s", p, exc)
                continue
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b}"},
            })

        # 4. Dispatch to the LLM. Use the same env-driven config every
        # other in-skill LLM call uses (compose_extract_args,
        # extract_price_table, contact_extractor).
        from backend.agent.llm import LlmClient
        import asyncio
        client = LlmClient(
            model=os.getenv("HOMEOS_MODEL", "qwen3.5-9b"),
            base_url=os.getenv("HOMEOS_LLM_BASE_URL", "http://127.0.0.1:8080/v1"),
            request_timeout=LLM_TIMEOUT_S,
        )
        try:
            result = await asyncio.to_thread(
                client.chat,
                [{"role": "user", "content": content}],
                max_tokens=800,
                temperature=0.1,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("read_document_vision LLM call failed for doc %s", doc_id_int)
            return {
                "ok":      False,
                "doc_id":  doc_id_int,
                "error":   f"LLM vision call failed: {type(exc).__name__}: {exc}",
            }

        reply = (result.get("content") or "").strip()

    return {
        "ok":         True,
        "doc_id":     doc_id_int,
        "source":     source,
        "title":      title,
        "text":       reply,
        "pages_read": len(png_paths),
        "page_range": f"{first}-{first + len(png_paths) - 1}",
    }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _parse_page_range(spec: str) -> tuple[int, int]:
    """Accept '1-10' / '5' / '3-7'. Defaults to 1-10 on garbage."""
    s = (spec or "").strip()
    if "-" in s:
        a, b = s.split("-", 1)
        try:
            return max(1, int(a.strip())), max(1, int(b.strip()))
        except ValueError:
            pass
    try:
        n = int(s)
        return n, n
    except ValueError:
        return 1, 10


def _resolve_pdf(doc_id: int) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Return (pdf_path_or_tempfile, source_label, title).

    Native uploads first — `documents.path` points at the on-disk PDF.
    Paperless second — we fetch via the REST API into a temp file.
    The caller is responsible for cleaning the temp file (we render
    inside a tempdir that gets cleaned anyway, but the PDF itself is
    held in /tmp briefly).
    """
    # Native upload first.
    try:
        from backend.documents import get_document
        doc = get_document(doc_id)
        if doc and doc.get("path"):
            return doc["path"], "native", doc.get("title")
    except Exception:  # noqa: BLE001
        pass

    # Paperless fallback — fetch into a temp file we leak briefly to /tmp.
    # The tempdir cleanup in execute() doesn't reach this path, so we use
    # /tmp directly with a deterministic name; subsequent calls overwrite.
    title = None
    try:
        import requests
        from backend.paperless_ingest import _paperless_settings, _fetch_doc
        s = _paperless_settings()
        if not s.get("api_key"):
            return None, None, None
        # Fetch metadata for the title (best-effort).
        meta = _fetch_doc(doc_id)
        if meta:
            title = meta.get("title")
        # Download the actual PDF.
        url = f"{s['base_url']}/api/documents/{doc_id}/download/"
        r = requests.get(url,
                          headers={"Authorization": f"Token {s['api_key']}"},
                          timeout=20)
        if r.status_code != 200:
            log.warning("paperless download %s returned %s", doc_id, r.status_code)
            return None, None, None
        pdf_path = f"/tmp/ydv-paperless-{doc_id}.pdf"
        with open(pdf_path, "wb") as fh:
            fh.write(r.content)
        return pdf_path, "paperless", title
    except Exception:  # noqa: BLE001
        log.exception("read_document_vision: paperless fetch failed for doc %s", doc_id)
        return None, None, None


def _render_pages(pdf_path: str, tmpdir: str, first: int, last: int) -> list[str]:
    """Render `[first..last]` pages to PNG in `tmpdir`. Returns sorted PNG paths."""
    prefix = os.path.join(tmpdir, "page")
    try:
        result = subprocess.run(
            ["pdftoppm", "-png", "-r", str(RENDER_DPI),
             "-f", str(first), "-l", str(last), pdf_path, prefix],
            capture_output=True, timeout=60,
        )
    except FileNotFoundError:
        log.error("pdftoppm not installed — install poppler-utils")
        return []
    except subprocess.TimeoutExpired:
        log.warning("pdftoppm timed out for %s pages %s-%s", pdf_path, first, last)
        return []
    if result.returncode != 0:
        log.warning("pdftoppm exited %s: %s", result.returncode, result.stderr.decode()[:300])
    return sorted(glob.glob(f"{prefix}-*.png"))


def _build_prompt(question: str) -> str:
    """Default to a structured extraction prompt; if the caller supplied
    a focused question, lead with it and let the model answer that
    directly (much shorter reply, much faster)."""
    q = (question or "").strip()
    if q:
        return (
            f"{q}\n\n"
            "Answer based ONLY on what you see in the attached document pages. "
            "If something is not visible or unclear, say so honestly — do not invent."
        )
    return (
        "Transcribe and summarise the visible content of these document pages. "
        "Be honest about anything unclear or unreadable; do not invent text. "
        "Preserve numbers, dates, IBANs, and Euro amounts verbatim."
    )
