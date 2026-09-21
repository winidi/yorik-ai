"""Who a document goes to: {contact_id, name, address_lines, email}.
Resolved from the contacts when possible, typed by hand otherwise. A
name nobody knows is still a recipient — the sheet then marks the
missing address instead of anybody being asked for it."""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def clean(raw: Any) -> Dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    lines = raw.get("address_lines")
    if isinstance(lines, str):
        lines = lines.splitlines()
    out: Dict[str, Any] = {
        "name": str(raw.get("name") or "").strip()[:160],
        "address_lines": [str(x).strip()[:120] for x in (lines or []) if str(x).strip()][:5],
        "email": str(raw.get("email") or "").strip()[:200],
    }
    try:
        out["contact_id"] = int(raw["contact_id"]) if raw.get("contact_id") not in (None, "") else None
    except (TypeError, ValueError):
        out["contact_id"] = None
    return out


def from_contact(contact: Dict[str, Any], *, sender_country: str = "DE") -> Dict[str, Any]:
    name = str(contact.get("legal_name") or contact.get("display_name") or "").strip()
    addresses = contact.get("addresses") or []
    # a letter goes to the billing or work address of a business, home of a person
    order = {"billing": 0, "work": 1, "home": 2} if contact.get("kind") == "business" else {"home": 0, "billing": 1, "work": 2}
    addresses = sorted(addresses, key=lambda a: order.get(a.get("kind") or "", 9))
    lines: List[str] = []
    if addresses:
        a = addresses[0]
        lines = [str(a.get("line1") or "").strip(), str(a.get("line2") or "").strip(),
                 f"{a.get('postcode') or ''} {a.get('city') or ''}".strip()]
        country = str(a.get("country") or "").strip()
        if country and country.upper() != (sender_country or "DE").upper():
            lines.append(country)
    email = next((str(c.get("value") or "") for c in (contact.get("channels") or []) if c.get("kind") == "email"), "")
    return clean({"contact_id": contact.get("id"), "name": name, "address_lines": lines, "email": email})


def resolve(query: str, *, role: Optional[str], user_id: Optional[str], sender_country: str = "DE") -> Dict[str, Any]:
    """The contact a name means, if it is clear; else just the name."""
    from .. import contacts as _contacts
    name = (query or "").strip()
    if not name:
        return clean({})
    try:
        hits = _contacts.search(name, limit=5, role=role, user_id=user_id)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 — a letter never fails on the address book
        hits = []
    low = name.lower()
    exact = [h for h in hits if low in (str(h.get("display_name") or "").lower(), str(h.get("legal_name") or "").lower())]
    pick = exact[0] if exact else (hits[0] if len(hits) == 1 else None)
    if not pick:
        return clean({"name": name})
    full = _contacts.get(int(pick["id"]), role=role, user_id=user_id) or pick  # type: ignore[arg-type]
    return from_contact(full, sender_country=sender_country)
