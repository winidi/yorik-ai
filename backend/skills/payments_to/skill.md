---
name: payments_to
description: Everything paid to one payee, or all unpaid bills — bank and receipts matched, totals computed.
when_to_use: |
  - "What have I paid Claude?", "What does netcup cost us?", "Has the Riverty invoice been paid?"
  - "Which invoices from X are still open?"
  One payee per call; for several payees call it once for each.
  Without payee: all bills and reminders found in mail and documents, matched with the bank — for "Which invoices are still open?".
when_not_to_use: |
  - A list of all bookings in a period or a category — show_transactions / spending_summary.
inputs:
  payee:
    type: string
    required: false
    description: The payee as the user names it ("Claude", "netcup").
  also:
    type: array
    items: { type: string }
    required: false
    description: Other names it may appear under ("Anthropic").
  from_date:
    type: string
    required: false
    description: YYYY-MM-DD; default one year back.
  to_date:
    type: string
    required: false
    description: YYYY-MM-DD; default today.
outputs:
  payments:
    type: array
    description: date, amount, status (paid_bank | paid_elsewhere | paid_before_records | open | unclear | bank_only), status_text, what, sources.
  totals:
    type: object
    description: paid, open — computed, quote them.
cost: one search per name; each receipt read once by the model (kept afterwards).
permissions: [admin, member, restricted]
side_effects: Stores what each receipt says (receipt_facts).
tags: [finance, bank, receipts, read, app:finance]
category: productivity
---

# payments_to

Bank bookings (round-ups to savings left out) and receipts or invoices
from documents and mail that name the payee. Each receipt is read once
for amount, date, paid or open and payment method; the amount must
stand in its text. A receipt and a booking with the same amount, the
booking 3 days before to 10 days after the receipt, are one payment.

- paid_bank: receipt and booking
- paid_elsewhere: the receipt says paid, no booking on these accounts (another card, PayPal)
- paid_before_records: the receipt says paid and is older than the first booking on record
- open: the receipt asks for payment, no booking
- unclear: a receipt that does not say (or is older than the bank records), no booking
- bank_only: a booking without a receipt

Visibility: the person's own bank accounts and shares, their own mail,
Paperless as the person.
