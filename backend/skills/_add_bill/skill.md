---
name: add_bill
description: Record a new bill / recurring expense
when_to_use: |
  - User says "ich hab eine Stromrechnung über 120 € fällig zum 15.6."
  - User says "add Netflix to my bills, 9.99 monthly"
  - Pass `recurring` only when the user explicitly stated repetition.
  - Pass `notes` and `category` only when the user dictated them.
inputs:
  name:
    type: string
    required: true
  amount:
    type: number
    required: true
    description: Amount in major units (euros, dollars). E.g. 120.50.
  currency:
    type: string
    required: false
    default: EUR
  due_date:
    type: string
    required: false
    description: ISO date YYYY-MM-DD when due.
  recurring:
    type: string
    required: false
    description: "monthly | yearly | weekly — set only when the user said the bill repeats ('jeden Monat', 'monthly', 'jährlich', 'every week'); omit otherwise."
  notes:
    type: string
    required: false
  document_id:
    type: integer
    required: false
    description: ID of the scanned PDF / source document in Paperless that backs this bill. Set this whenever the bill came from a document the user can open — lets "zeig mir die Rechnung" resolve to the real scan instead of guessing.
outputs:
  bill_id:
    type: integer
  bill:
    type: object
tags: [bills, mutation]
permissions: [admin, member, restricted]
---
# add_bill
Apply-then-confirm. Cancel/test deletes the row.
