"""draft_reply suggestion type.

Emitted when the LLM judges the incoming message warrants a reply
AND the user typically replies to this sender (from email_history
evidence).

On Accept the handler persists the LLM-drafted body as a pending
row in email_drafts — the existing Reader-pane draft chip then
picks it up, identical to the email_autodraft flow. The user
sends with one click. No duplicate compose code paths."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict

from ..registry import HandlerContext, SuggestionType, register_type

log = logging.getLogger("yorik.suggestions.types.draft_reply")


PAYLOAD_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["body"],
    "properties": {
        "body": {
            "type": "string",
            "description": "The full draft reply body. Same language as the incoming message.",
            "maxLength": 4000,
        },
        "subject": {
            "type": "string",
            "description": "Optional subject override; defaults to 'Re: <original>'.",
        },
        "tone": {
            "type": "string",
            "enum": ["friendly", "formal", "quick", "warm", "firm"],
            "description": "Tone label shown on the suggestion chip.",
        },
    },
}


async def _handle(payload: Dict[str, Any], ctx: HandlerContext) -> Dict[str, Any]:
    """Insert the drafted body into email_drafts for the original
    message. Reuses the email_drafts table so the Reader pane shows
    it via the existing pending-draft query — zero new UI surface
    for the send step.
    """
    body = (payload.get("body") or "").strip()
    if not body:
        return {"ok": False, "error": "empty body"}
    tone_label = (payload.get("tone") or "suggested").strip()
    if ctx.source_kind != "email":
        return {"ok": False, "error": f"draft_reply only valid for email, got {ctx.source_kind}"}

    from ...database import get_conn
    with get_conn() as conn:
        m = conn.execute(
            "SELECT id, thread_id FROM email_messages WHERE id=?",
            (ctx.source_id,),
        ).fetchone()
        if not m:
            return {"ok": False, "error": "source email vanished"}
        thread_id = m["thread_id"]

        group_id = str(uuid.uuid4())
        cur = conn.execute(
            "INSERT INTO email_drafts (message_id, thread_id, draft_text, "
            "  variant_label, variant_group_id, sources_json, owner_user_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (ctx.source_id, thread_id, body, tone_label, group_id,
             json.dumps({"origin": "suggestion", "suggestion_id": ctx.suggestion_id}),
             ctx.owner_user_id),
        )
        draft_id = int(cur.fetchone()["id"])
        conn.commit()

    log.info("draft_reply accepted: suggestion=%s → email_draft=%s",
             ctx.suggestion_id, draft_id)
    return {"ok": True, "draft_id": draft_id, "action": "draft_persisted"}


register_type(SuggestionType(
    type="draft_reply",
    payload_schema=PAYLOAD_SCHEMA,
    handler=_handle,
    fallback_title="Suggested reply",
))
