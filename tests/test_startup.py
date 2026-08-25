"""Startup contract: the app comes up against a reachable database and
refuses — with one sentence — when it is not.

`TestClient(app)` without `with` never fires startup events, so the
rest of the suite never exercised `_startup()`. These do.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


def test_startup_runs_and_health_names_the_database(fresh_app):
    with TestClient(fresh_app) as client:
        r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "database" in body and "/" in body["database"]
    assert "db_path" not in body


def test_startup_refuses_when_database_unreachable(fresh_app, monkeypatch):
    import backend.main as main_mod
    monkeypatch.setattr(main_mod._database, "check_connection",
                        lambda timeout_s=3.0: (False, "127.0.0.1:1/nowhere: OperationalError: refused"))
    with pytest.raises(BaseException) as exc:
        with TestClient(fresh_app):
            pass
    e = exc.value
    if isinstance(e, BaseExceptionGroup):
        assert exc.group_contains(RuntimeError, match="Postgres not reachable")
    else:
        assert isinstance(e, RuntimeError) and "Postgres not reachable" in str(e)
