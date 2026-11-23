"""delete_email skill: stage ONE email for deletion (confirm-before-apply).

Nothing is deleted when this returns. On the user's yes
(pending_actions.apply) the email moves to the Trash the way a mail
program does (email_actions.delete_message); from the Trash it is
removed for good only when it already was there.
"""
from __future__ import annotations

from typing import Any


async def execute(ctx, message_id: int) -> dict[str, Any]:
    if not isinstance(message_id, int) or message_id <= 0:
        raise ValueError(f"message_id must be one positive integer, got {message_id!r}")
    user_id = getattr(ctx, "user_id", None)
    if not user_id:
        raise ValueError("delete_email needs a signed-in user")

    # One deletion per request, as for events and tasks.
    from backend.ask import _deletes_this_turn, DELETE_TURN_LIMIT
    n_so_far = _deletes_this_turn.get()
    if n_so_far >= DELETE_TURN_LIMIT:
        raise ValueError(
            "REFUSED: a delete was already staged in this turn. Only ONE email may be deleted per "
            "request. STOP, tell the user which emails are left and wait for them to name the next one.")
    _deletes_this_turn.set(n_so_far + 1)

    from backend.database import get_conn
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, subject, from_email, date_received FROM email_messages "
            "WHERE id = ? AND owner_user_id = ?", (message_id, str(user_id))).fetchone()
    if not row:
        raise ValueError(f"email {message_id} not found among this user's emails (already deleted?)")
    mail = dict(row)
    sender = mail.get("from_email") or ""
    subject = mail.get("subject") or "(no subject)"

    from backend import pending_actions as pa
    pending_id = pa.stage_before_apply(
        skill="delete_email",
        ctx=ctx,
        apply_kind="delete_email",
        apply_args={"message_id": message_id, "user_id": str(user_id), "subject": subject},
        params={"message_id": message_id},
        preview={"action": "delete", "title": subject, "from": sender, "date": str(mail.get("date_received") or "")[:16]},
    )
    return {
        "pending":    True,
        "pending_id": pending_id,
        "email":      {"id": message_id, "subject": subject, "from": sender, "date": mail.get("date_received")},
        "_llm_hint": (
            f"shown_to_user: a confirmation card for deleting the email '{subject}' is on screen. "
            "NOTHING is deleted yet; on the user's yes it moves to the Trash. "
            "Tell the user the card is waiting for their confirmation; do not claim the email is deleted."
        ),
    }
