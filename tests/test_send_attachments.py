"""The chat hands files to a mail or a WhatsApp message: any mix of chat
attachments, archive documents and photos, each looked up as the person.
Built 2026-10-16 after the live test showed the chat could attach one
file to a mail and nothing to a WhatsApp message."""
from __future__ import annotations

import asyncio
import json

import pytest

from tests.conftest import login_client


@pytest.fixture
def dirk(fresh_app, tmp_path, monkeypatch):
    from backend import chat_attachments as A
    monkeypatch.setattr(A, "ROOT", tmp_path / "chat_attachments")
    global dirk_app
    dirk_app = fresh_app
    client, uid = login_client(fresh_app, role="platform_admin", name="Dirk", email="d@example.local")
    return client, uid


def _ctx(uid, conv="conv-1"):
    from backend.skills.registry import Registry, SkillContext
    return SkillContext(Registry(), role="platform_admin", user_id=uid, conversation_id=conv)


def _fake_photo(monkeypatch, asset="abc-123"):
    from backend import send_attachments as S
    monkeypatch.setattr(S, "photo_meta", lambda user_id, aid: (
        {"id": aid, "filename": "IMG_1.jpg", "mimetype": "image/jpeg"} if aid == asset else None))


def _fake_paperless(monkeypatch, doc=7):
    from backend import paperless_ingest as P
    monkeypatch.setattr(P, "user_creds", lambda uid: {"base_url": "http://paperless.test", "api_key": "k"})
    monkeypatch.setattr(P, "_fetch_doc", lambda doc_id, creds_override=None: (
        {"original_file_name": "rechnung.pdf", "mime_type": "application/pdf"} if int(doc_id) == doc else None))


def test_resolve_mixes_the_three_kinds_and_refuses_what_is_not_yours(dirk, monkeypatch):
    client, uid = dirk
    from backend import chat_attachments as A, send_attachments as S
    att = A.store(user_id=uid, filename="brief.pdf", mime_type="application/pdf", data=b"%PDF-1.4 x",
                  conversation_id="conv-1")
    _fake_photo(monkeypatch)
    _fake_paperless(monkeypatch)
    entries, problems = S.resolve(uid, attachment_ids=[att["id"]], paperless_doc_ids=7, photo_ids=["abc-123"])
    assert problems == []
    assert [e["kind"] for e in entries] == ["attachment", "document", "photo"]
    assert entries[0]["url"] == f"/api/chat/attachments/{att['id']}/raw"
    assert entries[1]["url"] == "/paperless/api/documents/7/download/" and entries[1]["filename"] == "rechnung.pdf"
    assert entries[2]["url"] == "/api/photos/abc-123/raw" and entries[2]["mimetype"] == "image/jpeg"
    # someone else's attachment, a document the person may not see, a foreign photo
    _, other = login_client(dirk_app, role="member", name="Beate", email="b@example.local")
    _, problems = S.resolve(other, attachment_ids=att["id"], paperless_doc_ids=8, photo_ids="zzz")
    assert len(problems) == 3


def test_prepare_email_stages_several_files_including_a_photo(dirk, monkeypatch):
    client, uid = dirk
    from backend import chat_attachments as A, ui_tools
    from backend.skills.prepare_email.skill import execute as prepare
    att = A.store(user_id=uid, filename="brief.pdf", mime_type="application/pdf", data=b"%PDF-1.4 x",
                  conversation_id="conv-1")
    _fake_photo(monkeypatch)
    _fake_paperless(monkeypatch)
    from backend import email_addresses
    monkeypatch.setattr(email_addresses, "known", lambda *a, **k: {"mama@example.local"})
    async def run(**kw):
        ui_tools.reset_ui_actions()
        res = await prepare(_ctx(uid), **kw)
        return res, ui_tools.get_ui_actions()
    out, actions = asyncio.run(run(to="mama@example.local", subject="Fotos", body="Hallo",
                                   attachment_ids=[att["id"]], paperless_doc_ids=[7], photo_ids=["abc-123"]))
    assert out["ok"] is True
    assert out["attachments"] == ["brief.pdf", "rechnung.pdf", "IMG_1.jpg"]
    assert out["replaced_previous_draft"] is False
    r = client.get("/api/email/pending-draft")
    draft = r.json()["draft"]
    assert [a["url"] for a in draft["attachments"]] == [
        f"/api/chat/attachments/{att['id']}/raw", "/paperless/api/documents/7/download/", "/api/photos/abc-123/raw"]
    card = [a for a in actions if a.get("type") == "email_ready"][-1]
    assert card["attachment_filenames"] == ["brief.pdf", "rechnung.pdf", "IMG_1.jpg"]
    # a second draft replaces the first and says so
    out2, actions2 = asyncio.run(run(to="mama@example.local", subject="Zwei", body="x"))
    assert out2["replaced_previous_draft"] is True
    card2 = [a for a in actions2 if a.get("type") == "email_ready"][-1]
    assert card2["replaced_previous"] is True
    assert client.get("/api/email/pending-draft").json()["draft"]["subject"] == "Zwei"


def test_prepare_email_refuses_a_file_that_is_not_the_persons(dirk, monkeypatch):
    client, uid = dirk
    from backend.skills.prepare_email.skill import execute as prepare
    _fake_photo(monkeypatch)
    out = asyncio.run(prepare(_ctx(uid), to="x@example.local", subject="s", body="b", photo_ids=["not-mine"]))
    assert out["ok"] is False and "photo not-mine" in out["_llm_hint"]
    assert client.get("/api/email/pending-draft").json()["draft"] is None


class _BridgeResp:
    status_code = 200
    text = ""
    def json(self): return {"msgId": "SENT9", "ts": 1760000000}


class _FakeBridge:
    posts: list = []
    def __init__(self, *a, **kw): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def post(self, url, json=None, **kw):
        _FakeBridge.posts.append((url, json))
        return _BridgeResp()


def test_whatsapp_send_by_reference_reads_the_file_as_the_person(dirk, monkeypatch, tmp_path):
    client, uid = dirk
    from backend import whatsapp as WA, whatsapp_media as WM, chat_attachments as A
    monkeypatch.setattr(WA.httpx, "AsyncClient", _FakeBridge)
    monkeypatch.setattr(WM, "MEDIA_DIR", str(tmp_path / "wa_media"))
    _FakeBridge.posts.clear()
    att = A.store(user_id=uid, filename="brief.pdf", mime_type="application/pdf", data=b"%PDF-1.4 hello",
                  conversation_id="conv-1")
    jid = "4915123456789@s.whatsapp.net"
    r = client.post(f"/api/whatsapp/chats/{jid}/send-media-ref",
                    json={"url": f"/api/chat/attachments/{att['id']}/raw", "caption": "Hier der Brief"})
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "document"
    url, payload = _FakeBridge.posts[-1]
    assert url.endswith(f"/chats/{jid}/send-media") and payload["caption"] == "Hier der Brief"
    assert payload["filename"] == "brief.pdf"
    # an external url or someone else's file is refused
    assert client.post(f"/api/whatsapp/chats/{jid}/send-media-ref",
                       json={"url": "https://evil.example/x.pdf", "caption": ""}).status_code == 404


def test_whatsapp_draft_card_carries_the_file(dirk, monkeypatch):
    client, uid = dirk
    from backend import ui_tools
    from backend.skills.whatsapp_draft import skill as WD
    _fake_photo(monkeypatch)
    WD._show_card("4915123456789@s.whatsapp.net", "Mama", [{"label": "a", "text": "Schau mal"}],
                  is_new_chat=True, attachment={"url": "/api/photos/abc-123/raw", "filename": "IMG_1.jpg",
                                                "mimetype": "image/jpeg", "kind": "photo", "ref": "abc-123"})
    card = [a for a in ui_tools.get_ui_actions() if a.get("type") == "whatsapp_draft_created"][-1]
    assert card["attachment"] == {"url": "/api/photos/abc-123/raw", "filename": "IMG_1.jpg", "mimetype": "image/jpeg"}


def test_meeting_times_without_a_mail_return_slots_only(dirk, monkeypatch):
    client, uid = dirk
    from backend.skills.propose_meeting_times.skill import execute as propose
    out = asyncio.run(propose(_ctx(uid), num_slots=2, window_days=3))
    assert "slots" in out and out["drafts"] == []
    assert "prepare_email or whatsapp_draft" in out["_llm_hint"]
