---
name: show_transactions
description: List recent bank transactions from the synced local copy, filterable by account and category.
when_to_use: |
  The user asks to see recent transactions, a specific account's
  activity, or spending in one category ("zeig mir meine
  Kontoumsätze", "was ging vom gemeinsamen Konto ab").
  Call list_bank_accounts first if you need an account_id.
when_not_to_use: |
  A totals/breakdown question ("wie viel für X diesen Monat") — use
  spending_summary, it aggregates instead of listing rows.
inputs:
  days:
    type: integer
    required: false
    default: 30
    description: How many days back, 1-365.
  account_id:
    type: integer
    required: false
    description: Limit to one account (from list_bank_accounts).
  category:
    type: string
    required: false
    description: Limit to one category, e.g. Lebensmittel, Wohnen, Abos.
outputs:
  transactions:
    type: array
    description: Each with booking_date, amount, currency, counterparty, purpose, category, account_name.
cost: 1 SELECT, no network — reads the local synced copy, never live FinTS.
permissions: [admin, member, restricted]
side_effects: none — read-only.
tags: [finance, bank, read, app:finance]
category: productivity
---

# show_transactions

Same visibility rule as list_bank_accounts: only the calling user's
own and shared accounts are in scope, enforced by the query itself
(backend/spaces.row_filter), not by anything the model has to get
right on its own. Capped at 200 rows; ask for a narrower range if the
user needs more.
