---
name: plan_my_day
description: Gather what a day plan needs (fixed appointments, open tasks, carry-over, yesterday's review, outside context) so you can draft the day with the user and iterate before plan_day writes it.
when_to_use: |
  Trigger: "plan meinen Tag", "wie sieht mein Tag aus", "lass uns den Tag strukturieren", "plan my day".
  Call it first; it returns the material, never a finished plan.
  Then draft 3 to 8 items around the fixed events, with 2 to 4 focus blocks at most, and show the draft as a short list with times; ask what to change.
  Iterate in conversation as often as the user wants; call plan_day only when they say the plan is good.
  When the result has `outside_context`, weave it in; when it has `outside_error`, say the workstation agent was not reachable and plan from Yorik's data alone.
  Carry-over tasks come first unless the user says otherwise.
when_not_to_use: |
  Adding a single task or appointment. Writing the plan: that is plan_day.
inputs:
  date:
    type: string
    required: false
    description: ISO date to plan; defaults to today.
  ask_agent:
    type: boolean
    required: false
    default: true
    description: Also ask the household's strong agent (Hermes) for today's briefing and open points.
  request:
    type: string
    required: false
    description: What the user said, verbatim, so the outside agent knows what to look for.
outputs:
  context:
    type: object
    description: date, weekday, fixed_events, existing_blocks, open_tasks, carry_over, yesterday_review.
  draft:
    type: object
    description: The draft saved for this day earlier in the conversation, if any.
  outside_context:
    type: string
    description: What the outside agent contributed (briefing, open points), when reachable.
cost: instant for Yorik data; 10–60 s when the outside agent is asked
permissions: [admin, member]
side_effects: none (reads only; the outside agent receives the request text)
tags: [tasks, calendar, planning]
category: planning
---

# plan_my_day

Material for a conversation, not a decision. The assistant drafts, the
user shapes it over two or three rounds, plan_day writes it.
