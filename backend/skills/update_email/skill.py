"""update_email skill — toggle is_starred / is_unread on an email.

Wraps the same email_actions.set_seen / set_starred functions the
PATCH /api/email/messages/{msg_id} route uses, so IMAP + the local
mirror stay consistent. Closes the gap that made the chat agent
create a new contact when the user asked to "star the support
email" — there's now a skill to call.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional


async def execute(
    ctx,
    message_id: int,
    is_starred: Optional[bool] = None,
    is_unread: Optional[bool] = None,
) -> dict[str, Any]:
    if not isinstance(message_id, int) or message_id <= 0:
        raise ValueError("message_id must be a positive integer")
    if is_starred is None and is_unread is None:
        raise ValueError("pass at least one of is_starred / is_unread")

    user_id = getattr(ctx, "user_id", None)
    if not user_id:
        raise ValueError("no user_id on context — update_email needs an owner")

    from backend import email_actions

    applied: dict[str, bool] = {}

    if is_unread is not None:
        # set_seen takes the opposite — seen=True means is_unread=False.
        ok = await asyncio.to_thread(
            email_actions.set_seen, message_id, user_id, not is_unread,
        )
        if not ok:
            raise RuntimeError(
                f"IMAP STORE failed setting is_unread={is_unread} on msg {message_id}"
            )
        applied["is_unread"] = bool(is_unread)

    if is_starred is not None:
        ok = await asyncio.to_thread(
            email_actions.set_starred, message_id, user_id, bool(is_starred),
        )
        if not ok:
            raise RuntimeError(
                f"IMAP STORE failed setting is_starred={is_starred} on msg {message_id}"
            )
        applied["is_starred"] = bool(is_starred)

    return {"ok": True, "message_id": message_id, "applied": applied}
