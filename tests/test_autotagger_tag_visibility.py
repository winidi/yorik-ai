"""Tags the autotagger creates must be visible to every family member.

Paperless shows an owned object only to its owner and to users it was
shared with. The autotagger used to create its taxonomy tags owned by
Yorik's service account, so a person's own Paperless user saw none of
them and the Documents folder view stayed empty although Settings
reported "tagged". Seen on the demo household on 2026-10-19.
"""

from backend import autotagger


class _Resp:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self._body = body if body is not None else {}
        self.text = ""

    def json(self):
        return self._body

    def raise_for_status(self):
        pass


def test_new_tag_is_created_unowned(monkeypatch):
    posted = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        return _Resp(200, {"results": []})

    def fake_post(url, headers=None, json=None, timeout=None):
        posted["url"] = url
        posted["json"] = json
        return _Resp(201, {"id": 42})

    monkeypatch.setattr(autotagger.requests, "get", fake_get)
    monkeypatch.setattr(autotagger.requests, "post", fake_post)

    tid = autotagger._ensure_paperless_tag(
        "Rechnung", "http://pl", {"Authorization": "Token x"}, {}, color="#3b82f6",
    )
    assert tid == 42
    assert posted["url"] == "http://pl/api/tags/"
    assert "owner" in posted["json"] and posted["json"]["owner"] is None
    assert posted["json"]["color"] == "#3b82f6"


def test_backfill_unshares_only_private_taxonomy_tags(monkeypatch):
    tags = [
        # ours, owned by the service account, shared with nobody -> fix
        {"id": 4, "name": "Rechnung", "owner": 3,
         "permissions": {"view": {"users": [], "groups": []}}},
        # ours, already unowned -> leave
        {"id": 5, "name": "Strom", "owner": None,
         "permissions": {"view": {"users": [], "groups": []}}},
        # ours, but a person shared it with a group on purpose -> leave
        {"id": 6, "name": "Gas", "owner": 8,
         "permissions": {"view": {"users": [], "groups": [2]}}},
        # not ours -> never touch
        {"id": 7, "name": "parents", "owner": 3,
         "permissions": {"view": {"users": [], "groups": []}}},
    ]
    seen_params = {}
    patched = []

    def fake_get(url, headers=None, params=None, timeout=None):
        seen_params.update(params or {})
        return _Resp(200, {"results": tags, "next": None})

    def fake_patch(url, headers=None, json=None, timeout=None):
        patched.append((url, json))
        return _Resp(200, {})

    monkeypatch.setattr(autotagger.requests, "get", fake_get)
    monkeypatch.setattr(autotagger.requests, "patch", fake_patch)

    n = autotagger._backfill_tag_visibility(
        "http://pl", {"Authorization": "Token x"}, {"Rechnung", "Strom", "Gas"},
    )
    assert n == 1
    assert seen_params.get("full_perms") == "true"
    assert patched == [("http://pl/api/tags/4/", {"owner": None})]
