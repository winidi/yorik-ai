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
    # user ids are UUID strings since Phase E; an integer here is the
    # pre-Phase-E shape and can never match a row.
    if not isinstance(with_user_id, str) or not with_user_id.strip():
        raise ValueError("with_user_id must be the user's id (a UUID string)")
    with_user_id = with_user_id.strip()

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

    from backend.database import get_conn
    with get_conn() as conn:
        # Verify the recipient is a real household user.
        recipient = conn.execute(
            "SELECT id, name FROM user_profiles WHERE id = ?",
            (with_user_id,),
        ).fetchone()
    if not recipient:
        raise ValueError(f"no such household user id={with_user_id}")

    # Someone else gets to see (or edit) this contact: the person
    # confirms on a card first, like a delete (s18, Dirk 2026-09-30).
    from backend import pending_actions as pa
    recipient_name = recipient["name"] or with_user_id
    pending_id = pa.stage_before_apply(
        skill="share_contact",
        ctx=ctx,
        apply_kind="share_contact",
        apply_args={"contact_id": int(contact_id), "with_user_id": with_user_id,
                    "can_edit": bool(can_edit), "sharer_id": getattr(ctx, "user_id", None),
                    "display_name": pre["display_name"], "with_name": recipient_name},
        params={"contact_id": int(contact_id), "with_user_id": with_user_id},
        preview={"action": "share", "contact": {"id": int(contact_id), "display_name": pre["display_name"]},
                 "with_name": recipient_name, "can_edit": bool(can_edit)},
    )
    return {
        "pending":      True,
        "pending_id":   pending_id,
        "contact_id":   int(contact_id),
        "with_user_id": with_user_id,
        "_llm_hint": (
            f"shown_to_user: a confirmation card for sharing '{pre['display_name']}' with {recipient_name} "
            "is on screen. NOTHING is shared yet — it happens only when the user taps Share on the card. "
            "Tell the user the card is waiting for their confirmation; do not claim the contact is shared."
        ),
    }


def apply_share(args: dict) -> dict[str, Any]:
    """The confirmed share — called from pending_actions.apply. Per-row
    sharing lives in row_shares (table_name='contacts', level 'write'
    for editable shares, 'read' otherwise); spaces.can_view_row /
    can_write_row consult it."""
    from backend.database import get_conn
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO row_shares "
            "  (table_name, row_id, user_id, level, shared_by_user_id) "
            "VALUES ('contacts', ?, ?, ?, ?) "
            "ON CONFLICT(table_name, row_id, user_id) DO UPDATE SET "
            "  level = excluded.level, "
            "  shared_at = datetime('now'), "
            "  shared_by_user_id = excluded.shared_by_user_id",
            (int(args["contact_id"]), args["with_user_id"],
             "write" if args.get("can_edit") else "read", args.get("sharer_id")),
        )
        conn.commit()
    from backend.ui_tools import _append
    _append({"type": "refresh_data", "table": "contacts",
             "highlight_id": int(args["contact_id"]),
             "reason": f"shared contact: {args.get('display_name')}"})
    return {
        "contact_id":   int(args["contact_id"]),
        "with_user_id": args["with_user_id"],
        "can_edit":     bool(args.get("can_edit")),
    }
