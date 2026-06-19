---
name: block_travel_time
description: Insert a 'Drive to X' event to block drive time before an appointment.
when_not_to_use: |
  Don't auto-call from an `add_calendar_event` result unless the user explicitly asked for a travel block in their message. Don't use to create a standalone event without a main event already on the calendar (this skill needs `event_id`). Exception: when the user requests forward AND return in the same message, call both back-to-back without re-asking.
when_to_use: |
  - User confirms a drive-time block after add_calendar_event offered one. Example:
      assistant: "Termin eingetragen. Anfahrt ca. 34 Min — soll ich die blockieren?"
      user: "ja" → `block_travel_time(event_id=<id>)`
  - User asks in hindsight ("blockier die Anfahrt zum Hannover-Termin morgen") — find the event_id via `check_calendar`, then call this skill.
  - User wants more buffer than the routed time → pass `minutes=<bigger>`.

  Never auto-call — always confirm verbally first. Quote `verified_state.starts_at`/`ends_at` verbatim; never recompute departure time.
inputs:
  event_id:
    type: integer
    required: true
    description: ID of the main event whose drive time to block.
  minutes:
    type: integer
    required: false
    description: Override block length. Defaults to the event's `travel_time_s` (rounded up). Use when the user wants extra buffer or the event has no stored travel time.
  direction:
    type: string
    required: false
    default: forward
    description: |
      'forward' = Anfahrt before the event; 'return' = Rückfahrt after. They coexist on the same event with different markers. Re-running with the same minutes is a no-op; re-running with different minutes slides the existing block in place — use this when the user corrects the drive time after a block is already placed.
outputs:
  block_event_id:
    type: integer
    description: ID of the newly-inserted "Drive to:" event.
  minutes:
    type: integer
    description: How long the block is.
  starts_at:
    type: string
    description: ISO 8601 start of the block.
  ends_at:
    type: string
    description: ISO 8601 end of the block (= main event's starts_at).
  already_blocked:
    type: boolean
    description: True if a buffer event was already linked. Pair with `updated_in_place` to tell whether it was untouched or slid.
  updated_in_place:
    type: boolean
    description: True if an existing block was found and slid to new times because its duration didn't match the requested `minutes`. When true, quote `verified_state` to the user.
  verified_state:
    type: object
    description: Authoritative current state of the block — {block_event_id, starts_at, ends_at, minutes}. Quote these times verbatim; never rely on what you think you set.
cost: 1 SELECT + 1 INSERT (no LLM, no network)
permissions: [admin, member, restricted]
side_effects: |
  Inserts a second row into the events table with a `[LINKED_TO=<id>]`
  marker in its notes column. delete_calendar_event sweeps these
  linked rows automatically when the main event is deleted.
tags: [calendar, scheduling, travel, write]
---

# block_travel_time

Companion to `add_calendar_event`. The main skill stores travel
metadata on the event row (drives the calendar's travel-time badge); this
skill takes that further by inserting a *separate* event that
visually reserves the slot before the appointment, so the user's
calendar shows the drive time as actually unavailable.

The block is linked to the main event via a `[LINKED_TO=<id>]`
marker in its `notes` column. `delete_calendar_event` looks for that
marker and cascade-deletes any buffer rows when the main event goes,
so deleting the appointment cleans up the block automatically.

Re-running on the same event+direction is a no-op (returns
`already_blocked: true` plus the existing block's id), so the LLM can
call it safely without worrying about duplicates if the user reconfirms.

## Return-trip playbook (forward calls only)

When called with the default `direction='forward'`, the result includes
a `return_trip` object the LLM MUST act on before moving on:

```
return_trip: {
  feasible:   true|false,
  kind:       "home" | "to_next_event",
  minutes:    <estimated drive minutes>,
  estimated:  true,
  next_event: null | { id, title, starts_at, has_location, gap_minutes },
  warning:    null | "<overlap text>",
}
```

After the forward block lands, decide based on what the user already said:

  - User's CURRENT message already requested BOTH directions ("anfahrt und
    rückfahrt", "hin- und rückweg", "drive there and back", "auch die
    rückfahrt mit X minuten") → DO NOT ask. Immediately call this skill
    again with `direction='return'` in the same turn, using the user's
    stated minutes (or `return_trip.minutes` if they didn't give one).
    Asking again would be redundant — they consented upfront.

  - User asked for forward only → ask one short sentence and wait for "ja":
    - `next_event` is null → "Soll ich die Heimfahrt (~N Min) auch blocken?"
    - `next_event` set, `feasible=true`  → "Danach geht's direkt zu
      „<next.title>" um <HH:MM> — soll ich die Weiterfahrt (~N Min) blocken?"
    - `feasible=false` → SURFACE the `warning` text verbatim before asking,
      so the user can decide whether to move the next event or skip the
      return block.

On the user's "ja" / "yes" (in the ask-then-call flow) → call this skill
again with `direction='return'` (same `minutes` from `return_trip.minutes`
unless the user gave a different number).
On "nein" → drop it silently, no further mention.
