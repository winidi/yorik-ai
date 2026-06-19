"""propose_meeting_times — chain check_calendar + email_draft.

Take an email_messages.id, find N free slots in the user's calendar,
and produce a draft reply naturally suggesting them. The big
"intelligent assistant" moment in chat — one user ask, multi-skill
result, sendable in two clicks.
"""
from __future__ import annotations
from datetime import datetime, timedelta
from typing import Any


_WEEKDAY_DE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
_WEEKDAY_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


async def execute(
    ctx,
    message_id: int,
    num_slots: int = 3,
    window_days: int = 7,
    duration_minutes: int = 60,
    earliest_hour: int = 9,
    latest_hour: int = 18,
) -> dict[str, Any]:
    if not isinstance(message_id, int) or message_id <= 0:
        raise ValueError("message_id must be a positive int")
    num_slots = max(1, min(int(num_slots), 5))
    window_days = max(1, min(int(window_days), 30))
    duration_minutes = max(15, min(int(duration_minutes), 240))

    # ── 1. Find free slots via check_calendar ────────────────────────
    from backend.skills.check_calendar.skill import execute as check_cal
    now = datetime.now()
    end = now + timedelta(days=window_days)
    cal = await check_cal(
        ctx,
        start_iso=now.isoformat(),
        end_iso=end.isoformat(),
        include_free_slots=True,
    )
    raw_slots = cal.get("free_slots") or []

    # Filter to working hours + the requested duration.
    candidates = []
    for slot in raw_slots:
        try:
            sh, sm = map(int, slot["start"].split(":"))
            eh, em = map(int, slot["end"].split(":"))
        except (KeyError, ValueError):
            continue
        if slot.get("duration_min", 0) < duration_minutes:
            continue
        if sh < earliest_hour or eh > latest_hour:
            # Truncate to working window if it overlaps partially.
            new_start_h = max(sh, earliest_hour)
            new_end_h = min(eh, latest_hour)
            avail = (new_end_h - new_start_h) * 60
            if avail < duration_minutes:
                continue
            slot = {**slot, "start": f"{new_start_h:02d}:00",
                    "end": f"{new_end_h:02d}:00",
                    "duration_min": avail}
        candidates.append(slot)

    # Pick a spread: prefer different dates, no two slots on the same day.
    chosen: list[dict[str, Any]] = []
    seen_dates: set[str] = set()
    for slot in candidates:
        d = slot.get("date")
        if not d or d in seen_dates:
            continue
        chosen.append({
            "date": d,
            "start": slot["start"],
            "end": _add_minutes(slot["start"], duration_minutes),
        })
        seen_dates.add(d)
        if len(chosen) >= num_slots:
            break

    if not chosen:
        return {
            "slots":   [],
            "drafts":  [],
            "message": f"No free slots ≥{duration_minutes}min found in next {window_days} days during {earliest_hour}:00-{latest_hour}:00",
        }

    # ── 2. Compose the slots into natural-language lines ─────────────
    language = (getattr(ctx, "language", "") or "en").lower()
    de = language.startswith("de")
    lines = []
    for s in chosen:
        try:
            dt = datetime.fromisoformat(f"{s['date']}T{s['start']}:00")
        except ValueError:
            continue
        wd = (_WEEKDAY_DE if de else _WEEKDAY_EN)[dt.weekday()]
        if de:
            lines.append(f"- {wd}, {dt.day}.{dt.month}., {s['start']}–{s['end']} Uhr")
        else:
            lines.append(f"- {wd}, {dt.strftime('%B %-d')} at {s['start']}–{s['end']}")

    if de:
        body_lines = [
            "Hallo,",
            "",
            "danke für die Nachricht. Folgende Termine würden mir passen:",
            "",
            *lines,
            "",
            "Sag mir gerne, welcher davon am besten für dich passt.",
        ]
    else:
        body_lines = [
            "Hi,",
            "",
            "Thanks for the message. The following times work for me:",
            "",
            *lines,
            "",
            "Let me know which one suits you best.",
        ]
    body = "\n".join(body_lines)

    # ── 3. Hand off to email_draft, which produces the chat card ──────
    from backend.skills.email_draft.skill import execute as draft_email
    draft_result = await draft_email(
        ctx,
        message_id=message_id,
        extra_instructions=(
            f"Reply suggesting these {len(chosen)} time slot{'s' if len(chosen) != 1 else ''}: "
            + ", ".join(f"{s['date']} {s['start']}-{s['end']}" for s in chosen)
            + f". Use this exact body if it fits the conversation tone:\n\n{body}"
        ),
        variants=1,
    )

    return {
        "slots":   chosen,
        "drafts":  draft_result.get("drafts") or [],
        "sources": draft_result.get("sources") or [],
    }


def _add_minutes(hhmm: str, minutes: int) -> str:
    try:
        h, m = map(int, hhmm.split(":"))
    except ValueError:
        return hhmm
    total = h * 60 + m + minutes
    return f"{(total // 60) % 24:02d}:{total % 60:02d}"
