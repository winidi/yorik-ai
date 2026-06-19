---
name: find_event_by_title
description: Look up event IDs by title — helper for the next update_calendar_event / delete step.
when_to_use: |
  When the user names a SPECIFIC event ("move the gym one to 7pm",
  "delete the dentist appointment", "wann ist der Steuer-Termin?")
  and you need the `event_id` for update_calendar_event /
  delete_calendar_event.

  check_calendar renders cards but hides titles + IDs from the LLM.
  This skill is the complement — LLM-only output, returns the exact
  rows needed.

  Default window is generous (30 days back, 90 days forward) because
  the user often names a past event ("delete last week's gym") or a
  future one without anchoring a date.

  After this returns:
    - 0 hits   → tell the user, ask them to clarify.
    - 1 hit    → use event_id directly in the follow-up call.
    - 2+ hits  → ask the user which one (quote title + date).
inputs:
  query:
    type: string
    required: true
    description: Substring of the event title. Case-insensitive.
  days_back:
    type: integer
    required: false
    default: 30
    description: How many days back to look. Negative not allowed; the skill clamps.
  days_forward:
    type: integer
    required: false
    default: 90
    description: How many days forward to look.
  limit:
    type: integer
    required: false
    default: 10
outputs:
  matches:
    type: array
    description: Each row has id, title, starts_at, ends_at, all_day. Quote dates back to the user only for disambiguation.
  count:
    type: integer
side_effects: None.
cost: One indexed SQLite LIKE query.
permissions: ["*"]
tags: [calendar, events, resolver, internal]
---

# find_event_by_title

Title resolver for the events table. LLM-internal — no cards. Use
BEFORE update_calendar_event / delete_calendar_event when the user
names the event by title rather than by ID.
