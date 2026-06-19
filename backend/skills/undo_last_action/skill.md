---
name: undo_last_action
description: Roll back the most recent mutation (delete / add / update) made by Yorik this session.
when_not_to_use: |
  NEVER trigger on bare "wieder" or "nochmal" — those are German discourse particles, not undo signals. "lösche X UND Y wieder bitte" means "delete X AND Y too, please" — the user wants Y deleted, NOT the previous action undone. Same for "schreib das wieder so" / "mach das wieder kurz" — "wieder" here is a softener.

  Only trigger when the user explicitly signals reversal: "rückgängig", "stell wieder her", "wiederherstellen", "doch nicht", "war falsch", "undo", "revert", "restore". If the message ALSO names a new action ("lösche … wieder"), do the new action; do NOT add a free undo_last_action call alongside it.
when_to_use: |
  Trigger this when the user signals that the previous mutation was wrong
  and should be reverted. Typical phrasings (German + English):

    - "das war falsch", "mach das rückgängig", "stell das wieder her"
    - "no, undo that", "wait, restore that", "I didn't mean that"
    - "rückgängig", "undo", "revert"

  Looks up the most recent pending_actions row for the current user
  (last 60 minutes), runs its registered rollback (re-inserting deleted
  rows, restoring pre-update field values, removing freshly-added rows),
  and returns a summary so the LLM can tell the user what was reverted.

  If nothing recent is undoable, the skill returns an empty result and
  the LLM should tell the user "there's nothing recent to undo".

  IMPORTANT: this undoes exactly ONE pending action — the most recent.
  If the user wants to undo multiple things, call this skill multiple
  times in separate turns (do not loop within one /api/ask — let the
  user confirm between each rollback so they don't accidentally roll
  back too far).
inputs: {}
outputs:
  undone:
    type: string
    description: The rollback_kind that was applied (e.g. "restore_event", "delete_event"). Empty if nothing to undo.
  skill:
    type: string
    description: The skill whose action was rolled back. Empty if nothing to undo.
  preview:
    type: object
    description: The original preview shown when the action was staged — useful for telling the user what was undone.
  age_seconds:
    type: number
    description: How old the action was when undone.
permissions: [admin, member, restricted]
tags: [system, undo, rollback]
---

# undo_last_action

Wraps `pending_actions.rollback()` for voice / chat-initiated undo. Same
machinery as the "Cancel" button in the confirmation modal, but
triggered by phrase instead of click. Limits itself to actions less
than 60 minutes old so a stray "undo" hours later doesn't surprise the
user by reverting something they had forgotten about.

Uses ctx.user_id for the lookup, so each user can only undo their own
actions (matches the per-user permission model on /api/pending/{id}/cancel).
