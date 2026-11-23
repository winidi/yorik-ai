---
name: delete_email
description: Delete one email the user names; it waits for their yes before anything is removed.
when_to_use: |
  - The user asks to delete or throw away one specific email.
  - Find it first with find_email_by_subject and pass its id.
when_not_to_use: |
  - Several emails at once: delete them one by one, each with its own yes.
  - Marking read, unread or starred: that is update_email.
inputs:
  message_id:
    type: integer
    required: true
    description: Exactly ONE email id (the id find_email_by_subject returns), no list.
outputs:
  pending:
    type: boolean
    description: Always true; the email moves to the Trash only after the user's yes.
  pending_id:
    type: string
  email:
    type: object
    description: The email that WOULD be deleted (subject, sender, date).
permissions: [admin, member]
tags: [email, write]
---
# delete_email
Hard 1-email cap. Confirm-before-apply: nothing happens until the user says yes; then the email moves to the Trash, as in a mail program. Reply that the question is waiting, never that the email is gone.
