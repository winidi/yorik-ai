---
name: read_email
description: Fetch the full body and metadata of a single email by message_id.
when_to_use: |
  - User asks what an email actually says: "what did Hans write?",
    "lies mir die E-Mail vor", "summarise the support thread".
  - You have a message_id from find_email_by_subject and need the
    full text to answer, summarise, or extract a fact.
  - NOT for replying — email_draft already pulls the thread internally
    and returns reply variants.
  - NOT for searching — call find_email_by_subject or universal_search
    first if you don't already have the message_id.
inputs:
  message_id:
    type: integer
    required: true
    description: The email_messages.id to read.
  include_html:
    type: boolean
    required: false
    default: false
    description: Include body_html alongside body_text. Default false — text is enough for most analysis and HTML bloats context.
outputs:
  message:
    type: object
    description: |
      Single row with from_email, from_name, to_addrs, cc_addrs,
      subject, date_received, date_sent, body_text (FULL, untruncated),
      is_starred, is_unread, is_sent, has_attachments, attachments[]:
      {filename, mimetype, size_bytes}.
  not_found:
    type: boolean
    description: True when no message with that id is owned by the calling user.
side_effects: None — does NOT mark the message as read. Use update_email(is_unread=False) explicitly if the user wants that.
cost: One indexed SELECT on email_messages + one on email_attachments.
permissions: [admin, member]
tags: [email, read, internal]
---

# read_email

Returns the full body of a single email the user owns. Pairs with
find_email_by_subject (resolver → reader) and mirrors the same
shape email_draft consumes internally, so the agent can read and
draft from the same message_id without a second lookup.
