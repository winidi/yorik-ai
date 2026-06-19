"""Tidy the pending WhatsApp contact backlog.

Two passes on the contacts table, both safe to run repeatedly:

1. DELETE pending @lid contacts whose only identity is the LID itself.
   The runtime autocapture used to park EVERY unknown WhatsApp sender
   on the pending list, including @lid ones with no pushName. Those
   rows show up in /r/contacts as "222273835368470 — pending — Person",
   asking the user to triage with no human-legible info. The
   2026-06-05 fix in backend/contact_autocapture.py skips them going
   forward; this pass clears the backlog already on disk.

2. UPDATE pending @s.whatsapp.net contacts where wa_chats has a name
   the autocapture didn't pick up. Bridge syncs populate wa_chats.name
   from the contact's profile name; the inbound autocapture only used
   the message's pushName, which is often empty. Same fallback we just
   added to the runtime path, applied retroactively. Contacts that get
   a name also get promoted from pending → active to match the runtime
   "name on file = active" policy.

Both passes are conservative — only touch rows the user clearly hasn't
labelled (display_name is bare digits) and hasn't annotated (no notes).
"""

from __future__ import annotations


def up(conn) -> None:
    _purge_nameless_lid(conn)
    _backfill_names_from_wa_chats(conn)


def _purge_nameless_lid(conn) -> None:
    # Find candidates. The subquery counts channels per contact so the
    # "exactly one channel" filter is cheap.
    rows = conn.execute("""
        SELECT c.id
        FROM contacts c
        JOIN contact_channels ch ON ch.contact_id = c.id
        WHERE c.status = 'pending'
          AND c.kind   = 'person'
          AND ch.kind  = 'whatsapp'
          AND ch.value LIKE '%@lid'
          AND c.display_name GLOB '[0-9]*'
          AND NOT EXISTS (
              SELECT 1 FROM contact_channels ch2
              WHERE ch2.contact_id = c.id AND ch2.id != ch.id
          )
          AND (c.notes IS NULL OR TRIM(c.notes) = '')
    """).fetchall()

    if not rows:
        return

    ids = [int(r["id"]) for r in rows]
    CHUNK = 500  # SQLite parameter-count guard rail
    for i in range(0, len(ids), CHUNK):
        batch = ids[i:i + CHUNK]
        placeholders = ",".join("?" * len(batch))
        conn.execute(
            f"DELETE FROM contacts WHERE id IN ({placeholders})",
            batch,
        )


def _backfill_names_from_wa_chats(conn) -> None:
    # Skip silently when wa_chats hasn't been created yet (e.g. a
    # fresh install where the WhatsApp connector never ran).
    try:
        conn.execute("SELECT 1 FROM wa_chats LIMIT 1")
    except Exception:
        return

    # Pending @s.whatsapp.net contacts whose display_name is the bare
    # phone number AND wa_chats has a proper name for them. JOIN on
    # the WhatsApp channel value so we link contact ↔ chat by JID.
    rows = conn.execute("""
        SELECT c.id           AS contact_id,
               ch.value       AS jid,
               wc.name        AS wa_name
        FROM contacts c
        JOIN contact_channels ch ON ch.contact_id = c.id
        JOIN wa_chats wc        ON wc.jid = ch.value
        WHERE c.status = 'pending'
          AND c.kind   = 'person'
          AND ch.kind  = 'whatsapp'
          AND ch.value LIKE '%@s.whatsapp.net'
          AND c.display_name GLOB '[0-9]*'
          AND (c.notes IS NULL OR TRIM(c.notes) = '')
          AND wc.name IS NOT NULL
          AND TRIM(wc.name) != ''
          AND TRIM(wc.name) NOT GLOB '[0-9]*'
    """).fetchall()

    for r in rows:
        conn.execute(
            "UPDATE contacts SET display_name = ?, status = 'active' "
            "WHERE id = ?",
            ((r["wa_name"] or "").strip(), int(r["contact_id"])),
        )
