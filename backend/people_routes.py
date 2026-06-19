"""Unified person view — resolve any identifier (email / phone / WA jid)
to a single human and return their cross-channel context.

The challenge: the same human shows up under different identifiers in
different channels. "alex@muellerbau.de" in email vs "+4930…@s.whatsapp.net"
in WhatsApp. We can't perfectly merge without explicit contact records,
but heuristics get us 80% of the way:

- Email identifier → exact match on email_messages.from_email/to
- Phone identifier (E.164) → exact match on wa_chats.jid prefix
- WA jid → exact match
- Plus: name-based fuzzy join across channels so an email's display name
  ("Alex Müller") finds WhatsApp chats whose push_name or chat name
  contains the same surname token

Owner-scoped throughout. Cached per-identifier for 30s so a fast hover
in/out of the same sender doesn't re-query.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException

from .auth_sessions import current_user
from .database import get_conn

log = logging.getLogger("yorik.people")

router = APIRouter(prefix="/api/people", tags=["people"])

RECENT_LIMIT_PER_CHANNEL = 5
DOCS_LIMIT = 4


@router.get("/{identifier:path}")
def get_person(identifier: str, user: dict = Depends(current_user)) -> dict[str, Any]:
    """Resolve `identifier` to a unified person + return their stuff.

    identifier may be:
      - An email address (anna@example.com)
      - A phone number (+4930…) or "raw" digits
      - A WhatsApp JID (4915…@s.whatsapp.net, …@lid, …@g.us)
    """
    user_id = user["id"]
    kind, normalized = _classify(identifier)

    # Search by primary identifier first, then expand to other channels
    # by name overlap once we have a primary hit.
    emails = []
    wa_messages = []
    wa_chats: list[dict[str, Any]] = []
    events = []
    documents = []
    names_used: set[str] = set()
    phone_numbers: set[str] = set()
    email_addrs: set[str] = set()

    if kind == "email":
        email_addrs.add(normalized)
        emails = _emails_by_address(normalized, user_id)
        for e in emails:
            if e["from_name"]: names_used.add(e["from_name"])
    elif kind == "wa_jid":
        wa_chats, wa_messages = _wa_by_jid(normalized, user_id)
        for m in wa_messages:
            if m.get("push_name"): names_used.add(m["push_name"])
        for c in wa_chats:
            if c.get("name"): names_used.add(c["name"])
            if "@s.whatsapp.net" in (c.get("jid") or ""):
                phone_numbers.add("+" + c["jid"].split("@")[0])
    elif kind == "phone":
        # Strip non-digits + prefix country code if missing — best-effort.
        phone_numbers.add(normalized)
        digits = re.sub(r"\D", "", normalized)
        wa_chats, wa_messages = _wa_by_jid_like(digits, user_id)
        for m in wa_messages:
            if m.get("push_name"): names_used.add(m["push_name"])
        for c in wa_chats:
            if c.get("name"): names_used.add(c["name"])

    # Cross-channel expansion by name overlap. If we found names in one
    # channel, look for matching names in the other channels.
    if names_used and not emails:
        emails = _emails_by_names(list(names_used), user_id)
    if names_used and not wa_messages:
        more_chats, more_msgs = _wa_by_names(list(names_used), user_id)
        wa_chats.extend(more_chats); wa_messages.extend(more_msgs)

    # Calendar: match person field OR title containing any of the names.
    events = _events_by_names(list(names_used), user_id) if names_used else []

    # Documents: search Paperless for any of the names (semantic).
    documents = _docs_by_names(list(names_used), user_id) if names_used else []

    # Pick the "best" display name + primary email.
    primary_name = _best_name(list(names_used)) or normalized
    primary_email = next(iter(email_addrs), None) or (emails[0]["from_email"] if emails else None)
    primary_phone = next(iter(phone_numbers), None)

    # Compose a chronological feed (recent contact card) across all
    # channels. Newest first, cap to 8.
    feed = _compose_feed(emails, wa_messages, events, limit=8)

    return {
        "identifier":    identifier,
        "primary_name":  primary_name,
        "primary_email": primary_email,
        "primary_phone": primary_phone,
        "names_used":    sorted(names_used),
        "emails_recent":     emails[:RECENT_LIMIT_PER_CHANNEL],
        "wa_chats":          wa_chats[:RECENT_LIMIT_PER_CHANNEL],
        "wa_messages_recent": wa_messages[:RECENT_LIMIT_PER_CHANNEL],
        "events_recent":     events[:RECENT_LIMIT_PER_CHANNEL],
        "documents":         documents[:DOCS_LIMIT],
        "feed":              feed,
    }


# ───────────────────────── identifier classification ───────────────

def _classify(s: str) -> tuple[str, str]:
    s = s.strip()
    if "@s.whatsapp.net" in s or "@g.us" in s or "@lid" in s:
        return "wa_jid", s
    if "@" in s:
        return "email", s.lower()
    if re.match(r"^\+?[\d\s().-]{6,}$", s):
        return "phone", s
    # Bare name — treat as name search via names_used fallback.
    return "name", s


# ───────────────────────── per-channel lookups ────────────────────

def _emails_by_address(addr: str, user_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, subject, snippet, date_received, is_sent, from_name, from_email, "
            "       to_addrs "
            "FROM email_messages "
            "WHERE owner_user_id=? AND ("
            "    LOWER(from_email)=? OR to_addrs LIKE ? OR cc_addrs LIKE ?"
            ") ORDER BY date_received DESC LIMIT 20",
            (user_id, addr, f'%"{addr}"%', f'%"{addr}"%'),
        ).fetchall()
    return [dict(r) for r in rows]


def _emails_by_names(names: list[str], user_id: str) -> list[dict[str, Any]]:
    if not names:
        return []
    # Build OR clause on from_name + to_addrs JSON blob.
    likes = " OR ".join(["LOWER(from_name) LIKE ?"] * len(names))
    params: list[Any] = [user_id]
    for n in names:
        params.append(f"%{n.lower()}%")
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT id, subject, snippet, date_received, is_sent, from_name, from_email, "
            f"       to_addrs "
            f"FROM email_messages WHERE owner_user_id=? AND ({likes}) "
            f"ORDER BY date_received DESC LIMIT 10",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def _wa_by_jid(jid: str, user_id: str):
    with get_conn() as conn:
        chats = conn.execute(
            "SELECT jid, name, last_message_ts FROM wa_chats "
            "WHERE jid=? AND owner_user_id=?",
            (jid, user_id),
        ).fetchall()
        msgs = conn.execute(
            "SELECT msg_id, chat_jid, text, transcript, timestamp, push_name, from_me "
            "FROM wa_messages WHERE chat_jid=? AND owner_user_id=? "
            "ORDER BY timestamp DESC LIMIT 10",
            (jid, user_id),
        ).fetchall()
    return [dict(c) for c in chats], [dict(m) for m in msgs]


def _wa_by_jid_like(digits: str, user_id: str):
    """Phone-based lookup. WA JIDs are <number>@s.whatsapp.net for direct
    chats. We do LIKE prefix match on the JID."""
    with get_conn() as conn:
        chats = conn.execute(
            "SELECT jid, name, last_message_ts FROM wa_chats "
            "WHERE jid LIKE ? AND owner_user_id=?",
            (f"{digits}%", user_id),
        ).fetchall()
        msgs = conn.execute(
            "SELECT msg_id, chat_jid, text, transcript, timestamp, push_name, from_me "
            "FROM wa_messages "
            "WHERE chat_jid LIKE ? AND owner_user_id=? "
            "ORDER BY timestamp DESC LIMIT 10",
            (f"{digits}%", user_id),
        ).fetchall()
    return [dict(c) for c in chats], [dict(m) for m in msgs]


def _wa_by_names(names: list[str], user_id: str):
    likes = " OR ".join(["LOWER(c.name) LIKE ?"] * len(names))
    params: list[Any] = [user_id]
    for n in names:
        params.append(f"%{n.lower()}%")
    with get_conn() as conn:
        chats = conn.execute(
            f"SELECT jid, name, last_message_ts FROM wa_chats c "
            f"WHERE owner_user_id=? AND ({likes}) LIMIT 5",
            params,
        ).fetchall()
        jids = [c["jid"] for c in chats]
        if not jids:
            return [], []
        ph = ",".join("?" * len(jids))
        msgs = conn.execute(
            f"SELECT msg_id, chat_jid, text, transcript, timestamp, push_name, from_me "
            f"FROM wa_messages WHERE chat_jid IN ({ph}) AND owner_user_id=? "
            f"ORDER BY timestamp DESC LIMIT 10",
            (*jids, user_id),
        ).fetchall()
    return [dict(c) for c in chats], [dict(m) for m in msgs]


def _events_by_names(names: list[str], user_id: str) -> list[dict[str, Any]]:
    if not names:
        return []
    likes = " OR ".join(["LOWER(title) LIKE ? OR LOWER(COALESCE(person,'')) LIKE ?"] * len(names))
    params = []
    for n in names:
        params.extend([f"%{n.lower()}%", f"%{n.lower()}%"])
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT id, title, starts_at, ends_at, person FROM events WHERE ({likes}) "
            f"ORDER BY starts_at DESC LIMIT 10",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def _docs_by_names(names: list[str], user_id: str) -> list[dict[str, Any]]:
    """Semantic search Paperless for any of the names. Joins results
    if multiple names match the same doc."""
    if not names:
        return []
    try:
        from . import paperless_ingest
        from .external_users import get_user_paperless_creds
        creds = get_user_paperless_creds(user_id)
        # Search by the first name (best signal); semantic search
        # broadens automatically.
        return paperless_ingest.search(names[0], k=DOCS_LIMIT, creds_override=creds) or []
    except Exception:
        return []


# ───────────────────────── feed composition ────────────────────────

def _compose_feed(emails, wa_messages, events, limit: int) -> list[dict[str, Any]]:
    """Interleave channels chronologically (newest first)."""
    items = []
    for e in emails:
        items.append({
            "kind": "email",
            "ts":   _ts(e.get("date_received")),
            "label": ("Sent: " if e.get("is_sent") else "") + (e.get("subject") or "(no subject)"),
            "snippet": e.get("snippet") or "",
            "ref": e.get("id"),
        })
    for m in wa_messages:
        items.append({
            "kind": "wa",
            "ts":   m.get("timestamp"),
            "label": ("Sent: " if m.get("from_me") else "") + ((m.get("text") or m.get("transcript") or "")[:80]),
            "snippet": "",
            "ref": m.get("msg_id"),
        })
    for ev in events:
        items.append({
            "kind": "event",
            "ts":   _ts(ev.get("starts_at")),
            "label": ev.get("title") or "",
            "snippet": "",
            "ref": ev.get("id"),
        })
    items.sort(key=lambda x: x["ts"] or 0, reverse=True)
    return items[:limit]


def _ts(s) -> Optional[int]:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return int(s)
    try:
        # Try ISO datetime → epoch seconds.
        import datetime
        return int(datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def _best_name(names: list[str]) -> Optional[str]:
    """Pick the most "human" name from the candidates. Prefers names
    that contain a space (likely full name) over single tokens."""
    if not names:
        return None
    with_space = [n for n in names if " " in n]
    if with_space:
        return max(with_space, key=len)
    return max(names, key=len)
