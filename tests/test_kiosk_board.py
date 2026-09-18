"""The wall: a per-device display mode and the family board feed, both
behind the kiosk gate; only people who ticked the wall consent appear."""

from tests.conftest import seed_user


def _kiosk_client(app, uid, monkeypatch=None):
    from fastapi.testclient import TestClient
    if monkeypatch is not None:                       # TestClient's host is "testclient", not a LAN address
        from backend import main as M
        monkeypatch.setattr(M, "is_trusted_lan_request", lambda request: True)
    from backend import auth_sessions
    from backend.database import get_conn
    sid = auth_sessions.create_session(uid, user_agent="wall", ip="127.0.0.1")
    with get_conn() as conn:
        conn.execute("UPDATE sessions SET is_kiosk = 1, device_label = 'Wall' WHERE id = ?", (sid,)); conn.commit()
    c = TestClient(app)
    c.cookies.set(auth_sessions.COOKIE_NAME, sid)
    return c


def test_mode_is_per_device_and_kiosk_only(fresh_app, monkeypatch):
    from fastapi.testclient import TestClient
    wall_user = seed_user(name="Wall", role="member")
    c = _kiosk_client(fresh_app, wall_user, monkeypatch)
    assert c.get("/api/ambient/mode").json()["mode"] == "photos"
    assert c.patch("/api/ambient/mode", json={"mode": "board"}).json()["mode"] == "board"
    assert c.get("/api/ambient/mode").json()["mode"] == "board"
    assert c.patch("/api/ambient/mode", json={"mode": "disco"}).status_code == 400
    # another wall device keeps its own mode
    assert c.get("/api/ambient/mode", headers={"x-yorik-wall-device": "tablet-2"}).json()["mode"] == "photos"
    assert TestClient(fresh_app).get("/api/ambient/mode").status_code in (401, 403)


def test_board_shows_only_consenting_people(fresh_app, monkeypatch):
    from backend import spaces as _sp
    from backend.calendars import ensure_calendars_for_user
    from backend.database import get_conn
    wall_user = seed_user(name="Wall", role="member")
    dirk = seed_user(name="Dirk", role="admin", kiosk_agenda_consent=1)
    beate = seed_user(name="Beate", role="member", kiosk_agenda_consent=1)
    kid = seed_user(name="Kid", role="restricted")          # no consent → invisible on the wall
    for uid, n in ((dirk, "Dirk"), (beate, "Beate"), (kid, "Kid")):
        _sp.ensure_workspace_exists(uid, n); _sp.ensure_personal_space(uid, n); ensure_calendars_for_user(uid, n)
    from datetime import date, timedelta
    today = date.today(); monday = today - timedelta(days=today.weekday())
    with get_conn() as conn:
        cal = conn.execute("SELECT id FROM calendars WHERE owner_user_id=? AND kind='personal'", (dirk,)).fetchone()["id"]
        kcal = conn.execute("SELECT id FROM calendars WHERE owner_user_id=? AND kind='personal'", (kid,)).fetchone()["id"]
        conn.execute("INSERT INTO events (title, starts_at, ends_at, all_day, calendar_id, owner_user_id) VALUES ('Zahnarzt', ?, ?, 0, ?, ?)",
                     (f"{monday}T15:00:00", f"{monday}T16:00:00", cal, dirk))
        conn.execute("INSERT INTO events (title, starts_at, ends_at, all_day, calendar_id, owner_user_id) VALUES ('Geheim', ?, ?, 0, ?, ?)",
                     (f"{monday}T10:00:00", f"{monday}T11:00:00", kcal, kid))
        conn.execute("INSERT INTO tasks (title, done, created_by_user_id, due_date) VALUES ('Küche', 0, ?, ?)", (beate, today.isoformat()))
        tid = conn.execute("SELECT id FROM tasks WHERE title='Küche'").fetchone()["id"]
        conn.execute("INSERT INTO task_assignees (task_id, user_id) VALUES (?, ?)", (tid, beate))
        conn.execute("INSERT INTO tasks (title, done, created_by_user_id, recurrence_rule) VALUES ('Zähne putzen', 0, ?, 'daily')", (kid,))
        conn.execute("INSERT INTO tasks (title, done, done_at, created_by_user_id) VALUES ('Erledigt heute', 1, ?, ?)", (f"{today}T08:00:00", dirk))
        conn.execute("INSERT INTO tasks (title, done, done_at, created_by_user_id) VALUES ('Alt erledigt', 1, '2020-01-01T08:00:00', ?)", (dirk,))
        conn.commit()
    c = _kiosk_client(fresh_app, wall_user, monkeypatch)
    b = c.get("/api/ambient/board").json()
    assert {p["name"] for p in b["people"]} == {"Dirk", "Beate"} and all(p["color"] for p in b["people"])
    assert [e["title"] for e in b["events"]] == ["Zahnarzt"] and b["events"][0]["owner_id"] == dirk
    titles = {t["title"]: t for t in b["tasks"]}
    assert set(titles) == {"Küche", "Erledigt heute"}
    assert titles["Küche"]["assignee_ids"] == [beate] and titles["Erledigt heute"]["done"] is True
    assert b["week_start"] == monday.isoformat() and b["days"] == 7


def test_board_feed_answers_a_signed_in_member_too(fresh_app):
    from fastapi.testclient import TestClient
    from tests.conftest import login_client
    client, uid = login_client(fresh_app, role="member", name="Beate")
    assert client.get("/api/ambient/board").json()["people"] == []          # signed in, nobody consented yet
    assert TestClient(fresh_app).get("/api/ambient/board").status_code in (401, 403)
