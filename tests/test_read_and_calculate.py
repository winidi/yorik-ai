"""whatsapp_read, calculate, date_info (chat test 2026-09-26 follow-up)."""

from __future__ import annotations

import asyncio

import pytest

from tests.conftest import seed_user


def _ctx(uid, role="admin"):
    from backend.skills.registry import Registry, SkillContext
    return SkillContext(Registry(), role=role, user_id=uid)


@pytest.fixture
def chat(fresh_app):
    from backend.database import get_conn
    me = seed_user(name="Max", role="admin", email="max@example.com")
    other = seed_user(name="Erika", role="member", email="erika@example.com")
    jid = "491770000000@s.whatsapp.net"
    msgs = [("m1", 0, 1767990000, "DE32500105175422716331"),
            ("m2", 0, 1768130000, "Sprawdzilam Konto."),
            ("m3", 1, 1768140000, "Danke!"),
            ("m4", 0, 1782900000, "Ich musste nach einem Betrugsanruf das Konto wechseln"),
            ("m5", 1, 1790000000, "Ok")]
    ids = {}
    with get_conn() as conn:
        conn.execute("INSERT INTO wa_chats (jid, name, is_group, owner_user_id) VALUES (?, 'Mama', 0, ?)", (jid, me))
        for mid, from_me, ts, text in msgs:
            ids[mid] = conn.execute(
                "INSERT INTO wa_messages (msg_id, chat_jid, from_me, push_name, timestamp, text, owner_user_id) "
                "VALUES (?, ?, ?, 'Mama', ?, ?, ?) RETURNING id", (mid, jid, from_me, ts, text, me)).fetchone()["id"]
        conn.commit()
    return {"me": me, "other": other, "jid": jid, "ids": ids}


def test_read_around_a_hit(chat):
    from backend.skills.whatsapp_read.skill import execute
    out = asyncio.run(execute(ctx=_ctx(chat["me"]), around_message_id=chat["ids"]["m1"], before=2, after=2))
    assert [m["text"] for m in out["messages"]] == ["DE32500105175422716331", "Sprawdzilam Konto.", "Danke!"]
    assert out["messages"][0]["hit"] is True and out["messages"][0]["who"] == "Mama"
    assert out["messages"][2]["who"] == "ich"
    assert out["messages_after_this_window"] == 2      # the later account change is flagged
    assert "still current" in out["_llm_hint"]


def test_latest_messages(chat):
    from backend.skills.whatsapp_read.skill import execute
    out = asyncio.run(execute(ctx=_ctx(chat["me"]), chat_jid=chat["jid"], last=2))
    assert [m["text"] for m in out["messages"]] == ["Ich musste nach einem Betrugsanruf das Konto wechseln", "Ok"]


def test_someone_elses_chat_is_not_readable(chat):
    from backend.skills.whatsapp_read.skill import execute
    with pytest.raises(ValueError):
        asyncio.run(execute(ctx=_ctx(chat["other"], "member"), around_message_id=chat["ids"]["m1"]))
    with pytest.raises(ValueError):
        asyncio.run(execute(ctx=_ctx(chat["other"], "member"), chat_jid=chat["jid"]))


def test_ics_parsing():
    from backend.skills.whatsapp_read.skill import parse_ics
    ics = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:Elternabend Kita\r\nDTSTART:20261101T130000Z\r\n"
           "LOCATION:Kita Sonnenschein\\, Peine\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    assert parse_ics(ics) == {"summary": "Elternabend Kita", "dtstart": "01.11.2026 14:00",
                              "location": "Kita Sonnenschein, Peine"}


@pytest.mark.parametrize("expr,expected", [
    ("551,07 / 12", 45.92), ("550 + 1200 + 300 + 300 + 1000 + 200 + 300", 3850.0),
    ("1.234,50 * 2", 2469.0), ("sum([1, 2, 3])", 6.0), ("round(214.20 / 1.19, 2)", 180.0),
])
def test_calculate(expr, expected):
    from backend.skills.calculate.skill import execute
    assert asyncio.run(execute(ctx=None, expression=expr))["result"] == expected


@pytest.mark.parametrize("bad", ["__import__('os').system('x')", "open('/etc/passwd')", "(1).real", "a + 1",
                                 "2 ** 1000", "1 / 0"])
def test_calculate_refuses_anything_but_arithmetic(bad):
    from backend.skills.calculate.skill import execute
    with pytest.raises(ValueError):
        asyncio.run(execute(ctx=None, expression=bad))


def test_date_info():
    from backend.skills.date_info.skill import execute
    out = asyncio.run(execute(ctx=None, date="2026-10-02"))
    assert out["result"]["weekday"] == "Freitag"
    out = asyncio.run(execute(ctx=None, date="2026-12-31", add_weeks=-4))
    assert out["result"]["shown"] == "Donnerstag, 03.12.2026"
    out = asyncio.run(execute(ctx=None, date="2026-01-31", add_months=1, until="2026-03-01"))
    assert out["result"]["date"] == "2026-02-28" and out["days_between"] == 29


def test_skills_load_and_rule_is_in_the_prompt(fresh_app):
    """The manifests parse, and the look-it-up rule (≤ 200 chars, Dirk)
    sits in the operating rules."""
    from backend.skills.registry import get_registry
    from backend.ask import _SYSTEM_PROMPT
    reg = get_registry()
    for name in ("whatsapp_read", "calculate", "date_info"):
        assert reg.get(name) is not None, name
    line = next(l for l in _SYSTEM_PROMPT.splitlines() if l.startswith("Facts about the user's data"))
    assert len(line) <= 200
