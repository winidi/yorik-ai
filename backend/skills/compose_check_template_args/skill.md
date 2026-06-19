---
name: compose_check_template_args
description: "Detect unfilled template arg slots and ask via inline form, between recipient and draft."
when_to_use: |
  After compose_check_recipient returns complete=true (or you've collected
  the recipient address via the form), call this skill with the args you
  have so far. It scans the template's body_html for unfilled slots and:
    - Auto-fills what it can from the user's profile (ort, sender_*, today)
    - Auto-fills from the contact (recipient_*, vermieter_*, …)
    - For everything else that's still empty: emits a needs_input form
      asking the user. Templates can override the field labels via the
      template-level `ask_user_for_args` metadata.

  Flow:
    compose_check_recipient → complete=true
      ↓
    compose_check_template_args(template_id=…, contact_id=…, args={user-collected so far})
      ↓
      complete=true   → call compose_draft now
      complete=false  → user sees a form; on submit, the playbook resumes
                        compose_draft with the filled args

  Without this skill, the LLM either (a) leaves slots empty and the
  template renders awkwardly ("vom  ordentlich" with empty
  mietvertrag_vom), or (b) makes up values to fill them. Neither is OK.

  Skill is read-only and cheap (single template load + a regex scan).
inputs:
  template_id:
    type: string
    required: true
    description: Active template id (from the Compose context line).
  contact_id:
    type: integer
    required: false
    description: Contact id if a recipient is resolved. Used to skip asking about recipient_* / vermieter_* / anbieter_* slots that compose_draft will fill.
  args:
    type: object
    required: false
    description: |
      Args the LLM has already collected from the user message ("zum
      17.08" → {beendigung_zum: "17.08.2026"}). Slots present here
      are considered filled and won't be asked about.
  existing_draft_id:
    type: integer
    required: false
    description: |
      When the user is iterating on an existing draft (the Compose
      context line carries `draft_id=N`), pass that here so the skill
      can pre-load any args the prior compose_draft call persisted
      (notably body_text). Without this the form re-asks for fields
      already filled in a previous turn.
outputs:
  complete:
    type: boolean
    description: True when no field needs to be asked about.
  missing:
    type: array
    description: When complete=false, list of {key, label, required} descriptors that the form is asking about.
permissions: [admin, member]
side_effects: none — read only. Emits needs_input ui_action when complete=false.
tags: [compose, templates]
---

# compose_check_template_args

Closes the "the LLM made up the Mietvertrag-Datum" gap. After we've
resolved the recipient (compose_check_recipient) and any structured
args the LLM extracted from the user's message, this skill is the
LAST chance to surface "still need: X, Y, Z" BEFORE the draft renders
with empty slots.

## What counts as "missing"

A template arg `{{ args.X }}` is asked about when ALL of these are true:

  1. The body_html references it (regex scan).
  2. Args don't carry a value for it.
  3. We can't auto-fill it from the contact or profile.
  4. The template's `default_args[X]` is empty (a non-empty default means
     the template author considers this a sensible starting value).

Auto-fill heuristic (no per-template config needed):

  - Recipient / vermieter / anbieter / locatore / sprzedawca name+address →
    auto-filled by compose_draft when contact_id is set, so skip.
  - Sender / absender / mieter / konduttore / konsument / wierzyciel
    name+address → auto-filled from user_profiles, skip.
  - `ort` → auto-filled from user.address_city.
  - `signature_image_url` / `unterschrift_url` → auto-filled from profile.
  - Everything else → ask the user.

## Per-template polish

Templates can declare `ask_user_for_args` to override the auto-detected
list with prettier labels and required/optional flags:

```json
"ask_user_for_args": [
  {"key":"mietvertrag_vom","label":"Wann wurde der Mietvertrag unterschrieben? (z.B. 15.06.2022)","required":false},
  {"key":"wohnung_adresse","label":"Welche Wohnung kündigst du? (Straße, Etage, PLZ Ort)","required":true},
  {"key":"neue_adresse","label":"Adresse für die Endabrechnung (optional)","required":false}
]
```

If `ask_user_for_args` is set, the skill uses ONLY those fields (filtered
to the still-missing ones). Templates without the override get the
auto-detected list with humanized labels ("mietvertrag_vom" → "Mietvertrag
vom"). Either way the user gets a form, not a wall of prose questions.
