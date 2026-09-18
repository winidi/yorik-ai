"""The calendar list follows the spaces rule: no admin exception."""
from backend import calendars


def _cal(owner, space_id):
    return {"id": 1, "owner_user_id": owner, "space_id": space_id, "hide_from_admin": 0}


def test_owner_writes():
    assert calendars.effective_access("u1", "member", _cal("u1", None)) == "write"


def test_admin_gets_nothing_without_a_space():
    for role in ("platform_admin", "admin"):
        assert calendars.effective_access("u1", role, _cal("u2", None)) is None


def test_space_level_decides_for_the_calendar_area(monkeypatch):
    from backend import spaces
    seen = {}

    def fake(user_id, space_id, role=None, area=None):
        seen["area"] = area
        return "read"

    monkeypatch.setattr(spaces, "user_space_level", fake)
    assert calendars.effective_access("u1", "platform_admin", _cal("u2", 5)) == "read"
    assert seen["area"] == "calendar"
