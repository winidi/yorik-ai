"""add_contact skill — apply-then-confirm INSERT on contacts."""
from __future__ import annotations
from typing import Any, List, Optional


async def execute(
    ctx,
    display_name: str,
    kind: str = "person",
    aliases: Optional[List[str]] = None,
    relation: Optional[str] = None,
    birthday: Optional[str] = None,
    language_pref: Optional[str] = None,
    salutation_pref: Optional[str] = None,
    legal_name: Optional[str] = None,
    tax_id: Optional[str] = None,
    iban: Optional[str] = None,
    notes: Optional[str] = None,
    status: str = "active",
    source: str = "manual",
    space: Optional[str] = None,
) -> dict[str, Any]:
    from backend import contacts as C
    from backend import spaces as _sp

    # Phase B: resolve sharing target. Explicit `space` (slug or id)
    # wins; otherwise read the per-household default-placement setting.
    # Default 'personal' means "the creator's personal space"; 'household'
    # / 'finance' / etc. picks a named shared space.
    space_id = None
    if space:
        from backend.database import conn_ctx, DEFAULT_DB_PATH as _DB
        s = str(space).strip().lower()
        with conn_ctx(_DB) as _c:
            if s.isdigit():
                r = _c.execute("SELECT id FROM spaces WHERE id=?", (int(s),)).fetchone()
            else:
                r = _c.execute("SELECT id FROM spaces WHERE LOWER(slug)=?", (s,)).fetchone()
        if r:
            space_id = int(r["id"])
    if space_id is None:
        from backend.household_settings import get_setting
        default_slug = get_setting("contacts_default_space", default="personal")
        if default_slug and default_slug != "personal":
            from backend.database import conn_ctx, DEFAULT_DB_PATH as _DB
            with conn_ctx(_DB) as _c:
                r = _c.execute(
                    "SELECT id FROM spaces WHERE LOWER(slug)=?", (default_slug.lower(),)
                ).fetchone()
                if r: space_id = int(r["id"])
    # contacts.create() falls back to creator's personal when space_id is None.

    contact_id = C.create(
        display_name=display_name,
        kind=kind,
        status=status,
        aliases=aliases,
        relation=relation,
        birthday=birthday,
        language_pref=language_pref,
        salutation_pref=salutation_pref,
        legal_name=legal_name,
        tax_id=tax_id,
        iban=iban,
        notes=notes,
        space_id=space_id,
        created_by_user_id=getattr(ctx, "user_id", None),
        source=source,
    )
    contact = C.get(contact_id)

    # Surface to UI so the contacts pane refreshes immediately.
    from backend.ui_tools import _append
    _append({"type": "refresh_data", "table": "contacts",
             "highlight_id": contact_id,
             "reason": f"added contact: {display_name}"})

    # Stage rollback for confirm-mode.
    from backend import pending_actions as pa
    if pa.should_confirm(ctx):
        pa.stage_with_rollback(
            skill="add_contact",
            rollback_kind="delete_contact",
            rollback_args={"contact_id": contact_id},
            preview={
                "action":       "create",
                "contact_id":   contact_id,
                "display_name": display_name,
                "kind":         kind,
                "status":       status,
                "relation":     relation,
            },
            ctx=ctx,
        )

    return {"contact_id": contact_id, "contact": contact}
