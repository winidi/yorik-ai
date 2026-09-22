"""A chat belongs to the person who had it. Until 2026-09-22 the routes
compared the role on the row, so two members could read, pin, delete
and regenerate each other's conversations, and admins everyone's
(audit docs/audits/2026-09-22-berechtigungen.md, 2.2–2.4)."""

from __future__ import annotations

from backend.agent import conversation_io as ci
from tests.conftest import login_client


def _chat(cid: str, user_id: str, role: str = "member") -> None:
    ci.save_messages(cid, role, user_id, [{"role": "user", "content": "Wo ist mein Mietvertrag?"},
                                         {"role": "assistant", "content": "Im Ordner Wohnung."}])


def test_another_member_and_an_admin_see_nothing(fresh_app):
    beate, beate_id = login_client(fresh_app, role="member", name="Beate", email="b@example.local")
    other, _ = login_client(fresh_app, role="member", name="Anna", email="a@example.local")
    admin, _ = login_client(fresh_app, role="platform_admin", name="Dirk", email="d@example.local")
    _chat("conv-beate", beate_id)

    assert beate.get("/api/conversations/conv-beate").status_code == 200
    assert [c["id"] for c in beate.get("/api/conversations").json()] == ["conv-beate"]
    for client in (other, admin):
        assert client.get("/api/conversations").json() == []
        assert client.get("/api/conversations/conv-beate").status_code == 404          # not 403: no probing by id
        assert client.post("/api/conversations/conv-beate/pin", json={"pinned": True}).status_code == 404
        assert client.get("/api/conversations/conv-beate/stash").json() == {"items": []}
        assert client.delete("/api/conversations/conv-beate").status_code == 404
        assert client.post("/api/conversations/conv-beate/regenerate").status_code == 404
    assert beate.get("/api/conversations/conv-beate").status_code == 200              # still there


def test_the_storage_layer_keys_on_the_person(fresh_app):
    from tests.conftest import seed_user
    a = seed_user(name="A", role="member", email="a@x.local", password="pytestpw123")
    b = seed_user(name="B", role="member", email="b@x.local", password="pytestpw123")
    _chat("conv-x", a)
    assert ci.load_messages("conv-x", a)[0]["content"].startswith("Wo ist")
    assert ci.load_messages("conv-x", b) == []
    assert ci.load_messages("conv-x", None) == []
    ci.save_messages("conv-x", "member", b, [{"role": "user", "content": "overwrite"}])   # refused
    assert ci.load_messages("conv-x", a)[0]["content"].startswith("Wo ist")
    ci.save_stash("conv-x", b, [{"url": "/api/x", "filename": "x"}])                     # refused
    assert ci.load_stash("conv-x", a) == []
    assert not ci.delete_conversation("conv-x", b)
    assert ci.delete_conversation("conv-x", a)
    ci.save_messages("conv-nobody", "member", None, [{"role": "user", "content": "hi"}])         # not created
    assert ci.load_messages("conv-nobody", None) == []
