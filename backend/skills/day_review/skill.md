---
name: day_review
description: Compare the day's plan with what actually got done and give the user a short, honest, encouraging review.
when_to_use: |
  Trigger: "wie war mein Tag", "Tagesabschluss", "was habe ich heute geschafft", "day review", or the evening check-in.
  The result is numbers and lists; turn them into three to five sentences in the user's language: what got done, where time was over or under the estimate, what carries over to tomorrow.
  Be warm and concrete, name the finished things, no lecturing about what is left.
when_not_to_use: |
  Planning tomorrow: that is plan_my_day (it reads this review on its own).
inputs:
  date:
    type: string
    required: false
    description: ISO date to review; defaults to today.
outputs:
  planned:
    type: integer
  done_planned:
    type: integer
  done_extra:
    type: integer
  open:
    type: integer
  estimated_minutes:
    type: integer
  actual_minutes:
    type: integer
  done:
    type: array
  left:
    type: array
cost: instant
permissions: [admin, member]
side_effects: Stores the review with the day plan so tomorrow's planning sees it.
tags: [tasks, planning]
category: planning
---

# day_review

Plan versus reality, once a day. The numbers come from tasks.done_at,
estimated_minutes and actual_minutes (the task timer).
