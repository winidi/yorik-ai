"""Universal search — one query across email, WhatsApp, Paperless,
Immich, calendar, tasks, contacts, recordings and drafts.

Each source returns up to 5 hits, all in parallel via asyncio.gather.
Total budget is ~500ms — slow sources (Immich CLIP can be 1-2s)
don't block the fast ones.

Sources with rows in Yorik's own database are hybrid: keyword hits
first, then hits by meaning from the semantic index
(backend/search_index.py), both behind the same visibility rule the
app applies (owner, spaces, row shares — no admin exception).

Source results share a common shape so the frontend can render them
in a uniform list:

    {
        "source": "email" | "whatsapp" | "paperless" | "immich" | "calendar"
                  | "tasks" | "contacts" | "recordings" | "drafts",
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
import re
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from .auth_sessions import current_user
from .database import get_conn

log = logging.getLogger("yorik.search")


def _build_tsquery(query: str) -> Optional[str]:
    """Turn a user query like "Müller invoice 2025" into a Postgres
    tsquery string: tokens AND-joined with prefix matching ("müller &
    invoice:* & 2025:*"). Strips characters that have special meaning
    in tsquery syntax so a stray `:` or `&` from the user doesn't blow
    up the planner. Returns None when no usable tokens remain."""
    raw = [t for t in (query or "").split() if t]
    cleaned = [re.sub(r"[^\w\-]", "", t, flags=re.UNICODE) for t in raw]
    cleaned = [t for t in cleaned if t]
    if not cleaned:
        return None
    return " & ".join(f"{t}:*" for t in cleaned)

router = APIRouter(prefix="/api", tags=["search"])

PER_SOURCE_LIMIT = 5
TOTAL_BUDGET_S = 4.0  # hard ceiling — Immich CLIP is the slow one


@router.get("/search")
async def universal_search(q: str = Query(..., min_length=2),
                            user: dict = Depends(current_user)) -> dict[str, Any]:
    """Fan out a query across every channel the user has, return
    grouped results. Single-shot — caller decides how to render."""
    user_id = user["id"]
    role = user.get("role")
    # One embedding of the query serves every hybrid source.
    from . import search_index
    qvec = await asyncio.to_thread(search_index.embed_query, q)

    tasks = {
        "email":      asyncio.to_thread(_search_email, q, user_id, qvec),
        "whatsapp":   asyncio.to_thread(_search_whatsapp, q, user_id, qvec),
        "paperless":  asyncio.to_thread(_search_paperless, q, user_id),
        "immich":     asyncio.to_thread(_search_immich, q, user_id),
        "calendar":   asyncio.to_thread(_search_calendar, q, user_id, role, qvec),
        "tasks":      asyncio.to_thread(_search_tasks, q, user_id, role, qvec),
        "contacts":   asyncio.to_thread(_search_contacts, q, user_id, role, qvec),
        "recordings": asyncio.to_thread(_search_recordings, q, user_id, role, qvec),
        "drafts":     asyncio.to_thread(_search_drafts, q, user_id, qvec),
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

def _hybrid(*, source: str, table: str, columns: str, visible: tuple[str, list],
            keyword: Optional[tuple[str, list]], order: str, qvec: Optional[str]) -> list[dict[str, Any]]:
    """Keyword hits, then hits by meaning, one row each, for one table.
    `visible` and `keyword` are (sql, params) over the table's own name.
    Rows found by meaning carry `_snippet`, the piece of text that matched."""
    vis_sql, vis_params = visible
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    with get_conn() as conn:
        if keyword:
            kw_sql, kw_params = keyword
            try:
                for r in conn.execute(
                    f"SELECT {columns} FROM {table} WHERE ({kw_sql}) AND ({vis_sql}) "
                    f"ORDER BY {order} LIMIT ?",
                    (*kw_params, *vis_params, PER_SOURCE_LIMIT),
                ).fetchall():
                    rows.append(dict(r)); seen.add(int(r["id"]))
            except Exception as exc:  # noqa: BLE001
                log.warning("universal-search %s keyword branch failed: %s", source, exc)
        if qvec and len(rows) < PER_SOURCE_LIMIT:
            from . import search_index
            limit = search_index.max_distance()
            try:
                for r in conn.execute(
                    f"SELECT {columns}, sc.text AS _snippet, (sc.embedding <=> ?::vector) AS _distance "
                    f"FROM search_chunks sc JOIN {table} ON {table}.id = sc.row_id "
                    f"WHERE sc.source = ? AND sc.model = ? AND sc.embedding IS NOT NULL AND ({vis_sql}) "
                    f"ORDER BY _distance LIMIT ?",
                    (qvec, source, search_index.model_tag(), *vis_params, PER_SOURCE_LIMIT * 4),
                ).fetchall():
                    if r["_distance"] is None or r["_distance"] > limit:
                        break
                    if int(r["id"]) in seen:
                        continue
                    rows.append(dict(r)); seen.add(int(r["id"]))
                    if len(rows) >= PER_SOURCE_LIMIT:
                        break
            except Exception as exc:  # noqa: BLE001
                log.warning("universal-search %s semantic branch failed: %s", source, exc)
    return rows


def _like(q: str, *cols: str) -> tuple[str, list]:
    """Every word of the query in any of the columns."""
    words = [w for w in q.lower().split() if w][:6] or [q.lower()]
    hay = " || ' ' || ".join(f"LOWER(COALESCE({c}, ''))" for c in cols)
    return " AND ".join(f"({hay}) LIKE ?" for _ in words), [f"%{w}%" for w in words]


def _search_email(q: str, user_id: str, qvec: Optional[str] = None) -> list[dict[str, Any]]:
    """tsvector over email_messages.search_tsv, then the semantic index."""
    tsq = _build_tsquery(q)
    rows = _hybrid(
        source="email", table="email_messages",
        columns="email_messages.id, subject, from_name, from_email, snippet, date_received",
        visible=("email_messages.owner_user_id = ?", [user_id]),
        keyword=("email_messages.search_tsv @@ to_tsquery('simple', ?)", [tsq]) if tsq else None,
        order="date_received DESC NULLS LAST", qvec=qvec,
    )
    return [{
        "source":      "email",
        "id":          r["id"],
        "title":       r["subject"] or "(no subject)",
        "subtitle":    r["from_name"] or r["from_email"],
        "snippet":     (r.get("_snippet") or r["snippet"] or "")[:200],
        "timestamp":   r["date_received"],
        # Mig 124: react app lives at /r/email; ?msg=<id> is the deep-
        # link the EmailApp parses on mount.
        "navigate_to": f"/r/email?msg={r['id']}",
    } for r in rows]


# ───────────────────────── WhatsApp ─────────────────────────────────

def _search_whatsapp(q: str, user_id: str, qvec: Optional[str] = None) -> list[dict[str, Any]]:
    """tsvector over wa_messages.search_tsv, then the semantic index."""
    tsq = _build_tsquery(q)
    rows = _hybrid(
        source="whatsapp", table="wa_messages",
        columns="wa_messages.id, wa_messages.chat_jid, wa_messages.text, wa_messages.transcript, "
                "wa_messages.push_name, wa_messages.timestamp, "
                "(SELECT c.name FROM wa_chats c WHERE c.jid = wa_messages.chat_jid LIMIT 1) AS chat_name",
        visible=("wa_messages.owner_user_id = ?", [user_id]),
        keyword=("wa_messages.search_tsv @@ to_tsquery('simple', ?)", [tsq]) if tsq else None,
        order="wa_messages.timestamp DESC", qvec=qvec,
    )
    out = []
    for r in rows:
        text = r["text"] or r["transcript"] or ""
        chat = r["chat_name"] or (r["chat_jid"].split("@")[0] if r["chat_jid"] else "?")
        out.append({
            "source":      "whatsapp",
            "id":          r["id"],
            "title":       chat,
            "subtitle":    r["push_name"] or "",
            "snippet":     text[:200],
            "timestamp":   r["timestamp"],
            "navigate_to": f"/r/whatsapp?chat={r['chat_jid']}",
        })
    return out


# ───────────────────────── Paperless ────────────────────────────────

# Cosine-distance threshold for surfacing paperless hits in
# universal search. paperless_ingest.search uses nearest-neighbour
# without a cutoff, so even when nothing in the corpus is actually
# related to the query it returns the K closest documents. For a
# user-facing search panel that's a noise factory — empirically the
# closest match for unrelated queries lands at distance 0.7-0.9.
# 0.55 keeps the "broadly the same topic" matches (~50° angle or
# cleaner) and drops everything weaker.
#
# Other callers of paperless_ingest.search (agent RAG context,
# explicit /docs search) still see all hits — only the universal
# /api/search panel gates by relevance.
_PAPERLESS_MAX_DISTANCE = 0.55


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
    relevant = [
        h for h in (hits or [])
        if h.get("distance") is not None and h["distance"] <= _PAPERLESS_MAX_DISTANCE
    ]
    return [{
        "source":      "paperless",
        "id":          h.get("paperless_doc_id"),
        "title":       h.get("doc_title") or "(untitled doc)",
        "subtitle":    h.get("correspondent") or "",
        "snippet":     (h.get("text") or "")[:200],
        "timestamp":   h.get("doc_date"),
        "navigate_to": h.get("doc_url") or "/docs",
        "thumbnail_url": h.get("preview_url"),
    } for h in relevant]


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

def _search_calendar(q: str, user_id: str, role: Optional[str] = None,
                     qvec: Optional[str] = None) -> list[dict[str, Any]]:
    """Events the person can see in the calendar, nothing else: the
    calendar's own filter, and another person's private event (shown
    there as "Busy") is not found at all."""
    from . import calendars as _cal
    vis_sql, vis_params = _cal.visible_event_filter(user_id, role or "")
    rows = _hybrid(
        source="events", table="events",
        columns="events.id, title, starts_at, ends_at, person, notes, location",
        visible=(f"{vis_sql} AND (events.visibility IS DISTINCT FROM 'private' OR events.owner_user_id = ?)",
                 [*vis_params, user_id]),
        keyword=_like(q, "events.title", "events.notes", "events.person", "events.location"),
        order="starts_at DESC", qvec=qvec,
    )
    return [{
        "source":      "calendar",
        "id":          r["id"],
        "title":       r["title"],
        "subtitle":    r["person"] or r["location"] or "",
        "snippet":     (r["notes"] or "")[:200],
        "timestamp":   r["starts_at"],
        "navigate_to": f"/r/calendar?event={r['id']}",
    } for r in rows]


# ───────────────────────── tasks ────────────────────────────────────

def _search_tasks(q: str, user_id: str, role: Optional[str] = None,
                  qvec: Optional[str] = None) -> list[dict[str, Any]]:
    from . import spaces as _sp
    rows = _hybrid(
        source="tasks", table="tasks",
        columns="tasks.id, title, notes, due_date, done, person",
        visible=_sp.row_filter(user_id, role, "tasks"),
        keyword=_like(q, "tasks.title", "tasks.notes", "tasks.person"),
        order="done ASC, COALESCE(due_date, '9999') ASC, tasks.id DESC", qvec=qvec,
    )
    return [{
        "source":      "tasks",
        "id":          r["id"],
        "title":       r["title"],
        "subtitle":    ("done" if r["done"] else "open") + (f" · {r['person']}" if r["person"] else ""),
        "snippet":     (r["notes"] or "")[:200],
        "timestamp":   r["due_date"],
        "navigate_to": f"/r/tasks?task={r['id']}",
    } for r in rows]


# ───────────────────────── contacts ─────────────────────────────────

def _search_contacts(q: str, user_id: str, role: Optional[str] = None,
                     qvec: Optional[str] = None) -> list[dict[str, Any]]:
    from . import spaces as _sp
    vis_sql, vis_params = _sp.row_filter(user_id, role, "contacts")
    rows = _hybrid(
        source="contacts", table="contacts",
        columns="contacts.id, display_name, relation, role, notes, last_interaction_at",
        visible=(f"{vis_sql} AND contacts.status = 'active' AND contacts.merged_into_id IS NULL", vis_params),
        keyword=_like(q, "contacts.display_name", "contacts.aliases", "contacts.legal_name",
                      "contacts.notes", "contacts.tags"),
        order="COALESCE(last_interaction_at, '') DESC, contacts.id DESC", qvec=qvec,
    )
    return [{
        "source":      "contacts",
        "id":          r["id"],
        "title":       r["display_name"],
        "subtitle":    r["relation"] or r["role"] or "",
        "snippet":     (r["notes"] or "")[:200],
        "timestamp":   r["last_interaction_at"],
        "navigate_to": f"/r/contacts?contact={r['id']}",
    } for r in rows]


# ───────────────────────── recordings ───────────────────────────────

def _search_recordings(q: str, user_id: str, role: Optional[str] = None,
                       qvec: Optional[str] = None) -> list[dict[str, Any]]:
    """Title, report and transcript of the recordings the person was
    part of. The keyword branch reads the transcript as well."""
    rows = _recordings_rows(q, user_id, role, qvec)
    return [{
        "source":      "recordings",
        "id":          r["id"],
        "title":       r["title"] or "Recording",
        "subtitle":    r["kind"] or "",
        "snippet":     (r.get("_snippet") or r.get("seg_hit") or "")[:200],
        "timestamp":   r["started_at"],
        "navigate_to": f"/r/recordings/{r['id']}",
    } for r in rows]


def _recordings_rows(q: str, user_id: str, role: Optional[str], qvec: Optional[str]) -> list[dict[str, Any]]:
    from . import spaces as _sp
    like_sql, like_params = _like(q, "recordings.title", "recordings.report_json")
    words = [w for w in q.lower().split() if w][:6] or [q.lower()]
    seg_sql = " AND ".join("LOWER(s.text) LIKE ?" for _ in words)
    seg_params = [f"%{w}%" for w in words]
    rows = _hybrid(
        source="recordings", table="recordings",
        columns="recordings.id, title, kind, started_at",
        visible=_sp.row_filter(user_id, role, "recordings"),
        keyword=(f"(({like_sql}) OR EXISTS (SELECT 1 FROM recording_segments s "
                 f"WHERE s.recording_id = recordings.id AND {seg_sql}))", [*like_params, *seg_params]),
        order="started_at DESC", qvec=qvec,
    )
    # The sentence that matched, for rows found by keyword.
    with get_conn() as conn:
        for r in rows:
            if r.get("_snippet"):
                continue
            hit = conn.execute(
                f"SELECT s.text FROM recording_segments s WHERE s.recording_id = ? AND {seg_sql} "
                f"ORDER BY s.seq LIMIT 1", (r["id"], *seg_params)).fetchone()
            r["seg_hit"] = hit["text"] if hit else ""
    return rows


# ───────────────────────── drafts ───────────────────────────────────

def _search_drafts(q: str, user_id: str, qvec: Optional[str] = None) -> list[dict[str, Any]]:
    rows = _hybrid(
        source="drafts", table="compose_drafts",
        columns="compose_drafts.id, subject, recipient, kind, updated_at",
        visible=("compose_drafts.user_id = ?", [user_id]),
        keyword=_like(q, "compose_drafts.subject", "compose_drafts.recipient", "compose_drafts.body_html"),
        order="updated_at DESC", qvec=qvec,
    )
    return [{
        "source":      "drafts",
        "id":          r["id"],
        "title":       r["subject"] or "(no subject)",
        "subtitle":    " · ".join(x for x in (r["kind"], r["recipient"]) if x),
        "snippet":     (r.get("_snippet") or "")[:200],
        "timestamp":   r["updated_at"],
        "navigate_to": f"/r/compose?draft_id={r['id']}",
    } for r in rows]
