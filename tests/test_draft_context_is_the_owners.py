"""The WhatsApp semantic index and the suggestion retrievers feed one
person's context, not the household's (audit
docs/audits/2026-09-22-berechtigungen.md, package 9: 3.4, 3.6)."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta

import pytest

from tests.conftest import login_client


@pytest.fixture
def two_people(fresh_app):
    from backend import spaces as S
    from backend.calendars import ensure_calendars_for_user
    from backend.database import get_conn
    _, dirk = login_client(fresh_app, role="platform_admin", name="Dirk", email="d@example.local")
    _, beate = login_client(fresh_app, role="member", name="Beate", email="b@example.local")
    S.ensure_workspace_exists(dirk, "Dirk")
    for uid, name in ((dirk, "Dirk"), (beate, "Beate")):
        S.ensure_personal_space(uid, name); ensure_calendars_for_user(uid, name)
    with get_conn() as conn:
        for uid, who, text in ((dirk, "Anna", "Dirk, kommst du Freitag zum Grillen?"),
                               (beate, "Anna", "Beate, bringst du den Salat mit?")):
            conn.execute("INSERT INTO wa_chats (jid, name, is_group, owner_user_id) VALUES ('4917@s.whatsapp.net', ?, 0, ?) "
                         "ON CONFLICT DO NOTHING", (who, uid))
            conn.execute("INSERT INTO wa_messages (chat_jid, msg_id, owner_user_id, from_me, text, timestamp, push_name) "
                         "VALUES ('4917@s.whatsapp.net', ?, ?, 0, ?, 1758500000, 'Anna')", (f"m-{uid[:8]}", uid, text))
        conn.commit()
    return dirk, beate


def test_the_semantic_index_is_searched_per_owner(two_people, monkeypatch):
    from backend import whatsapp_semantic as WS
    dirk, beate = two_people
    monkeypatch.setattr(WS, "ollama_reachable", lambda: True)
    monkeypatch.setattr(WS, "embed", lambda text: [1.0] + [0.0] * 383)
    r = WS.backfill()
    assert r["indexed"] == 2, r
    assert [h["text"] for h in WS.search("Grillen", owner_user_id=dirk)] == ["Dirk, kommst du Freitag zum Grillen?"]
    assert [h["text"] for h in WS.search("Grillen", owner_user_id=beate)] == ["Beate, bringst du den Salat mit?"]
    assert WS.search("Grillen") == []                                                  # no person, nothing
    assert WS.index_message("x", "4917@s.whatsapp.net", "ohne Besitzer, lang genug", 1, owner_user_id=None) is False


def test_suggestion_retrievers_read_the_owners_rows(two_people):
    from backend import spaces as S
    from backend.database import get_conn
    from backend.suggestions.registry import RetrieverContext
    from backend.suggestions.retrievers import calendar as RC, tasks as RT, email_history as RE
    dirk, beate = two_people
    start = datetime.now() + timedelta(days=1)
    with get_conn() as conn:
        cid = conn.execute("INSERT INTO contacts (display_name, kind, status, created_by_user_id, space_id) "
                           "VALUES ('Anna Beispiel', 'person', 'active', ?, ?) RETURNING id",
                           (beate, S.personal_space_id(beate))).fetchone()["id"]
        conn.execute("INSERT INTO contact_channels (contact_id, kind, value) VALUES (?, 'email', 'anna@example.org')", (cid,))
        for uid in (dirk, beate):
            cal = conn.execute("SELECT id FROM calendars WHERE owner_user_id=? AND kind='personal'", (uid,)).fetchone()["id"]
            conn.execute("INSERT INTO events (title, starts_at, ends_at, all_day, calendar_id, owner_user_id, person) "
                         "VALUES (?, ?, ?, 0, ?, ?, 'Anna Beispiel')",
                         (f"Kaffee mit Anna ({uid[:4]})", start.isoformat(), (start + timedelta(hours=1)).isoformat(), cal, uid))
            conn.execute("INSERT INTO tasks (title, done, created_by_user_id, space_id) VALUES (?, 0, ?, ?)",
                         (f"Anna Beispiel anrufen ({uid[:4]})", uid, S.personal_space_id(uid)))
            aid = conn.execute("INSERT INTO email_accounts (owner_user_id, email, imap_host, imap_username, smtp_host, smtp_username, credential_key) "
                               "VALUES (?, ?, 'i', 'u', 's', 'u', 'k') RETURNING id", (uid, f"{uid[:4]}@example.local")).fetchone()["id"]
            conn.execute("INSERT INTO email_messages (account_id, uid, owner_user_id, message_id, from_email, subject, body_text, snippet) "
                         "VALUES (?, 1, ?, ?, 'anna@example.org', ?, '', ?)",
                         (aid, uid, f"<m-{uid[:4]}@x>", f"Betreff für {uid[:4]}", f"Betreff für {uid[:4]}"))
        conn.commit()
    ctx = RetrieverContext(owner_user_id=beate, source_kind="wa", source_id=0, contact_id=cid)
    assert [e.snippet.split(" · ")[0] for e in asyncio.run(RC._fetch(ctx))] == [f"Kaffee mit Anna ({beate[:4]})"]
    assert [e.snippet for e in asyncio.run(RT._fetch(ctx)) if "anrufen" in e.snippet] and \
        all(dirk[:4] not in e.snippet for e in asyncio.run(RT._fetch(ctx)))
    assert all(dirk[:4] not in e.snippet for e in asyncio.run(RE._fetch(ctx))) and asyncio.run(RE._fetch(ctx))
