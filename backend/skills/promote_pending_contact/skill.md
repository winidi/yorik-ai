---
name: promote_pending_contact
description: Move a contact from status='pending' to 'active'.
when_to_use: |
  - User explicitly confirms a pending row from the contacts inbox ("yes, save Anna").
  - Implicit signals (reply detected, calendar invite sent, draft addressed to them) trigger this from the backend automatically — the LLM should rarely need to call it directly. Only use when the user is reviewing the pending list and approves a specific one.
inputs:
  contact_id:
    type: integer
    required: true
outputs:
  contact:
    type: object
permissions: [admin, member]
side_effects: status update + bumps last_used_at
tags: [contacts, mutation]
---

# promote_pending_contact

The pending → active transition is the user's "yes, this is a real person".
The contact joins autocomplete + the main /r/contacts list. Reversible via
revert_contact_fields (status back to 'pending').
