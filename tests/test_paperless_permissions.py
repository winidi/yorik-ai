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
