---
name: check_tasks
description: List open tasks in a date window (default = today + open-ended)
when_not_to_use: |
  Calendar events → `check_calendar` (events have date+time, tasks have an optional due_date). Never use `run_sql` on the `tasks` table — the gated runner blocks it and the model loses the interactive task-card UI this skill emits.
when_to_use: |
  Trigger phrases (all of these → call this skill):
  - "what tasks do I have today / tomorrow / this week"
  - "welche Aufgaben sind überfällig" / "what's overdue" → `overdue_only=true` (do NOT pass start_iso / end_iso for this — they'd build a wrong window)
  - "open tasks" / "offene Aufgaben" → no window, include_undated=true
  - "was muss Anna noch erledigen" → person="Anna" (her list: tasks assigned to her)
  - "alle Aufgaben" / "was ist im Haushalt offen" / "die der Familie" → everyone=true
  - Default ("meine Aufgaben", "was muss ich tun", "was steht heute an") → no person, no everyone: the asker's OWN list only
  - "did I finish anything yesterday" → include_done=true, start/end=yesterday
  - briefing templates needing the per-day task list

  MUST emit `tasks_found` whenever results.length >= 1. The skill does
  this automatically — don't re-enumerate the tasks in your text reply.
  Reply ONE short sentence ("Drei offene Aufgaben, siehe Karten unten").
inputs:
  start_iso:
    type: string
    required: false
    description: ISO date or datetime. If set, only tasks with due_date >= this are included. Tasks with no due date are still included when include_undated is true.
  end_iso:
    type: string
    required: false
    description: ISO date or datetime. If set, only tasks with due_date <= this are included.
  include_undated:
    type: boolean
    required: false
    default: false
    description: When true (and start_iso unset, or behaviour matches the user intent), also return tasks with no due_date. Useful for "what's open" — but noisy for "what's planned tomorrow", so off by default.
  include_done:
    type: boolean
    required: false
    default: false
    description: Set true for a retrospective ("what did I finish yesterday"). Default off — most callers want open tasks only.
  person:
    type: string
    required: false
    description: Someone else's list — a household member's first name ("Anna"). Tasks assigned to that person, among those the asker may see.
  overdue_only:
    type: boolean
    required: false
    default: false
    description: Strictly past-due open tasks (`due_date < today AND done = 0`). Overrides start_iso / end_iso / include_undated — pass this alone for any "überfällig / overdue" question instead of trying to build the date window by hand.
  mine_only:
    type: boolean
    required: false
    default: true
    description: The asker's own list (assigned to them, or assigned to nobody and created by them) — the default, same as the Tasks app. Kept for briefings.
  everyone:
    type: boolean
    required: false
    default: false
    description: Everything the asker may see — other members' tasks and the children's chores included, each labelled with whose it is. Only when they ask for the household / everyone.
outputs:
  tasks:
    type: array
  window:
    type: object
permissions: [admin, member, restricted]
tags: [tasks, read]
---
# check_tasks
Read-only query against the tasks table. Answers "whose" as well as "when": by default only the asker's own list; tasks of other people carry the owner's first name on the card (`person`), so say "Beates Aufgabe", never "deine". Date filter is INCLUSIVE on both sides (matches check_calendar). Returns a list shaped for direct briefing rendering.

UI: this skill ALSO emits a `tasks_found` ui_action that the chat renders as interactive task rows (checkbox to mark done, click → opens /tasks with that task highlighted). When you call this skill, do NOT mirror the full task list as a markdown bullet list in your reply — the user already sees the clickable cards. Keep your prose short: a one-line summary ("Du hast 7 überfällige Aufgaben — markier sie unten fertig oder öffne /tasks") is enough. The card is the answer.
