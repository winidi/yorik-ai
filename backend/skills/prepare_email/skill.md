---
name: prepare_email
description: Stage a new email with a recipient, subject, body and one attachment for the user to review and send.
when_to_use: |
  The user wants to send something from the chat by email — a filled
  form, a document, a result — as a brand-new outgoing email (not a
  reply within an existing thread). Triggers: "kannst du das schicken",
  "können wir das absenden", "schick das per Mail an X", "bereite eine
  Mail vor".
  Write `subject` and `body` yourself from the conversation — a short,
  plain cover message is enough. Pass `from_email` only if the user
  named which of their own accounts to send from.
  ALWAYS tell the user afterwards that nothing was sent: the draft is
  staged in the Email app for them to open, check, and press send
  themselves.
when_not_to_use: |
  - Replying inside an existing email thread — that's email_draft.
  - Actually sending — no skill in Yorik does this yet; sending is
    always a manual click in the Email app's Composer.
inputs:
  to:
    type: string
    required: true
    description: The recipient's email address.
  subject:
    type: string
    required: true
    description: The email subject line, written from the conversation.
  body:
    type: string
    required: true
    description: A short plain-text cover message, written from the conversation.
  attachment_id:
    type: integer
    required: true
    description: "The attachment number (Anhang #<n>) of the file to include."
  from_email:
    type: string
    required: false
    description: Which of the user's own email accounts to send from, only if they named one.
outputs:
  ok:
    type: boolean
  staged_to:
    type: string
  attachment_id:
    type: integer
cost: instant — no LLM call, no network.
permissions: [admin, member, restricted]
side_effects: Stages a draft for the Email app (sessionStorage handoff). Sends nothing, files nothing, never touches SMTP.
tags: [email, chat, write, draft]
category: productivity
---

# prepare_email

Queues a `stash_pending_email` UI action instead of writing to any
database table — the same handoff channel the chat's attachment stash
tray and the documents "Send via email" button already use, just with
`to`/`subject`/`body` added so the draft needs no further typing. The
Email app's Composer reads it once on next mount and clears it; the
send button there is the only thing that ever calls SMTP.
