"""Paperless answers as the person asking (audit
docs/audits/2026-09-22-berechtigungen.md, package 6: 1.4–1.6, 1.8–1.10,
3.5). Their own token hydrates search hits and what it may not see is
dropped; no Paperless account means no documents, never the admin's view."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from backend import paperless_ingest as PI
from tests.conftest import login_client


class _Resp:
    def __init__(self, results):
        self._r = results
    ok, status_code = True, 200
    def raise_for_status(self): pass
    def json(self): return {"results": self._r, "count": len(self._r)}


def _rows(*ids):
    return [{"id": 100 + i, "paperless_doc_id": i, "chunk_index": 0, "text": f"text of {i}", "distance": 0.1} for i in ids]


def test_hydration_keeps_only_what_the_persons_token_returns(monkeypatch):
    monkeypatch.setattr(PI, "_paperless_settings", lambda: {"base_url": "http://p", "api_key": "admin"})
    monkeypatch.setattr(PI.requests, "get", lambda url, **kw: _Resp([{"id": 4, "title": "Projektvertrag", "tags": []}]))
    beate = {"base_url": "http://p", "api_key": "beate"}
    out = PI._hydrate(_rows(3, 4), beate)
    assert [(h["paperless_doc_id"], h["doc_title"]) for h in out] == [(4, "Projektvertrag")]   # 3 is not hers
    out = PI._hydrate(_rows(3, 4), None)                                                          # housekeeping: admin keeps all
    assert [h["paperless_doc_id"] for h in out] == [3, 4]

    def boom(url, **kw):
        raise PI.requests.RequestException("down")
    monkeypatch.setattr(PI.requests, "get", boom)
    assert PI._hydrate(_rows(3, 4), beate) == []                     # cannot tell what she may see → nothing


def test_the_search_routes_answer_nothing_without_a_paperless_account(fresh_app, monkeypatch):
    from backend import paperless_ingest as PI          # fresh_app reloads the backend: bind after it
    client, uid = login_client(fresh_app, role="member", name="Beate", email="b@example.local")
    monkeypatch.setattr(PI, "user_creds", lambda user_id: None)
    monkeypatch.setattr(PI, "search", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not search")))
    monkeypatch.setattr(PI, "search_hybrid", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not search")))
    assert client.get("/api/paperless/search?q=vertrag").json() == {"query": "vertrag", "results": []}
    r = client.post("/api/documents/search", json={"query": "vertrag", "k": 5}).json()
    assert [h for h in r["hits"] if h.get("source") == "paperless"] == []
    assert "no Paperless account" in r["legs"]["semantic"]["error"]


def test_read_document_looks_the_document_up_as_the_person(fresh_app, monkeypatch):
    import asyncio
    from backend import paperless_ingest as PI
    from backend.documents import get_docs_conn, DOCS_DB_PATH
    from backend.skills.read_document.skill import execute
    from backend.skills.registry import Registry, SkillContext
    with get_docs_conn(DOCS_DB_PATH) as conn:
        conn.execute("INSERT INTO paperless_chunks (paperless_doc_id, chunk_index, text, char_start, char_end) "
                     "VALUES (4, 0, 'Projektvertrag zwischen Kommpact und Beate Mayer', 0, 48)")
        conn.commit()
    ctx = SkillContext(Registry(), role="member", user_id="beate-uuid")
    monkeypatch.setattr(PI, "user_creds", lambda user_id: None)
    assert asyncio.run(execute(ctx, 4))["ok"] is False                                 # no account → not found
    monkeypatch.setattr(PI, "user_creds", lambda user_id: {"base_url": "http://p", "api_key": "beate"})
    monkeypatch.setattr(PI, "_fetch_doc", lambda doc_id, creds_override=None: None)   # Paperless: not hers
    assert asyncio.run(execute(ctx, 4))["ok"] is False
    monkeypatch.setattr(PI, "_fetch_doc", lambda doc_id, creds_override=None: {"title": "Projektvertrag"} if creds_override["api_key"] == "beate" else None)
    out = asyncio.run(execute(ctx, 4))
    assert out["ok"] is True and out["title"] == "Projektvertrag" and "Kommpact" in out["text"]


def test_draft_context_is_the_persons_own(fresh_app, monkeypatch):
    from backend import paperless_ingest as PI
    from backend import spaces as S, whatsapp as W
    from backend import connectors
    from backend.calendars import ensure_calendars_for_user
    from backend.database import get_conn
    _, dirk = login_client(fresh_app, role="platform_admin", name="Dirk", email="d@example.local")
    _, beate = login_client(fresh_app, role="member", name="Beate", email="b@example.local")
    S.ensure_workspace_exists(dirk, "Dirk")
    for uid, name in ((dirk, "Dirk"), (beate, "Beate")):
        S.ensure_personal_space(uid, name); ensure_calendars_for_user(uid, name)
    start = (datetime.now() + timedelta(days=1)).replace(microsecond=0)
    with get_conn() as conn:
        for uid, title in ((dirk, "Zahnarzt Dirk"), (beate, "Yoga Beate")):
            cal = conn.execute("SELECT id FROM calendars WHERE owner_user_id=? AND kind='personal'", (uid,)).fetchone()["id"]
            conn.execute("INSERT INTO events (title, starts_at, ends_at, all_day, calendar_id, owner_user_id) VALUES (?, ?, ?, 0, ?, ?)",
                         (title, start.isoformat(), (start + timedelta(hours=1)).isoformat(), cal, uid))
        conn.commit()
    assert [e["title"] for e in W._calendar_context(user_id=beate)] == ["Yoga Beate"]
    assert [e["title"] for e in W._calendar_context(user_id=dirk)] == ["Zahnarzt Dirk"]
    assert W._calendar_context() == []                                                  # no person, no context
    monkeypatch.setattr(PI, "search", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not search")))
    assert W._paperless_hints("Rechnung vom Elektriker") == []                           # no person
    monkeypatch.setattr(PI, "user_creds", lambda user_id: None)
    assert W._paperless_hints("Rechnung vom Elektriker", user_id=beate) == []            # no account

    import asyncio
    r = asyncio.run(connectors.invoke("paperless", {"op": "search", "query": "x"}))
    assert r["ok"] is False and "person" in r["error"]
    r = asyncio.run(connectors.invoke("paperless", {"op": "search", "query": "x"}, user_id=beate))
    assert r["ok"] is False and "no Paperless account" in r["error"]
