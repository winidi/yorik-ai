"""delete_compose_draft skill — DELETE one row from compose_drafts.

Mirrors delete_contact / delete_calendar_event: per-turn throttle, owner
gate, snapshot for rollback, refresh_data ui_action, confirm_then_apply
to stage the rollback card. Apply-then-confirm pattern — the row is gone
the moment this returns; cancel restores it via restore_compose_draft.
"""
from __future__ import annotations
from typing import Any


async def execute(ctx, draft_id: int) -> dict[str, Any]:
    if not isinstance(draft_id, int) or draft_id <= 0:
        raise ValueError("draft_id must be a positive integer")

    # Per-turn destructive throttle — shared counter with the other
    # delete_* skills so "lösche X und Y wieder" can't bulk-delete in one
    # turn either.
    from backend.ask import _deletes_this_turn, DELETE_TURN_LIMIT
    n_so_far = _deletes_this_turn.get()
    if n_so_far >= DELETE_TURN_LIMIT:
        raise ValueError(
            "REFUSED: another item was already deleted in this turn. "
            "Only ONE row may be deleted per request. List the remaining "
            "drafts (id + subject + recipient) and wait for the user to "
            "name which to delete next."
        )
    _deletes_this_turn.set(n_so_far + 1)

    from backend.database import get_conn
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, user_id, kind, template_id, recipient, subject, "
            "       body_html, args_json, created_at, updated_at "
            "FROM compose_drafts WHERE id=?", (draft_id,),
        ).fetchone()
    if not row:
        raise ValueError(f"draft {draft_id} not found (already deleted?)")
    snapshot = dict(row)

    # Owner gate — a non-admin caller may only delete drafts they own.
    # Matches the HTTP endpoint's check (main.py compose_saved_draft_delete).
    caller_role = getattr(ctx, "role", None)
    caller_id = getattr(ctx, "user_id", None)
    if caller_role not in ("platform_admin", "admin") and snapshot["user_id"] != caller_id:
        raise PermissionError(
            f"draft {draft_id} belongs to a different user; only the owner "
            f"or an admin can delete it"
        )

    with get_conn() as conn:
        cur = conn.execute("DELETE FROM compose_drafts WHERE id=?", (draft_id,))
        deleted_count = cur.rowcount
        conn.commit()
    if deleted_count != 1:
        raise RuntimeError(f"expected to delete 1 row, deleted {deleted_count}")

    from backend.ui_tools import _append
    _append({
        "type":   "refresh_data",
        "table":  "compose_drafts",
        "reason": f"deleted draft: {snapshot.get('subject') or '(no subject)'}",
    })

    from backend import pending_actions as pa
    pa.confirm_then_apply(
        skill="delete_compose_draft",
        ctx=ctx,
        rollback_kind="restore_compose_draft",
        rollback_args={"snapshot": snapshot},
        preview={
            "action":    "delete",
            "draft_id":  draft_id,
            "subject":   snapshot.get("subject"),
            "recipient": snapshot.get("recipient"),
            "kind":      snapshot.get("kind"),
        },
    )

    return {
        "deleted_draft_id": draft_id,
        "draft":            {
            "subject":   snapshot.get("subject"),
            "recipient": snapshot.get("recipient"),
            "kind":      snapshot.get("kind"),
        },
    }
