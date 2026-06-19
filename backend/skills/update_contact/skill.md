---
name: update_contact
description: "Modify an existing contact (name, relation, business fields, status, etc.)."
when_to_use: |
  - "Oma heißt eigentlich Lena Hoffmann" → update display_name.
  - "Sie spricht Englisch" → update language_pref.
  - "Müller GmbH hat eine neue USt-IdNr DE…" → update tax_id.
  - To toggle status (active ↔ pending ↔ spam ↔ archived) prefer the dedicated promote_pending_contact / mark_contact_spam skills — they're clearer in the trace.
  - For adding/removing channels or addresses use add_contact_channel / add_contact_address — this skill only edits the contact row itself.
  - Always call find_person first to get the id; do NOT guess.
inputs:
  contact_id:
    type: integer
    required: true
  display_name:
    type: string
    required: false
  aliases:
    type: array
    required: false
  kind:
    type: string
    required: false
  relation:
    type: string
    required: false
  birthday:
    type: string
    required: false
  language_pref:
    type: string
    required: false
  salutation_pref:
    type: string
    required: false
  legal_name:
    type: string
    required: false
  tax_id:
    type: string
    required: false
  iban:
    type: string
    required: false
  notes:
    type: string
    required: false
outputs:
  contact:
    type: object
permissions: [admin, member]
side_effects: updates one row in `contacts`
tags: [contacts, mutation]
---

# update_contact

Pass only the fields you want to change. Captures pre-update values into
the pending_actions rollback so cancel/test restores them exactly.
