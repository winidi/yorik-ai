"""A child's school timetable reaches the calendar skill (2026-11-27).

The family board keeps it in `timetables`, not as calendar events, so
"which lesson does Ben have at ten on Tuesday" found nothing through
check_calendar — and an agent over MCP had no other way to it."""

import asyncio
import json

import pytest

from tests.conftest import seed_user

TUESDAY = "2026-12-01"


@pytest.fixture
def house(fresh_app):
    from backend import spaces as S
    from backend.database import get_conn
    h = {
        "anna": seed_user(name="Anna Muster", role="admin", first_name="Anna", email="anna@example.local"),
        "carl": seed_user(name="Carl Muster", role="member", first_name="Carl", email="carl@example.local"),
        "ben":  seed_user(name="Ben Muster", role="restricted", first_name="Ben", email="ben@example.local"),
        "mia":  seed_user(name="Mia Muster", role="restricted", first_name="Mia", email="mia@example.local"),
        "guest": seed_user(name="Gast", role="member", first_name="Gast", email="gast@example.local"),
    }
    S.ensure_workspace_exists(h["anna"], "Anna")
    for k in ("anna", "carl", "ben", "mia"):
        S.ensure_personal_space(h[k], k)
        S.add_user_to_household(h[k])
    plan = {"periods": [{"start": "08:00", "end": "08:45"}, {"start": "08:50", "end": "09:35"},
                        {"start": "09:55", "end": "10:40"}, {"start": "", "end": ""}],
            "cells": {"1-0": {"subject": "Mathe", "room": "R 12"},
                      "1-2": {"subject": "Englisch", "room": "R 3"},
                      "1-3": {"subject": "AG Schach", "room": ""},
                      "4-1": {"subject": "Sport", "room": "Halle"}}}
    with get_conn() as conn:
        conn.execute("INSERT INTO timetables (user_id, data) VALUES (?, ?)", (h["ben"], json.dumps(plan)))
        conn.commit()
    return h


def _cal(h, who, **kw):
    from backend.skills.check_calendar.skill import execute
    from backend.skills.registry import Registry, SkillContext
    roles = {"anna": "admin", "carl": "member", "ben": "restricted", "mia": "restricted", "guest": "member"}
    ctx = SkillContext(Registry(), role=roles[who], user_id=h[who])
    kw.setdefault("start_iso", f"{TUESDAY}T10:00:00")
    kw.setdefault("end_iso", f"{TUESDAY}T10:30:00")
    return asyncio.run(execute(ctx, **kw))


def test_a_parent_reads_the_lesson_at_ten(house):
    out = _cal(house, "carl", everyone=True)
    lessons = out["school_timetable"]["lessons"]
    assert [(l["who"], l["subject"], l["room"], l["start"], l["end"]) for l in lessons] == [
        ("Ben", "Englisch", "R 3", "09:55", "10:40"), ("Ben", "AG Schach", "", "", "")]
    assert lessons[0]["weekday"] == "Tuesday" and lessons[0]["period"] == 3
    assert "Ben" in out["school_timetable"]["note"]


def test_default_scope_points_a_parent_to_it(house):
    out = _cal(house, "anna")
    assert "school_timetable" not in out
    assert "Ben" in out["also_reachable"] and "everyone=true" in out["also_reachable"]


def test_the_child_gets_its_own_and_not_its_sisters(house):
    own = _cal(house, "ben", start_iso=TUESDAY, end_iso=f"{TUESDAY}T23:59:59")
    assert [l["subject"] for l in own["school_timetable"]["lessons"]] == ["Mathe", "Englisch", "AG Schach"]
    assert all(l["who"] == "" for l in own["school_timetable"]["lessons"])
    assert "school_timetable" not in _cal(house, "mia", everyone=True)
    assert "also_reachable" not in _cal(house, "mia", everyone=True)


def test_outside_the_household_no_timetable(house):
    out = _cal(house, "guest", everyone=True)
    assert "school_timetable" not in out and "also_reachable" not in out


def test_subject_filter_and_a_whole_week(house):
    out = _cal(house, "anna", everyone=True, start_iso="2026-11-30", end_iso="2026-12-06T23:59:59",
               title_contains="sport")
    assert [(l["date"], l["subject"]) for l in out["school_timetable"]["lessons"]] == [("2026-12-04", "Sport")]


def test_weekend_says_no_lesson(house):
    out = _cal(house, "carl", everyone=True, start_iso="2026-12-05", end_iso="2026-12-05T23:59:59")
    assert out["school_timetable"]["lessons"] == []
    assert "No lesson" in out["school_timetable"]["note"]


def test_events_carry_their_calendar_name(house):
    from backend import calendars as C
    from backend.database import get_conn
    cal = C.create_calendar(name="Stundenplan Ben", owner_user_id=house["anna"])
    with get_conn() as conn:
        conn.execute("INSERT INTO events (title, starts_at, ends_at, calendar_id, owner_user_id) "
                     "VALUES ('Mathe', ?, ?, ?, ?)", (f"{TUESDAY}T10:00:00", f"{TUESDAY}T10:45:00", cal, house["anna"]))
        conn.commit()
    got = [e for e in _cal(house, "anna")["events"] if e["title"] == "Mathe"]
    assert got and got[0]["calendar"] == "Stundenplan Ben"


def test_board_route_still_cleans_the_same_way(house):
    from backend.timetables import clean
    assert clean({"periods": [{"start": "8:00", "end": "08:45"}], "cells": {"0-0": {"subject": " Mathe "}, "5-0": {"subject": "x"}}}) == {
        "periods": [{"start": "", "end": "08:45"}], "cells": {"0-0": {"subject": "Mathe", "room": ""}}}
