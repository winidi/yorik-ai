---
name: read_attachment
description: Read a file the user attached in the chat (PDF, Word, text, photo) by its number.
when_to_use: |
  The user says they uploaded or attached a file and the message names it as "Anhang #<number>"; call this first with that number.
  Use it again with `question` when the user asks something specific about the same attachment, and always for a question about a picture.
  After reading, tell the user in one or two sentences what the file is (kind, sender, date, amount), then follow `_llm_hint` about filing.
when_not_to_use: |
  The document is already in Paperless or in the library; that is find_document and read_document.
inputs:
  attachment_id:
    type: integer
    required: true
    description: The number after "Anhang #" in the user's message.
  question:
    type: string
    required: false
    description: What to look for; for pictures the vision model answers exactly this.
outputs:
  filename:
    type: string
  text:
    type: string
    description: The extracted text, or for a picture what the vision model read (cut at 40000 characters).
  filed:
    type: boolean
    description: True when the attachment already went to Paperless.
cost: instant for text and PDF, a few seconds for a picture
permissions: [admin, member, restricted]
side_effects: none
tags: [chat, documents, read]
---

# read_attachment

A file dropped into the chat belongs to that conversation. It is not in
Paperless and not in any library until the user says it should be filed
(`file_attachment`). Unfiled attachments are deleted with the
conversation or after 30 days.
