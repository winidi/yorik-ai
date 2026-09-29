---
name: add_task
description: Create a new task in Yorik's task list
when_not_to_use: |
  Calendar events → `add_calendar_event` (events have date+time, tasks have an optional due_date).
when_to_use: |
  - User says "add a task: X", "remind me to Y", "put Z on the list"
  - User says "write that on my task list"
inputs:
  title:
    type: string
    required: true
    description: Short task title (e.g. "Take out the trash", "Send the invoice to Müller").
  due_date:
    type: string
    required: false
    description: ISO date YYYY-MM-DD if the user gave one ("by Friday" → resolve to the date). Omit when open-ended.
  person:
    type: string
    required: false
    description: Who it's for. "admin" | "member" | "child" | a name string.
  category:
    type: string
    required: false
    description: One of the configured task categories (admin / haushalt / arbeit / etc.) — only if you can pick the right one with high confidence. Otherwise omit.
  notes:
    type: string
    required: false
  parent_task_id:
    type: integer
    required: false
    description: |
      When the user adds a sub-step to an existing task ("for the Q1 tax
      task: scan all receipts"), set this to the parent task's id.
      Resolve via check_tasks first if you only have a title. Subtasks
      cascade-delete with their parent.
  recurrence_rule:
    type: string
    required: false
    description: |
      Only when the user said it recurs ("every Monday", "every week",
      "monthly"). Use one of the shorthands the parser understands:
        "daily", "weekly", "biweekly", "monthly", "quarterly", "yearly",
        "every N days|weeks|months|years", "every Mon", "every Mon,Wed,Fri".
      When the user marks the task done, the next instance is
      auto-materialised. Omit for one-shot tasks.
outputs:
  task_id:
    type: integer
  task:
    type: object
permissions: [admin, member, restricted]
tags: [tasks, write]
---
# add_task
Apply-then-confirm pattern: INSERTs immediately + stages a rollback (delete by id) so cancel/test removes the row.
