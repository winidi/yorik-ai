"""read_attachment — the text of a file the user attached in the chat."""

from __future__ import annotations

from typing import Any, Dict, Optional


async def execute(ctx, attachment_id: int, question: Optional[str] = None) -> Dict[str, Any]:
    from backend import chat_attachments as A
    user_id = getattr(ctx, "user_id", None)
    if not user_id:
        raise ValueError("read_attachment needs a signed-in user")
    row = A.get(int(attachment_id), str(user_id))
    if not row:
        return {"ok": False, "_llm_hint": "REJECTED: there is no attachment with this number for this user. "
                                          "Ask them to attach the file again."}
    conv = getattr(ctx, "conversation_id", None)
    if conv and not row.get("conversation_id"):
        A.bind_conversation(row["id"], str(user_id), str(conv))

    is_image = (row["mime_type"] or "").startswith("image/")
    text = (row.get("text") or "").strip()
    source = "text"
    pages_note = ""
    if is_image:
        try:
            text = await A.describe_image(row, question)
            source = "vision"
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "filename": row["filename"],
                    "_llm_hint": f"The picture could not be read ({type(exc).__name__}). Say so in one line."}
    elif not text and row["mime_type"] == "application/pdf":
        # a scan: the vision model transcribes the pages (kept, so only once)
        try:
            text, read, total = await A.read_scanned_pdf(row)
            source = "vision" if text else "empty"
            if text and total > read:
                pages_note = (f" Only the first {read} of {total} pages were read here; say so, and that Paperless "
                              f"reads all of them once the document is filed.")
        except Exception:  # noqa: BLE001
            source = "empty"
    truncated = len(text) > A.TEXT_CAP
    filed = bool(row["filed_at"])
    if source == "empty":
        hint = ("This scanned PDF could not be read here (the vision model gave nothing). Say so in one line and "
                "offer to file it in Paperless, where it gets OCR and becomes searchable (file_attachment).")
    elif filed:
        hint = "Already filed in Paperless. Say what the file is in one or two sentences; do not ask about filing."
    elif is_image:
        hint = ("Say in one or two sentences what the picture shows. It stays with this conversation only; mention "
                "filing in Paperless only if it is clearly a document (a letter, an invoice, a certificate).")
    else:
        hint = ("Say in one or two sentences what this document is (kind, sender, date, amount if any). Then ask "
                "once, in the user's language: shall I file it in Paperless, and who should see it there — only "
                "you, the parents, or the whole family? Tell them that otherwise it stays with this conversation and "
                "is deleted after 30 days. When they answer, call file_attachment with visibility 'private' (only "
                "them), 'parents' (the adults, not the children) or 'shared' (everyone). Do not guess the visibility; if they only say yes, ask who should see it.")
    return {"ok": True, "attachment_id": row["id"], "filename": row["filename"], "mime_type": row["mime_type"],
            "source": source, "text": text[:A.TEXT_CAP], "truncated": truncated, "filed": filed,
            "expires_at": row["expires_at"], "_llm_hint": hint + pages_note, "_full_output": True}
