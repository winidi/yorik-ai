"""'Shared' must give the household group view on the DOCUMENT; a tag
alone lets nobody open it (seen on the live box 2026-09-21)."""

from backend import paperless_visibility as pv


class _Resp:
    def __init__(self, ok=True, data=None):
        self.ok, self._data, self.status_code, self.text = ok, data or {}, 200 if ok else 400, ""

    def json(self):
        return self._data


def test_shared_sets_the_household_group_on_the_document(monkeypatch):
    calls = []
    monkeypatch.setattr(pv, "_settings", lambda: {"api_key": "k", "base_url": "http://p"})
    monkeypatch.setattr(pv, "_ensure_groups", lambda base, headers: {"household": 2, "business": 1})
    monkeypatch.setattr(pv.requests, "patch", lambda url, **kw: calls.append((url, kw["json"])) or _Resp())
    assert pv.apply_document_permissions(7, "shared")
    assert pv.apply_document_permissions(7, "private")
    assert calls[0] == ("http://p/api/documents/7/", {"set_permissions": {"view": {"users": [], "groups": [2]},
                                                                           "change": {"users": [], "groups": []}}})
    assert calls[1][1]["set_permissions"]["view"]["groups"] == []       # private takes the group away again


def test_task_lookup(monkeypatch):
    monkeypatch.setattr(pv, "_settings", lambda: {"api_key": "k", "base_url": "http://p"})
    monkeypatch.setattr(pv.requests, "get", lambda url, **kw: _Resp(data=[{"related_document": "12"}]))
    assert pv.document_id_for_task("abc") == 12
    monkeypatch.setattr(pv.requests, "get", lambda url, **kw: _Resp(data=[{"related_document": None}]))
    assert pv.document_id_for_task("abc") is None


def test_parents_group_holds_the_adults_only(monkeypatch, fresh_app):
    from tests.conftest import seed_user
    from backend.database import get_conn
    ids = {"dirk": seed_user(name="Dirk", role="platform_admin", email="d@x.local", password="pytestpw123"),
           "beate": seed_user(name="Beate", role="member", email="b@x.local", password="pytestpw123"),
           "kid": seed_user(name="Yorik", role="restricted", email="k@x.local", password="pytestpw123")}
    with get_conn() as conn:
        for n, (key, uid) in enumerate(ids.items(), start=8):
            conn.execute("UPDATE user_profiles SET paperless_user_id = ? WHERE id = ?", (n, uid))
        conn.commit()
    groups = {8: [2], 9: [2], 10: [2, 5]}                      # the child was (wrongly) in parents=5
    patched = {}
    monkeypatch.setattr(pv, "_settings", lambda: {"api_key": "k", "base_url": "http://p"})
    monkeypatch.setattr(pv.requests, "get", lambda url, **kw: _Resp(data={"groups": groups[int(url.rstrip("/").split("/")[-1])]}))
    monkeypatch.setattr(pv.requests, "patch", lambda url, **kw: patched.update({int(url.rstrip("/").split("/")[-1]): kw["json"]["groups"]}) or _Resp())
    assert pv.sync_parents_group(5) == 2
    assert patched == {8: [2, 5], 9: [2, 5], 10: [2]}          # adults in, child out, household untouched

    calls = []
    monkeypatch.setattr(pv, "_ensure_groups", lambda base, headers: {"household": 2, "parents": 5})
    monkeypatch.setattr(pv, "sync_parents_group", lambda gid=None: 2)
    monkeypatch.setattr(pv.requests, "patch", lambda url, **kw: calls.append(kw["json"]) or _Resp())
    assert pv.apply_document_permissions(7, "parents")
    assert calls[0]["set_permissions"]["view"]["groups"] == [5]


def test_default_owner_workflow_fires_on_consumption_only(monkeypatch):
    """The "Document Added" trigger ignores `sources`, so the old workflow
    took Beate's uploads away from her (seen on the live box 2026-09-22).
    A new install registers "Consumption Started"; an old one is repaired."""
    posted, patched = [], []
    monkeypatch.setattr(pv.requests, "get", lambda url, **kw: _Resp(data={"results": []}))
    monkeypatch.setattr(pv.requests, "post", lambda url, **kw: posted.append(kw["json"]) or _Resp(data={"id": 9}))
    monkeypatch.setattr(pv.requests, "patch", lambda url, **kw: patched.append((url, kw["json"])) or _Resp())
    assert pv._ensure_default_owner_workflow("http://p", {}, 3, parents_group_id=4) == 9
    assert posted[0]["triggers"] == [{"type": 1, "sources": [1], "filter_filename": "*"}]
    assert posted[0]["actions"] == [{"type": 1, "assign_owner": 3, "assign_view_groups": [4]}]   # the parents see the scans

    old = {"id": 1, "name": pv._DEFAULT_OWNER_WORKFLOW_NAME,
           "triggers": [{"type": 2, "sources": [1]}], "actions": [{"type": 1, "assign_owner": 3}]}
    monkeypatch.setattr(pv.requests, "get", lambda url, **kw: _Resp(data={"results": [old]}))
    assert pv._ensure_default_owner_workflow("http://p", {}, 3, parents_group_id=4) == 1
    assert patched == [("http://p/api/workflows/1/",
                        {"triggers": [{"type": 1, "sources": [1], "filter_filename": "*"}],
                         "actions": [{"type": 1, "assign_owner": 3, "assign_view_groups": [4]}]})]

    # a workflow with the right trigger but without the parents group is repaired too
    half = dict(old, triggers=[{"type": 1, "sources": [1]}])
    monkeypatch.setattr(pv.requests, "get", lambda url, **kw: _Resp(data={"results": [half]}))
    assert pv._ensure_default_owner_workflow("http://p", {}, 3, parents_group_id=4) == 1
    assert len(patched) == 2

    good = dict(half, actions=[{"type": 1, "assign_owner": 3, "assign_view_groups": [4]}])
    monkeypatch.setattr(pv.requests, "get", lambda url, **kw: _Resp(data={"results": [good]}))
    assert pv._ensure_default_owner_workflow("http://p", {}, 3, parents_group_id=4) == 1
    assert len(patched) == 2                                   # a correct workflow is left alone
