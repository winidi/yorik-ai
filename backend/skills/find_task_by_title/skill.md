---
name: find_task_by_title
description: Look up task IDs by title — helper for the next update_task / delete / mark_done step.
when_to_use: |
  When the user names a SPECIFIC task ("delete the milk task", "mark the gym
  one done", "change the deadline of the Steuer-Erklärung") and you need its
  `task_id` to call update_task / delete_task / mark_task_done.

  check_tasks renders cards to the user but hides titles + IDs from the LLM
  (anti-enumeration). This skill is the complement — LLM-only output, no
  cards, returns the exact rows needed.

  Default `include_done=true` so "mark the milk task done" finds already-
  completed tasks too (the user may want to re-open or delete them).

  After this returns:
    - 0 hits   → tell the user, ask them to clarify.
    - 1 hit    → use task_id directly in the follow-up call.
    - 2+ hits  → ask the user which one (quote title + due_date).
inputs:
  query:
    type: string
    required: true
    description: Substring or word fragment to match against task title. Case-insensitive. Wildcards not needed — implementation already does LIKE-style matching.
  include_done:
    type: boolean
    required: false
    default: true
    description: Include completed tasks. Default true (covers "the milk task I marked done earlier").
  limit:
    type: integer
    required: false
    default: 10
    description: Cap result count. Stays small because this is LLM-internal.
outputs:
  matches:
    type: array
    description: Each row has id, title, due_date, done, parent_task_id. Read these directly; do NOT quote to the user except for disambiguation when multiple matches.
  count:
    type: integer
side_effects: None. No UI emission, no LLM-visible cards.
cost: One indexed SQLite LIKE query.
permissions: ["*"]
tags: [tasks, resolver, internal]
---

# find_task_by_title

Title resolver for the tasks table. Companion to `check_tasks` — that
one shows cards to the user without leaking titles to the LLM; this
one returns IDs to the LLM without showing anything to the user.

Use this BEFORE update_task / delete_task / mark_task_done whenever
the user names the task by title rather than by ID.
