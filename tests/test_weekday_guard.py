"""Phase 12.3: refuse past-date scheduling + echo the weekday.

Live eval batch (2026-05-29) caught Yorik scheduling 'Thursday at 4 pm'
yesterday and 'Mittwoch' two days ago — the model picked the previous
occurrence of a weekday instead of the next one. The system prompt's
date table already covers the right answer; the model just didn't
read it.

Two guards in this phase:
  1. add_calendar_event / update_calendar_event refuse starts_at < today
     with a loud message that points back at the prompt's date table.
  2. add_calendar_event success result carries verified_weekday so the
     model sees the actual day-of-week of the date it picked. A wrong
     weekday is now impossible to miss.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta

import pytest


def _mk_ctx(*, role: str, user_id: int):
    from backend.skills.registry import Registry, SkillContext
    return SkillContext(Registry(), role=role, user_id=user_id)


@pytest.fixture
def seeded_app(fresh_app):
    """fresh_app + a user_profiles row + a personal calendar.
    add_calendar_event needs both to insert into events."""
    from backend.database import get_conn
    from backend.calendars import ensure_calendars_for_user
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO user_profiles "
            "(id, name, role, voice_id, email) "
            "VALUES (1, 'Admin', 'admin', 'admin', 'a@x')"
        )
        conn.commit()
    ensure_calendars_for_user(1, "Admin")
    return fresh_app


def _yesterday_iso(hour: int = 14) -> str:
    d = date.today() - timedelta(days=1)
    return f"{d.isoformat()}T{hour:02d}:00:00"


def _tomorrow_iso(hour: int = 14) -> str:
    d = date.today() + timedelta(days=1)
    return f"{d.isoformat()}T{hour:02d}:00:00"


def _today_iso(hour: int = 14) -> str:
    return f"{date.today().isoformat()}T{hour:02d}:00:00"


class TestAddCalendarEventRefusesPastDates:

    def test_yesterday_rejected_with_loud_message(self, seeded_app):
        from backend.skills.add_calendar_event.skill import execute
        with pytest.raises(ValueError) as excinfo:
            asyncio.run(execute(
                ctx=_mk_ctx(role="admin", user_id=1),
                title="EvalTest-Past",
                starts_at=_yesterday_iso(),
            ))
        msg = str(excinfo.value)
        assert "REFUSED" in msg
        assert "past" in msg.lower()
        assert "date table" in msg
        # Loud enough that the LLM can't miss it
        assert "NEXT future occurrence" in msg

    def test_today_accepted(self, seeded_app):
        """Scheduling for today is fine — only past dates refuse."""
        from backend.skills.add_calendar_event.skill import execute
        result = asyncio.run(execute(
            ctx=_mk_ctx(role="admin", user_id=1),
            title="EvalTest-Today",
            starts_at=_today_iso(23),  # late today
        ))
        assert result["event_id"] > 0

    def test_tomorrow_accepted(self, seeded_app):
        from backend.skills.add_calendar_event.skill import execute
        result = asyncio.run(execute(
            ctx=_mk_ctx(role="admin", user_id=1),
            title="EvalTest-Tomorrow",
            starts_at=_tomorrow_iso(),
        ))
        assert result["event_id"] > 0


class TestVerifiedWeekday:

    def test_result_carries_weekday_in_de_and_en(self, seeded_app):
        from backend.skills.add_calendar_event.skill import execute
        result = asyncio.run(execute(
            ctx=_mk_ctx(role="admin", user_id=1),
            title="EvalTest-Weekday",
            starts_at=_tomorrow_iso(),
        ))
        assert "verified_weekday" in result
        vw = result["verified_weekday"]
        assert "weekday_de" in vw
        assert "weekday_en" in vw
        assert vw["weekday_de"] in [
            "Montag", "Dienstag", "Mittwoch", "Donnerstag",
            "Freitag", "Samstag", "Sonntag",
        ]
        # The DE and EN must agree about which day-of-week index this is.
        expected_dow = (datetime.fromisoformat(_tomorrow_iso())).weekday()
        de_index = [
            "Montag", "Dienstag", "Mittwoch", "Donnerstag",
            "Freitag", "Samstag", "Sonntag",
        ].index(vw["weekday_de"])
        assert de_index == expected_dow

    def test_llm_hint_includes_weekday(self, seeded_app):
        """The hint text is what the model actually reads; weekday MUST
        appear there or the echo is useless."""
        from backend.skills.add_calendar_event.skill import execute
        result = asyncio.run(execute(
            ctx=_mk_ctx(role="admin", user_id=1),
            title="EvalTest-Weekday-Hint",
            starts_at=_tomorrow_iso(),
        ))
        hint = result.get("_llm_hint", "")
        weekday_de = result["verified_weekday"]["weekday_de"]
        assert weekday_de in hint
        # The hint also tells the model what to do if it picked wrong
        assert "wrong day" in hint or "doesn't match" in hint


class TestUpdateCalendarEventRefusesPastDates:

    def test_moving_event_into_past_rejected(self, seeded_app):
        """update_calendar_event with starts_at < today must refuse —
        same loud message as add_calendar_event."""
        from backend.skills.add_calendar_event.skill import execute as add_event
        from backend.skills.update_calendar_event.skill import execute as upd_event

        # Create an event for tomorrow so we have a real id to target.
        created = asyncio.run(add_event(
            ctx=_mk_ctx(role="admin", user_id=1),
            title="EvalTest-MoveTarget",
            starts_at=_tomorrow_iso(),
        ))
        event_id = created["event_id"]

        with pytest.raises(ValueError) as excinfo:
            asyncio.run(upd_event(
                ctx=_mk_ctx(role="admin", user_id=1),
                event_id=event_id,
                starts_at=_yesterday_iso(),
            ))
        msg = str(excinfo.value)
        assert "REFUSED" in msg
        assert "past" in msg.lower()
        assert "date table" in msg
