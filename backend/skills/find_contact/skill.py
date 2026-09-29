"""find_contact — internal helper, NOT a registered skill.

The skill registry only loads dirs that have BOTH skill.md AND skill.py.
This module ships skill.py only — intentional so the LLM never sees
`find_contact` in its tool catalog (the unified `find_person` skill is
the public surface).

`find_person/skill.py` imports `execute()` below to do the actual
contacts-table lookup with picker-UI emission. Deleting this file
breaks find_person at runtime; don't.
"""
from __future__ import annotations
from typing import Any, List, Optional


def _first_address_line(addresses: Optional[List[dict]]) -> Optional[str]:
    """Format the first postal address as a single line for the picker
    card subline: "Karlstraße 102, 38106 Braunschweig"."""
    if not addresses:
        return None
    a = addresses[0] or {}
    bits = []
    street = (a.get("street") or a.get("line1") or "").strip()
    if street:
        bits.append(street)
    pc = (a.get("postcode") or "").strip()
    city = (a.get("city") or "").strip()
    locality = " ".join(b for b in (pc, city) if b)
    if locality:
        bits.append(locality)
    return ", ".join(bits) or None


def _last_contact(contacts: List[dict], user_id: Any) -> dict:
    """{contact_id: unix time of the latest WhatsApp or mail exchange}."""
    from datetime import datetime
    from backend.database import get_conn
    try:
        from backend.whatsapp_aliases import same_chat
    except Exception:  # noqa: BLE001
        same_chat = lambda _u, j: [j]  # noqa: E731
    out: dict = {}
    with get_conn() as conn:
        for c in contacts:
            best = 0
            for ch in c.get("channels") or []:
                v = (ch.get("value") or "").strip()
                if not v:
                    continue
                try:
                    if ch.get("kind") == "whatsapp":
                        jids = same_chat(str(user_id), v.lower() if "@" in v else
                                         f"{''.join(x for x in v if x.isdigit())}@s.whatsapp.net")
                        marks = ",".join("?" * len(jids))
                        row = conn.execute(f"SELECT MAX(timestamp) AS t FROM wa_messages WHERE chat_jid IN ({marks}) "
                                           "AND owner_user_id = ?", (*jids, user_id)).fetchone()
                        best = max(best, int(row["t"] or 0))
                    elif ch.get("kind") == "email":
                        row = conn.execute("SELECT MAX(COALESCE(date_received, date_sent)) AS t FROM email_messages "
                                           "WHERE LOWER(from_email) = ? AND owner_user_id = ?",
                                           (v.lower(), user_id)).fetchone()
                        t = row["t"] if row else None
                        if t:
                            best = max(best, int(datetime.fromisoformat(str(t).replace("Z", "+00:00")).timestamp()))
                except Exception:  # noqa: BLE001 — ranking is a nicety
                    continue
            if best:
                out[int(c["id"])] = best
    return out


async def execute(
    ctx,
    query: Optional[str] = None,
    kind: Optional[str] = None,
    status: Optional[str] = "any",
    channel_kind: Optional[str] = None,
    channel_value: Optional[str] = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Search behavior — default is `status='any'` so both active and
    pending contacts surface. The LLM was hallucinating "I can't find
    Hans" when Hans existed but was pending; broadening the default
    catches that case without a retry.

    Excludes by default: status='spam' (intentionally suppressed) and
    status='archived' (user-hidden). Pass status='spam' or 'archived'
    explicitly to include them — useful for "is this number blocked?"
    or "I deleted them — bring them back" queries.
    """
    from backend import contacts as C

    # Phase 9.4: gate every lookup to what THIS caller can see — owner,
    # admin, role-allowlist, or per-user share. Without this, find_contact
    # returns rows that update_contact then refuses, which is worse UX
    # than just not surfacing them.
    role = getattr(ctx, "role", None)
    user_id = getattr(ctx, "user_id", None)

    # Channel lookup wins when present — one indexed hit instead of a fuzzy scan.
    if channel_kind and channel_value:
        hit = C.find_by_channel(channel_kind, channel_value)
        if not hit:
            return {"_llm_hint": "no contact matched", "contacts": [], "ambiguous": False}
        full = C.get(hit["id"], role=role, user_id=user_id)
        if not full:
            # Caller doesn't have visibility on the channel-hit row.
            return {"_llm_hint": "no contact matched", "contacts": [], "ambiguous": False}
        return {
            "_llm_hint": f"matched {full['display_name']} via {channel_kind}",
            "contacts": [full],
            "ambiguous": False,
        }

    # Name/alias search. status='any' (default) returns active+pending
    # but skips spam/archived. Caller can override.
    if not status or status == "any":
        rows = []
        for s in ("active", "pending"):
            rows += C.search(query or "", kind=kind, status=s,
                              limit=int(limit), role=role, user_id=user_id)
    else:
        rows = C.search(query or "", kind=kind, status=status,
                         limit=int(limit), role=role, user_id=user_id)
    # search() already filtered by visibility; get() with the same gate
    # is belt-and-suspenders against the rare edge case where a share
    # is revoked between the SELECT and the hydration.
    hydrated = [C.get(r["id"], role=role, user_id=user_id) for r in rows]
    hydrated = [h for h in hydrated if h is not None]
    # The same person twice — once by number, once as a bare WhatsApp
    # LID — is one candidate, not two (contacts.lid_twins).
    _twins = C.lid_twins(hydrated, user_id)
    hydrated = [h for h in hydrated if int(h["id"]) not in _twins]
    # Several matches: the one the person wrote with last comes first
    # (Dirk 2026-09-29: of seven Beates, the one I talk to is on top).
    if len(hydrated) > 1:
        _recent = _last_contact(hydrated, user_id)
        for h in hydrated:
            h["last_contact_ts"] = _recent.get(int(h["id"]))
        hydrated.sort(key=lambda h: h.get("last_contact_ts") or 0, reverse=True)

    # Ambiguity hint — when >1 candidate matches a name query, the LLM
    # must STOP and ask the user which one. Auto-picking the first match
    # is what landed a message to "Tom" on the user's brother. The hint
    # text embeds the actual chat_jid / email / contact_id INLINE (not
    # just in JSON) so even an inattentive model can grab them in turn 2
    # without re-asking or re-calling. Qwen3 specifically tends to ignore
    # structured tool result fields and read only the hint string.
    if len(hydrated) == 0:
        hint = (
            f"no direct match for query={query!r}. "
            "NEXT STEP: call list_contacts_for_picking — it returns the "
            "FULL address book (~30 chars per contact). Scan it with "
            "natural-language matching, in any language: 'Grandma' might be "
            "relation='grandmother', 'the plumber' a business, 'my brother' the "
            "relation field. Only after THAT comes back empty too, ask "
            "the user."
        )
    elif len(hydrated) == 1:
        c = hydrated[0]
        wa_jid = next((ch["value"] for ch in (c.get("channels") or [])
                        if ch["kind"] == "whatsapp"), None)
        email = next((ch["value"] for ch in (c.get("channels") or [])
                       if ch["kind"] == "email"), None)
        addrs = c.get("addresses") or []
        # Pick best address for display — home > work > billing > shipping.
        _order = {"home": 0, "work": 1, "billing": 2, "shipping": 3}
        best_addr = sorted(
            addrs, key=lambda a: _order.get((a.get("kind") or "").lower(), 9)
        )[0] if addrs else None
        has_postal = bool(
            best_addr and (best_addr.get("line1") or "").strip()
            and (best_addr.get("city") or "").strip()
        )

        bits = [f"id={c['id']}"]
        if wa_jid: bits.append(f"chat_jid={wa_jid}")
        if email:  bits.append(f"email={email}")
        if has_postal:
            bits.append(
                f"postal_address='{best_addr['line1']}, "
                f"{(best_addr.get('postcode') or '').strip()} "
                f"{best_addr['city']}' (on file)"
            )
        else:
            bits.append("postal_address=NONE on file")

        postal_hint = (
            "  • For a POSTAL letter (compose_draft with template_id): "
            "call compose_check_recipient(contact_id, template_id) FIRST. "
            "It returns complete=true/false and the exact missing fields. "
            "Do NOT call compose_draft directly when address presence is "
            "unclear — that's how the placeholder loop happens."
            if not has_postal else
            "  • For a POSTAL letter (compose_draft with template_id): "
            "the address above will be filled into the recipient block "
            "automatically. Proceed straight to compose_draft."
        )

        hint = (
            f"single match: {c['display_name']} ({', '.join(bits)}).\n"
            "Use these values directly in the next skill call — do NOT ask "
            "the user to repeat them.\n"
            + postal_hint
        )
    else:
        lines: list[str] = []
        for c in hydrated[:5]:
            wa_jid = next((ch["value"] for ch in (c.get("channels") or [])
                            if ch["kind"] == "whatsapp"), None)
            email = next((ch["value"] for ch in (c.get("channels") or [])
                           if ch["kind"] == "email"), None)
            phone = next((ch["value"] for ch in (c.get("channels") or [])
                           if ch["kind"] == "phone"), None)
            tag_parts: list[str] = []
            if c.get("relation"): tag_parts.append(c["relation"])
            if wa_jid:
                tag_parts.append(f"chat_jid={wa_jid}")
            if email:
                tag_parts.append(f"email={email}")
            if phone and not wa_jid:
                tag_parts.append(f"phone={phone}")
            lines.append(
                f"  • id={c['id']} {c['display_name']} — "
                + ", ".join(tag_parts)
            )
        hint = (
            f"AMBIGUOUS — {len(hydrated)} candidates matched query={query!r}:\n"
            + "\n".join(lines)
            + "\n\nNEXT STEP (mandatory):\n"
            "  1. STOP. Do NOT call whatsapp_draft/email_draft/compose_draft yet.\n"
            "  2. The chat has auto-rendered a contact picker card; reply "
            "EXACTLY one short question naming the entity ('Welcher Joel?', "
            "'Which Anna?') — no list, no enumeration, no recap of names.\n"
            "  3. When the user disambiguates: pull the chat_jid / email / "
            "contact_id directly from the candidate list ABOVE — every value "
            "you need is RIGHT HERE in this hint. Do NOT ask them for the "
            "number; do NOT claim 'I don't have the chat ID' — you have it.\n"
            "  4. If you're not 100% sure which candidate matches the "
            "disambiguation, re-call find_contact with the more specific "
            "query (e.g. 'Hans Neue Nummer' instead of 'Hans')."
        )
        # Emit a contact picker card so the user clicks instead of typing.
        # Same pattern as pois_found from find_provider_nearby. The chat
        # picks up the click and seeds a follow-up message identifying
        # the chosen contact by id, which the LLM then routes correctly.
        from backend.ui_tools import _append
        _append({
            "type":     "contact_picker",
            "query":    query or "",
            "contacts": [
                {
                    "id":            c["id"],
                    "display_name":  c["display_name"],
                    "relation":      c.get("relation") or "",
                    "kind":          c.get("kind") or "person",
                    # First channel of each kind, if any — surface enough
                    # so the user can distinguish identical-named entries.
                    "email": next((ch["value"] for ch in (c.get("channels") or [])
                                   if ch["kind"] == "email"), None),
                    "phone": next((ch["value"] for ch in (c.get("channels") or [])
                                   if ch["kind"] == "phone"), None),
                    "whatsapp": next((ch["value"] for ch in (c.get("channels") or [])
                                       if ch["kind"] == "whatsapp"), None),
                    "last_contact_ts": c.get("last_contact_ts"),
                    # First postal address one-lined. Powers the picker's
                    # address subline AND lets downstream skills
                    # (add_calendar_event, Anfahrt routing) reuse the
                    # location without another lookup.
                    "address": _first_address_line(c.get("addresses")),
                }
                for c in hydrated[:10]  # cap at 10; rare for more to be relevant
            ],
        })
    return {"_llm_hint": hint, "contacts": hydrated, "ambiguous": len(hydrated) > 1}
