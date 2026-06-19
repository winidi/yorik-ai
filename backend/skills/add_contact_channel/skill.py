"""add_contact_channel skill — attach an email/phone/whatsapp/etc. to a contact."""
from __future__ import annotations
import sqlite3
from typing import Any, Optional


async def execute(
    ctx,
    contact_id: int,
    kind: str,
    value: str,
    label: Optional[str] = None,
) -> dict[str, Any]:
    if not isinstance(contact_id, int) or contact_id <= 0:
        raise ValueError("contact_id must be a positive integer")
    if not kind or not value or not value.strip():
        raise ValueError("kind and value are both required")

    from backend import contacts as C

    # Contact must exist + caller must own it (or be admin)
    pre = C.get(contact_id, include_children=False)
    if not pre:
        raise ValueError(f"no such contact id={contact_id}")
    from backend.calendars import require_contact_access
    require_contact_access(
        getattr(ctx, "role", None),
        getattr(ctx, "user_id", None),
        pre,
    )

    try:
        channel_id = C.add_channel(
            contact_id, kind=kind, value=value, label=label, source="manual",
        )
    except sqlite3.IntegrityError:
        # UNIQUE(kind, value) violation — surface which contact already owns it.
        existing = C.find_by_channel(kind, value)
        owner = existing["display_name"] if existing else "(unknown)"
        raise ValueError(
            f"channel {kind}={value!r} is already linked to contact "
            f"{owner!r} (id={existing['id'] if existing else '?'}). "
            f"Pick that contact instead of creating a duplicate, or remove "
            f"the channel from the other contact first."
        )

    from backend.ui_tools import _append
    _append({"type": "refresh_data", "table": "contacts",
             "highlight_id": contact_id,
             "reason": f"added {kind} channel"})

    from backend import pending_actions as pa
    if pa.should_confirm(ctx):
        pa.stage_with_rollback(
            skill="add_contact_channel",
            rollback_kind="remove_contact_channel",
            rollback_args={"channel_id": channel_id},
            preview={
                "action":     "add_channel",
                "contact_id": contact_id,
                "kind":       kind,
                "value":      value,
                "label":      label,
            },
            ctx=ctx,
        )

    return {"channel_id": channel_id, "contact": C.get(contact_id)}
