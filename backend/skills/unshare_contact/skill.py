"""unshare_contact skill — per-user contact ACL revoke."""
from __future__ import annotations
from typing import Any


async def execute(
    ctx,
    contact_id: int,
    with_user_id: str,
) -> dict[str, Any]:
    if not isinstance(contact_id, int) or contact_id <= 0:
        raise ValueError("contact_id must be a positive integer")
    if not isinstance(with_user_id, int) or with_user_id <= 0:
        raise ValueError("with_user_id must be a positive integer")

    from backend import contacts as C
    pre = C.get(contact_id, include_children=False)
    if not pre:
        raise ValueError(f"no such contact id={contact_id}")

    # Strict owner-only — symmetric with share_contact.
    from backend.calendars import require_row_owner_or_admin
    require_row_owner_or_admin(
        getattr(ctx, "role", None),
        getattr(ctx, "user_id", None),
        pre,
        subject="contact",
        owner_col="created_by_user_id",
    )

    from backend.database import get_conn
    with get_conn() as conn:
        # Phase B: contact_shares table is gone; per-row sharing now
        # lives in row_shares (see share_contact for the mirror INSERT).
        cur = conn.execute(
            "DELETE FROM row_shares "
            "WHERE table_name='contacts' AND row_id = ? AND user_id = ?",
            (int(contact_id), with_user_id),
        )
        removed = cur.rowcount > 0
        conn.commit()

    if removed:
        from backend.ui_tools import _append
        _append({"type": "refresh_data", "table": "contacts",
                 "highlight_id": contact_id,
                 "reason": f"unshared contact: {pre['display_name']}"})

    return {
        "contact_id":   contact_id,
        "with_user_id": with_user_id,
        "removed":      removed,
    }
