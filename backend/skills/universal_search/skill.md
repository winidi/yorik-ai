---
name: universal_search
description: "Search everything the user may see in one query, by keyword and by meaning."
when_to_use: |
  - User asks "find anything about X" without specifying a source
  - User mentions a person/topic that could be in any channel
  - The chat agent needs to gather context from multiple places before drafting
  - "What did Müller say?" / "Have I seen this invoice before?" / "Show me everything from last summer"
inputs:
  query:
    type: string
    required: true
    description: Free-text search, a few words. Every source matches by keyword first and then by meaning, so a paraphrase works too.
outputs:
  query:
    type: string
  total:
    type: integer
    description: Total hits across all sources
  results:
    type: object
    description: "Object with keys email / whatsapp / paperless / immich / calendar / tasks / contacts / recordings / drafts, each an array of {source, id, title, subtitle, snippet, timestamp, navigate_to}"
cost: 9 parallel queries (~500ms p50). Immich CLIP is the slowest source and can hit a 4s deadline.
permissions: [admin, member, restricted]
side_effects: none — read-only
tags: [search, cross-channel, rag]
---

# universal_search

Fan-out search across everything the user may see. Visibility is the
app's own rule (owner, spaces, shares); nobody finds another person's
private rows, admins included.

- email, whatsapp: full-text index, then the semantic index
- calendar, tasks, contacts, recordings (title, report, transcript),
  drafts: every word of the query, then the semantic index
- paperless: semantic search over the filed documents
- immich: CLIP content search via the Immich API

Each result is normalised into a uniform shape including a
`navigate_to` URL the UI can deep-link to. Capped at 5 per source.

Owner-scoped throughout — only the calling user's own data.

The UI renders cards from the result set — do NOT paraphrase hits in prose, and if no source has a clearly-related hit, say nothing was found instead of presenting weak matches as answers.
