"""LLM-assisted dedupe for pending/spam contacts.

Two passes:

1. **Aggressive normalisation** — group by name with business-suffix
   stripping (``GmbH``, ``e.K.``, ``AG``, ``SE`` …), em-dash / hyphen
   clauses removed, parentheticals removed, connector words (``und`` /
   ``and`` / ``&``) collapsed. Catches deterministic matches like
   ``Autohaus am See``, ``Autohaus am See – Robin Hentig GmbH`` →
   same bucket without LLM cost.

2. **LLM confirmation per bucket** — for every bucket of size ≥ 2 we
   serialise each member's name + channels + addresses and ask the LLM
   to pick a canonical id and reject any members that look like a
   different real-world entity (different phone area, different
   address, different person name). Members the LLM rejects are
   surfaced in ``skipped_groups`` so the user can review manually
   instead of being merged silently.

Output is a plan dict that's safe to inspect (dry-run) and apply
later (idempotent: re-running ``apply_plan`` over already-merged ids
just no-ops).

Merge semantics:
* All unique channels from losers move to the winner (skip when the
  winner already owns ``(kind, value)`` or another contact does).
* All addresses from losers move to the winner — duplicates collapsed
  by ``(line1, postcode, city)``.
* Persons whose ``employer_contact_id`` pointed at a loser are
  re-pointed at the winner.
* Losers are then hard-deleted.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from .database import conn_ctx
from . import contacts as _contacts


# In-process progress tracker keyed by user_id. The dedupe LLM pass is
# long enough (10-60 s) that the modal needs a live "12 / 22 clusters"
# indicator — generic spinners fail the "is this hung?" anxiety test.
# Single-process uvicorn (--no-reload) makes a module-level dict safe;
# if Yorik ever moves to multi-worker, swap for SQLite or Redis.
_PROGRESS: Dict[str, Dict[str, Any]] = {}


def set_progress(key: str, current: int, total: int, label: str = "") -> None:
    _PROGRESS[key] = {
        "current": int(current),
        "total":   int(total),
        "label":   label[:200],
        "ts":      time.time(),
    }


def get_progress(key: str) -> Dict[str, Any]:
    p = _PROGRESS.get(key)
    if not p:
        return {"current": 0, "total": 0, "label": "", "done": True}
    # If a progress entry hasn't been updated in 90 s assume the run is
    # over / crashed and stop returning stale numbers.
    if time.time() - p.get("ts", 0) > 90:
        _PROGRESS.pop(key, None)
        return {"current": 0, "total": 0, "label": "", "done": True}
    return {**p, "done": False}


def clear_progress(key: str) -> None:
    _PROGRESS.pop(key, None)

log = logging.getLogger("yorik.contacts.dedupe_llm")

# ---------------------------------------------------------------------------
# Aggressive normalisation — pass 1 pre-clustering key
# ---------------------------------------------------------------------------

# Business legal suffixes. Order matters slightly (longer first) for
# compound ones like ``GmbH & Co. KG``. Word-boundaries on each side so
# ``Aag`` doesn't match ``AG``.
_BIZ_SUFFIX_PATTERNS = [
    r"\bgmbh\s*&\s*co\.?\s*kg\b",
    r"\bgmbh\s*&\s*co\.?\s*ohg\b",
    r"\bag\s*&\s*co\.?\s*kg\b",
    r"\bgmbh\s*&\s*co\b",
    r"\b(?:e\.?\s*k\.?|e\s*v\.?)\b",          # e.K. / e.V.
    r"\b(?:gmbh|mbh|ag|se|ohg|kg|gbr|ug|partg|stiftung)\b",
    r"\b(?:inc|llc|ltd|corp|plc|sa|s\.?a\.?|s\.?à\.?r\.?l\.?)\b",
]
_BIZ_SUFFIX_RE = re.compile("|".join(_BIZ_SUFFIX_PATTERNS), flags=re.IGNORECASE)

# Trailing dash/em-dash/en-dash clauses: " – Robin Hentig GmbH", " - foo bar"
_DASH_CLAUSE_RE = re.compile(r"\s+[-–—]\s+.+$")

# Parentheticals: "(Hannover)", "(West)"
_PAREN_RE = re.compile(r"\s*\([^)]*\)")

# Punctuation we collapse to spaces
_PUNCT_RE = re.compile(r"[,;./]")

# Connectors that don't carry identity
_CONNECTOR_RE = re.compile(r"\b(?:und|and|or|oder|&|\+)\b", flags=re.IGNORECASE)

# Multi-whitespace collapse
_WS_RE = re.compile(r"\s+")


def aggressive_norm(name: str) -> str:
    """Aggressive normalisation: lowercase, strip business suffixes,
    dash/paren clauses, connectors, collapse whitespace.

    Returns "" for empty input so callers can filter unkeyable rows.
    """
    n = (name or "").strip().lower()
    if not n:
        return ""

    # Multiple passes for chained suffixes ("GmbH & Co. KG" → "GmbH & Co" → "")
    for _ in range(4):
        new_n = _BIZ_SUFFIX_RE.sub(" ", n)
        if new_n == n:
            break
        n = new_n

    n = _DASH_CLAUSE_RE.sub(" ", n)
    n = _PAREN_RE.sub(" ", n)
    n = _PUNCT_RE.sub(" ", n)
    n = _CONNECTOR_RE.sub(" ", n)
    n = _WS_RE.sub(" ", n).strip()
    return n


# ---------------------------------------------------------------------------
# Pass 1 — pre-clustering
# ---------------------------------------------------------------------------


def precluster(contacts: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Group contacts by aggressive-normalised name. Returns only buckets
    of size ≥ 2 (singletons aren't dedupe candidates by definition)."""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for c in contacts:
        key = aggressive_norm(c.get("display_name") or "")
        if not key:
            continue
        groups.setdefault(key, []).append(c)
    return [g for g in groups.values() if len(g) >= 2]


# ---------------------------------------------------------------------------
# Pass 1b — fuzzy pre-clustering (typos, transposed letters)
# ---------------------------------------------------------------------------

# Ratio threshold above which two names are "close enough" that the
# LLM should at least see the pair. 0.85 catches single-character
# typos and transpositions ("Mnsiolek" / "Musiolek" → 0.93) without
# pulling in obviously-different names.
_FUZZY_RATIO = 0.85

# Length of the blocking key. Pairs that disagree in the first N chars
# are never compared, so a one-letter typo at position N+1 is fine
# (the key still matches) but a typo at position 1 isn't.
_FUZZY_BLOCK_LEN = 4


def _ufind(parent: Dict[int, int], x: int) -> int:
    """Iterative path-compressing union-find find()."""
    root = x
    while parent[root] != root:
        root = parent[root]
    while parent[x] != root:
        parent[x], x = root, parent[x]
    return root


def fuzzy_precluster(
    contacts: List[Dict[str, Any]],
    *,
    threshold: float = _FUZZY_RATIO,
    block_len: int = _FUZZY_BLOCK_LEN,
) -> List[List[Dict[str, Any]]]:
    """Cluster contacts whose names are similar but not exactly equal
    after aggressive normalisation. Catches OCR / transcription typos
    like ``Mnsiolek`` vs ``Musiolek``.

    All-pairs ratio comparison is O(n²) so we block by the first
    ``block_len`` characters of the normalised name first. Pairs across
    blocks are never compared. This is fine for typo-class differences
    (a one-letter typo deep in the name still has the same prefix);
    it would miss typos in the first 4 chars, but those are rare in
    practice and the resulting cluster sizes would be huge if we
    relaxed the block.
    """
    blocks: Dict[str, List[Tuple[int, str, Dict[str, Any]]]] = {}
    for c in contacts:
        key = aggressive_norm(c.get("display_name") or "")
        if len(key) < 3:
            continue
        block_key = key[:block_len]
        blocks.setdefault(block_key, []).append((int(c["id"]), key, c))

    parent: Dict[int, int] = {}
    members: Dict[int, Dict[str, Any]] = {}

    def union(a: int, b: int) -> None:
        ra, rb = _ufind(parent, a), _ufind(parent, b)
        if ra != rb:
            parent[ra] = rb

    for block in blocks.values():
        if len(block) < 2:
            continue
        for cid, _, c in block:
            if cid not in parent:
                parent[cid] = cid
                members[cid] = c
        for i in range(len(block)):
            id_i, name_i, _ = block[i]
            for j in range(i + 1, len(block)):
                id_j, name_j, _ = block[j]
                if name_i == name_j:
                    union(id_i, id_j)
                    continue
                if difflib.SequenceMatcher(None, name_i, name_j).ratio() >= threshold:
                    union(id_i, id_j)

    groups: Dict[int, List[Dict[str, Any]]] = {}
    for cid in parent:
        groups.setdefault(_ufind(parent, cid), []).append(members[cid])
    return [g for g in groups.values() if len(g) >= 2]


# ---------------------------------------------------------------------------
# Pass 2 — LLM confirmation per bucket
# ---------------------------------------------------------------------------

# How long to wait per LLM call. Qwen 9B on local hardware should answer
# a small dedupe prompt in 1–4 s; 30 s gives generous slack.
_LLM_TIMEOUT_S = 30.0

# Cap the total number of LLM calls per dedupe run so a runaway DB
# can't blow tokens or time. ~250 should cover thousands of pending
# contacts after pre-clustering.
_MAX_LLM_CALLS = 250


def _collect_provenance(contact_id: int, *, limit: int = 3) -> List[Dict[str, Any]]:
    """Pull up to ``limit`` extraction-proposal snapshots that produced or
    matched this contact. Each row of ``contact_extraction_proposals``
    carries the LLM's original interpretation of one Paperless doc:
    IBAN, tax_id, the full extracted name, emails, phones, address, and
    a short document_summary. These are gold-standard identity signals
    — an IBAN match is conclusive — so feeding them into the dedupe
    prompt is much stronger context than channels+addresses alone.
    """
    out: List[Dict[str, Any]] = []
    try:
        with conn_ctx() as c:
            rows = c.execute(
                "SELECT source_paperless_doc_id, proposed_json, created_at "
                "FROM contact_extraction_proposals "
                "WHERE match_candidate_id = ? OR created_contact_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (contact_id, contact_id, limit),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001
        log.debug("provenance: lookup failed for %s: %s", contact_id, exc)
        return out
    for r in rows:
        try:
            pj = json.loads(r["proposed_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        out.append({
            "paperless_doc_id": r["source_paperless_doc_id"],
            "iban": pj.get("iban"),
            "tax_id": pj.get("tax_id"),
            "legal_name": pj.get("legal_name") or pj.get("business_name"),
            "emails": pj.get("emails") or [],
            "phones": pj.get("phones") or [],
            "address": {
                "line1": pj.get("address_street"),
                "postcode": pj.get("address_postcode"),
                "city": pj.get("address_city"),
            },
            "document_summary": (pj.get("document_summary") or "")[:200],
        })
    return out


def _summarise_for_llm(c: Dict[str, Any]) -> Dict[str, Any]:
    """Compact JSON view of a contact for the prompt: id, name, kind,
    channels, addresses, plus any source-doc provenance we can find.

    Provenance is included opportunistically — many older / autocaptured
    contacts have none. When present it's an authoritative signal
    (IBAN, tax_id, emails, phones from the original Paperless doc that
    produced the row)."""
    out: Dict[str, Any] = {
        "id": int(c["id"]),
        "kind": c.get("kind"),
        "name": c.get("display_name"),
        "channels": [
            {"kind": ch.get("kind"), "value": ch.get("value")}
            for ch in (c.get("channels") or [])
        ],
        "addresses": [
            {
                "line1": a.get("line1"),
                "postcode": a.get("postcode"),
                "city": a.get("city"),
            }
            for a in (c.get("addresses") or [])
        ],
    }
    prov = _collect_provenance(int(c["id"]))
    if prov:
        out["source_documents"] = prov
    return out


# Auto-detection threshold: an address is treated as residential /
# extractor-confused when it appears on this many contacts with
# DISTINCT normalised names. Real shared business addresses (e.g. a
# clinic with multiple departments, a government building) have many
# contacts under the SAME name; residential addresses have many
# contacts under DIFFERENT names because every letter from every
# different business is addressed to the household.
_AUTO_RESIDENTIAL_DISTINCT_NAMES = 5


def _explicit_residential_addresses() -> List[Dict[str, str]]:
    """User-declared addresses: user_profiles rows + optional manual
    list in app_settings. These don't depend on the contact corpus, so
    a fresh install with the user's home address in their profile
    already gets the basic protection.
    """
    out: List[Dict[str, str]] = []
    try:
        with conn_ctx() as c:
            rows = c.execute(
                "SELECT address_street, address_postcode, address_city "
                "FROM user_profiles "
                "WHERE address_street IS NOT NULL OR address_postcode IS NOT NULL"
            ).fetchall()
            for r in rows:
                line1 = (r["address_street"] or "").strip()
                pc    = (r["address_postcode"] or "").strip()
                city  = (r["address_city"] or "").strip()
                if line1 or pc or city:
                    out.append({"line1": line1, "postcode": pc, "city": city})
            extra_row = c.execute(
                "SELECT value FROM app_settings WHERE key = 'dedupe_extra_residential_addresses'"
            ).fetchone()
            if extra_row and extra_row["value"]:
                try:
                    extra = json.loads(extra_row["value"])
                    if isinstance(extra, list):
                        for a in extra:
                            if isinstance(a, dict):
                                out.append({
                                    "line1": str(a.get("line1") or "").strip(),
                                    "postcode": str(a.get("postcode") or "").strip(),
                                    "city": str(a.get("city") or "").strip(),
                                })
                except json.JSONDecodeError:
                    pass
    except Exception as exc:  # noqa: BLE001
        log.debug("explicit residential addresses: lookup failed: %s", exc)
    return out


def _auto_detect_residential_addresses(
    contacts: List[Dict[str, Any]],
    *,
    min_distinct_names: int = _AUTO_RESIDENTIAL_DISTINCT_NAMES,
) -> List[Dict[str, str]]:
    """Find addresses that appear on many DIFFERENT entities (distinct
    aggressive-normalised names). These are almost certainly residential
    addresses confused by the extractor: a household receives mail from
    20+ different businesses, all of which end up with the household
    address attached.

    Key vs naive count: a clinic with 9 contacts at one address all
    normalised to ``kurklinik strandrobbe`` has 1 distinct name and
    is NOT flagged. A residential with 22 contacts at one address
    normalised to 22 different names IS flagged.
    """
    name_keys_at_addr: Dict[Tuple[str, str, str], set] = {}
    canonical_form: Dict[Tuple[str, str, str], Dict[str, str]] = {}
    for c in contacts:
        name_key = aggressive_norm(c.get("display_name") or "")
        if not name_key:
            continue
        for a in (c.get("addresses") or []):
            line1 = (a.get("line1") or "").strip()
            if not line1:
                continue
            pc = (a.get("postcode") or "").strip()
            city = (a.get("city") or "").strip()
            key = (line1.lower(), pc, city.lower())
            name_keys_at_addr.setdefault(key, set()).add(name_key)
            if key not in canonical_form:
                canonical_form[key] = {"line1": line1, "postcode": pc, "city": city}
    out: List[Dict[str, str]] = []
    for key, names in name_keys_at_addr.items():
        if len(names) >= min_distinct_names:
            out.append(canonical_form[key])
    return out


def snapshot_auto_residentials_to_settings() -> int:
    """Run the auto-detect pass NOW and persist the result into
    ``app_settings.dedupe_extra_residential_addresses`` so the
    extractor (which doesn't have a corpus to learn from per-doc)
    benefits from it on future scans. Idempotent — merges with any
    addresses already in the setting. Returns count saved.
    """
    from . import contacts as _contacts
    rows = _contacts.search("", kind=None, status=None, limit=20_000,
                            role="admin", user_id=None)
    hydrated: List[Dict[str, Any]] = []
    for r in rows:
        if not r: continue
        c = _contacts.get(r["id"], role="admin", user_id=None)
        if c: hydrated.append(c)
    auto = _auto_detect_residential_addresses(hydrated)
    if not auto:
        return 0
    # Merge with whatever's already there.
    with conn_ctx() as c:
        existing_row = c.execute(
            "SELECT value FROM app_settings "
            "WHERE key = 'dedupe_extra_residential_addresses'"
        ).fetchone()
        existing: List[Dict[str, str]] = []
        if existing_row and existing_row["value"]:
            try:
                v = json.loads(existing_row["value"])
                if isinstance(v, list):
                    existing = [x for x in v if isinstance(x, dict)]
            except json.JSONDecodeError:
                pass
        seen = set(
            (
                (x.get("line1") or "").lower().strip(),
                (x.get("postcode") or "").strip(),
                (x.get("city") or "").lower().strip(),
            )
            for x in existing
        )
        added = 0
        for a in auto:
            key = (
                (a.get("line1") or "").lower().strip(),
                (a.get("postcode") or "").strip(),
                (a.get("city") or "").lower().strip(),
            )
            if key in seen:
                continue
            existing.append(a)
            seen.add(key)
            added += 1
        c.execute(
            "INSERT INTO app_settings (key, value) VALUES "
            "('dedupe_extra_residential_addresses', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (json.dumps(existing, ensure_ascii=False),),
        )
    return added


def _load_residential_addresses(
    contacts: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, str]]:
    """Union of explicit (user_profiles + manual list) and auto-detected
    residential addresses. When ``contacts`` is None the auto-detect
    pass is skipped — only the explicit set is returned.
    """
    out: List[Dict[str, str]] = []
    out.extend(_explicit_residential_addresses())
    if contacts:
        out.extend(_auto_detect_residential_addresses(contacts))
    # Dedupe identical entries (case/whitespace-normalised).
    seen: set[Tuple[str, str, str]] = set()
    deduped: List[Dict[str, str]] = []
    for a in out:
        key = (
            (a.get("line1") or "").lower().strip(),
            (a.get("postcode") or "").strip(),
            (a.get("city") or "").lower().strip(),
        )
        if not key[0] and not key[1] and not key[2]:
            continue
        if key in seen:
            continue
        seen.add(key)
        deduped.append(a)
    return deduped


_PROMPT_SYSTEM = (
    "You are deduplicating contact records that already share a "
    "normalised name. Decide which entries refer to the SAME real-world "
    "entity.\n"
    "\n"
    "Signal hierarchy (strongest first):\n"
    "1. IBAN or tax_id (in source_documents) — if two contacts share "
    "either, they ARE the same business; merge with high confidence.\n"
    "2. Same physical address (line1 + postcode + city) — strong match, "
    "BUT only when the address is NOT in household_residential_addresses. "
    "If members share an address that matches a household address, that "
    "address is the recipient's home (an extractor mistake) and carries "
    "zero identity signal — decide on tax_id, IBAN, emails, or phones "
    "alone, NOT address.\n"
    "3. Same email domain or overlapping phone area code — supportive.\n"
    "4. Name variations that differ only by legal suffix (GmbH, e.K., "
    "AG), or dash-clauses, or capitalisation — supportive.\n"
    "5. Different addresses in different cities — distinct branches, "
    "do NOT merge.\n"
    "6. Different phone numbers can mean two lines for the same dealer/"
    "office (still merge if non-residential address + name match) OR "
    "distinct branches (do not merge). Use the source_documents context "
    "to decide.\n"
    "\n"
    "Output ONLY a single JSON object with two arrays:\n"
    '  "merge": [{"canonical_id": int, "member_ids": [int,...], "reason": str, "confidence": "high"|"medium"}]\n'
    '  "skip":  [{"ids": [int,...], "reason": str}]\n'
    "\n"
    "Rules:\n"
    "- Pick the canonical_id as the id with the most complete data "
    "(channels + addresses + source_documents + full legal name); "
    "break ties by lowest id.\n"
    "- Every input id MUST appear in exactly one of merge.member_ids or skip.ids.\n"
    "- Low-confidence cases go in skip with the reason.\n"
    "- Singleton merge groups (one id) are not allowed — put them in skip.\n"
    "- Do not invent ids.\n"
    "- No prose outside the JSON."
)


def _strip_code_fence(s: str) -> str:
    """LLMs sometimes wrap JSON in ```json ... ```. Strip it."""
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


def _parse_llm_response(raw: str, valid_ids: List[int]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Parse the LLM's JSON. Return (merge_groups, skip_groups) sanitised
    to only include ids the caller actually sent."""
    try:
        obj = json.loads(_strip_code_fence(raw))
    except json.JSONDecodeError as exc:
        log.warning("dedupe-llm: bad JSON, falling back to skip-all: %s", exc)
        return [], [{"ids": list(valid_ids), "reason": f"LLM returned invalid JSON: {exc}"}]

    id_set = set(int(i) for i in valid_ids)
    merge: List[Dict[str, Any]] = []
    skip: List[Dict[str, Any]] = []
    seen_ids: set[int] = set()

    for g in obj.get("merge") or []:
        try:
            members = [int(m) for m in g.get("member_ids") or [] if int(m) in id_set]
            canon = int(g["canonical_id"])
        except (KeyError, TypeError, ValueError):
            continue
        if canon not in id_set or canon not in members or len(members) < 2:
            continue
        members = sorted(set(members))
        merge.append({
            "canonical_id": canon,
            "member_ids": members,
            "reason": str(g.get("reason") or "")[:300],
            "confidence": "high" if g.get("confidence") == "high" else "medium",
        })
        seen_ids.update(members)

    for s in obj.get("skip") or []:
        ids = [int(m) for m in (s.get("ids") or []) if int(m) in id_set and int(m) not in seen_ids]
        if not ids:
            continue
        skip.append({"ids": sorted(set(ids)), "reason": str(s.get("reason") or "")[:300]})
        seen_ids.update(ids)

    # Anything the LLM forgot → skip silently
    leftover = sorted(id_set - seen_ids)
    if leftover:
        skip.append({"ids": leftover, "reason": "LLM did not classify"})

    return merge, skip


def _ask_llm_for_bucket(
    bucket: List[Dict[str, Any]],
    *,
    residential_addresses: Optional[List[Dict[str, str]]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """One LLM call. Returns (merge_groups, skip_groups). On any error
    we conservatively skip the whole bucket so nothing gets merged
    accidentally."""
    from .agent.llm import LlmClient
    from . import ask as _ask

    summaries = [_summarise_for_llm(c) for c in bucket]
    payload: Dict[str, Any] = {"contacts": summaries}
    if residential_addresses:
        payload["household_residential_addresses"] = residential_addresses
    user_msg = (
        "Group of contacts that share a normalised name. Classify them.\n"
        "When household_residential_addresses is provided, any contact "
        "address matching one of those is the recipient's home (an "
        "extractor mistake) and MUST NOT be used as a merge signal.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )

    client = LlmClient(
        model=_ask.LLM_MODEL,
        base_url=_ask.LLM_BASE_URL,
        api_key=_ask._boot_api_key(),
        request_timeout=_LLM_TIMEOUT_S,
        max_retries=1,
    )
    try:
        resp = client.chat(
            [
                {"role": "system", "content": _PROMPT_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=2048,
            temperature=0.0,
        )
        raw = resp.get("content") or ""
    except Exception as exc:  # noqa: BLE001
        log.warning("dedupe-llm: LLM call failed for bucket of %d: %s",
                    len(bucket), exc)
        return [], [{"ids": [int(c["id"]) for c in bucket],
                     "reason": f"LLM call failed: {exc}"}]

    return _parse_llm_response(raw, [int(c["id"]) for c in bucket])


# ---------------------------------------------------------------------------
# Plan builder
# ---------------------------------------------------------------------------


def _is_name_only_bucket(bucket: List[Dict[str, Any]]) -> bool:
    """True when every member of `bucket` carries zero distinguishing data
    besides a name.

    Detects the signature-cascade case: same person's printed name
    extracted as a contact from N form letters (board member signatures
    on insurance/bank correspondence is the canonical example). The
    LLM has nothing to weigh in this case — no channels, no addresses,
    no IBAN, no tax_id, no provenance signals — and correctly refuses
    to guess. Auto-merging here is safe because the probability of N
    real distinct people sharing an exact name AND having no other
    extractable data is functionally zero.

    Implementation: one batched provenance query per bucket so the cost
    is 1 SQL round-trip, not 1-per-member."""
    # Fast in-memory checks first — bail as soon as any member has data.
    for c in bucket:
        if c.get("channels"):
            return False
        if c.get("addresses"):
            return False
        if (c.get("tax_id") or "").strip():
            return False
        if (c.get("iban") or "").strip():
            return False

    ids = [int(c["id"]) for c in bucket]
    if not ids:
        return False
    ph = ",".join("?" * len(ids))
    try:
        with conn_ctx() as cx:
            rows = cx.execute(
                f"SELECT proposed_json FROM contact_extraction_proposals "
                f"WHERE match_candidate_id IN ({ph}) "
                f"   OR created_contact_id IN ({ph})",
                [*ids, *ids],
            ).fetchall()
    except Exception as exc:  # noqa: BLE001
        log.debug("name-only check: provenance lookup failed: %s", exc)
        # Conservative on lookup failure — fall through to the LLM path
        # rather than auto-merge based on incomplete info.
        return False

    for r in rows:
        try:
            pj = json.loads(r["proposed_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if (pj.get("iban") or "").strip():
            return False
        if (pj.get("tax_id") or "").strip():
            return False
        if pj.get("emails"):
            return False
        if pj.get("phones"):
            return False
        if (pj.get("address_street") or "").strip():
            return False
        if (pj.get("address_postcode") or "").strip():
            return False
        if (pj.get("address_city") or "").strip():
            return False

    return True


def build_plan(
    *,
    role: str,
    user_id: Optional[int],
    status: str = "pending",
    kind: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a merge plan over the requested bucket of contacts.

    Args:
        role: caller's role (passed to the visibility-gated contacts.search).
        user_id: caller's user_id (same).
        status: 'pending', 'active', or 'spam'. 'active' is for cleaning
            up duplicates that slipped through pending review (e.g. the
            same business autocaptured under different sender variants
            before the dedupe gate existed).
        kind: None = both, 'business' = business only, 'person' = person only.

    Returns:
        ``{"merge": [...], "skip": [...], "stats": {...}}`` — the same
        shape ``apply_plan`` consumes.
    """
    if status not in ("pending", "active", "spam"):
        raise ValueError(f"status must be 'pending' / 'active' / 'spam', got {status!r}")
    if kind not in (None, "person", "business"):
        raise ValueError(f"kind must be None / 'person' / 'business', got {kind!r}")

    t0 = time.monotonic()
    rows = _contacts.search(
        "",
        kind=kind,
        status=status,
        limit=10_000,
        role=role,
        user_id=user_id,
    )
    hydrated: List[Dict[str, Any]] = []
    for r in rows:
        if not r:
            continue
        c = _contacts.get(r["id"], role=role, user_id=user_id)
        if c:
            hydrated.append(c)

    # Phase A — exact aggressive-norm clusters.
    exact_buckets = precluster(hydrated)
    exact_clustered_ids = set(int(c["id"]) for b in exact_buckets for c in b)

    # Phase B — fuzzy clusters on the remaining singletons.
    # Catches "Mnsiolek" vs "Musiolek" (one-letter typo) that produce
    # different exact keys but high difflib ratios.
    singletons = [c for c in hydrated if int(c["id"]) not in exact_clustered_ids]
    fuzzy_buckets = fuzzy_precluster(singletons)

    buckets = exact_buckets + fuzzy_buckets
    # Pass the full hydrated set so auto-detect can find addresses that
    # appear on many distinct entities — those are almost always
    # residential / extractor-confused.
    residential = _load_residential_addresses(contacts=hydrated)
    stats = {
        "loaded": len(hydrated),
        "preclusters_exact": len(exact_buckets),
        "preclusters_fuzzy": len(fuzzy_buckets),
        "preclusters": len(buckets),
        "llm_calls": 0,
        "llm_skipped_buckets": 0,
        "residential_addresses": len(residential),
    }

    merge_groups: List[Dict[str, Any]] = []
    skip_groups: List[Dict[str, Any]] = []
    stats["auto_merged_name_only_buckets"] = 0

    progress_key = str(user_id) if user_id else "anon"
    set_progress(progress_key, 0, len(buckets), "starting")

    for idx, bucket in enumerate(buckets):
        if stats["llm_calls"] >= _MAX_LLM_CALLS:
            skip_groups.append({
                "ids": [int(c["id"]) for c in bucket],
                "reason": "max LLM calls cap reached for this run",
            })
            stats["llm_skipped_buckets"] += 1
            continue
        label = (bucket[0].get("display_name") or "").strip()[:60]
        set_progress(progress_key, idx, len(buckets), f"reviewing: {label}")

        # Fast path: bucket where every member carries zero distinguishing
        # data (no channels, no addresses, no tax_id, no IBAN, no signals
        # in any source document). These are signature cascades — a
        # board member's printed name extracted from N form letters.
        # The LLM cannot disambiguate without signals so it always skips;
        # we merge here directly, deterministic canonical = lowest id,
        # confidence='high' so the modal pre-selects it.
        if _is_name_only_bucket(bucket):
            sorted_b = sorted(bucket, key=lambda c: int(c["id"]))
            canon = sorted_b[0]
            display = (canon.get("display_name") or "").strip()
            merge_groups.append({
                "canonical_id": int(canon["id"]),
                "member_ids":   [int(c["id"]) for c in sorted_b],
                "reason": (
                    f"{len(bucket)} rows named {display!r} with no channels, "
                    "addresses, IBAN or tax_id on any source document. "
                    "Treated as the same person — likely a printed signature "
                    "on multiple form letters."
                ),
                "confidence":   "high",
            })
            stats["auto_merged_name_only_buckets"] += 1
            continue

        m, s = _ask_llm_for_bucket(bucket, residential_addresses=residential)
        stats["llm_calls"] += 1
        merge_groups.extend(m)
        skip_groups.extend(s)

    clear_progress(progress_key)

    # Attach a display payload so the frontend can render the modal
    # without an extra round-trip.
    by_id = {int(c["id"]): c for c in hydrated}
    for g in merge_groups:
        g["members"] = [_summarise_for_llm(by_id[i]) for i in g["member_ids"] if i in by_id]
    for s in skip_groups:
        s["members"] = [_summarise_for_llm(by_id[i]) for i in s["ids"] if i in by_id]

    stats["merge_groups"] = len(merge_groups)
    stats["skip_groups"] = len(skip_groups)
    stats["proposed_deletions"] = sum(len(g["member_ids"]) - 1 for g in merge_groups)
    stats["duration_s"] = round(time.monotonic() - t0, 2)

    # Surface the Paperless base URL so the frontend can build deep
    # links to source documents in the review modal.
    paperless_base = ""
    try:
        with conn_ctx() as c:
            r = c.execute(
                "SELECT value FROM app_settings WHERE key = 'paperless_base_url'"
            ).fetchone()
            if r:
                paperless_base = str(r["value"] or "").rstrip("/")
    except Exception:
        pass

    return {
        "merge": merge_groups,
        "skip": skip_groups,
        "stats": stats,
        "paperless_base_url": paperless_base,
        "residential_addresses": residential,
    }


# ---------------------------------------------------------------------------
# Plan applier — moves channels/addresses, re-points employer refs, deletes
# ---------------------------------------------------------------------------


def _move_channels(c, winner_id: int, loser_id: int) -> int:
    """Move every channel owned by ``loser_id`` to ``winner_id``, dedupe
    by (kind, value). Returns count moved."""
    moved = 0
    rows = c.execute(
        "SELECT id, kind, value, label, source, verified_at "
        "FROM contact_channels WHERE contact_id = ?",
        (loser_id,),
    ).fetchall()
    for r in rows:
        # Does the winner already have this channel?
        dup = c.execute(
            "SELECT 1 FROM contact_channels WHERE contact_id = ? AND kind = ? AND value = ?",
            (winner_id, r["kind"], r["value"]),
        ).fetchone()
        if dup:
            continue
        # Does ANOTHER contact already own this (kind, value)? — UNIQUE
        # constraint would refuse the update, so check first and skip.
        owner = c.execute(
            "SELECT contact_id FROM contact_channels WHERE kind = ? AND value = ?",
            (r["kind"], r["value"]),
        ).fetchone()
        if owner and int(owner["contact_id"]) not in (winner_id, loser_id):
            continue
        c.execute(
            "UPDATE contact_channels SET contact_id = ? WHERE id = ?",
            (winner_id, r["id"]),
        )
        moved += 1
    return moved


def _move_addresses(c, winner_id: int, loser_id: int) -> int:
    """Move addresses, dedupe by (line1, postcode, city). Returns count moved."""
    moved = 0
    rows = c.execute(
        "SELECT id, line1, line2, postcode, city FROM contact_addresses "
        "WHERE contact_id = ?",
        (loser_id,),
    ).fetchall()
    for r in rows:
        dup = c.execute(
            "SELECT 1 FROM contact_addresses WHERE contact_id = ? AND "
            "IFNULL(line1,'') = IFNULL(?,'') AND IFNULL(postcode,'') = IFNULL(?,'') "
            "AND IFNULL(city,'') = IFNULL(?,'')",
            (winner_id, r["line1"], r["postcode"], r["city"]),
        ).fetchone()
        if dup:
            continue
        c.execute(
            "UPDATE contact_addresses SET contact_id = ? WHERE id = ?",
            (winner_id, r["id"]),
        )
        moved += 1
    return moved


def _repoint_employer_refs(c, winner_id: int, loser_id: int) -> int:
    """Persons whose employer_contact_id == loser_id → winner_id."""
    cur = c.execute(
        "UPDATE contacts SET employer_contact_id = ? "
        "WHERE employer_contact_id = ?",
        (winner_id, loser_id),
    )
    return cur.rowcount or 0


def apply_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Apply a plan returned by ``build_plan``. Idempotent — losers that
    no longer exist are silently skipped.

    The plan may include a top-level ``dismiss`` list of contact ids to
    mark as ``status='spam'`` (no merge, no delete — just hides them
    from Active/Pending lists). Used for OCR garbage that came out of
    the extractor as a "contact" but isn't a real person/business.

    Returns a per-group breakdown plus aggregate counts."""
    results: List[Dict[str, Any]] = []
    total_deleted = 0
    total_channels = 0
    total_addresses = 0
    total_employer = 0
    total_dismissed = 0

    # Process dismissals first — these are independent of merge groups
    # and don't need the channel/address moving logic. Just status-flip.
    dismiss_ids = [int(i) for i in (plan.get("dismiss") or []) if int(i)]
    if dismiss_ids:
        with conn_ctx() as c:
            placeholders = ",".join(["?"] * len(dismiss_ids))
            cur = c.execute(
                f"UPDATE contacts SET status = 'spam' "
                f"WHERE id IN ({placeholders}) AND status != 'spam'",
                dismiss_ids,
            )
            total_dismissed = cur.rowcount or 0

    for group in plan.get("merge") or []:
        winner_id = int(group["canonical_id"])
        loser_ids = [int(i) for i in group["member_ids"] if int(i) != winner_id]
        group_result = {
            "canonical_id": winner_id,
            "deleted_ids": [],
            "channels_moved": 0,
            "addresses_moved": 0,
            "employer_refs_repointed": 0,
            "errors": [],
        }

        for loser_id in loser_ids:
            try:
                with conn_ctx() as c:
                    # Bail if the loser was already merged (or never existed)
                    exists = c.execute(
                        "SELECT 1 FROM contacts WHERE id = ?", (loser_id,)
                    ).fetchone()
                    if not exists:
                        continue
                    winner_exists = c.execute(
                        "SELECT 1 FROM contacts WHERE id = ?", (winner_id,)
                    ).fetchone()
                    if not winner_exists:
                        group_result["errors"].append({
                            "loser_id": loser_id,
                            "error": f"winner {winner_id} no longer exists",
                        })
                        continue

                    moved_ch = _move_channels(c, winner_id, loser_id)
                    moved_ad = _move_addresses(c, winner_id, loser_id)
                    repointed = _repoint_employer_refs(c, winner_id, loser_id)

                    c.execute("DELETE FROM contact_channels WHERE contact_id = ?", (loser_id,))
                    c.execute("DELETE FROM contact_addresses WHERE contact_id = ?", (loser_id,))
                    c.execute("DELETE FROM contacts WHERE id = ?", (loser_id,))

                group_result["deleted_ids"].append(loser_id)
                group_result["channels_moved"] += moved_ch
                group_result["addresses_moved"] += moved_ad
                group_result["employer_refs_repointed"] += repointed
                total_channels += moved_ch
                total_addresses += moved_ad
                total_employer += repointed
                total_deleted += 1
            except Exception as exc:  # noqa: BLE001
                log.exception("dedupe-llm: failed to merge %s into %s",
                              loser_id, winner_id)
                group_result["errors"].append({
                    "loser_id": loser_id,
                    "error": f"{type(exc).__name__}: {exc}",
                })

        results.append(group_result)

    return {
        "applied_groups": len(results),
        "deleted_contacts": total_deleted,
        "channels_moved": total_channels,
        "addresses_moved": total_addresses,
        "employer_refs_repointed": total_employer,
        "dismissed_contacts": total_dismissed,
        "groups": results,
    }
