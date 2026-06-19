"""share_contact skill — per-user contact ACL grant."""
from __future__ import annotations
from typing import Any


async def execute(
    ctx,
    contact_id: int,
    with_user_id: str,
    can_edit: bool = True,
) -> dict[str, Any]:
    if not isinstance(contact_id, int) or contact_id <= 0:
        raise ValueError("contact_id must be a positive integer")
    if not isinstance(with_user_id, int) or with_user_id <= 0:
        raise ValueError("with_user_id must be a positive integer")

    from backend import contacts as C
    pre = C.get(contact_id, include_children=False)
    if not pre:
        raise ValueError(f"no such contact id={contact_id}")

    # Only the owner (or admin) may grant shares. Use the strict
    # require_row_owner_or_admin here — NOT require_contact_access —
    # because a member who only has access via an allowed_role or a
    # share-from-someone-else shouldn't be able to grant onwards.
    from backend.calendars import require_row_owner_or_admin
    require_row_owner_or_admin(
        getattr(ctx, "role", None),
        getattr(ctx, "user_id", None),
        pre,
        subject="contact",
        owner_col="created_by_user_id",
    )

    sharer_id = getattr(ctx, "user_id", None)
    from backend.database import get_conn
    with get_conn() as conn:
        # Verify the recipient is a real household user.
        recipient = conn.execute(
            "SELECT id FROM user_profiles WHERE id = ?",
            (with_user_id,),
        ).fetchone()
        if not recipient:
            raise ValueError(f"no such household user id={with_user_id}")
        # Phase B: contact_shares table is gone; per-row sharing now
        # lives in the polymorphic row_shares table (table_name='contacts',
        # row_id=contact_id, level='write' for editable shares, 'read'
        # otherwise). spaces.can_view_row / can_write_row consult it.
        conn.execute(
            "INSERT INTO row_shares "
            "  (table_name, row_id, user_id, level, shared_by_user_id) "
            "VALUES ('contacts', ?, ?, ?, ?) "
            "ON CONFLICT(table_name, row_id, user_id) DO UPDATE SET "
            "  level = excluded.level, "
            "  shared_at = datetime('now'), "
            "  shared_by_user_id = excluded.shared_by_user_id",
            (
                int(contact_id),
                with_user_id,
                "write" if can_edit else "read",
                sharer_id,
            ),
        )
        conn.commit()

    from backend.ui_tools import _append
    _append({"type": "refresh_data", "table": "contacts",
             "highlight_id": contact_id,
             "reason": f"shared contact: {pre['display_name']}"})

    return {
        "contact_id":   contact_id,
        "with_user_id": with_user_id,
        "can_edit":     bool(can_edit),
    }
