---
name: compose_check_recipient
description: Check whether a contact has the postal address a letter template needs.
when_to_use: |
  After find_person has resolved a single contact AND the user wants a
  postal letter (compose_draft with template_id), call this skill to
  decide whether you can proceed straight to compose_draft or whether
  you first need to gather the recipient's address.

  Flow:
    user: "schreibe diese Kündigung an Hans Becker"
    you: find_person("Hans Becker")           → single match, contact_id=42
    you: compose_check_recipient(contact_id=42,  → complete=true  → compose_draft now
                                  template_id="kuendigung-mietvertrag-de")
                                                 → complete=false → ask user / Phase 3 Paperless mining

  Don't call this for email_draft or whatsapp_draft — those only need
  email/jid, not postal address. find_person already surfaces those.

  This skill is read-only and cheap (one contact lookup + one template
  load). Always preferable to guessing addresses in compose_draft.
inputs:
  contact_id:
    type: integer
    required: true
    description: Contact id returned by find_person.
  template_id:
    type: string
    required: true
    description: Template id the user is composing with (from the active template in Compose).
outputs:
  complete:
    type: boolean
    description: True when the contact has enough postal address data to render the template's recipient block cleanly.
  address:
    type: object
    description: The chosen postal address (when one exists) — keys line1, line2, postcode, city, country.
  missing:
    type: array
    description: When complete=false, the list of field descriptors the LLM should collect (key, label, required).
  contact_name:
    type: string
    description: The contact's display name — handy for the next chat sentence to the user.
permissions: [admin, member]
side_effects: none — read only
tags: [compose, contacts, address]
---

# compose_check_recipient

One deterministic step between `find_person` and `compose_draft` that
answers a single question: **"Do we have what we need to put the
recipient block on the letter, or do we have to collect it first?"**

The skill picks the contact's best postal address (preferring home →
work → billing → shipping), checks that line1 + city are filled, and
returns a clear `complete: true/false` decision. When incomplete, it
returns the list of fields the LLM should ask about (or — once Phase 3
ships — that should be Paperless-mined).

This is the seam where Yorik decides between "draft it" and "first
gather data". Keeping it as its own skill (instead of inline in
compose_draft) means:

  - The LLM follows a single deterministic playbook
    (find_person → compose_check_recipient → compose_draft) instead of
    branching based on prose interpretation.
  - The chat UI can render the `missing` array as an inline form
    (Phase 4) without compose_draft having to know about UI shapes.
  - Future Paperless mining (Phase 3) plugs in here, not in
    compose_draft, so the draft pipeline stays focused on rendering.
