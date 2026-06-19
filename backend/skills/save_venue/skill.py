"""save_venue — remember a place the user looked up so the next
question about it is instant. Wraps add_contact (kind='business') +
add_contact_address + cached price-table on the contact's notes."""

from __future__ import annotations

import json
from typing import Any, Optional


def _parse_address(addr: str) -> dict[str, str]:
    """Best-effort split of '<street + nr>, <plz> <city>' into the
    contact_addresses schema. German formats; falls back to dumping
    everything into line1 if the pattern doesn't match."""
    if not addr:
        return {}
    parts = [p.strip() for p in addr.split(",") if p.strip()]
    if len(parts) == 1:
        return {"line1": parts[0]}
    # Try to split the last segment into "PLZ City".
    last = parts[-1].split()
    if len(last) >= 2 and last[0].isdigit() and len(last[0]) in (4, 5):
        return {
            "line1":    ", ".join(parts[:-1]),
            "postcode": last[0],
            "city":     " ".join(last[1:]),
        }
    return {"line1": addr}


async def execute(
    ctx,
    display_name: str,
    url: Optional[str] = None,
    category: Optional[str] = None,
    address: Optional[str] = None,
    phone: Optional[str] = None,
    email: Optional[str] = None,
    notes: Optional[str] = None,
    price_table: Optional[list] = None,
) -> dict[str, Any]:
    nm = (display_name or "").strip()
    if not nm:
        return {"_llm_hint": "save_venue requires `display_name`.", "ok": False}

    from backend import contacts as C

    # Composite notes: free-text + structured price_table (JSON-encoded
    # so it survives round-trips intact). The notes column stores the
    # canonical record; future skills can lift the price table from
    # there without re-LLM'ing.
    notes_parts: list[str] = []
    if notes and notes.strip():
        notes_parts.append(notes.strip())
    if url:
        notes_parts.append(f"Website: {url}")
    if category:
        notes_parts.append(f"Kategorie: {category}")
    if price_table:
        try:
            notes_parts.append(
                "Preise (Stand letzte Suche):\n"
                + json.dumps(price_table, ensure_ascii=False)
            )
        except Exception:
            pass
    final_notes = "\n\n".join(notes_parts) if notes_parts else None

    # Aliases: include the category as a searchable tag so
    # find_known_provider's substring match catches it next time.
    aliases = []
    if category:
        aliases.append(category)

    contact_id = C.create(
        display_name=nm,
        kind="business",
        status="active",
        relation=category or None,
        aliases=aliases or None,
        notes=final_notes,
        created_by_user_id=getattr(ctx, "user_id", None),
        source="auto",
    )

    addr_id: Optional[int] = None
    if address:
        ap = _parse_address(address)
        if ap:
            addr = C.add_address(contact_id, kind="work", **ap)
            addr_id = addr.get("id") if isinstance(addr, dict) else None
    if phone:
        try:
            C.add_channel(contact_id, kind="phone", value=phone)
        except Exception:
            pass
    if email:
        try:
            C.add_channel(contact_id, kind="email", value=email.lower())
        except Exception:
            pass

    # UI confirmation card.
    from backend.ui_tools import _append
    _append({
        "type":        "venue_saved",
        "contact_id":  contact_id,
        "name":        nm,
        "category":    category,
        "url":         url,
        "address":     address,
        "has_prices":  bool(price_table),
    })

    hint = (
        f"shown_to_user: saved {nm!r} as a known venue (contact id="
        f"{contact_id}). Next time the user asks about this place, "
        "find_known_provider returns it immediately. Reply ONE short "
        "sentence confirming the save."
    )
    return {
        "_llm_hint":   hint,
        "ok":          True,
        "contact_id":  contact_id,
        "name":        nm,
        "category":    category,
    }
