"""The name Yorik shows for a WhatsApp chat is the one WhatsApp shows.

WhatsApp picks it from a fixed order of sources — the phone's address
book, then a verified business name, then the name the other person
chose for themselves. Yorik showed whatever arrived last, which was
almost always that self-chosen pushName: on 2026-09-22, 21 of the 37
visible one-to-one chats carried one and not a single one carried an
address-book name. Plan: docs/plans/2026-09-22-whatsapp-namen.md.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.conftest import login_client


@pytest.fixture
def dirk(fresh_app):
    client, uid = login_client(fresh_app, role="platform_admin", name="Dirk",
                               email="d@example.local")
    return client, uid


def _chat(jid: str):
    from backend.database import get_conn
    with get_conn() as conn:
        return conn.execute(
            "SELECT name, name_source FROM wa_chats WHERE jid=?", (jid,)
        ).fetchone()


def _put_chat(jid: str, owner: str, *, name=None, source=None, ts=None):
    from backend.database import get_conn
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO wa_chats (jid, name, name_source, is_group, last_message_ts, "
            "owner_user_id) VALUES (?, ?, ?, 0, ?, ?)",
            (jid, name, source, ts, owner),
        )
        conn.commit()


NUMBER = "4915123456789@s.whatsapp.net"
LID = "22227383536847@lid"


def test_a_pushname_never_replaces_the_address_book_name(dirk):
    _, uid = dirk
    from backend.whatsapp import _upsert_chat
    _upsert_chat(jid=NUMBER, name="Elena Müller", name_source="book", is_group=False,
                 ts=100, last_text="hi", owner_user_id=uid)
    _upsert_chat(jid=NUMBER, name="Ela ✨", name_source="push", is_group=False,
                 ts=200, last_text="hi again", owner_user_id=uid)
    row = _chat(NUMBER)
    assert row["name"] == "Elena Müller"
    assert row["name_source"] == "book"


def test_the_address_book_replaces_a_pushname(dirk):
    _, uid = dirk
    from backend.whatsapp import _upsert_chat
    _upsert_chat(jid=NUMBER, name="Ela ✨", name_source="push", is_group=False,
                 ts=100, last_text="hi", owner_user_id=uid)
    _upsert_chat(jid=NUMBER, name="Elena Müller", name_source="book", is_group=False,
                 ts=None, last_text=None, owner_user_id=uid)
    row = _chat(NUMBER)
    assert row["name"] == "Elena Müller"
    assert row["name_source"] == "book"


def test_a_rename_on_the_phone_arrives(dirk):
    _, uid = dirk
    from backend.whatsapp import _upsert_chat
    _upsert_chat(jid=NUMBER, name="Elena Müller", name_source="book", is_group=False,
                 ts=100, last_text="hi", owner_user_id=uid)
    _upsert_chat(jid=NUMBER, name="Elena Schmidt", name_source="book", is_group=False,
                 ts=110, last_text="hi", owner_user_id=uid)
    assert _chat(NUMBER)["name"] == "Elena Schmidt"


def test_our_own_message_does_not_rename_the_chat(dirk):
    """An outgoing message carries the user's own pushName. Learning it
    is how six people in this household came to be called "Dirk"."""
    _, uid = dirk
    from backend.whatsapp import _insert_message
    _insert_message({"jid": NUMBER, "id": "m1", "text": "bin unterwegs",
                     "timestamp": 100, "fromMe": True, "pushName": "Dirk"}, uid)
    assert not (_chat(NUMBER)["name"] or "")


def test_a_group_is_not_named_after_whoever_spoke(dirk):
    _, uid = dirk
    from backend.whatsapp import _insert_message
    group = "120363000000000000@g.us"
    _put_chat(group, uid, name="Familie", source="book", ts=50)
    _insert_message({"jid": group, "id": "m2", "text": "bin da", "timestamp": 100,
                     "fromMe": False, "pushName": "Ela ✨",
                     "participant": LID}, uid)
    assert _chat(group)["name"] == "Familie"


def test_an_incoming_message_still_names_a_nameless_chat(dirk):
    _, uid = dirk
    from backend.whatsapp import _insert_message
    _insert_message({"jid": NUMBER, "id": "m3", "text": "hallo", "timestamp": 100,
                     "fromMe": False, "pushName": "Ela ✨"}, uid)
    row = _chat(NUMBER)
    assert row["name"] == "Ela ✨"
    assert row["name_source"] == "push"


def test_a_contact_names_the_chat_it_actually_runs_in(dirk):
    """The conversation runs under the LID, the address-book name
    arrives under the phone number. The contact event ties them."""
    _, uid = dirk
    from backend.whatsapp import _handle_event
    _put_chat(LID, uid, name="Ela ✨", source="push", ts=100)
    asyncio.run(_handle_event({
        "type": "contact", "userId": uid,
        "payload": {"jid": NUMBER, "name": "Elena Müller",
                    "nameSource": "book", "lid": LID},
    }))
    row = _chat(LID)
    assert row["name"] == "Elena Müller"
    assert row["name_source"] == "book"


def test_a_contact_never_creates_a_chat(dirk):
    """509 of the 532 number-addressed chats in this household had never
    carried a message: they were address-book entries sent as chats."""
    _, uid = dirk
    from backend.whatsapp import _handle_event
    asyncio.run(_handle_event({
        "type": "contact", "userId": uid,
        "payload": {"jid": NUMBER, "name": "Elena Müller", "nameSource": "book"},
    }))
    assert _chat(NUMBER) is None


def test_the_list_leaves_out_rows_that_never_carried_a_message(dirk):
    client, uid = dirk
    _put_chat(NUMBER, uid, name="Elena Müller", source="book", ts=None)
    _put_chat(LID, uid, name="Elena Müller", source="book", ts=1700000000)
    rows = client.get("/api/whatsapp/chats").json()
    assert [r["jid"] for r in rows] == [LID]
    assert rows[0]["name_source"] == "book"


def test_the_list_leaves_out_a_nameless_stub(dirk):
    """After the re-pairing on 2026-09-25 the history sync handed over
    84 rows with a timestamp, no name and nothing in them — all they
    could show is a 15-digit LID."""
    client, uid = dirk
    _put_chat("99999999999999@lid", uid, name=None, source=None, ts=1700000000)
    _put_chat(LID, uid, name="Elena Müller", source="book", ts=1700000001)
    rows = client.get("/api/whatsapp/chats").json()
    assert [r["jid"] for r in rows] == [LID]


def test_a_nameless_chat_with_messages_stays(dirk):
    """Hiding it would put a real conversation out of reach."""
    client, uid = dirk
    from backend.whatsapp import _insert_message
    stub = "88888888888888@lid"
    _put_chat(stub, uid, name=None, source=None, ts=1700000000)
    _insert_message({"jid": stub, "id": "m9", "text": "hallo?", "timestamp": 1700000000,
                     "fromMe": False, "pushName": None}, uid)
    rows = client.get("/api/whatsapp/chats").json()
    assert stub in [r["jid"] for r in rows]


def _put_contact(owner: str, display_name: str, jid: str) -> int:
    """A contact with one WhatsApp channel, the way autocapture makes it."""
    from backend import spaces as S
    from backend.database import get_conn
    with get_conn() as conn:
        cid = conn.execute(
            "INSERT INTO contacts (display_name, kind, status, created_by_user_id, space_id) "
            "VALUES (?, 'person', 'active', ?, ?) RETURNING id",
            (display_name, owner, S.personal_space_id(owner)),
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO contact_channels (contact_id, kind, value, source) "
            "VALUES (?, 'whatsapp', ?, 'wa_sync')",
            (cid, jid),
        )
        conn.commit()
    return int(cid)


@pytest.fixture
def bridge_says(monkeypatch):
    """Answer for the bridge without one running."""
    import requests

    def _install(names: dict, aliases: dict | None = None):
        class _Resp:
            status_code = 200
            text = "{}"

            def json(self):
                return {"names": names, "aliases": aliases or {},
                        "contacts": {j: v["name"] for j, v in names.items()}}

        monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())
        monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp())
    return _install


def test_the_backfill_renames_the_chat_the_conversation_runs_in(dirk, bridge_says):
    """The name sits on the number, the chat runs under the LID. Until
    2026-09-24 the backfill only touched contacts, so the list never
    changed and the run looked like it had done nothing."""
    _, uid = dirk
    from backend import spaces as S
    S.ensure_personal_space(uid, "Dirk")
    _put_chat(LID, uid, name="Ela ✨", source="push", ts=1700000000)
    _put_contact(uid, "22227383536847", LID)
    bridge_says({LID: {"name": "Elena Müller", "source": "book"}}, {LID: NUMBER})

    from backend.contact_autocapture import backfill_whatsapp_display_names
    result = backfill_whatsapp_display_names(owner_user_id=uid)

    assert result["updated_chats"] == 1
    assert _chat(LID)["name"] == "Elena Müller"
    assert _chat(LID)["name_source"] == "book"
    # The contact was still showing the raw number, so it gets the name too.
    assert result["updated_contacts"] == 1


def test_the_backfill_leaves_a_stronger_name_alone(dirk, bridge_says):
    _, uid = dirk
    from backend import spaces as S
    S.ensure_personal_space(uid, "Dirk")
    _put_chat(LID, uid, name="Elena Müller", source="book", ts=1700000000)
    _put_contact(uid, "Mama", LID)
    bridge_says({LID: {"name": "Ela ✨", "source": "push"}})

    from backend.contact_autocapture import backfill_whatsapp_display_names
    result = backfill_whatsapp_display_names(owner_user_id=uid)

    assert result["updated_chats"] == 0
    assert _chat(LID)["name"] == "Elena Müller"
    # A name the user typed is never overwritten, whatever WhatsApp says.
    from backend.database import get_conn
    with get_conn() as conn:
        assert conn.execute("SELECT display_name FROM contacts").fetchone()["display_name"] == "Mama"
