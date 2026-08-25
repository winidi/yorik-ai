---
name: delete_compose_draft
description: "Delete a saved compose draft (letter, email, invoice, offer, memo) by draft_id."
when_to_use: |
  - User says "lösche den Entwurf" / "delete the draft" / "wirf den Brief weg" / "Entwurf weg".
  - User references a draft by id ("lösche Draft 28", "delete draft #14") or as "the one we just wrote" / "den letzten Brief".

  Single-draft only. If the user said "lösche alle Entwürfe" / "clear all drafts", refuse: list the recent drafts (id + subject + recipient), ask which ones to delete, then call this skill ONCE per draft across separate turns.

  The skill is owner-gated — a non-admin can only delete their own drafts. If the row's user_id doesn't match the caller, the skill raises and the LLM should pass that through verbatim.
when_not_to_use: |
  - User wants to EDIT the draft, not discard it → navigate_to(app="compose", query_params={"draft_id": N}) so they can edit it in the Compose UI.
  - User asks to delete a SENT email / posted letter — drafts and sent items are separate; this skill only touches compose_drafts.
inputs:
  draft_id:
    type: integer
    required: true
    description: The integer id of the compose draft to delete. Required — no fuzzy lookup by subject or recipient is performed.
outputs:
  pending:
    type: boolean
    description: Always true — the delete is staged, not executed. It runs only when the user taps Delete on the card.
  pending_id:
    type: string
  draft:
    type: object
    description: id, subject, recipient, kind of the draft that WOULD be deleted — quote the subject so the user can check the card.
side_effects: |
  - Stages ONE compose_drafts row for deletion and shows a confirmation card. Nothing is removed until the user taps Delete.
  - On Delete: removes the row; compose_draft_versions rows go with it (ON DELETE CASCADE). Not reversible.
  - Emits a `refresh_data` UI action after the delete so the chat / compose surface refetches.
cost: One DELETE in Postgres, after confirmation.
permissions: [admin, member]
tags: [compose, draft, delete]
---

# delete_compose_draft

Hard delete of one compose draft row, gated by the per-turn delete throttle (same `_deletes_this_turn` counter as delete_calendar_event / delete_contact). Confirm-before-apply: the skill stages the delete and shows a card; the row is removed only when the user taps Delete, and the version history goes with it.

Reply with ONE short sentence ("Die Karte unten wartet auf deine Bestätigung, bevor der Entwurf gelöscht wird.") — the pending_confirmation card carries the rest.
