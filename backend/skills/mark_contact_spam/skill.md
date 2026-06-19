---
name: mark_contact_spam
description: Move a contact to status='spam' — future inbound from its channels gets quietly dropped.
when_to_use: |
  - User explicitly rejects a pending row ("nein, das ist Werbung", "block").
  - User flags an active contact that turned out to be a marketing/scam source.
  - The contact's channels stay indexed so future emails from the same address get auto-suppressed instead of recreating the pending row.
inputs:
  contact_id:
    type: integer
    required: true
outputs:
  contact:
    type: object
permissions: [admin, member]
side_effects: status update
tags: [contacts, mutation, spam]
---

# mark_contact_spam

Soft block. The contact row stays so the (kind, value) channel lookup
still resolves — the email fetcher reads `status='spam'` and drops the
message instead of importing it. Reversible by promote_pending_contact
(it accepts spam too, not just pending).
