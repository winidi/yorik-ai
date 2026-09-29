---
name: remind_me
description: Remind the user at a later moment — a bell entry with a push to their phone at that time.
when_to_use: |
  The user wants to be reminded later: "remind me in an hour about …", "tell me tomorrow at 7 that …", "remind me at 5 to …".
  Pass in_minutes for "in X minutes/hours", or at (household local time, YYYY-MM-DDTHH:MM) for a clock time.
  If they gave no time at all, ask for one instead of guessing.
when_not_to_use: |
  - Something to note right now without a time — that's notify.
  - An appointment others should see in the calendar — that's add_calendar_event.
  - A to-do without a specific moment — that's add_task.
inputs:
  title:
    type: string
    required: true
    description: What the reminder says, one short line (e.g. "Hang up the laundry").
  at:
    type: string
    required: false
    description: Clock time in household local time, YYYY-MM-DDTHH:MM (e.g. 2026-10-01T07:00).
  in_minutes:
    type: integer
    required: false
    description: Minutes from now (e.g. 60 for "in an hour"). Use instead of at.
  body:
    type: string
    required: false
    description: One or two sentences of detail, optional.
outputs:
  reminder_id:
    type: integer
  due_local:
    type: string
    description: The moment it fires, household local time.
cost: instant
permissions: [admin, member, restricted]
side_effects: Stores one reminder for the calling user; at due time it becomes a bell entry and a push.
tags: [notifications, reminders, write]
category: productivity
---

# remind_me

Rows live in `reminders`; backend/reminders.py fires due ones once a
minute through notifications.create (bell + push). Only the calling
user is reminded; nobody can set a reminder for somebody else.
