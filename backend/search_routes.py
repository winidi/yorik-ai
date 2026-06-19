"""Universal search — cross-channel query across email, WhatsApp,
Paperless, Immich, and calendar.

Each source returns up to 5 hits, all in parallel via asyncio.gather.
Total budget is ~500ms — slow sources (Immich CLIP can be 1-2s)
don't block the fast ones.

Source results share a common shape so the frontend can render them
in a uniform list:

    {
        "source": "email" | "whatsapp" | "paperless" | "immich" | "calendar",
        "id": <opaque id used to deep-link>,
        "title": <main label, e.g. email subject>,
        "subtitle": <secondary label, e.g. sender name>,
        "snippet": <body preview, ≤200 chars>,
        "timestamp": <ISO or epoch, when this thing happened>,
        "navigate_to": <relative URL the UI should open on click>,
        "thumbnail_url": <optional, for photos>,
    }
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from .auth_sessions import current_user
from .database import get_conn

log = logging.getLogger("yorik.search")

router = APIRouter(prefix="/api", tags=["search"])

PER_SOURCE_LIMIT = 5
TOTAL_BUDGET_S = 4.0  # hard ceiling — Immich CLIP is the slow one


@router.get("/search")
async def universal_search(q: str = Query(..., min_length=2),
                            user: dict = Depends(current_user)) -> dict[str, Any]:
    """Fan out a query across every channel the user has, return
    grouped results. Single-shot — caller decides how to render."""
    user_id = user["id"]

    tasks = {
        "email":     _search_email(q, user_id),
        "whatsapp":  _search_whatsapp(q, user_id),
        "paperless": asyncio.to_thread(_search_paperless, q, user_id),
        "immich":    asyncio.to_thread(_search_immich, q, user_id),
        "calendar":  _search_calendar(q, user_id),
    }
    # Apply a hard deadline so the slow sources don't block the UI.
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*tasks.values(), return_exceptions=True),
            timeout=TOTAL_BUDGET_S,
        )
    except asyncio.TimeoutError:
        results = []  # everything past deadline gets dropped silently

    out: dict[str, list] = {}
    total = 0
    for source_name, result in zip(tasks.keys(), results):
        if isinstance(result, Exception):
            log.debug("search source %s raised: %s", source_name, result)
            out[source_name] = []
            continue
        # Cap to PER_SOURCE_LIMIT in case a helper returned more.
        hits = (result or [])[:PER_SOURCE_LIMIT]
        out[source_name] = hits
        total += len(hits)
    return {"query": q, "total": total, "results": out}


# ───────────────────────── email ────────────────────────────────────

async def _search_email(q: str, user_id: str) -> list[dict[str, Any]]:
    """FTS5 across email_messages_fts."""
    terms = [t for t in q.split() if t]
    if not terms:
        return []
    match = " ".join(f'"{t.replace(chr(34), "")}"*' for t in terms)
    with get_conn() as conn:
        try:
            rows = conn.execute(
                "SELECT m.id, m.subject, m.from_name, m.from_email, m.snippet, "
                "       m.date_received "
                "FROM email_messages_fts f JOIN email_messages m ON m.rowid=f.rowid "
                "WHERE f MATCH ? AND m.owner_user_id=? "
                "ORDER BY m.date_received DESC LIMIT ?",
                (match, user_id, PER_SOURCE_LIMIT),
            ).fetchall()
        except Exception:
            return []
    return [{
        "source":      "email",
        "id":          r["id"],
        "title":       r["subject"] or "(no subject)",
        "subtitle":    r["from_name"] or r["from_email"],
        "snippet":     r["snippet"] or "",
        "timestamp":   r["date_received"],
        "navigate_to": f"/r/email?open={r['id']}",
    } for r in rows]


# ───────────────────────── WhatsApp ─────────────────────────────────

async def _search_whatsapp(q: str, user_id: str) -> list[dict[str, Any]]:
    """FTS5 across wa_messages_fts. JID gets resolved to chat name."""
    terms = [t for t in q.split() if t]
    if not terms:
        return []
    match = " OR ".join(t for t in terms if len(t) > 1) or terms[0]
    with get_conn() as conn:
        try:
            rows = conn.execute(
                "SELECT m.msg_id, m.chat_jid, m.text, m.transcript, m.push_name, "
                "       m.timestamp, c.name AS chat_name "
                "FROM wa_messages_fts f JOIN wa_messages m ON m.rowid=f.rowid "
                "LEFT JOIN wa_chats c ON c.jid=m.chat_jid "
                "WHERE f.text MATCH ? AND m.owner_user_id=? "
                "ORDER BY m.timestamp DESC LIMIT ?",
                (match, user_id, PER_SOURCE_LIMIT),
            ).fetchall()
        except Exception:
            return []
    out = []
    for r in rows:
        text = r["text"] or r["transcript"] or ""
        chat = r["chat_name"] or (r["chat_jid"].split("@")[0] if r["chat_jid"] else "?")
        out.append({
            "source":      "whatsapp",
            "id":          r["msg_id"],
            "title":       chat,
            "subtitle":    r["push_name"] or "",
            "snippet":     text[:200],
            "timestamp":   r["timestamp"],
            # Vanilla URL — WhatsApp is still a vanilla app for now.
            "navigate_to": f"/whatsapp?chat={r['chat_jid']}",
        })
    return out


# ───────────────────────── Paperless ────────────────────────────────

def _search_paperless(q: str, user_id: str) -> list[dict[str, Any]]:
    """Semantic search through paperless_ingest with the user's per-user
    Paperless token (so Anna's search only sees Anna's docs)."""
    try:
        from . import paperless_ingest
        from .external_users import get_user_paperless_creds
        creds = get_user_paperless_creds(user_id)
        hits = paperless_ingest.search(q, k=PER_SOURCE_LIMIT, creds_override=creds)
    except Exception:
        return []
    return [{
        "source":      "paperless",
        "id":          h.get("paperless_doc_id"),
        "title":       h.get("doc_title") or "(untitled doc)",
        "subtitle":    h.get("correspondent") or "",
        "snippet":     (h.get("text") or "")[:200],
        "timestamp":   h.get("doc_date"),
        "navigate_to": h.get("doc_url") or "/docs",
        "thumbnail_url": h.get("preview_url"),
    } for h in (hits or [])]


# ───────────────────────── Immich ───────────────────────────────────

def _search_immich(q: str, user_id: str) -> list[dict[str, Any]]:
    try:
        from .connectors.immich import immich
        from .external_users import get_user_immich_creds
        creds = get_user_immich_creds(user_id)
        result = immich(op="search", query=q, take_count=PER_SOURCE_LIMIT,
                         creds_override=creds)
        photos = result.get("photos") or []
    except Exception:
        return []
    return [{
        "source":      "immich",
        "id":          p.get("id"),
        "title":       p.get("original_name") or "Photo",
        "subtitle":    "",
        "snippet":     "",
        "timestamp":   p.get("taken_at"),
        "navigate_to": p.get("view_url") or "/photos",
        "thumbnail_url": p.get("thumbnail_url"),
    } for p in photos]


# ───────────────────────── calendar ─────────────────────────────────

async def _search_calendar(q: str, user_id: str) -> list[dict[str, Any]]:
    """No FTS on events yet; LIKE match on title + notes + person.
    Cheap enough at <1000 events; revisit when calendars grow."""
    needle = f"%{q.lower()}%"
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, title, starts_at, ends_at, person, notes "
            "FROM events "
            "WHERE LOWER(title) LIKE ? OR LOWER(COALESCE(notes,'')) LIKE ? "
            "      OR LOWER(COALESCE(person,'')) LIKE ? "
            "ORDER BY starts_at DESC LIMIT ?",
            (needle, needle, needle, PER_SOURCE_LIMIT),
        ).fetchall()
    return [{
        "source":      "calendar",
        "id":          r["id"],
        "title":       r["title"],
        "subtitle":    r["person"] or "",
        "snippet":     (r["notes"] or "")[:200],
        "timestamp":   r["starts_at"],
        "navigate_to": f"/calendar?event={r['id']}",
    } for r in rows]
