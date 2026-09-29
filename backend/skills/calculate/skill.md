---
name: calculate
description: "Do arithmetic exactly (sums, per month, VAT, differences) instead of in your head."
when_to_use: |
  Any number you would otherwise compute yourself: "what is that per month" (551.07 / 12), a total of several amounts, net/gross with VAT, a difference between two amounts, a percentage.
  Write the expression with a dot for decimals (551.07 / 12). German cent amounts like 551,07 are understood too.
when_not_to_use: |
  - Totals a skill already returned (show_transactions total, spending_summary) — quote those.
  - Dates and weekdays — that's date_info.
inputs:
  expression:
    type: string
    required: true
    description: 'The calculation, e.g. "551.07 / 12", "550 + 1200 + 300", "463.08 * 1.19", "round(214.20 / 1.19, 2)". Allowed: numbers, + - * / // % **, parentheses, round, sum, min, max, abs.'
  decimals:
    type: integer
    required: false
    default: 2
    description: Decimal places of the result.
outputs:
  result:
    type: number
  result_de:
    type: string
    description: The result in German notation (45,92).
cost: instant, no network.
permissions: [admin, member, restricted]
side_effects: none.
tags: [math, tools]
category: productivity
---

# calculate

An AST evaluator — no names, no attribute access, no other calls — so
nothing but arithmetic can run.
