---
name: list_subtasks
description: List the subtasks of a parent task — helper before update_task / delete_task on children.
when_to_use: |
  When the user asks about subtasks of a specific parent ("show me
  the subtasks for X", "what's left in the launch prep task", "wie
  weit bin ich mit der Steuer-Erklärung").

  check_tasks lists ALL open tasks the user can see and renders cards
  — it has no parent filter, so asking it for "subtasks of X" returns
  the whole inbox. This skill is the complement: pass a parent_task_id
  and get JUST that parent's children, with no card emission.

  If you don't have the parent_task_id yet, call find_task_by_title
  first to resolve the parent, then pass its id here.

  After this returns:
    - 0 hits → the parent has no subtasks (or the parent id was wrong).
    - 1+ hits → quote progress to the user (e.g. "2 von 3 erledigt").
inputs:
  parent_task_id:
    type: integer
    required: true
    description: The id of the parent task whose children to list.
  include_done:
    type: boolean
    required: false
    default: true
    description: Include completed subtasks. Default true so "wie weit bin ich" surfaces what's been done already.
  limit:
    type: integer
    required: false
    default: 50
outputs:
  matches:
    type: array
    description: Each row has id, title, due_date, done. Open subtasks first, then done.
  count:
    type: integer
side_effects: None.
cost: One indexed SQLite query on parent_task_id.
permissions: ["*"]
tags: [tasks, resolver, internal]
---

# list_subtasks

Subtask resolver. Mirrors find_task_by_title in shape — LLM-internal
output, no cards. Pair with find_task_by_title to go from a parent's
title to its children's status in two cheap calls.
