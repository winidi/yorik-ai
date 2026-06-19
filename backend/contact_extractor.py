"""Contact extraction from Paperless documents — top-down pass.

Walks every paperless_doc_id once and proposes a contact (new or
merge with existing) based on what's in the document's header. The
existing contact_address_scraper.py + contact_enricher.py modules do
the *bottom-up* direction (start from a contact, enrich it from
docs); this one fills the gap where the contact doesn't exist yet.

Pipeline per document:
  1. Pull the doc's header zone from paperless_chunks (first ~800
     chars — German Briefe put the sender block on page 1 top).
  2. Regex pass over the WHOLE document for the perfect-extraction
     fields where regex beats the LLM on speed AND accuracy:
       - IBAN (with checksum verify)
       - email
       - phone (German formats: +49, 0049, 030 / 0511 / 0151 …)
       - USt-IdNr / VAT-id (German + EU forms)
       - Steuernummer (best-effort; no checksum)
  3. LLM pass over only the header zone with a tight JSON-out prompt
     ("extract sender name, business name, address lines"). One short
     call per doc — the rest of the doc is irrelevant to "who sent
     this letter".
  4. Fuzzy match against contacts: IBAN equality > tax_id equality >
     name+city Levenshtein > business_name substring. First positive
     match wins; the score + reason are stored alongside the
     proposal so the admin UI can show "looks like Sparkasse Hannover
     (matched on IBAN)".
  5. Write to contact_extraction_proposals — UNIQUE on
     source_paperless_doc_id makes re-runs idempotent.

Failure semantics — all best-effort:
  - LLM returns garbage / unparseable JSON → the proposal carries the
    regex-only fields, with display_name falling back to the doc's
    Paperless title. Better an incomplete proposal than a missing one.
  - paperless_chunks empty for the doc → skip; the paperless_ingest
    reconciler will populate it later.
  - Existing proposal for the same doc_id → INSERT OR IGNORE skips.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from .database import get_conn, get_docs_conn, DEFAULT_DB_PATH
from .documents import DOCS_DB_PATH

log = logging.getLogger("yorik.contact_extractor")


HEADER_CHAR_BUDGET = 800   # legacy — kept for back-compat with old prompt
MAX_REGEX_TEXT     = 8000  # legacy — regex pass is no longer the source of truth
LLM_DOC_BUDGET     = 6000  # full-text budget for the LLM-only extraction pass
LEVENSHTEIN_MATCH  = 0.85  # name similarity threshold for a "looks like"
                           # match. Tuned tight — false matches are worse
                           # than miss matches because the admin then has
                           # to undo a merge.


# ─── Regex patterns ─────────────────────────────────────────────────
#
# Patterns are deliberately conservative — better to miss than to
# misextract, because every proposal lands in the admin's review
# queue and a wrong field is more friction than a missing one.

_IBAN_RE = re.compile(
    r"\b([A-Z]{2}\d{2}(?:\s?[A-Z0-9]){11,30})\b"
)
_EMAIL_RE = re.compile(
    r"\b([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b"
)
# German phone shapes: +49, 0049, 0xxx with optional spaces/slashes.
# Deliberately requires ≥7 digits total so a 4-digit Hausnummer doesn't
# match. Allows separators but not letters.
_PHONE_RE = re.compile(
    r"(?:(?:\+|00)49[\s/\-]?|0)\d{2,5}[\s/\-]?\d{4,12}"
)
# USt-IdNr (German VAT): DE followed by 9 digits, case-insensitive,
# optionally with whitespace.
_VAT_DE_RE = re.compile(
    r"\b(DE\s?\d{9})\b", re.IGNORECASE
)
# Steuernummer (German tax number, NOT the same as VAT). Format varies
# per Bundesland: 10-13 digits with slashes (e.g. 25/123/45678). Best
# effort — there are dozens of regional formats.
_STEUERNR_RE = re.compile(
    r"\bSteuer-?Nr\.?\s*:?\s*(\d{2,3}[/\s]\d{3}[/\s]\d{4,5})\b",
    re.IGNORECASE,
)


def _iban_checksum_ok(iban: str) -> bool:
    """ISO 13616 mod-97 check. Returns False for any malformed input.
    Strips spaces and lower-cases; the rearrange-then-mod97 algorithm
    is the same for every country."""
    s = iban.replace(" ", "").upper()
    if len(s) < 15 or len(s) > 34:
        return False
    if not s[:2].isalpha() or not s[2:4].isdigit():
        return False
    # Rearrange: move first 4 chars to the end, then convert letters
    # to numbers (A=10, …, Z=35), then mod 97 == 1.
    rearranged = s[4:] + s[:4]
    expanded = []
    for ch in rearranged:
        if ch.isdigit():
            expanded.append(ch)
        elif "A" <= ch <= "Z":
            expanded.append(str(ord(ch) - 55))
        else:
            return False
    try:
        return int("".join(expanded)) % 97 == 1
    except ValueError:
        return False


def _normalize_phone(raw: str) -> str:
    """Strip separators, leave digits + leading +. Used as a stable
    form for downstream dedup."""
    cleaned = re.sub(r"[\s/\-]+", "", raw)
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]
    return cleaned


def _regex_pass(text: str) -> Dict[str, Any]:
    """Extract perfect-shape fields. Returns a dict keyed by the
    contacts column name where applicable; multi-value fields (email,
    phone) come back as lists since a doc often lists several."""
    snippet = text[:MAX_REGEX_TEXT]
    out: Dict[str, Any] = {}

    # IBAN: take the first checksum-valid match, normalize spacing out.
    for m in _IBAN_RE.finditer(snippet):
        candidate = m.group(1).replace(" ", "")
        if _iban_checksum_ok(candidate):
            out["iban"] = candidate
            break

    # Tax IDs: VAT first (more specific), then Steuernummer fallback.
    vat = _VAT_DE_RE.search(snippet)
    if vat:
        out["tax_id"] = vat.group(1).upper().replace(" ", "")
    elif (st := _STEUERNR_RE.search(snippet)):
        out["tax_id"] = st.group(1).replace(" ", "")

    # Emails & phones: dedup and keep all (often multiple per doc).
    emails = []
    seen_emails = set()
    for m in _EMAIL_RE.finditer(snippet):
        v = m.group(1).lower()
        if v not in seen_emails:
            seen_emails.add(v)
            emails.append(v)
    if emails:
        out["emails"] = emails

    phones = []
    seen_phones = set()
    for m in _PHONE_RE.finditer(snippet):
        v = _normalize_phone(m.group(0))
        # Drop obvious noise: less than 7 digits after normalisation.
        digits_only = re.sub(r"\D", "", v)
        if len(digits_only) >= 7 and v not in seen_phones:
            seen_phones.add(v)
            phones.append(v)
    if phones:
        out["phones"] = phones

    return out


# ─── LLM header pass ────────────────────────────────────────────────

_LLM_PROMPT = """You will extract contact information about the SENDER of a document
(a letter, invoice, contract, or similar). The recipient is the user;
their data must NEVER be in the output.

Output ONLY a JSON object matching the schema below. No prose before
or after, no markdown code fences.

---
DOCUMENT TEXT:
{header_text}
---

HOUSEHOLD ADDRESSES TO REJECT:
{household_addresses_block}

The lines above are the user's own residential addresses (current and
past). Any address in the document that matches one of those is the
RECIPIENT'S home, not the sender's office. NEVER emit one of those
as the sender's address. When the only address you can find in the
document is one of these, the correct output is address: null — do
NOT fall back to the household address just because nothing else is
present. Better to emit no address than the wrong address.

---

THINK STEP BY STEP:

1. Identify the document type (invoice / letter / contract / order
   confirmation / membership letter / form / etc.).

2. Count the addresses in the document:
   - TWO addresses (typical formal letter): one is the sender
     (letterhead / footer / signing block), the other is the
     recipient (window-envelope position / under "An:"). Use the
     sender's, never the recipient's.
   - ONE address (short form, simple invoice, form letter without
     letterhead): you must decide what kind of address it is. If
     the address matches a HOUSEHOLD ADDRESS above, it's the
     recipient — emit address: null. If it matches the company
     name's location (e.g. footer says "...AG, Hauptstraße 5,
     Berlin" — same address as the body), it's the sender — keep it.
   - ZERO addresses: emit address: null.

3. Find the SENDER's identity block. Heuristics:
   - Letterhead at the top-left or top-center (logo + name + address).
   - Footer with company name, register number (HRB), tax ID, IBAN —
     this is almost always the sender on German business mail.
   - "Sender" / "Absender" / "Von" / "From" labels.
   - The name signing off at the bottom (in personal letters).
   - If only the company NAME is visible (no address shown), keep
     business_name and emit address: null.

4. Identify the RECIPIENT block (so you can EXCLUDE it):
   - Address block under "An:" / "To:" / "Empfänger:".
   - First address window position on the page (window-envelope spot
     in German business mail — that's the recipient).
   - A name immediately following "Sehr geehrte/r" / "Dear" — that's
     the recipient, not the sender.
   - Order numbers, customer numbers, contract numbers near a label
     like "Kundennummer:" / "Customer no.:" / "Vertragsnummer:" —
     these belong to the recipient, NOT the sender.
   - Anything matching a HOUSEHOLD ADDRESS above.

4. Distinguish sender from recipient on every contact channel:
   - A phone number in the footer with the company logo → sender.
   - A phone number near the recipient block, especially labelled
     "Ihre Telefonnummer" / "Your phone" → recipient — SKIP IT.
   - An email like "service@..." / "kontakt@..." / "info@..." → sender.
   - An email like a personal one labelled "Ihre E-Mail-Adresse" →
     recipient — SKIP IT.

5. Pull these fields ONLY for the sender:
   - display_name: the name as it would appear in an address book.
     A real estate company → company name. A doctor's office → the
     practice name or doctor name. A friend → their full name.
   - kind: "business" or "person".
   - first_name / last_name: when kind="person", the sender's given
     name and family name as separate fields. When kind="business",
     both must be null — the business identity is in display_name /
     business_name / legal_name, not here.
   - role: the sender's job title when kind="person" AND they sign
     for an organisation ("Sachbearbeiterin", "Geschäftsführer",
     "Steuerberater"). null for kind="business" and for personal
     letters where the sender has no job role attached.
   - business_name: company / organisation name (only if applicable).
   - legal_name: full legal-form name if shown (e.g. "Stadtwerke
     Hannover AG", "Schmidt & Partner mbB"). Otherwise null.
   - address: { street_line, postcode, city, country }. country only
     if NOT Germany (assume DE when no country shown on a DE-format
     address).
   - emails: list of email addresses belonging to the sender. Lowercase.
   - phones: list of phone numbers belonging to the sender, normalised
     to E.164 (e.g. "+4951112345678"). DROP anything that's clearly
     a customer / order / invoice / contract / tax / VAT number even
     if it looks numeric. A real phone number has a country code or
     local area code pattern; an order number is usually 6–10 digits
     with no separators.
   - iban: the sender's IBAN if shown. Spaces removed, uppercase.
     Validate the IBAN length matches the country code (DE = 22,
     AT = 20, CH = 21). Skip if length is wrong.
   - tax_id: the sender's USt-IdNr / VAT number / Steuernummer if
     shown. Preserve formatting (e.g. "DE123456789").
   - salutation_pref: "Sie" | "du" | null. German business mail
     defaults to "Sie"; only emit "du" when the document clearly
     uses informal address.

6. When kind="business", ALSO list any NAMED individuals who appear
   in the document as belonging to the sender organisation
   (signatories at the bottom, contact persons in the header,
   "Ihr Ansprechpartner: Maria Schmidt"). Emit them in the persons
   array — one entry per named person, with first_name, last_name
   and role. RULES:
     - ONLY named individuals (with a first or last name in plain
       text). Skip generic placeholders like "Ihr Kundenservice"
       or "Service-Team".
     - Skip the recipient — "Sehr geehrte Frau Müller" puts Frau
       Müller in the recipient block, NOT in persons.
     - When the document has no named individual, persons is an
       empty list [], NOT null.
     - When kind="person", persons MUST be an empty list — the
       sender is already covered by the top-level first/last/role.

7. Free-text summary (one short sentence) of what the document is
   about — for the human reviewing the proposal.

NEGATIVE RULES — do NOT include:
- Anything from the recipient block.
- A "Kundennummer" / "Vertragsnummer" / "Auftragsnummer" /
  "Rechnungsnummer" / "Customer no." / "Order no." / "Invoice no."
  in the phones list. These are NEVER phone numbers.
- A VAT number in the phones list.
- "Sehr geehrte Frau Müller" — Frau Müller is the recipient.
- Made-up fields. If you can't find a value with confidence, the
  field MUST be null. An empty string is NOT acceptable.

OUTPUT FORMAT (strict JSON, this and nothing else):

{
  "display_name":   "string or null",
  "kind":           "person" or "business",
  "first_name":     "string or null",
  "last_name":      "string or null",
  "role":           "string or null",
  "business_name":  "string or null",
  "legal_name":     "string or null",
  "address": {
    "street_line":  "string or null",
    "postcode":     "string or null",
    "city":         "string or null",
    "country":      "string or null"
  },
  "emails":  ["string", ...],
  "phones":  ["string", ...],
  "iban":    "string or null",
  "tax_id":  "string or null",
  "salutation_pref": "Sie" or "du" or null,
  "persons": [
    { "first_name": "string", "last_name": "string or null", "role": "string or null" },
    ...
  ],
  "document_summary": "one short sentence describing what this is"
}
"""


def _household_addresses_block() -> str:
    """Format the user's known residential addresses for injection into
    the extractor prompt. Uses the same source as the dedupe layer so
    the two stay in sync.

    Auto-detection (from contact_dedupe_llm) needs a corpus of contacts
    to learn from. The extractor runs PER-DOC, so we only pull the
    explicit set here (user_profiles + manual list) — auto-detect
    would need a pre-computed cache to be useful here, which is post-
    launch work. For now, the user_profiles row(s) cover the common
    case: a household whose current address is on file.
    """
    try:
        from . import contacts_dedupe_llm as _dl
        # _load_residential_addresses with contacts=None merges
        # user_profiles + the manual cache in app_settings AND
        # deduplicates. Auto-detection runs on dedupe time over the
        # contact corpus; we persist its output into the same
        # settings cache (see snapshot_residential_addresses below)
        # so the extractor sees those too on subsequent runs.
        addrs = _dl._load_residential_addresses(contacts=None)  # noqa: SLF001
    except Exception:
        return "  (none)"
    if not addrs:
        return "  (none — assume nothing about the recipient's address)"
    lines = []
    for a in addrs:
        parts = [a.get("line1") or "", a.get("postcode") or "", a.get("city") or ""]
        line = ", ".join(p for p in parts if p)
        if line:
            lines.append(f"  - {line}")
    return "\n".join(lines) if lines else "  (none)"


def _llm_extract_full(doc_text: str) -> Dict[str, Any]:
    """Single LLM call over the full document. Returns the parsed
    JSON dict (flattened to the schema downstream consumers expect)
    or {} on any failure.

    The new prompt asks the model to think step-by-step about sender
    vs recipient before emitting JSON, and explicitly lists what NOT
    to include (customer / order / VAT numbers in the phones list,
    recipient block data, etc.). The result is then flattened — the
    prompt's nested address object becomes the flat address_* fields
    the rest of the extractor pipeline writes to.
    """
    if not doc_text.strip():
        return {}
    from .agent.llm import LlmClient
    client = LlmClient(
        model=os.getenv("HOMEOS_MODEL", "qwen3.5-9b"),
        base_url=os.getenv("HOMEOS_LLM_BASE_URL", "http://127.0.0.1:8080/v1"),
    )
    household_block = _household_addresses_block()
    prompt = (
        _LLM_PROMPT
        .replace("{header_text}", doc_text[:LLM_DOC_BUDGET])
        .replace("{household_addresses_block}", household_block)
    )
    try:
        resp = client.chat(
            messages=[{"role": "user", "content": prompt}],
            # Wider budget than the old header-only call because the
            # JSON now has emails / phones lists + a summary. Reasoning
            # budget (when the model supports it) is set at the
            # inference server level — not here.
            max_tokens=1200,
            temperature=0.0,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("contact_extractor LLM call failed: %s", exc)
        return {}

    content = (resp.get("content") or "").strip()
    return _flatten_extracted_json(content)


def _flatten_extracted_json(content: str) -> Dict[str, Any]:
    """Parse the LLM's raw text reply (after ```json strip) into the flat
    schema downstream consumers expect.

    Shared by _llm_extract_full (text path) and _llm_extract_full_vision
    (image / scanned-PDF path) so both extractors return the same shape.
    """
    # Defensive: strip ```json fences the model sometimes adds despite
    # the prompt. Same handling pattern as contact_address_scraper.
    content = re.sub(r"^```(?:json)?\s*", "", content or "")
    content = re.sub(r"\s*```$", "", content)
    if not content:
        return {}
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", content, re.DOTALL)
        if not m:
            return {}
        try:
            parsed = json.loads(m.group(0))
        except json.JSONDecodeError:
            return {}
    if not isinstance(parsed, dict):
        return {}

    # Flatten the prompt's nested {"address": {street_line, postcode,
    # city, country}} into the flat address_* keys the downstream
    # pipeline writes to. Preserves prior schema → no downstream
    # consumer changes needed.
    flat: Dict[str, Any] = {}
    addr = parsed.get("address")
    if isinstance(addr, dict):
        flat["address_street"]   = addr.get("street_line") or None
        flat["address_postcode"] = addr.get("postcode")    or None
        flat["address_city"]     = addr.get("city")        or None
        flat["address_country"]  = addr.get("country")     or None
    # Top-level fields — preserve as-is, normalise blank-to-None.
    # first_name / last_name / role added with mig 045: when kind=
    # "person" the sender's own name lives here, separate from the
    # already-flat display_name; the prompt forbids them for
    # kind="business" so they'll come back null on company rows.
    for k in ("display_name", "kind", "first_name", "last_name", "role",
              "business_name", "legal_name",
              "iban", "tax_id", "salutation_pref", "document_summary"):
        v = parsed.get(k)
        flat[k] = v if v not in ("", None) else None
    # Lists — coerce to list of non-empty strings.
    for k in ("emails", "phones"):
        v = parsed.get(k)
        if isinstance(v, list):
            cleaned = [str(item).strip() for item in v if str(item).strip()]
            if k == "emails":
                cleaned = [e.lower() for e in cleaned]
            flat[k] = cleaned
        else:
            flat[k] = []
    # persons array — named individuals belonging to a business sender
    # (signatories, "Ihr Ansprechpartner …"). Each entry becomes its
    # own kind="person" contact row linked back to the business via
    # employer_contact_id. Validate shape and drop anything missing a
    # first_name (we need at least that to identify the human).
    persons_raw = parsed.get("persons")
    persons_out: List[Dict[str, Any]] = []
    if isinstance(persons_raw, list):
        for entry in persons_raw:
            if not isinstance(entry, dict):
                continue
            first = (entry.get("first_name") or "").strip() or None
            last  = (entry.get("last_name")  or "").strip() or None
            role_ = (entry.get("role")       or "").strip() or None
            if not first and not last:
                continue
            persons_out.append({
                "first_name": first,
                "last_name":  last,
                "role":       role_,
            })
    flat["persons"] = persons_out
    return flat


_SMART_ADD_PROMPT = """You will extract contact information about a single
entity (person or business) from the input below. The input may be:
- a free-text paste (website snippet, email signature, business card),
- a scanned letter or invoice (sender is the entity),
- a photo of a letterhead, business card, or notice.

Output ONLY a JSON object matching the schema below. No prose before
or after, no markdown code fences.

---
SOURCE TYPE: {source_type}
{input_block}
---

HOUSEHOLD ADDRESSES TO REJECT:
{household_addresses_block}

The lines above are the user's own residential addresses. NEVER emit
one of those as the contact's address — those belong to the user, not
the contact being added.

---

Pull these fields for the contact:
- display_name: the name as it would appear in an address book.
- kind: "person" if the entity is an individual; "business" if it's a
  company, authority, practice, or other organisation. Legal-entity
  suffixes (GmbH, AG, Ltd, e.V., …), branded names, or known
  organisation names → "business". A first-name + last-name pair →
  "person".
- first_name / last_name / role: only when kind="person" and the input
  reveals them. For "business" leave these null.
- business_name / legal_name: when kind="business". legal_name is the
  full registered name including suffix; business_name is the trading
  name (often the same).
- address: {{ street_line, postcode, city, country }} — street_line is
  "Street 123" or "Street 123, Bldg B". Country is the ISO 3166-1
  alpha-2 code ("DE" / "US" / "AT"). Null any field that's not present.
- emails: array of email addresses, lowercased.
- phones: array of phone numbers as written.
- iban / tax_id: only when explicitly stated in the input.
- salutation_pref: "du" or "Sie" when the input clearly signals one;
  otherwise null.
- document_summary: null (not needed for smart-add).

JSON schema:
{{
  "display_name": "...",
  "kind": "person" | "business",
  "first_name": "...", "last_name": "...", "role": "...",
  "business_name": "...", "legal_name": "...",
  "address": {{ "street_line": "...", "postcode": "...",
               "city": "...", "country": "DE" }},
  "emails": ["..."], "phones": ["..."],
  "iban": null, "tax_id": null,
  "salutation_pref": null, "document_summary": null,
  "persons": []
}}
"""


def _llm_extract_full_vision(
    image_payloads: List[Dict[str, Any]],
    *,
    hint_text: str = "",
) -> Dict[str, Any]:
    """Vision counterpart of _llm_extract_full.

    `image_payloads` is a list of OpenAI multimodal `{"type":
    "image_url", "image_url": {"url": "data:image/...;base64,..."}}`
    dicts — same shape read_document_vision builds for PDF pages /
    image uploads. `hint_text` is optional extra context (e.g. the
    user's typed prompt accompanying the image).

    Returns {} on failure. Same flat schema as _llm_extract_full.
    """
    if not image_payloads:
        return {}
    from .agent.llm import LlmClient
    client = LlmClient(
        model=os.getenv("HOMEOS_MODEL", "qwen3.5-9b"),
        base_url=os.getenv("HOMEOS_LLM_BASE_URL", "http://127.0.0.1:8080/v1"),
    )
    household_block = _household_addresses_block()
    prompt = _SMART_ADD_PROMPT.format(
        source_type="image / scanned page",
        input_block="(see attached image — extract from the visual content)",
        household_addresses_block=household_block,
    )
    if hint_text.strip():
        prompt += f"\n\nUSER HINT (optional context): {hint_text.strip()[:500]}"
    content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
    content.extend(image_payloads)
    try:
        resp = client.chat(
            messages=[{"role": "user", "content": content}],
            max_tokens=1200,
            temperature=0.0,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("contact_extractor vision LLM call failed: %s", exc)
        return {}
    return _flatten_extracted_json((resp.get("content") or "").strip())


def _llm_extract_full_smart_text(text: str) -> Dict[str, Any]:
    """Text-path smart-add extractor. Reuses _SMART_ADD_PROMPT (no
    sender-vs-recipient framing — the input is the entity itself) so
    pasted snippets like Google Maps results, email signatures, or
    business cards extract cleanly without the document-letter framing
    that _llm_extract_full assumes.
    """
    if not text.strip():
        return {}
    from .agent.llm import LlmClient
    client = LlmClient(
        model=os.getenv("HOMEOS_MODEL", "qwen3.5-9b"),
        base_url=os.getenv("HOMEOS_LLM_BASE_URL", "http://127.0.0.1:8080/v1"),
    )
    household_block = _household_addresses_block()
    prompt = _SMART_ADD_PROMPT.format(
        source_type="pasted text",
        input_block=f"INPUT TEXT:\n{text[:LLM_DOC_BUDGET]}",
        household_addresses_block=household_block,
    )
    try:
        resp = client.chat(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1200,
            temperature=0.0,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("contact_extractor smart-text LLM call failed: %s", exc)
        return {}
    return _flatten_extracted_json((resp.get("content") or "").strip())


# ─── Per-doc text fetch ─────────────────────────────────────────────

def _fetch_doc_text(paperless_doc_id: int) -> str:
    """Pull text for one doc from paperless_chunks. Returns the full
    text capped at LLM_DOC_BUDGET — the LLM-only extractor handles
    the rest. Returns "" when the doc isn't indexed yet."""
    chunks: List[str] = []
    try:
        with get_docs_conn(DOCS_DB_PATH) as conn:
            rows = conn.execute(
                "SELECT chunk_index, text FROM paperless_chunks "
                "WHERE paperless_doc_id=? ORDER BY chunk_index ASC LIMIT 8",
                (paperless_doc_id,),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001
        log.warning("fetch_doc_text: docs db unreachable for %d: %s",
                    paperless_doc_id, exc)
        return ""
    for r in rows:
        chunks.append(r["text"] or "")
    return ("\n".join(chunks))[:LLM_DOC_BUDGET]


# ─── Fuzzy matcher against existing contacts ────────────────────────

def _ratio(a: str, b: str) -> float:
    """Cheap Levenshtein-ratio without pulling python-Levenshtein.
    SequenceMatcher is in stdlib and gets us within a few percentage
    points of true Levenshtein; the matcher tolerates it because
    LEVENSHTEIN_MATCH is set conservatively."""
    from difflib import SequenceMatcher
    return SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()


def _match_against_contacts(
    proposed: Dict[str, Any],
) -> Optional[Tuple[int, float, str]]:
    """Look for an existing contact this proposal might refer to.
    Returns (contact_id, score, reason) of the strongest match, or
    None when nothing exceeds the threshold.

    Priority (first hit wins):
      1. IBAN equality              — 1.0 (only one IBAN per business)
      2. tax_id equality            — 1.0 (USt-IdNr is globally unique)
      3. display_name ratio + city  — Levenshtein ≥ threshold AND same
                                       city, when both have one
      4. display_name ratio alone   — Levenshtein ≥ threshold
    """
    iban    = (proposed.get("iban") or "").strip()
    tax_id  = (proposed.get("tax_id") or "").strip()
    name    = (proposed.get("display_name")
               or proposed.get("business_name")
               or proposed.get("legal_name") or "").strip()
    city    = (proposed.get("address_city") or "").strip().lower()

    if not (iban or tax_id or name):
        return None

    with get_conn() as conn:
        # IBAN — only ever one match expected.
        if iban:
            row = conn.execute(
                "SELECT id FROM contacts WHERE iban = ? COLLATE NOCASE",
                (iban,),
            ).fetchone()
            if row:
                return (int(row["id"]), 1.0, "iban")

        # tax_id — same, one expected.
        if tax_id:
            row = conn.execute(
                "SELECT id FROM contacts WHERE tax_id = ? COLLATE NOCASE",
                (tax_id,),
            ).fetchone()
            if row:
                return (int(row["id"]), 1.0, "tax_id")

        if not name:
            return None

        # Name fuzzy. Pull every active contact's display_name +
        # business legal_name; the table is tiny by design (≤ a few
        # thousand) so iterating in Python is fine.
        rows = conn.execute(
            "SELECT id, display_name, legal_name "
            "FROM contacts WHERE status='active'",
        ).fetchall()

    best: Optional[Tuple[int, float, str]] = None
    for r in rows:
        # Score against either display_name OR legal_name, whichever
        # is closer (covers e.g. "Sparkasse Hannover" vs the legal
        # "Sparkasse Hannover AöR").
        candidates = [c for c in (r["display_name"], r["legal_name"]) if c]
        for cand in candidates:
            score = _ratio(name, cand)
            if score < LEVENSHTEIN_MATCH:
                continue
            reason = "name"
            # Optional city co-occurrence bumps confidence — if the
            # contact has an address we can compare with, use it.
            if city:
                # Doesn't necessarily exist — addresses live in the
                # contacts_addresses side table — punt on that for
                # now (would need a JOIN); rely on name match alone.
                pass
            if not best or score > best[1]:
                best = (int(r["id"]), score, reason)
    return best


# ─── Public API ─────────────────────────────────────────────────────

def extract_from_doc(paperless_doc_id: int) -> Optional[Dict[str, Any]]:
    """Run the LLM-only pipeline on one doc. Returns the proposal
    dict the worker writes to contact_extraction_proposals, or None
    when the LLM couldn't identify a sender. Caller is responsible
    for the DB write.

    Migration note (2026-06-05): this used to be LLM-for-identity +
    regex-for-channels (IBAN / phone / email). The regex pass produced
    too many wrong phone numbers (customer / order / contract numbers
    leaking through) and gave the LLM nothing to disambiguate sender
    from recipient on channels. Now LLM-only: one prompt extracts
    everything, with explicit sender-vs-recipient heuristics in the
    prompt. The regex helpers remain in the file as dead code in
    case we want to add belt-and-braces IBAN/email validation later,
    but they're no longer called.
    """
    doc_text = _fetch_doc_text(paperless_doc_id)
    if not doc_text:
        return None

    proposed = _llm_extract_full(doc_text)
    if not proposed:
        return None

    # Identity floor: refuse to propose a contact with no name AND
    # no IBAN AND no email. Would be junk.
    if not (proposed.get("display_name")
            or proposed.get("business_name")
            or proposed.get("legal_name")
            or proposed.get("iban")
            or proposed.get("emails")):
        return None

    # Default kind when the LLM was uncertain (rare — the prompt asks
    # for it explicitly — but defensive).
    if not proposed.get("kind"):
        proposed["kind"] = "business" if proposed.get("business_name") else "person"

    # Short snippet for admin review. Prefer the LLM's one-sentence
    # summary; fall back to the doc's first 500 chars when the model
    # didn't emit one.
    summary = (proposed.get("document_summary") or "").strip()
    proposed["source_snippet"] = summary or doc_text[:500]

    match = _match_against_contacts(proposed)

    return {
        "source_paperless_doc_id": paperless_doc_id,
        "proposed": proposed,
        "match": match,  # (contact_id, score, reason) or None
    }


def write_extracted_sides(
    conn,
    contact_id: int,
    proposed: Dict[str, Any],
    *,
    overwrite_address: bool,
) -> None:
    """Write the proposal's address + email + phone fields into the
    contacts side tables (contact_addresses + contact_channels). Called
    from both write_pending_contact_from_doc (scanner-side) and from
    decide_contact_extraction's accept_create / accept_merge paths in
    main.py — they all need the same dispatch logic.

    Without this helper those fields were silently dropped (original
    cause of the "Landkreis München has no info" bug).

    - Address: one row per kind in contact_addresses. accept_create
      writes a fresh 'work' (business) / 'home' (person) row;
      accept_merge only inserts when no address of that kind exists.
    - Channels: one row per email + per phone, deduped against rows
      already on the contact so re-running stays idempotent.
      source='paperless' tags the audit trail.
    """
    addr_kind = "work" if (proposed.get("kind") or "").lower() == "business" else "home"
    addr_line = (proposed.get("address_street") or "").strip()
    addr_pc   = (proposed.get("address_postcode") or "").strip()
    addr_city = (proposed.get("address_city") or "").strip()
    addr_cnty = (proposed.get("address_country") or "").strip()
    if addr_line or addr_pc or addr_city:
        already = conn.execute(
            "SELECT 1 FROM contact_addresses "
            "WHERE contact_id = ? AND kind = ? LIMIT 1",
            (contact_id, addr_kind),
        ).fetchone()
        if not already or overwrite_address:
            if already and overwrite_address:
                conn.execute(
                    "DELETE FROM contact_addresses "
                    "WHERE contact_id = ? AND kind = ?",
                    (contact_id, addr_kind),
                )
            conn.execute(
                "INSERT INTO contact_addresses "
                "(contact_id, kind, line1, postcode, city, country) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (contact_id, addr_kind, addr_line or None,
                 addr_pc or None, addr_city or None,
                 addr_cnty or None),
            )

    # contact_channels has a GLOBAL `UNIQUE (kind, value)` — same
    # email can't appear on two contacts (intentional: it lets inbound
    # routing ask "do we know this address?" in one indexed lookup).
    # So an extracted email/phone that's already attached to ANY other
    # contact must be skipped, not just deduped within this contact.
    # Naively inserting blows up the whole transaction.
    own_channels = {
        (r["kind"], (r["value"] or "").strip().lower())
        for r in conn.execute(
            "SELECT kind, value FROM contact_channels WHERE contact_id = ?",
            (contact_id,),
        ).fetchall()
    }

    def _try_insert_channel(kind: str, value: str) -> None:
        normalised = value.strip().lower() if kind == "email" else value.strip()
        key = (kind, normalised.lower())
        if not normalised or key in own_channels:
            return
        # Check global existence first to avoid a noisy IntegrityError
        # in the application log; the UNIQUE check still backs us up.
        already = conn.execute(
            "SELECT contact_id FROM contact_channels "
            "WHERE kind = ? AND value = ? LIMIT 1",
            (kind, normalised),
        ).fetchone()
        if already:
            # Channel already known on another contact — skip silently.
            # User can manually merge the duplicate later. The note on
            # this contact (set by the caller) flags the likely match
            # via match_candidate_id when one was detected by score.
            return
        try:
            conn.execute(
                "INSERT INTO contact_channels "
                "(contact_id, kind, value, source) "
                "VALUES (?, ?, ?, 'paperless')",
                (contact_id, kind, normalised),
            )
            own_channels.add(key)
        except Exception:  # noqa: BLE001
            # Race or check-constraint violation — never crash the
            # contact create over a channel side-write.
            pass

    for email in (proposed.get("emails") or []):
        _try_insert_channel("email", email or "")
    for phone in (proposed.get("phones") or []):
        _try_insert_channel("phone", phone or "")


def write_pending_contact_from_doc(result: Dict[str, Any]) -> Optional[int]:
    """Walk one extract_from_doc() result straight into contacts.

    Replaces the old write_proposal(). The scanner used to park
    candidates in contact_extraction_proposals waiting for the
    admin to review them at Settings → Extractions — a separate
    screen that produced exactly the friction the user called out
    ("flipping between Extractions and Contacts makes no sense").
    Now we write the contact directly as status='pending', the
    address + channels are attached via write_extracted_sides, and
    the new row appears in the existing Contacts → Pending tab
    alongside email/wa auto-captures. Same UX for every "Yorik
    found this for you, please confirm" entry.

    The contact_extraction_proposals row is still written, as a
    pure audit/idempotency trail: status='accepted', created_contact_id
    pointing at the new contact, decided_at=now. The reconcile loop
    uses these rows to skip docs that already have a contact, so
    re-running the scan never produces dupes. UNIQUE on
    source_paperless_doc_id is the load-bearing constraint.

    Returns the new contact_id, or None when there isn't enough to
    create a contact (no display_name AND no IBAN AND no email —
    caller writes a 'skipped' tombstone instead).
    """
    proposed = result["proposed"]
    paperless_doc_id = result["source_paperless_doc_id"]
    match = result.get("match")  # (contact_id, score, reason) | None

    display_name = (
        proposed.get("display_name")
        or proposed.get("business_name")
        or proposed.get("legal_name")
    )
    if not display_name:
        return None  # caller falls through to the skipped-tombstone path

    kind = proposed.get("kind", "business")
    notes_lines = [f"Extracted from Paperless doc #{paperless_doc_id}."]
    if match:
        # Note the lookalike so admin can spot the dupe at a glance.
        # We don't auto-merge — even an IBAN match could be a sibling
        # account from the same bank, etc.
        notes_lines.append(
            f"Possible match: contact #{match[0]} (score {match[1]:.2f} on {match[2]})."
        )
    notes = " ".join(notes_lines)

    proposed_json = json.dumps(proposed, ensure_ascii=False)
    match_id     = match[0] if match else None
    match_score  = match[1] if match else None
    match_reason = match[2] if match else None
    with get_conn() as conn:
        # Idempotency: refuse to double-create. The UNIQUE constraint
        # on source_paperless_doc_id would trip, but checking first
        # gives us a cleaner None return.
        prior = conn.execute(
            "SELECT created_contact_id FROM contact_extraction_proposals "
            "WHERE source_paperless_doc_id = ?",
            (paperless_doc_id,),
        ).fetchone()
        if prior:
            return None

        # 1. Create the contact in Pending. Person-identity columns
        # (mig 045) are populated when kind="person" — the prompt
        # forces them null for kind="business" so a defensive coalesce
        # would be redundant.
        is_person = (kind or "").lower() == "person"
        # Phase C: extracted contacts land in WS1's Household. Without
        # an explicit space_id, member-role users wouldn't see them at
        # all (spaces.row_filter excludes NULL). Extractors process the
        # bundled connectors which are WS1-scoped today; future multi-
        # workspace email/Paperless will need per-workspace routing.
        hh_row = conn.execute(
            "SELECT id FROM spaces WHERE slug='household' LIMIT 1"
        ).fetchone()
        hh_space_id = int(hh_row[0]) if hh_row else None
        cur = conn.execute(
            "INSERT INTO contacts "
            "(display_name, kind, status, notes, "
            " first_name, last_name, role, space_id) "
            "VALUES (?, ?, 'pending', ?, ?, ?, ?, ?)",
            (
                display_name, kind, notes,
                proposed.get("first_name") if is_person else None,
                proposed.get("last_name")  if is_person else None,
                proposed.get("role")       if is_person else None,
                hh_space_id,
            ),
        )
        contact_id = int(cur.lastrowid)

        # 2. Top-level columns the LLM/regex extracted.
        for col in ("legal_name", "tax_id", "iban", "salutation_pref"):
            v = proposed.get(col)
            if v:
                conn.execute(
                    f"UPDATE contacts SET {col} = ? WHERE id = ?",
                    (v, contact_id),
                )

        # 3. Side tables.
        write_extracted_sides(conn, contact_id, proposed,
                               overwrite_address=True)

        # 4. Linked persons (mig 045). When the sender is a business
        # the LLM may have listed signatories / Ansprechpartner in
        # `persons[]`; each becomes its own kind="person" contact row
        # pointing back via employer_contact_id. Status is also
        # 'pending' so the admin reviews them next to the parent.
        # Per-person idempotency is best-effort: we don't dedup
        # against existing persons on the same employer because the
        # display_name + role combination is what the user sees and
        # accidental duplicates can be merged from the UI later.
        if not is_person:
            for person in proposed.get("persons") or []:
                first = person.get("first_name")
                last  = person.get("last_name")
                role_ = person.get("role")
                if not first and not last:
                    continue
                person_display = " ".join(p for p in (first, last) if p).strip()
                if not person_display:
                    continue
                person_notes = (
                    f"Extracted from Paperless doc #{paperless_doc_id} "
                    f"as a contact at {display_name}."
                )
                conn.execute(
                    "INSERT INTO contacts "
                    "(display_name, kind, status, notes, "
                    " first_name, last_name, role, employer_contact_id, "
                    " space_id) "
                    "VALUES (?, 'person', 'pending', ?, ?, ?, ?, ?, ?)",
                    (person_display, person_notes,
                     first, last, role_, contact_id, hh_space_id),
                )

        # 5. Audit trail in proposals — single source of truth for
        # "this doc has been processed."
        conn.execute(
            "INSERT INTO contact_extraction_proposals "
            "(source_paperless_doc_id, proposed_json, "
            " match_candidate_id, match_score, match_reason, "
            " status, created_contact_id, decided_at) "
            "VALUES (?, ?, ?, ?, ?, 'accepted', ?, datetime('now'))",
            (paperless_doc_id, proposed_json,
             match_id, match_score, match_reason,
             contact_id),
        )
        conn.commit()
        return contact_id


# Back-compat shim: nothing in tree calls this anymore, but the
# accept_create endpoint in main.py used to read its return shape.
# Kept here as a one-line forwarder so an external caller (a saved
# script, a smoke test) doesn't break on import.
def write_proposal(result: Dict[str, Any]) -> Optional[int]:
    return write_pending_contact_from_doc(result)


# ─── Manual trigger machinery ───────────────────────────────────────
#
# This module used to ship a long-lived background worker that ticked
# every 6 h. It was changed to admin-triggered (a button in the
# Contacts app) because the full corpus scan is multi-hour LLM work
# and burning that CPU silently on every install isn't the right
# default — the admin pays the cost when THEY want the queue
# populated. The reconcile_once() function is unchanged; main.py
# wraps it in a background asyncio task spawned from the /run
# endpoint, and _run_lock keeps two parallel runs from racing.

import asyncio as _asyncio

WORKER_NAME = "contact_extractor"
_run_lock: "_asyncio.Lock | None" = None  # lazy-init so module import
                                          # doesn't need an event loop
_last_run_summary: Dict[str, Any] = {}

# Cancellation: a plain module-level bool that the per-doc loop checks
# between iterations. Reads + writes of a single bool are atomic in
# CPython so no lock is needed; the runtime cost is one branch per doc.
# Reset on every fresh run so a previous cancel doesn't bleed into the
# next click.
_cancel_requested: bool = False

# Live progress so the UI can render a real bar + counter while the
# scan is in flight. `total` is set once we know how many docs are
# going to be processed (after the diff against already-seen); current
# ticks per doc. Both reset to 0 between runs.
_progress: Dict[str, Any] = {"current": 0, "total": 0, "started_at": None}


def _get_lock() -> "_asyncio.Lock":
    global _run_lock
    if _run_lock is None:
        _run_lock = _asyncio.Lock()
    return _run_lock


def is_running() -> bool:
    """True when a reconcile is currently in flight. Cheap — the lock
    state is a single bool. Used by the status endpoint so the UI can
    grey out the Scan button instead of letting the user fire a
    duplicate."""
    lock = _get_lock()
    return lock.locked()


def last_run_summary() -> Dict[str, Any]:
    """Snapshot of the most recent reconcile_once() return value.
    Empty dict until the first manual run completes. Carries
    {checked, processed, proposed, skipped, finished_at} (or
    {error, finished_at} on failure)."""
    return dict(_last_run_summary)


def request_cancel() -> None:
    """Ask the in-flight scan to stop at the next safe point (between
    documents — we never cancel mid-doc to avoid corrupting state).
    No-op if nothing is running. UI calls this from the Stop button."""
    global _cancel_requested
    _cancel_requested = True


def get_progress() -> Dict[str, Any]:
    """Live progress for the status endpoint. {current, total,
    started_at}. Zeroed between runs so the UI can detect "scan
    finished" by total going back to 0."""
    return dict(_progress)


async def run_manual() -> Dict[str, Any]:
    """Acquire the run-lock, run reconcile_once on a thread, stash the
    summary for the status endpoint. Raises asyncio.LockError-style
    behaviour (we just return early) if another run is already in
    flight — duplicate trigger detection lives in the route handler.

    Heartbeats into the workers registry on each phase so Settings →
    Quality surfaces the work."""
    from . import workers
    lock = _get_lock()
    if lock.locked():
        return {"skipped": "already_running"}
    async with lock:
        workers.register(WORKER_NAME, kind="batch",
                         expected_interval_s=0)  # ad-hoc, no schedule
        workers.heartbeat(WORKER_NAME, "ok", "scanning Paperless documents…")
        try:
            res = await _asyncio.to_thread(reconcile_once)
        except Exception as exc:  # noqa: BLE001
            log.warning("contact_extractor manual run raised: %s", exc)
            res = {"error": str(exc)}
        from datetime import datetime, timezone
        res["finished_at"] = datetime.now(timezone.utc).isoformat()
        _last_run_summary.clear()
        _last_run_summary.update(res)
        if "error" in res:
            workers.heartbeat(WORKER_NAME, "warn",
                              f"run failed: {res['error'][:60]}")
        else:
            workers.heartbeat(
                WORKER_NAME, "ok",
                f"{res.get('proposed', 0)} new proposal(s), "
                f"{res.get('skipped', 0)} skipped, "
                f"{res.get('checked', 0)} total docs",
            )
        return res


RUN_EVERY_S = 6 * 3600  # historical — kept for backwards-compat with
                        # any caller that still imports the constant
                        # (none today, but a follow-up that adds an
                        # admin-toggleable schedule will want it).


def reconcile_once() -> Dict[str, Any]:
    """Diff paperless_chunks's distinct doc ids vs already-proposed,
    process the new ones. Idempotent — re-running picks up only
    docs that haven't been seen yet. Returns a summary dict for the
    worker heartbeat."""
    try:
        with get_docs_conn(DOCS_DB_PATH) as dconn:
            doc_rows = dconn.execute(
                "SELECT DISTINCT paperless_doc_id "
                "FROM paperless_chunks ORDER BY paperless_doc_id",
            ).fetchall()
        all_doc_ids = {int(r["paperless_doc_id"]) for r in doc_rows}
    except Exception as exc:  # noqa: BLE001 — docs.db missing on fresh install
        log.warning("contact_extractor: docs db unreachable: %s", exc)
        return {"checked": 0, "processed": 0, "proposed": 0,
                "skipped": 0, "error": str(exc)}

    with get_conn() as conn:
        prior_rows = conn.execute(
            "SELECT source_paperless_doc_id "
            "FROM contact_extraction_proposals",
        ).fetchall()
    seen = {int(r["source_paperless_doc_id"]) for r in prior_rows}

    todo = sorted(all_doc_ids - seen)
    if not todo:
        # Still reset progress so the UI's status endpoint reads
        # "nothing in flight" cleanly.
        _progress.update({"current": 0, "total": 0, "started_at": None})
        return {"checked": len(all_doc_ids), "processed": 0,
                "proposed": 0, "skipped": 0, "cancelled": False}

    # Mark the start: total now known, current resets, started_at is
    # used by the UI to show "running since 14:23:11" if the run is
    # long-lived. Reset the cancel flag here too so a stale request
    # from a prior session doesn't immediately abort.
    global _cancel_requested
    _cancel_requested = False
    from datetime import datetime, timezone
    _progress["total"]      = len(todo)
    _progress["current"]    = 0
    _progress["started_at"] = datetime.now(timezone.utc).isoformat()

    log.info("contact_extractor: %d new doc(s) to process", len(todo))
    proposed  = 0
    skipped   = 0
    cancelled = False
    for i, did in enumerate(todo):
        # Cancel point — only between docs, so a half-written proposal
        # never reaches the DB. The UI's stop button sets this flag.
        if _cancel_requested:
            cancelled = True
            log.info("contact_extractor: cancelled at %d/%d",
                     i, len(todo))
            break
        _progress["current"] = i
        try:
            result = extract_from_doc(did)
        except Exception as exc:  # noqa: BLE001
            log.warning("contact_extractor: extract failed for %d: %s",
                        did, exc)
            skipped += 1
            continue
        if result is None:
            skipped += 1
            # Even for skipped docs, write a tombstone row so we don't
            # re-process them next tick.
            with get_conn() as conn:
                try:
                    conn.execute(
                        "INSERT INTO contact_extraction_proposals "
                        "(source_paperless_doc_id, proposed_json, status) "
                        "VALUES (?, '{}', 'skipped')",
                        (did,),
                    )
                    conn.commit()
                except Exception:  # noqa: BLE001 — UNIQUE collision is fine
                    pass
            continue
        # Direct write to contacts.status='pending'. The function
        # also stamps an idempotency row in contact_extraction_proposals
        # so the next reconcile tick's `seen` set picks up this doc.
        if write_pending_contact_from_doc(result) is not None:
            proposed += 1
        else:
            skipped += 1
    # Final tick — current matches total (or wherever cancel hit).
    _progress["current"] = i + (0 if cancelled else 1)

    # Reset progress to {0,0,None} so the UI knows the scan is done.
    # We hand the final counts back via the return value (also stashed
    # into _last_run_summary by run_manual).
    _progress.update({"current": 0, "total": 0, "started_at": None})

    return {"checked": len(all_doc_ids), "processed": i + (0 if cancelled else 1),
            "proposed": proposed, "skipped": skipped,
            "cancelled": cancelled}


_BACKGROUND_WORKER_REMOVED = (
    "The background_worker() / background tick was removed when the "
    "feature switched to admin-triggered. Use run_manual() from the "
    "/api/contacts/extractions/run endpoint instead."
)
