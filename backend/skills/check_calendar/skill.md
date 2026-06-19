---
name: check_calendar
description: Check the user's calendar for events / free slots in a given window
when_not_to_use: |
  Tasks → `check_tasks` (tasks have an optional due_date, events have date+time). Never use `run_sql` on the `events` table — gated runner blocks it.
when_to_use: |
  - User asks "what's on Thursday?" / "am I free at 3pm?"
  - Another skill (whatsapp_draft) needs availability before proposing meeting times
  - Briefing skill wants today's schedule for the morning summary
inputs:
  start_iso:
    type: string
    required: false
    description: ISO date/datetime — start of window. Defaults to now.
  end_iso:
    type: string
    required: false
    description: ISO date/datetime — end of window. Defaults to start + days.
  days:
    type: integer
    required: false
    default: 7
    description: Window length when end_iso is omitted (1-30)
  title_contains:
    type: string
    required: false
    description: |
      Case-insensitive substring filter on event title. USE THIS whenever
      the user is asking about a SPECIFIC event ("the Zahnarzttermin",
      "when's the haircut", "find the Müller meeting"). Pass the key word
      (e.g. "Zahnarzt", "haircut", "Müller"). The skill auto-widens to
      ±2 days if your window has 0 hits — handy when your weekday math
      was off by one. Without this you'd get ALL events in the window
      and have to filter mentally, which fails when the date is wrong.
  include_free_slots:
    type: boolean
    required: false
    default: false
    description: If true, also computes gaps between events (>= 30min) as candidate free slots.
outputs:
  events:
    type: array
    description: |
      [{date, time, title, who, starts_at, ends_at, all_day}] — events
      in the window, oldest first.
  free_slots:
    type: array
    description: |
      Only present when include_free_slots=true. [{date, start, end,
      duration_min}] — gaps of >= 30 min between events, grouped by day,
      08:00–22:00 working hours.
  window:
    type: object
    description: "{start_iso, end_iso} of the resolved window"
cost: 1 SQLite query (no LLM, no network)
permissions: [admin, member, restricted]
side_effects: none — read-only
tags: [calendar, scheduling, availability]
---

# check_calendar

Queries family.db `events` table for a time window. The optional
`include_free_slots` toggle does the gap-detection arithmetic so a
caller asking "what time can I propose?" doesn't have to reason about
event overlaps client-side.

Free slots are bound to 08:00–22:00 (configurable later via app_settings
if needed) and only emitted when ≥30 min — shorter gaps aren't useful
for proposing meetings.
