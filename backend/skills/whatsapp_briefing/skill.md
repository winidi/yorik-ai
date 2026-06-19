---
name: whatsapp_briefing
description: Generate a natural-language briefing of recent WhatsApp activity (action items + per-chat summary)
when_to_use: |
  - User asks "what's new on WhatsApp?" / "summarise my inbox"
  - Morning routine: user opens Yorik and wants to know what to act on
  - Before composing a reply, agent wants a quick read of the chat landscape
inputs:
  hours:
    type: integer
    required: false
    default: 24
    description: Look-back window in hours (1-168)
outputs:
  summary:
    type: string
    description: Markdown briefing — "Action items" section + "By conversation" section. Auto-language (German if inbox is mostly German, else English).
  stats:
    type: object
    description: "Object with keys hours / chats_with_new_msgs / chats_with_pending_drafts / media_auto_filed"
  chats_needing_reply:
    type: array
    description: "List of {jid, name, msg_count} — chats that have unanswered incoming messages"
  generated_at:
    type: string
    description: ISO timestamp of when the briefing was generated
cost: 1 LLM call (~3-8s on qwen3, depends on inbox size)
permissions: [admin, member]
side_effects: none — read-only
tags: [whatsapp, summary, llm]
---

# whatsapp_briefing

Aggregates incoming WA messages from the last N hours grouped by chat,
counts pending drafts + auto-filed media, hands it all to qwen3 with
a structured "Action items / By conversation" prompt.

Language detection: the prompt tells the LLM to write in German if
most of the source messages are German, English otherwise. No explicit
language parameter needed — derived from the inbox content.

`summary` is user-ready markdown — surface it verbatim or with one framing sentence, and never re-summarise it or enumerate `chats_needing_reply` in prose.
