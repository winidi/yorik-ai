"""In-app update (backend/update_routes.py): status and the guards."""
from tests.conftest import login_client


def _fake(monkeypatch, behind=2, dirty="", unit=True):
    from backend import update_routes as u
    def git(*args, timeout=30):
        if args[0] == "log" and "HEAD..@{u}" in args:
            return 0, "\n".join(f"change {i}" for i in range(behind))
        if args[0] == "status":
            return 0, dirty
        if args[0] == "log":
            return 0, "abc1234 2026-09-26"
        if args[0] == "rev-parse":
            return 0, "origin/main"
        return 0, ""
    monkeypatch.setattr(u, "_git", git)
    monkeypatch.setattr(u, "_unit_installed", lambda: unit)
    started = []
    monkeypatch.setattr(u, "_systemctl", lambda *a: started.append(a) or (1 if a[0] == "is-active" else 0))
    return started


def test_status_reports_waiting_changes(fresh_app, monkeypatch):
    _fake(monkeypatch)
    admin, _ = login_client(fresh_app, role="admin")
    st = admin.get("/api/system/update").json()
    assert st["behind"] == 2 and st["can_update"] and not st["local_changes"]


def test_update_starts_the_unit(fresh_app, monkeypatch):
    started = _fake(monkeypatch)
    admin, _ = login_client(fresh_app, role="admin")
    assert admin.post("/api/system/update").status_code == 200
    assert ("start", "--no-block", "yorik-update.service") in started


def test_local_changes_are_never_updated_from_the_app(fresh_app, monkeypatch):
    _fake(monkeypatch, dirty=" M backend/main.py")
    admin, _ = login_client(fresh_app, role="admin")
    assert admin.post("/api/system/update").status_code == 409


def test_members_cannot_update(fresh_app, monkeypatch):
    _fake(monkeypatch)
    member, _ = login_client(fresh_app, role="member")
    assert member.post("/api/system/update").status_code == 403
