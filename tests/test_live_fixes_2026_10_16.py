"""Fixes from the live chat test of 2026-10-16: superseded draft cards,
picker quality, day planning on by default."""
from __future__ import annotations

import asyncio


def test_a_newer_draft_takes_the_send_button_off_the_earlier_card():
    from backend.agent.conversation_io import supersede_cards
    older = [
        {"role": "assistant", "content": "x", "metadata": {"ui_actions": [
            {"type": "whatsapp_draft_created", "uid": "a", "chat_jid": "1@s.whatsapp.net", "text": "alt"},
            {"type": "whatsapp_draft_created", "uid": "b", "chat_jid": "2@s.whatsapp.net", "text": "anderer chat"},
            {"type": "whatsapp_draft_created", "uid": "c", "chat_jid": "1@s.whatsapp.net", "text": "schon weg", "done": "sent"},
            {"type": "email_ready", "uid": "e", "to": "x@example.local"},
        ]}},
    ]
    new = [{"type": "whatsapp_draft_created", "uid": "n", "chat_jid": "1@s.whatsapp.net", "text": "neu"},
           {"type": "email_ready", "uid": "m", "to": "y@example.local"}]
    assert supersede_cards(older, new) == 2
    cards = {a["uid"]: a.get("done") for a in older[0]["metadata"]["ui_actions"]}
    assert cards == {"a": "replaced", "b": None, "c": "sent", "e": "replaced"}


def test_guessed_and_junk_picks_do_not_lead_the_picker(fresh_app, monkeypatch):
    from tests.conftest import login_client
    from backend import ui_tools
    from backend.skills.registry import Registry, SkillContext
    from backend.skills.list_contacts_for_picking.skill import _render_ranked
    client, uid = login_client(fresh_app, role="platform_admin", name="Dirk", email="d@example.local")
    ids = {}
    for name, rel in (("Hausverwaltung Schmidt", "landlord"), ("Erbbaurecht", ""), ("104539671802024", ""), ("Molly", "")):
        r = client.post("/api/contacts", json={"display_name": name, "kind": "person", "relation": rel})
        ids[name] = r.json()["id"]
    ctx = SkillContext(Registry(), role="platform_admin", user_id=uid, conversation_id="c")
    picks = [
        {"id": ids["Erbbaurecht"], "confidence": 0.5, "reason": "Geschäftskontakt – könnte Vermieter sein"},
        {"id": ids["104539671802024"], "confidence": 0.9, "reason": "Nummer"},
        {"id": ids["Hausverwaltung Schmidt"], "confidence": 0.95, "reason": "relation landlord"},
        {"id": ids["Molly"], "confidence": 0.6, "reason": "irgendwer"},
    ]
    async def run():
        ui_tools.reset_ui_actions()
        out = await _render_ranked(ctx, "vermieter", picks)
        return out, [a for a in ui_tools.get_ui_actions() if a["type"] == "contact_picker"][-1]
    out, card = asyncio.run(run())
    rows = card["contacts"]
    assert rows[0]["display_name"] == "Hausverwaltung Schmidt" and rows[0]["confidence"] >= 0.75
    names = [r["display_name"] for r in rows]
    assert "104539671802024" not in names
    erb = next(r for r in rows if r["display_name"] == "Erbbaurecht")
    assert erb["confidence"] == 0.0 and erb["reason"] == ""
    molly = next(r for r in rows if r["display_name"] == "Molly")
    assert molly["confidence"] == 0.0


def test_day_planning_is_on_by_default():
    from backend.skills.registry import NICHE_OFF_BY_DEFAULT
    assert {"plan_my_day", "plan_day", "remember_planning_rule"}.isdisjoint(NICHE_OFF_BY_DEFAULT)
    assert "ask_agent" in NICHE_OFF_BY_DEFAULT
