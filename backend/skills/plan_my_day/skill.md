---
name: plan_my_day
description: Gather what a day plan needs (fixed appointments, open tasks, carry-over, yesterday's review, outside context) so you can draft the day with the user and iterate before plan_day writes it.
when_to_use: |
  Trigger: "plan meinen Tag", "wie sieht mein Tag aus", "lass uns den Tag strukturieren", "plan my day".
  Call it first; it returns the material, never a finished plan.
  Draft with fixed sections: fixed events, at most 4 time blocks, today's full task list without times, suggestions from conversations, suggestions from the person's agent, one backlog line.
  Follow the person's `rules` (who does what at home, working hours, how many items).
  Suggestions (`report_candidates`, `agent_candidates`) stay out of the plan until the user picks one; list all agent candidates, never a selection.
  Then ask what to change; call plan_day only when the user says the plan is good.
  When the user corrects who does what or a habit, ask once whether to remember it and call remember_planning_rule.
  When the result has `outside_error`, plan from Yorik's data alone and mention it in half a sentence.
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
    description: Also ask the person's own agent (their Hermes) for candidates from their backlog and notes.
  request:
    type: string
    required: false
    description: What the user said, verbatim, so the outside agent knows what to look for.
outputs:
  context:
    type: object
    description: date, weekday, fixed_events, existing_blocks, open_tasks, backlog (open_total, due_later, more_undated), free_minutes, carry_over, report_candidates, rules, yesterday_review.
  agent_candidates:
    type: array
    description: Suggestions from the person's own agent, each {title, estimated_minutes, why}.
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
