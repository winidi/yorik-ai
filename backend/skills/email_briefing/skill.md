---
name: email_briefing
description: Natural-language summary of recent email activity across all of the user's accounts
when_to_use: |
  - User asks "what's new in my email?" / "any important emails today?"
  - Morning routine: combined with whatsapp_briefing for a full inbox overview
  - Before composing a reply, agent wants a quick read of the email landscape
  - For finding a SPECIFIC email by sender or subject phrase, call `find_email_by_subject` first — this skill is for broad recent-activity summaries only.
inputs:
  hours:
    type: integer
    required: false
    default: 24
    description: Look-back window in hours (1-168)
outputs:
  summary:
    type: string
    description: Markdown briefing — "Action items" + "By thread" sections, auto-language matched to the inbox content
  stats:
    type: object
    description: "Object with keys hours / accounts / new_messages / threads_needing_reply / unread_count"
  threads_needing_reply:
    type: array
    description: "List of {message_id, thread_id, from, subject} for messages the user hasn't replied to"
  generated_at:
    type: string
cost: 1 LLM call (~3-8s on qwen3)
permissions: [admin, member]
side_effects: none — read-only
tags: [email, summary, llm]
---

# email_briefing

Aggregates recent inbound email from all of the user's accounts grouped
by sender/thread, identifies which ones plausibly need a reply (you
got an email and haven't sent one in the same thread yet), and asks
the LLM for a prioritised briefing.

Auto-language: prompt tells the LLM to write in German if most messages
are German, English otherwise.

Naturally pairs with whatsapp_briefing — a future "morning_briefing"
composite skill calls both and stitches them.

`summary` is user-ready markdown — surface it verbatim or with one framing sentence, and never re-summarise it or enumerate `threads_needing_reply` in prose.
