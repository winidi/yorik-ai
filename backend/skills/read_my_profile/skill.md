---
name: read_my_profile
description: "Read the calling user's own profile — name, address, phone, email, business, IBAN, signature."
when_to_use: |
  When the user asks about themselves and you need fields the system
  prompt doesn't already carry. Triggers:
    "what's my address" / "wie ist meine adresse"
    "what's my IBAN"    / "wie ist meine kontonummer"
    "what's my phone"   / "wie ist meine nummer"
    "do I have a signature on file"
    "what's my business name"

  Also call BEFORE asking the user for their own address, phone, IBAN,
  or business name in any flow — the profile usually has it, so don't
  make the user repeat what Yorik already stores.
when_not_to_use: |
  - compose_draft auto-fills sender_name + sender_address from the
    profile server-side, so calling this before compose_draft is wasted
    work. Just call compose_draft.
  - For another household member's profile, use find_user.
  - For a contact (third party), use find_person or find_contact.
  - Do not call to "check who's logged in" — the system prompt already
    names the logged-in user on every turn.
inputs: {}
outputs:
  id:               { type: integer, description: The user's id }
  name:             { type: string }
  first_name:       { type: string }
  last_name:        { type: string }
  email:            { type: string }
  phone:            { type: string }
  address_street:   { type: string }
  address_postcode: { type: string }
  address_city:     { type: string }
  country:          { type: string, description: ISO-2 country code }
  business_name:    { type: string, description: Sole-trader or company name if set }
  tax_id:           { type: string }
  iban:             { type: string }
  has_signature:    { type: boolean, description: True if a signature image is on file }
permissions: [admin, member, restricted]
side_effects: none — read only.
cost: One SQLite SELECT on user_profiles.
tags: [user, profile, identity, read]
---

# read_my_profile

Yorik already stores the logged-in user's name, address, phone, email,
and business details. This skill surfaces them so the LLM can answer
"what's my address" without asking, and so it never asks the user for
data the system already has.

## Operating rules

- Reads `ctx.user_id` — never accepts a user id argument; you cannot
  read someone else's profile through this skill.
- Returns NULL for fields the profile doesn't have set yet; treat NULL
  as "not on file" and never fabricate a placeholder.
- Secrets (password hash, paperless token, immich api key, voice
  embedding) are intentionally not returned.
