---
name: recurring_payments
description: Payments that come back (subscriptions, servers, contracts) with the monthly amount computed.
when_to_use: |
  - "What do we spend on computer stuff every month?", "Which subscriptions are running?", "What does X cost us per month?"
  Pick the payees that fit the question; add their per_month values with calculate.
when_not_to_use: |
  - One payee's payments with receipts — payments_to. Spending by category — spending_summary.
inputs:
  from_date:
    type: string
    required: false
    description: YYYY-MM-DD; default about 13 months back.
  to_date:
    type: string
    required: false
    description: YYYY-MM-DD; default today.
outputs:
  recurring:
    type: array
    description: payee, rhythm (monthly | quarterly | yearly), regular_amount, per_month, last_booked, last_amount, other_amounts, category.
  per_month_total:
    type: string
  bank_records_from:
    type: string
cost: one query over the bank bookings.
permissions: [admin, member, restricted]
side_effects: none — read-only
tags: [finance, bank, subscriptions, read, app:finance]
category: productivity
---

# recurring_payments

A payee is recurring when the same amount (within 5 %) comes back at a
steady gap — about a month, a quarter or a year. Groceries (a different
amount every time), one-off purchases, round-ups to savings and money
coming in drop out. `per_month` is the recurring amount divided by the
months of its rhythm, computed by the app (backend/recurring.py).

Visibility: the bank accounts the person may see.
