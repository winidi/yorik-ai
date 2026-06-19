---
name: find_email_by_subject
description: Look up email IDs by subject — helper for the next email_draft / read step.
when_to_use: |
  When the user names a SPECIFIC email by subject or sender phrase
  ("reply to the mail.de support thread", "what did the bank say",
  "open the smoke test email") and you need the `message_id` for
  email_draft / read_email / similar follow-ups.

  Yorik has no email-scoped search skill that returns rows to the
  LLM — `email_briefing` summarises and `universal_search` is
  semantic-everything (often misses keyword subject matches). This
  is the targeted resolver.

  Default `include_sent=true` so "the smoke test email I sent" finds
  outbound too.

  After this returns:
    - 0 hits → tell the user, ask them to clarify the subject.
    - 1 hit  → use message_id directly in the follow-up call.
    - 2+ hits → ask the user which one (quote subject + from + date).
inputs:
  query:
    type: string
    required: true
    description: Substring or word fragment from the subject (or from-address). Case-insensitive. Multi-word queries match if every token appears.
  include_sent:
    type: boolean
    required: false
    default: true
    description: Include emails the user sent themselves. Default true.
  days_back:
    type: integer
    required: false
    default: 90
    description: How far back to search.
  limit:
    type: integer
    required: false
    default: 10
outputs:
  matches:
    type: array
    description: Each row has id, from_email, subject, date_received, is_sent. Quote only for disambiguation.
  count:
    type: integer
side_effects: None.
cost: One indexed SQLite LIKE query.
permissions: ["*"]
tags: [email, resolver, internal]
---

# find_email_by_subject

Subject/sender resolver for the email_messages table. LLM-internal —
no cards. Use BEFORE email_draft when the user names an email by
subject phrase rather than passing a message_id.
