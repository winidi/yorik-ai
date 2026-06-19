"""update_contact skill — apply-then-confirm UPDATE on contacts."""
from __future__ import annotations
from typing import Any, List, Optional


async def execute(
    ctx,
    contact_id: int,
    display_name: Optional[str] = None,
    aliases: Optional[List[str]] = None,
    kind: Optional[str] = None,
    relation: Optional[str] = None,
    birthday: Optional[str] = None,
    language_pref: Optional[str] = None,
    salutation_pref: Optional[str] = None,
    legal_name: Optional[str] = None,
    tax_id: Optional[str] = None,
    iban: Optional[str] = None,
    notes: Optional[str] = None,
    space: Optional[str] = None,
) -> dict[str, Any]:
    if not isinstance(contact_id, int) or contact_id <= 0:
        raise ValueError("contact_id must be a positive integer")

    from backend import contacts as C

    # Collect non-None fields
    patch: dict[str, Any] = {}
    for k, v in [
        ("display_name", display_name), ("aliases", aliases), ("kind", kind),
        ("relation", relation), ("birthday", birthday),
        ("language_pref", language_pref), ("salutation_pref", salutation_pref),
        ("legal_name", legal_name), ("tax_id", tax_id), ("iban", iban),
        ("notes", notes),
    ]:
        if v is not None:
            patch[k] = v
    # Phase B: optional `space` (slug or numeric id) reassigns the
    # contact to a different space. Resolved to space_id before update.
    if space is not None:
        from backend.database import conn_ctx, DEFAULT_DB_PATH as _DB
        s = str(space).strip().lower()
        with conn_ctx(_DB) as _c:
            if s.isdigit():
                r = _c.execute("SELECT id FROM spaces WHERE id=?", (int(s),)).fetchone()
            else:
                r = _c.execute("SELECT id FROM spaces WHERE LOWER(slug)=?", (s,)).fetchone()
        if r is None:
            raise ValueError(f"unknown space {space!r}")
        patch["space_id"] = int(r["id"])
    if not patch:
        raise ValueError("nothing to update — pass at least one field")

    # Capture pre-update snapshot for rollback (only the fields we'll touch).
    before_full = C.get(contact_id)
    if not before_full:
        raise ValueError(f"contact {contact_id} not found")

    from backend.calendars import require_contact_access
    require_contact_access(
        getattr(ctx, "role", None),
        getattr(ctx, "user_id", None),
        before_full,
    )

    before_clean = {k: before_full.get(k) for k in patch.keys()}

    C.update(contact_id, **patch)

    from backend.ui_tools import _append
    _append({"type": "refresh_data", "table": "contacts",
             "highlight_id": contact_id,
             "reason": f"updated contact: {before_full['display_name']}"})

    from backend import pending_actions as pa
    if pa.should_confirm(ctx):
        pa.stage_with_rollback(
            skill="update_contact",
            rollback_kind="revert_contact_fields",
            rollback_args={"contact_id": contact_id, "before": before_clean},
            preview={
                "action":     "update",
                "contact_id": contact_id,
                "before":     before_clean,
                "after":      patch,
            },
            ctx=ctx,
        )

    return {"contact": C.get(contact_id)}
