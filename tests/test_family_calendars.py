"""A family sees each other's calendars by default — as ordinary,
untickable sharing, not as an admin exception. Plan calendars stay
with their owner."""

from __future__ import annotations

import pytest

from tests.conftest import login_client


@pytest.fixture
def family(fresh_app):
    from backend import spaces as S
    from backend.calendars import ensure_calendars_for_user
    people = {}
    for key, role, name in (("dirk", "platform_admin", "Dirk"), ("beate", "member", "Beate"),
                            ("kid", "restricted", "Kid"), ("butler", "admin", "Test Butler")):
        client, uid = login_client(fresh_app, role=role, name=name, email=f"{key}@example.local")
        people[key] = (client, uid, role)
    S.ensure_workspace_exists(people["dirk"][1], "Dirk")
    for key, (_, uid, role) in people.items():
        S.ensure_personal_space(uid, key)
        ensure_calendars_for_user(uid, key)
        S.add_user_to_household(uid, "read" if role == "restricted" else "write")
    return people


def _event(owner_id, title, *, kind="personal", visibility="default"):
    from backend.calendars import create_calendar
    from backend.database import get_conn
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM calendars WHERE owner_user_id = ? AND kind = ? ORDER BY id LIMIT 1",
                           (owner_id, kind)).fetchone()
    cal_id = int(row["id"]) if row else create_calendar(name="Plan", owner_user_id=owner_id, kind=kind)
    with get_conn() as conn:
        conn.execute("INSERT INTO events (title, starts_at, ends_at, all_day, calendar_id, "
                     "owner_user_id, visibility) VALUES (?, '2026-09-21T10:00:00', '2026-09-21T11:00:00', 0, "
                     "?, ?, ?)", (title, cal_id, owner_id, visibility))
        conn.commit()


def _titles(client):
    r = client.get("/api/events?start=2026-09-21&end=2026-09-22")
    assert r.status_code == 200, r.text
    data = r.json()
    return {e["title"] for e in (data.get("events") if isinstance(data, dict) else data)}


def _owners_in_sidebar(client, me):
    return {c["owner_user_id"] for c in client.get("/api/calendars").json()
            if c["kind"] == "personal" and c["owner_user_id"] != me}


def test_no_default_until_it_is_switched_on(family):
    dirk_c, dirk, _ = family["dirk"]
    _event(family["beate"][1], "Beates Termin")
    assert "Beates Termin" not in _titles(dirk_c)
    assert _owners_in_sidebar(dirk_c, dirk) == set()      # the operator has no exception


def test_household_switch_shares_both_ways_read_only(family):
    dirk_c, dirk, _ = family["dirk"]; beate_c, beate, _ = family["beate"]
    kid_c, kid, _ = family["kid"]; _, butler, _ = family["butler"]
    _event(beate, "Beates Termin"); _event(dirk, "Dirks Termin"); _event(kid, "Kids Termin")
    _event(beate, "Geschenk kaufen", visibility="private")
    _event(beate, "Beates Planblock", kind="plan")

    assert beate_c.post("/api/sharing/family-calendars", json={}).status_code == 403
    r = dirk_c.post("/api/sharing/family-calendars", json={"exclude_user_ids": [butler]})
    assert r.status_code == 200 and r.json()["added"] == 6, r.text
    assert dirk_c.post("/api/sharing/family-calendars", json={"exclude_user_ids": [butler]}).json()["added"] == 0

    seen = _titles(dirk_c)
    assert {"Beates Termin", "Kids Termin", "Dirks Termin"} <= seen
    assert "Geschenk kaufen" not in seen and "Beates Planblock" not in seen
    assert {"Dirks Termin", "Beates Termin"} <= _titles(kid_c)
    assert _owners_in_sidebar(dirk_c, dirk) == {beate, kid}       # no Test Butler
    assert all(c["access_level"] == "read" for c in dirk_c.get("/api/calendars").json()
               if c["owner_user_id"] in (beate, kid) and c["kind"] == "personal")

    # it is ordinary sharing: Beate sees it and takes it back
    row = next(m for m in beate_c.get("/api/sharing").json()["members"] if m["user_id"] == dirk)
    assert row["i_share"] == {"areas": ["calendar"], "level": "read"}
    assert beate_c.put(f"/api/sharing/{dirk}", json={"areas": []}).status_code == 200
    assert "Beates Termin" not in _titles(dirk_c)


def test_new_account_joins_the_default_but_not_an_opt_out(family):
    from backend import spaces as S
    from backend.calendars import ensure_calendars_for_user
    dirk_c, dirk, _ = family["dirk"]; beate_c, beate, _ = family["beate"]
    _, kid, _ = family["kid"]; _, butler, _ = family["butler"]
    dirk_c.post("/api/sharing/family-calendars", json={"exclude_user_ids": [butler]})
    for other in (dirk, kid):                                   # Beate opts out
        beate_c.put(f"/api/sharing/{other}", json={"areas": []})

    new_c, new = login_client(beate_c.app, role="restricted", name="Yarik", email="yarik@example.local")
    S.ensure_personal_space(new, "Yarik"); ensure_calendars_for_user(new, "Yarik")
    S.add_user_to_household(new, "read")
    added = S.share_calendars_in_household(only_user=new)

    assert (new, dirk) in added and (dirk, new) in added
    assert (beate, new) not in added                            # her choice stands
    assert (new, beate) in added
