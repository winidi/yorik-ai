"""Compose → Paperless save flow.

Takes the body_html the user just edited in the Compose UI, renders it to
PDF via Gotenberg, then uploads to Paperless's REST API. Paperless ingests
the PDF, OCR/text-extracts it, fires the consume webhook → Yorik's
embedding pipeline → the new document is immediately voice-searchable.

That's the platform loop closing: 'invoice Müller for last week' →
draft in Compose → save → next month 'what did I charge Müller'
returns the citation. No manual filing step.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import requests

from .pdf import render_pdf
from ..connectors.paperless import _settings as _paperless_settings

log = logging.getLogger("homeos.compose.save")


def save_to_paperless(body_html: str, *,
                       title: str,
                       tags: Optional[List[str]] = None,
                       correspondent: Optional[str] = None,
                       page_size: str = "A4",
                       margins_mm=(20, 18, 25, 18)) -> Dict[str, Any]:
    """Render the HTML to PDF and upload to Paperless. Returns
    {ok, paperless_task_id?, error?, pdf_bytes}. Paperless's upload
    endpoint returns 200 with a UUID task id even before OCR finishes —
    the doc will appear in Paperless shortly after."""
    pdf_bytes = render_pdf(body_html, page_size=page_size, margins_mm=margins_mm,
                            filename=f"{title}.pdf")
    if not pdf_bytes:
        return {"ok": False, "error": "Gotenberg render failed"}

    s = _paperless_settings()
    if not s.get("api_key"):
        return {"ok": False, "error": "Paperless not configured (no api_key)"}
    headers = {"Authorization": f"Token {s['api_key']}"}
    files = {"document": (f"{title}.pdf", pdf_bytes, "application/pdf")}
    data: Dict[str, Any] = {"title": title}
    if tags:
        # Paperless API accepts repeated `tags` form fields; use a list.
        # Tag names are looked up / created via /api/tags/ first.
        tag_ids = _ensure_tag_ids(s, tags)
        for tid in tag_ids:
            data.setdefault("tags", []).append(tid)
    if correspondent:
        cid = _ensure_correspondent_id(s, correspondent)
        if cid is not None:
            data["correspondent"] = cid
    try:
        r = requests.post(
            f"{s['base_url'].rstrip('/')}/api/documents/post_document/",
            headers=headers, files=files, data=data, timeout=20,
        )
        if not r.ok:
            return {"ok": False, "error": f"paperless HTTP {r.status_code}: {r.text[:200]}"}
        # Return the actual bytes so the caller can compute a SHA256
        # for the GoBD audit trail. The size is also exposed for UI.
        return {"ok": True, "paperless_task_id": r.text.strip().strip('"'),
                "pdf_size": len(pdf_bytes), "pdf_bytes": pdf_bytes}
    except requests.RequestException as exc:
        return {"ok": False, "error": f"paperless unreachable: {exc}"}


def _ensure_tag_ids(s: Dict[str, Any], names: List[str]) -> List[int]:
    """Look up each tag by name, create if missing. Returns list of ids."""
    out: List[int] = []
    headers = {"Authorization": f"Token {s['api_key']}", "Accept": "application/json"}
    for name in names:
        try:
            r = requests.get(f"{s['base_url']}/api/tags/", headers=headers,
                             params={"name__iexact": name}, timeout=8)
            r.raise_for_status()
            results = (r.json() or {}).get("results") or []
            if results:
                out.append(results[0]["id"])
            else:
                cr = requests.post(f"{s['base_url']}/api/tags/", headers=headers,
                                   json={"name": name}, timeout=8)
                if cr.ok:
                    out.append(cr.json()["id"])
        except requests.RequestException as exc:
            log.warning("tag '%s' ensure failed: %s", name, exc)
    return out


def _ensure_correspondent_id(s: Dict[str, Any], name: str) -> Optional[int]:
    headers = {"Authorization": f"Token {s['api_key']}", "Accept": "application/json"}
    try:
        r = requests.get(f"{s['base_url']}/api/correspondents/", headers=headers,
                         params={"name__iexact": name}, timeout=8)
        r.raise_for_status()
        results = (r.json() or {}).get("results") or []
        if results:
            return results[0]["id"]
        cr = requests.post(f"{s['base_url']}/api/correspondents/", headers=headers,
                           json={"name": name}, timeout=8)
        if cr.ok:
            return cr.json()["id"]
    except requests.RequestException as exc:
        log.warning("correspondent '%s' ensure failed: %s", name, exc)
    return None
