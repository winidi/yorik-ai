"""036 — Phase B step 0: workspaces, spaces, space_members, row_shares.

Lays the data layer for the spaces-based ACL model (decision recorded in
project_yorik_phase_b_acl_scope memory, 2026-06-02). This migration ONLY
adds schema + seeds defaults + backfills space_id on existing rows. No
code reads from the new tables yet — that's B.1 (read paths) and B.2
(write paths) in later commits.

What this migration creates
---------------------------
- workspaces        : one row per install (kind = 'family' default).
- spaces            : ACL containers. One personal space per existing
                      user + one shared 'household' + one shared 'finance'.
- space_members     : per-user level in a space (read|write|admin).
                      Personal-space owner is implicit admin (no row).
- row_shares        : polymorphic per-row share. Generalises
                      contact_shares (which lives on until B.2 land).
- <domain>.space_id : new nullable column on calendars / tasks / contacts
                      / bills / agent_conversations. Backfilled here.

Backfill rules
--------------
- calendars: personal → owner's personal space; shared → household.
- tasks: created_by_user_id IS NULL → household; else creator's personal.
- contacts: allowed_roles contains 'member' → household; else owner's
            personal (created_by_user_id → personal; NULL → admin's personal).
- bills: → finance.
- agent_conversations: user_id → user's personal.

Restricted role migration: any user_profiles.role in {child, employee,
viewer} is rewritten to 'restricted'. Logged via raise(NOTICE)-style
comment-row; the rewrite is data, no schema change.

Idempotency
-----------
Each step is gated by "does this artifact already exist?" so a partial
apply followed by a retry doesn't double-insert. The schema_migrations
table itself blocks re-run in practice, but the gates are belt-and-
braces for hand-run hot fixes.
"""
from __future__ import annotations

import sqlite3
from typing import Optional


def up(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    # SQLite row factory — read by [col] AND [idx]
    cur.row_factory = sqlite3.Row

    # ─── New tables ─────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS workspaces (
            id            INTEGER PRIMARY KEY,
            name          TEXT NOT NULL,
            kind          TEXT NOT NULL CHECK (kind IN ('family', 'business')),
            owner_user_id INTEGER NOT NULL REFERENCES user_profiles(id),
            created_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS spaces (
            id             INTEGER PRIMARY KEY,
            workspace_id   INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            name           TEXT NOT NULL,
            kind           TEXT NOT NULL CHECK (kind IN ('personal', 'shared')),
            owner_user_id  INTEGER REFERENCES user_profiles(id),
            slug           TEXT,
            color          TEXT,
            icon           TEXT,
            created_at     TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (workspace_id, slug)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS space_members (
            space_id          INTEGER NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
            user_id           INTEGER NOT NULL REFERENCES user_profiles(id) ON DELETE CASCADE,
            level             TEXT NOT NULL CHECK (level IN ('read', 'write', 'admin')),
            added_at          TEXT NOT NULL DEFAULT (datetime('now')),
            added_by_user_id  INTEGER REFERENCES user_profiles(id),
            PRIMARY KEY (space_id, user_id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS row_shares (
            id                INTEGER PRIMARY KEY,
            table_name        TEXT NOT NULL,
            row_id            INTEGER NOT NULL,
            user_id           INTEGER NOT NULL REFERENCES user_profiles(id) ON DELETE CASCADE,
            level             TEXT NOT NULL CHECK (level IN ('read', 'write')),
            shared_by_user_id INTEGER REFERENCES user_profiles(id),
            shared_at         TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (table_name, row_id, user_id)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_row_shares_row ON row_shares (table_name, row_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_row_shares_user ON row_shares (user_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_space_members_user ON space_members (user_id)")

    # ─── space_id columns on domain tables (nullable, no FK now) ────
    # NOT NULL would block the migration since existing rows have no
    # space yet; we backfill below and a follow-up migration can tighten
    # the constraint once Phase B.2 has flipped writes.
    for table in ("calendars", "tasks", "contacts", "bills", "agent_conversations"):
        if not _column_exists(cur, table, "space_id"):
            cur.execute(f"ALTER TABLE {table} ADD COLUMN space_id INTEGER REFERENCES spaces(id)")

    # ─── Seed workspace ─────────────────────────────────────────────
    # One workspace per install. Default kind=family per locked decision
    # (user can switch in Settings). Owner = first admin we find, falling
    # back to user_id=1.
    ws_row = cur.execute("SELECT id FROM workspaces LIMIT 1").fetchone()
    if ws_row is None:
        admin = cur.execute(
            "SELECT id, name FROM user_profiles WHERE role='admin' ORDER BY id LIMIT 1"
        ).fetchone()
        if admin is None:
            admin = cur.execute(
                "SELECT id, name FROM user_profiles ORDER BY id LIMIT 1"
            ).fetchone()
        if admin is None:
            # No users exist (fresh install). Skip the rest — the user
            # provisioning code in auth.create_user will run this seed
            # logic on first user creation post-migration.
            return
        cur.execute(
            "INSERT INTO workspaces (id, name, kind, owner_user_id) VALUES (1, ?, 'family', ?)",
            (f"{admin['name']}'s household", admin["id"]),
        )
        ws_id = 1
    else:
        ws_id = ws_row["id"]

    # ─── Seed spaces ────────────────────────────────────────────────
    # Personal space per existing user (kind=personal, owner=that user,
    # slug=NULL since slug is reserved for well-known shared spaces).
    users = cur.execute("SELECT id, name FROM user_profiles ORDER BY id").fetchall()
    personal_by_user: dict[int, int] = {}
    for u in users:
        existing = cur.execute(
            "SELECT id FROM spaces WHERE workspace_id=? AND kind='personal' AND owner_user_id=?",
            (ws_id, u["id"]),
        ).fetchone()
        if existing:
            personal_by_user[u["id"]] = existing["id"]
            continue
        cur.execute(
            "INSERT INTO spaces (workspace_id, name, kind, owner_user_id) VALUES (?, ?, 'personal', ?)",
            (ws_id, f"{u['name']}'s space", u["id"]),
        )
        personal_by_user[u["id"]] = int(cur.lastrowid)

    # Shared (slug 'household'): everyone is a member with write level.
    household_id = _ensure_shared_space(cur, ws_id, "household", "Shared")
    for u in users:
        cur.execute(
            "INSERT OR IGNORE INTO space_members (space_id, user_id, level) VALUES (?, ?, 'write')",
            (household_id, u["id"]),
        )

    # Finance: admins only, admin level. Members can be added later.
    finance_id = _ensure_shared_space(cur, ws_id, "finance", "Finance")
    for u in users:
        role = cur.execute("SELECT role FROM user_profiles WHERE id=?", (u["id"],)).fetchone()
        if role and (role["role"] or "").lower() == "admin":
            cur.execute(
                "INSERT OR IGNORE INTO space_members (space_id, user_id, level) VALUES (?, ?, 'admin')",
                (finance_id, u["id"]),
            )

    # admin_personal — referenced by the contacts + tasks fallbacks
    # below. Resolved once and reused for both.
    admin_row = cur.execute(
        "SELECT id FROM user_profiles WHERE role='admin' ORDER BY id LIMIT 1"
    ).fetchone()
    admin_personal = personal_by_user.get(admin_row["id"]) if admin_row else None

    # ─── Backfill space_id on existing rows ─────────────────────────
    # Calendars: personal → owner's personal space; shared (== Household
    # in seed-speak) → household.
    cur.execute("""
        UPDATE calendars
        SET space_id = (
            SELECT id FROM spaces
            WHERE workspace_id=? AND kind='personal' AND owner_user_id=calendars.owner_user_id
        )
        WHERE kind='personal' AND space_id IS NULL
    """, (ws_id,))
    cur.execute(
        "UPDATE calendars SET space_id=? WHERE kind='shared' AND space_id IS NULL",
        (household_id,),
    )

    # Tasks. Heuristics for placing legacy rows in a sensible space:
    #   - created_by_user_id IS NOT NULL → creator's personal space.
    #   - created_by IS NULL AND allowed_roles includes 'member' → household
    #     (the seed encoded "everyone can see this" via that allowlist).
    #   - created_by IS NULL AND allowed_roles is admin-only → admin's
    #     personal space. Otherwise an admin-only chore (e.g. "File the
    #     quote with Mustermann GmbH") would leak into the Household view
    #     once allowed_roles is dropped in B.2.
    cur.execute("""
        UPDATE tasks
        SET space_id = (
            SELECT id FROM spaces
            WHERE workspace_id=? AND kind='personal' AND owner_user_id=tasks.created_by_user_id
        )
        WHERE created_by_user_id IS NOT NULL AND space_id IS NULL
    """, (ws_id,))
    cur.execute(
        "UPDATE tasks SET space_id=? "
        "WHERE space_id IS NULL "
        "  AND created_by_user_id IS NULL "
        "  AND INSTR(',' || IFNULL(allowed_roles,'') || ',', ',member,') > 0",
        (household_id,),
    )
    # Fallback for any remaining (typically allowed_roles='admin' only) →
    # admin's personal so they stay private post-B.2.
    cur.execute(
        "UPDATE tasks SET space_id=? WHERE space_id IS NULL",
        (admin_personal,),
    )

    # Contacts: allowed_roles containing 'member' → household; else
    # creator's personal (or admin's personal as fallback for legacy
    # rows with no creator).
    cur.execute(
        "UPDATE contacts SET space_id=? "
        "WHERE space_id IS NULL "
        "  AND INSTR(',' || IFNULL(allowed_roles,'') || ',', ',member,') > 0",
        (household_id,),
    )
    cur.execute("""
        UPDATE contacts
        SET space_id = (
            SELECT id FROM spaces
            WHERE workspace_id=? AND kind='personal' AND owner_user_id=contacts.created_by_user_id
        )
        WHERE space_id IS NULL AND created_by_user_id IS NOT NULL
    """, (ws_id,))
    cur.execute(
        "UPDATE contacts SET space_id=? WHERE space_id IS NULL",
        (admin_personal,),
    )

    # Bills: → finance.
    cur.execute(
        "UPDATE bills SET space_id=? WHERE space_id IS NULL",
        (finance_id,),
    )

    # Agent conversations: user_id → user's personal space. Rows whose
    # user_id is no longer present in user_profiles (orphans) inherit
    # admin's personal so they remain accessible.
    cur.execute("""
        UPDATE agent_conversations
        SET space_id = (
            SELECT id FROM spaces
            WHERE workspace_id=? AND kind='personal' AND owner_user_id=agent_conversations.user_id
        )
        WHERE space_id IS NULL AND user_id IS NOT NULL
    """, (ws_id,))
    cur.execute(
        "UPDATE agent_conversations SET space_id=? WHERE space_id IS NULL",
        (admin_personal,),
    )

    # ─── Role consolidation ──────────────────────────────────────────
    # child / employee / viewer all collapse to 'restricted' per Phase B
    # decisions. Auto-migrate per user; logging is left to the caller.
    cur.execute(
        "UPDATE user_profiles SET role='restricted' "
        "WHERE LOWER(role) IN ('child','employee','viewer')"
    )


def _column_exists(cur: sqlite3.Cursor, table: str, column: str) -> bool:
    rows = cur.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)


def _ensure_shared_space(
    cur: sqlite3.Cursor, workspace_id: int, slug: str, name: str,
) -> int:
    existing = cur.execute(
        "SELECT id FROM spaces WHERE workspace_id=? AND slug=?",
        (workspace_id, slug),
    ).fetchone()
    if existing:
        return existing["id"]
    cur.execute(
        "INSERT INTO spaces (workspace_id, name, kind, slug) VALUES (?, ?, 'shared', ?)",
        (workspace_id, name, slug),
    )
    return int(cur.lastrowid)
