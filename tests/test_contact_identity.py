"""Contact identity: one rule for "same person", proposals, undoable merges."""

from __future__ import annotations

import asyncio
import json

import pytest

from tests.conftest import login_client, seed_user


def _mk(name, **channels):
    from backend import contacts as C
    cid = C.create(display_name=name, kind="person", status=channels.pop("status", "active"),
                   source="manual")
    for kind, value in channels.items():
        C.add_channel(cid, kind=kind, value=value, source="manual")
    return cid


def test_phone_normalisation_and_cross_kind_lookup(fresh_app):
    from backend import contact_identity as I, contacts as C
    assert I.to_e164("0511 / 12 34 56") == "+49511123456"
    assert I.to_e164("+49 151 23456789") == "+4915123456789"
    assert I.to_e164("4915123456789@s.whatsapp.net") == "+4915123456789"
    assert I.to_e164("222273835368470@lid") is None
    assert I.to_e164("hello") is None
    # channels are stored in E.164 now
    cid = _mk("Anna", phone="0151 23456789")
    assert C.get(cid)["channels"][0]["value"] == "+4915123456789"
    # a WhatsApp id finds the phone-only contact and vice versa
    assert I.owner_of("whatsapp", "4915123456789@s.whatsapp.net")["id"] == cid
    wa = _mk("Bob", whatsapp="4917612345678@s.whatsapp.net")
    assert I.owner_of("phone", "0176 12345678")["id"] == wa
    assert I.owner_of("phone", "0176 99999999") is None


def test_resolve_reports_matches_conflicts_and_new(fresh_app):
    from backend import contact_identity as I
    a = _mk("Anna Müller", email="anna@example.org")
    b = _mk("Bob", phone="+4915100000001")
    r = I.resolve([("email", "ANNA@example.org"), ("phone", "0151 00000002")], display_name="Anna Mueller")
    assert r.contact["id"] == a and r.matched == [("email", "anna@example.org")]
    assert r.unclaimed == [("phone", "+4915100000002")] and not r.conflicts and r.name_conflict
    r = I.resolve([("email", "anna@example.org"), ("phone", "+4915100000001")])
    assert r.contact["id"] == a and r.conflicts and r.conflicts[0]["contact"]["id"] == b
    assert I.resolve([("email", "nobody@example.org")]).is_new


def test_add_contact_skill_reuses_existing(fresh_app):
    from backend.skills.registry import Registry, SkillContext
    from backend.skills.add_contact.skill import execute
    from backend import contacts as C
    uid = seed_user(name="Skill User", role="admin", email="sk@example.local")
    a = _mk("Anna", email="anna@example.org")
    ctx = SkillContext(Registry(), role="admin", user_id=uid)
    out = asyncio.run(execute(ctx, display_name="Anna M.", emails=["anna@example.org"], phones=["0151 11111111"]))
    assert out["existing"] is True and out["contact_id"] == a
    kinds = {(c["kind"], c["value"]) for c in C.get(a)["channels"]}
    assert ("phone", "+4915111111111") in kinds
    b = _mk("Bob", phone="+4915122222222")
    with pytest.raises(ValueError):
        asyncio.run(execute(ctx, display_name="X", emails=["anna@example.org"], phones=["+4915122222222"]))


def test_merge_moves_everything_and_unmerge_restores(fresh_app):
    from backend import contact_identity as I, contacts as C
    keep = _mk("Anna (Mail)", email="anna@example.org", status="pending")
    drop = _mk("Anna", whatsapp="4915123456789@s.whatsapp.net")
    C.update(drop, birthday="1990-05-01")
    C.add_address(drop, kind="home", line1="Weg 1", postcode="30159", city="Hannover")
    rec = I.merge(keep, drop, reason="test")
    k = C.get(keep)
    assert {c["kind"] for c in k["channels"]} == {"email", "whatsapp"}
    assert k["birthday"] == "1990-05-01" and len(k["addresses"]) == 1 and k["status"] == "active"
    d = C.get(drop)
    assert d["status"] == "merged" and d["merged_into_id"] == keep and not d["channels"]
    # the merged tombstone is invisible to identity lookups; the survivor owns the id
    assert I.owner_of("whatsapp", "4915123456789@s.whatsapp.net")["id"] == keep
    I.unmerge(rec["id"])
    k, d = C.get(keep), C.get(drop)
    assert {c["kind"] for c in k["channels"]} == {"email"} and k["status"] == "pending" and not k["birthday"]
    assert d["status"] == "active" and {c["kind"] for c in d["channels"]} == {"whatsapp"} and len(d["addresses"]) == 1
    with pytest.raises(ValueError):
        I.unmerge(rec["id"])


def test_signature_bridge_proposes_merge_and_channel(fresh_app, monkeypatch):
    from backend import contact_identity as I, contact_autocapture as A, contacts as C
    from backend.database import get_conn
    wa = _mk("Anna", whatsapp="4915123456789@s.whatsapp.net")
    mail = _mk("Anna Müller", email="anna@example.org")
    owner = seed_user(name="Mail Owner", role="admin", email="owner@example.local")
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO email_messages (account_id, uid, owner_user_id, message_id, from_email, from_name, subject, body_text) "
            "VALUES (1, 1, ?, ?, ?, ?, ?, ?)",
            (owner, "<sig@test>", "anna@example.org", "Anna Müller", "Hallo",
             "Hallo,\n\nbis morgen!\n\n-- \nAnna Müller\nMobil: 0151 23456789\nBüro: 0511 987654"),
        )
        mid = cur.lastrowid
        conn.commit()
    assert A.on_inbound_email(from_email="anna@example.org", from_name="Anna Müller", message_id=mid) is None
    props = I.list_proposals()
    kinds = {(p["kind"], p.get("other_contact_id"), p.get("channel_value")) for p in props}
    assert ("merge", wa, None) in kinds
    assert ("add_channel", None, "+49511987654") in kinds
    # a second identical mail creates no duplicate proposals
    A.on_inbound_email(from_email="anna@example.org", from_name="Anna Müller", message_id=mid)
    assert len(I.list_proposals()) == len(props)
    # accept the merge: keep the mail contact, fold WhatsApp in
    merge_p = next(p for p in props if p["kind"] == "merge")
    I.accept_proposal(merge_p["id"], keep_id=mail)
    assert {c["kind"] for c in C.get(mail)["channels"]} == {"email", "whatsapp"}
    assert C.get(wa)["status"] == "merged"
    # accept the add-channel proposal
    add_p = next(p for p in props if p["kind"] == "add_channel")
    I.accept_proposal(add_p["id"])
    assert ("phone", "+49511987654") in {(c["kind"], c["value"]) for c in C.get(mail)["channels"]}


def test_whatsapp_inbound_attaches_to_phone_contact_by_merge(fresh_app):
    from backend import contact_autocapture as A, contacts as C
    phone = _mk("Anna", phone="0151 23456789")
    stray = _mk("4915123456789", whatsapp="4915123456789@s.whatsapp.net", status="pending")
    A.on_inbound_whatsapp(from_jid="4915123456789@s.whatsapp.net", from_name="Anna")
    assert {c["kind"] for c in C.get(phone)["channels"]} == {"phone", "whatsapp"}
    assert C.get(stray)["status"] == "merged"


def test_vcard_import_matches_whatsapp_contact_by_phone(fresh_app):
    from backend import contacts_import as V
    wa = _mk("Anna", whatsapp="4915123456789@s.whatsapp.net")
    cards = V.parse_vcards("BEGIN:VCARD\nVERSION:3.0\nFN:Anna\nTEL;TYPE=CELL:0151 23456789\nEMAIL:anna@example.org\nEND:VCARD\n")
    plan = V.plan_import(cards)
    e = plan.entries[0]
    assert e.outcome == "merge" and e.existing_id == wa


def test_mass_mailer_domain_matching(fresh_app):
    from backend import contacts as C
    assert C.is_mass_mailer_email("deals@ebay.de")
    assert C.is_mass_mailer_email("news@mail.ebay.com")
    assert not C.is_mass_mailer_email("kontakt@ebayfan.de")
    assert not C.is_mass_mailer_email("anna@hello.mycompany.de") or C.is_mass_mailer_email("x@hello.mycompany.de")
    assert C.is_mass_mailer_email("x@newsletter.acme.de")
    assert not C.is_transactional_email("meinhard@example.org")
    assert C.is_transactional_email("mein-ebay@ebay.de")


def test_proposal_routes(fresh_app):
    from backend import contact_identity as I
    client, uid = login_client(fresh_app, role="member")
    a = _mk("A", email="a@example.org")
    b = _mk("B", phone="+4915100000009")
    pid = I.propose(kind="merge", contact_id=a, other_contact_id=b, reason="t")
    r = client.get("/api/contacts/proposals")
    assert r.status_code == 200 and [p["id"] for p in r.json()["proposals"]] == [pid]
    r = client.post(f"/api/contacts/proposals/{pid}/accept", json={"keep_id": b})
    assert r.status_code == 200, r.text
    mid = r.json()["merge"]["id"]
    assert client.post(f"/api/contacts/proposals/{pid}/accept", json={}).status_code == 409
    r = client.get("/api/contacts/merges")
    assert r.json()["merges"][0]["id"] == mid
    assert client.post(f"/api/contacts/merges/{mid}/undo").status_code == 200
    assert client.post(f"/api/contacts/merges/{mid}/undo").status_code == 409
