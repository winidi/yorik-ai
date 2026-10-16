"""One person, two WhatsApp rows: the alias map makes a proposal, a
person decides (2026-10-16)."""
from __future__ import annotations

from tests.conftest import login_client


def _mk(client, name, kind, value):
    r = client.post("/api/contacts", json={"display_name": name, "kind": "person"})
    cid = r.json()["id"]
    r2 = client.post(f"/api/contacts/{cid}/channels", json={"kind": kind, "value": value})
    assert r2.status_code == 201, r2.text
    return cid


def test_lid_and_number_rows_become_one_proposal_not_a_merge(fresh_app, monkeypatch):
    client, uid = login_client(fresh_app, role="platform_admin", name="Dirk", email="d@example.local")
    number_id = _mk(client, "Beate <3", "whatsapp", "4917637995916@s.whatsapp.net")
    lid_id = _mk(client, "Beate", "whatsapp", "64373087281262@lid")
    other = _mk(client, "Jan", "whatsapp", "4915111111111@s.whatsapp.net")
    from backend import whatsapp_aliases as A, contact_twins as T
    monkeypatch.setattr(A, "lid_to_number", lambda user_id: {"64373087281262@lid": "4917637995916@s.whatsapp.net",
                                                               "999@lid": "4915100000000@s.whatsapp.net"})
    out = T.propose_twins(uid)
    assert out == {"pairs": 1, "proposals": 1}
    assert T.propose_twins(uid)["proposals"] == 0  # no duplicate
    # both rows still exist: nothing merged
    assert client.get(f"/api/contacts/{lid_id}").status_code == 200
    props = client.get("/api/contacts/proposals").json()["proposals"]
    p = [x for x in props if x["kind"] == "merge"][-1]
    assert {p["contact_id"], p["other_contact_id"]} == {number_id, lid_id}
    assert "same account" in p["reason"]
    assert T.twins_among(uid, [number_id, lid_id, other]) == [(number_id, lid_id)]
    assert T.twins_among(uid, [number_id, other]) == []
    # the person accepts: one contact with both channels, undoable
    r = client.post(f"/api/contacts/proposals/{p['id']}/accept", json={})
    assert r.status_code == 200, r.text
    kept = client.get(f"/api/contacts/{number_id}").json()
    assert {c["value"] for c in kept["channels"]} >= {"4917637995916@s.whatsapp.net", "64373087281262@lid"}


def test_household_numbers_never_become_merge_reasons(fresh_app, monkeypatch):
    client, uid = login_client(fresh_app, role="platform_admin", name="Dirk", email="d@example.local")
    from backend.database import get_conn
    with get_conn() as conn:
        conn.execute("UPDATE user_profiles SET phone = ? WHERE id = ?", ("+49 151 28811000", uid))
        conn.commit()
    from backend import contact_identity as I
    nums = I.household_numbers()
    assert "+4915128811000" in nums


def test_a_group_only_whatsapp_contact_is_last_and_never_padding(fresh_app):
    client, uid = login_client(fresh_app, role="platform_admin", name="Dirk", email="d@example.local")
    from backend import contacts as C
    from backend.database import get_conn
    stranger = C.create(display_name="Fremder aus Gruppe", kind="person", status="active", source="wa_sync",
                        created_by_user_id=uid)
    C.add_channel(stranger, kind="whatsapp", value="4915100000001@s.whatsapp.net", source="wa_sync")
    friend = C.create(display_name="Fremder Freund", kind="person", status="active", source="wa_sync",
                      created_by_user_id=uid)
    C.add_channel(friend, kind="whatsapp", value="4915100000002@s.whatsapp.net", source="wa_sync")
    with get_conn() as conn:
        conn.execute("INSERT INTO wa_chats (jid, name, is_group, last_message_ts, owner_user_id) VALUES (?, ?, 0, 1760000000, ?)",
                     ("4915100000002@s.whatsapp.net", "Fremder Freund", uid))
        conn.commit()
    assert C.is_whatsapp_stranger(C.get(stranger)) is True
    assert C.is_whatsapp_stranger(C.get(friend)) is False
    import asyncio
    from backend.skills.registry import Registry, SkillContext
    from backend.skills.find_person.skill import execute as find
    out = asyncio.run(find(SkillContext(Registry(), role="platform_admin", user_id=uid), query="Fremder", source="contacts"))
    names = [r["display_name"] for r in out["results"]]
    assert names[-1] == "Fremder aus Gruppe" and out["results"][-1].get("whatsapp_stranger") is True


def test_the_person_card_offers_to_save_a_sender(fresh_app):
    client, uid = login_client(fresh_app, role="platform_admin", name="Dirk", email="d@example.local")
    d = client.get("/api/people/neu@example.local").json()
    assert d["contact"] is None and d["channel"] == {"kind": "email", "value": "neu@example.local"}
    cid = client.post("/api/contacts", json={"display_name": "Neu", "kind": "person"}).json()["id"]
    client.post(f"/api/contacts/{cid}/channels", json={"kind": "email", "value": "neu@example.local"})
    d = client.get("/api/people/neu@example.local").json()
    assert d["contact"]["id"] == cid
