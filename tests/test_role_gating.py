"""Role-gating tests — the SECURITY boundary.

These pin the invariant that a 'child' session can never read 'admin'-
only tables (bills) and only sees rows tagged for their role within
shared tables (events, tasks). One bad refactor of auth.py or
backend/main.py's per-endpoint require_role / apply_filter calls would
silently leak data — this test would fail loudly.

We exercise the REST surface end-to-end via TestClient. The SQL-agent
path (RoleGatedSqliteRunner) is gated by the same auth.filter_query_by_role
helper, so a direct unit test on that function covers the agent path
without needing to spin up the LLM.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


def _setup_admin(client: TestClient) -> dict:
    """Setup the admin user. Returns the user dict."""
    r = client.post("/api/auth/setup", json={
        "name": "Pytest Admin", "email": "pytest-admin@yorik.local", "password": "admin-pw-12345",
    })
    assert r.status_code == 200, r.text
    return r.json()


def _login(client: TestClient, email: str, password: str) -> None:
    """Login on the same client — session cookie sticks for subsequent
    requests. Asserts success."""
    # Clear any prior session cookie first so the test starts clean.
    client.cookies.clear()
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text


def _create_child(client: TestClient) -> dict:
    """Admin-only: create a child user. Returns the user dict."""
    r = client.post("/api/users", json={
        "name": "Kid", "email": "kid@yorik.local",
        "password": "kid-pw-12345", "role": "child",
    })
    assert r.status_code == 201, r.text
    return r.json()


# ─── REST-level role gating ────────────────────────────────────────────

def test_child_cannot_read_bills(fresh_app):
    """/api/bills is admin-only (per auth.ROLE_TABLES). A logged-in
    child must get 403, not 200, not 401."""
    client = TestClient(fresh_app)
    _setup_admin(client)
    _create_child(client)
    _login(client, "kid@yorik.local", "kid-pw-12345")

    r = client.get("/api/bills")
    assert r.status_code == 403, (
        f"ROLE LEAK: child got {r.status_code} from /api/bills (expected 403). "
        f"Body: {r.text}"
    )


def test_admin_sees_bills_child_does_not(fresh_app):
    """Same endpoint, different sessions: admin → 200, child → 403."""
    client = TestClient(fresh_app)
    _setup_admin(client)
    _create_child(client)

    # admin session is still active from setup
    r_admin = client.get("/api/bills")
    assert r_admin.status_code == 200, r_admin.text

    _login(client, "kid@yorik.local", "kid-pw-12345")
    r_child = client.get("/api/bills")
    assert r_child.status_code == 403


def test_child_events_subset_of_admin_events(fresh_app):
    """Both roles can read /api/events, but the child only sees rows
    whose allowed_roles includes 'child'. So child's count must be <=
    admin's, and every child-visible row must contain 'child' in
    allowed_roles. Pins the apply_filter() WHERE-clause invariant."""
    client = TestClient(fresh_app)
    _setup_admin(client)
    _create_child(client)

    r = client.get("/api/events")
    assert r.status_code == 200, r.text
    admin_events = r.json()

    _login(client, "kid@yorik.local", "kid-pw-12345")
    r = client.get("/api/events")
    assert r.status_code == 200, r.text
    child_events = r.json()

    assert isinstance(admin_events, list) and isinstance(child_events, list)
    assert len(child_events) <= len(admin_events), (
        "Child sees MORE events than admin — apply_filter() is broken or "
        "the role allowlist for events drifted."
    )
    for e in child_events:
        roles = (e.get("allowed_roles") or "").split(",")
        assert "child" in roles, (
            f"ROLE LEAK: child can see event id={e.get('id')} "
            f"title={e.get('title')!r} whose allowed_roles={e.get('allowed_roles')!r} "
            f"doesn't include 'child'."
        )


def test_child_cannot_create_users(fresh_app):
    """POST /api/users is admin-only (Depends(require_admin)). A child
    must get 403, not 201."""
    client = TestClient(fresh_app)
    _setup_admin(client)
    _create_child(client)
    _login(client, "kid@yorik.local", "kid-pw-12345")

    r = client.post("/api/users", json={
        "name": "Sneaky", "email": "sneaky@yorik.local",
        "password": "sneaky-pw-12345", "role": "admin",
    })
    assert r.status_code == 403, (
        f"PRIVILEGE ESCALATION: child created a user, got {r.status_code} "
        f"(expected 403). Body: {r.text}"
    )


# ─── Direct unit test on the SQL-agent guard ──────────────────────────

def test_filter_query_by_role_blocks_disallowed_tables(fresh_app):
    """The LLM's RunSqlTool wraps every SQL string through
    filter_query_by_role(). It must raise/refuse when the role lacks
    access to a referenced table — otherwise the chat agent could be
    coaxed into 'SELECT * FROM bills' as a child."""
    # fresh_app fixture sets the env vars so backend.* import correctly;
    # the import itself happens here.
    from backend.auth import filter_query_by_role

    # Child cannot reach bills
    with pytest.raises(PermissionError):
        filter_query_by_role("SELECT * FROM bills", "child")

    # Child CAN reach events (the SQL string just passes through;
    # row-level filtering is layered elsewhere)
    out = filter_query_by_role("SELECT * FROM events", "child")
    assert "events" in out.lower()


def test_admin_can_reach_every_role_gated_table(fresh_app):
    """Sanity: admin must not be locked out of any table by the guard."""
    from backend.auth import filter_query_by_role
    for table in ("events", "tasks", "bills", "documents", "user_profiles"):
        # Shouldn't raise
        out = filter_query_by_role(f"SELECT * FROM {table}", "admin")
        assert table in out.lower()


# ─── Phase 7.6: SELECT on skill-gated tables routes through check_* ──

def test_gated_read_table_detects_select_on_gated_tables(fresh_app):
    """_gated_read_table must return the gated table name for any
    plain SELECT on tasks/events/bills/documents, including when the
    SQL is multi-line, lowercase, has a leading comment, or backticked."""
    from backend.ask import _gated_read_table

    assert _gated_read_table("SELECT * FROM tasks") == "tasks"
    assert _gated_read_table("select id, title from events where done=0") == "events"
    assert _gated_read_table("SELECT * FROM `bills` WHERE provider LIKE '%X%'") == "bills"
    assert _gated_read_table(
        "-- preflight\nSELECT title, due_date\nFROM tasks\nWHERE due_date=?"
    ) == "tasks"
    assert _gated_read_table("SELECT * FROM documents LIMIT 5") == "documents"


def test_gated_read_table_passes_through_non_gated(fresh_app):
    """Writes, DDL, and SELECTs on ungated tables return None — the
    runner must not block these."""
    from backend.ask import _gated_read_table

    # Writes are handled by _mutating_table; not our job
    assert _gated_read_table("INSERT INTO tasks(title) VALUES('x')") is None
    assert _gated_read_table("UPDATE tasks SET done=1 WHERE id=1") is None
    # DDL is handled by _is_ddl
    assert _gated_read_table("CREATE TABLE x (id INT)") is None
    # SELECT on a non-gated table
    assert _gated_read_table("SELECT * FROM contacts") is None
    assert _gated_read_table("SELECT * FROM user_profiles") is None
    # SELECT on a table whose name starts with a gated one — exact match only
    assert _gated_read_table("SELECT * FROM events_archive") is None
