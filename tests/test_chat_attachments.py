"""A file shown to Yorik belongs to the conversation; Paperless is the
only place documents live, and only on the user's yes."""

from __future__ import annotations

import asyncio

import pytest

from tests.conftest import login_client


@pytest.fixture
def two(fresh_app, tmp_path, monkeypatch):
    from backend import chat_attachments as A
    monkeypatch.setattr(A, "ROOT", tmp_path / "chat_attachments")
    pushed = []

    def fake_push(data, *, filename, title, mime_type, tags, user_id=None, visibility="private"):
        pushed.append({"filename": filename, "title": title, "user_id": str(user_id), "visibility": visibility, "bytes": len(data)})
        return {"ok": True, "task_id": "t-1", "visibility": visibility}

    from backend import main
    monkeypatch.setattr(main, "_push_to_paperless", fake_push)
    dirk_c, dirk = login_client(fresh_app, role="platform_admin", name="Dirk", email="d@example.local")
    beate_c, beate = login_client(fresh_app, role="member", name="Beate", email="b@example.local")
    return {"dirk": (dirk_c, dirk), "beate": (beate_c, beate), "pushed": pushed}


def _upload(client, name="notiz.txt", body=b"Rechnung der Stadtwerke, 84 Euro, faellig am 15.", mime="text/plain", conv=None):
    q = f"?conversation_id={conv}" if conv else ""
    r = client.post(f"/api/chat/attachments{q}", files={"file": (name, body, mime)})
    assert r.status_code == 201, r.text
    return r.json()


def test_upload_is_private_and_not_in_paperless(two):
    beate_c, _ = two["beate"]; dirk_c, _ = two["dirk"]
    att = _upload(beate_c)
    assert att["filed"] is False and att["suggest"] == "file" and att["has_text"]
    assert two["pushed"] == []                                         # nothing goes to the archive unasked
    assert beate_c.get(att["raw_url"]).status_code == 200
    assert dirk_c.get(f"/api/chat/attachments/{att['id']}").status_code == 404     # operator included
    assert dirk_c.post(f"/api/chat/attachments/{att['id']}/file").status_code == 404
    assert _upload(beate_c, "foto.png", b"\\x89PNG....", "image/png")["suggest"] == "keep"


def test_filing_goes_to_paperless_once(two):
    beate_c, beate = two["beate"]
    att = _upload(beate_c)
    r = beate_c.post(f"/api/chat/attachments/{att['id']}/file")
    assert r.status_code == 200 and r.json()["filed"] is True
    assert beate_c.post(f"/api/chat/attachments/{att['id']}/file").status_code == 200
    assert len(two["pushed"]) == 1 and two["pushed"][0]["user_id"] == beate and two["pushed"][0]["visibility"] == "private"


def test_skills_read_then_file(two):
    from backend.skills.registry import Registry, SkillContext
    from backend.skills.read_attachment.skill import execute as read
    from backend.skills.file_attachment.skill import execute as file_it
    beate_c, beate = two["beate"]; _, dirk = two["dirk"]
    att = _upload(beate_c)
    ctx = SkillContext(Registry(), role="member", user_id=beate, conversation_id="conv-7")
    out = asyncio.run(read(ctx, attachment_id=att["id"]))
    assert "Stadtwerke" in out["text"] and "file it in Paperless" in out["_llm_hint"]
    assert beate_c.get(f"/api/chat/attachments/{att['id']}").json()["conversation_id"] == "conv-7"
    other = asyncio.run(read(SkillContext(Registry(), role="platform_admin", user_id=dirk), attachment_id=att["id"]))
    assert other["ok"] is False
    done = asyncio.run(file_it(ctx, attachment_id=att["id"], title="Stadtwerke Rechnung"))
    assert done["ok"] and two["pushed"][0]["title"] == "Stadtwerke Rechnung"
    again = asyncio.run(read(ctx, attachment_id=att["id"]))
    assert again["filed"] is True and "do not ask about filing" in again["_llm_hint"]


def test_gone_with_the_conversation_and_after_the_retention(two):
    from backend import chat_attachments as A
    from backend.database import get_conn
    beate_c, beate = two["beate"]
    a = _upload(beate_c, conv="conv-1"); b = _upload(beate_c, conv="conv-2")
    assert A.delete_for_conversation("conv-1") == 1
    assert beate_c.get(f"/api/chat/attachments/{a['id']}").status_code == 404
    assert not (A.ROOT / str(a["id"])).exists()
    with get_conn() as conn:
        conn.execute("UPDATE chat_attachments SET expires_at = '2020-01-01T00:00:00' WHERE id = ?", (b["id"],))
        conn.commit()
    assert A.purge_expired() == 1
    assert beate_c.get(f"/api/chat/attachments/{b['id']}").status_code == 404
    c = _upload(beate_c)
    assert beate_c.delete(f"/api/chat/attachments/{c['id']}").status_code == 204
