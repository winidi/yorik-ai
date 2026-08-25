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
  pending:
    type: boolean
    description: Always true — the delete is staged, not executed. It runs only when the user taps Delete on the card.
  pending_id:
    type: string
  task:
    type: object
    description: The row that WOULD be deleted.
permissions: [admin, member, restricted]
tags: [tasks, write]
---
# delete_task
Hard 1-row cap. Confirm-before-apply — nothing is deleted until the user taps Delete on the card; Keep discards it. Reply that the card is waiting, never that the task is gone.
