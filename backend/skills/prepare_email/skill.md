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
  A card with an "open and send" button appears in the chat on its
  own — don't repeat recipient/subject/attachment in your reply, just
  confirm briefly that it's ready. Nothing is sent by this skill.
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
side_effects: Writes one app_settings row for the calling user (the staged draft) and shows a card in the chat. Sends nothing, files nothing, never touches SMTP.
tags: [email, chat, write, draft]
category: productivity
---

# prepare_email

Writes the draft into app_settings (key pending_email_draft_<user_id>)
so GET /api/email/pending-draft can hand it to the Email app's
Composer on mount — works regardless of which tab or window the user
opens Email in, since it isn't tied to browser storage. Also returns
an `email_ready` UI action so the chat shows a real card
(EmailDraftReadyCard.tsx) with an "open and send" button, instead of
just telling the user in text to go open Email themselves. The send
button in the Composer is the only thing that ever calls SMTP.
