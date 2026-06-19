---
name: list_compose_templates
description: List every Compose template as one compact line so you can pick candidates.
when_to_use: |
  Call FIRST on any Compose intent — letter, email, invoice, Brief, Kündigung, Rechnung. Read the
  compact list, pick the 1-3 best-fit ids by description, then call pick_compose_template with them
  in best-first order. If two siblings look equally plausible, call view_compose_template(template_id=X)
  on each to read the when_to_use bullets before deciding. Skip only when the user explicitly named
  a canonical template_id (e.g. by name) or you're iterating on an existing draft.
when_not_to_use: |
  - User explicitly named a known canonical template_id — call pick_compose_template directly with that id as the only candidate.
  - Iterating on an existing draft (Compose context line carries `draft_id=N` — the template is already locked).
  - Discovering what compose skills exist (use `list_skills` for that).
inputs: {}
outputs:
  _llm_hint:
    type: string
    description: |
      One line per template in the form
      `id(kind, tag1, tag2) — description`, sorted by (kind, name). Plus a
      short follow-up line telling you to pick 1-3 ids and call
      pick_compose_template, or view_compose_template first when sibling
      candidates need disambiguation.
permissions: [admin, member, restricted]
side_effects: none — read-only directory scan
cost: One template-directory scan (~1ms; templates load is cached at startup).
tags: [compose, templates, discovery]
---

# list_compose_templates

Returns every Compose template — bundled and community — as one compact
line each: `id(kind, tag1, tag2) — description`. Mirrors list_skills.

The full when_to_use / when_to_not_use bullets live in
view_compose_template; keeping them out of this call avoids the
truncation that hid most ids before.

## Operating rules

- Read the compact list, pick 1-3 ids whose description matches the user's intent.
- For ambiguous sibling candidates (e.g. two installed templates with overlapping descriptions — a domain-specific one vs a general one), call view_compose_template(template_id=X) on each before deciding.
- Use only ids from this output — do not invent ids.
- Hand the picks straight to pick_compose_template; do NOT enumerate them in chat prose.
