---
name: update_calendar_event
description: Modify an existing calendar event (move time, change title, etc.)
when_to_use: |
  - User asks to reschedule: "verschieb Müller-Termin auf Freitag 14 Uhr"
  - User clarifies a title: "der Termin am Dienstag ist eigentlich mit Anna"
  - User adds notes: "füg hinzu: Geschenk mitbringen"
  Always call check_calendar first if you don't already know the event_id —
  never guess.
inputs:
  event_id:
    type: integer
    required: true
    description: ID of the event to update (from check_calendar or a previous skill result).
  title:
    type: string
    required: false
    description: New title. Omit to keep current.
  starts_at:
    type: string
    required: false
    description: New ISO 8601 start. Omit to keep current.
  ends_at:
    type: string
    required: false
    description: New ISO 8601 end. Omit to keep current.
  all_day:
    type: boolean
    required: false
  person:
    type: string
    required: false
  notes:
    type: string
    required: false
  category:
    type: string
    required: false
    description: Recolour the event. One of family / business / drive / health / personal / social. Empty string clears it. See add_calendar_event for category coverage.
outputs:
  event_id:
    type: integer
  event:
    type: object
    description: The updated row.
  verified_state:
    type: object
    description: Post-update state re-read from the DB. Quote these times verbatim; never rely on what you remember about what you set.
  pending:
    type: boolean
    description: True if user confirmation is required before the update lands.
permissions: [admin, member, restricted]
tags: [calendar, event, mutation]
---

# update_calendar_event

Single-row UPDATE on the events table. Only fields explicitly passed are
changed; omitted fields stay as-is.

Beta safety: when the user has `confirm_mutations=true`, the UPDATE is
deferred and the user sees a modal showing old → new values before the
write happens.
