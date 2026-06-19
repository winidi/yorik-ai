"""029 — change wa_messages PK to include owner_user_id.

Why: with the multi-tenant WhatsApp bridge each user has their own
session. If two family members are in the same WhatsApp group, both
of their Baileys sessions receive every message and try to write
identical (chat_jid, msg_id) rows — which collides on the current
PRIMARY KEY (chat_jid, msg_id).

Fix: extend the PK to (chat_jid, msg_id, owner_user_id). Each user
gets their own row for shared messages. Per-user mute/archive/etc.
becomes natural in future. Tiny storage cost.

What this migration does:
  1. Backfills owner_user_id on wa_chats / wa_messages / wa_drafts
     to the actual admin user id (no-op if admin is user id 1, which
     is the default in fresh installs).
  2. Rebuilds wa_messages with the new PK while preserving rowid so
     the wa_messages_fts content-rowid mapping stays valid.
  3. Recreates the three FTS triggers on the rebuilt table.
  4. Forces a full FTS rebuild so the search index is guaranteed
     consistent with the new table.

The FTS table itself (wa_messages_fts) is NOT dropped — its content
references the renamed wa_messages, and the explicit rebuild at the
end re-syncs it. All existing message history is preserved.
"""

from __future__ import annotations

import logging
import sqlite3

log = logging.getLogger("yorik.migrations.029")


def up(conn: sqlite3.Connection) -> None:
    # ── 1. Discover the actual admin user id (defensive — most installs
    #      have admin = user id 1, but we don't hard-code). The user
    #      table is called user_profiles in Yorik. On a fresh-bootstrap
    #      install the table may not have any admin row yet (the seed
    #      runs after init_db); in that case we fall back to 1.
    admin_id = 1
    try:
        row = conn.execute(
            "SELECT id FROM user_profiles WHERE role = 'admin' ORDER BY id ASC LIMIT 1"
        ).fetchone()
        if row:
            admin_id = int(row[0])
    except Exception as e:
        log.info("029: user_profiles not populated yet (%s); defaulting admin=1", e)
    log.info("029: admin user id = %d", admin_id)

    # ── 2. Backfill owner_user_id if the default of 1 doesn't match
    #      the real admin id. No-op in the common case.
    if admin_id != 1:
        for table in ("wa_chats", "wa_messages", "wa_drafts"):
            cur = conn.execute(
                f"UPDATE {table} SET owner_user_id = ? WHERE owner_user_id = 1",
                (admin_id,),
            )
            log.info("029: backfilled %s.owner_user_id rows=%d", table, cur.rowcount)

    # ── 3. Drop FTS triggers — they reference wa_messages by name and
    #      we're about to swap that table out. We'll recreate them after.
    conn.executescript("""
        DROP TRIGGER IF EXISTS wa_messages_ai;
        DROP TRIGGER IF EXISTS wa_messages_au;
        DROP TRIGGER IF EXISTS wa_messages_ad;
    """)

    # ── 4. Build the new table with the extended PK.
    conn.executescript("""
        CREATE TABLE wa_messages_new (
            msg_id             TEXT NOT NULL,
            chat_jid           TEXT NOT NULL,
            from_me            INTEGER NOT NULL DEFAULT 0,
            participant        TEXT,
            push_name          TEXT,
            timestamp          INTEGER NOT NULL,
            text               TEXT,
            media_kind         TEXT,
            mimetype           TEXT,
            filename           TEXT,
            media_local_path   TEXT,
            media_paperless_id INTEGER,
            media_immich_id    TEXT,
            transcript         TEXT,
            owner_user_id      INTEGER NOT NULL DEFAULT 1,
            created_at         TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (chat_jid, msg_id, owner_user_id),
            FOREIGN KEY (chat_jid) REFERENCES wa_chats(jid) ON DELETE CASCADE
        );
    """)

    # ── 5. Copy data. Preserve rowid explicitly so the wa_messages_fts
    #      content-rowid pointer stays aligned. If we let SQLite assign
    #      new rowids, the FTS table would silently dereference the
    #      wrong rows.
    conn.execute("""
        INSERT INTO wa_messages_new
            (rowid, msg_id, chat_jid, from_me, participant, push_name,
             timestamp, text, media_kind, mimetype, filename,
             media_local_path, media_paperless_id, media_immich_id,
             transcript, owner_user_id, created_at)
        SELECT
             rowid, msg_id, chat_jid, from_me, participant, push_name,
             timestamp, text, media_kind, mimetype, filename,
             media_local_path, media_paperless_id, media_immich_id,
             transcript, owner_user_id, created_at
        FROM wa_messages
    """)
    moved = conn.execute("SELECT COUNT(*) FROM wa_messages_new").fetchone()[0]
    log.info("029: copied %d rows into wa_messages_new", moved)

    # ── 6. Swap the tables.
    conn.executescript("""
        DROP TABLE wa_messages;
        ALTER TABLE wa_messages_new RENAME TO wa_messages;
        CREATE INDEX IF NOT EXISTS ix_wa_messages_chat_ts
            ON wa_messages(chat_jid, timestamp DESC);
        CREATE INDEX IF NOT EXISTS ix_wa_messages_ts
            ON wa_messages(timestamp DESC);
    """)

    # ── 7. Recreate the FTS triggers against the rebuilt table.
    #      Identical to the originals in database.py.
    conn.executescript("""
        CREATE TRIGGER wa_messages_ai AFTER INSERT ON wa_messages BEGIN
            INSERT INTO wa_messages_fts(rowid, text, transcript)
                VALUES (new.rowid, COALESCE(new.text, ''), COALESCE(new.transcript, ''));
        END;
        CREATE TRIGGER wa_messages_au AFTER UPDATE ON wa_messages BEGIN
            INSERT INTO wa_messages_fts(wa_messages_fts, rowid, text, transcript)
                VALUES('delete', old.rowid, COALESCE(old.text, ''), COALESCE(old.transcript, ''));
            INSERT INTO wa_messages_fts(rowid, text, transcript)
                VALUES (new.rowid, COALESCE(new.text, ''), COALESCE(new.transcript, ''));
        END;
        CREATE TRIGGER wa_messages_ad AFTER DELETE ON wa_messages BEGIN
            INSERT INTO wa_messages_fts(wa_messages_fts, rowid, text, transcript)
                VALUES('delete', old.rowid, COALESCE(old.text, ''), COALESCE(old.transcript, ''));
        END;
    """)

    # ── 8. Force-rebuild the FTS index. Even though we preserved rowids,
    #      FTS5 doesn't track table-replace operations and the internal
    #      doc-mapping may have drifted during the swap. The 'rebuild'
    #      command is FTS5's own re-sync from content. Idempotent and
    #      fast at household scale.
    conn.execute("INSERT INTO wa_messages_fts(wa_messages_fts) VALUES('rebuild')")
    log.info("029: rebuilt wa_messages_fts from wa_messages content")
