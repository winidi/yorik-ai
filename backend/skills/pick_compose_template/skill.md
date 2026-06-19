---
name: pick_compose_template
description: Shows the user a template picker card with the 3 candidate templates you picked.
when_to_use: |
  Call AFTER list_compose_templates with the 3 best-fit template_ids you chose by reading their
  prose, in best-first order. The user clicks one; you'll see a `[template_picked id=X]` reply.
  Wait for that reply, then call compose_draft with the chosen id. Do NOT enumerate the candidates
  in prose — the card carries them.
when_not_to_use: |
  - You haven't read the template list yet — call list_compose_templates first.
  - Iterating on an existing draft (Compose context line carries `draft_id=N` — template is locked).
  - The user already named the exact template — pass a single-element candidates list so the picker still confirms but skips ambiguity.
inputs:
  candidates:
    type: array
    items: { type: string }
    required: true
    description: |
      1-5 template_ids in best-first order, as returned by list_compose_templates. First
      element is your top pick; order carries the ranking. Each id must exist in the
      list_compose_templates output — unknown ids are silently dropped (the picker still
      renders with the surviving candidates).
outputs:
  ok:
    type: boolean
    description: True once the picker card is emitted with at least one valid candidate.
  rendered:
    type: array
    description: The candidate ids that actually rendered on the card (after dedupe + unknown-id filtering).
  unknown:
    type: array
    description: Candidate ids passed in but not found in the template directory — log only, not user-visible.
permissions: [admin, member, restricted]
side_effects: |
  Emits a `template_picker` ui_action that the chat renders inline. The card carries
  the candidates' name, description, kind, and when_to_use bullets. User click →
  `[template_picked id=X]` follow-up message routes the chosen template back to the LLM.
cost: One template-directory lookup. Sub-millisecond.
tags: [compose, templates, picker]
---

# pick_compose_template

Renders a template picker card with the candidate template_ids you pass.
The user clicks one and you receive a `[template_picked id=X]` follow-up
message; resume the request with compose_check_recipient + compose_draft
using that id.

## Operating rules

- Pass `candidates` in best-first order — position carries the ranking.
- 1-5 candidates; 3 is the standard ambiguous-intent count.
- Each id must come from a recent list_compose_templates call.
- After the card emits, reply ONE short sentence asking the user to pick; do NOT list templates in prose.
- Wait for `[template_picked id=…]` before calling compose_draft.
