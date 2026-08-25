"""delete_calendar_event skill — stage ONE event for deletion.

Confirm-before-apply: nothing is deleted when this returns. The skill
validates, gates ownership, captures the row, and stages a
`pending_confirmation` card. The DELETE runs only when the user taps
"Delete" on the card (pending_actions.apply); "Keep" discards it.

Hard safety: event_id is `int`, not a list, and at most one delete may
be staged per turn — a single misheard voice command can never wipe a
day of events.
"""
from __future__ import annotations
from typing import Any


async def execute(
    ctx,
    event_id: int,
) -> dict[str, Any]:
    if not isinstance(event_id, int):
        raise ValueError(f"event_id must be an integer, got {type(event_id).__name__}")
    if event_id <= 0:
        raise ValueError(f"event_id must be a positive integer, got {event_id}")

    # Bulk-delete guardrail: one staged delete per request. The user
    # must initiate further deletes in separate turns.
    from backend.ask import _deletes_this_turn, DELETE_TURN_LIMIT
    n_so_far = _deletes_this_turn.get()
    if n_so_far >= DELETE_TURN_LIMIT:
        raise ValueError(
            "REFUSED: a delete was already staged in this turn. "
            "To prevent accidental bulk-deletion, only ONE event may be "
            "deleted per request. STOP, list the remaining events to the "
            "user with their ids and titles, and wait for the user to "
            "confirm exactly which one(s) to delete next. Do NOT call "
            "delete_calendar_event again in this turn."
        )
    _deletes_this_turn.set(n_so_far + 1)

    from backend.database import get_conn
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, title, starts_at, ends_at, all_day, person, notes, "
            "       owner_user_id "
            "FROM events WHERE id=?", (event_id,),
        ).fetchone()
    if not row:
        raise ValueError(f"event {event_id} not found (already deleted?)")
    event_dict = dict(row)

    # Ownership gate: a non-admin caller may only delete events they
    # themselves own. Calendar write-share does NOT grant deletion.
    from backend.calendars import require_event_owner_or_admin
    require_event_owner_or_admin(
        getattr(ctx, "role", None),
        getattr(ctx, "user_id", None),
        event_dict,
    )

    # "Anfahrt:" buffer events linked to this one (block_travel_time
    # marks them with [LINKED_TO=<id>] in notes) go with it.
    link_marker = f"[LINKED_TO={event_id}]"
    with get_conn() as conn:
        linked_rows = conn.execute(
            "SELECT id, title FROM events WHERE notes LIKE ?",
            (f"%{link_marker}%",),
        ).fetchall()
    linked_ids = [int(r["id"]) for r in linked_rows]

    from backend import pending_actions as pa
    pending_id = pa.stage_before_apply(
        skill="delete_calendar_event",
        ctx=ctx,
        apply_kind="delete_event",
        apply_args={"event_id": event_id, "linked_ids": linked_ids},
        params={"event_id": event_id},
        preview={
            "action":    "delete",
            "event_id":  event_id,
            "event":     {
                "title":     event_dict.get("title"),
                "starts_at": event_dict.get("starts_at"),
                "ends_at":   event_dict.get("ends_at"),
                "person":    event_dict.get("person"),
                "notes":     event_dict.get("notes"),
            },
            "cascaded":  len(linked_ids),
        },
    )

    title = event_dict.get("title") or f"event {event_id}"
    return {
        "pending":    True,
        "pending_id": pending_id,
        "event":      event_dict,
        "cascaded_event_ids": linked_ids,
        "_llm_hint": (
            f"shown_to_user: a confirmation card for deleting '{title}' is on screen. "
            "NOTHING is deleted yet — it happens only when the user taps Delete on the card. "
            "Tell the user the card is waiting for their confirmation; do not claim the event is deleted."
        ),
    }
