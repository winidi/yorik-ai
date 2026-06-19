"""promote_pending_contact skill — pending → active."""
from __future__ import annotations
from typing import Any


async def execute(ctx, contact_id: int) -> dict[str, Any]:
    if not isinstance(contact_id, int) or contact_id <= 0:
        raise ValueError("contact_id must be a positive integer")

    from backend import contacts as C
    full = C.get(contact_id, include_children=False)
    if not full:
        raise ValueError(f"no such contact id={contact_id}")
    from backend.calendars import require_contact_access
    require_contact_access(
        getattr(ctx, "role", None),
        getattr(ctx, "user_id", None),
        full,
    )
    if full["status"] not in ("pending", "spam"):
        # Already active or archived — no-op but report cleanly so the LLM
        # doesn't keep retrying.
        return {"contact": full, "_llm_hint": f"contact is already status={full['status']!r}"}

    before = {"status": full["status"], "last_used_at": full.get("last_used_at")}
    C.promote_pending(contact_id)
    contact = C.get(contact_id)

    from backend.ui_tools import _append
    _append({"type": "refresh_data", "table": "contacts",
             "highlight_id": contact_id,
             "reason": f"promoted contact: {full['display_name']}"})

    from backend import pending_actions as pa
    if pa.should_confirm(ctx):
        pa.stage_with_rollback(
            skill="promote_pending_contact",
            rollback_kind="revert_contact_fields",
            rollback_args={"contact_id": contact_id, "before": before},
            preview={
                "action":       "promote",
                "contact_id":   contact_id,
                "display_name": full["display_name"],
                "from":         full["status"],
                "to":           "active",
            },
            ctx=ctx,
        )

    return {"contact": contact}
