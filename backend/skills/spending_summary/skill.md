---
name: spending_summary
description: Totals of incoming and outgoing money by category over a period, from the synced local copy.
when_to_use: |
  The user asks for a total or breakdown ("how much on groceries
  this month", "how much salary came in", "spending by category").
when_not_to_use: |
  The user wants to see the actual list of bookings — that's
  show_transactions, this only aggregates.
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
  from_date:
    type: string
    required: false
    description: First booking day to include, YYYY-MM-DD. For a calendar month ("in September") pass the 1st here and the last day as to_date; overrides days.
  to_date:
    type: string
    required: false
    description: Last booking day to include, YYYY-MM-DD.
outputs:
  by_category:
    type: array
    description: Each with category, total (negative = outgoing), n (transaction count).
  total_outgoing:
    type: number
  total_incoming:
    type: number
cost: 1 SELECT, no network — reads the local synced copy, never live FinTS.
permissions: [admin, member, restricted]
side_effects: none — read-only.
tags: [finance, bank, read, app:finance]
category: productivity
---

# spending_summary

Categorisation is a small hardcoded keyword table today (v1) — a
sizeable "Uncategorised" bucket is expected and should be named
honestly, not smoothed over. Same visibility rule as the other finance
skills, enforced by the query, not by the model.
