"""Contact details out of pasted text or a photographed card — the
"smart add" helpers behind POST /api/contacts/parse-blob.

The user pastes an email signature, a letterhead, or uploads a photo of a
business card; the LLM (with vision when it is an image) returns a flat
dict that only PRE-FILLS the New-contact form. Nothing is written until
the person saves, and saving goes through the normal create path, which
checks contact identity (email / phone) before inserting.

The former top-down Paperless walk that turned every document into a
pending contact was removed in September 2026: it produced duplicates
faster than anyone could review them. The regexes and prompts below are
what remains, because the smart-add form uses them.
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
