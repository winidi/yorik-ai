---
name: unshare_contact
description: Revoke a user's per-user share on a contact you own.
when_to_use: |
  - User says "Anna soll Hans nicht mehr bearbeiten können" / "revoke Anna from Hans".
  - Pair with `find_person(source='household')` to resolve the user_id first.

  This does NOT remove the contact's space membership — for that, call `update_contact(space="household")`.
when_not_to_use: |
  For "make this contact private again" use `update_contact(contact_id, space="household")` — that resets role-based access. unshare_contact only removes one specific per-user grant.
inputs:
  contact_id:
    type: integer
    required: true
  with_user_id:
    type: integer
    required: true
    description: The household user_id whose share you're revoking.
outputs:
  contact_id:
    type: integer
  with_user_id:
    type: integer
  removed:
    type: boolean
    description: True if a share row was deleted; false if there was no share to remove.
permissions: [admin, member]
side_effects: Deletes one row from `contact_shares`. No error if the share didn't exist.
tags: [contacts, sharing, write]
---

# unshare_contact

Per-user ACL revoke. Owner-only (or admin). Idempotent — calling it
twice in a row returns `removed: false` the second time.
