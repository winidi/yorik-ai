"""whatsapp_read — the messages of one WhatsApp chat: around a search
hit, or the latest ones. Only the calling person's own chats.

The chat test (2026-09-26) found no way to read a chat: the model used
whatsapp_draft to "read" and invented what Beate wrote. With a search
hit (universal_search gives `id` and `chat_jid`) the model reads the
messages around it and checks the hit before answering.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional

MAX_WINDOW = 25


async def execute(ctx, chat_jid: Optional[str] = None, contact_id: Optional[int] = None,
                  around_message_id: Optional[int] = None, before: int = 3, after: int = 3,
                  last: int = 10) -> Dict[str, Any]:
    from backend.database import get_conn
    from backend.skills.registry import require_user_id
    user_id = str(require_user_id(ctx))

    before = max(0, min(int(before or 0), MAX_WINDOW))
    after = max(0, min(int(after or 0), MAX_WINDOW))
    last = max(1, min(int(last or 10), MAX_WINDOW))

    with get_conn() as conn:
        anchor = None
        if around_message_id:
            anchor = conn.execute(
                "SELECT id, chat_jid, timestamp FROM wa_messages WHERE id = ? AND owner_user_id = ?",
                (int(around_message_id), user_id)).fetchone()
            if not anchor:
                raise ValueError(f"message {around_message_id} not found in this person's WhatsApp")
            chat_jid = anchor["chat_jid"]
        if not chat_jid and contact_id:
            chat_jid = _jid_for_contact(int(contact_id), ctx)
        if not chat_jid:
            raise ValueError("give around_message_id (from a search hit), chat_jid or contact_id")
        chat = conn.execute("SELECT name FROM wa_chats WHERE jid = ? AND owner_user_id = ?",
                            (chat_jid, user_id)).fetchone()
        if not chat:
            raise ValueError(f"no WhatsApp chat {chat_jid} for this person")
        cols = ("SELECT id, msg_id, from_me, push_name, timestamp, text, transcript, media_kind, mimetype, filename "
                "FROM wa_messages WHERE chat_jid = ? AND owner_user_id = ?")
        if anchor:
            older = conn.execute(cols + " AND (timestamp < ? OR (timestamp = ? AND id < ?)) "
                                 "ORDER BY timestamp DESC, id DESC LIMIT ?",
                                 (chat_jid, user_id, anchor["timestamp"], anchor["timestamp"], anchor["id"], before)).fetchall()
            here = conn.execute(cols + " AND id = ?", (chat_jid, user_id, anchor["id"])).fetchall()
            newer = conn.execute(cols + " AND (timestamp > ? OR (timestamp = ? AND id > ?)) "
                                 "ORDER BY timestamp ASC, id ASC LIMIT ?",
                                 (chat_jid, user_id, anchor["timestamp"], anchor["timestamp"], anchor["id"], after)).fetchall()
            rows = list(reversed(older)) + list(here) + list(newer)
            later = conn.execute(f"SELECT COUNT(*) AS n FROM wa_messages WHERE chat_jid = ? AND owner_user_id = ? "
                                 f"AND timestamp > ?", (chat_jid, user_id, anchor["timestamp"])).fetchone()["n"]
        else:
            rows = list(reversed(conn.execute(cols + " ORDER BY timestamp DESC, id DESC LIMIT ?",
                                              (chat_jid, user_id, last)).fetchall()))
            later = 0

    messages: List[Dict[str, Any]] = []
    for r in rows:
        item: Dict[str, Any] = {
            "id": r["id"],
            "when": _when(r["timestamp"]),
            "who": "me" if r["from_me"] else (r["push_name"] or chat["name"] or ""),
            "text": (r["text"] or r["transcript"] or "").strip(),
        }
        if r["media_kind"]:
            item["media"] = r["media_kind"] + (f" ({r['filename']})" if r["filename"] else "")
            if (r["mimetype"] or "").startswith("text/calendar") or (r["filename"] or "").lower().endswith(".ics"):
                item["calendar"] = await _read_ics(user_id, r["msg_id"])
        if anchor and r["id"] == anchor["id"]:
            item["hit"] = True
        messages.append(item)

    out: Dict[str, Any] = {"chat": chat["name"] or chat_jid, "chat_jid": chat_jid, "messages": messages}
    if anchor:
        out["messages_after_this_window"] = max(0, int(later) - after)
    out["_llm_hint"] = (
        "Check before you answer: is this the person and the thing the user asked about, and is it still "
        "current — does a later message change or replace it? If it does not fit, search on; if it fits, "
        "answer from these messages only and say from whom and when. "
        "If messages_after_this_window is large, read the latest messages too (whatsapp_read with chat_jid)."
    )
    return out


_WD = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def _when(ts: Any) -> str:
    try:
        d = datetime.fromtimestamp(int(ts))
    except (TypeError, ValueError, OSError):
        return ""
    return f"{_WD[d.weekday()]} {d.strftime('%d.%m.%Y %H:%M')}"


def _jid_for_contact(contact_id: int, ctx) -> Optional[str]:
    from backend import contacts as _contacts
    c = _contacts.get(contact_id, role=getattr(ctx, "role", None) or "member", user_id=getattr(ctx, "user_id", None))
    if not c:
        raise ValueError(f"contact_id={contact_id} not found")
    for ch in c.get("channels") or []:
        if ch.get("kind") == "whatsapp" and (ch.get("value") or "").strip():
            v = ch["value"].strip().lower()
            if "@" in v:
                return v
            digits = re.sub(r"\D", "", v)
            return f"{digits}@s.whatsapp.net" if digits else None
    return None


async def _read_ics(user_id: str, msg_id: Optional[str]) -> Dict[str, str]:
    """Title, start and place of a calendar file, fetched from the bridge
    while WhatsApp still has it; otherwise says it cannot be read."""
    if not msg_id:
        return {"note": "calendar file not readable"}
    try:
        import httpx
        from backend import whatsapp as wa
        async with httpx.AsyncClient(timeout=6.0, headers=wa._bridge_headers()) as c:
            r = await c.get(wa._bridge_url(f"/media/{msg_id}", user_id))
        if r.status_code != 200:
            return {"note": "calendar file no longer available from WhatsApp"}
        return parse_ics(r.content.decode("utf-8", "replace")) or {"note": "calendar file without an event"}
    except Exception:  # noqa: BLE001
        return {"note": "calendar file not readable"}


def parse_ics(text: str) -> Dict[str, str]:
    """First VEVENT: SUMMARY, DTSTART, DTEND, LOCATION, DESCRIPTION."""
    unfolded = re.sub(r"\r?\n[ \t]", "", text)
    ev = unfolded.split("BEGIN:VEVENT", 1)
    if len(ev) < 2:
        return {}
    out: Dict[str, str] = {}
    for line in ev[1].split("END:VEVENT", 1)[0].splitlines():
        key, _, val = line.partition(":")
        name = key.split(";", 1)[0].upper()
        if name in ("SUMMARY", "LOCATION", "DESCRIPTION"):
            out[name.lower()] = val.replace("\\n", " ").replace("\\,", ",").strip()[:300]
        elif name in ("DTSTART", "DTEND"):
            out[name.lower()] = _ics_time(val.strip())
    return out


def _ics_time(v: str) -> str:
    """Household local time; UTC stamps ("…Z") are converted."""
    from datetime import timezone
    try:
        utc = datetime.strptime(v, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        from backend.push import _tz
        return utc.astimezone(_tz()).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        pass
    for fmt, shown in (("%Y%m%dT%H%M%S", "%d.%m.%Y %H:%M"), ("%Y%m%d", "%d.%m.%Y")):
        try:
            return datetime.strptime(v, fmt).strftime(shown)
        except ValueError:
            continue
    return v
