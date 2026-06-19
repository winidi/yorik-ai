---
name: compute_group_price
description: Sum group entry prices deterministically from tickets + per-ticket headcounts.
when_to_use: |
  After the user asked for a group total ("was kostet das für 2
  Erwachsene + 2 Kinder?") and you've identified the right prices
  (either from web_extract or the user typed them). DON'T do the
  arithmetic in your head — call this skill. It also emits a nice
  receipt-style card in the chat (price_summary ui_action).

  Workflow:
    1. From web_extract, identify the relevant tickets.
    2. Ideally use the CLARIFY-BEFORE-CALCULATING rule first if the
       price structure is ambiguous (state interpretation + ask).
    3. Call compute_group_price with the matched items.
    4. Reply with the total + offer a follow-up (e.g. "soll ich euren
       Besuch eintragen?").

  Example call:
    compute_group_price(
      items=[
        {"label": "Erwachsene (Begleitperson)", "unit_eur": 5.90, "count": 2},
        {"label": "Kind 7 Jahre",               "unit_eur": 11.90, "count": 1},
        {"label": "Kleinkind 0 Jahre",          "unit_eur": 7.00, "count": 1}
      ],
      title="Monkeytown Braunschweig",
      source_url="https://monkeytown.eu/de/braunschweig/preise"
    )
  → returns total=30.70, breakdown rendered as a receipt card.

  Use this for ANY group-cost calculation — restaurants, museums,
  sports clubs, transport tickets, anything. Not just admission.
inputs:
  items:
    type: array
    required: true
    description: |
      List of {label, unit_eur, count} dicts — one per ticket category
      the user actually needs. `unit_eur` is the per-person price
      (decimal number; "5,90" → 5.90). `count` is how many.
  title:
    type: string
    required: false
    description: Header shown on the price-summary card. Usually the venue or event name.
  source_url:
    type: string
    required: false
    description: URL the prices were extracted from. Shown as the source on the card so the user can verify.
  currency:
    type: string
    required: false
    default: EUR
    description: ISO 4217 (default EUR). Used for display only; math is always plain decimals.
outputs:
  total_eur:
    type: number
    description: Sum of unit_eur × count across items, rounded to 2 decimals.
  line_items:
    type: array
    description: Each input item augmented with subtotal_eur. Same order as input for the receipt.
  total_count:
    type: integer
    description: Sum of `count` values across all items — the total head count.
permissions: [admin, member, restricted]
side_effects: |
  Emits a `price_summary` ui_action that renders as an inline receipt-
  style card in chat (line items + total). No DB writes.
tags: [math, prices, web]
---

# compute_group_price

Tiny deterministic arithmetic helper. Two reasons it exists despite
being "just multiplication":

1. **LLM math is unreliable** for anything past 3 line items. Especially
   with comma-vs-dot decimals (German "5,90 €" vs English "5.90"). One
   missed conversion and the total is off by 90 €.
2. **Receipt-style display** in the chat is much friendlier than the
   LLM listing them in prose. The price_summary card shows line items
   + total + source URL + a "Save these prices to <venue>" button (so
   the next time the user asks, it's instant).
