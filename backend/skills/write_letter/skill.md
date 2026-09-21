---
name: write_letter
description: Write a letter as a draft in the Schreiben app, with the user's letterhead; never asks back.
when_to_use: |
  The user wants a letter, a notice of termination, a complaint, an objection, a request or any other written letter to a person, company or authority.
  Write the WHOLE letter yourself and pass it as `text`, from the salutation to the closing and the name. Do not add sender, recipient address, date or subject line to `text`; the letterhead sets those.
  Pass the recipient as the user named them; the address comes from the contacts. If something is unknown, write the letter anyway and leave a short placeholder in square brackets. Never ask the user for missing details first.
  To change a letter you already drafted in this conversation, call again with `document_id`.
when_not_to_use: |
  Emails (use email_draft), WhatsApp messages, invoices and quotes.
inputs:
  recipient:
    type: string
    required: true
    description: Person, company or authority the letter goes to, as the user named them.
  subject:
    type: string
    required: true
    description: The subject line, short, without the word Betreff.
  text:
    type: string
    required: true
    description: The complete letter text, salutation to closing and name, paragraphs separated by blank lines.
  document_id:
    type: integer
    required: false
    description: Id of a draft from earlier in this conversation to replace its text instead of starting a new one.
outputs:
  document_id:
    type: integer
  missing:
    type: array
cost: instant
permissions: [admin, member, restricted]
side_effects: Creates or updates a draft letter of the calling user. Nothing is sent or filed.
tags: [write, letter, brief, kuendigung, schreiben, app:write]
category: productivity
---

# write_letter

The letter half of the "Schreiben" app. The model writes the text, the
code sets the look (the person's letterhead, DIN 5008). The draft opens
in /r/write, where it is edited, turned into a PDF, sent or filed.
