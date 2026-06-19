---
name: email_draft
description: Generate one or more reply drafts for an email thread
when_to_use: |
  - User opens an email and asks "what should I reply?"
  - Auto-draft on incoming (debounced) so when the user opens the message, drafts are already waiting
  - Composite "send_with_…" skills that need a body before sending
  - If you don't have message_id, call find_email_by_subject first.
inputs:
  message_id:
    type: integer
    required: true
    description: The email_messages.id to draft a reply to
  extra_instructions:
    type: string
    required: false
    description: Tone or intent nudge from the user ("formal", "in German", "say no politely")
  variants:
    type: integer
    required: false
    default: 3
    description: 1 (single draft) or 3 (brief / warm / detailed angles)
outputs:
  drafts:
    type: array
    description: One per variant — {label, text}
  sources:
    type: array
    description: Context snippets injected (thread history, calendar, cross-channel hits)
cost: 1 LLM call (~3-6s on qwen3, the same call returns all variants)
permissions: [admin, member]
side_effects: none — drafts are returned, persistence is the caller's choice
tags: [email, drafting, llm]
---

# email_draft

Pulls the whole thread (oldest → newest), checks the user's upcoming
calendar (for scheduling-relevant messages), runs FTS5 + semantic
search across both emails AND WhatsApp for related context, asks
qwen3 for 3 distinct angle drafts in a single call.

Same prompt template as whatsapp_autodraft so the user gets consistent
output across both inboxes — different sources, same tone calibration.
