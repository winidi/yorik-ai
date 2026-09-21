---
name: file_attachment
description: File a chat attachment in Paperless, the household's document archive.
when_to_use: |
  The user agreed to file an attachment ("ja, ablegen", "leg das ab", "ab in Paperless") after read_attachment, or asks for it later in the same conversation.
  Always pass `visibility` from what the user said; "nur ich" / "privat" is private, "wir beide" / "die Eltern" / "nicht die Kinder" is parents, "für alle" / "die ganze Familie" is shared, "Firma" / "Geschäft" is business.
  If the user agreed to file but did not say who should see it, ask that one question first instead of calling this.
  Pass `title` when the file name says nothing ("scan_0042.pdf") and you know what the document is.
when_not_to_use: |
  The user only wanted to show you something (a screenshot, a photo in passing). Never file without the user's yes.
inputs:
  attachment_id:
    type: integer
    required: true
    description: The number after "Anhang #" in the user's message.
  visibility:
    type: string
    required: false
    description: private (only the user), parents (the adults, not the children), shared (the whole household, children included) or business (the business group).
  title:
    type: string
    required: false
    description: A speaking title for Paperless, e.g. "Stadtwerke Rechnung September 2026".
outputs:
  ok:
    type: boolean
  visibility:
    type: string
cost: instant; Paperless needs a minute for OCR and indexing afterwards
permissions: [admin, member, restricted]
side_effects: uploads the file to Paperless under the user's own account
tags: [chat, documents, paperless, write]
---

# file_attachment

Paperless is the one place documents live. Filing sends the attachment
there through the user's own Paperless token, with their default
visibility unless they name another.
