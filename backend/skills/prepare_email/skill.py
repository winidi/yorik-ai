"""prepare_email — stage an email draft (recipient, subject, body, any
number of attachments: chat files, archive documents, photos) for the
user to review and send themselves in the Email app.

Never calls email_sender.send() and never touches SMTP. Two things
happen:
  1. The draft is written server-side into app_settings (key
     pending_email_draft_<user_id>), read-and-cleared by GET
     /api/email/pending-draft on the Email app's mount. This is the
     data channel — it works no matter which tab, window, or later
     moment the user opens Email in. (V1 used only a sessionStorage
     handoff; the user reported "I don't see the draft" because that
     only reached the one browser tab that happened to receive the
     assistant's reply — 2026-09-25.)
  2. An `email_ready` UI action is returned so the chat renders a real
     card — recipient, subject, a body preview, the attachment name —
     with an "Open and send" button (EmailDraftReadyCard.tsx) that
     jumps straight to /email. Before this, the assistant only ever
     said "open Email yourself" in plain text, which the user rightly
     called out as not UX-friendly (2026-09-25) next to how a letter
     or invoice draft gets a proper inline card elsewhere in Yorik.
Nothing is sent, filed, or delivered by this skill itself.
"""

from __future__ import annotations

import json
from typing import Any, Optional


async def execute(
    ctx,
    to: str,
    subject: str,
    body: str,
    attachment_id: Optional[int] = None,
    from_email: Optional[str] = None,
    paperless_doc_id: Optional[int] = None,
    attachment_ids: Optional[list] = None,
    paperless_doc_ids: Optional[list] = None,
    photo_ids: Optional[list] = None,
) -> dict[str, Any]:
    user_id = getattr(ctx, "user_id", None)
    if not user_id:
        raise ValueError("prepare_email needs a signed-in user")

    to = (to or "").strip()
    if not to or "@" not in to:
        return {"ok": False, "_llm_hint": f"REJECTED: {to!r} doesn't look like an email address. "
                                          f"Ask the user for a real recipient."}

    # Attachments are optional ("schreib Dirk, dass ich später komme"
    # has none). Any mix of chat files, archive documents and photos;
    # the singular names stay as aliases for the model. Every file is
    # looked up as the person (own chat row, own Paperless token, own
    # Immich key), so a file they may not see cannot be sent.
    from backend import send_attachments as _sa
    attachments, problems = _sa.resolve(
        str(user_id),
        attachment_ids=list(attachment_ids or []) + ([attachment_id] if attachment_id not in (None, "", 0) else []),
        paperless_doc_ids=list(paperless_doc_ids or []) + ([paperless_doc_id] if paperless_doc_id not in (None, "", 0) else []),
        photo_ids=list(photo_ids or []),
    )
    if problems:
        return {"ok": False, "problems": problems,
                "_llm_hint": "REJECTED: " + " ".join(problems) + " Nothing was staged; fix the ids or ask the user."}
    # The Composer and the staged draft know only url/filename/mimetype.
    attachments = [{k: a[k] for k in ("url", "filename", "mimetype")} for a in attachments]
    attachment_filenames = [a["filename"] for a in attachments]
    attachment_filename = ", ".join(attachment_filenames)
    row = None

    # An address the person never used and did not type gets a card with
    # the closest known addresses (Dirk 2026-09-27); nothing is staged yet.
    from backend import email_addresses as _ea
    known = _ea.known(user_id, getattr(ctx, "role", None), getattr(ctx, "conversation_id", None))
    if to.lower() not in known:
        suggestions = _ea.closest(to, known)
        pending = {"to": to, "subject": (subject or "").strip(), "body": body or "",
                   "attachments": attachments, "from_email": from_email}
        from backend.database import get_conn as _gc
        with _gc() as conn:
            conn.execute("INSERT OR REPLACE INTO app_settings (key, value, updated_at) VALUES (?, ?, datetime('now'))",
                         (f"pending_email_check_{user_id}", json.dumps(pending)))
            conn.commit()
        from backend.ui_tools import _append
        _append({"type": "email_recipient_check", "typed": to, "suggestions": suggestions,
                 "subject": pending["subject"], "attachment_filename": attachment_filename})
        return {"ok": False, "unknown_recipient": to, "suggestions": suggestions,
                "_llm_hint": (f"UNKNOWN RECIPIENT: {to} is not a known address (contacts, own accounts, earlier "
                              "mail, or typed by the user). A card shows the closest known addresses to pick or "
                              "correct; tell the user in one sentence to check the address there.")}

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

    # Which of the person's own accounts sends it. Without a chosen
    # default and with several accounts, nothing is picked: the card
    # asks (Dirk 2026-09-29 — the Composer silently took the first).
    from backend.database import get_conn as _gc2
    with _gc2() as conn:
        own = [dict(r) for r in conn.execute(
            "SELECT id, email, display_name, is_default FROM email_accounts "
            "WHERE owner_user_id = ? AND enabled = 1 ORDER BY id", (user_id,)).fetchall()]
    if account_id is None and own:
        default = [a for a in own if a.get("is_default")]
        if default:
            account_id = default[0]["id"]
        elif len(own) == 1:
            account_id = own[0]["id"]

    payload = {
        "to":          to,
        "subject":     (subject or "").strip(),
        "body":        body or "",
        "account_id":  account_id,
        "attachments": attachments,
    }

    # One staged draft per person: a newer one replaces the older. The
    # card says so, so nobody opens the first card and sends the second
    # mail (seen live 2026-10-16).
    from backend.database import get_conn as _gc0
    with _gc0() as conn:
        replaced_previous = bool(conn.execute(
            "SELECT 1 FROM app_settings WHERE key = ?", (f"pending_email_draft_{user_id}",)).fetchone())

    # 1. A real card in the chat, not just a sentence — mirrors
    # writing_draft_created's WritingDraftCard for letters/invoices.
    body_preview = (body or "").strip().replace("\n", " ")
    if len(body_preview) > 160:
        body_preview = body_preview[:157] + "…"
    from backend.ui_tools import _append
    card = {
        "type":                "email_ready",
        "to":                  to,
        "subject":             payload["subject"],
        "preview":             body_preview,
        "attachment_filename": attachment_filename,
        "attachment_filenames": attachment_filenames,
        "replaced_previous":   replaced_previous,
        "account_id":          account_id,
        "accounts":            [{"id": a["id"], "email": a["email"],
                                 "name": a.get("display_name") or ""} for a in own] if len(own) > 1 else [],
    }
    _append(card)
    # The card learns what became of the draft (sent / thrown away).
    payload["card"] = {"conversation_id": getattr(ctx, "conversation_id", None), "uid": card.get("uid")}

    # 2. Durable — survives a different tab, a reload, or the user
    # coming back later. See module docstring for why this exists.
    from backend.database import get_conn
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value, updated_at) "
            "VALUES (?, ?, datetime('now'))",
            (f"pending_email_draft_{user_id}", json.dumps(payload)),
        )
        conn.commit()

    return {
        "ok":            True,
        "staged_to":     to,
        "attachments":   attachment_filenames,
        "replaced_previous_draft": replaced_previous,
        "_llm_hint": (
            f"Staged, NOT sent — a card with an 'open and send' button already showed the user everything "
            f"(recipient, subject, preview, attachment). Just confirm in one short line that it's ready; don't "
            f"repeat the details the card already shows, and don't say 'open the Email app yourself', the card "
            f"does that." + account_note
        ),
    }
