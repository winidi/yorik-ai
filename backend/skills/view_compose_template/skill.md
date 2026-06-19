---
name: view_compose_template
description: Read full prose for ONE Compose template by id — call to disambiguate siblings.
when_to_use: |
  After list_compose_templates when two or more candidate ids look plausible by description but you need the bullets to choose between them. Skip when intent already maps cleanly to a single id.
when_not_to_use: |
  - User already named one canonical template_id — call pick_compose_template directly with that id.
  - Iterating on an existing draft (Compose context line carries `draft_id=N` — template is locked).
  - You haven't called list_compose_templates yet this conversation — call that first to see real ids.
inputs:
  template_id:
    type: string
    required: true
    description: |
      The canonical id of one template, as returned by list_compose_templates.
      Unknown ids return an UNKNOWN_TEMPLATE_ID
      hint pointing back to list_compose_templates.
outputs:
  _llm_hint:
    type: string
    description: |
      Full template JSON (id, name, kind, description, tags, vertical,
      when_to_use, when_to_not_use) plus a short follow-up steering line.
permissions: [admin, member, restricted]
side_effects: none — read-only template lookup.
cost: One template-directory scan (~1ms; templates load is cached at startup).
tags: [compose, templates, view, discovery]
---

# view_compose_template

Reads the full author-written prose for one Compose template — the same
description + when_to_use / when_to_not_use bullets the template author
wrote — so the LLM can disambiguate sibling candidates before handing
ids to pick_compose_template.

## Operating rules

- Pass exactly one `template_id` per call; check two siblings with two calls.
- Use only ids that appear in a recent list_compose_templates output.
- After reading, either call pick_compose_template (with the confirmed id leading) or view a different sibling.
- Do NOT enumerate the prose back to the user in chat — it's grounding for your pick, not user-facing copy.
