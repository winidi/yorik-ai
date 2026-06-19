---
name: share_contact
description: Give another household user edit access to a contact you own (per-user share).
when_to_use: |
  - User says "teile den kontakt von Hans mit Anna" / "share Hans with Anna" / "lass Anna auch Hans bearbeiten".
  - User wants finer control than the contact's space (Household / Customers / etc.). `share_contact` is per-user — share with Anna specifically, not "all members".
  - Call `find_person(query=<user-name>, source='household')` first to resolve the recipient's user_id. Then call this skill with the contact_id and that user_id.

  After this, the recipient sees the contact in find_person results and can edit it (or view it only if can_edit=false).
when_not_to_use: |
  For household-wide sharing use `update_contact(contact_id, space="household")` — simpler than enumerating each user.
inputs:
  contact_id:
    type: integer
    required: true
    description: The contact to share. Caller must own it (or be admin).
  with_user_id:
    type: integer
    required: true
    description: Household user_id to share with. Get this from `find_person(source='household')`.
  can_edit:
    type: boolean
    required: false
    default: true
    description: True = recipient can update/delete; false = read-only (visible in find_person, but mutations refuse).
outputs:
  contact_id:
    type: integer
  with_user_id:
    type: integer
  can_edit:
    type: boolean
permissions: [admin, member]
side_effects: Inserts/updates one row in `contact_shares`. The recipient sees the contact on their next find_person / contacts-app refresh.
tags: [contacts, sharing, write]
---

# share_contact

Per-user contact ACL. Pair with `update_contact(space=...)`
for role-level sharing.

Only the contact's owner or an admin can grant shares. The recipient
gets edit-or-view access depending on `can_edit`. To revoke later,
call `unshare_contact(contact_id, with_user_id)`.
