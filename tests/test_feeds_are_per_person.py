"""The small feeds around the chat and the home screen show one person's
own things. Until 2026-09-22 `today`, the @-mention popover, the family
board (for a signed-in member) and the worker list read the whole
household (audit docs/audits/2026-09-22-berechtigungen.md, 2.5–2.7, 3.1)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from tests.conftest import login_client


@pytest.fixture
def household(fresh_app):
    from backend import spaces as S
    from backend.calendars import ensure_calendars_for_user
    from backend.database import get_conn
    dirk_c, dirk = login_client(fresh_app, role="platform_admin", name="Dirk", email="d@example.local")
    beate_c, beate = login_client(fresh_app, role="member", name="Beate", email="b@example.local")
    kid_c, kid = login_client(fresh_app, role="restricted", name="Kid", email="k@example.local")
    S.ensure_workspace_exists(dirk, "Dirk")
    for uid, name in ((dirk, "Dirk"), (beate, "Beate"), (kid, "Kid")):
        S.ensure_personal_space(uid, name); ensure_calendars_for_user(uid, name)
    today = date.today()
    with get_conn() as conn:
        conn.execute("UPDATE user_profiles SET kiosk_agenda_consent = 1 WHERE id IN (?, ?)", (dirk, beate))
        for uid, title, vis in ((dirk, "Zahnarzt Dirk", "default"), (dirk, "Geheim Dirk", "private"), (beate, "Yoga Beate", "default")):
            cal = conn.execute("SELECT id FROM calendars WHERE owner_user_id=? AND kind='personal'", (uid,)).fetchone()["id"]
            conn.execute("INSERT INTO events (title, starts_at, ends_at, all_day, calendar_id, owner_user_id, visibility, notes) "
                         "VALUES (?, ?, ?, 0, ?, ?, ?, 'Notiz')",
                         (title, f"{today}T15:00:00", f"{today}T16:00:00", cal, uid, vis))
        for uid, title in ((dirk, "Dirks Aufgabe"), (beate, "Beates Aufgabe")):
            conn.execute("INSERT INTO tasks (title, done, created_by_user_id, space_id, due_date) VALUES (?, 0, ?, ?, ?)",
                         (title, uid, S.personal_space_id(uid), (today - timedelta(days=2)).isoformat()))
        for uid, name in ((dirk, "Dirks Kontakt"), (beate, "Beates Kontakt")):
            conn.execute("INSERT INTO contacts (display_name, kind, status, created_by_user_id, space_id, birthday) "
                         "VALUES (?, 'person', 'active', ?, ?, ?)", (name, uid, S.personal_space_id(uid), f"1980-{today:%m-%d}"))
        conn.commit()
    return {"dirk": (dirk_c, dirk), "beate": (beate_c, beate), "kid": (kid_c, kid)}


def test_today_is_the_persons_own_day(household):
    beate_c, _ = household["beate"]; kid_c, _ = household["kid"]; dirk_c, _ = household["dirk"]
    d = beate_c.get("/api/today").json()
    assert [e["title"] for e in d["events_today"]] == ["Yoga Beate"]
    assert [t["title"] for t in d["tasks_overdue_sample"]] == ["Beates Aufgabe"] and d["tasks_overdue_count"] == 1
    assert [b["display_name"] for b in d["birthdays_this_week"]] == ["Beates Kontakt"]
    k = kid_c.get("/api/today").json()
    assert k["events_today"] == [] and k["tasks_overdue_count"] == 0 and k["birthdays_this_week"] == []
    d = dirk_c.get("/api/today").json()                                  # the admin sees only his own too
    assert {e["title"] for e in d["events_today"]} == {"Zahnarzt Dirk", "Geheim Dirk"}
    assert d["tasks_overdue_count"] == 1


def test_mentions_offer_only_the_persons_contacts_and_events(household):
    beate_c, _ = household["beate"]; kid_c, _ = household["kid"]
    m = beate_c.get("/api/chat/mentions?prefix=&types=contact,event").json()
    assert [c["label"] for c in m["contact"]] == ["Beates Kontakt"]
    assert [e["label"] for e in m["event"]] == ["Yoga Beate"]
    m = kid_c.get("/api/chat/mentions?prefix=Dirk&types=contact,event").json()
    assert m["contact"] == [] and m["event"] == []


def test_board_hides_private_events_and_follows_the_viewers_rights(household, monkeypatch):
    from tests.test_kiosk_board import _kiosk_client
    from tests.conftest import seed_user
    dirk_c, dirk = household["dirk"]; beate_c, beate = household["beate"]; kid_c, _ = household["kid"]
    wall = _kiosk_client(household["dirk"][0].app, seed_user(name="Wall", role="member"), monkeypatch)
    b = wall.get("/api/ambient/board").json()
    assert {p["name"] for p in b["people"]} == {"Dirk", "Beate"}
    assert {e["title"] for e in b["events"]} == {"Zahnarzt Dirk", "Busy", "Yoga Beate"}   # private → Busy on the wall
    assert all(e["notes"] == "" for e in b["events"])                                    # notes never on the wall
    # a signed-in child: Dirk shares nothing with the child, so it sees people and tasks, not Dirk's events
    b = kid_c.get("/api/ambient/board").json()
    assert {p["name"] for p in b["people"]} == {"Dirk", "Beate"} and b["events"] == []
    # Dirk himself: his private event with its title and notes, Beate's not (she shares nothing)
    b = dirk_c.get("/api/ambient/board").json()
    assert {e["title"] for e in b["events"]} == {"Zahnarzt Dirk", "Geheim Dirk"}
    assert {e["notes"] for e in b["events"]} == {"Notiz"}
    # Beate shares her calendar with Dirk → he sees Yoga, still without her notes
    assert beate_c.put(f"/api/sharing/{dirk}", json={"areas": ["calendar"], "level": "read"}).status_code == 200
    b = dirk_c.get("/api/ambient/board").json()
    yoga = next(e for e in b["events"] if e["title"] == "Yoga Beate")
    assert yoga["notes"] == ""


def test_workers_of_a_mail_account_are_the_owners_only(household):
    from backend import workers as W
    from backend.database import get_conn
    dirk_c, dirk = household["dirk"]; beate_c, beate = household["beate"]; kid_c, _ = household["kid"]
    with get_conn() as conn:
        aid = conn.execute("INSERT INTO email_accounts (owner_user_id, email, imap_host, imap_username, smtp_host, smtp_username, credential_key) "
                           "VALUES (?, 'beate@gmx.example', 'i', 'b', 's', 'b', 'k') RETURNING id", (beate,)).fetchone()["id"]
        conn.commit()
    W.register(f"email_account_{aid}", kind="imap-idle"); W.heartbeat(f"email_account_{aid}", "ok", "beate@gmx.example idling")
    W.register("backup_scheduler", kind="scheduler"); W.heartbeat("backup_scheduler", "ok", "next run 03:00 at /srv/backup")
    try:
        names = lambda c: {w["name"]: w["detail"] for w in c.get("/api/dashboard/workers").json()["workers"]}
        assert f"email_account_{aid}" in names(beate_c)
        assert f"email_account_{aid}" not in names(dirk_c)                # the admin has no business with her mail worker
        assert f"email_account_{aid}" not in names(kid_c)
        assert names(beate_c)["backup_scheduler"] == ""                    # infrastructure detail is for admins
        assert names(dirk_c)["backup_scheduler"].startswith("next run")
    finally:
        with W._lock:
            W._workers.pop(f"email_account_{aid}", None); W._workers.pop("backup_scheduler", None)
