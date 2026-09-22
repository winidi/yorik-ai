"""What arrives for a person is filed as that person (audit
docs/audits/2026-09-22-berechtigungen.md, package 7: 1.11, 1.12, 1.14):
WhatsApp documents and photos with the recipient's own Paperless/Immich
credentials, composed documents with the author's, and no upload ever
falls back to the admin token."""

from __future__ import annotations

import asyncio

import pytest


class _FakeResp:
    ok, status_code, text = True, 200, '"task-7"'
    headers = {"content-type": "application/json"}
    def json(self): return {"id": "asset-7"}


class _FakeClient:
    posts: list = []
    def __init__(self, *a, **kw): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def post(self, url, headers=None, files=None, data=None):
        _FakeClient.posts.append((url, headers, data))
        return _FakeResp()


def test_whatsapp_document_and_photo_go_to_the_recipients_accounts(fresh_app, monkeypatch):
    from backend import whatsapp_media as WM, paperless_visibility as PV, external_users as EU
    from backend.database import get_conn
    from tests.conftest import seed_user
    beate = seed_user(name="Beate", role="member", email="b@x.local", password="pytestpw123")
    with get_conn() as conn:
        conn.execute("INSERT INTO wa_chats (jid, name, is_group, owner_user_id) VALUES ('4917@s.whatsapp.net', 'Anna', 0, ?)", (beate,))
        conn.execute("INSERT INTO wa_messages (chat_jid, msg_id, owner_user_id, from_me, text, media_kind, timestamp) VALUES ('4917@s.whatsapp.net', 'm1', ?, 0, '', 'document', 1758500000)", (beate,))
        conn.commit()
    _FakeClient.posts.clear()
    monkeypatch.setattr(WM.httpx, "AsyncClient", _FakeClient)
    async def _dl(msg_id): return b"%PDF-1.4"
    monkeypatch.setattr(WM, "_download_from_bridge", _dl)
    monkeypatch.setattr(PV, "ensure_household_tag", lambda name: 9)
    monkeypatch.setattr(PV, "resolve_visibility_tag_id", lambda level: None)
    applied = []
    def _consumed(task_id, vis, on_document=None):          # Paperless is done: document 44 exists
        applied.append((task_id, vis)); on_document and on_document(44)
    monkeypatch.setattr(PV, "apply_after_consume", _consumed)
    msg = {"id": "m1", "jid": "4917@s.whatsapp.net", "filename": "vertrag.pdf", "mimetype": "application/pdf"}

    # no account of her own → nothing is uploaded anywhere
    monkeypatch.setattr(EU, "get_user_paperless_creds", lambda uid: None)
    monkeypatch.setattr(EU, "get_user_immich_creds", lambda uid: None)
    asyncio.run(WM._route_to_paperless(msg, beate))
    asyncio.run(WM._route_to_immich(msg, beate, is_video=False))
    assert _FakeClient.posts == []

    # her own tokens → her document, her library, her default visibility after the consume
    monkeypatch.setattr(EU, "get_user_paperless_creds", lambda uid: {"base_url": "http://p", "api_key": f"pl-{uid}"})
    monkeypatch.setattr(EU, "get_user_immich_creds", lambda uid: {"base_url": "http://i", "api_key": f"im-{uid}"})
    with get_conn() as conn:
        conn.execute("UPDATE user_profiles SET default_doc_visibility = 'parents' WHERE id = ?", (beate,)); conn.commit()
    asyncio.run(WM._route_to_paperless(msg, beate))
    asyncio.run(WM._route_to_immich(msg, beate, is_video=False))
    (purl, pheaders, pdata), (iurl, iheaders, _) = _FakeClient.posts
    assert purl == "http://p/api/documents/post_document/" and pheaders["Authorization"] == f"Token pl-{beate}"
    assert pdata["tags"] == [9]
    assert iurl == "http://i/api/assets" and iheaders["x-api-key"] == f"im-{beate}"
    assert applied == [("task-7", "parents")]
    with get_conn() as conn:
        row = conn.execute("SELECT media_paperless_id, media_immich_id FROM wa_messages WHERE msg_id='m1'").fetchone()
    assert (row["media_paperless_id"], row["media_immich_id"]) == (44, "asset-7")


def test_compose_and_uploads_never_fall_back_to_the_admin_token(fresh_app, monkeypatch):
    from backend import paperless_ingest as PI
    from backend.compose import save as SV
    from backend import main as M
    import backend.connectors.paperless as CP
    monkeypatch.setattr(CP, "_settings", lambda: {"base_url": "http://p", "api_key": "admin"})   # admin token present, must stay unused
    monkeypatch.setattr(SV, "render_pdf", lambda *a, **kw: b"%PDF-1.4")
    monkeypatch.setattr(PI, "user_creds", lambda uid: None)
    r = SV.save_to_paperless("<p>x</p>", title="Angebot")
    assert r["ok"] is False and "no Paperless account" in r["error"]
    r = M._push_to_paperless(b"%PDF-1.4", filename="a.pdf", title="a", mime_type="application/pdf", tags=[], user_id="nobody")
    assert r["ok"] is False and "no Paperless account" in r["error"]

    posted = []
    monkeypatch.setattr(PI, "user_creds", lambda uid: {"base_url": "http://p", "api_key": f"pl-{uid}"} if uid else None)
    monkeypatch.setattr(SV, "_ensure_tag_ids", lambda s, names: [])
    monkeypatch.setattr(SV.requests, "post", lambda url, **kw: posted.append(kw["headers"]) or _FakeResp())
    r = SV.save_to_paperless("<p>x</p>", title="Angebot", user_id="dirk")
    assert r["ok"] is True and posted[0]["Authorization"] == "Token pl-dirk"
