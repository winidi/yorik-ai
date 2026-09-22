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
    assert titles["Küche"]["category"] == "" and b["routine_log"] == []
    assert b["week_start"] == monday.isoformat() and b["days"] == 7


def test_board_feed_answers_a_signed_in_member_too(fresh_app):
    from fastapi.testclient import TestClient
    from tests.conftest import login_client
    client, uid = login_client(fresh_app, role="member", name="Beate")
    assert client.get("/api/ambient/board").json()["people"] == []          # signed in, nobody consented yet
    assert TestClient(fresh_app).get("/api/ambient/board").status_code in (401, 403)


def test_timetable_is_for_children_and_kept_by_parents_and_the_child(fresh_app):
    from fastapi.testclient import TestClient
    from tests.conftest import login_client
    from backend.database import get_conn
    mum_c, mum = login_client(fresh_app, role="member", name="Beate", email="b@example.local")
    kid_c, kid = login_client(fresh_app, role="restricted", name="Yorik", email="k@example.local")
    bro_c, bro = login_client(fresh_app, role="restricted", name="Yarik", email="y@example.local")
    with get_conn() as conn:
        conn.execute("UPDATE user_profiles SET kiosk_agenda_consent = 1"); conn.commit()
    people = mum_c.get("/api/ambient/timetable").json()["people"]
    assert {p["name"] for p in people} == {"Yorik", "Yarik"}                  # children only
    assert len(people[0]["timetable"]["periods"]) == 6 and people[0]["filled"] is False
    plan = {"periods": [{"start": "08:00", "end": "08:45"}, {"start": "nonsense", "end": "09:35"}],
            "cells": {"0-0": {"subject": "Mathe", "room": "R 12"}, "4-1": {"subject": " Sport "}, "5-0": {"subject": "Samstag"}, "0-7": {"subject": "zu spät"}, "1-1": {"subject": ""}}}
    r = mum_c.put(f"/api/ambient/timetable/{kid}", json=plan)
    assert r.status_code == 200, r.text
    saved = {p["id"]: p for p in kid_c.get("/api/ambient/timetable").json()["people"]}[kid]
    assert saved["filled"] and saved["timetable"]["cells"] == {"0-0": {"subject": "Mathe", "room": "R 12"}, "4-1": {"subject": "Sport", "room": ""}}
    assert saved["timetable"]["periods"][1] == {"start": "", "end": "09:35"}
    assert kid_c.put(f"/api/ambient/timetable/{kid}", json=plan).status_code == 200      # the child keeps its own
    assert kid_c.put(f"/api/ambient/timetable/{bro}", json=plan).status_code == 403      # not the brother's
    assert mum_c.put(f"/api/ambient/timetable/{mum}", json=plan).status_code == 404      # adults have none
    assert TestClient(fresh_app).get("/api/ambient/timetable").status_code in (401, 403)
    assert TestClient(fresh_app).put(f"/api/ambient/timetable/{kid}", json=plan).status_code in (401, 403)


def test_wall_routes_survive_uuid_user_ids(fresh_app, monkeypatch):
    """Every wall route reads the bound user's id, and that id is a UUID.

    The live wall died on `int(meta["user_id"])` — the slideshow answered
    500 and the frontend swallowed it, so the wall showed a black
    rectangle and nobody could tell why. Same coercion sat in the idle
    bundle. These are the plain calls; they only have to not crash.
    """
    wall_user = seed_user(name="Wall", role="member")
    c = _kiosk_client(fresh_app, wall_user, monkeypatch)

    r = c.get("/api/ambient/slideshow")
    assert r.status_code == 200, r.text
    # No album and no today-photos on this device: a clean "nothing set
    # up" answer, not an exception.
    assert r.json()["configured"] is False

    assert c.get("/api/ambient/idle").status_code == 200


def test_pin_switch_takes_a_uuid_and_a_wrong_pin_is_not_a_crash(fresh_app, monkeypatch):
    """The wall's PIN pad answered 500 for every entry, right and wrong
    alike: the user id went through int(). A wrong PIN is a 401, a right
    one switches the session over."""
    from backend import auth_sessions

    wall_user = seed_user(name="Wall", role="member")
    beate = seed_user(name="Beate", role="member")
    auth_sessions.set_pin(beate, "2468")
    c = _kiosk_client(fresh_app, wall_user, monkeypatch)
    # Marking a device as a kiosk stamps trusted_until a year out; that
    # is what lets the wall's PIN pad run at all (see auth_pin_switch).
    from backend.database import get_conn
    with get_conn() as conn:
        conn.execute("UPDATE sessions SET trusted_until = ? WHERE id = ?",
                     ("2099-01-01 00:00:00", c.cookies.get(auth_sessions.COOKIE_NAME)))
        conn.commit()

    bad = c.post("/api/auth/pin-switch", json={"user_id": str(beate), "pin": "1111"})
    assert bad.status_code == 401, bad.text

    ok = c.post("/api/auth/pin-switch", json={"user_id": str(beate), "pin": "2468"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["user"]["id"] == str(beate)


def test_the_wall_app_sets_itself_up_and_the_device_stays_a_wall(fresh_app, monkeypatch):
    """Opening the wall app is the whole setup.

    The two calls the wrapper makes on first launch are the same ones
    Settings → Geräte makes, with the same admin-only, trusted-LAN-only
    guards. What matters afterwards is that the WALL, not the session,
    is what Yorik remembers: a PIN switch or an app restart mints a new
    session, and the tablet has to keep showing the wall through it.
    """
    from fastapi.testclient import TestClient
    from backend import auth_sessions
    from backend.database import get_conn

    admin = seed_user(name="Dirk", role="platform_admin")
    from backend import main as M
    monkeypatch.setattr(M, "is_trusted_lan_request", lambda request: True)

    wall_uuid = "11111111-2222-3333-4444-555555555555"
    ua = ("Mozilla/5.0 (Linux; Android 15; wv) AppleWebKit/537.36 "
          "YorikWall/0.1.0 (Xiaomi 2405CPCFBG)")
    sid = auth_sessions.create_session(admin, user_agent=ua, ip="127.0.0.1")
    c = TestClient(fresh_app, headers={"user-agent": ua,
                                       "x-yorik-wall-device": wall_uuid})
    c.cookies.set(auth_sessions.COOKIE_NAME, sid)

    # Before: an ordinary session, and the wall routes say no.
    assert c.get("/api/ambient/slideshow").status_code in (401, 403)

    # What the app does on first launch, with the admin's own rights.
    mine = [d for d in c.get("/api/devices").json() if d["is_current"]][0]
    assert c.post(f"/api/devices/{mine['id']}/kiosk",
                  json={"is_kiosk": True, "device_label": "Xiaomi 2405CPCFBG",
                        "show_today": True}).status_code == 200
    assert c.post("/api/devices/trust").status_code == 200
    assert c.patch("/api/ambient/mode", json={"mode": "calendar"}).status_code == 200

    assert c.get("/api/ambient/slideshow").json()["show_today"] is True

    # A plain new session on the same tablet — a fresh sign-in, nothing
    # kiosk about it. The wall keeps working because the TABLET is
    # trusted, which is the whole point of pinning the policy to the
    # device: this is where the black screen used to come back.
    fresh_sid = auth_sessions.create_session(admin, user_agent=ua, ip="127.0.0.1")
    with get_conn() as conn:
        assert conn.execute("SELECT is_kiosk FROM sessions WHERE id = ?",
                            (fresh_sid,)).fetchone()["is_kiosk"] == 0
    c2 = TestClient(fresh_app, headers={"user-agent": ua,
                                        "x-yorik-wall-device": wall_uuid})
    c2.cookies.set(auth_sessions.COOKIE_NAME, fresh_sid)
    assert c2.get("/api/ambient/slideshow").status_code == 200
    assert c2.get("/api/ambient/mode").json()["mode"] == "calendar"

    # And the session a PIN switch mints — it carries the wall's UUID —
    # comes out flagged, so Settings → Geräte shows the tablet for what
    # it is instead of a stray browser session.
    switched = auth_sessions.create_session(admin, user_agent=ua, ip="127.0.0.1",
                                            wall_device_id=wall_uuid)
    with get_conn() as conn:
        row = conn.execute("SELECT is_kiosk, kiosk_show_today_photos FROM sessions "
                           "WHERE id = ?", (switched,)).fetchone()
    assert row["is_kiosk"] == 1 and row["kiosk_show_today_photos"] == 1

    # And a browser on the same LAN that is not the wall stays out.
    plain = TestClient(fresh_app)
    assert plain.get("/api/ambient/slideshow").status_code in (401, 403)
