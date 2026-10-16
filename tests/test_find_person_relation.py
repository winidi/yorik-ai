"""'meine Mama', 'der Vermieter': a relation word finds the contact whose
relation field says so, in either language, without a picker."""
from __future__ import annotations

import asyncio

from tests.conftest import login_client


def _ctx(uid):
    from backend.skills.registry import Registry, SkillContext
    return SkillContext(Registry(), role="platform_admin", user_id=uid, conversation_id="c1")


def test_a_german_relation_word_finds_the_english_relation(fresh_app):
    client, uid = login_client(fresh_app, role="platform_admin", name="Dirk", email="d@example.local")
    r = client.post("/api/contacts", json={"display_name": "Hausverwaltung Schmidt", "kind": "business", "relation": "landlord"})
    assert r.status_code == 201, r.text
    client.post("/api/contacts", json={"display_name": "Vermieter Müller GmbH", "kind": "business"})
    from backend.skills.find_person.skill import execute as find
    out = asyncio.run(find(_ctx(uid), query="vermieter", source="contacts"))
    assert out["count"] == 1 and out["results"][0]["display_name"] == "Hausverwaltung Schmidt"
    assert out["ambiguous"] is False
    assert "Matched by relation" in (out["_llm_hint"] or "")


def test_mama_prefers_the_contact_whose_relation_says_so(fresh_app):
    client, uid = login_client(fresh_app, role="platform_admin", name="Dirk", email="d@example.local")
    client.post("/api/contacts", json={"display_name": "Beate Mama", "kind": "person"})
    r = client.post("/api/contacts", json={"display_name": "Mama Nowa", "kind": "person", "relation": "Mama (Mutter)"})
    assert r.status_code == 201
    from backend.skills.find_person.skill import execute as find
    out = asyncio.run(find(_ctx(uid), query="Mama", source="contacts"))
    assert out["count"] == 1 and out["results"][0]["display_name"] == "Mama Nowa"
    out2 = asyncio.run(find(_ctx(uid), query="mother", source="contacts"))
    assert out2["count"] == 1 and out2["results"][0]["display_name"] == "Mama Nowa"


def test_several_contacts_with_the_relation_still_ask(fresh_app):
    client, uid = login_client(fresh_app, role="platform_admin", name="Dirk", email="d@example.local")
    client.post("/api/contacts", json={"display_name": "Dr. A", "kind": "person", "relation": "dentist"})
    client.post("/api/contacts", json={"display_name": "Dr. B", "kind": "person", "relation": "Zahnarzt"})
    from backend.skills.find_person.skill import execute as find
    out = asyncio.run(find(_ctx(uid), query="zahnarzt", source="contacts"))
    assert out["count"] >= 2 and out["ambiguous"] is True
    assert {r["display_name"] for r in out["results"][:2]} == {"Dr. A", "Dr. B"}
