---
name: find_bill_by_name
description: Look up bill IDs by name — helper for the next update_bill / delete / mark_bill_paid step.
when_to_use: |
  When the user names a SPECIFIC bill ("mark the Stromrechnung as
  paid", "delete the gym membership bill", "wann ist die
  Hausratversicherung fällig?") and you need the `bill_id` to act.

  check_bills renders cards but hides names + IDs from the LLM. This
  skill is the complement.

  Default `include_paid=true` so the LLM can find historical paid
  bills too (the user may want to look up an amount from last month).

  After this returns:
    - 0 hits   → tell the user, ask them to clarify.
    - 1 hit    → use bill_id directly.
    - 2+ hits  → ask the user which one (quote name + amount + due_date).

  NOT for scanned invoice PDFs / Rechnungs-Belege — route those to
  search_documents instead. This is the bill REGISTER (calendar of
  expected payments), not the document archive.
inputs:
  query:
    type: string
    required: true
    description: Substring of the bill name. Case-insensitive.
  include_paid:
    type: boolean
    required: false
    default: true
    description: Include already-paid bills. Default true (covers "what did I pay for X last month").
  limit:
    type: integer
    required: false
    default: 10
outputs:
  matches:
    type: array
    description: Each row has id, name, amount, currency, due_date, paid, recurring.
  count:
    type: integer
side_effects: None.
cost: One indexed SQLite LIKE query.
permissions: ["*"]
tags: [bills, finance, resolver, internal]
---

# find_bill_by_name

Name resolver for the bills table. LLM-internal — no cards. Use
BEFORE update_bill / delete_bill / mark_bill_paid when the user
names the bill rather than passing an ID.
