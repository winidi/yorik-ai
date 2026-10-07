"""Deadlines follow the machine (Dirk 2026-09-28: slower computers must
not make Yorik useless)."""

from __future__ import annotations

import asyncio

import pytest


@pytest.fixture(autouse=True)
def fresh_speed(monkeypatch):
    from backend import speed
    monkeypatch.setattr(speed, "_avg", {})


def test_a_slow_machine_gets_longer_deadlines_never_shorter():
    from backend import speed
    assert speed.budget(2.5, "embed") == 2.5                   # nothing measured: as on the reference
    speed.record("embed", 0.01)                                 # faster than the reference
    assert speed.budget(2.5, "embed") == 2.5                   # never tighter
    for _ in range(10):
        speed.record("embed", 0.24)                             # four times slower
    assert 9.0 < speed.budget(2.5, "embed") <= 10.0
    for _ in range(20):
        speed.record("embed", 60)                               # hopeless: capped
    assert speed.budget(2.5, "embed") == 2.5 * speed.MAX_FACTOR


def test_a_timeout_counts_so_the_deadline_catches_up(monkeypatch):
    from backend import search_index, speed
    monkeypatch.setattr(search_index, "enabled", lambda: True)
    monkeypatch.setattr(search_index, "use_service", lambda: True)
    def slow(texts, timeout=0):
        raise TimeoutError("Read timed out.")
    monkeypatch.setattr(search_index, "_post_embeddings", slow)
    limits = []
    for _ in range(4):
        limits.append(speed.budget(search_index.QUERY_TIMEOUT_S, "embed"))
        assert search_index.embed_query("server rechnung") is None
    assert limits[0] == 2.5 and limits[-1] > limits[0] * 5


def test_one_late_source_does_not_drop_the_others(fresh_app, monkeypatch):
    """One late source (photos) dropped every result until 2026-09-28."""
    import time
    from backend import search_routes
    from tests.conftest import seed_user
    uid = seed_user(name="Beate", role="member", email="b@example.com")
    monkeypatch.setattr(search_routes, "TOTAL_BUDGET_S", 0.5)
    # As on the reference machine: a slow CI runner would stretch 0.5 s past the 2 s sleep.
    from backend import speed
    monkeypatch.setattr(speed, "factor", lambda kind: 1.0)
    monkeypatch.setattr(search_routes, "_search_email",
                        lambda q, u, *a, **k: [{"source": "email", "id": 1, "title": "Rechnung"}])
    def slow(q, u, *a, **k):
        time.sleep(2)
        return [{"source": "immich", "id": "x"}]
    monkeypatch.setattr(search_routes, "_search_immich", slow)
    out = asyncio.run(search_routes.universal_search(q="rechnung", user={"id": uid, "role": "member"}))
    assert [h["id"] for h in out["results"]["email"]] == [1] and out["results"]["immich"] == []
