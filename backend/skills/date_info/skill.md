---
name: date_info
description: "Weekday of a date and date arithmetic (\"in 3 Wochen\", days until …), never done in your head."
when_to_use: |
  - You are about to name a weekday for a date ("der 2. Oktober ist ein …"): look it up here.
  - "in 3 Wochen", "nächsten Monat am 5.", "4 Wochen vor dem 31.12.": add_days / add_weeks / add_months (negative to go back).
  - "wie viele Tage bis …": until.
when_not_to_use: |
  - Looking up appointments — that's check_calendar.
inputs:
  date:
    type: string
    required: false
    description: YYYY-MM-DD; empty or "today" means today (household time).
  add_days:
    type: integer
    required: false
    default: 0
    description: Days to add (negative goes back).
  add_weeks:
    type: integer
    required: false
    default: 0
  add_months:
    type: integer
    required: false
    default: 0
  until:
    type: string
    required: false
    description: A second date, YYYY-MM-DD, to count the days between.
outputs:
  result:
    type: object
    description: date, weekday (German), weekday_en, iso_week, shown ("Freitag, 02.10.2026").
  days_between:
    type: integer
cost: instant, no network.
permissions: [admin, member, restricted]
side_effects: none.
tags: [dates, tools]
category: productivity
---

# date_info

Pure date arithmetic; months keep the day, clamped to the month's end.
