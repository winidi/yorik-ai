---
name: add_contact
description: Create a new contact (person or business) with ALL its channels and address in one call.
when_to_use: |
  - User asks to save someone — "Speicher Anna mit der Adresse X", "Save Müller GmbH as a vendor", "Trag Oma ein mit Adresse …".
  - YOU asked the user for missing contact info (after find_person returned nothing) and they answered — call this to persist it before continuing the original task.
  - Always check find_person first to avoid duplicates. If a likely match exists, ask the user "is this the one you mean?" instead of creating a new row.
  - If a pending contact already matches the name/email/phone, call `promote_pending_contact` instead of add_contact — never create a duplicate that shadows the pending row.
  - For incoming-channel auto-creation use status='pending' and source='email_in' / 'wa_sync' / etc. — leaves the row out of autocomplete until the user confirms.
  - ONE-SHOT: pass EVERY field you know in this single call — emails, phones, whatsapp_jids, websites, address, first_name, last_name, role, employer_contact_id. Do NOT chain `add_contact_channel` or `add_contact_address` after a fresh create — those skills are for adding a SECOND email/address to an already-existing contact.
inputs:
  display_name:
    type: string
    required: true
    description: |
      Address-book name. For a person this is usually "First Last".
      For a business, the brand or organisation name.
  kind:
    type: string
    required: false
    default: person
    description: |
      'person' (default) or 'business'. STRONG default to 'person'.
      Pick 'business' ONLY when the name clearly identifies an
      organisation: legal-entity suffix (GmbH, AG, UG, Ltd, Inc,
      LLC, e.V., e.K., Co., etc.), known company branding, or the
      user explicitly says "Firma", "company", "business". A
      first-name-last-name pair is ALWAYS 'person'. When unsure,
      'person' is the correct fallback — businesses are the
      exception, not the default.
  first_name:
    type: string
    required: false
    description: Person's given name. Only set when kind='person'. Pair with last_name.
  last_name:
    type: string
    required: false
    description: Person's family name. Only set when kind='person'.
  role:
    type: string
    required: false
    description: |
      Person's JOB TITLE at their employer ("Senior Service-Monteur",
      "CEO", "Sachbearbeiterin"). For kind='person' only. Distinct
      from `relation` — `role` is what they do at work, `relation`
      is how they relate to the user.
  employer_contact_id:
    type: integer
    required: false
    description: |
      ID of the parent business contact when this person works for
      a business already in the user's contacts. Use find_person to
      look up the employer first. Leave null when no business
      contact exists yet — don't fabricate.
  aliases:
    type: array
    required: false
    description: Additional names the user calls them. Picked up by find_person's fuzzy search. Example for Oma Schmidt → ['Oma', 'Grossmutter'].
  relation:
    type: string
    required: false
    description: |
      How the contact relates to the user — "grandmother", "plumber",
      "tax advisor", "client". Free-text. NOT the job title — use
      `role` for that.
  birthday:
    type: string
    required: false
    description: YYYY-MM-DD
  language_pref:
    type: string
    required: false
    description: "'de' | 'en' | etc. Default behaviour falls back to the user's language."
  salutation_pref:
    type: string
    required: false
    description: "'du' | 'Sie' | 'first-name' | 'formal'"
  legal_name:
    type: string
    required: false
    description: Business legal name (e.g. "Müller Immobilien GmbH"). Only when kind='business'.
  tax_id:
    type: string
    required: false
    description: USt-IdNr / VAT / EIN. Business only.
  iban:
    type: string
    required: false
  notes:
    type: string
    required: false
    description: |
      Free-text notes. Do NOT use this to dump structured data that
      has a real field — emails, phone numbers, and addresses each
      have their own field on this skill. notes is for prose that
      doesn't fit anywhere else ("met at the Schützenfest", "prefers
      morning calls").
  status:
    type: string
    required: false
    default: active
    description: "'active' for explicit user adds, 'pending' for auto-captured (email_in / wa_sync), 'spam' for known-bad."
  source:
    type: string
    required: false
    default: manual
  space:
    type: string
    required: false
    description: |
      Slug or numeric ID of the space (shared scope) this contact
      lives in. Omit to use the household's default placement
      setting (usually the creator's personal space).
  emails:
    type: array
    required: false
    description: |
      Array of email addresses to attach as channels. Pass EVERY
      email you know about the contact in this single call — don't
      chain add_contact_channel afterwards. Each entry creates one
      channel row; UNIQUE(kind, value) across channels means if one
      address is already on another contact the create rolls back
      entirely.
  phones:
    type: array
    required: false
    description: Phone numbers as written (with country code preferred). Same shape + rules as `emails`.
  whatsapp_jids:
    type: array
    required: false
    description: WhatsApp JIDs (e.g. "4915128811000@s.whatsapp.net"). Use chat_jid, not bare digits.
  websites:
    type: array
    required: false
    description: Personal site / company URL. Include the scheme ("https://…").
  address:
    type: object
    required: false
    description: |
      Postal address as a single object: {line1, line2, postcode,
      city, region, country, kind, label}. Country is ISO 3166-1
      alpha-2 ("DE"/"AT"/"US"). kind defaults to "home" for persons
      and is implicitly "work" for businesses — set explicitly
      when you know better. Pass the FULL address in this single
      call — don't chain add_contact_address afterwards.
outputs:
  contact_id:
    type: integer
  contact:
    type: object
permissions: [admin, member, restricted]
side_effects: writes one row to `contacts` plus N rows to `contact_channels` + optionally one row to `contact_addresses`. All inserts are transactional — any failure rolls the whole create back so no half-formed contacts get persisted.
tags: [contacts, mutation]
---

# add_contact

One-shot create. Pass EVERYTHING you know about the new contact in this single call —
display_name, first_name, last_name, role, kind, employer_contact_id, emails, phones,
whatsapp_jids, websites, address. The whole thing inserts transactionally; if a channel
hits the UNIQUE constraint (the email is already on another contact) the entire create
rolls back so no orphan contact row sticks around.

DO NOT chain `add_contact_channel` or `add_contact_address` for a fresh create — those
skills exist for adding a SECOND email / address to an already-existing contact.
