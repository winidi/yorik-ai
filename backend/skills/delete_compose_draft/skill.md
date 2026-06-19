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
  deleted_draft_id:
    type: integer
    description: The id of the row that was deleted.
  draft:
    type: object
    description: Snapshot of the deleted row (subject, recipient, kind, template_id, body_html, args_json) — surfaced so the LLM can quote what was removed.
side_effects: |
  - Removes ONE row from compose_drafts.
  - Cascade-removes any rows in compose_draft_versions (ON DELETE CASCADE in the schema). Version history is NOT restored on rollback; the body content IS.
  - Emits a `refresh_data` UI action so the chat / compose surface refetches.
  - Stages a rollback (`restore_compose_draft`) via pending_actions so the chat's confirm card can revert.
cost: One DELETE in sqlite.
permissions: [admin, member]
tags: [compose, draft, delete]
---

# delete_compose_draft

Hard delete of one compose draft row, gated by the per-turn delete throttle (same `_deletes_this_turn` counter as delete_calendar_event / delete_contact). Apply-then-confirm: the row is removed immediately so the user sees the draft disappear; the rollback handler can re-insert it with the same id if the user cancels.

The version history (compose_draft_versions) is cascade-deleted by the schema and is NOT recreated on rollback. The user gets the most recent body back; refine/restore history is gone. Acceptable for an undo — the audit found no user expecting version history to survive a delete.

Reply with ONE short sentence ("Der Entwurf wurde gelöscht — die Karte unten zeigt Rückgängig.") — the pending_confirmation card carries the rest.
