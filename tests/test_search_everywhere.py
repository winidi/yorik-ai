"""Universal search: every area, by keyword and by meaning, and only
what the person may see (docs/plans/2026-09-19-suche-ueberall.md)."""

from __future__ import annotations

import pytest

from tests.conftest import login_client

CONCEPTS = (("strom", "stadtwerke", "energie", "abschlag"), ("zahnarzt", "arzt", "praxis"),
            ("urlaub", "ferien", "reise"))


def _fake_embed_many(texts):
    """A vector per text: one axis per concept, plus a rest axis."""
    out = []
    for t in texts:
        low = t.lower()
        v = [0.0] * 384
        for i, words in enumerate(CONCEPTS):
            if any(w in low for w in words):
                v[i] = 1.0
        if not any(v):
            v[10] = 1.0
        n = sum(x * x for x in v) ** 0.5
        out.append([x / n for x in v])
    return out


@pytest.fixture
def house(fresh_app, monkeypatch):
    from backend import search_index, spaces as S
    from backend.calendars import ensure_calendars_for_user
    monkeypatch.setattr(search_index, "embed_many", _fake_embed_many)
    dirk_c, dirk = login_client(fresh_app, role="platform_admin", name="Dirk", email="d@example.local")
    beate_c, beate = login_client(fresh_app, role="member", name="Beate", email="b@example.local")
    S.ensure_workspace_exists(dirk, "Dirk")
    for uid, name in ((dirk, "Dirk"), (beate, "Beate")):
        S.ensure_personal_space(uid, name)
        ensure_calendars_for_user(uid, name)
    return {"dirk": (dirk_c, dirk), "beate": (beate_c, beate)}


def _personal_calendar(uid):
    from backend.database import get_conn
    with get_conn() as conn:
        return int(conn.execute("SELECT id FROM calendars WHERE owner_user_id = ? AND kind = 'personal'",
                                (uid,)).fetchone()["id"])


def _add_event(uid, title, visibility="default"):
    from backend import spaces as S
    from backend.database import get_conn
    with get_conn() as conn:
        conn.execute("INSERT INTO events (title, starts_at, ends_at, all_day, calendar_id, owner_user_id, "
                     "visibility, space_id) VALUES (?, '2026-09-21T10:00:00', '2026-09-21T11:00:00', 0, ?, ?, ?, ?)",
                     (title, _personal_calendar(uid), uid, visibility, S.personal_space_id(uid)))
        conn.commit()


def _add_task(uid, title, notes=None):
    from backend import spaces as S
    from backend.database import get_conn
    with get_conn() as conn:
        conn.execute("INSERT INTO tasks (title, notes, done, created_by_user_id, space_id) VALUES (?, ?, 0, ?, ?)",
                     (title, notes, uid, S.personal_space_id(uid)))
        conn.commit()


def _hits(client, q, source):
    r = client.get("/api/search", params={"q": q})
    assert r.status_code == 200, r.text
    return [h["title"] for h in r.json()["results"].get(source, [])]


def test_calendar_search_follows_calendar_visibility(house):
    dirk_c, dirk = house["dirk"]; beate_c, beate = house["beate"]
    _add_event(beate, "Beates Yogakurs")
    _add_event(beate, "Beates Geheimnis Yoga", visibility="private")
    assert set(_hits(beate_c, "yoga", "calendar")) == {"Beates Yogakurs", "Beates Geheimnis Yoga"}
    assert _hits(dirk_c, "yoga", "calendar") == []                   # operator, no exception

    assert beate_c.put(f"/api/sharing/{dirk}", json={"areas": ["calendar"], "level": "read"}).status_code == 200
    assert _hits(dirk_c, "yoga", "calendar") == ["Beates Yogakurs"]  # shared, but never the private one


def test_new_sources_by_keyword_with_visibility(house):
    from backend.database import get_conn
    from backend import spaces as S
    dirk_c, dirk = house["dirk"]; beate_c, beate = house["beate"]
    _add_task(dirk, "Winterreifen bestellen", notes="beim Händler in Celle")
    with get_conn() as conn:
        conn.execute("INSERT INTO contacts (display_name, kind, status, notes, created_by_user_id, space_id) "
                     "VALUES ('Reifen Müller', 'person', 'active', 'Winterreifen und Felgen', ?, ?)",
                     (dirk, S.personal_space_id(dirk)))
        conn.execute("INSERT INTO compose_drafts (user_id, kind, subject, body_html, created_at, updated_at) "
                     "VALUES (?, 'letter', 'Reklamation Winterreifen', '<p>Sehr geehrte Damen</p>', "
                     "'2026-09-19T10:00:00', '2026-09-19T10:00:00')", (dirk,))
        rid = conn.execute("INSERT INTO recordings (owner_user_id, space_id, title, kind, status, started_at, created_at) "
                           "VALUES (?, ?, 'Abendessen', 'dinner', 'done', '2026-09-18T19:00:00', '2026-09-18T19:00:00') "
                           "RETURNING id", (dirk, S.personal_space_id(dirk))).fetchone()["id"]
        conn.execute("INSERT INTO recording_segments (recording_id, seq, start_s, end_s, speaker_label, text) "
                     "VALUES (?, 1, 0, 4, 'Sprecher 1', 'Wir brauchen noch Winterreifen fürs Auto.')", (rid,))
        conn.commit()

    assert _hits(dirk_c, "winterreifen", "tasks") == ["Winterreifen bestellen"]
    assert _hits(dirk_c, "celle händler", "tasks") == ["Winterreifen bestellen"]    # every word, any column
    assert _hits(dirk_c, "winterreifen", "contacts") == ["Reifen Müller"]
    assert _hits(dirk_c, "winterreifen", "drafts") == ["Reklamation Winterreifen"]
    assert _hits(dirk_c, "winterreifen", "recordings") == ["Abendessen"]
    for source in ("tasks", "contacts", "drafts", "recordings"):
        assert _hits(beate_c, "winterreifen", source) == [], source


def test_found_by_meaning_after_the_sweep(house):
    from backend import search_index
    dirk_c, dirk = house["dirk"]; beate_c, _ = house["beate"]
    _add_task(dirk, "Abschlag Stadtwerke überweisen")
    _add_task(dirk, "Zahnarzt anrufen")
    assert _hits(dirk_c, "Stromrechnung", "tasks") == []             # no such word anywhere

    assert search_index.sweep()["tasks"] == 2
    assert _hits(dirk_c, "Stromrechnung", "tasks") == ["Abschlag Stadtwerke überweisen"]
    assert _hits(beate_c, "Stromrechnung", "tasks") == []            # the index knows no shortcuts


def test_sweep_follows_changes_and_deletes(house):
    from backend import search_index
    from backend.database import get_conn
    _, dirk = house["dirk"]
    _add_task(dirk, "Urlaub buchen")
    assert search_index.sweep()["tasks"] == 1
    assert search_index.sweep()["tasks"] == 0                        # nothing changed
    with get_conn() as conn:
        conn.execute("UPDATE tasks SET notes = 'Ferienhaus an der Ostsee' WHERE title = 'Urlaub buchen'")
        conn.commit()
    assert search_index.sweep()["tasks"] == 1                        # hash changed
    with get_conn() as conn:
        conn.execute("DELETE FROM tasks WHERE title = 'Urlaub buchen'")
        conn.commit()
    search_index.sweep()
    assert search_index.stats().get("tasks", 0) == 0


def test_chunking_keeps_the_head_and_the_limit():
    from backend.search_index import chunk_text
    assert chunk_text("ok", 3) == []
    text = "Betreff Stromrechnung. " + "Ein Satz über gar nichts. " * 120
    chunks = chunk_text(text, 3)
    assert len(chunks) == 3 and chunks[0].startswith("Betreff Stromrechnung")
    assert all(len(c) <= 600 for c in chunks)


def test_model_switch_rebuilds_and_never_mixes(house, monkeypatch):
    from backend import search_index
    from backend.database import get_conn
    dirk_c, dirk = house["dirk"]
    _add_task(dirk, "Abschlag Stadtwerke überweisen")
    assert search_index.sweep()["tasks"] == 1

    monkeypatch.setattr(search_index, "model_tag", lambda: "another-model")
    assert _hits(dirk_c, "Stromrechnung", "tasks") == []             # old vectors are not compared
    assert search_index.sweep()["tasks"] == 1                        # re-embedded with the new model
    assert _hits(dirk_c, "Stromrechnung", "tasks") == ["Abschlag Stadtwerke überweisen"]
    with get_conn() as conn:
        models = {r["model"] for r in conn.execute("SELECT DISTINCT model FROM search_chunks").fetchall()}
    assert models == {"another-model"}


def test_switch_in_the_settings(house):
    from backend import search_index
    dirk_c, dirk = house["dirk"]; beate_c, _ = house["beate"]
    _add_task(dirk, "Abschlag Stadtwerke überweisen")
    search_index.sweep()
    assert _hits(dirk_c, "Stromrechnung", "tasks") == ["Abschlag Stadtwerke überweisen"]

    assert beate_c.put("/api/search/index", json={"enabled": False}).status_code == 403
    st = dirk_c.put("/api/search/index", json={"enabled": False}).json()
    assert st["enabled"] is False and st["embedder"] == "bundled"
    assert _hits(dirk_c, "Stromrechnung", "tasks") == []             # keyword only
    assert _hits(dirk_c, "stadtwerke", "tasks") == ["Abschlag Stadtwerke überweisen"]
    _add_task(dirk, "Zahnarzt anrufen")
    assert search_index.sweep() == {}                                # the indexer rests

    st = dirk_c.put("/api/search/index", json={"enabled": True}).json()
    assert st["enabled"] is True
    assert search_index.sweep()["tasks"] == 1                        # catches up
    tasks = next(x for x in dirk_c.get("/api/search/index").json()["sources"] if x["source"] == "tasks")
    assert tasks == {"source": "tasks", "indexed": 2, "total": 2}

    assert dirk_c.put("/api/search/index", json={"embedder": "service"}).status_code == 400   # none installed
    assert dirk_c.post("/api/search/index/rebuild").json()["dropped"] >= 2
