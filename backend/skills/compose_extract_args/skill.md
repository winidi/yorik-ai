---
name: compose_extract_args
description: "Extract Compose template arg values from a freeform text blob (paste, OCR, web snippet)."
when_to_use: |
  The user pasted unstructured text into the Compose args panel (or
  uploaded a document whose text has been OCR'd) and we need to map it
  into the template's structured arg slots. The text can be noisier than
  what fits — extra paragraphs, marketing copy, multiple addresses; the
  skill keeps only the values it can confidently identify for the
  target args.

  Two modes:
    - Single-recipient template OR omit `contact_group` → extract values
      for all args declared in ask_user_for_args.
    - Multi-recipient template with `contact_group` → extract ONLY for
      args declaring that contact_group. Other groups untouched.

  Returns `{args: {key: value, ...}}` — pass this dict straight to
  compose_draft as the `args` parameter (or merge with existing draft args).
when_not_to_use: |
  - Reading the active draft's current args (compose_draft returns those).
  - Discovering which arg names a template has (compose_check_template_args).
  - Pulling addresses from Paperless documents the user already filed —
    `find_recipient_address_from_documents` is the contact-id-driven path.
inputs:
  text:
    type: string
    required: true
    description: |
      Freeform text. Can be the user's typed paste, the OCR text of an
      uploaded document, or any blob containing the values to extract.
      Noise / extra content is fine; the prompt instructs the LLM to
      ignore anything it can't confidently map.
  template_id:
    type: string
    required: true
    description: |
      Canonical template id — call list_compose_templates first if you
      don't know it. Determines which arg names are valid targets.
  contact_group:
    type: string
    required: false
    description: |
      When set, restrict extraction to args declaring this contact_group
      (e.g. "vermieter", "arbeitgeber", "company"). Omit for single-
      recipient templates or to extract everything at once.
outputs:
  args:
    type: object
    description: |
      Map of arg key → extracted value. Only confidently-matched fields
      appear (the LLM doesn't invent values for fields not present in the
      text). Pass to compose_draft.args.
  confidence:
    type: object
    description: |
      Per-key confidence note ("explicit label", "inferred from format",
      "guessed", …) for the UI to surface a "verify" hint if the value
      was inferred rather than labelled in the source text.
side_effects: One LLM call (~1-3s on local Qwen). No DB writes, no UI emissions.
permissions: [admin, member]
tags: [compose, extraction, templates]
---

# compose_extract_args

The "paste anything and we'll figure out what goes where" skill. Powers
the per-slot paste textarea + upload-doc button in the Compose args
panel. The user dumps text from wherever (a copied letter, a snipped
website paragraph, an OCR'd PDF) and we fill the right structured slots
without making them re-type.

Confidence policy: when the source text contains a labelled value
("Vermieter: Hans Müller"), that maps directly to `vermieter_name`.
When the value is inferred from format (a postal address block with no
explicit label), the skill returns it with confidence="inferred" so
the UI can flag it for verification. Anything the LLM can't confidently
identify is omitted — the user can always fill manually.
