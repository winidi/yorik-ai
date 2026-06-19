"""mark_contact_spam skill — anything → spam."""
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
    if full["status"] == "spam":
        return {"contact": full, "_llm_hint": "already spam"}

    before = {"status": full["status"]}
    C.mark_spam(contact_id)
    contact = C.get(contact_id)

    from backend.ui_tools import _append
    _append({"type": "refresh_data", "table": "contacts",
             "highlight_id": contact_id,
             "reason": f"marked spam: {full['display_name']}"})

    from backend import pending_actions as pa
    if pa.should_confirm(ctx):
        pa.stage_with_rollback(
            skill="mark_contact_spam",
            rollback_kind="revert_contact_fields",
            rollback_args={"contact_id": contact_id, "before": before},
            preview={
                "action":       "mark_spam",
                "contact_id":   contact_id,
                "display_name": full["display_name"],
                "from":         full["status"],
            },
            ctx=ctx,
        )

    return {"contact": contact}
