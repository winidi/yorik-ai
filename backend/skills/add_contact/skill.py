"""add_contact skill — one-shot create with channels + address.

Historically split into add_contact + add_contact_channel +
add_contact_address. The 3-skill chain failed in practice because
the LLM frequently called only step 1 and dumped email/address into
the notes field. One-shot create accepts everything in a single
tool call. The granular skills stay around for EDIT cases (adding
a second email / address to an existing contact).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


async def execute(
    ctx,
    display_name: str,
    kind: str = "person",
    # Person identity (mig 045). The agent used to stuff "Stefan Meier"
    # into display_name only and the job title into `relation`; now
    # there's a proper place for each.
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    role: Optional[str] = None,
    employer_contact_id: Optional[int] = None,
    # Identity / metadata.
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
    # NEW one-shot channel + address arguments. All optional; the
    # skill still works as a name-only create when these aren't
    # provided.
    emails: Optional[List[str]] = None,
    phones: Optional[List[str]] = None,
    whatsapp_jids: Optional[List[str]] = None,
    websites: Optional[List[str]] = None,
    address: Optional[Dict[str, Optional[str]]] = None,
) -> Dict[str, Any]:
    from backend import contacts as C
    from backend import spaces as _sp  # noqa: F401  (used implicitly via household_settings)

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
        first_name=first_name,
        last_name=last_name,
        role=role,
        employer_contact_id=employer_contact_id,
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

    # Best-effort transactional: any channel / address insert failure
    # rolls the entire create back (delete cascades children) so the
    # agent never leaves a half-formed row behind.
    try:
        if emails:
            for e in emails:
                e = (e or "").strip()
                if not e:
                    continue
                C.add_channel(contact_id, kind="email", value=e, source=source)
        if phones:
            for p in phones:
                p = (p or "").strip()
                if not p:
                    continue
                C.add_channel(contact_id, kind="phone", value=p, source=source)
        if whatsapp_jids:
            for j in whatsapp_jids:
                j = (j or "").strip()
                if not j:
                    continue
                C.add_channel(contact_id, kind="whatsapp", value=j, source=source)
        if websites:
            for w in websites:
                w = (w or "").strip()
                if not w:
                    continue
                C.add_channel(contact_id, kind="website", value=w, source=source)
        if address:
            # Skip when the dict is all-null — the agent sometimes
            # passes an empty address shell.
            if any(address.get(k) for k in ("line1", "line2", "postcode", "city")):
                C.add_address(
                    contact_id,
                    kind=str(address.get("kind") or "home"),
                    line1=address.get("line1"),
                    line2=address.get("line2"),
                    postcode=address.get("postcode"),
                    city=address.get("city"),
                    region=address.get("region"),
                    country=address.get("country"),
                    label=address.get("label"),
                    source=source,
                )
    except Exception:
        # Rollback: delete the contact (cascades to channels + addresses).
        try:
            C.delete(contact_id)
        except Exception:
            pass
        raise

    contact = C.get(contact_id)

    # Surface to UI so the contacts pane refreshes immediately.
    from backend.ui_tools import _append
    _append({"type": "refresh_data", "table": "contacts",
             "highlight_id": contact_id,
             "reason": f"added contact: {display_name}"})

    # Stage rollback for confirm-mode. delete_contact already cascades
    # channels + addresses, so the existing rollback args still work
    # for the one-shot path.
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
                "role":         role,
                "relation":     relation,
            },
            ctx=ctx,
        )

    return {"contact_id": contact_id, "contact": contact}
