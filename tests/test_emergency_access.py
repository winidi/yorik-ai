"""Emergency access: an adult can see everything for a while, not
casually, and the others learn of it (decided with Dirk 2026-09-22;
backend/emergency.py)."""

from __future__ import annotations

import pytest

from tests.conftest import login_client


@pytest.fixture
def household(fresh_app):
    from backend import spaces as S
    from backend.database import get_conn
    dirk_c, dirk = login_client(fresh_app, role="platform_admin", name="Dirk", email="d@example.local")
    beate_c, beate = login_client(fresh_app, role="member", name="Beate", email="b@example.local")
    kid_c, kid = login_client(fresh_app, role="restricted", name="Kid", email="k@example.local")
    S.ensure_workspace_exists(dirk, "Dirk")
    for uid, name in ((dirk, "Dirk"), (beate, "Beate"), (kid, "Kid")):
        S.ensure_personal_space(uid, name)
    with get_conn() as conn:
        conn.execute("INSERT INTO tasks (title, done, created_by_user_id, space_id) VALUES ('Dirks Vertrag kündigen', 0, ?, ?)",
                     (dirk, S.personal_space_id(dirk)))
        conn.commit()
    return {"dirk": (dirk_c, dirk), "beate": (beate_c, beate), "kid": (kid_c, kid)}


def _titles(client):
    return {t["title"] for t in client.get("/api/tasks").json()}


def test_not_casually(household):
    beate_c, _ = household["beate"]; kid_c, _ = household["kid"]
    assert kid_c.get("/api/emergency-access").status_code == 403
    assert kid_c.post("/api/emergency-access", json={"reason": "ich will alles sehen", "hours": 2, "password": "pytestpw123"}).status_code == 400
    r = beate_c.post("/api/emergency-access", json={"reason": "kurz", "hours": 2, "password": "pytestpw123"})
    assert r.status_code == 400 and "characters" in r.json()["detail"]
    r = beate_c.post("/api/emergency-access", json={"reason": "Dirk liegt im Krankenhaus", "hours": 48, "password": "pytestpw123"})
    assert r.status_code == 400 and "24" in r.json()["detail"]
    r = beate_c.post("/api/emergency-access", json={"reason": "Dirk liegt im Krankenhaus", "hours": 2, "password": "falsch"})
    assert r.status_code == 400 and "password" in r.json()["detail"]
    assert beate_c.get("/api/emergency-access").json()["mine"] is None


def test_it_opens_the_house_and_the_others_are_told(household):
    from backend import emergency as E
    from backend.database import get_conn
    dirk_c, dirk = household["dirk"]; beate_c, beate = household["beate"]; kid_c, kid = household["kid"]
    assert _titles(beate_c) == set()                                                   # nothing shared: nothing seen
    r = beate_c.post("/api/emergency-access", json={"reason": "Dirk liegt im Krankenhaus, ich brauche die Verträge",
                                                    "hours": 2, "password": "pytestpw123"})
    assert r.status_code == 200, r.text
    E._cache.clear()
    assert "Dirks Vertrag kündigen" in _titles(beate_c)                               # now she sees his task
    assert "Dirks Vertrag kündigen" not in _titles(kid_c)                              # only her
    status = beate_c.get("/api/emergency-access").json()
    assert status["mine"]["reason"].startswith("Dirk liegt") and status["history"][0]["user_name"] == "Beate"
    assert status["history"][0]["active"] is True
    # Dirk (and no child) was told, by name, with the reason
    with get_conn() as conn:
        rows = conn.execute("SELECT user_id, title, body FROM notifications WHERE kind = 'emergency_access' ORDER BY id").fetchall()
    assert [str(r["user_id"]) for r in rows] == [dirk]
    assert "Beate" in rows[0]["title"] and "Krankenhaus" in rows[0]["body"]
    # Dirk reads the same log
    assert dirk_c.get("/api/emergency-access").json()["history"][0]["user_name"] == "Beate"
    # she ends it early: the house closes again and Dirk hears of that too
    assert beate_c.delete(f"/api/emergency-access/{status['mine']['id']}").status_code == 200
    E._cache.clear()
    assert _titles(beate_c) == set()
    with get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM notifications WHERE kind = 'emergency_access'").fetchone()["n"]
    assert n == 2
    assert beate_c.delete("/api/emergency-access/999").status_code == 404
