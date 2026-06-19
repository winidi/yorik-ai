"""vCard (.vcf) import — parse + plan + apply.

Source of truth for both the Contacts UI's "Import .vcf" modal and the
chat composer's vCard drop. Lifecycle:

  1. parse_vcards(text)       — vobject → list[ParsedCard]
  2. plan_import(cards, user) — for each card, decide one of:
       "new"            — no existing match by channel → would INSERT
       "merge"          — match by channel, names align → would ADD
                          missing channels / addresses to existing
       "name_conflict"  — match by channel, names disagree → SKIP and
                          let the user resolve one-by-one
  3. apply_import(plan, target_status, user) — execute the plan against
     the contacts module (no direct SQL — composes contacts.create /
     add_channel / add_address so we inherit existing behaviour)

The plan is intentionally stateless: the preview endpoint returns it
in full, the apply endpoint takes it back. No server-side cache, no
database table for in-flight imports — keeps the request lifecycle
short and trivially testable.

Channel dedup uses `contacts.find_by_channel` which hits the
contact_channels (kind, value) UNIQUE index in one shot — fast even
for a 500-card dump.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

from . import contacts as _contacts


log = logging.getLogger("yorik.contacts_import")


# ───────────────────────── parser ─────────────────────────

@dataclass
class ParsedChannel:
    kind: str           # 'email' | 'phone' | 'whatsapp' | ...
    value: str          # raw (will be normalised before lookup/insert)
    label: Optional[str] = None  # 'home' | 'work' | 'cell' | etc.


@dataclass
class ParsedAddress:
    kind: str = "home"  # 'home' | 'work' | …
    line1: Optional[str] = None
    line2: Optional[str] = None
    postcode: Optional[str] = None
    city: Optional[str] = None
    region: Optional[str] = None
    country: Optional[str] = None


@dataclass
class ParsedCard:
    display_name: str
    kind: str = "person"                       # 'person' | 'business'
    relation: Optional[str] = None             # often empty from .vcf
    birthday: Optional[str] = None             # ISO 8601 (YYYY-MM-DD)
    legal_name: Optional[str] = None           # ORG (organisation)
    notes: Optional[str] = None
    channels: List[ParsedChannel] = field(default_factory=list)
    addresses: List[ParsedAddress] = field(default_factory=list)


def parse_vcards(text: str) -> List[ParsedCard]:
    """Parse a .vcf blob into ParsedCard objects.

    Tolerant of:
      - UTF-8 BOM at file start (common from iOS, Outlook, Notepad).
      - Mixed line endings (CRLF / LF / bare CR).
      - One bad card not killing the rest.
      - 2.1 / 3.0 / 4.0 syntax (vobject normalises within each card).

    Why we don't use vobject.readComponents directly: it's all-or-
    nothing. A single malformed line anywhere in the file raises and
    the user sees zero — or, via the historical fallback that called
    readOne on the whole text, sees just ONE card. We've been hit by
    both. The reliable shape is: split on END:VCARD ourselves, then
    feed each chunk to readOne individually so bad cards skip
    quietly while good cards land.
    """
    try:
        import vobject  # type: ignore
    except ImportError as exc:  # pragma: no cover — install-time check
        raise RuntimeError(
            "vobject is required for vCard import. "
            "Add `vobject` to backend/requirements.txt and reinstall."
        ) from exc

    if not text:
        return []

    # 1. Strip UTF-8 BOM (and the same codepoint if it slipped in via
    #    UTF-16 → UTF-8 round-trip in some exporters).
    if text.startswith("﻿"):
        text = text[1:]

    # 2. Normalise line endings to LF so the chunker is uniform.
    #    vobject is happy with either CRLF or LF; the chunk-split
    #    just needs ONE convention to scan.
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # 3. Split on END:VCARD (case-insensitive, tolerant of trailing
    #    whitespace). Keep the END marker on each chunk — vobject
    #    needs it to close the card.
    chunks = re.split(r"(?i)(?<=END:VCARD)\s*\n", text)

    out: List[ParsedCard] = []
    parsed_cards = 0
    failed_cards = 0
    for raw_chunk in chunks:
        chunk = raw_chunk.strip()
        if not chunk:
            continue
        # Skip junk-only chunks (no BEGIN marker means there's nothing
        # to parse — e.g. a stray blank line after the last END:VCARD).
        if "BEGIN:VCARD" not in chunk.upper():
            continue
        try:
            card = vobject.readOne(chunk)
        except Exception as exc:  # noqa: BLE001
            failed_cards += 1
            log.exception("vcard chunk failed to parse, skipping: %s "
                          "(first 80 chars: %r)", exc, chunk[:80])
            continue
        if card is None:
            failed_cards += 1
            continue
        try:
            out.append(_card_to_parsed(card))
            parsed_cards += 1
        except Exception as exc:  # noqa: BLE001
            failed_cards += 1
            log.exception("vcard converted but failed to map: %s", exc)

    if failed_cards:
        log.info("vcard import: %d cards parsed, %d skipped due to errors",
                 parsed_cards, failed_cards)
    return out


def _card_to_parsed(card: Any) -> ParsedCard:
    # FN ("Formatted Name") is the canonical display name in vCard.
    # Fall back to N (structured name) if FN is missing.
    fn = _get_value(card, "fn")
    if not fn:
        n_obj = getattr(card, "n", None)
        if n_obj is not None and getattr(n_obj, "value", None) is not None:
            n = n_obj.value
            fn = " ".join(
                p for p in (
                    getattr(n, "given", ""),
                    getattr(n, "additional", ""),
                    getattr(n, "family", ""),
                ) if p
            ).strip()
    fn = (fn or "").strip()
    if not fn:
        raise ValueError("card has no FN or N — skipping")

    org = _get_value(card, "org")
    if isinstance(org, list):
        org = " · ".join(p for p in org if p)
    org = (org or "").strip() or None

    bday_raw = _get_value(card, "bday")
    birthday = _normalise_bday(bday_raw)

    notes = (_get_value(card, "note") or "").strip() or None

    # If ORG is set and FN looks like the org name (or N has no
    # personal-name parts), treat as a business card.
    kind = "person"
    if org and (org == fn or _looks_like_business(fn)):
        kind = "business"

    channels: List[ParsedChannel] = []
    # Emails
    for em in card.contents.get("email", []) or []:
        v = (getattr(em, "value", "") or "").strip()
        if v:
            channels.append(ParsedChannel(
                kind="email", value=v,
                label=_first_type_param(em),
            ))
    # Phones — TEL with TYPE=CELL becomes 'phone' here (the channel
    # taxonomy doesn't separate mobile vs landline today).
    for tel in card.contents.get("tel", []) or []:
        v = (getattr(tel, "value", "") or "").strip()
        if v:
            channels.append(ParsedChannel(
                kind="phone", value=v,
                label=_first_type_param(tel),
            ))
    # Websites
    for url in card.contents.get("url", []) or []:
        v = (getattr(url, "value", "") or "").strip()
        if v:
            channels.append(ParsedChannel(
                kind="website", value=v,
                label=_first_type_param(url),
            ))

    addresses: List[ParsedAddress] = []
    for adr in card.contents.get("adr", []) or []:
        v = getattr(adr, "value", None)
        if v is None:
            continue
        addresses.append(ParsedAddress(
            kind=_first_type_param(adr) or "home",
            line1=(getattr(v, "street", "") or "").strip() or None,
            line2=(getattr(v, "extended", "") or "").strip() or None,
            postcode=(getattr(v, "code", "") or "").strip() or None,
            city=(getattr(v, "city", "") or "").strip() or None,
            region=(getattr(v, "region", "") or "").strip() or None,
            country=(getattr(v, "country", "") or "").strip() or None,
        ))

    return ParsedCard(
        display_name=fn,
        kind=kind,
        legal_name=org if kind == "person" else None,
        birthday=birthday,
        notes=notes,
        channels=channels,
        addresses=addresses,
    )


def _get_value(card: Any, prop: str) -> Optional[str]:
    obj = getattr(card, prop, None)
    if obj is None:
        return None
    return getattr(obj, "value", None)


def _first_type_param(prop: Any) -> Optional[str]:
    """Pull TYPE=… off a vCard property (e.g. EMAIL;TYPE=WORK:…)."""
    try:
        params = prop.params or {}
    except Exception:
        return None
    types = params.get("TYPE") or params.get("type")
    if not types:
        return None
    if isinstance(types, list):
        return (types[0] or "").lower() or None
    return str(types).lower() or None


_BDAY_RE = re.compile(r"^(\d{4})[-]?(\d{2})[-]?(\d{2})")


def _normalise_bday(raw: Any) -> Optional[str]:
    """Coerce vCard BDAY (YYYY-MM-DD, YYYYMMDD, or datetime) to ISO."""
    if raw is None:
        return None
    if hasattr(raw, "isoformat"):
        try:
            return raw.isoformat()[:10]
        except Exception:
            pass
    s = str(raw).strip()
    if not s:
        return None
    m = _BDAY_RE.match(s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


_BUSINESS_HINTS = re.compile(
    r"\b(gmbh|ag|kg|ohg|ug|ltd|llc|inc|s\.?p\.?a\.?|sarl|bv|nv|"
    r"corp|company|gesellschaft|verein|stiftung)\b",
    re.IGNORECASE,
)


def _looks_like_business(name: str) -> bool:
    return bool(_BUSINESS_HINTS.search(name or ""))


# ───────────────────────── planner ─────────────────────────

@dataclass
class PlanEntry:
    outcome: str                     # 'new' | 'merge' | 'name_conflict'
    card: Dict[str, Any]             # ParsedCard.as_dict()
    existing_id: Optional[int] = None  # set when outcome != 'new'
    existing_name: Optional[str] = None
    matched_via: Optional[str] = None  # 'email:foo@bar' for the user UI
    new_channels: List[Dict[str, Any]] = field(default_factory=list)
    new_addresses: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class ImportPlan:
    entries: List[PlanEntry]

    def summary(self) -> Dict[str, int]:
        c = {"new": 0, "merge": 0, "name_conflict": 0}
        for e in self.entries:
            c[e.outcome] = c.get(e.outcome, 0) + 1
        c["total"] = len(self.entries)
        return c


def plan_import(cards: List[ParsedCard]) -> ImportPlan:
    """Decide outcome per card. No DB writes — pure inspection.

    The match algorithm: any channel (email / phone / whatsapp) that
    already exists in contact_channels wins. We don't fuzzy-match on
    display_name — that's brittle and we'd merge "Anna Müller" and
    "Anna Müller-Schmidt". Channel UNIQUE is enough.
    """
    entries: List[PlanEntry] = []
    for card in cards:
        existing_id: Optional[int] = None
        existing_name: Optional[str] = None
        matched_via: Optional[str] = None

        for ch in card.channels:
            if ch.kind not in ("email", "phone", "whatsapp"):
                continue
            normed = _contacts.normalize_channel(ch.kind, ch.value)
            if not normed:
                continue
            existing = _contacts.find_by_channel(ch.kind, normed)
            if existing:
                existing_id = int(existing["id"])
                existing_name = existing.get("display_name")
                matched_via = f"{ch.kind}:{normed}"
                break

        card_dict = _to_dict(card)
        if existing_id is None:
            entries.append(PlanEntry(outcome="new", card=card_dict))
            continue

        # Name comparison is intentionally strict: a casefold-equal check
        # only. Anything else risks merging two real-but-similarly-named
        # people. The UI can offer manual resolution for conflicts later.
        if (existing_name or "").casefold().strip() != \
           card_dict["display_name"].casefold().strip():
            entries.append(PlanEntry(
                outcome="name_conflict", card=card_dict,
                existing_id=existing_id, existing_name=existing_name,
                matched_via=matched_via,
            ))
            continue

        # Merge candidate — figure out which channels / addresses
        # would be added (so the preview can show "+ 2 phones").
        added_channels = _diff_channels(existing_id, card.channels)
        added_addresses = _diff_addresses(existing_id, card.addresses)
        entries.append(PlanEntry(
            outcome="merge", card=card_dict,
            existing_id=existing_id, existing_name=existing_name,
            matched_via=matched_via,
            new_channels=[asdict(c) for c in added_channels],
            new_addresses=[asdict(a) for a in added_addresses],
        ))
    return ImportPlan(entries=entries)


def _to_dict(card: ParsedCard) -> Dict[str, Any]:
    d = asdict(card)
    return d


def _diff_channels(existing_id: int,
                   incoming: List[ParsedChannel]) -> List[ParsedChannel]:
    """Return the subset of `incoming` channels not already on the
    existing contact. Comparison uses the same normaliser the contacts
    module writes with, so we match across formatting differences."""
    existing = _contacts.get(existing_id) or {}
    have: set[Tuple[str, str]] = {
        (ch["kind"], _contacts.normalize_channel(ch["kind"], ch["value"]))
        for ch in (existing.get("channels") or [])
    }
    out: List[ParsedChannel] = []
    for ch in incoming:
        normed = _contacts.normalize_channel(ch.kind, ch.value)
        if not normed:
            continue
        if (ch.kind, normed) in have:
            continue
        out.append(ch)
    return out


def _diff_addresses(existing_id: int,
                    incoming: List[ParsedAddress]) -> List[ParsedAddress]:
    """Coarse address dedup — same line1+postcode+city is considered
    a duplicate. Anything subtler (Strasse vs Str.) is left to the user
    to clean up manually."""
    existing = _contacts.get(existing_id) or {}
    have = {
        (
            (a.get("line1") or "").strip().casefold(),
            (a.get("postcode") or "").strip(),
            (a.get("city") or "").strip().casefold(),
        )
        for a in (existing.get("addresses") or [])
    }
    out: List[ParsedAddress] = []
    for a in incoming:
        key = (
            (a.line1 or "").strip().casefold(),
            (a.postcode or "").strip(),
            (a.city or "").strip().casefold(),
        )
        if key in have:
            continue
        out.append(a)
    return out


# ───────────────────────── applier ─────────────────────────

@dataclass
class ApplyResult:
    created_ids: List[int] = field(default_factory=list)
    merged_ids: List[int] = field(default_factory=list)
    skipped: int = 0                 # name_conflict outcomes left alone
    errors: List[Dict[str, Any]] = field(default_factory=list)


def apply_import(
    plan: ImportPlan,
    *,
    target_status: str = "pending",
    user_id: Optional[int] = None,
) -> ApplyResult:
    """Execute the plan.

    `target_status` is one of 'active' | 'pending'. The Contacts UI
    lets the user pick; the chat drop defaults to 'pending' so the
    user reviews them in the existing Pending tab.

    Skips name_conflict entries entirely — the user resolves those
    individually via the normal Contacts UI later.
    """
    if target_status not in ("active", "pending"):
        raise ValueError(f"invalid target_status={target_status!r}")

    result = ApplyResult()
    for entry in plan.entries:
        try:
            if entry.outcome == "new":
                cid = _create_one(entry.card, target_status, user_id)
                result.created_ids.append(cid)
            elif entry.outcome == "merge":
                _merge_one(entry)
                if entry.existing_id is not None:
                    result.merged_ids.append(entry.existing_id)
            else:  # name_conflict
                result.skipped += 1
        except Exception as exc:  # noqa: BLE001
            log.exception(
                "apply_import: card %r failed: %s",
                (entry.card or {}).get("display_name"), exc,
            )
            result.errors.append({
                "display_name": (entry.card or {}).get("display_name"),
                "error": str(exc),
            })
    return result


def _create_one(card_dict: Dict[str, Any],
                target_status: str,
                user_id: Optional[int]) -> int:
    cid = _contacts.create(
        display_name=card_dict["display_name"],
        kind=card_dict.get("kind") or "person",
        status=target_status,
        relation=card_dict.get("relation"),
        birthday=card_dict.get("birthday"),
        legal_name=card_dict.get("legal_name"),
        notes=card_dict.get("notes"),
        created_by_user_id=user_id,
        source="vcard",
    )
    for ch in card_dict.get("channels") or []:
        kind = ch.get("kind")
        value = ch.get("value")
        if not kind or not value:
            continue
        try:
            _contacts.add_channel(cid, kind=kind, value=value,
                                   label=ch.get("label"),
                                   source="vcard")
        except Exception as exc:  # noqa: BLE001
            log.info("skip channel %s:%s on %d: %s",
                     kind, value, cid, exc)
    for a in card_dict.get("addresses") or []:
        try:
            _contacts.add_address(
                cid,
                kind=a.get("kind") or "home",
                line1=a.get("line1"),
                line2=a.get("line2"),
                postcode=a.get("postcode"),
                city=a.get("city"),
                region=a.get("region"),
                country=a.get("country"),
                source="vcard",
            )
        except Exception as exc:  # noqa: BLE001
            log.info("skip address on %d: %s", cid, exc)
    return cid


def _merge_one(entry: PlanEntry) -> None:
    cid = entry.existing_id
    if cid is None:
        return
    for ch in entry.new_channels or []:
        try:
            _contacts.add_channel(
                cid, kind=ch["kind"], value=ch["value"],
                label=ch.get("label"), source="vcard",
            )
        except Exception as exc:  # noqa: BLE001
            log.info("merge: skip channel on %d: %s", cid, exc)
    for a in entry.new_addresses or []:
        try:
            _contacts.add_address(
                cid,
                kind=a.get("kind") or "home",
                line1=a.get("line1"),
                line2=a.get("line2"),
                postcode=a.get("postcode"),
                city=a.get("city"),
                region=a.get("region"),
                country=a.get("country"),
                source="vcard",
            )
        except Exception as exc:  # noqa: BLE001
            log.info("merge: skip address on %d: %s", cid, exc)


def plan_to_jsonable(plan: ImportPlan) -> Dict[str, Any]:
    """Wire shape used by the HTTP layer. Mirrored by plan_from_jsonable
    so the apply endpoint can reconstruct the dataclass."""
    return {
        "entries": [asdict(e) for e in plan.entries],
        "summary": plan.summary(),
    }


def plan_from_jsonable(blob: Dict[str, Any]) -> ImportPlan:
    raw_entries = blob.get("entries") or []
    entries: List[PlanEntry] = []
    for e in raw_entries:
        entries.append(PlanEntry(
            outcome=e.get("outcome", ""),
            card=e.get("card") or {},
            existing_id=e.get("existing_id"),
            existing_name=e.get("existing_name"),
            matched_via=e.get("matched_via"),
            new_channels=e.get("new_channels") or [],
            new_addresses=e.get("new_addresses") or [],
        ))
    return ImportPlan(entries=entries)
