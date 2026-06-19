#!/usr/bin/env python3
"""Phase E §1 — seed the 5 reference test users + their workspaces / spaces.

Creates users via GoTrue's admin API (POST /auth/v1/admin/users) so the
passwords get the correct bcrypt hash and `auth.users.id` UUIDs come
from the same source the user-facing login will use. Then inserts the
matching `user_profiles` rows (id = auth.users.id), the Phase C
workspaces / spaces / space_members fixture, and the WS2 secret
sentinel rows the isolation suite asserts against.

Idempotent: safe to re-run. Existing auth.users entries are skipped
(POST returns 422 on duplicate email); user_profiles uses
ON CONFLICT DO NOTHING.

Run after applying migrations_pg/100_phase_e_init.sql to the postgres
database.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

import requests

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))


# ─── Supabase + auth config ────────────────────────────────────────


def _read_env_file() -> dict:
    env: dict[str, str] = {}
    for line in (PROJECT / "infra/supabase/docker/.env").read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


_env = _read_env_file()
KONG = "http://localhost:8400"
SERVICE_ROLE_KEY = _env["SERVICE_ROLE_KEY"]
ANON_KEY = _env["ANON_KEY"]
POSTGRES_PASSWORD = _env["POSTGRES_PASSWORD"]


# ─── Test users ────────────────────────────────────────────────────


USERS = [
    # (email, password, name, yorik_role, is_workspace_owner_of)
    ("dirk@winiecki.ai",      "test1234", "Dirk Winiecki", "platform_admin", "Dirk Winiecki's household"),
    ("beatemayer1@gmx.net",   "test1234", "Beate",         "member",         None),
    ("ws2_admin@test.local",  "test1234", "Mom (test)",    "admin",          "Test Household B"),
    ("ws2_member@test.local", "test1234", "Lily (test)",   "member",         None),
    ("jane@ws3.test",         "test1234", "Jane Smith",    "admin",          "Smith Family"),
]


def _admin_post_user(email: str, password: str, name: str, role: str) -> Optional[str]:
    """Create a Supabase Auth user via the GoTrue admin API. Returns
    the new user's UUID, or None when the email already exists."""
    headers = {
        "apikey": SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "email": email,
        "password": password,
        "email_confirm": True,
        "user_metadata": {"name": name, "yorik_role": role},
    }
    r = requests.post(f"{KONG}/auth/v1/admin/users", json=body, headers=headers, timeout=10)
    if r.status_code in (200, 201):
        return r.json()["id"]
    if r.status_code in (422, 409, 400):
        # Already exists — fetch the id
        r2 = requests.get(
            f"{KONG}/auth/v1/admin/users",
            params={"email": email},
            headers=headers, timeout=10,
        )
        r2.raise_for_status()
        users = (r2.json() or {}).get("users") or []
        if users:
            return users[0]["id"]
    raise RuntimeError(f"create user {email!r} failed: HTTP {r.status_code} — {r.text[:300]}")


def _pg_connect():
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(
        f"postgresql://postgres:{POSTGRES_PASSWORD}@127.0.0.1:5435/postgres",
        row_factory=dict_row,
    )


def main() -> int:
    print("==> Creating Supabase Auth users")
    auth_ids: dict[str, str] = {}
    for email, password, name, role, _ws in USERS:
        uid = _admin_post_user(email, password, name, role)
        auth_ids[email] = uid
        print(f"  {email:32}  → {uid}")

    print("\n==> Inserting user_profiles rows (FK to auth.users)")
    with _pg_connect() as conn:
        for email, _password, name, role, _ws in USERS:
            uid = auth_ids[email]
            conn.execute(
                "INSERT INTO user_profiles (id, name, email, role, password_hash) "
                "VALUES (%s, %s, %s, %s, 'supabase_auth') "
                "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, role = EXCLUDED.role",
                (uid, name, email, role),
            )
            print(f"  user_profiles.id = {uid}  ({role})")
        conn.commit()

        # ─── Workspaces + spaces (mirrors the Phase C fixture) ────
        print("\n==> Workspaces + spaces + memberships")
        dirk = auth_ids["dirk@winiecki.ai"]
        beate = auth_ids["beatemayer1@gmx.net"]
        mom = auth_ids["ws2_admin@test.local"]
        lily = auth_ids["ws2_member@test.local"]
        jane = auth_ids["jane@ws3.test"]

        def workspace(name: str, owner: str) -> int:
            row = conn.execute(
                "INSERT INTO workspaces (name, kind, owner_user_id) "
                "VALUES (%s, 'family', %s) RETURNING id",
                (name, owner),
            ).fetchone()
            return int(row["id"])

        def space(workspace_id: int, name: str, kind: str, slug: str | None, owner: str | None) -> int:
            row = conn.execute(
                "INSERT INTO spaces (workspace_id, name, kind, slug, owner_user_id) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (workspace_id, name, kind, slug, owner),
            ).fetchone()
            return int(row["id"])

        def add_member(space_id: int, user_id: str, level: str) -> None:
            conn.execute(
                "INSERT INTO space_members (space_id, user_id, level) VALUES (%s, %s, %s) "
                "ON CONFLICT DO NOTHING",
                (space_id, user_id, level),
            )

        ws1 = workspace("Dirk Winiecki's household", dirk)
        ws2 = workspace("Test Household B", mom)
        ws3 = workspace("Smith Family", jane)
        print(f"  workspaces: WS1={ws1} (Dirk's), WS2={ws2} (Mom's), WS3={ws3} (Jane's)")

        ws1_shared    = space(ws1, "Shared",  "shared",   "household", None)
        ws1_finance   = space(ws1, "Finance", "shared",   "finance",   None)
        ws1_personal_dirk  = space(ws1, "Dirk Winiecki's space", "personal", None, dirk)
        ws1_personal_beate = space(ws1, "Beate's space",         "personal", None, beate)
        ws2_shared    = space(ws2, "Shared",  "shared",   None,        None)
        ws2_personal_mom  = space(ws2, "Mom (test)'s space",  "personal", None, mom)
        ws2_personal_lily = space(ws2, "Lily (test)'s space", "personal", None, lily)
        ws3_shared    = space(ws3, "Shared",  "shared",   "smith-family", jane)
        ws3_personal_jane = space(ws3, "Jane Smith's space",  "personal", None, jane)

        add_member(ws1_shared, beate, "write")
        add_member(ws2_shared, lily,  "write")
        add_member(ws3_shared, jane,  "admin")
        print(f"  spaces: 9 created")
        print(f"  memberships: Beate→WS1 shared, Lily→WS2 shared, Jane→WS3 shared")

        # ─── WS2 isolation sentinels (so iso suite asserts pass) ──
        print("\n==> WS2 isolation sentinels (the strings the iso suite greps for)")
        conn.execute(
            "INSERT INTO contacts (display_name, kind, status, space_id) "
            "VALUES (%s, 'person', 'active', %s)",
            ("WS2_Secret_Contact", ws2_shared),
        )
        # WS2 calendar + event with the SECRET PARTY title
        cal_row = conn.execute(
            "INSERT INTO calendars (name, owner_user_id, space_id) "
            "VALUES (%s, %s, %s) RETURNING id",
            ("Mom's Family", mom, ws2_shared),
        ).fetchone()
        ws2_cal_id = int(cal_row["id"])
        conn.execute(
            "INSERT INTO events (title, starts_at, ends_at, owner_user_id, calendar_id, space_id) "
            "VALUES (%s, '2026-12-31 20:00:00', '2026-12-31 23:00:00', %s, %s, %s)",
            ("WS2 SECRET PARTY", mom, ws2_cal_id, ws2_shared),
        )
        conn.execute(
            "INSERT INTO tasks (title, created_by_user_id, space_id) "
            "VALUES (%s, %s, %s)",
            ("WS2 SECRET TASK", mom, ws2_shared),
        )
        # WS1 sentinel — Dirk's 'Doctor appointment' the iso suite expects
        ws1_cal_row = conn.execute(
            "INSERT INTO calendars (name, owner_user_id, space_id) "
            "VALUES (%s, %s, %s) RETURNING id",
            ("Dirk's Family", dirk, ws1_shared),
        ).fetchone()
        ws1_cal_id = int(ws1_cal_row["id"])
        conn.execute(
            "INSERT INTO events (title, starts_at, ends_at, owner_user_id, calendar_id, space_id) "
            "VALUES ('Doctor appointment', '2026-07-10 09:00:00', '2026-07-10 10:00:00', %s, %s, %s)",
            (dirk, ws1_cal_id, ws1_shared),
        )

        conn.commit()
        print("  ✓ WS2_Secret_Contact, WS2 SECRET PARTY, WS2 SECRET TASK inserted")
        print("  ✓ Dirk's Doctor appointment inserted (WS1 sentinel)")

    print("\n==> Done. Counts:")
    with _pg_connect() as conn:
        for t in ("user_profiles", "workspaces", "spaces", "space_members",
                  "contacts", "calendars", "events", "tasks"):
            n = conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
            print(f"  {t:20}  {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
