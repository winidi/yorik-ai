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
  pending:
    type: boolean
    description: Always true — the delete is staged, not executed. It runs only when the user taps Delete on the card.
  pending_id:
    type: string
  event:
    type: object
    description: The row that WOULD be deleted (quote its title so the user can check the card).
permissions: [admin, member, restricted]
tags: [calendar, event, mutation, destructive]
---

# delete_calendar_event

**Hard safety constraint:** exactly one event per call. The backend
rejects anything else (list, missing, zero, negative). This is
deliberate — the LLM cannot mass-delete calendar entries even if it
tries to.

Confirm-before-apply: the skill only stages the delete and shows a card
with the full event; nothing is removed until the user taps Delete on
that card. Reply that the card is waiting — never say the event is gone.
