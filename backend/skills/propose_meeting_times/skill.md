---
name: propose_meeting_times
description: Find N free calendar slots and draft an email reply proposing them
when_to_use: |
  - User says "antworte Müller, dass nächste Woche Zeit habe — schlag drei Termine vor"
  - "reply to this email with 3 meeting times next week"
  - "schick ihm drei Vorschläge, wann wir telefonieren können"
  - Any combination of: "reply to an email" + "with meeting times" + "from my calendar"
inputs:
  message_id:
    type: integer
    required: true
    description: The email message id to reply to (from email_messages.id). Get it from the user's context — they're usually replying to a visible email.
  num_slots:
    type: integer
    required: false
    default: 3
    description: How many free slots to propose (1-5)
  window_days:
    type: integer
    required: false
    default: 7
    description: Search this many days from now for free time
  duration_minutes:
    type: integer
    required: false
    default: 60
    description: Length of the meeting being proposed
  earliest_hour:
    type: integer
    required: false
    default: 9
    description: Don't suggest slots before this hour (24-hour clock)
  latest_hour:
    type: integer
    required: false
    default: 18
    description: Don't suggest slots after this hour (24-hour clock)
outputs:
  slots:
    type: array
    description: The chosen free slots (date, start_time, end_time)
  drafts:
    type: array
    description: Draft email variants from email_draft, each {label, text}
  sources:
    type: array
    description: Citation snippets surfaced alongside the draft (from email_draft)
permissions: [admin, member, restricted]
tags: [calendar, email, scheduling, free-time]
---

# propose_meeting_times

The "intelligent assistant" skill: chains `check_calendar` (for free
slots) with `email_draft` (for the reply) so one user request produces
a sendable proposal in seconds.

Workflow:
1. Compute free slots in the [now → now+window_days] window during
   working hours (earliest_hour..latest_hour), avoiding existing events.
2. Pick `num_slots` slots that are well-distributed (no two on the same
   day, spread across the week).
3. Call email_draft on the target message_id with a body that includes
   the slots formatted naturally (matched to the user's language).
4. Return both the slots and the draft id. The chat renders a card
   that lets the user open the draft to edit + send.

Speaks naturally in the user's language — German if their profile says
de, English otherwise. Times rendered conversationally ("Dienstag um
14 Uhr", "Friday at 3 PM") not as ISO.

Pattern: this is the canonical "skill composition" example — one skill
calls two others. See docs/SKILLS.md.
