"""list_contacts_for_picking — compact full-address-book dump.

The find_contact skill does an SQL substring/word-boundary search.
That's cheap and right for precise lookups ("Müller GmbH"), but it
misses cases where the connection between the user's word and the
stored contact requires natural-language reasoning:
  - "Oma" → match by relation, not name
  - "der Klempner" → match by trade tag
  - "mein Steuerberater" → match by relation
  - nicknames the user never added as aliases

For those, dumping the whole address book and letting the LLM pick
is more reliable than ever-fancier SQL. At ≤1000 contacts the
payload (id + name + relation + kind, ~30 chars/row) is ~30kb —
fits comfortably in qwen3's context window.

Keep the payload TIGHT — no channels, no addresses, no notes.
Those come via find_contact(channel_*) or get-by-id once the LLM
has picked.

Two-mode shape (the fallback flow is a two-call dance on the SAME skill,
instead of a separate present_ranked_contacts skill that bloats the
skill_index every turn):

  Call 1 — list_contacts_for_picking(defer_card=true)
    → returns the full list, suppresses the alphabetical card
  Call 2 — list_contacts_for_picking(ranked_picks=[{id, confidence, reason}, ...], query="<phrase>")
    → skips SQL entirely, hydrates the supplied picks, emits the ranked
      contact_picker card
"""

from __future__ import annotations

from typing import Any, List, Optional


def _first_address_line(addresses: Optional[List[dict]]) -> Optional[str]:
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


async def _render_ranked(
    ctx,
    query: str,
    ranked_picks: List[dict],
) -> dict[str, Any]:
    """Render-mode: hydrate the LLM's ranked picks and emit a ranked
    contact_picker card. Mirrors the visibility gate the browse path
    uses — picks the caller can't see are dropped."""
    from backend import contacts as C

    role = getattr(ctx, "role", None) if ctx else None
    user_id = getattr(ctx, "user_id", None) if ctx else None

    def _conf(p: dict) -> float:
        try:
            return float(p.get("confidence") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    submitted = list(ranked_picks or [])
    ordered = sorted(submitted, key=_conf, reverse=True)[:10]

    hydrated: list[dict[str, Any]] = []
    dropped: list[int] = []
    for p in ordered:
        try:
            cid = int(p.get("id"))
        except (TypeError, ValueError):
            continue
        full = C.get(cid, role=role, user_id=user_id)
        if not full:
            dropped.append(cid)
            continue
        channels = full.get("channels") or []
        hydrated.append({
            "id":            full["id"],
            "display_name":  full["display_name"],
            "relation":      full.get("relation") or "",
            "kind":          full.get("kind") or "person",
            "email":    next((ch["value"] for ch in channels if ch["kind"] == "email"), None),
            "phone":    next((ch["value"] for ch in channels if ch["kind"] == "phone"), None),
            "whatsapp": next((ch["value"] for ch in channels if ch["kind"] == "whatsapp"), None),
            "address":  _first_address_line(full.get("addresses")),
            "confidence": max(0.0, min(1.0, _conf(p))),
            "reason":   (p.get("reason") or "").strip()[:140],
        })

    if not hydrated:
        return {
            "_llm_hint": (
                f"None of your ranked_picks ids resolved to a visible "
                f"contact (dropped: {dropped}). Re-call with "
                "defer_card=true to refresh the list, then re-pick."
            ),
            "rendered": 0,
            "dropped":  dropped,
        }

    # Pad to a minimum of 5 rows. The LLM sometimes returns 1-2 confident
    # picks; the user benefits from seeing alternatives next to the top
    # guess so they can override if the LLM guessed wrong. Padded rows
    # carry no confidence + empty reason — frontend skips the pill / reason
    # subtitle, so they read as neutral options below the ranked picks.
    MIN_ROWS = 5
    if len(hydrated) < MIN_ROWS:
        picked_ids = {h["id"] for h in hydrated}
        needed = MIN_ROWS - len(hydrated)
        padded = _fetch_padding_contacts(role, user_id, exclude=picked_ids, limit=needed)
        hydrated.extend(padded)

    from backend.ui_tools import _append
    _append({
        "type":     "contact_picker",
        "query":    query or "",
        "contacts": hydrated,
        "ranked":   True,
    })

    top = hydrated[0]
    return {
        "_llm_hint": (
            f"Rendered {len(hydrated)} rows for query={query!r} "
            f"({len(ordered)} your picks, {len(hydrated) - len(ordered)} "
            f"padded from recent contacts). Top: {top['display_name']} "
            f"(id={top['id']}, confidence={top['confidence']:.2f}). Reply "
            "ONE short sentence asking the user to confirm — do NOT "
            "enumerate the names; the card carries them. Aim for 10 picks "
            "next time so we don't have to pad."
        ),
        "rendered":  len(hydrated),
        "top_id":    top["id"],
        "top_confidence": top["confidence"],
        "dropped":   dropped,
    }


def _fetch_padding_contacts(
    role: Optional[str],
    user_id: Optional[int],
    exclude: set,
    limit: int,
) -> List[dict]:
    """Top recently-used visible contacts not in `exclude`. Used to pad
    a thin ranked_picks list up to the 5-row floor."""
    from backend.database import conn_ctx
    from backend.contacts import _visibility_clause, get as contacts_get

    where: list[str] = ["status IN ('active', 'pending')"]
    params: list[Any] = []
    vis_clause, vis_params = _visibility_clause(role, user_id)
    if vis_clause:
        where.append(vis_clause)
        params.extend(vis_params)
    if exclude:
        placeholders = ",".join("?" for _ in exclude)
        where.append(f"id NOT IN ({placeholders})")
        params.extend(exclude)
    where_sql = " AND ".join(where)

    with conn_ctx() as c:
        rows = c.execute(
            f"SELECT id FROM contacts WHERE {where_sql} "
            f"ORDER BY (last_used_at IS NULL), last_used_at DESC, "
            f"display_name ASC LIMIT ?",
            params + [int(limit)],
        ).fetchall()

    padded: List[dict] = []
    for r in rows:
        full = contacts_get(r["id"], role=role, user_id=user_id)
        if not full:
            continue
        channels = full.get("channels") or []
        padded.append({
            "id":            full["id"],
            "display_name":  full["display_name"],
            "relation":      full.get("relation") or "",
            "kind":          full.get("kind") or "person",
            "email":    next((ch["value"] for ch in channels if ch["kind"] == "email"), None),
            "phone":    next((ch["value"] for ch in channels if ch["kind"] == "phone"), None),
            "whatsapp": next((ch["value"] for ch in channels if ch["kind"] == "whatsapp"), None),
            "address":  _first_address_line(full.get("addresses")),
            # No confidence + empty reason → frontend renders these as
            # neutral rows below the LLM-ranked top.
        })
    return padded


async def execute(
    ctx,
    status: Optional[str] = "any",
    kind: Optional[str] = None,
    pick_to_chat: bool = True,
    defer_card: bool = False,
    ranked_picks: Optional[List[dict]] = None,
    query: str = "",
) -> dict[str, Any]:
    # Render-mode short-circuit. When the LLM passes ranked_picks it is
    # in the second leg of the fallback dance — render the ranked card
    # and skip the SQL list entirely.
    if ranked_picks is not None:
        return await _render_ranked(ctx, query, list(ranked_picks))

    # Auto-enter fallback mode when the LLM passes `query` without
    # `ranked_picks`. The model frequently treats `query` as a SQL
    # filter — it isn't (the SQL is unfiltered by design; `query` is
    # just the ranked card header). Reading the instinct as "I want
    # to disambiguate this descriptor" steers the LLM into the proper
    # two-call flow without it having to know about `defer_card`.
    if query and not defer_card:
        defer_card = True

    from backend.database import conn_ctx
    from backend.contacts import _visibility_clause

    where: list[str] = []
    params: list[Any] = []

    # Phase 9.4: scope the dump to what the caller can see. Without
    # this, list_contacts_for_picking is a backdoor around find_contact's
    # gate — a member would see every private contact in the household.
    role = getattr(ctx, "role", None) if ctx else None
    user_id = getattr(ctx, "user_id", None) if ctx else None
    vis_clause, vis_params = _visibility_clause(role, user_id)
    if vis_clause:
        where.append(vis_clause)
        params.extend(vis_params)

    if status and status != "any":
        where.append("status = ?")
        params.append(status)
    elif status == "any" or status is None:
        # Exclude spam + archived from the default any-status dump —
        # surfacing 535 spam senders for the LLM to pick from would
        # be both noisy and a privacy footgun.
        where.append("status IN ('active', 'pending')")
    if kind in ("person", "business"):
        where.append("kind = ?")
        params.append(kind)
    where_sql = "WHERE " + " AND ".join(where) if where else ""

    with conn_ctx() as c:
        rows = c.execute(
            f"SELECT id, display_name, relation, kind FROM contacts {where_sql} "
            f"ORDER BY (last_used_at IS NULL), last_used_at DESC, display_name ASC",
            params,
        ).fetchall()

    contacts = [
        {
            "id":           r["id"],
            "display_name": r["display_name"],
            "relation":     r["relation"],
            "kind":         r["kind"],
        }
        for r in rows
    ]

    # Surface the result as an interactive chat card (searchable list,
    # click → opens that contact in /contacts) so a question like "zeig
    # mir meine pending Kontakte" renders as rows the user can act on
    # rather than a wall of markdown text. Send EVERY visible contact —
    # the in-card filter operates only on what's in the payload, so a
    # cap silently hides matches the user can't otherwise reach.
    #
    # defer_card skips this emit — caller is in the FALLBACK flow and
    # will follow up with this same skill (ranked_picks=...) to render
    # an LLM-ranked card.
    if contacts and not defer_card:
        from backend.ui_tools import _append, get_ui_actions
        # Suppress duplicate `contacts_found` emissions within one turn.
        # When the LLM hammers the skill (different queries, hoping one
        # filters), every call would otherwise stack another alphabetical
        # 93-card under the assistant message. One card per turn is
        # always enough — the user can filter in-card.
        already = any(
            (a or {}).get("type") == "contacts_found"
            for a in (get_ui_actions() or [])
        )
        if already:
            return {
                "_llm_hint": (
                    "Alphabetical contacts card already shown this turn — "
                    "skipping duplicate emit. If the first card didn't help, "
                    "switch to the FALLBACK flow: call this skill with "
                    "defer_card=true to get the list silently, then re-call "
                    "with ranked_picks=[…] to render YOUR ranked picks. "
                    "Do not keep retrying the browse path."
                ),
                "count":    len(contacts),
                "contacts": contacts,
            }
        filter_label = None
        if status and status != "any":
            filter_label = f"Status: {status}"
        elif kind in ("person", "business"):
            filter_label = "Personen" if kind == "person" else "Unternehmen"
        _append({
            "type":     "contacts_found",
            "contacts": contacts,
            "total":    len(contacts),
            "filter":   filter_label,
            # pick_to_chat defaults TRUE because this skill is named
            # "for_picking" — picking IS the primary intent. The user's
            # click then resolves back to the LLM as "I meant this one"
            # instead of navigating away mid-draft. The LLM opts out
            # (pick_to_chat=false) only for explicit BROWSE queries
            # ("zeig mir alle Pending-Kontakte"). The skill's typo-
            # forgiving default skews to the safer-for-users behavior:
            # a stray click on a picker card never abandons the chat.
            "pick_to_chat": bool(pick_to_chat),
        })

    if defer_card:
        # Auto-emit the picker when the list is small enough that the
        # user can scan it directly — no ranking needed. Qwen 3.5 9B
        # reliably calls this skill with defer_card=true to enter the
        # two-call dance, then never makes the second call (it falls
        # back to enumerating names in chat prose, which defeats the
        # whole point of having a picker UI). With 2–10 candidates the
        # ranking step adds zero value: the user can scan the whole
        # list faster than the LLM can rank it. Skip the dance.
        # Single match: pass through silently — caller can use the one id.
        # Large list (>10): keep the two-call flow so the LLM's NL
        # matching still narrows down before the user has to scan.
        if 1 < len(contacts) <= 10:
            from backend.ui_tools import _append, get_ui_actions
            already = any(
                (a or {}).get("type") == "contact_picker"
                for a in (get_ui_actions() or [])
            )
            if not already:
                _append({
                    "type":     "contact_picker",
                    "query":    query or "",
                    "contacts": [
                        {
                            "id":           c["id"],
                            "display_name": c["display_name"],
                            "relation":     c.get("relation") or "",
                            "kind":         c.get("kind") or "person",
                        }
                        for c in contacts
                    ],
                    "ranked":   False,
                })
            return {
                "_llm_hint": (
                    f"Contact picker rendered with {len(contacts)} candidate(s). "
                    "Reply ONE short sentence in the user's language asking "
                    "them to pick (e.g. 'Welcher Kontakt?' / 'Which one?'). "
                    "Do NOT enumerate the contacts in prose. The user's "
                    "click seeds a follow-up like `Ich meine: <name>, "
                    "contact_id=N` — when that arrives, proceed with "
                    "that contact_id."
                ),
                "count":    len(contacts),
                "contacts": contacts,
            }
        hint = (
            f"full address book: {len(contacts)} contacts (card NOT emitted). "
            "Now re-call THIS SAME skill with ranked_picks=[{id, confidence, "
            "reason}, ...] (top 10 ordered by confidence DESC) and "
            "query=<user phrase> to render the ranked picker card. "
            "Confidence 0.95+ for unambiguous relation/alias maps (e.g. "
            "relation='Großmutter' for 'Oma'); ≤0.30 for long shots. "
            "Include `reason` so the user sees WHY each pick matches."
        )
    else:
        hint = (
            f"full address book: {len(contacts)} contacts. Match the user's "
            "phrase ('Oma', 'der Klempner', 'mein Bruder') against the name "
            "AND relation fields below — your NL matching is the whole point of "
            "calling this skill. Once you've picked one, pass its id as "
            "contact_id to whatsapp_draft / compose_draft / email_draft. If "
            "even with the full list you can't tell who the user means, ASK."
        )
    return {
        "_llm_hint": hint,
        "count":    len(contacts),
        "contacts": contacts,
    }
