---
name: find_recipient_address_from_documents
description: Find postal addresses in past Paperless docs for a contact; always confirm with the user.
when_to_use: |
  When compose_check_recipient returned complete=false (the contact has
  no postal address on file), call this skill BEFORE asking the user.

  Flow:
    compose_check_recipient → complete=false
      ↓
    find_recipient_address_from_documents(contact_id=42)
      ↓
    candidates=[…]   → present top candidate(s) to user, one short question:
                       "Found this address in 'Mietvertrag 2024-09': Muststraße 12,
                        30159 Hannover. Use it?"
    candidates=[]    → no Paperless hit, ASK user for the address.

  When the user confirms a candidate:
    1. Call add_contact_address(contact_id=…, kind='home', line1=…, …)
       so it's saved for next time.
    2. Call compose_draft(…).

  Don't call without compose_check_recipient first — this is the fallback when no address is on file, not a replacement.

  Cached results expire after 30 days (pass `use_cache=false` to force re-scan). Caps at 5 docs/call.
inputs:
  contact_id:
    type: integer
    required: true
    description: Contact id returned by find_person.
  max_docs:
    type: integer
    required: false
    default: 5
    description: Cap on Paperless documents to scan. 1–10 sensible.
  use_cache:
    type: boolean
    required: false
    default: true
    description: When true (default), returns previously-mined candidates if any exist. Pass false to force a fresh Paperless scan + LLM extract.
outputs:
  candidates:
    type: array
    description: Address candidates with source_doc attribution. Empty if nothing found. Sorted by confidence DESC.
  scanned_count:
    type: integer
    description: Number of Paperless documents actually scanned this call (0 when served from cache).
  cached:
    type: boolean
    description: True when the result came from the cache, false on fresh scan.
permissions: [admin, member]
side_effects: writes to contact_address_suggestions cache table (no external API mutations).
tags: [compose, contacts, paperless, address, ocr]
---

# find_recipient_address_from_documents

The magic step in the Compose playbook. When `compose_check_recipient`
returns `complete=false` — meaning the contact has no postal address on
file — this skill scans the user's Paperless documents for past
correspondence with that person and extracts any postal addresses it
finds.

The user almost always *has* the address — it's on a past invoice, on
the rental contract, on a letter they scanned. The LLM looking it up
once and remembering it (via the `contact_address_suggestions` cache)
is dramatically better UX than asking "wie ist die Adresse von Hans"
every time.

## How it works

1. **Cache check** — if we've scanned for this contact before, return
   the cached candidates immediately (no Paperless call, no LLM call).
2. **Paperless search** — looks for documents where the contact is the
   `correspondent` AND where the contact's name appears in full-text.
3. **OCR fetch** — pulls the `content` field for each candidate
   document (capped at `max_docs`).
4. **Address extract** — reuses the same `call_llm_extract` LLM prompt
   that powers WhatsApp/email address scraping. Letterheads tend to be
   at the top, signatures at the bottom — so the skill keeps the first
   and last 2KB of OCR text and discards the middle.
5. **Cache write** — saves results with `source_kind='paperless'` and
   `source_ref=str(paperless_doc_id)` so the next call returns instantly.

## What the LLM does with the result

The `_llm_hint` field gives concrete next-turn guidance:

  - `candidates=[]` → "No documents found mentioning <name>. Ask the
    user for the address (one short question)."
  - `candidates=[…]` → "Found <N> addresses. Top match: <line1, postcode
    city> (source: <doc_title>). Ask: 'Soll ich an diese Adresse
    schicken?' If yes → add_contact_address + compose_draft."

The skill never picks for the user — confirmation is always required
before an address gets saved to the contact or used in a letter.
