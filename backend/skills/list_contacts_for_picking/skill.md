---
name: list_contacts_for_picking
description: "List contacts for the user to pick from."
when_to_use: |
  Trigger A — BROWSE: user says "zeig mir / show me / list / alle Kontakte" with or without filter; emits the alphabetical contacts_found card.

  Trigger B — RELATIONAL DESCRIPTOR: user used a relational descriptor ("Oma", "der Klempner", "mein Steuerberater"); run the two-call ranking flow regardless of any prior find_person result.

  Two-call ranking flow:
    1. list_contacts_for_picking(defer_card=true) — full address book, no card.
    2. list_contacts_for_picking(query="<descriptor>", ranked_picks=[{id, confidence, reason}, ...]) — top 10 ordered by confidence DESC, emits the ranked picker card.

  Score each pick 0.0..1.0: 0.95+ for unambiguous relation/alias matches, ≤0.30 for long shots.

  Aim for 10 picks; shorter lists auto-pad to 5 rows with neutral recent contacts that have no relevance signal.

  Do NOT use for a specific known name ("schreibe an Lilian Wende") — that's find_person.

  Do NOT enumerate names in your chat reply after a card emits — the card carries them.

inputs:
  pick_to_chat:
    type: boolean
    required: false
    default: true
    description: "Default true (write/draft intent → click routes back to chat). Set false only for explicit browse ('zeig mir alle Kontakte') so a click navigates to /contacts."
  status:
    type: string
    required: false
    default: any
    description: "'any' (default — active + pending), 'active', 'pending', 'spam', 'archived'. Spam and archived excluded from 'any' by design."
  kind:
    type: string
    required: false
    description: "Filter to 'person' or 'business'. Omit for both."
  defer_card:
    type: boolean
    required: false
    default: false
    description: "Suppress the alphabetical contacts_found card in the fallback flow's first call."
  ranked_picks:
    type: array
    required: false
    description: "Top 10 picks ordered by confidence DESC, each {id, confidence (0..1), reason}; emits the ranked picker card and skips the SQL list."
  query:
    type: string
    required: false
    description: "User's original phrase for the ranked card header; does NOT filter the SQL list."
outputs:
  count:
    type: integer
    description: "Browse/fallback-call-1 only: number of contacts in the list."
  contacts:
    type: array
    description: "Browse/fallback-call-1 only: each entry {id, display_name, relation, kind}. NO channels, NO addresses — keep tight."
  rendered:
    type: integer
    description: "Render-mode (ranked_picks) only: picks actually rendered (ids the caller can't see are dropped)."
  dropped:
    type: array
    description: "Render-mode only: contact_ids the caller can't see — re-fetch with defer_card=true if non-empty."
permissions: [admin, member, restricted]
side_effects: "browse → contacts_found card; ranked_picks → contact_picker card with ranked=true"
tags: [contacts, lookup, fallback]
---

# list_contacts_for_picking

Compact full-address-book dump AND the renderer for LLM-ranked picks. One skill, two call shapes — keeps the skill_index lean (one entry instead of two for the same fallback flow).

Browse path: ~30 chars/row × N contacts. At household scale (≤1000) this is ~30kb, fits qwen3's context window.

Fallback ranking path: the user phrase "Oma" can't be resolved by SQL because relation='Großmutter' isn't a name match. Call 1 returns the whole list; you score with NL knowledge (Oma↔Großmutter↔grandma, Klempner↔plumber, nicknames, trade tags); call 2 renders the curated top 10 with confidence pills + reason subtitles. Be honest with confidence — the user trusts the ordering.
