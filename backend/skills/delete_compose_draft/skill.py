"""delete_compose_draft skill — stage ONE compose draft for deletion.

Confirm-before-apply like the other delete_* skills: nothing is deleted
when this returns; the DELETE runs when the user taps "Delete" on the
pending_confirmation card (pending_actions.apply). The version history
(compose_draft_versions) is cascade-deleted by the schema at that point.
"""
from __future__ import annotations
from typing import Any


async def execute(ctx, draft_id: int) -> dict[str, Any]:
    if not isinstance(draft_id, int) or draft_id <= 0:
        raise ValueError("draft_id must be a positive integer")

    # Per-turn destructive throttle — shared counter with the other
    # delete_* skills so "lösche X und Y" can't stage two deletes at once.
    from backend.ask import _deletes_this_turn, DELETE_TURN_LIMIT
    n_so_far = _deletes_this_turn.get()
    if n_so_far >= DELETE_TURN_LIMIT:
        raise ValueError(
            "REFUSED: a delete was already staged in this turn. "
            "Only ONE row may be deleted per request. List the remaining "
            "drafts (id + subject + recipient) and wait for the user to "
            "name which to delete next."
        )
    _deletes_this_turn.set(n_so_far + 1)

    from backend.database import get_conn
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, user_id, kind, template_id, recipient, subject, created_at "
            "FROM compose_drafts WHERE id=?", (draft_id,),
        ).fetchone()
    if not row:
        raise ValueError(f"draft {draft_id} not found (already deleted?)")
    draft = dict(row)

    # Owner gate — a non-admin caller may only delete drafts they own.
    caller_role = getattr(ctx, "role", None)
    caller_id = getattr(ctx, "user_id", None)
    if caller_role not in ("platform_admin", "admin") and draft["user_id"] != caller_id:
        raise PermissionError(
            f"draft {draft_id} belongs to a different user; only the owner "
            f"or an admin can delete it"
        )

    from backend import pending_actions as pa
    pending_id = pa.stage_before_apply(
        skill="delete_compose_draft",
        ctx=ctx,
        apply_kind="delete_compose_draft",
        apply_args={"draft_id": draft_id, "subject": draft.get("subject")},
        params={"draft_id": draft_id},
        preview={
            "action":    "delete",
            "draft_id":  draft_id,
            "subject":   draft.get("subject"),
            "recipient": draft.get("recipient"),
            "kind":      draft.get("kind"),
        },
    )

    label = draft.get("subject") or f"draft {draft_id}"
    return {
        "pending":    True,
        "pending_id": pending_id,
        "draft":      {
            "id":        draft_id,
            "subject":   draft.get("subject"),
            "recipient": draft.get("recipient"),
            "kind":      draft.get("kind"),
        },
        "_llm_hint": (
            f"shown_to_user: a confirmation card for deleting the draft '{label}' is on screen. "
            "NOTHING is deleted yet — it happens only when the user taps Delete on the card. "
            "Tell the user the card is waiting for their confirmation; do not claim the draft is deleted."
        ),
    }
