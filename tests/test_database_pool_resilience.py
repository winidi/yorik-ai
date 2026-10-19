"""Pooled connections must notice a vanished Postgres and requests must
fail instead of waiting forever (F27, 2026-10-19)."""

from __future__ import annotations

import pytest

from backend import database_pg as db


def test_connections_use_keepalives_and_a_connect_timeout():
    p = db.connect_params("public,docs")
    assert p["options"] == "-c search_path=public,docs"
    assert p["connect_timeout"] <= 10
    assert p["keepalives"] == 1
    # dead peer detected within about a minute
    assert p["keepalives_idle"] + p["keepalives_interval"] * p["keepalives_count"] <= 90


def test_pool_checks_connections_and_bounds_the_wait():
    o = db.pool_options()
    assert o["check"] is db.check_connection          # bounded, not the library's unbounded one
    assert 0 < o["timeout"] <= 30
    assert 0 < o["reconnect_timeout"] <= 120
    assert callable(o["reconnect_failed"])


def test_a_live_checkout_still_works(fresh_app):
    from backend import database_pg as db  # the module fresh_app re-imported
    with db.conn_ctx_pg("main") as conn:
        assert conn.execute("select 1").fetchone()[0] == 1


def test_bounded_check_passes_on_a_live_connection(fresh_app):
    from backend import database_pg as db  # the module fresh_app re-imported
    with db.conn_ctx_pg("main") as conn:
        db.check_connection(conn, timeout=2.0)
        assert conn.execute("select 2").fetchone()[0] == 2


def test_connecting_to_a_silent_server_fails_within_the_timeout():
    """libpq connects at TCP level and then waits for the startup answer;
    with connect_timeout a frozen Postgres fails the connect instead of
    holding the worker (the check itself is covered by the docker-pause
    run noted in fixes.md F27)."""
    import socket
    import threading
    import time

    import psycopg

    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
    port = srv.getsockname()[1]
    accepted = []
    threading.Thread(target=lambda: accepted.append(srv.accept()), daemon=True).start()
    t0 = time.monotonic()
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(host="127.0.0.1", port=port, user="x", password="x", dbname="x",
                        **{k: v for k, v in db.connect_params("public").items() if k != "options"})
    assert time.monotonic() - t0 < 15
    srv.close()
