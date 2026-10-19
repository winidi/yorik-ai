"""The box restarts itself when the server stops answering (F27,
2026-10-19: 8.5 h of an open port that answered nothing)."""

from __future__ import annotations

from backend import watchdog


def test_restarts_only_after_consecutive_failures():
    answers = iter([True, False, False, True, False, False, False, False])
    died = []
    w = watchdog.Watchdog(lambda: next(answers), lambda: died.append(1), failures_to_restart=4)
    results = [w.tick() for _ in range(8)]
    assert results[:7] == [False] * 7           # one good answer resets the count
    assert results[7] is True and died == [1]


def test_recovery_resets_the_counter():
    state = {"ok": False}
    w = watchdog.Watchdog(lambda: state["ok"], lambda: None, failures_to_restart=3)
    w.tick(); w.tick()
    assert w.failures == 2
    state["ok"] = True
    assert w.tick() is False and w.failures == 0


def test_probe_reports_a_dead_port_without_raising():
    assert watchdog.http_probe("http://127.0.0.1:1/api/health/live", timeout=1) is False


def test_disabled_under_pytest_and_by_env(monkeypatch):
    assert watchdog.enabled() is False           # PYTEST_CURRENT_TEST is set here
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    assert watchdog.enabled() is True
    monkeypatch.setenv("YORIK_WATCHDOG", "0")
    assert watchdog.enabled() is False


def test_port_follows_the_tenant_or_host_setting(monkeypatch):
    monkeypatch.delenv("HOMEOS_PORT", raising=False)
    monkeypatch.delenv("YORIK_PORT", raising=False)
    assert watchdog.bind_port() == 8000
    monkeypatch.setenv("YORIK_PORT", "8177")
    assert watchdog.bind_port() == 8177
    monkeypatch.setenv("HOMEOS_PORT", "8201")
    assert watchdog.bind_port() == 8201
    monkeypatch.setenv("HOMEOS_PORT", "x")
    assert watchdog.bind_port() == 8000


def test_live_route_answers_without_the_database(fresh_app):
    from fastapi.testclient import TestClient
    r = TestClient(fresh_app).get("/api/health/live")
    assert r.status_code == 200 and r.json()["ok"] is True
