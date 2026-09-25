"""set_document_visibility skill — chat-driven Paperless visibility change.

Wraps backend.paperless_visibility.change_document_visibility with the
same owner-check the HTTP route applies. Members can only change docs
they own; admin bypasses.
"""
from __future__ import annotations
from typing import Any

import requests


_VALID = ("private", "parents", "business", "shared")


async def execute(
    ctx,
    document_id: int,
    visibility: str,
) -> dict[str, Any]:
    if not isinstance(document_id, int) or document_id <= 0:
        raise ValueError("document_id must be a positive integer")
    vis = (visibility or "").strip().lower()
    if vis not in _VALID:
        raise ValueError(f"visibility must be one of {_VALID}; got {visibility!r}")

    from backend.connectors.paperless import _settings as _ps
    s = _ps()
    if not s.get("api_key"):
        raise RuntimeError("Paperless is not configured on this Yorik instance")
    base = (s.get("base_url") or "http://localhost:8010").rstrip("/")

    # Owner check — mirror the /api/documents/-N/visibility HTTP route:
    # the owner decides, nobody else (audit 2026-09-22, 1.17, 1.18).
    user_id = getattr(ctx, "user_id", None)
    me_paperless_uid = None
    if user_id is not None:
        from backend.database import get_conn
        with get_conn() as conn:
            prow = conn.execute("SELECT paperless_user_id FROM user_profiles WHERE id = ?", (user_id,)).fetchone()
        me_paperless_uid = int(prow["paperless_user_id"]) if prow and prow["paperless_user_id"] else None

    # Look up the doc's owner + title from Paperless.
    try:
        r = requests.get(
            f"{base}/api/documents/{int(document_id)}/",
            headers={"Authorization": f"Token {s['api_key']}"},
            timeout=10,
        )
    except Exception as exc:
        raise RuntimeError(f"Paperless lookup failed: {exc}")
    if not r.ok:
        raise ValueError(f"document {document_id} not found in Paperless")
    body = r.json()
    owner = body.get("owner")
    title = body.get("title") or f"document {document_id}"

    if me_paperless_uid is None or owner != me_paperless_uid:
        from backend.calendars import RowOwnerPermissionError
        # No title: the lookup ran with the admin token, so the asker
        # may not even see this document (audit 2026-09-25, L13).
        raise RowOwnerPermissionError(
            f"only the document's owner can change its visibility — "
            f"document {int(document_id)} is not yours."
        )

    from backend import paperless_visibility as _pv
    result = _pv.change_document_visibility(int(document_id), vis)
    if not result.get("ok"):
        raise RuntimeError(
            f"Paperless visibility update failed: {result.get('error')}"
        )

    from backend.ui_tools import _append
    _append({
        "type":         "refresh_data",
        "table":        "documents",
        "highlight_id": int(document_id),
        "reason":       f"visibility set to {vis}: {title}",
    })

    return {
        "document_id": int(document_id),
        "visibility":  vis,
        "title":       title,
        "tag_ids":     result.get("tag_ids") or [],
    }
