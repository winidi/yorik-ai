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
  deleted_contact_id:
    type: integer
  contact:
    type: object
    description: Full pre-delete snapshot (channels + addresses) so the undo machinery can re-create.
permissions: [admin, member]
side_effects: deletes 1 contact row + its channels + addresses
tags: [contacts, mutation, destructive]
---

# delete_contact

Hard delete. Channels + addresses are removed in the same transaction.
The pending_actions rollback (kind='restore_contact') re-creates everything
from a full snapshot, so cancel/test gets the contact back atomically.
