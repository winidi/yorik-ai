---
name: delete_task
description: Delete ONE task by id
when_to_use: |
  - User says "lösch die Aufgabe X" / "delete that task"
  - User says "die Aufgabe ist hinfällig"
inputs:
  task_id:
    type: integer
    required: true
    description: Exactly ONE task id per call — no list, no wildcard. To delete multiple tasks, call this skill multiple times (each requires confirmation).
outputs:
  deleted_task_id:
    type: integer
  task:
    type: object
permissions: [admin, member, restricted]
tags: [tasks, write]
---
# delete_task
Hard 1-row cap. Apply-then-confirm — cancel/test re-inserts the row.
