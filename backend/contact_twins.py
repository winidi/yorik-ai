"""WhatsApp twins: one person, two contact rows.

WhatsApp addresses a person by number (``4917…@s.whatsapp.net``) and by
an opaque LID (``2664…@lid``). The sync created a contact row for each
address it saw, so one person became two or three contacts ("Beate",
"Beate <3", "Beate"). The bridge's alias map says which LID belongs to
which number (``whatsapp_aliases``). This module turns each such pair
into a *merge proposal*; a person accepts it in Contacts, or not. Nothing
merges by itself (Dirk, 2026-10-16: Yorik suggests, the human decides).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("yorik.contact_twins")


def _row(kind: str, value: str) -> Optional[Dict[str, Any]]:
    from . import contacts as _contacts
    try:
        hit = _contacts.find_by_channel(kind, value)
    except Exception:  # noqa: BLE001
        return None
    if not hit or hit.get("merged_into_id") or hit.get("status") == "spam":
        return None
    return hit


def find_twins(user_id: str) -> List[Tuple[Dict[str, Any], Dict[str, Any], str, str]]:
    """(number_row, lid_row, number_jid, lid_jid) for every LID ↔ number
    pair of this person's WhatsApp that lives in two different contacts."""
    from . import whatsapp_aliases as _al
    from . import contact_identity as _ident
    pairs: List[Tuple[Dict[str, Any], Dict[str, Any], str, str]] = []
    seen: set = set()
    for lid, number in (_al.lid_to_number(user_id) or {}).items():
        if not lid.endswith("@lid") or not number.endswith("@s.whatsapp.net"):
            continue
        lid_row = _row("whatsapp", lid)
        if not lid_row:
            continue
        number_row = _row("whatsapp", number)
        if not number_row:
            e164 = _ident.to_e164(number)
            number_row = _row("phone", e164) if e164 else None
        if not number_row or int(number_row["id"]) == int(lid_row["id"]):
            continue
        key = tuple(sorted((int(number_row["id"]), int(lid_row["id"]))))
        if key in seen:
            continue
        seen.add(key)
        pairs.append((number_row, lid_row, number, lid))
    return pairs


def propose_twins(user_id: str) -> Dict[str, Any]:
    """Write one merge proposal per twin pair (the number row is the one
    to keep by default). Returns counts; existing pending proposals for a
    pair are not duplicated."""
    from . import contact_identity as _ident
    pairs = find_twins(user_id)
    created = 0
    for number_row, lid_row, number, lid in pairs:
        digits = number.split("@")[0]
        pid = _ident.propose(
            kind=_ident.PROPOSAL_MERGE,
            contact_id=int(number_row["id"]),
            other_contact_id=int(lid_row["id"]),
            reason=f"WhatsApp says {lid_row.get('display_name')!r} is the same account as +{digits} ({number_row.get('display_name')!r})",
            evidence={"source": "whatsapp_alias_map", "lid": lid, "number": number},
            confidence=0.95,
            user_id=str(user_id),
        )
        if pid:
            created += 1
    if created:
        log.info("contact_twins: %d twin proposal(s) for user %s", created, user_id)
    return {"pairs": len(pairs), "proposals": created}


def twins_among(user_id: str, contact_ids: List[int]) -> List[Tuple[int, int]]:
    """Pairs among `contact_ids` that the alias map says are one WhatsApp
    account — for the chat, which then asks once instead of showing both."""
    ids = {int(i) for i in contact_ids if i is not None}
    out: List[Tuple[int, int]] = []
    for number_row, lid_row, _n, _l in find_twins(user_id):
        a, b = int(number_row["id"]), int(lid_row["id"])
        if a in ids and b in ids:
            out.append((a, b))
    return out
