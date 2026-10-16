"""Files the chat may hand to a mail or a WhatsApp message.

Three kinds of files exist in Yorik: a chat attachment (uploaded in the
conversation), an archive document (Paperless) and a photo (Immich).
The chat's send paths used to know one of them at a time: prepare_email
took one chat file *or* one document and never a photo, whatsapp_draft
took none. This module resolves any mix of the three into the one shape
the Email Composer and the WhatsApp card consume::

    {"url": "/api/photos/<id>/raw", "filename": "IMG_1.jpg", "mimetype": "image/jpeg",
     "kind": "photo", "ref": "<id>"}

Every lookup runs as the person (their chat row, their Paperless token,
their Immich key), so a file they may not see does not resolve. The same
rule holds for `fetch_bytes`, which the WhatsApp media send uses server-
side: it reads the file as the person and never with an admin key.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("yorik.send_attachments")

_ASSET_ID = re.compile(r"^[A-Za-z0-9._-]+$")


def _ids(value: Any) -> List[Any]:
    """Accept one id, a list of ids, or nothing."""
    if value in (None, "", 0, []):
        return []
    if isinstance(value, (list, tuple, set)):
        return [v for v in value if v not in (None, "", 0)]
    return [value]


def resolve(user_id: str, *, attachment_ids: Any = None, paperless_doc_ids: Any = None,
            photo_ids: Any = None) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Turn ids into send-ready entries. Returns (entries, problems); a
    problem is one short sentence per file that could not be resolved,
    meant for the model's hint."""
    entries: List[Dict[str, Any]] = []
    problems: List[str] = []

    for att in _ids(attachment_ids):
        from backend import chat_attachments as A
        try:
            row = A.get(int(att), str(user_id))
        except (TypeError, ValueError):
            row = None
        if not row:
            problems.append(f"attachment #{att}: no attachment with this number for this user")
            continue
        entries.append({"url": f"/api/chat/attachments/{row['id']}/raw", "filename": row["filename"],
                        "mimetype": row["mime_type"] or "application/octet-stream",
                        "kind": "attachment", "ref": str(row["id"])})

    for doc in _ids(paperless_doc_ids):
        from backend.paperless_ingest import _fetch_doc, user_creds
        creds = user_creds(user_id)
        try:
            meta = _fetch_doc(int(doc), creds_override=creds) if creds else None
        except (TypeError, ValueError):
            meta = None
        if not meta:
            problems.append(f"document {doc}: not available to this user (search again with search_documents)")
            continue
        filename = meta.get("original_file_name") or f"{meta.get('title') or 'Dokument'}.pdf"
        entries.append({"url": f"/paperless/api/documents/{int(doc)}/download/", "filename": filename,
                        "mimetype": meta.get("mime_type") or "application/pdf",
                        "kind": "document", "ref": str(int(doc))})

    for pid in _ids(photo_ids):
        meta = photo_meta(user_id, str(pid))
        if not meta:
            problems.append(f"photo {pid}: not in this user's photo library (use the id from find_photo)")
            continue
        entries.append({"url": f"/api/photos/{meta['id']}/raw", "filename": meta["filename"],
                        "mimetype": meta["mimetype"], "kind": "photo", "ref": meta["id"]})

    return entries, problems


def _immich_creds(user_id: str) -> Dict[str, str]:
    from backend import external_users
    creds = external_users.get_user_immich_creds(str(user_id)) or {}
    if not (creds.get("base_url") and creds.get("api_key")):
        return {}
    return {"base_url": str(creds["base_url"]).rstrip("/"), "api_key": str(creds["api_key"])}


def photo_meta(user_id: str, asset_id: str) -> Optional[Dict[str, str]]:
    """Immich asset metadata as the person: id, filename, mimetype. None
    when the asset is not theirs or Immich is not set up for them."""
    if not _ASSET_ID.match(asset_id or ""):
        return None
    creds = _immich_creds(user_id)
    if not creds:
        return None
    import httpx
    try:
        r = httpx.get(f"{creds['base_url']}/api/assets/{asset_id}",
                      headers={"x-api-key": creds["api_key"]}, timeout=10.0)
    except Exception as exc:  # noqa: BLE001
        log.warning("immich asset lookup failed for %s: %s", asset_id, exc)
        return None
    if r.status_code != 200:
        return None
    j = r.json() if r.content else {}
    filename = j.get("originalFileName") or f"{asset_id[:8]}.jpg"
    mimetype = j.get("originalMimeType") or _guess_mime(filename) or "image/jpeg"
    return {"id": asset_id, "filename": filename, "mimetype": mimetype}


def _guess_mime(filename: str) -> Optional[str]:
    import mimetypes
    return mimetypes.guess_type(filename)[0]


def url_allowed(url: str) -> bool:
    """Only Yorik's own same-origin proxies; nothing external."""
    return (url or "").startswith(("/api/chat/attachments/", "/api/photos/", "/paperless/api/documents/"))


def fetch_bytes(user_id: str, url: str) -> Tuple[bytes, str, str]:
    """Read one of the three kinds of files as the person. Returns
    (data, mimetype, filename). Raises ValueError with a short reason."""
    url = (url or "").strip()
    if not url_allowed(url):
        raise ValueError("this file cannot be sent from here")

    m = re.match(r"^/api/chat/attachments/(\d+)/raw$", url)
    if m:
        from backend import chat_attachments as A
        row = A.get(int(m.group(1)), str(user_id))
        if not row or not row.get("path") or not Path(row["path"]).exists():
            raise ValueError("attachment not found")
        return Path(row["path"]).read_bytes(), row["mime_type"] or "application/octet-stream", row["filename"]

    m = re.match(r"^/paperless/api/documents/(\d+)/download/?$", url)
    if m:
        from backend.paperless_ingest import user_creds
        creds = user_creds(user_id)
        if not creds:
            raise ValueError("no document access for this user")
        import httpx
        base = str(creds["base_url"]).rstrip("/")
        r = httpx.get(f"{base}/api/documents/{int(m.group(1))}/download/",
                      headers={"Authorization": f"Token {creds['api_key']}"}, timeout=30.0)
        if r.status_code != 200:
            raise ValueError("document not available to this user")
        filename = _filename_from_disposition(r.headers.get("content-disposition", "")) or f"dokument-{m.group(1)}.pdf"
        return r.content, r.headers.get("content-type", "application/pdf").split(";")[0].strip(), filename

    m = re.match(r"^/api/photos/([A-Za-z0-9._-]+)/raw$", url)
    if m:
        creds = _immich_creds(user_id)
        if not creds:
            raise ValueError("no photo access for this user")
        import httpx
        r = httpx.get(f"{creds['base_url']}/api/assets/{m.group(1)}/original",
                      headers={"x-api-key": creds["api_key"]}, timeout=30.0)
        if r.status_code != 200:
            raise ValueError("photo not available to this user")
        filename = _filename_from_disposition(r.headers.get("content-disposition", "")) or f"{m.group(1)[:8]}.jpg"
        return r.content, r.headers.get("content-type", "image/jpeg").split(";")[0].strip(), filename

    raise ValueError("this file cannot be sent from here")


def _filename_from_disposition(cd: str) -> Optional[str]:
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd or "")
    if m:
        from urllib.parse import unquote
        return unquote(m.group(1))
    m = re.search(r'filename="?([^";]+)"?', cd or "")
    return m.group(1) if m else None
