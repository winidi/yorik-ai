"""delete_calendar_event skill — DELETE one row from events.

Hard safety: event_id is `int`, not a list. Backend rejects non-positive
integers. Mass-delete is impossible via this skill — at most one row
vanishes per LLM call, each requires confirmation when confirm_mutations
is on.

Apply-then-confirm pattern (matches add/update): the DELETE happens
immediately so the user sees the row disappear. On cancel/test the row
is re-inserted with the same id.
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

    # Bulk-delete guardrail. Without this, the LLM can wipe out a day's
    # worth of events in a single /api/ask turn by looping this skill (an
    # incident we hit when the user asked to delete one babysitting termin
    # and got every event from yesterday deleted instead). After the first
    # delete, refuse and tell the LLM to slow down. The user must initiate
    # subsequent deletes in separate turns OR be given an explicit summary
    # to confirm.
    from backend.ask import _deletes_this_turn, DELETE_TURN_LIMIT
    n_so_far = _deletes_this_turn.get()
    if n_so_far >= DELETE_TURN_LIMIT:
        raise ValueError(
            "REFUSED: another event was already deleted in this turn. "
            "To prevent accidental bulk-deletion, only ONE event may be "
            "deleted per request. STOP, list the remaining events to the "
            "user with their ids and titles, and wait for the user to "
            "confirm exactly which one(s) to delete next. Do NOT call "
            "delete_calendar_event again in this turn."
        )
    _deletes_this_turn.set(n_so_far + 1)

    # Capture the full row first so rollback can re-insert it AND so we
    # have something to quote in the reply. owner_user_id is selected
    # so the ownership gate just below can run.
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
    # themselves own. Calendar write-share does NOT grant deletion
    # rights — that's by design.
    from backend.calendars import require_event_owner_or_admin
    require_event_owner_or_admin(
        getattr(ctx, "role", None),
        getattr(ctx, "user_id", None),
        event_dict,
    )

    # Cascade: also capture + delete any "Anfahrt:" buffer events
    # linked to this one (block_travel_time marks them with
    # [LINKED_TO=<id>] in notes). Captured rows are stashed alongside
    # the main one so the rollback path restores them too.
    link_marker = f"[LINKED_TO={event_id}]"
    with get_conn() as conn:
        linked_rows = conn.execute(
            "SELECT id, title, starts_at, ends_at, all_day, person, notes "
            "FROM events WHERE notes LIKE ?",
            (f"%{link_marker}%",),
        ).fetchall()
    linked_dicts = [dict(r) for r in linked_rows]

    # Apply the delete (main + any linked buffers, in one transaction).
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM events WHERE id=?", (event_id,))
        deleted_count = cur.rowcount
        if linked_dicts:
            conn.execute(
                f"DELETE FROM events WHERE id IN ("
                f"{','.join('?' * len(linked_dicts))})",
                [d["id"] for d in linked_dicts],
            )
        conn.commit()
    if deleted_count != 1:
        raise RuntimeError(f"expected to delete 1 row, deleted {deleted_count}")

    # Surface the change to the calendar UI — no highlight since the
    # event is gone, but a refetch + jump to the right month.
    from backend.ui_tools import _append
    _append({
        "type":        "show_calendar",
        "view":        "month",
        "anchor_date": (event_dict.get("starts_at") or "")[:10],
        "reason":      f"deleted event: {event_dict.get('title')}",
    })

    # Stage rollback (re-insert the row) if confirm-mode is on.
    # When buffer rows were cascade-deleted, pass them along so the
    # rollback handler can restore them too — otherwise canceling the
    # delete leaves a "Termin" but no "Anfahrt:".
    from backend import pending_actions as pa
    pa.confirm_then_apply(
        skill="delete_calendar_event",
        ctx=ctx,
        rollback_kind="restore_event",
        rollback_args={
            "event_row":   event_dict,
            "linked_rows": linked_dicts,
        },
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
            "cascaded":  len(linked_dicts),
        },
    )

    return {
        "deleted_event_id":      event_id,
        "event":                 event_dict,
        "cascaded_event_ids":    [d["id"] for d in linked_dicts],
    }
