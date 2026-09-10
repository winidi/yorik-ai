---
name: recording_report
description: The structured report of a recording: decisions, tasks with a person, nice moments, friction, open questions and dates.
when_to_use: |
  Use it when the user asks what came out of the dinner or meeting, wants the report, or wants the tasks from it ("was ist beim Abendessen rausgekommen", "zeig den Bericht", "welche Aufgaben").
  Without `recording_id` it takes the user's latest finished recording; the report is generated on first call and reused afterwards (`refresh: true` writes it anew).
  Present the sections in the user's language in this order: summary, decisions, tasks (numbered, with the person and date), nice moments, what did not go well, open questions, dates.
  Tasks are proposals: offer to add them, and when the user agrees call add_task for each chosen one with title, person and due_date.
when_not_to_use: |
  The plain transcript or the recording's state; that is recording_status.
inputs:
  recording_id:
    type: integer
    required: false
    description: The recording; defaults to the latest finished one the user may see.
  template:
    type: string
    required: false
    description: dinner or meeting; defaults to the recording's kind.
  refresh:
    type: boolean
    required: false
    description: Generate the report again even if one exists.
outputs:
  report:
    type: object
    description: summary, decisions[], tasks[{title, person, due_date, why}], highlights[], friction[], open_questions[], dates[].
  generated:
    type: boolean
    description: True when the report was written in this call (takes 10 to 90 seconds).
cost: instant when the report exists, else one long LLM pass
permissions: [admin, member, restricted]
side_effects: Stores the report on the recording; the first generation notifies the participants.
tags: [recordings, read]
category: system
---

# recording_report

Reads `recordings.report_json`, or builds it through
`backend/recording_reports.py` when missing. Dinner and meeting
recordings normally arrive with the report already written by the
pipeline (`HOMEOS_RECORDING_AUTO_REPORT`), so the usual call is a
lookup.

Visibility follows the recording: participants only.
