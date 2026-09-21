"""write_letter — a draft letter in the Schreiben app. The model brings
the text, the contacts bring the address, the letterhead the look; what
is missing is marked on the sheet and nobody is asked."""

from __future__ import annotations

from typing import Any, Dict, Optional


async def execute(ctx, recipient: str, subject: str, text: str, document_id: Optional[int] = None) -> Dict[str, Any]:
    from backend.writing import layouts, letterhead as lh_mod, recipient as rcp_mod, store
    user_id = getattr(ctx, "user_id", None)
    if not user_id:
        raise ValueError("write_letter needs a signed-in user")
    uid, role = str(user_id), getattr(ctx, "role", None)
    text = (text or "").strip()
    if not text:
        raise ValueError("text is required: write the whole letter")
    subject = (subject or "").strip()[:200]
    content = {"subject": subject, "text_html": layouts.text_to_html(text), "add_closing": False}

    doc = store.get(int(document_id), uid) if document_id else None
    if doc and doc["status"] == "draft" and doc["kind"] == "letter":
        rcp = doc["recipient"] if (recipient or "").strip().lower() == (doc["recipient"].get("name") or "").lower() else None
        doc = store.update(doc["id"], uid, title=subject, content=content,
                           recipient=rcp or rcp_mod.resolve(recipient, role=role, user_id=uid))
    else:
        lh = lh_mod.default_for(uid)
        rcp = rcp_mod.resolve(recipient, role=role, user_id=uid, sender_country=lh["data"]["country"])
        doc = store.create(uid, "letter", title=subject, recipient=rcp, content=content, letterhead_id=lh["id"])

    missing = [label for label, gone in (("Empfänger", not doc["recipient"].get("name")),
                                         ("Adresse", not doc["recipient"].get("address_lines"))) if gone]
    import re
    preview = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", content["text_html"])).strip()[:180]
    from backend.ui_tools import _append
    _append({"type": "writing_draft_created", "document_id": doc["id"], "kind": "letter", "recipient": doc["recipient"].get("name") or "",
             "subject": subject, "preview": preview, "missing": missing})
    return {"document_id": doc["id"], "missing": missing,
            "_llm_hint": (f"shown_to_user: the draft letter (document_id={doc['id']}) is on a card the user can open. "
                          + (f"On the sheet still marked as missing: {', '.join(missing)}; mention it in half a sentence, do not ask for it. " if missing else "")
                          + f"Answer in one short sentence in the user's language. For changes call write_letter again with document_id={doc['id']}.")}
