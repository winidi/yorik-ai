---
name: delete_calendar_event
description: Delete ONE calendar event by id
when_to_use: |
  - User cancels a meeting: "der Termin am Dienstag fällt aus"
  - User asks to clean up: "lösch den Test-Termin"
  Always call check_calendar first to get the event_id — never guess.
  IMPORTANT: this skill deletes exactly ONE event per call. If the user
  asks to delete multiple events, call this skill multiple times — once
  per event_id. Never combine.
inputs:
  event_id:
    type: integer
    required: true
    description: The single event id to delete. NOT a list. NOT a wildcard. Exactly one positive integer.
outputs:
  deleted_event_id:
    type: integer
  event:
    type: object
    description: The row that was deleted (so it can be quoted in the reply).
  pending:
    type: boolean
    description: True if user confirmation is required before the delete lands.
permissions: [admin, member, restricted]
tags: [calendar, event, mutation, destructive]
---

# delete_calendar_event

**Hard safety constraint:** exactly one event per call. The backend
rejects anything else (list, missing, zero, negative). This is
deliberate — the LLM cannot mass-delete calendar entries even if it
tries to.

Beta safety: when `confirm_mutations=true`, deletion is deferred until
the user confirms via the modal. The modal shows the full event so the
user can verify they're deleting the right one.
