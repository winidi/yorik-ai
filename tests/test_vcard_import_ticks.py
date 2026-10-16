"""The import preview is where the person decides: a tick per card, the
card's name on a conflict only when ticked, and a card that matches two
rows makes a merge proposal instead of a merge (2026-10-16)."""
from __future__ import annotations

from tests.conftest import login_client

VCF = """BEGIN:VCARD
VERSION:3.0
FN:Beate Mayer
TEL;TYPE=CELL:+49 176 37995916
EMAIL:beatemayer1@gmx.net
END:VCARD
BEGIN:VCARD
VERSION:3.0
FN:Neue Person
EMAIL:neu@example.local
END:VCARD
"""


def _mk(client, name, kind, value, status="active"):
    cid = client.post("/api/contacts", json={"display_name": name, "kind": "person"}).json()["id"]
    assert client.post(f"/api/contacts/{cid}/channels", json={"kind": kind, "value": value}).status_code == 201
    return cid


def test_a_card_matching_two_rows_makes_a_proposal_not_a_merge(fresh_app):
    client, uid = login_client(fresh_app, role="platform_admin", name="Dirk", email="d@example.local")
    wa = _mk(client, "Beate <3", "whatsapp", "4917637995916@s.whatsapp.net")
    mail = _mk(client, "Beate Mayer", "email", "beatemayer1@gmx.net")
    plan = client.post("/api/contacts/import/preview", json={"text": VCF}).json()
    e0, e1 = plan["entries"]
    assert e0["outcome"] in ("merge", "name_conflict")
    assert {e0["existing_id"], *[a["id"] for a in e0["also_matches"]]} == {wa, mail}
    assert e1["outcome"] == "new"
    # the tick is the decision: untick the new card, tick the conflict (if any)
    e0["use_card_name"] = True
    e1["selected"] = False
    r = client.post("/api/contacts/import/apply", json={"plan": plan, "target_status": "active"}).json()
    assert r["created"] == 0 and r["merged"] == 1 and r["skipped"] == 1
    assert client.get(f"/api/contacts/{wa}").status_code == 200
    assert client.get(f"/api/contacts/{mail}").status_code == 200  # both still there
    props = [p for p in client.get("/api/contacts/proposals").json()["proposals"] if p["kind"] == "merge"]
    assert {props[-1]["contact_id"], props[-1]["other_contact_id"]} == {wa, mail}
    assert "imported card" in props[-1]["reason"]


def test_a_name_conflict_takes_the_cards_name_only_when_ticked(fresh_app):
    client, uid = login_client(fresh_app, role="platform_admin", name="Dirk", email="d@example.local")
    wa = _mk(client, "Beate <3", "whatsapp", "4917637995916@s.whatsapp.net")
    card = "BEGIN:VCARD\nVERSION:3.0\nFN:Beate Mayer\nTEL;TYPE=CELL:+49 176 37995916\nEND:VCARD\n"
    plan = client.post("/api/contacts/import/preview", json={"text": card}).json()
    e = plan["entries"][0]
    assert e["outcome"] == "name_conflict" and e["existing_id"] == wa
    r = client.post("/api/contacts/import/apply", json={"plan": plan, "target_status": "active"}).json()
    assert r["skipped"] == 1 and client.get(f"/api/contacts/{wa}").json()["display_name"] == "Beate <3"
    e["use_card_name"] = True
    r = client.post("/api/contacts/import/apply", json={"plan": plan, "target_status": "active"}).json()
    assert r["merged"] == 1 and client.get(f"/api/contacts/{wa}").json()["display_name"] == "Beate Mayer"
