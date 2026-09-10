"""Web Push: subscriptions, nudges, the notify skill, the bell → push hook."""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from tests.conftest import login_client


@pytest.fixture
def sent(monkeypatch, tmp_path):
    """Capture webpush calls instead of talking to a push service."""
    from backend import push as P
    monkeypatch.setattr(P, "VAPID_FILE", tmp_path / "vapid.json")
    P._keys = None
    calls = []

    class _Resp:
        status_code = 410

    class WebPushException(Exception):
        def __init__(self, msg, response=None):
            super().__init__(msg)
            self.response = response

    def fake_webpush(subscription_info, data, **kw):
        calls.append({"endpoint": subscription_info["endpoint"], "data": data})
        if "gone" in subscription_info["endpoint"]:
            raise WebPushException("gone", response=_Resp())

    import types, sys
    fake = types.ModuleType("pywebpush")
    fake.webpush = fake_webpush
    fake.WebPushException = WebPushException
    monkeypatch.setitem(sys.modules, "pywebpush", fake)
    return calls


def _sub(endpoint):
    return {"endpoint": endpoint, "keys": {"p256dh": "BPK", "auth": "AUTH"}}


def test_subscribe_status_and_push_delivery(fresh_app, sent):
    client, uid = login_client(fresh_app, role="member")
    r = client.get("/api/push/vapid-public-key")
    assert r.status_code == 200 and len(r.json()["key"]) > 40
    assert client.post("/api/push/subscribe", json={"subscription": _sub("https://push.example/a")}).status_code == 200
    assert client.post("/api/push/subscribe", json={"subscription": _sub("https://push.example/gone")}).status_code == 200
    assert client.post("/api/push/subscribe", json={"subscription": {"endpoint": "x"}}).status_code == 400
    st = client.get("/api/push/status").json()
    assert len(st["devices"]) == 2 and st["nudge_morning"] is None

    # a bell notification is pushed to both; the dead one is removed
    from backend import notifications as N
    nid = N.create(user_id=uid, kind="agent_message", title="Hallo", body="Test", navigate_to="/r/home")
    assert {c["endpoint"] for c in sent} == {"https://push.example/a", "https://push.example/gone"}
    assert '"title": "Hallo"' in sent[0]["data"]
    assert N.last_push_count(nid) == 1
    st = client.get("/api/push/status").json()
    assert [d["endpoint"] for d in st["devices"]] == ["https://push.example/a"]

    r = client.post("/api/push/test")
    assert r.json() == {"ok": True, "delivered": 1}
    assert client.post("/api/push/unsubscribe", json={"endpoint": "https://push.example/a"}).json()["ok"] is True
    assert client.get("/api/push/status").json()["devices"] == []


def test_nudges_fire_once_per_minute(fresh_app, sent):
    from backend import push as P
    client, uid = login_client(fresh_app, role="member")
    assert client.patch("/api/push/nudges", json={"nudge_morning": "7:5", "nudge_evening": ""}).json()["nudge_morning"] == "07:05"
    assert client.patch("/api/push/nudges", json={"nudge_morning": "25:00"}).status_code == 400
    client.post("/api/push/subscribe", json={"subscription": _sub("https://push.example/phone")})
    P._last_fired.clear()
    now = datetime(2030, 5, 1, 7, 5, tzinfo=P._tz())
    assert P.fire_due_nudges(now) == 1
    assert P.fire_due_nudges(now) == 0          # same minute, no repeat
    assert P.fire_due_nudges(now.replace(minute=6)) == 0
    bell = client.get("/api/notifications").json()["notifications"]
    assert bell[0]["kind"] == "nudge_morning" and bell[0]["navigate_to"].startswith("/r/chat?say=")
    assert sent and "say=" in sent[-1]["data"]


def test_notify_skill_reaches_bell_and_phone(fresh_app, sent):
    from backend.skills.registry import Registry, SkillContext
    from backend.skills.notify.skill import execute
    client, uid = login_client(fresh_app, role="member")
    client.post("/api/push/subscribe", json={"subscription": _sub("https://push.example/phone")})
    ctx = SkillContext(Registry(), role="member", user_id=uid, source="token:hermes")
    out = asyncio.run(execute(ctx, title="Recherche fertig", body="Headscale gewinnt.", url="/r/documents"))
    assert out["pushed_devices"] == 1
    bell = client.get("/api/notifications").json()["notifications"]
    assert bell[0]["title"] == "Recherche fertig" and bell[0]["payload"]["source"] == "token:hermes"
    with pytest.raises(ValueError):
        asyncio.run(execute(ctx, title="x", url="javascript:alert(1)"))
    # over MCP the tool is visible
    from backend.mcp_server import list_tools
    assert "notify" in {t["name"] for t in list_tools({"id": uid, "role": "member"})}


def test_notifications_can_be_dismissed(fresh_app, sent):
    from backend import notifications as N
    client, uid = login_client(fresh_app, role="member")
    other, _ = login_client(fresh_app, role="member", name="Other", email="other@example.local")
    a = N.create(user_id=uid, kind="agent_message", title="A")
    b = N.create(user_id=uid, kind="agent_message", title="B")
    c = N.create(user_id=uid, kind="agent_message", title="C")
    assert other.delete(f"/api/notifications/{a}").status_code == 404          # not theirs
    assert client.delete(f"/api/notifications/{a}").json() == {"ok": True}
    assert [n["title"] for n in client.get("/api/notifications").json()["notifications"]] == ["C", "B"]
    client.post(f"/api/notifications/{b}/read")
    assert client.delete("/api/notifications?read_only=true").json()["removed"] == 1
    assert [n["title"] for n in client.get("/api/notifications").json()["notifications"]] == ["C"]
    assert client.delete("/api/notifications").json()["removed"] == 1
    assert client.get("/api/notifications").json()["notifications"] == []
    assert c and client.delete(f"/api/notifications/{c}").status_code == 404
