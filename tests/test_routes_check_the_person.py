"""Routes that took `user` and never looked at it (audit
docs/audits/2026-09-22-berechtigungen.md, package 4: 2.9–2.11, 2.17,
2.18, 3.2, 3.3, 3.9, 3.10). Another person's row answers 404 on a read
and 403 on a change, like PATCH /api/contacts/{id} always did."""

from __future__ import annotations

from datetime import date

import pytest

from tests.conftest import login_client


@pytest.fixture
def household(fresh_app):
    from backend import spaces as S
    from backend.calendars import ensure_calendars_for_user
    from backend.database import get_conn
    dirk_c, dirk = login_client(fresh_app, role="platform_admin", name="Dirk", email="d@example.local")
    beate_c, beate = login_client(fresh_app, role="member", name="Beate", email="b@example.local")
    S.ensure_workspace_exists(dirk, "Dirk")
    for uid, name in ((dirk, "Dirk"), (beate, "Beate")):
        S.ensure_personal_space(uid, name); ensure_calendars_for_user(uid, name)
    today = date.today().isoformat()
    with get_conn() as conn:
        cid = conn.execute("INSERT INTO contacts (display_name, kind, status, created_by_user_id, space_id) "
                           "VALUES ('Dirks Kontakt', 'person', 'pending', ?, ?) RETURNING id",
                           (dirk, S.personal_space_id(dirk))).fetchone()["id"]
        chid = conn.execute("INSERT INTO contact_channels (contact_id, kind, value) VALUES (?, 'email', 'dk@example.org') RETURNING id",
                            (cid,)).fetchone()["id"]
        cal = conn.execute("SELECT id FROM calendars WHERE owner_user_id=? AND kind='personal'", (dirk,)).fetchone()["id"]
        eid = conn.execute("INSERT INTO events (title, starts_at, ends_at, all_day, calendar_id, owner_user_id) "
                           "VALUES ('Zahnarzt', ?, ?, 0, ?, ?) RETURNING id",
                           (f"{today}T15:00:00", f"{today}T16:00:00", cal, dirk)).fetchone()["id"]
        bid = conn.execute("INSERT INTO bills (name, amount, due_date, space_id) VALUES ('Strom', 80, ?, ?) RETURNING id",
                           (today, S.personal_space_id(dirk))).fetchone()["id"]
        conn.execute("INSERT INTO wa_drafts (chat_jid, draft_text, owner_user_id, status) VALUES ('491700000000@s.whatsapp.net', 'Hallo', ?, 'pending')",
                     (dirk,))
        conn.commit()
    return {"dirk": (dirk_c, dirk), "beate": (beate_c, beate), "contact": cid, "channel": chid,
            "event": eid, "calendar": cal, "bill": bid}


def test_contact_satellites_follow_the_contact_gate(household):
    beate_c, _ = household["beate"]; dirk_c, _ = household["dirk"]; cid = household["contact"]
    # reads: a contact outside the person's view looks missing
    for path in (f"/api/contacts/{cid}/timeline", f"/api/contacts/{cid}/proposals",
                 f"/api/contacts/{cid}/address-suggestions", f"/api/contacts/{cid}/birthday-suggestion",
                 f"/api/contacts/{cid}/email-suggestions", "/api/contacts/by-channel/email/dk@example.org"):
        assert beate_c.get(path).status_code == 404, path
    assert beate_c.get("/api/contacts/_counts").json()["pending"] == 0
    assert dirk_c.get("/api/contacts/_counts").json()["pending"] == 1
    # writes: refused
    assert beate_c.post(f"/api/contacts/{cid}/promote").status_code == 403
    assert beate_c.post(f"/api/contacts/{cid}/pin", json={"pinned": True}).status_code == 403
    assert beate_c.post(f"/api/contacts/{cid}/spam").status_code == 403
    assert beate_c.post(f"/api/contacts/{cid}/channels", json={"kind": "phone", "value": "+491111"}).status_code == 403
    assert beate_c.post(f"/api/contacts/{cid}/addresses", json={"kind": "home", "line1": "Weg 1", "city": "Peine", "postcode": "31224"}).status_code == 403
    assert beate_c.delete(f"/api/contacts/channels/{household['channel']}").status_code == 403
    assert beate_c.post(f"/api/contacts/{cid}/proposals/decide", json={"proposal_id": 1, "decision": "rejected"}).status_code == 403
    # the owner keeps working
    assert dirk_c.get(f"/api/contacts/{cid}/timeline").status_code == 200
    assert dirk_c.post(f"/api/contacts/{cid}/pin", json={"pinned": True}).status_code == 200
    assert dirk_c.post(f"/api/contacts/{cid}/promote").json()["status"] == "active"
    assert dirk_c.get("/api/contacts/by-channel/email/dk@example.org").json()["id"] == cid
    assert dirk_c.delete(f"/api/contacts/channels/{household['channel']}").status_code == 204


def test_bills_attendees_and_calendar_shares_check_the_person(household):
    beate_c, _ = household["beate"]; dirk_c, _ = household["dirk"]
    assert beate_c.patch(f"/api/bills/{household['bill']}", json={"paid": True}).status_code == 403
    assert dirk_c.patch(f"/api/bills/{household['bill']}", json={"paid": True}).json()["paid"] == 1
    assert beate_c.get(f"/api/events/{household['event']}/attendees").status_code == 404
    assert dirk_c.get(f"/api/events/{household['event']}/attendees").status_code == 200
    assert beate_c.get(f"/api/calendars/{household['calendar']}/shares").status_code == 404
    assert dirk_c.get(f"/api/calendars/{household['calendar']}/shares").status_code == 200


def test_whatsapp_drafts_and_media_are_the_owners(household):
    from backend.database import get_conn
    beate_c, _ = household["beate"]; dirk_c, _ = household["dirk"]
    jid = "491700000000@s.whatsapp.net"
    assert beate_c.post(f"/api/whatsapp/drafts/{jid}/discard").json() == {"discarded": 0}
    with get_conn() as conn:
        assert conn.execute("SELECT status FROM wa_drafts WHERE chat_jid=?", (jid,)).fetchone()["status"] == "pending"
    assert dirk_c.post(f"/api/whatsapp/drafts/{jid}/discard").json() == {"discarded": 1}
    r = beate_c.post(f"/api/whatsapp/messages/abc/reprocess?chat_jid={jid}")
    assert r.status_code == 200 and r.json() == {"ok": False, "error": "message not found"}
    for path in ("/api/whatsapp/settings", "/api/whatsapp/semantic-status", "/api/whatsapp/bridge/info"):
        assert beate_c.get(path).status_code == 403, path


def test_n8n_is_for_admins(household):
    beate_c, _ = household["beate"]
    assert beate_c.get("/n8n/").status_code == 403
