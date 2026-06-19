---
name: update_task
description: Modify an existing task (mark done, change title, set due date, etc.)
when_to_use: |
  - User says "ich hab X erledigt" → set done=true on matching task
  - User says "verschieb die Aufgabe auf Freitag" → change due_date
  - User says "die Aufgabe heißt eigentlich Y" → change title
  Always call list/SELECT tasks first if you don't already know the task_id — don't guess.
inputs:
  task_id:
    type: integer
    required: true
  title:
    type: string
    required: false
  due_date:
    type: string
    required: false
    description: ISO date YYYY-MM-DD, or empty string "" to clear.
  done:
    type: boolean
    required: false
  person:
    type: string
    required: false
  category:
    type: string
    required: false
  notes:
    type: string
    required: false
  parent_task_id:
    type: integer
    required: false
    description: Re-parent this task under another (or set to null to top-level it). Use cautiously — usually only when the user explicitly asks to nest something.
  recurrence_rule:
    type: string
    required: false
    description: |
      Set/change/clear the recurrence. Pass an empty string "" to clear.
      Shorthand forms: "daily", "weekly", "biweekly", "monthly",
      "quarterly", "yearly", "every N days|weeks|months|years",
      "every Mon", "every Mon,Wed,Fri". When marking a recurring task
      done (done=true), the next instance is auto-materialised and the
      response includes `next_instance_id` — mention it to the user.
outputs:
  task_id:
    type: integer
  task:
    type: object
  next_instance_id:
    type: integer
    description: Set ONLY when marking a recurring task done; the id of the freshly-created next instance.
permissions: [admin, member, restricted]
tags: [tasks, write]
---
# update_task
Apply-then-confirm. Captures pre-update values; cancel/test restores them.
Recurring-task awareness: when `done` flips false→true and the task has a
`recurrence_rule`, the next instance is materialised in the same
transaction (see backend/tasks_recurrence.py). The skill returns
`next_instance_id` and a brief `_llm_hint` so the chat can confirm the
roll-over without a second read.
