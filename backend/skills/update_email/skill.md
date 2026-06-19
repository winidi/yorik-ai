---
name: update_email
description: "Toggle the starred or unread flag on an existing email, on IMAP + local mirror."
when_to_use: |
  - User says "star the X email", "mark X as read", "mark this email
    unread", "unstar the support thread".
  - If you don't have message_id, call `find_email_by_subject` first.
  - NOT for moving to folders or snoozing — those have dedicated
    endpoints; this skill only toggles is_starred and is_unread.
  - NOT for creating contacts; "star this email" is a flag flip,
    never a contact-creation trigger.
inputs:
  message_id:
    type: integer
    required: true
    description: The email_messages.id to update.
  is_starred:
    type: boolean
    required: false
    description: True to star, false to unstar. Omit to leave unchanged.
  is_unread:
    type: boolean
    required: false
    description: True to mark unread, false to mark read. Omit to leave unchanged.
outputs:
  ok:
    type: boolean
  message_id:
    type: integer
  applied:
    type: object
    description: Map of fields actually changed in this call.
side_effects: Issues IMAP STORE on the user's account; updates the local email_messages row to match.
cost: One IMAP round-trip per flag changed.
permissions: [admin, member]
tags: [email, mutation]
---

# update_email

Flag toggle for the user's own email. Wraps the same `email_actions.set_seen` /
`set_starred` calls the PATCH endpoint uses, so IMAP + local mirror
stay in sync.

If you got a `find_email_by_subject` result back, the `id` field is
the `message_id` you pass here.
