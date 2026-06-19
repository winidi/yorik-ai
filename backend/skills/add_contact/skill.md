---
name: add_contact
description: Create a new contact (person or business) in the identity hub.
when_to_use: |
  - User asks to save someone: "Speicher Anna mit der Adresse X", "Save Müller GmbH as a vendor", "Trag Oma ein mit Adresse …".
  - YOU asked the user for missing contact info (after find_person returned nothing) and they answered — call this to persist it before continuing the original task.
  - Always check find_person first to avoid duplicates. If a likely match exists, ask the user "is this the one you mean?" instead of creating a new row.
  - If a pending contact already matches the name/email/phone, call `promote_pending_contact` instead of add_contact — never create a duplicate that shadows the pending row.
  - For incoming-channel auto-creation use status='pending' and source='email_in' / 'wa_sync' / etc. — leaves the row out of autocomplete until the user confirms.
inputs:
  display_name:
    type: string
    required: true
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
  aliases:
    type: array
    required: false
    description: Additional names the user calls them. Picked up by find_person's fuzzy search. Example for Oma Schmidt → ['Oma', 'Grossmutter'].
  relation:
    type: string
    required: false
    description: Free-text relation/role — 'grandmother', 'plumber', 'employer', 'vendor', 'client'.
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
  status:
    type: string
    required: false
    default: active
    description: "'active' for explicit user adds, 'pending' for auto-captured (email_in / wa_sync), 'spam' for known-bad."
  source:
    type: string
    required: false
    default: manual
outputs:
  contact_id:
    type: integer
  contact:
    type: object
permissions: [admin, member, restricted]
side_effects: writes one row to `contacts`
tags: [contacts, mutation]
---

# add_contact

Creates a single contact row. Channels (email/phone/whatsapp) and addresses
go through their own skills (add_contact_channel / add_contact_address) so
each insert is independently undoable and the UNIQUE(kind, value) constraint
on channels can be handled per-row.
