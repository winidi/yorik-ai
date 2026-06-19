---
name: find_person
description: "Look up a person by name; pick source='household' for user_id, 'contacts' for chat/email/postal."
when_to_use: |
  Use this for ANY "find X" / "wer ist X?" / "termin mit X" / "schreib X" intent.
  Choose `source` by what you'll DO with the result, not by who you think the
  person is. The model used to flip between find_user and find_contact and
  get it wrong; one tool with an enum removes the choice.

  ┌───────────────────────────────┬────────────────────┬─────────────────────────────────┐
  │ Your next action              │ source             │ What you get back               │
  ├───────────────────────────────┼────────────────────┼─────────────────────────────────┤
  │ add_calendar_event with       │ "household"        │ user_id — pass as                │
  │   attendee_user_ids / RSVPs   │                    │   attendee_user_ids=[id]        │
  │                               │                    │                                 │
  │ whatsapp_draft, email_draft,  │ "contacts"         │ chat_jid, email, postal, +      │
  │ compose_draft (letters)       │                    │   contact_id (NOT a user_id)    │
  │                               │                    │                                 │
  │ Don't know yet / user just    │ "auto" (default)   │ both household + contacts;      │
  │ asked "wer ist X?"            │                    │   each row tagged with          │
  │                               │                    │   `source` so you pick at       │
  │                               │                    │   quote-time                    │
  └───────────────────────────────┴────────────────────┴─────────────────────────────────┘

  A `contact_id` is NEVER a valid `attendee_user_ids` value — different tables, overlapping integer ranges. Always read the `source` field on the row before reusing `id`.

  If multiple candidates matched, STOP and ask the user. Don't pick the first one. Contact results auto-render a picker card; household results — list them with role + name and ask.

  AMBIGUOUS RELATIONAL DESCRIPTOR ("my friend", "der Klempner", "Oma"): do NOT ask for a name in prose.
    1. Try find_person(query="<descriptor>", source="contacts") — matches the relation field.
    2. If 0 hits, run list_contacts_for_picking's two-call ranking flow (defer_card=true → ranked_picks=[…]).
    3. Aim for 10 picks; shorter lists pad with neutral recent contacts.
inputs:
  query:
    type: string
    required: false
    description: |
      Free-text search. Matches against name / first_name / last_name /
      email (and contact aliases when source includes contacts). Omit
      to list everyone in the chosen source (typically only useful for
      source='household' to enumerate logged-in users).
  source:
    type: string
    required: false
    default: auto
    description: |
      Where to look: 'household' (user_profiles, returns user_id for
      attendees), 'contacts' (address book, returns chat_jid/email/
      postal for messaging), 'auto' (both, with per-row source field).
  kind:
    type: string
    required: false
    description: |
      Contacts-only filter — 'person' or 'business'. Ignored for
      household lookups.
  role:
    type: string
    required: false
    description: |
      Household-only filter — 'admin' | 'member' | 'child' | 'employee'
      | 'viewer'. Ignored for contact lookups.
  status:
    type: string
    required: false
    default: any
    description: |
      Contacts-only — 'active' | 'pending' | 'spam' | 'archived' | 'any'.
      Default 'any' returns active + pending, skips spam + archived.
  channel_kind:
    type: string
    required: false
    description: |
      Contacts-only channel lookup. Pair with channel_value for an
      indexed (kind, value) hit — e.g. channel_kind='phone',
      channel_value='+493012345'.
  channel_value:
    type: string
    required: false
  limit:
    type: integer
    required: false
    default: 10
outputs:
  results:
    type: array
    description: |
      Each row has `source` ('household'|'contacts') and its natural fields.
      Household: {source, id (=user_id), name, first_name, last_name, role, email}.
      Contacts:  {source, id (=contact_id), display_name, relation, kind, channels[], addresses[]}.
      Read `source` BEFORE using `id` anywhere — the two id spaces overlap.
  count:
    type: integer
  ambiguous:
    type: boolean
    description: True if multiple candidates matched (calls for user disambiguation).
cost: 1-2 indexed SELECTs depending on source
permissions: [admin, member, restricted]
side_effects: |
  May emit a contact_picker UI card when contact results are ambiguous
  (existing behavior inherited from find_contact). No DB writes.
tags: [users, contacts, lookup, read]
---

# find_person

Single lookup tool, two sources, one decision point: pick `source` by the
action you'll take with the result.

Replaced the old `find_user` + `find_contact` pair (since removed from the
registry). The whole reason for the merge was to dissolve the model's
"which of two near-identical lookup tools?" choice into the much easier
"which `source` enum?" choice.

## Common cases

  - "termin mit anna" → `source='household'`, get `user_id`, pass as
    `attendee_user_ids` to add_calendar_event so Anna gets a real
    RSVP notification.
  - "schreib oma einen brief" → `source='contacts'`, get the postal
    address, hand off to compose_check_recipient → compose_draft.
  - "wer ist anna?" → `source='auto'`, see if she's a household user
    AND/OR a contact, answer the user based on what's there.
