---
name: pipeline
description: "Follow up a sent mail until the answer comes, and list, pause or resume what Yorik follows."
when_to_use: |
  - After a mail went out, the user says "follow up if there's no answer", "stay on it", "remind them if nothing comes": op=follow_up with the recipient (to) or subject of that mail.
  - "Where am I still waiting for an answer?", "what about the thing I wanted to follow up on?": op=list.
  - "Pause the follow-up", "carry on with it": op=pause / op=resume with pipeline_id from op=list.
  A follow-up starts as a draft: Yorik writes the reminders and the person approves each one on the pipeline page before anything is sent.
when_not_to_use: |
  - A mail that was only prepared and not sent yet — it has to be sent first.
  - A one-off reminder for the user — that's remind_me.
inputs:
  op:
    type: string
    required: true
    description: follow_up | list | pause | resume
  to:
    type: string
    required: false
    description: Recipient address (or part of it) of the sent mail to follow up.
  subject:
    type: string
    required: false
    description: Subject (or part of it) of the sent mail, when the recipient is not known.
  mail_id:
    type: integer
    required: false
    description: The sent mail's id, if known.
  pipeline_id:
    type: integer
    required: false
    description: For pause/resume, from op=list.
outputs:
  pipeline_id:
    type: integer
  pipelines:
    type: array
    description: For op=list — title, state (entwurf/laeuft/pausiert/erledigt/abgebrochen), next step, link.
cost: 1–3 SELECTs; follow_up starts one background LLM call that writes the reminders.
permissions: [admin, member]
side_effects: follow_up creates a draft pipeline (nothing is sent before the person approves); pause/resume change its state.
tags: [pipelines, email, follow-up, app:pipelines]
category: communication
---

# pipeline

follow_up takes the newest mail the person sent in the last 14 days
that matches `to`/`subject`. Only the person's own pipelines are listed
or changed.
