---
name: universal_search
description: "Search across email, WhatsApp, Paperless docs, Immich photos, and calendar in one query."
when_to_use: |
  - User asks "find anything about X" without specifying a source
  - User mentions a person/topic that could be in any channel
  - The chat agent needs to gather context from multiple places before drafting
  - "What did Müller say?" / "Have I seen this invoice before?" / "Show me everything from last summer"
inputs:
  query:
    type: string
    required: true
    description: Free-text search. Tokens AND-required within email/calendar; OR-style for WhatsApp; semantic for Paperless/Immich.
outputs:
  query:
    type: string
  total:
    type: integer
    description: Total hits across all sources
  results:
    type: object
    description: "Object with keys email / whatsapp / paperless / immich / calendar, each an array of {source, id, title, subtitle, snippet, timestamp, navigate_to}"
cost: 5 parallel queries (~500ms p50). Immich CLIP is the slowest source and can hit a 4s deadline.
permissions: [admin, member, restricted]
side_effects: none — read-only
tags: [search, cross-channel, rag]
---

# universal_search

Fan-out search across every local channel the user has. Each source
uses its native search primitive:

- email: FTS5 across subject + sender + snippet + body
- whatsapp: FTS5 across messages + voice transcripts
- paperless: semantic search (nomic-embed-text + sqlite-vec)
- immich: CLIP content search via the Immich API
- calendar: LIKE on title/notes/person

Each result is normalised into a uniform shape including a
`navigate_to` URL the UI can deep-link to. Capped at 5 per source.

Owner-scoped throughout — only the calling user's own data.

The UI renders cards from the result set — do NOT paraphrase hits in prose, and if no source has a clearly-related hit, say nothing was found instead of presenting weak matches as answers.
