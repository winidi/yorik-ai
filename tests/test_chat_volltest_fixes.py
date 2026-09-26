"""Regressions from the chat test on real data (2026-09-26).

Each class names the finding in the test report it guards.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.conftest import seed_user

IDS: dict[str, str] = {}


def _mk_ctx(*, role: str, user_id: str):
    from backend.skills.registry import Registry, SkillContext
    return SkillContext(Registry(), role=role, user_id=user_id)


@pytest.fixture
def person(fresh_app):
    from backend import spaces as _sp
    from backend.calendars import ensure_calendars_for_user
    IDS["dirk"] = seed_user(name="Dirk", role="admin", email="dirk@example.com")
    _sp.ensure_workspace_exists(IDS["dirk"], "Dirk")
    _sp.ensure_personal_space(IDS["dirk"], "Dirk")
    ensure_calendars_for_user(IDS["dirk"], "Dirk")
    return IDS["dirk"]


class TestUndoLastAction:
    """E1: undo_last_action used SQLite's julianday() and failed on Postgres."""

    def test_undo_rolls_back_a_fresh_task(self, person):
        from backend.database import get_conn
        from backend.skills.add_task.skill import execute as add_task
        from backend.skills.undo_last_action.skill import execute as undo

        ctx = _mk_ctx(role="admin", user_id=person)
        created = asyncio.run(add_task(ctx=ctx, title="Müll rausbringen"))
        task_id = created.get("task_id") or created.get("id")
        assert task_id

        result = asyncio.run(undo(ctx=ctx))
        assert result["skill"] == "add_task"
        assert result["undone"]
        assert 0 <= result["age_seconds"] < 60
        with get_conn() as conn:
            assert conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone() is None

    def test_nothing_to_undo(self, person):
        from backend.skills.undo_last_action.skill import execute as undo
        result = asyncio.run(undo(ctx=_mk_ctx(role="admin", user_id=person)))
        assert result["undone"] == ""

    def test_age_from_text_timestamp(self):
        from datetime import datetime, timedelta, timezone
        from backend.skills.undo_last_action.skill import _age_seconds
        ten_min_ago = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        assert 590 <= _age_seconds(ten_min_ago) <= 610
        assert _age_seconds("kaputt") == 0


class TestCheckCalendarRecurring:
    """D2: a weekly series that started before the window was missing."""

    def _add_series(self, owner: str, title: str, starts_at: str, ends_at: str, rec: str) -> int:
        from backend.database import get_conn
        with get_conn() as conn:
            cal = conn.execute(
                "SELECT id, space_id FROM calendars WHERE owner_user_id = ? AND kind = 'personal' LIMIT 1",
                (owner,),
            ).fetchone()
            cur = conn.execute(
                "INSERT INTO events (title, starts_at, ends_at, all_day, calendar_id, owner_user_id, space_id, recurring) "
                "VALUES (?, ?, ?, 0, ?, ?, ?, ?)",
                (title, starts_at, ends_at, cal["id"], owner, cal["space_id"], rec),
            )
            conn.commit()
            return cur.lastrowid

    def test_weekly_instances_inside_window(self, person):
        from backend.skills.check_calendar.skill import execute
        eid = self._add_series(person, "Basketball Training",
                               "2026-09-22T16:00:00", "2026-09-22T17:30:00", "weekly")
        out = asyncio.run(execute(ctx=_mk_ctx(role="admin", user_id=person),
                                  start_iso="2026-09-27T00:00:00", end_iso="2026-10-20T23:59:59"))
        starts = [e["starts_at"] for e in out["events"] if e["id"] == eid]
        assert starts == ["2026-09-29T16:00:00", "2026-10-06T16:00:00",
                          "2026-10-13T16:00:00", "2026-10-20T16:00:00"]
        assert all(e["weekday"] == "Tuesday" for e in out["events"] if e["id"] == eid)

    def test_title_filter_finds_series(self, person):
        from backend.skills.check_calendar.skill import execute
        self._add_series(person, "Basketball Training",
                         "2026-09-24T15:30:00", "2026-09-24T17:00:00", "weekly")
        out = asyncio.run(execute(ctx=_mk_ctx(role="admin", user_id=person),
                                  title_contains="basketball", days=30,
                                  start_iso="2026-09-26T15:00:00"))
        assert [e["date"] for e in out["events"]][:2] == ["2026-10-01", "2026-10-08"]

    def test_base_occurrence_not_doubled(self, person):
        from backend.skills.check_calendar.skill import execute
        self._add_series(person, "Turnen", "2026-09-28T16:00:00", "2026-09-28T17:00:00", "weekly")
        out = asyncio.run(execute(ctx=_mk_ctx(role="admin", user_id=person),
                                  start_iso="2026-09-28T00:00:00", end_iso="2026-10-05T23:59:59"))
        assert [e["starts_at"] for e in out["events"]] == ["2026-09-28T16:00:00", "2026-10-05T16:00:00"]


class TestWhatsAppDraftLid:
    """C4: a contact whose WhatsApp channel is a LID got a made-up phone JID."""

    def test_lid_channel_is_used_as_is(self, person, monkeypatch):
        from backend import contacts as _c
        from backend import whatsapp as wa
        from backend.skills.whatsapp_draft.skill import execute

        cid = _c.create(display_name="Beate", created_by_user_id=person)
        _c.add_channel(cid, kind="whatsapp", value="64373087281262@lid")

        async def fake_llm(prompt, *a, **kw):
            return "Hallo Beate, ich bringe nachher Brot mit."
        monkeypatch.setattr(wa, "_call_llm", fake_llm)
        monkeypatch.setattr(wa, "_calendar_context", lambda **kw: "")

        out = asyncio.run(execute(ctx=_mk_ctx(role="admin", user_id=person),
                                  contact_id=cid, intent="sag, dass ich Brot mitbringe"))
        assert out["chat_jid"] == "64373087281262@lid"


class TestLetterAddressPick:
    """H1: the letter to Kobra carried only 'Peine' as address."""

    def test_complete_address_beats_city_only(self):
        from backend.writing.recipient import from_contact
        contact = {"id": 807, "kind": "business", "display_name": "Kobra Kampfsport Peine e.V.",
                   "addresses": [{"kind": "work", "line1": "", "postcode": "", "city": "Peine"},
                                 {"kind": "work", "line1": "Niedersachsenstraße 14", "postcode": "31226",
                                  "city": "Peine"}]}
        assert from_contact(contact)["address_lines"] == ["Niedersachsenstraße 14", "31226 Peine"]

    def test_kind_order_still_first(self):
        from backend.writing.recipient import from_contact
        contact = {"id": 1, "kind": "person", "display_name": "Oma",
                   "addresses": [{"kind": "work", "line1": "Büroweg 1", "postcode": "10115", "city": "Berlin"},
                                 {"kind": "home", "line1": "", "postcode": "", "city": "Peine"}]}
        assert from_contact(contact)["address_lines"] == ["Peine"]


class TestSearchLinksStayInApp:
    """A11: universal_search handed out http://localhost:8010/... links."""

    def test_paperless_and_immich_links(self, fresh_app, monkeypatch):
        from backend import paperless_ingest, search_routes
        from backend import external_users
        from backend.connectors import immich as immich_mod
        monkeypatch.setattr(external_users, "get_user_paperless_creds", lambda uid: {"token": "x"})
        monkeypatch.setattr(external_users, "get_user_immich_creds", lambda uid: {"key": "x"})
        monkeypatch.setattr(paperless_ingest, "search", lambda q, k, creds_override=None: [
            {"paperless_doc_id": 3, "doc_title": "Kobra", "distance": 0.1,
             "doc_url": "http://localhost:8010/documents/3/",
             "preview_url": "http://localhost:8010/api/documents/3/preview/"}])
        monkeypatch.setattr(immich_mod, "immich", lambda **kw: {"photos": [
            {"id": "abc", "view_url": "http://localhost:2283/photos/abc",
             "thumbnail_url": "/api/photos/abc/thumbnail"}]})
        doc = search_routes._search_paperless("kobra", "u1")[0]
        assert doc["navigate_to"] == "/r/documents?doc=3&source=paperless"
        assert "localhost" not in str(doc)
        photo = search_routes._search_immich("kobra", "u1")[0]
        assert photo["navigate_to"] == "/r/photos?asset=abc"


class TestWhatsAppDraftCardAndIntent:
    """C4: the reply draft ignored the user's intent, and no card appeared."""

    def test_reply_mode_uses_intent_and_shows_card(self, person, monkeypatch):
        from backend import contacts as _c
        from backend import whatsapp as wa
        from backend.database import get_conn
        from backend.skills.whatsapp_draft.skill import execute
        from backend.ui_tools import _pending_ui_actions

        jid = "64373087281262@lid"
        cid = _c.create(display_name="Beate <3", created_by_user_id=person)
        _c.add_channel(cid, kind="whatsapp", value=jid)
        with get_conn() as conn:
            conn.execute("INSERT INTO wa_chats (jid, name, is_group, owner_user_id) VALUES (?, ?, 0, ?)",
                         (jid, "Beate <3", person))
            conn.execute("INSERT INTO wa_messages (msg_id, chat_jid, from_me, timestamp, text, owner_user_id) "
                         "VALUES ('m1', ?, 0, 1790000000, 'Dann am 01.11 um 14 Uhr', ?)", (jid, person))
            conn.commit()

        seen: dict[str, str] = {}

        async def fake_llm(prompt, *a, **kw):
            seen["prompt"] = prompt
            return "Ich bring nachher Brot mit!"
        monkeypatch.setattr(wa, "_call_llm", fake_llm)
        for name in ("_cross_chat_hints", "_semantic_hints", "_paperless_hints"):
            monkeypatch.setattr(wa, name, lambda *a, **kw: [])
        monkeypatch.setattr(wa, "_calendar_context", lambda **kw: "")

        async def run():
            _pending_ui_actions.set([])
            await execute(ctx=_mk_ctx(role="admin", user_id=person),
                          contact_id=cid, intent="sag, dass ich nachher Brot mitbringe")
            return _pending_ui_actions.get()
        actions = asyncio.run(run())
        assert "Brot" in seen["prompt"]
        cards = [a for a in actions if a["type"] == "whatsapp_draft_created"]
        assert cards == [{"type": "whatsapp_draft_created", "chat_jid": jid, "recipient": "Beate <3",
                          "text": "Ich bring nachher Brot mit!", "is_new_chat": False}]


class TestReadLoopIsNoticed:
    """A4/C7: 27 identical universal_search calls went unnoticed because
    invoke_skill counted as mutating."""

    def test_identical_read_skill_call_warns_on_second_repeat(self):
        from backend.agent.guardrails import GuardrailController
        g = GuardrailController()
        args = {"name": "universal_search", "args": {"query": "Kontonummer IBAN"}}
        first = g.after_call("invoke_skill", args, "3 results: …")
        second = g.after_call("invoke_skill", args, "3 results: …")
        assert first.action == "allow"
        assert second.action == "warn" and second.code == "idempotent_no_progress_warning"

    def test_mutating_skill_repeat_stays_quiet(self):
        from backend.agent.guardrails import GuardrailController
        g = GuardrailController()
        args = {"name": "add_task", "args": {"title": "Milch"}}
        g.after_call("invoke_skill", args, "created task 1")
        assert g.after_call("invoke_skill", args, "created task 1").action == "allow"


class TestPartialResultNote:
    """A1/A8/G5: cut results read as complete to the model."""

    def test_document_fits_in_full(self):
        from backend.ui_tools import render_skill_result
        doc = {"ok": True, "doc_id": 1, "text": "Zwischensumme 463,08 EUR\n" * 60 + "Invoice amount 551,07 EUR"}
        out = render_skill_result(doc, skill="read_document")
        assert "551,07" in out and "PARTIAL RESULT" not in out

    def test_cut_rows_carry_the_note_in_front(self):
        from backend.ui_tools import render_skill_result
        rows = {"transactions": [{"counterparty": f"Firma {i}", "amount": -10.0} for i in range(400)],
                "_llm_hint": "400 transaction(s)."}
        out = render_skill_result(rows, skill="show_transactions")
        assert out.startswith("PARTIAL RESULT: you see only 6000 of ")
        assert "400 transaction(s)." in out

    def test_small_result_has_no_note(self):
        from backend.ui_tools import render_skill_result
        assert "PARTIAL" not in render_skill_result({"events": [], "_llm_hint": "none"}, skill="check_calendar")
