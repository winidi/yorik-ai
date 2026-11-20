"""Short-lived links that show one file without a login.

An outside agent's client (Brepper) has no Yorik session, but the person
using it wants to look at the invoice the agent just found or filed. A tool
puts a link into its card; opening it shows that one file inline for 30
minutes:

    /api/file-links/<token>

The token is HMAC-signed (per-install secret in the credential store) over
who it is for, which file and until when. It names one file of one person
and cannot be turned into another: no session, no listing, no other file.
Anyone holding the link can open that file until it expires, so it goes
only into the card of the person's own conversation.

kinds:
    email_attachment   an attachment of one of the person's emails (fetched
                       from the mail server, as the Email app's preview)
    document           a Paperless document, through the person's own token
    upload             one of the person's own uploads (Yorik's local copy)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import os
from pathlib import Path
import secrets
import time
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

TTL_SECONDS = 30 * 60
PREFIX = "/api/file-links/"
KINDS = ("email_attachment", "document", "upload")
INLINE_TYPES = {"application/pdf", "image/png", "image/jpeg", "image/gif", "image/webp", "text/plain"}
_STORE_KEY = "file_links_secret"
_cached_secret: Optional[bytes] = None

router = APIRouter(tags=["files"])


def _secret() -> bytes:
    """Per-install secret, made on first use and kept in the credential
    store; without a store, per process (old links then stop working)."""
    global _cached_secret
    if _cached_secret:
        return _cached_secret
    fresh = secrets.token_bytes(32)
    try:
        from . import credential_store
        stored = (credential_store.get(_STORE_KEY) or {}).get("secret")
        if stored:
            _cached_secret = base64.urlsafe_b64decode(stored)
            return _cached_secret
        credential_store.put(_STORE_KEY, {"secret": base64.urlsafe_b64encode(fresh).decode("ascii")})
    except Exception:  # noqa: BLE001 — store unavailable: per-process secret
        pass
    _cached_secret = fresh
    return _cached_secret


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def mint(user_id: str, kind: str, item_id: int, ttl: int = TTL_SECONDS, now: Optional[float] = None) -> str:
    """The token for one file of one person."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    body = json.dumps({"u": str(user_id), "k": kind, "i": int(item_id),
                       "e": int((now or time.time()) + ttl)}, separators=(",", ":")).encode()
    sig = hmac.new(_secret(), body, hashlib.sha256).digest()[:18]
    return f"{_b64(body)}.{_b64(sig)}"


def verify(token: str, now: Optional[float] = None) -> Optional[dict[str, Any]]:
    """{"u", "k", "i", "e"} of a good, unexpired token, else None."""
    try:
        body_s, sig_s = token.split(".", 1)
        body = _unb64(body_s)
        if not hmac.compare_digest(hmac.new(_secret(), body, hashlib.sha256).digest()[:18], _unb64(sig_s)):
            return None
        data = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("k") not in KINDS or int(data.get("e", 0)) < (now or time.time()):
        return None
    return data


def base_url() -> str:
    """Where the person's devices reach Yorik: YORIK_PUBLIC_URL, the
    tailnet name, or this machine."""
    try:
        from . import tailscale_local
        url = tailscale_local.base_url()
        if url:
            return url.rstrip("/")
    except Exception:  # noqa: BLE001
        pass
    return f"http://127.0.0.1:{os.getenv('HOMEOS_PORT') or os.getenv('PORT') or 8000}"


def url(user_id: str, kind: str, item_id: int) -> str:
    return f"{base_url()}{PREFIX}{mint(user_id, kind, item_id)}"


def _paperless_document(doc_id: int, user_id: str) -> Optional[dict[str, Any]]:
    from .external_users import get_user_paperless_creds
    import requests
    creds = get_user_paperless_creds(user_id)
    if not creds or not creds.get("api_key"):
        return None
    r = requests.get(f"{creds['base_url'].rstrip('/')}/api/documents/{int(doc_id)}/preview/",
                     headers={"Authorization": f"Token {creds['api_key']}"}, timeout=30)
    if not r.ok:
        return None
    name = "document.pdf"
    disp = r.headers.get("content-disposition") or ""
    if "filename=" in disp:
        name = disp.split("filename=", 1)[1].strip().strip('"') or name
    return {"content": r.content, "mimetype": r.headers.get("content-type") or "application/pdf", "filename": name}


def _upload(doc_id: int, user_id: str) -> Optional[dict[str, Any]]:
    from .documents import get_document, owned_by
    doc = get_document(int(doc_id))
    if not owned_by(doc, user_id) or not doc.get("path") or not Path(doc["path"]).is_file():
        return None
    path = Path(doc["path"])
    return {"content": path.read_bytes(), "mimetype": doc.get("mime_type") or "", "filename": path.name}


def mime_of(blob: dict[str, Any]) -> str:
    """The file's type; a mail server's application/octet-stream for a
    PDF is told by the file name instead."""
    mime = (blob.get("mimetype") or "").split(";")[0].strip().lower()
    if mime in ("", "application/octet-stream", "binary/octet-stream"):
        mime = (mimetypes.guess_type(blob.get("filename") or "")[0] or "application/octet-stream").lower()
    return mime


@router.get(PREFIX + "{token}")
async def open_file_link(token: str):
    import asyncio
    data = verify(token)
    if not data:
        raise HTTPException(404, "this link has expired or is not valid")
    if data["k"] == "email_attachment":
        from . import email_actions
        blob = await asyncio.to_thread(email_actions.fetch_attachment_binary, int(data["i"]), data["u"])
    elif data["k"] == "upload":
        blob = await asyncio.to_thread(_upload, int(data["i"]), data["u"])
    else:
        blob = await asyncio.to_thread(_paperless_document, int(data["i"]), data["u"])
    if not blob:
        raise HTTPException(404, "the file is not available")
    safe = (blob.get("filename") or "file").replace('"', "_")
    mime = mime_of(blob)
    # Shown in place only when it cannot run code on Yorik's origin: a PDF or
    # a plain picture. Anything else (HTML, SVG, office files) downloads.
    how = "inline" if mime in INLINE_TYPES else "attachment"
    return Response(content=blob["content"], media_type=mime,
                    headers={"content-disposition": f'{how}; filename="{safe}"',
                             "cache-control": "private, no-store", "x-content-type-options": "nosniff"})


def _language(user_id: str) -> str:
    try:
        from .database import get_conn
        with get_conn() as conn:
            row = conn.execute("SELECT language FROM user_profiles WHERE id = ?", (user_id,)).fetchone()
        return str((row["language"] if row else "") or "")
    except Exception:  # noqa: BLE001
        return ""


def view_action(user_id: str, kind: str, item_id: int, name: str = "") -> dict[str, str]:
    """A card button for an outside agent's client: opens the file
    ("View" or "View: invoice.pdf")."""
    label = "Anzeigen" if _language(user_id).lower().startswith("de") else "View"
    return {"id": f"view-{kind}-{int(item_id)}", "label": f"{label}: {name}" if name else label,
            "url": url(user_id, kind, item_id)}


def document_url(user_id: str, doc: dict[str, Any]) -> str:
    """The link for a document hit of search_documents / read_document:
    an own upload ("local"/"native") or a Paperless document."""
    kind = "upload" if (doc.get("source") or "paperless") in ("local", "native") else "document"
    return url(user_id, kind, int(doc["doc_id"]))


def for_outside_agent(ctx) -> bool:
    """Cards get these buttons only for an outside agent (an API token,
    e.g. Hermes in Brepper); Yorik's own chat has its own viewers."""
    return str(getattr(ctx, "source", "") or "").startswith("token:")
