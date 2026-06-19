"""add_contact_address skill — attach a postal address to a contact."""
from __future__ import annotations
from typing import Any, Optional


async def execute(
    ctx,
    contact_id: int,
    kind: str = "home",
    line1: Optional[str] = None,
    line2: Optional[str] = None,
    postcode: Optional[str] = None,
    city: Optional[str] = None,
    region: Optional[str] = None,
    country: Optional[str] = None,
    label: Optional[str] = None,
) -> dict[str, Any]:
    if not isinstance(contact_id, int) or contact_id <= 0:
        raise ValueError("contact_id must be a positive integer")

    from backend import contacts as C
    pre = C.get(contact_id, include_children=False)
    if not pre:
        raise ValueError(f"no such contact id={contact_id}")
    from backend.calendars import require_contact_access
    require_contact_access(
        getattr(ctx, "role", None),
        getattr(ctx, "user_id", None),
        pre,
    )

    address_id = C.add_address(
        contact_id, kind=kind, line1=line1, line2=line2,
        postcode=postcode, city=city, region=region, country=country,
        label=label, source="manual",
    )

    from backend.ui_tools import _append
    _append({"type": "refresh_data", "table": "contacts",
             "highlight_id": contact_id,
             "reason": f"added {kind} address"})

    from backend import pending_actions as pa
    if pa.should_confirm(ctx):
        pa.stage_with_rollback(
            skill="add_contact_address",
            rollback_kind="remove_contact_address",
            rollback_args={"address_id": address_id},
            preview={
                "action":     "add_address",
                "contact_id": contact_id,
                "kind":       kind,
                "line1":      line1, "postcode": postcode, "city": city,
                "country":    country,
            },
            ctx=ctx,
        )

    return {"address_id": address_id, "contact": C.get(contact_id)}
