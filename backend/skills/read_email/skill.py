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
            "       is_sent, has_attachments "
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
    # _full_output tells the invoke_skill wrapper in ui_tools.py to
    # skip its default 800-char cap on the LLM-facing preview; the
    # whole point of this skill is to surface the full body.
    return {"message": d, "_full_output": True}
