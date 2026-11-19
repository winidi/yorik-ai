---
name: file_email_attachment
description: File an attachment of an email in Paperless, the household's document archive.
when_to_use: |
  The user wants an attachment of an email filed ("put the invoice from that mail into Paperless", "file the receipt").
  Find the email first (find_email_by_subject, then read_email); pass its message_id, and the attachment's `filename` when the email has more than one.
  Always pass `visibility` from what the user said; "only me" / "private" is private, "the two of us" / "the parents" / "not the kids" is parents, "for everyone" / "the whole family" is shared, "company" / "the business" is business.
  If the user wants it filed but did not say who should see it, ask that one question first instead of calling this.
when_not_to_use: |
  The file was attached in the chat; that is file_attachment. Never file without the user's yes.
inputs:
  message_id:
    type: integer
    required: true
    description: The email's id from find_email_by_subject or read_email.
  filename:
    type: string
    required: false
    description: Which attachment, by its file name as read_email lists it; needed when the email has more than one.
  visibility:
    type: string
    required: false
    description: private (only the user), parents (the adults, not the children), shared (the whole household, children included) or business (the business group).
outputs:
  ok:
    type: boolean
  filename:
    type: string
  visibility:
    type: string
cost: a few seconds (the file comes from the mail server); Paperless needs a minute for OCR afterwards
permissions: [admin, member, restricted]
side_effects: uploads the attachment to Paperless under the user's own account
tags: [email, documents, paperless, write]
---

# file_email_attachment

The same as the Email app's "File to Paperless" button: the attachment is
fetched from the mail server and sent to Paperless through the person's
own token, with their default visibility unless they name another. An
outside agent's card gets a View button for the file.
