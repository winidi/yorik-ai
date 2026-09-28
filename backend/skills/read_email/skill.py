"""read_email — fetch the full body of a single email by message_id.

Pairs with find_email_by_subject (resolver) so the agent can answer
"what did Hans write?" by looking up the id, then reading the body.
Owner-scoped query: only emails on accounts the calling user owns
are returned. No side effects — reading via the agent does NOT mark
the message as read; update_email handles that explicitly.
"""

from __future__ import annotations

import json
from typing import Any


async def execute(
    ctx,
    message_id: int,
    include_html: bool = False,
) -> dict[str, Any]:
    from backend.database import get_conn

    if not isinstance(message_id, int) or message_id <= 0:
        raise ValueError("message_id must be a positive integer")

    user_id = getattr(ctx, "user_id", None)
    if not user_id:
        raise ValueError("no user_id on context — read_email needs an owner")

    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, from_email, from_name, to_addrs, cc_addrs, "
            "       subject, date_received, date_sent, "
            "       body_text, body_html, is_starred, is_unread, "
            "       is_sent, has_attachments, thread_id "
            "FROM email_messages "
            "WHERE id = ? AND owner_user_id = ?",
            (message_id, user_id),
        ).fetchone()
        if not row:
            return {"not_found": True}
        atts = conn.execute(
            "SELECT filename, mimetype, size_bytes "
            "FROM email_attachments WHERE message_id = ?",
            (message_id,),
        ).fetchall()
        # The whole conversation: Yorik read Oliver's answer and still
        # said "he has not written back" (chat rerun 2026-09-27).
        thread = conn.execute(
            "SELECT id, message_id, from_email, from_name, date_received, is_sent, snippet "
            "FROM email_messages WHERE owner_user_id = ? AND thread_id = ? AND COALESCE(is_draft, 0) = 0 "
            "ORDER BY date_received, id LIMIT 50",
            (user_id, row["thread_id"]),
        ).fetchall() if row["thread_id"] else []

    d = dict(row)
    for col in ("to_addrs", "cc_addrs"):
        try:
            d[col] = json.loads(d.get(col) or "[]")
        except json.JSONDecodeError:
            d[col] = []

    d["body_text"] = d.get("body_text") or ""

    if not include_html:
        d.pop("body_html", None)

    d["attachments"] = [dict(a) for a in atts]
    d.pop("thread_id", None)
    from backend.search_routes import _local       # household time with offset, not UTC
    for col in ("date_received", "date_sent"):     # "am 21.09." for a mail of 22.09., 0:00 (2026-09-28)
        if d.get(col):
            d[col] = _local(d[col])
    conversation, seen = [], set()
    for t in thread:
        key = t["message_id"] or f"id:{t['id']}"
        if key in seen:                      # the same mail in two folders
            continue
        seen.add(key)
        conversation.append({"message_id": t["id"],
                             "who": "you" if t["is_sent"] else (t["from_name"] or t["from_email"]),
                             "date": _local(t["date_received"]), "first_line": (t["snippet"] or "")[:120],
                             **({"this_mail": True} if t["id"] == message_id else {})})
    out: dict[str, Any] = {"message": d, "_full_output": True}
    if len(conversation) > 1:
        out["conversation"] = conversation
        # Wording approved by Dirk 2026-09-28.
        out["_llm_hint"] = ("conversation is this mail's whole thread, oldest first — say who wrote last "
                            "and when before you say anyone has not answered.")
    # _full_output tells the invoke_skill wrapper in ui_tools.py to
    # skip its default 800-char cap on the LLM-facing preview; the
    # whole point of this skill is to surface the full body.
    return out
