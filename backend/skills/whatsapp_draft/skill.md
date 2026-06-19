---
name: whatsapp_draft
description: Generate one or more reply drafts for a WhatsApp conversation
when_to_use: |
  - The user receives a WhatsApp message and asks "what should I reply?"
  - The user clicks the manual "Draft" or "Regenerate" button in the WhatsApp UI
  - Another skill (e.g. whatsapp_send_with_photo) needs a body text generated
    in context before sending
  - The user wants to start a new WhatsApp conversation with someone whose number Yorik already has (no prior thread). Pass `contact_id` + `intent` instead of `chat_jid`.
inputs:
  chat_jid:
    type: string
    required: false
    description: WhatsApp chat identifier of an EXISTING thread (e.g. "4915123456789@s.whatsapp.net" or "...@g.us"). Pass this for replies. Omit for new conversations and pass contact_id + intent instead.
  contact_id:
    type: integer
    required: false
    description: Contact id from find_person — used to start a NEW WhatsApp conversation. The skill derives the JID from the contact's saved WhatsApp channel. Mutually exclusive with chat_jid for the initiate path.
  intent:
    type: string
    required: false
    description: One-line description of what to say (e.g. "ask Marie if Friday 3pm coffee at Café Klatsch works"). Required when starting a new conversation since there's no thread history to riff off of.
  extra_instructions:
    type: string
    required: false
    description: Tone or intent nudge from the user ("keep it formal", "in German", "be brief")
  variants:
    type: integer
    required: false
    default: 1
    description: Number of distinct draft variants to produce (1 or 3). 3 returns brief/warm/detailed angles in a single LLM call.
outputs:
  drafts:
    type: array
    description: One element per variant — {label, text, sources}
  sources:
    type: array
    description: Context snippets the LLM saw (other chats, paperless docs, calendar events). Same for every variant.
cost: 1 LLM call (~3-5s on qwen3 for 1 variant, similar for 3 variants because it's one call)
permissions: [admin, member]
side_effects: |
  Writes generated drafts to wa_drafts table with status='pending' so
  the UI can render them and the user can pick one to send.
tags: [whatsapp, drafting, llm]
---

# whatsapp_draft

Pulls the last 20 messages from the chat, runs three retrieval channels
over the most-recent inbound message — FTS5 keyword search across all
other WhatsApp chats, semantic search via wa_vec (sqlite-vec), and
Paperless document search via paperless_ingest.search — fuses the
results round-robin (cap 6 sources), and adds the user's next 7 days
of calendar events as a separate "availability" section.

The LLM prompt enforces:
- Match the language of the last incoming message
- Match the conversation's tone
- No greetings mid-thread
- Reference scheduling concretely when calendar-relevant
- State facts only if they appear in the messages or sources

When `variants > 1`, asks for distinct angles in a single call with
"---DRAFT N---" delimiters, parsed back into a list.

Drafts are stored in wa_drafts with a shared variant_group_id so
later actions (send, discard, regenerate) can address the whole set
atomically.
