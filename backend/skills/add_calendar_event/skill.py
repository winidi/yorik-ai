"""add_calendar_event skill — insert into the events table.

Beta confirmation flow: the INSERT always happens immediately so the
user sees the event highlighted in the calendar. If confirm_mutations
is ON, we ALSO stage a pending action whose rollback is "delete event
by id". The user clicks Cancel / Just-testing to roll back, or Confirm
to keep — handled by /api/pending/{id}/{op} endpoints.

This "apply-then-confirm" UX is intentional: the user wants to SEE the
draft event before approving. The fallback if they walk away is a real
event in the calendar — better than a ghost the user can't verify.
"""

from __future__ import annotations
from datetime import datetime, timedelta
from typing import Any, Optional


async def execute(
    ctx,
    title: str,
    starts_at: str,
    ends_at: Optional[str] = None,
    all_day: bool = False,
    person: Optional[str] = None,
    notes: Optional[str] = None,
    # Calendar-overlay extensions (migration 010): pick the target
    # calendar explicitly, OR pass attendees and let auto-route choose.
    calendar_id: Optional[int] = None,
    attendee_user_ids: Optional[list[int]] = None,
    attendee_names: Optional[list[str]] = None,
    visibility: str = "default",  # 'default' | 'private'
    # Travel-time integration (migration 019): when `location` is set,
    # geocode it via the maps connector + compute driving time from the
    # user's home address. Cached on the event row so the calendar UI's
    # travel badge doesn't have to recompute on every render.
    location: Optional[str] = None,
    # Colour category (migration 026): closed enum, frontend maps to a
    # subtle palette. Pick the best match from the user's phrasing.
    category: Optional[str] = None,
) -> dict[str, Any]:
    if not title or not title.strip():
        raise ValueError("title is required")
    try:
        start_dt = datetime.fromisoformat(starts_at)
    except ValueError as e:
        raise ValueError(f"starts_at must be ISO 8601 (e.g. 2026-05-22T14:00:00): {e}")
    if not ends_at:
        ends_at = (start_dt + timedelta(hours=1)).isoformat()
    else:
        try:
            datetime.fromisoformat(ends_at)
        except ValueError as e:
            raise ValueError(f"ends_at must be ISO 8601: {e}")

    # Phase 12.3: refuse past-date scheduling unless the caller (LLM)
    # was clearly instructed by the user to backfill. Yesterday's
    # "Thursday" → today minus 1 was a recurring eval failure where the
    # model picked the previous occurrence of a weekday instead of the
    # next one. The date table in the system prompt covers this, but
    # the model still slips. Loud refusal at the skill layer beats
    # depending on prompt discipline.
    today = datetime.now().date()
    if start_dt.date() < today:
        days_in_past = (today - start_dt.date()).days
        raise ValueError(
            f"REFUSED: starts_at is {days_in_past} day(s) in the past "
            f"({start_dt.date().isoformat()}; today is {today.isoformat()}). "
            f"If the user named a weekday ('Mittwoch', 'Thursday'), look it "
            f"up in the date table in the system prompt — bare weekday names "
            f"ALWAYS resolve to the NEXT future occurrence, never the past. "
            f"If the user genuinely wants to backfill a past event, ask them "
            f"to confirm the explicit date."
        )

    # Auto-route: if calendar_id wasn't passed, pick based on attendees.
    # Solo → creator's Personal calendar. Multi-user → Shared.
    creator_id = getattr(ctx, "user_id", None)
    from backend import calendars as _cal
    if calendar_id is None and creator_id is not None:
        calendar_id = _cal.auto_route_calendar(
            creator_id, attendee_user_ids or [],
        )

    if visibility not in ("default", "private"):
        visibility = "default"

    # Category — normalise from the LLM's free-text (accepts slug or
    # synonyms like "Arzttermin" → "health"). Unknown → NULL (no colour
    # rather than wrong colour).
    from backend.event_categories import normalize_category
    category_norm = normalize_category(category)

    # Geocode the location + compute travel time BEFORE the INSERT so
    # the row is born complete. Best-effort: any failure (no profile
    # address, routing service down, geocode miss) silently leaves the
    # travel columns NULL — the calendar UI handles that gracefully.
    loc_norm = (location or "").strip() or None
    loc_lat: Optional[float] = None
    loc_lon: Optional[float] = None
    travel_s: Optional[int] = None
    travel_m: Optional[int] = None
    travel_provider: Optional[str] = None
    travel_computed_at: Optional[str] = None
    if loc_norm:
        try:
            from backend.connectors import invoke as _conn_invoke
            geo = await _conn_invoke("maps", {"op": "geocode", "query": loc_norm})
            if geo and geo.get("ok") is not False and geo.get("lat") is not None:
                loc_lat = float(geo["lat"])
                loc_lon = float(geo["lon"])
                # Compute travel time when the user has a home address.
                # Try progressively-less-specific forms (full → PLZ+City →
                # City) so an obscure street doesn't kill travel-time.
                from backend.skills.calculate_travel_time.skill import (
                    _user_home_address_variants as _home_variants,
                )
                for candidate in (_home_variants(creator_id) if creator_id else []):
                    route = await _conn_invoke("maps", {
                        "op":   "directions",
                        "from": candidate,
                        "to":   f"{loc_lat},{loc_lon}",
                        "mode": "driving",
                    })
                    if route and route.get("ok") is not False:
                        travel_s = int(route.get("duration_s") or 0) or None
                        travel_m = int(route.get("distance_m") or 0) or None
                        travel_provider = route.get("provider")
                        travel_computed_at = datetime.now().isoformat(timespec="seconds")
                        break
        except Exception:
            # Never block event creation on a maps glitch.
            pass

    # Apply the INSERT immediately. The calendar UI will see it and
    # render the highlight. If confirm_mutations is on, we stage the
    # rollback below — the modal is shown alongside the inserted row.
    from backend.database import get_conn
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO events (title, starts_at, ends_at, all_day, person, "
            " notes, calendar_id, owner_user_id, visibility, "
            " location, location_lat, location_lon, "
            " travel_time_s, travel_distance_m, travel_provider, travel_computed_at, "
            " category) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (title.strip(), starts_at, ends_at, 1 if all_day else 0,
             person, notes, calendar_id, creator_id, visibility,
             loc_norm, loc_lat, loc_lon,
             travel_s, travel_m, travel_provider, travel_computed_at,
             category_norm),
        )
        event_id = cur.lastrowid
        row = conn.execute(
            "SELECT id, title, starts_at, ends_at, all_day, person, notes, "
            "       calendar_id, owner_user_id, visibility, "
            "       location, location_lat, location_lon, "
            "       travel_time_s, travel_distance_m, travel_provider, travel_computed_at, "
            "       category "
            "FROM events WHERE id=?",
            (event_id,),
        ).fetchone()
        conn.commit()

    # Defensive validation: every value in attendee_user_ids must be a
    # real user_profiles.id. Catches the classic LLM bug of passing a
    # contact_id (from find_contact) where a user_id is needed — the
    # two tables have overlapping integer ranges so silent acceptance
    # leads to "invitation sent to id=356" turning into "random
    # household member got pinged" or a silently-dropped insert + the
    # LLM confidently reporting success either way.
    if attendee_user_ids:
        with get_conn() as conn:
            rows = conn.execute(
                f"SELECT id FROM user_profiles WHERE id IN "
                f"({','.join('?' * len(attendee_user_ids))})",
                [int(x) for x in attendee_user_ids],
            ).fetchall()
        valid = {int(r["id"]) for r in rows}
        bogus = [int(x) for x in attendee_user_ids if int(x) not in valid]
        if bogus:
            raise ValueError(
                f"attendee_user_ids contains id(s) {bogus} that don't exist "
                "in user_profiles. Did you pass a contact_id from "
                "find_contact instead of a user_id from find_user? Those "
                "are different tables — call find_user(query=<name>) for "
                "calendar attendees; find_contact only returns contacts "
                "(address book), whose ids are NOT valid here."
            )

    # Attach attendees + dispatch invitations.
    if attendee_user_ids or attendee_names:
        _cal.add_attendees(
            event_id,
            user_ids=attendee_user_ids or [],
            person_names=attendee_names or [],
        )
        try:
            from backend import notifications  # late import — avoids cycle
            for uid in (attendee_user_ids or []):
                if creator_id is not None and uid == creator_id:
                    continue
                notifications.create(
                    uid,
                    kind="event_invitation",
                    title=f"New invitation: {title.strip()}",
                    body=f"{starts_at} — RSVP required",
                    navigate_to=f"/r/calendar?event={event_id}",
                )
        except Exception:
            pass

    # Surface the new row to the calendar with the highlight ring.
    from backend.ui_tools import _append
    _append({
        "type":                "show_calendar",
        "view":                "month",
        "anchor_date":         starts_at[:10],
        "highlight_event_ids": [event_id],
        "reason":              f"created event: {title.strip()}",
    })

    # If confirm-mode is on, stage the rollback so the user can cancel.
    # The chat / voice UI renders inline confirm buttons attached to the
    # assistant message; the cancel/test path deletes this event.
    from backend import pending_actions as pa
    if pa.should_confirm(ctx):
        pa.stage_with_rollback(
            skill="add_calendar_event",
            rollback_kind="delete_event",
            rollback_args={"event_id": event_id},
            preview={
                "action":     "create",
                "event_id":   event_id,         # so the UI can hint "this is the row"
                "title":      title.strip(),
                "starts_at":  starts_at,
                "ends_at":    ends_at,
                "all_day":    all_day,
                "person":     person,
                "notes":      notes,
            },
            ctx=ctx,
        )

    # Post-write conflict scan — non-blocking. If the event we just
    # inserted overlaps anything else this user already has, return the
    # conflicts in the result so the LLM can warn the user. Matches
    # Google Calendar's "scheduled, but here are your overlaps" UX.
    # Excludes Anfahrt buffers and the event we just inserted itself.
    conflicts: list[dict[str, Any]] = []
    if not all_day and creator_id is not None:
        try:
            conflicts = _cal.scan_overlaps(
                starts_at, ends_at,
                owner_user_id=creator_id,
                exclude_event_id=event_id,
            )
        except Exception as e:
            log = __import__("logging").getLogger("yorik.add_calendar_event")
            log.debug("conflict scan failed: %s", e)

    # Phase 12.3: weekday echo. If the user said "Mittwoch" but the
    # date is a Monday, the model needs to notice. Embed the actual
    # weekday in the success hint so it's impossible to miss.
    _WEEKDAYS_DE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag",
                    "Freitag", "Samstag", "Sonntag"]
    weekday_de = _WEEKDAYS_DE[start_dt.weekday()]
    weekday_en = start_dt.strftime("%A")

    result: dict[str, Any] = {
        "event_id": event_id,
        "event": dict(row) if row else None,
        "verified_weekday": {
            "starts_at": starts_at,
            "weekday_de": weekday_de,
            "weekday_en": weekday_en,
        },
    }
    if conflicts:
        result["conflicts"] = conflicts
        result["_llm_hint"] = (
            f"Event created (id={event_id}) on {weekday_de} ({weekday_en}). "
            f"BUT it overlaps {len(conflicts)} existing event(s) for this user: " +
            "; ".join(
                f"{c['title']} ({c['starts_at'][11:16]}-{c['ends_at'][11:16]}, id={c['id']})"
                for c in conflicts
            ) +
            ". SURFACE this to the user immediately — quote the conflicting "
            "event titles + times. Ask whether to reschedule one of them. "
            "Do not silently move on."
        )
    else:
        result["_llm_hint"] = (
            f"Event created (id={event_id}) on {weekday_de} ({weekday_en}), "
            f"{starts_at[:10]}. If the user said a weekday name and this "
            f"weekday doesn't match, you scheduled the wrong day — apologise "
            f"and call update_calendar_event with the correct date."
        )
    return result
