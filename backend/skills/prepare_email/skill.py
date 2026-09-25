"""prepare_email — stage an email draft (recipient, subject, body, one
attachment) for the user to review and send themselves in the Email app.

Never calls email_sender.send() and never touches SMTP. It only queues
a `stash_pending_email` UI action — consumed client-side by
EmailStashBridge, which writes it into the very sessionStorage key the
AttachmentStashTray's "Send via email" button already uses. Net
effect: the Email app's Composer opens pre-filled next time the user
visits it. Nothing is sent, filed, or delivered by this skill itself.
"""

from __future__ import annotations

from typing import Any, Optional


async def execute(
    ctx,
    to: str,
    subject: str,
    body: str,
    attachment_id: int,
    from_email: Optional[str] = None,
) -> dict[str, Any]:
    user_id = getattr(ctx, "user_id", None)
    if not user_id:
        raise ValueError("prepare_email needs a signed-in user")

    to = (to or "").strip()
    if not to or "@" not in to:
        return {"ok": False, "_llm_hint": f"REJECTED: {to!r} doesn't look like an email address. "
                                          f"Ask the user for a real recipient."}

    from backend import chat_attachments as A
    row = A.get(int(attachment_id), str(user_id))
    if not row:
        return {"ok": False, "_llm_hint": "REJECTED: there is no attachment with this number for this user. "
                                          "Ask them to attach or create the file again."}

    account_id: Optional[int] = None
    account_note = ""
    if from_email:
        from backend import email_sender
        acct = email_sender.resolve_account_for_from_address(from_email)
        if acct and str(acct.get("owner_user_id")) == str(user_id):
            account_id = acct["id"]
        else:
            account_note = (
                f" Couldn't find {from_email!r} among the user's own configured email accounts — the Composer's "
                f"default account will be pre-selected instead; mention that so they can double-check it."
            )

    from backend.ui_tools import _append
    _append({
        "type":        "stash_pending_email",
        "to":          to,
        "subject":     (subject or "").strip(),
        "body":        body or "",
        "account_id":  account_id,
        "attachments": [{
            "url":      f"/api/chat/attachments/{row['id']}/raw",
            "filename": row["filename"],
            "mimetype": row["mime_type"] or "application/octet-stream",
        }],
    })

    return {
        "ok":            True,
        "staged_to":     to,
        "attachment_id": row["id"],
        "_llm_hint": (
            f"Staged, NOT sent. Tell the user in one line that the email to {to} is ready and waiting in the Email "
            f"app with '{row['filename']}' attached, and that they need to open Email themselves, check it, and "
            f"press send — nothing was sent or delivered by this call." + account_note
        ),
    }
