"""find_person skill — unified lookup for household members + contacts.

Replaces the old split-by-tool model (find_user vs find_contact) with
one tool + a `source` enum. Per Hermes' design rule: when two tools
do almost the same thing, fold them into one with an action parameter.

Why we did the merge:
  The LLM kept picking find_contact for calendar-attendee intents
  (because its manifest was heavy and find_user's was lean), then
  passing the contact_id where a user_id was needed. Wrong table,
  wrong semantics. With one tool, the question changes from "which
  of two near-identical tools?" (where the LLM was losing) to "which
  `source` enum value?" (which it gets right).

Back-compat: find_user and find_contact remain registered but their
execute() functions now delegate to this skill. Old callers and
prompts keep working unchanged.
"""
from __future__ import annotations

from typing import Any, Optional


_VALID_SOURCES = ("household", "contacts", "auto")


async def execute(
    ctx,
    query: Optional[str] = None,
    source: str = "auto",
    kind: Optional[str] = None,
    role: Optional[str] = None,
    status: Optional[str] = "any",
    channel_kind: Optional[str] = None,
    channel_value: Optional[str] = None,
    limit: int = 10,
) -> dict[str, Any]:
    src = (source or "auto").strip().lower()
    if src not in _VALID_SOURCES:
        raise ValueError(
            f"source must be one of {_VALID_SOURCES}; got {source!r}"
        )

    # Run the requested sources concurrently-ish. They hit different
    # tables, no shared state.
    household_rows: list[dict[str, Any]] = []
    contact_rows: list[dict[str, Any]] = []
    contact_payload: dict[str, Any] = {}  # full payload from find_contact (carries the picker side-effect + hint)

    if src in ("household", "auto"):
        household_rows = await _query_household(query, role, limit, ctx=ctx)

    if src in ("contacts", "auto"):
        contact_payload = await _query_contacts(
            query=query, kind=kind, status=status,
            channel_kind=channel_kind, channel_value=channel_value,
            limit=limit, ctx=ctx,
        )
        contact_rows = contact_payload.get("contacts") or []

    # Genitive-s retry — if the original query found nothing AND has a
    # token plausibly stripped of a possessive 's' or German genitive
    # bare-s, retry once with the trimmed form. "Anna's" → "Anna",
    # "Beispiels" → "Beispiel". Bare-s only trimmed on tokens >= 6
    # chars so short names ("Hans", "Klaus") aren't mauled.
    if query and not household_rows and not contact_rows:
        trimmed = _trim_genitive_s(query)
        if trimmed and trimmed != query:
            if src in ("household", "auto"):
                household_rows = await _query_household(trimmed, role, limit, ctx=ctx)
            if src in ("contacts", "auto"):
                contact_payload = await _query_contacts(
                    query=trimmed, kind=kind, status=status,
                    channel_kind=channel_kind, channel_value=channel_value,
                    limit=limit, ctx=ctx,
                )
                contact_rows = contact_payload.get("contacts") or []

    # Tag each row with its `source` so the LLM can't mix the two
    # downstream. This is the whole reason the merge is safe.
    results: list[dict[str, Any]] = []
    for h in household_rows:
        results.append({**h, "source": "household"})
    for c in contact_rows:
        results.append({
            **c,
            "source": "contacts",
            # Make the (different!) id field explicit so the LLM can't
            # forget which it's holding.
            "contact_id": c.get("id"),
        })

    ambiguous = len(results) > 1 and bool(query)

    # Compose the routing hint. When find_contact already built a
    # detailed hint (single match w/ postal info, or ambiguous w/
    # picker UI), we splice it in so we don't lose that work.
    parts: list[str] = []
    if src == "auto":
        n_h = len(household_rows)
        n_c = len(contact_rows)
        parts.append(
            f"auto-lookup: {n_h} household member(s), {n_c} contact(s). "
            "Read each result's `source` field BEFORE reusing its id."
        )
    if not results:
        relational = _looks_relational(query)
        if relational:
            # The user referred to someone by relation ("my friend",
            # "der Klempner") not by name. find_person can't NL-match
            # those against the address book — list_contacts_for_picking
            # runs the two-call ranking flow: first dump the whole book
            # for you, then YOU re-call with ranked_picks to render the
            # top 10 the user sees. Asking for a name in prose is the
            # wrong move, and so is rendering the alphabetical card.
            parts.append(
                f"No person matched query={query!r} in source={src!r}. "
                f"RELATIONAL DESCRIPTOR detected — '{query}' is a "
                "role/relation word not a name.\n\n"
                "MANDATORY TWO-CALL FLOW:\n"
                "  1. list_contacts_for_picking(defer_card=true) — full "
                "address book, no card.\n"
                f"  2. list_contacts_for_picking(query={query!r}, "
                "ranked_picks=[{id, confidence, reason}, ...]) — top 10 "
                "ordered by confidence DESC.\n"
                "Include lower-confidence backups; shorter lists "
                "auto-pad with neutral recent contacts."
            )
        else:
            parts.append(
                f"No person matched query={query!r} in source={src!r}. "
                "If you were about to claim 'X gibt es nicht' — quote this "
                "honest empty result, do not hallucinate. If you searched "
                "only one source and the user clearly meant the other, try "
                "again with source='auto'."
            )
    elif src in ("contacts", "auto") and contact_payload.get("_llm_hint"):
        # Inherit find_contact's rich hint when it spoke up.
        parts.append(contact_payload["_llm_hint"])

    return {
        "results":     results,
        "count":       len(results),
        "ambiguous":   ambiguous,
        "source":      src,
        "_llm_hint":   "\n\n".join(parts) if parts else None,
    }


_RELATIONAL_WORDS = {
    # English — kinship / relations / generic-role descriptors users
    # commonly use without a name ("write to my friend", "schreib dem
    # Klempner", "Mail an meinen Steuerberater").
    "friend", "mom", "mum", "mother", "dad", "father",
    "brother", "sister", "uncle", "aunt", "cousin",
    "grandma", "grandpa", "grandmother", "grandfather",
    "neighbor", "neighbour", "boss", "colleague", "client",
    "vendor", "doctor", "dentist", "plumber", "electrician",
    "landlord", "tenant", "accountant",
    # German
    "freund", "freundin", "kumpel",
    "mama", "mutter", "papa", "vater",
    "bruder", "schwester", "onkel", "tante", "cousine", "cousin",
    "oma", "opa", "grossmutter", "grossvater", "großmutter", "großvater",
    "nachbar", "nachbarin", "chef", "chefin", "kollege", "kollegin",
    "kunde", "kundin", "anbieter", "arzt", "ärztin", "zahnarzt",
    "klempner", "elektriker", "vermieter", "mieter",
    "steuerberater", "steuerberaterin", "anwalt", "anwältin",
}


def _looks_relational(query: Optional[str]) -> bool:
    """True when `query` is a role/relation word, not a real name.

    Catches the "write to my friend" / "schreib dem Klempner" pattern
    so a 0-hit find_person can hard-redirect the LLM to the contacts
    picker instead of asking the user to type a real name. Case-
    insensitive, single-token check — multi-word queries ("John Smith")
    fall through as not-relational even if one word happens to match.
    """
    if not query:
        return False
    tok = (query or "").strip().lower()
    # Single token: direct match.
    if tok in _RELATIONAL_WORDS:
        return True
    # Multi-word: only count when EVERY non-stop token is relational
    # ("der Klempner" → ["der", "klempner"], "klempner" matches → True;
    # "John Smith" → 2 names, neither matches → False).
    _DROP = {"der", "die", "das", "den", "dem", "ein", "eine",
             "the", "a", "an", "my", "mein", "meine", "meinen",
             "meines", "meiner"}
    parts = [p for p in tok.split() if p not in _DROP]
    if not parts:
        return False
    return all(p in _RELATIONAL_WORDS for p in parts)


def _trim_genitive_s(query: str) -> str:
    """Strip a trailing possessive from each token: English "'s" /
    "'s" / "´s" → "", German bare-s on tokens >= 6 chars → "".
    Conservative: bare-s threshold is 6 to keep "Hans"/"Klaus"/"Anna"
    intact. Returns the rejoined string."""
    out = []
    for tok in (query or "").split():
        # Apostrophe-s variants first.
        for suffix in ("'s", "’s", "´s"):
            if tok.lower().endswith(suffix):
                tok = tok[: -len(suffix)]
                break
        else:
            # Bare-s genitive — only when the prefix would still be
            # >= 5 chars (i.e. original >= 6) so we don't trash short
            # names that legitimately end in 's'.
            if len(tok) >= 6 and tok.lower().endswith("s") and not tok.lower().endswith("ss"):
                tok = tok[:-1]
        if tok:
            out.append(tok)
    return " ".join(out)


async def _query_household(
    query: Optional[str], role: Optional[str], limit: int,
    *, ctx: Any = None,
) -> list[dict[str, Any]]:
    """Reuse the existing find_user execute so its formatting + role
    filter behaviour are the source of truth; we just strip the
    user-facing hint here because find_person composes its own.

    Phase C T13: pass `ctx` through so find_user can workspace-scope
    user_profiles — without it Jane (WS3 admin) saw every workspace's
    users as "household members"."""
    from backend.skills.find_user import skill as _fu
    result = await _fu.execute(ctx, query=query, role=role)
    rows = result.get("users") or []
    return rows[:limit]


async def _query_contacts(
    *,
    query: Optional[str], kind: Optional[str], status: Optional[str],
    channel_kind: Optional[str], channel_value: Optional[str],
    limit: int, ctx: Any,
) -> dict[str, Any]:
    """Delegate to find_contact so we inherit its picker UI emission
    + single-match postal hint + ambiguity card."""
    from backend.skills.find_contact import skill as _fc
    return await _fc.execute(
        ctx,
        query=query, kind=kind, status=status,
        channel_kind=channel_kind, channel_value=channel_value,
        limit=limit,
    )
