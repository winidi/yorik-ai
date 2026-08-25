---
name: delete_contact
description: Permanently delete a contact and its channels + addresses.
when_to_use: |
  - User explicitly wants the contact GONE ("vergiss diesen Kontakt", "delete X").
  - PREFER archiving over deletion: an archived contact disappears from autocomplete but old invoices/emails that reference them stay coherent. Use this skill only when the user is explicit ("permanent löschen", "delete forever").
  - Subject to the per-turn delete throttle: only ONE delete per request. If the user asks for multiple, list them and ask which one(s).
inputs:
  contact_id:
    type: integer
    required: true
outputs:
  pending:
    type: boolean
    description: Always true — the delete is staged, not executed. It runs only when the user taps Delete on the card.
  pending_id:
    type: string
  contact:
    type: object
    description: id + display_name of the contact that WOULD be deleted.
permissions: [admin, member]
side_effects: deletes 1 contact row + its channels + addresses
tags: [contacts, mutation, destructive]
---

# delete_contact

Hard delete once confirmed: channels + addresses go in the same
transaction. Confirm-before-apply — the skill stages the delete and shows
a card; nothing is removed until the user taps Delete. Reply that the card
is waiting, never that the contact is gone.
