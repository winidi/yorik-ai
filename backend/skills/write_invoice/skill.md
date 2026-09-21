---
name: write_invoice
description: Draft an invoice or a quote in the Schreiben app from customer and line items; never asks back.
when_to_use: |
  The user wants an invoice (Rechnung) or a quote/offer (Angebot) for a customer.
  Pass what the user said as data. Each line item has a text, a quantity, a unit and a net unit price. Do NOT compute sums, VAT or totals and do NOT invent an invoice number or a date; the app computes and numbers.
  If a detail is unknown (address, service date, a price), create the draft anyway and leave it out; the sheet marks what is missing. Never ask the user for missing details first.
  To change a draft from earlier in this conversation, call again with `document_id` and the complete new list of lines.
when_not_to_use: |
  Letters (use write_letter), emails, reminders about unpaid invoices.
inputs:
  kind:
    type: string
    required: true
    description: invoice or quote.
  customer:
    type: string
    required: true
    description: The customer as the user named them; the address comes from the contacts.
  lines:
    type: array
    required: true
    description: Line items, each an object with text, qty, unit (Std., Stk., pauschal), unit_price (net, a number) and optionally vat_percent.
  subject:
    type: string
    required: false
    description: What the invoice or quote is about, one short line.
  intro:
    type: string
    required: false
    description: One or two sentences above the line items, without salutation.
  service_from:
    type: string
    required: false
    description: First day of the service, YYYY-MM-DD, only if the user said it.
  service_to:
    type: string
    required: false
    description: Last day of the service, YYYY-MM-DD, only if the user said it.
  document_id:
    type: integer
    required: false
    description: Id of a draft from earlier in this conversation to replace instead of starting a new one.
outputs:
  document_id:
    type: integer
  total:
    type: string
  missing:
    type: array
cost: instant
permissions: [admin, member]
side_effects: Creates or updates a draft invoice or quote of the calling user. No number is taken, nothing is sent.
tags: [write, invoice, rechnung, quote, angebot, schreiben, app:write]
category: productivity
---

# write_invoice

The data half of the "Schreiben" app. The model hands over customer and
line items; the code computes the sums, sets the look (the person's
letterhead) and, when the person finalises the invoice, takes the number
and builds the e-invoice. Nothing here can burn an invoice number.
