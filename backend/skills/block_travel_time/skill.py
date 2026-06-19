"""block_travel_time skill — insert a buffer event for drive time.

Pairs with add_calendar_event. The main skill stores travel metadata
on the event row (drives the calendar's travel-time badge); this skill inserts
a *separate* event titled "Drive to: <title>" from
`main.starts_at - minutes` to `main.starts_at`, so the slot is
visually reserved.

direction='forward' (default) = "Drive to:" buffer ending when the
event starts. direction='return' = "Drive back:" buffer starting when
the event ends, either to the user's home OR to the next event's
venue when one follows on the same day.

After a successful forward block we ALSO compute a feasibility
analysis for the return trip (next-event lookup, home-drive minutes,
overlap detection) and return it so the LLM can ask the user
"Soll ich die Rückfahrt auch einplanen?" with concrete numbers.

Link tracking: the buffer's `notes` column carries `[LINKED_TO=<id>]`
so `delete_calendar_event` can cascade-delete it when the main event
is removed. Re-running on the same event+direction is a no-op (deduped
via the marker plus direction tag).
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any, Optional


LINK_MARKER_PREFIX = "[LINKED_TO="
LINK_MARKER_TMPL = "[LINKED_TO={event_id}]"
RETURN_TAG = "[DIR=return]"  # second marker so forward + return can coexist


async def execute(
    ctx,
    event_id: int,
    minutes: Optional[int] = None,
    direction: str = "forward",
) -> dict[str, Any]:
    if not isinstance(event_id, int) or event_id <= 0:
        raise ValueError(f"event_id must be a positive integer, got {event_id!r}")
    if direction not in ("forward", "return"):
        raise ValueError(f"direction must be 'forward' or 'return', got {direction!r}")
    if minutes is not None:
        try:
            minutes = int(minutes)
        except (TypeError, ValueError):
            raise ValueError(f"minutes must be an integer, got {minutes!r}")
        if minutes <= 0:
            raise ValueError(f"minutes must be > 0, got {minutes}")
        if minutes > 24 * 60:
            raise ValueError(f"minutes can't exceed 24h ({24 * 60}), got {minutes}")

    from backend.database import get_conn
    with get_conn() as conn:
        main = conn.execute(
            "SELECT id, title, starts_at, ends_at, calendar_id, owner_user_id, "
            "       visibility, location, location_lat, "
            "       location_lon, travel_time_s "
            "FROM events WHERE id = ?",
            (event_id,),
        ).fetchone()
    if not main:
        raise ValueError(f"event {event_id} not found")

    # Ownership gate: only the main event's owner (or admin) can attach
    # or modify a travel buffer. Same rule as update/delete; a member
    # with calendar read-share on someone else's event can't add or
    # change drive-time blocks.
    from backend.calendars import require_event_owner_or_admin
    require_event_owner_or_admin(
        getattr(ctx, "role", None),
        getattr(ctx, "user_id", None),
        dict(main),
    )

    # Determine buffer length. Explicit `minutes` wins; otherwise derive
    # from the row's stored travel_time_s (set by add_calendar_event when
    # the event has a geocodable location).
    if minutes is None:
        travel_s = main["travel_time_s"]
        if not travel_s or travel_s <= 0:
            return {
                "_llm_hint": (
                    f"event {event_id} has no stored travel_time_s — "
                    "ask the user how long the drive takes (call with "
                    "`minutes=<n>`), or first set a location on the "
                    "event so Yorik can compute it."
                ),
                "ok": False,
            }
        minutes = max(1, math.ceil(travel_s / 60))

    # Dedupe: forward and return are tracked separately. Return blocks
    # additionally carry RETURN_TAG so a forward+return pair can coexist
    # without one cannibalising the other.
    marker = LINK_MARKER_TMPL.format(event_id=event_id)
    direction_match_sql = (
        "notes LIKE ? AND notes LIKE ?" if direction == "return"
        else "notes LIKE ? AND notes NOT LIKE ?"
    )
    direction_match_params = (
        (f"%{marker}%", f"%{RETURN_TAG}%") if direction == "return"
        else (f"%{marker}%", f"%{RETURN_TAG}%")
    )
    with get_conn() as conn:
        existing = conn.execute(
            f"SELECT id, title, starts_at, ends_at "
            f"FROM events WHERE {direction_match_sql}",
            direction_match_params,
        ).fetchone()
    if existing:
        # Compute the existing duration so we can decide whether to
        # leave it alone or slide it to match the new `minutes`.
        try:
            existing_start = datetime.fromisoformat(existing["starts_at"])
            existing_end   = datetime.fromisoformat(existing["ends_at"])
            existing_minutes = int((existing_end - existing_start).total_seconds() // 60)
        except (TypeError, ValueError):
            existing_minutes = -1  # treat as "unknown / re-write"

        if existing_minutes == minutes:
            # Truly idempotent — already correct, no DB write needed.
            return {
                "ok":               True,
                "already_blocked":  True,
                "updated_in_place": False,
                "direction":        direction,
                "block_event_id":   existing["id"],
                "minutes":          minutes,
                "starts_at":        existing["starts_at"],
                "ends_at":          existing["ends_at"],
                "verified_state": {
                    "block_event_id": existing["id"],
                    "starts_at":      existing["starts_at"],
                    "ends_at":        existing["ends_at"],
                    "minutes":        minutes,
                },
                "_llm_hint": (
                    f"event {event_id} already has a {direction} drive-time "
                    f"block at the requested duration ({minutes} min). "
                    "Quote verified_state.starts_at and .ends_at to the user."
                ),
            }

        # Existing block has wrong duration — slide it in place. This is
        # what fixes the "60-min Anfahrt stuck at the original times after
        # the user asked for 168 min" bug from the Tropical Islands
        # transcript: the LLM kept calling us with new `minutes` and we
        # used to return "already_blocked" without doing anything.
        try:
            main_start = datetime.fromisoformat(main["starts_at"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"event {event_id} has invalid starts_at: {exc}")
        if direction == "return":
            main_end_raw = main["ends_at"] or main["starts_at"]
            try:
                main_end = datetime.fromisoformat(main_end_raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"event {event_id} has invalid ends_at: {exc}")
            new_start = main_end
            new_end   = main_end + timedelta(minutes=minutes)
        else:
            new_start = main_start - timedelta(minutes=minutes)
            new_end   = main_start
        new_start_iso = new_start.isoformat(timespec="seconds")
        new_end_iso   = new_end.isoformat(timespec="seconds")

        with get_conn() as conn:
            conn.execute(
                "UPDATE events SET starts_at=?, ends_at=? WHERE id=?",
                (new_start_iso, new_end_iso, existing["id"]),
            )
            verified = conn.execute(
                "SELECT id, title, starts_at, ends_at FROM events WHERE id=?",
                (existing["id"],),
            ).fetchone()
            conn.commit()

        # Refresh the calendar UI so the user sees the moved block right away.
        from backend.ui_tools import _append
        _append({
            "type":                "show_calendar",
            "view":                "week",
            "anchor_date":         main["starts_at"][:10],
            "highlight_event_ids": [event_id, existing["id"]],
            "reason":              f"updated {direction} travel block for event {event_id}",
        })

        # Conflict scan on the new window — the slid block may now sit
        # on top of events that the old (shorter/longer) duration
        # avoided.
        slid_conflicts: list[dict[str, Any]] = []
        owner_id = main["owner_user_id"]
        if owner_id is not None:
            try:
                from backend.calendars import scan_overlaps
                slid_conflicts = scan_overlaps(
                    new_start_iso, new_end_iso,
                    owner_user_id=owner_id,
                    exclude_event_id=existing["id"],
                )
            except Exception as e:
                __import__("logging").getLogger("yorik.block_travel_time").debug(
                    "slid-block conflict scan failed: %s", e,
                )
        slid_hint_extra = ""
        if slid_conflicts:
            slid_hint_extra = (
                f"\n\nWARNING: the new window overlaps {len(slid_conflicts)} "
                f"existing event(s): " +
                "; ".join(
                    f"{c['title']} ({c['starts_at'][11:16]}-{c['ends_at'][11:16]}, id={c['id']})"
                    for c in slid_conflicts
                ) + ". Surface to the user before moving on."
            )

        out = {
            "ok":               True,
            "already_blocked":  True,
            "updated_in_place": True,
            "direction":        direction,
            "block_event_id":   existing["id"],
            "minutes":          minutes,
            "starts_at":        new_start_iso,
            "ends_at":          new_end_iso,
            "verified_state": {
                "block_event_id": existing["id"],
                "starts_at":      verified["starts_at"] if verified else new_start_iso,
                "ends_at":        verified["ends_at"]   if verified else new_end_iso,
                "minutes":        minutes,
            },
            "_llm_hint": (
                f"existing {direction} drive-time block was {existing_minutes} min — "
                f"slid in place to {minutes} min ({new_start_iso} – {new_end_iso}). "
                "Quote verified_state.starts_at and .ends_at to the user EXACTLY; "
                "do not rely on your memory of what the duration used to be."
            ) + slid_hint_extra,
        }
        if slid_conflicts:
            out["conflicts"] = slid_conflicts
        return out

    # Compute the buffer window. Forward = ends exactly when event starts.
    # Return = starts exactly when event ends.
    try:
        main_start = datetime.fromisoformat(main["starts_at"])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"event {event_id} has invalid starts_at: {exc}")

    if direction == "return":
        main_end_raw = main["ends_at"] or main["starts_at"]
        try:
            main_end = datetime.fromisoformat(main_end_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"event {event_id} has invalid ends_at: {exc}")
        block_start = main_end
        block_end = main_end + timedelta(minutes=minutes)
    else:
        block_start = main_start - timedelta(minutes=minutes)
        block_end = main_start

    main_title = (main["title"] or "Event").strip()
    block_title = (
        f"Drive back: {main_title}" if direction == "return"
        else f"Drive to: {main_title}"
    )
    # Notes carry the LINKED_TO marker, plus the RETURN_TAG when relevant.
    # Both stay machine-readable and never collide with user prose.
    block_notes = marker + (RETURN_TAG if direction == "return" else "")

    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO events (title, starts_at, ends_at, all_day, person, "
            " notes, calendar_id, owner_user_id, visibility, "
            " category) "
            "VALUES (?, ?, ?, 0, NULL, ?, ?, ?, ?, ?)",
            (
                block_title,
                block_start.isoformat(timespec="seconds"),
                block_end.isoformat(timespec="seconds"),
                block_notes,
                main["calendar_id"],
                main["owner_user_id"],
                main["visibility"] or "default",
                "drive",  # buffer always coloured as drive (amber)
            ),
        )
        block_event_id = cur.lastrowid
        conn.commit()

    # Refresh the calendar UI and highlight both rows so the user sees
    # the new block alongside the main appointment.
    from backend.ui_tools import _append
    _append({
        "type":                "show_calendar",
        "view":                "week",
        "anchor_date":         main["starts_at"][:10],
        "highlight_event_ids": [event_id, block_event_id],
        "reason":              f"blocked {direction} travel for event {event_id}",
    })

    # Confirm-mode rollback: deleting the buffer is enough — the main
    # event is unaffected.
    from backend import pending_actions as pa
    if pa.should_confirm(ctx):
        pa.stage_with_rollback(
            skill="block_travel_time",
            rollback_kind="delete_event",
            rollback_args={"event_id": block_event_id},
            preview={
                "action":         "create_buffer",
                "direction":      direction,
                "main_event_id":  event_id,
                "block_event_id": block_event_id,
                "title":          block_title,
                "starts_at":      block_start.isoformat(timespec="seconds"),
                "ends_at":        block_end.isoformat(timespec="seconds"),
                "minutes":        minutes,
            },
            ctx=ctx,
        )

    block_start_iso = block_start.isoformat(timespec="seconds")
    block_end_iso   = block_end.isoformat(timespec="seconds")

    # Conflict scan — mirrors add_calendar_event / update_calendar_event.
    # The block was just inserted; surface any real events it overlaps
    # so the LLM can warn the user. Phase 5 Test A flushed this gap out
    # — a Rückfahrt landed silently on top of an existing meeting. The
    # scan excludes other Anfahrt/Rückfahrt blocks (LINKED_TO marker)
    # so we don't false-positive on the user's own travel buffers.
    conflicts: list[dict[str, Any]] = []
    owner_id = main["owner_user_id"]
    if owner_id is not None:
        try:
            from backend.calendars import scan_overlaps
            conflicts = scan_overlaps(
                block_start_iso, block_end_iso,
                owner_user_id=owner_id,
                exclude_event_id=block_event_id,
            )
        except Exception as e:
            __import__("logging").getLogger("yorik.block_travel_time").debug(
                "conflict scan failed: %s", e,
            )

    result: dict[str, Any] = {
        "ok":              True,
        "already_blocked": False,
        "direction":       direction,
        "block_event_id":  block_event_id,
        "minutes":         minutes,
        "starts_at":       block_start_iso,
        "ends_at":         block_end_iso,
        "verified_state": {
            "block_event_id": block_event_id,
            "starts_at":      block_start_iso,
            "ends_at":        block_end_iso,
            "minutes":        minutes,
        },
    }
    if conflicts:
        result["conflicts"] = conflicts
        conflict_text = "; ".join(
            f"{c['title']} ({c['starts_at'][11:16]}-{c['ends_at'][11:16]}, id={c['id']})"
            for c in conflicts
        )
        result["_conflict_hint"] = (
            f"WARNING: this {direction} drive-time block overlaps {len(conflicts)} "
            f"existing event(s): {conflict_text}. Surface to the user so they can "
            f"reschedule if needed."
        )

    # FORWARD ONLY — also do a return-trip feasibility check so the LLM
    # can ask the user "Soll ich die Rückfahrt auch blocken?" with real
    # numbers. We don't insert anything here — the LLM decides based on
    # the user's reply and calls back with direction='return'.
    if direction == "forward":
        result["return_trip"] = _analyse_return_trip(ctx, main, minutes)
        result["_llm_hint"] = (
            "Forward drive-time blocked. Before moving on, OFFER the user "
            "the return trip — call out the destination from the "
            "return_trip object ('Heimfahrt ~X Min' OR 'Weiterfahrt zu "
            "<next event title> ~X Min'). If return_trip.warning is set, "
            "surface it to the user so they can decide whether to "
            "reschedule. On 'ja': call block_travel_time again with "
            "direction='return' and the minutes from return_trip.minutes."
        )

    # If there are conflicts, prepend the warning so the LLM sees it
    # before whatever the return-trip hint says. Conflicts must surface
    # — they're the user's other events at the same time.
    if conflicts:
        existing_hint = result.get("_llm_hint", "")
        result["_llm_hint"] = (
            result.pop("_conflict_hint", "") + (("\n\n" + existing_hint) if existing_hint else "")
        )
    elif "_conflict_hint" in result:
        # Shouldn't happen (only set when conflicts non-empty), but be tidy.
        result.pop("_conflict_hint")

    return result


def _analyse_return_trip(
    ctx,
    main_event: Any,
    forward_minutes: int,
) -> dict[str, Any]:
    """Look at the day AFTER `main_event` ends and figure out what the
    return trip should look like. Returns a dict the LLM can quote
    back to the user — does NOT insert anything. All fields besides
    `feasible` are best-effort and may be None when we can't compute.
    """
    from backend.database import get_conn

    main_end_raw = main_event["ends_at"] or main_event["starts_at"]
    try:
        main_end = datetime.fromisoformat(main_end_raw)
    except (TypeError, ValueError):
        return {"feasible": False, "reason": "main event has invalid ends_at"}

    day_str = main_end.strftime("%Y-%m-%d")
    next_day_str = (main_end + timedelta(days=1)).strftime("%Y-%m-%d")

    # Find the next event on the SAME day (any calendar the user can
    # see). We don't filter by calendar here — overlap matters even if
    # the next event is on the kids' calendar.
    with get_conn() as conn:
        next_row = conn.execute(
            "SELECT id, title, starts_at, ends_at, location, location_lat, "
            "       location_lon, travel_time_s "
            "FROM events "
            "WHERE owner_user_id IS NOT DISTINCT FROM ? "
            "  AND starts_at >= ? "
            "  AND starts_at <  ? "
            "  AND id != ? "
            "  AND id NOT IN (SELECT id FROM events WHERE notes LIKE ?) "
            "ORDER BY starts_at ASC LIMIT 1",
            (
                main_event["owner_user_id"],
                main_end.isoformat(timespec="seconds"),
                next_day_str,
                main_event["id"],
                # Skip our own drive-time buffers — they don't count as
                # "next appointment" since they ARE the trip.
                f"%{LINK_MARKER_PREFIX}%",
            ),
        ).fetchone()

    # No follow-up event today → straight home-drive. Estimate as same
    # duration as the forward trip (good enough; the LLM can ask user
    # if rush hour matters).
    if not next_row:
        return {
            "feasible":          True,
            "kind":              "home",
            "minutes":           forward_minutes,
            "estimated":         True,
            "next_event":        None,
            "warning":           None,
        }

    next_start = datetime.fromisoformat(next_row["starts_at"])
    gap_minutes = int((next_start - main_end).total_seconds() // 60)

    # Has the next event a location AND a stored drive-time? If yes, we
    # can suggest "Weiterfahrt" (event-to-event) instead of "Heimfahrt
    # + back out". When add_calendar_event geocoded the next event we
    # have travel_time_s FROM HOME — we don't have venue→venue. Best we
    # can do without a maps roundtrip is the home-drive estimate; flag
    # `estimated=True` so the LLM tells the user this is an estimate.
    next_has_loc = bool((next_row["location"] or "").strip())

    # Heuristic: home-drive ≈ forward trip; venue-to-venue we can't
    # compute synchronously here without geocoding. Suggest the same
    # forward_minutes as the floor and let the LLM ask the user if
    # the next venue is much further.
    drive_minutes = forward_minutes
    overlap = drive_minutes - gap_minutes

    warning: Optional[str] = None
    if overlap > 0:
        warning = (
            f"Rückfahrt ({drive_minutes} Min) überschneidet sich um "
            f"{overlap} Min mit „{next_row['title']}“ um "
            f"{next_start.strftime('%H:%M')}. Termin verschieben?"
        )

    return {
        "feasible":  overlap <= 0,
        "kind":      "to_next_event" if next_has_loc else "home",
        "minutes":   drive_minutes,
        "estimated": True,
        "next_event": {
            "id":         next_row["id"],
            "title":      next_row["title"],
            "starts_at":  next_row["starts_at"],
            "has_location": next_has_loc,
            "gap_minutes":  gap_minutes,
        },
        "warning":   warning,
    }
