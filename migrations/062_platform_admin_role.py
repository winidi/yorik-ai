"""Phase C step T10 — introduce platform_admin role + demote workspace admins.

Before: `admin` role globally bypasses space filtering — sees every
calendar / contact / row in every workspace. That works in single-
family installs but breaks multi-family hosting (every workspace
admin would see every other workspace's data).

After:
  - `platform_admin` is the new global role for the infrastructure
    operator. Sees everything. There is exactly ONE platform admin
    per install in normal operation.
  - `admin` becomes the workspace-admin role: full access within
    workspaces this user owns (workspaces.owner_user_id = user.id),
    member-level access elsewhere via space_members.
  - Existing single-family installs preserved: the first
    non-disabled admin (typically user_id=1, the founder) is promoted
    to platform_admin so their behaviour is unchanged.

The role transition is in three coordinated pieces; this migration
is just the data + recognition flag. The visibility logic update
ships in the same commit but lives in backend/auth.py, backend/
spaces.py and backend/calendars.py — Yorik will pick them up on
restart.

Idempotent: safe to re-run.
"""
from __future__ import annotations

import sqlite3


def up(conn: sqlite3.Connection) -> None:
    # Idempotent on data state: if a platform_admin already exists,
    # we're done. Without this guard the migration would re-promote
    # the next-lowest admin on every replay (which exactly happened
    # in dev when the manual + on-startup invocations collided).
    existing = conn.execute(
        "SELECT 1 FROM user_profiles "
        "WHERE role = 'platform_admin' AND disabled = 0 LIMIT 1"
    ).fetchone()
    if existing is not None:
        return  # Founder already promoted; nothing to do

    # The first (lowest-id) non-disabled admin becomes platform_admin.
    # On a brand-new install the founder is user 1; this captures
    # whichever user holds the "I provisioned the box" role today.
    row = conn.execute(
        "SELECT id FROM user_profiles "
        "WHERE role = 'admin' AND disabled = 0 "
        "ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        return  # Nobody to promote — fresh install before first user
    founder_id = row[0]
    conn.execute(
        "UPDATE user_profiles SET role = 'platform_admin' WHERE id = ?",
        (founder_id,),
    )
