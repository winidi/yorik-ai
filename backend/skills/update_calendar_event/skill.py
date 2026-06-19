"""update_calendar_event skill — single-row UPDATE on events.

Apply-then-confirm pattern (matches add_calendar_event): the UPDATE
happens immediately so the user sees the change reflected in the
calendar. If confirm_mutations is ON, we stage a rollback that
restores the captured pre-update field values on cancel/test.
"""
from __future__ import annotations
from datetime import datetime
from typing import Any, Optional


async def execute(
    ctx,
    event_id: int,
    title: Optional[str] = None,
    starts_at: Optional[str] = None,
    ends_at: Optional[str] = None,
    all_day: Optional[bool] = None,
    person: Optional[str] = None,
    notes: Optional[str] = None,
    # Colour category (migration 026): pass to recolour the event on the
    # calendar. Pass empty string to clear it. See backend/event_categories.py
    # for the enum.
    category: Optional[str] = None,
) -> dict[str, Any]:
    if not isinstance(event_id, int) or event_id <= 0:
        raise ValueError("event_id must be a positive integer")

    for label, value in (("starts_at", starts_at), ("ends_at", ends_at)):
        if value is not None:
            try:
                datetime.fromisoformat(value)
            except ValueError as e:
                raise ValueError(f"{label} must be ISO 8601: {e}")

    # Phase 12.3: same past-date guard as add_calendar_event. Moving an
    # event INTO the past is almost always a weekday-math slip — the
    # model picked "last Tuesday" instead of "next Tuesday". Refuse
    # with the same loud message so the model checks the date table.
    if starts_at:
        new_date = datetime.fromisoformat(starts_at).date()
        today = datetime.now().date()
        if new_date < today:
            raise ValueError(
                f"REFUSED: starts_at is {(today - new_date).days} day(s) in "
                f"the past ({new_date.isoformat()}; today is "
                f"{today.isoformat()}). Bare weekday names ALWAYS resolve "
                f"to the NEXT future occurrence — look it up in the date "
                f"table in the system prompt before retrying."
            )

    # Collect only the fields actually being changed.
    updates: dict[str, Any] = {}
    for k, v in (
        ("title",     title.strip() if isinstance(title, str) else title),
        ("starts_at", starts_at),
        ("ends_at",   ends_at),
        ("all_day",   None if all_day is None else (1 if all_day else 0)),
        ("person",    person),
        ("notes",     notes),
    ):
        if v is not None:
            updates[k] = v

    # Category — separate branch because "" means "clear" (write NULL).
    if category is not None:
        if category == "":
            updates["category"] = None
        else:
            from backend.event_categories import normalize_category
            normd = normalize_category(category)
            if normd is None:
                raise ValueError(
                    f"unknown category {category!r} — valid: family, "
                    "business, drive, health, personal, social"
                )
            updates["category"] = normd

    if not updates:
        raise ValueError("nothing to update — pass at least one field")

    # Capture the pre-update values so we can roll back. Include
    # owner_user_id so the ownership gate below can run.
    from backend.database import get_conn
    with get_conn() as conn:
        before = conn.execute(
            "SELECT id, title, starts_at, ends_at, all_day, person, notes, "
            "       owner_user_id "
            "FROM events WHERE id=?", (event_id,),
        ).fetchone()
    if not before:
        raise ValueError(f"event {event_id} not found")
    before_dict = dict(before)

    # Ownership gate: a non-admin caller may only update events they
    # themselves own. Calendar write-share does NOT grant mutation
    # rights — that's by design (see calendars.require_event_owner_or_admin).
    from backend.calendars import require_event_owner_or_admin
    require_event_owner_or_admin(
        getattr(ctx, "role", None),
        getattr(ctx, "user_id", None),
        before_dict,
    )

    # Apply the update.
    set_clause = ", ".join(f"{k}=?" for k in updates)
    params = list(updates.values()) + [event_id]
    with get_conn() as conn:
        conn.execute(f"UPDATE events SET {set_clause} WHERE id=?", params)
        # Re-read the row to capture the AUTHORITATIVE post-update
        # state. This row is what gets surfaced as `verified_state` so
        # the LLM can quote times verbatim instead of relying on what
        # it intended to set.
        row = conn.execute(
            "SELECT id, title, starts_at, ends_at, all_day, person, notes "
            "FROM events WHERE id=?", (event_id,),
        ).fetchone()
        conn.commit()

    # Surface the change to the calendar UI.
    from backend.ui_tools import _append
    _append({
        "type":                "show_calendar",
        "view":                "month",
        "anchor_date":         (row["starts_at"] or "")[:10],
        "highlight_event_ids": [event_id],
        "reason":              f"updated event: {row['title']}",
    })

    # Stage rollback if confirm-mode is on.
    from backend import pending_actions as pa
    if pa.should_confirm(ctx):
        before_for_rollback = {k: before_dict[k] for k in updates}
        pa.stage_with_rollback(
            skill="update_calendar_event",
            rollback_kind="revert_event_fields",
            rollback_args={"event_id": event_id, "before": before_for_rollback},
            preview={
                "action":   "update",
                "event_id": event_id,
                "before":   {k: before_dict[k] for k in (
                    "title", "starts_at", "ends_at", "all_day", "person", "notes"
                )},
                "after":    {k: (updates.get(k) if k in updates else before_dict[k]) for k in (
                    "title", "starts_at", "ends_at", "all_day", "person", "notes"
                )},
            },
            ctx=ctx,
        )

    verified = dict(row) if row else None

    # Post-update conflict scan — non-blocking. Only fires when the
    # update changed the time window (start or end). Excludes Anfahrt
    # buffers and the event itself; surfaces overlaps as a warning in
    # the response so the LLM can tell the user.
    conflicts: list[dict[str, Any]] = []
    time_changed = "starts_at" in updates or "ends_at" in updates
    if time_changed and verified and verified.get("starts_at") and verified.get("ends_at"):
        owner_id = before_dict.get("owner_user_id")
        if owner_id is not None:
            try:
                from backend.calendars import scan_overlaps
                conflicts = scan_overlaps(
                    verified["starts_at"], verified["ends_at"],
                    owner_user_id=owner_id,
                    exclude_event_id=event_id,
                )
            except Exception as e:
                __import__("logging").getLogger("yorik.update_calendar_event").debug(
                    "conflict scan failed: %s", e,
                )

    out: dict[str, Any] = {
        "event_id":       event_id,
        "event":          verified,
        # Reinforce ground truth for the LLM. The same data is in
        # `event`, but giving it a name the skill manifest references
        # makes the system prompt's "quote verbatim" rule self-evident.
        "verified_state": verified,
        "_llm_hint": (
            "Update complete. Quote verified_state.starts_at and "
            "verified_state.ends_at to the user EXACTLY — do not "
            "recompute from your own memory of what you intended to "
            "set. If those times don't match what the user wanted, "
            "the update is wrong and you must call update_calendar_event "
            "again with the correct values."
        ),
    }
    if conflicts:
        out["conflicts"] = conflicts
        out["_llm_hint"] += (
            f" Also: the new time overlaps {len(conflicts)} other event(s) — " +
            "; ".join(
                f"{c['title']} ({c['starts_at'][11:16]}-{c['ends_at'][11:16]}, id={c['id']})"
                for c in conflicts
            ) +
            ". Mention this to the user so they can decide whether to reschedule."
        )
    return out
