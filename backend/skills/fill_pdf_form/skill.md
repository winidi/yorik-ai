---
name: fill_pdf_form
description: Fill the fields of a PDF form the user attached in chat, from their profile and what they say.
when_to_use: |
  The user attached a PDF (referenced as "Anhang #<number>") and asks to
  fill it out — "füll das Formular aus", "trag meine Daten da ein",
  "kannst du das für mich ausfüllen".
  Pass `notes` with anything the user said that isn't already in their
  profile (a product name, a chosen category, an answer to a yes/no
  question on the form) — in their own words, don't restructure it.
  Only works on PDFs with real fillable fields (an AcroForm). If the
  attachment has none, the skill says so; it does not attempt the
  scanned/flat-PDF case yet.
when_not_to_use: |
  - Just reading or summarizing an attachment — that's read_attachment.
  - Yorik's own documents (invoices, letters, quotes) — those are
    write_invoice / write_letter / compose_draft, never this skill.
  - Filing the result in Paperless — that's a separate file_attachment
    call, only if the user asks for it.
inputs:
  attachment_id:
    type: integer
    required: true
    description: "The number after Anhang # for the PDF form to fill."
  notes:
    type: string
    required: false
    description: Anything the user said that the form needs and their profile doesn't cover, in their own words.
outputs:
  ok:
    type: boolean
  filled_attachment_id:
    type: integer
    description: Id of the new chat attachment holding the filled PDF.
  fields_filled:
    type: array
    description: field + value pairs that were set, for the model to summarize back to the user.
  fields_unresolved:
    type: array
    description: Fields the form has but no value could be found for; the model should ask the user about these.
  warning:
    type: string
    description: Set when the PDF has no fillable fields, nothing could be mapped, or the visual check found a concern.
cost: a few seconds — field discovery is instant, one internal text-LLM call maps values, then a vision pass checks the render.
permissions: [admin, member, restricted]
side_effects: Creates a new chat attachment with the filled PDF. Never modifies the original attachment, never sends or files anything anywhere.
tags: [chat, documents, pdf, write, forms]
category: productivity
---

# fill_pdf_form

Reads the AcroForm fields of an attached PDF, fills what it can from the
calling user's own profile (the same columns read_my_profile serves,
read directly — no extra round trip) plus `notes`, and renders the
result to check nothing looks wrong (an unfilled required-looking
field, a radio button that lost its mark on write) before handing back
a new, unfiled attachment for the user to review.

A field already at its target value is left untouched. Re-setting an
already-correct radio or checkbox regenerates a generic appearance
instead of reusing the PDF's own on-state artwork — found 2026-09-25
while hand-filling a real form, now guarded against here permanently.
