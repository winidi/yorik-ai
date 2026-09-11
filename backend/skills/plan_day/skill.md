---
name: plan_day
description: Write an agreed day plan into Yorik as tasks plus time blocks in the user's Plan calendar, as one undoable action.
when_to_use: |
  Call it only after the user has said the plan is good ("mach's", "passt", "übernehmen"); never on the first draft.
  Pass the full list of items for that day every time; items the previous version had and this one lacks are removed, items with the same key are updated.
  Give each item a short stable `key` (e.g. "schreiben-video-9") so a later revision updates instead of duplicating.
  Only items with fixed times or deliberate focus blocks get `start`/`end` (HH:MM); at most 6 blocks a day, the rest stay tasks.
  Use `task_id` when an item stands for an existing open task so it is scheduled, not recreated.
  Use `report_ref` (from report_candidates) when an item was a suggestion from a recording report, so the report shows it as adopted.
  Items without times are fine and expected; a plan may hold up to 40 items.
when_not_to_use: |
  Drafting or discussing a plan: that is plan_my_day. A single appointment or task: add_calendar_event / add_task.
inputs:
  date:
    type: string
    required: true
    description: ISO date YYYY-MM-DD of the day being planned.
  items:
    type: array
    required: true
    description: 'List of {key, title, start?, end?, category?, notes?, estimated_minutes?, task_id?, report_ref?}. Times as HH:MM.'
outputs:
  summary:
    type: object
    description: created / updated / removed / blocks counts.
  pending_id:
    type: string
    description: One card undoes the whole day.
cost: instant, one DB transaction
permissions: [admin, member]
side_effects: Creates, updates or deletes tasks and events of that day's plan (undoable as a whole).
tags: [tasks, calendar, planning, mutation]
category: planning
---

# plan_day

The mechanics of a day plan: idempotent per date and item key, one
preview card, one undo. See backend/day_plans.py.
