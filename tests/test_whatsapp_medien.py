"""Photos, videos and PDFs go out over WhatsApp from Yorik, the way the
WhatsApp app sends them (Dirk 2026-10-02: Yorik should replace the real
app as far as it can). A photo is shrunk to the app's size, the text
becomes the caption, Yorik keeps its own copy so the thread shows it
without asking the bridge, and anything else is refused with a reason."""

from __future__ import annotations

import io
import json

import pytest

from tests.conftest import login_client


class _BridgeResp:
    status_code = 200
    text = ""
    def json(self): return {"msgId": "SENT1", "ts": 1759400000}


class _FakeBridge:
    posts: list = []
    def __init__(self, *a, **kw): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def post(self, url, json=None, **kw):
        _FakeBridge.posts.append((url, json))
        return _BridgeResp()


@pytest.fixture
def beate(fresh_app, monkeypatch, tmp_path):
    from backend import whatsapp as WA, whatsapp_media as WM
    client, uid = login_client(fresh_app, role="member", name="Beate", email="b@example.local")
    monkeypatch.setattr(WA.httpx, "AsyncClient", _FakeBridge)
    monkeypatch.setattr(WM, "MEDIA_DIR", str(tmp_path / "wa_media"))
    _FakeBridge.posts.clear()
    return client, uid


JID = "4915123456789@s.whatsapp.net"


def _png(w: int, h: int) -> bytes:
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (w, h), (200, 30, 30)).save(out, "PNG")
    return out.getvalue()


def test_a_photo_goes_out_shrunk_with_its_caption_and_stays_in_the_thread(beate):
    client, uid = beate
    r = client.post(f"/api/whatsapp/chats/{JID}/send-media",
                    files={"file": ("garten.png", _png(4000, 3000), "image/png")},
                    data={"caption": "Schau mal"})
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "image" and r.json()["mimetype"] == "image/jpeg"

    url, body = _FakeBridge.posts[-1]
    assert url.endswith(f"/users/{uid}/chats/{JID}/send-media")
    assert body["kind"] == "image" and body["caption"] == "Schau mal"
    from PIL import Image
    import base64
    im = Image.open(io.BytesIO(base64.b64decode(body["data"])))
    assert max(im.size) == 1600                       # the app's size, not 4000 px

    from backend.database import get_conn
    with get_conn() as conn:
        row = conn.execute("SELECT from_me, text, media_kind, mimetype, owner_user_id FROM wa_messages "
                           "WHERE msg_id='SENT1' AND chat_jid=?", (JID,)).fetchone()
    assert row and row["from_me"] == 1 and row["text"] == "Schau mal"
    assert row["media_kind"] == "image" and row["mimetype"] == "image/jpeg" and row["owner_user_id"] == uid

    # the thread shows it from Yorik's own copy — no bridge call
    before = len(_FakeBridge.posts)
    g = client.get("/api/whatsapp/media/SENT1")
    assert g.status_code == 200 and g.headers["content-type"].startswith("image/jpeg")
    assert len(_FakeBridge.posts) == before


def test_a_small_photo_is_sent_as_it_is(beate):
    client, _ = beate
    r = client.post(f"/api/whatsapp/chats/{JID}/send-media",
                    files={"file": ("x.png", _png(800, 600), "image/png")})
    assert r.status_code == 200 and r.json()["mimetype"] == "image/png"
    assert _FakeBridge.posts[-1][1]["caption"] == ""


def test_a_pdf_keeps_its_name_and_a_video_is_a_video(beate):
    client, _ = beate
    r = client.post(f"/api/whatsapp/chats/{JID}/send-media",
                    files={"file": ("Vertrag 2026.pdf", b"%PDF-1.4 x", "application/pdf")})
    assert r.status_code == 200 and r.json()["kind"] == "document"
    assert _FakeBridge.posts[-1][1]["filename"] == "Vertrag 2026.pdf"
    from backend.database import get_conn
    with get_conn() as conn:
        row = conn.execute("SELECT filename FROM wa_messages WHERE msg_id='SENT1' AND chat_jid=?", (JID,)).fetchone()
    assert row["filename"] == "Vertrag 2026.pdf"

    r = client.post(f"/api/whatsapp/chats/{JID}/send-media",
                    files={"file": ("clip.mp4", b"\x00\x00\x00\x18ftypmp42", "video/mp4")})
    assert r.status_code == 200 and r.json()["kind"] == "video"
    assert _FakeBridge.posts[-1][1]["kind"] == "video"


def test_what_whatsapp_cannot_show_is_refused_with_a_reason(beate):
    client, _ = beate
    r = client.post(f"/api/whatsapp/chats/{JID}/send-media",
                    files={"file": ("notes.txt", b"hi", "text/plain")})
    assert r.status_code == 415 and "PDF" in r.json()["detail"]
    r = client.post(f"/api/whatsapp/chats/{JID}/send-media",
                    files={"file": ("IMG_1.heic", b"\x00" * 10, "image/heic")})
    assert r.status_code == 415 and "HEIC" in r.json()["detail"]
    r = client.post(f"/api/whatsapp/chats/{JID}/send-media",
                    files={"file": ("empty.png", b"", "image/png")})
    assert r.status_code == 400
    assert _FakeBridge.posts == []


def test_without_a_session_the_bridge_answer_reaches_the_person(beate, monkeypatch):
    client, _ = beate
    from backend import whatsapp as WA
    class _Down:
        status_code, text = 503, '{"error":"not_connected"}'
        def json(self): return {}
    class _Client(_FakeBridge):
        async def post(self, url, json=None, **kw): return _Down()
    monkeypatch.setattr(WA.httpx, "AsyncClient", _Client)
    r = client.post(f"/api/whatsapp/chats/{JID}/send-media",
                    files={"file": ("x.png", _png(10, 10), "image/png")})
    assert r.status_code == 503
