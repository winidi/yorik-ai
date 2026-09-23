"""Settings → Spaces answered 500 for everyone: the member list sorted
with SQLite's COLLATE NOCASE, which Postgres does not know. Found by the
e2e crawler."""

from tests.conftest import login_client


def test_space_detail_lists_members(fresh_app):
    from backend import spaces as S
    client, uid = login_client(fresh_app, role="platform_admin", name="Anna")
    S.ensure_workspace_exists(uid, "Anna")
    space_id = S.ensure_personal_space(uid, "Anna")
    r = client.get(f"/api/spaces/{space_id}")
    assert r.status_code == 200, r.text
