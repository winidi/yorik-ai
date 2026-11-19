---
name: send_prepared_email
description: "Send the email prepare_email staged, once the user said to send it (outside agents only)."
when_to_use: |
  The user told you to send the email you prepared with prepare_email: they pressed Send on its card, or said "send it" / "ja, senden".
  Pass `to` as the recipient you prepared it for; a draft staged for someone else is not sent.
  Report what this returns: sent, or why not.
when_not_to_use: |
  - The user has not said to send it yet: prepare_email shows the card, then wait.
  - Inside Yorik's own chat the card's own button sends; this skill refuses there.
inputs:
  to:
    type: string
    required: true
    description: The recipient you prepared the email for, as in prepare_email.
outputs:
  sent:
    type: boolean
  to:
    type: string
  subject:
    type: string
permissions: [admin, member, restricted]
side_effects: Sends one email over SMTP from the user's own account, then clears the staged draft and marks its card sent.
tags: [email, write, send]
category: productivity
---

# send_prepared_email

Sends exactly the draft prepare_email staged for this person
(app_settings pending_email_draft_<user_id>): recipient, subject, body,
account and attachments as the card showed them. Attachments are read
as the person (send_attachments.fetch_bytes). One call sends once; the
staged draft is cleared afterwards, so a second call finds nothing.

Only for agents that reach Yorik over MCP (ctx.source "token:…"): a
client such as Brepper shows the email_ready card with a Send button
whose press tells the agent to call this. Yorik's own chat sends from
the Composer instead.
