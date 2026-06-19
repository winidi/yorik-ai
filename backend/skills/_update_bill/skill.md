---
name: update_bill
description: Modify a bill (mark paid, change amount, change due date, etc.)
when_to_use: |
  - User says "ich hab die Stromrechnung bezahlt" → set paid=true on matching bill
  - User says "die GEZ kostet jetzt 18,36 €" → change amount
  Always look up the bill_id first if you don't know it — never guess.
inputs:
  bill_id:
    type: integer
    required: true
  name:
    type: string
    required: false
  amount:
    type: number
    required: false
  currency:
    type: string
    required: false
  due_date:
    type: string
    required: false
  recurring:
    type: string
    required: false
  paid:
    type: boolean
    required: false
  notes:
    type: string
    required: false
  document_id:
    type: integer
    required: false
    description: Link this bill to a scanned PDF / source document (Paperless id). Set when the user uploads or identifies the matching scan after the bill was first recorded.
outputs:
  bill_id:
    type: integer
  bill:
    type: object
tags: [bills, mutation]
permissions: [admin, member, restricted]
---
# update_bill
Apply-then-confirm. Cancel/test restores pre-update values.
