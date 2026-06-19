"""IMAP write-side operations: mark seen/unseen, star, move, delete,
and fetch attachment binaries on demand.

Each action opens a short-lived IMAPClient connection per call. That's
~300-500ms of latency per user action, which is fine for click-driven
operations (Gmail does similar — clicks aren't free). The alternative
of reusing the IDLE connection is brittle (IDLE blocks the socket;
unblocking + re-IDLE is racy).

The local SQLite is updated *first* (optimistic), then the IMAP server
gets the change. If the server call fails, we roll back the local
update and surface the error. That ordering means the UI feels
instant on the happy path and only stutters on actual network errors.
"""

from __future__ import annotations

import logging
import ssl
from contextlib import contextmanager
from typing import Optional

from imapclient import IMAPClient

from . import credential_store
from .database import get_conn

log = logging.getLogger("yorik.email.actions")


@contextmanager
def imap_for_account(account_id: int):
    """Short-lived IMAP connection for one account. Caller-managed
    select_folder + action; client is closed on exit."""
    with get_conn() as conn:
        cfg = conn.execute(
            "SELECT id, imap_host, imap_port, imap_ssl, imap_starttls, "
            "       imap_username, credential_key "
            "FROM email_accounts WHERE id=?",
            (account_id,),
        ).fetchone()
    if not cfg:
        raise RuntimeError(f"account {account_id} not found")
    creds = credential_store.get(cfg["credential_key"]) or {}
    pw = creds.get("imap_password") if isinstance(creds, dict) else creds
    if not pw:
        raise RuntimeError("no IMAP password in credential store")
    ssl_on = bool(cfg["imap_ssl"])
    use_starttls = bool(cfg["imap_starttls"]) and not ssl_on
    from .email_ssl import make_ssl_context
    ssl_ctx = make_ssl_context(cfg["imap_host"]) if (ssl_on or use_starttls) else None
    with IMAPClient(host=cfg["imap_host"], port=cfg["imap_port"],
                     ssl=ssl_on, ssl_context=(ssl_ctx if ssl_on else None),
                     timeout=20) as c:
        if use_starttls:
            c.starttls(ssl_context=ssl_ctx)
        c.login(cfg["imap_username"], pw)
        yield c


def _msg_lookup(message_id: int, user_id: str) -> Optional[dict]:
    """Resolve a message_id (our DB) to its account + folder + IMAP UID,
    with ownership check baked in."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT m.id, m.account_id, m.folder_id, m.uid, m.is_unread, m.is_starred, "
            "       f.name AS folder_name "
            "FROM email_messages m "
            "LEFT JOIN email_folders f ON f.id = m.folder_id "
            "WHERE m.id=? AND m.owner_user_id=?",
            (message_id, user_id),
        ).fetchone()
    return dict(row) if row else None


# ───────────────────────── flag updates ─────────────────────────────

def set_seen(message_id: int, user_id: str, seen: bool) -> bool:
    msg = _msg_lookup(message_id, user_id)
    if not msg or not msg["folder_name"]:
        return False
    # Local update first (optimistic).
    with get_conn() as conn:
        conn.execute(
            "UPDATE email_messages SET is_unread=? WHERE id=?",
            (0 if seen else 1, message_id),
        )
        conn.commit()
    # IMAP flag update.
    try:
        with imap_for_account(msg["account_id"]) as c:
            c.select_folder(msg["folder_name"])
            if seen:
                c.add_flags([msg["uid"]], [br"\Seen"])
            else:
                c.remove_flags([msg["uid"]], [br"\Seen"])
        return True
    except Exception as e:
        # Roll back the optimistic update.
        with get_conn() as conn:
            conn.execute(
                "UPDATE email_messages SET is_unread=? WHERE id=?",
                (msg["is_unread"], message_id),
            )
            conn.commit()
        log.warning("IMAP set_seen failed for msg %d: %s", message_id, e)
        return False


def set_starred(message_id: int, user_id: str, starred: bool) -> bool:
    msg = _msg_lookup(message_id, user_id)
    if not msg or not msg["folder_name"]:
        return False
    with get_conn() as conn:
        conn.execute(
            "UPDATE email_messages SET is_starred=? WHERE id=?",
            (1 if starred else 0, message_id),
        )
        conn.commit()
    try:
        with imap_for_account(msg["account_id"]) as c:
            c.select_folder(msg["folder_name"])
            if starred:
                c.add_flags([msg["uid"]], [br"\Flagged"])
            else:
                c.remove_flags([msg["uid"]], [br"\Flagged"])
        return True
    except Exception as e:
        with get_conn() as conn:
            conn.execute(
                "UPDATE email_messages SET is_starred=? WHERE id=?",
                (msg["is_starred"], message_id),
            )
            conn.commit()
        log.warning("IMAP set_starred failed for msg %d: %s", message_id, e)
        return False


# ───────────────────────── move / delete ────────────────────────────

def move_to_folder(message_id: int, user_id: str, target_folder_id: int) -> bool:
    """Move via IMAP UID MOVE (or COPY+EXPUNGE if MOVE unsupported).
    Local row is updated to the new folder_id but UID may change on
    move — we update the UID via the server's COPYUID response when
    available."""
    msg = _msg_lookup(message_id, user_id)
    if not msg or not msg["folder_name"]:
        return False
    with get_conn() as conn:
        target = conn.execute(
            "SELECT f.id, f.name FROM email_folders f "
            "JOIN email_accounts a ON a.id = f.account_id "
            "WHERE f.id=? AND a.owner_user_id=?",
            (target_folder_id, user_id),
        ).fetchone()
    if not target or target["id"] is None:
        return False
    if target["id"] == msg["folder_id"]:
        return True  # already there

    try:
        with imap_for_account(msg["account_id"]) as c:
            c.select_folder(msg["folder_name"])
            if c.has_capability(b"MOVE"):
                c.move([msg["uid"]], target["name"])
            else:
                c.copy([msg["uid"]], target["name"])
                c.delete_messages([msg["uid"]])
                c.expunge()
    except Exception as e:
        log.warning("IMAP move failed for msg %d: %s", message_id, e)
        return False

    # Update the local row. The UID is no longer valid in the old
    # folder; we set folder_id and clear uid (resync picks it up
    # under the new folder later via the standard fetch loop).
    with get_conn() as conn:
        conn.execute(
            "UPDATE email_messages SET folder_id=?, uid=? WHERE id=?",
            (target["id"], 0, message_id),
        )
        conn.commit()
    return True


def delete_message(message_id: int, user_id: str) -> bool:
    """Move to the account's Trash folder. Multi-step fallback:
      1. UID MOVE source → Trash (normal IMAP)
      2. UID COPY source → Trash, no source mutation (Gmail-style:
         "All Mail" / virtual folders are read-only; COPY adds the
         message under \\Trash which is how the provider models a
         label/delete, and the source folder stays untouched)
      3. STORE +FLAGS \\Deleted + EXPUNGE in source (no-Trash servers)

    Local row is removed after step 1 OR step 2 succeeds, so the UI
    reflects the delete even when the source folder won't accept an
    EXPUNGE. The next fetcher tick may re-create the row under the
    Trash folder, which is the correct end state.
    """
    msg = _msg_lookup(message_id, user_id)
    if not msg:
        return False
    with get_conn() as conn:
        # Find the Trash folder for this account.
        trash = conn.execute(
            "SELECT id, name FROM email_folders WHERE account_id=? AND "
            "(flags LIKE '%\\\\Trash%' OR LOWER(name) IN ('trash','gelöscht','geloescht','deleted','papierkorb')) "
            "LIMIT 1",
            (msg["account_id"],),
        ).fetchone()
    # Capture the Message-ID + source folder up front so every
    # successful-delete path can tombstone correctly (see migrations
    # 043 + 044). suppress_folder_id scopes the tombstone — NULL means
    # "block in any folder" (last-resort), a specific id means "block
    # re-insert into the original source folder, but allow Trash to
    # reappear via the fetcher's next tick".
    with get_conn() as conn:
        msg_row = conn.execute(
            "SELECT message_id, account_id, folder_id FROM email_messages WHERE id=?",
            (message_id,),
        ).fetchone()
    msg_mid = (msg_row["message_id"] if msg_row else None) or None
    msg_acct = int(msg_row["account_id"]) if msg_row else int(msg["account_id"])
    msg_source_folder = int(msg_row["folder_id"]) if (msg_row and msg_row["folder_id"]) else None

    def _tombstone(suppress_folder_id: Optional[int]) -> None:
        if not msg_mid:
            return
        try:
            with get_conn() as conn:
                # INSERT OR REPLACE so a second delete of the same
                # message updates the scope rather than failing on PK.
                conn.execute(
                    "INSERT OR REPLACE INTO email_deleted_message_ids "
                    "(account_id, message_id, suppress_folder_id) "
                    "VALUES (?, ?, ?)",
                    (msg_acct, msg_mid, suppress_folder_id),
                )
                conn.commit()
        except Exception as exc:  # noqa: BLE001
            log.debug("tombstone insert failed for msg %d (%s): %s",
                      message_id, msg_mid, exc)

    if trash and trash["id"]:
        # Step 1: full MOVE (works on standard IMAP servers).
        if move_to_folder(message_id, user_id, trash["id"]):
            # MOVE physically removed the message from the source folder
            # on the server. Tombstone the source folder defensively so
            # a quirky IMAP server can't re-create it on the next tick.
            _tombstone(suppress_folder_id=msg_source_folder)
            return True
        log.info("delete_message: MOVE failed for msg %d, trying COPY-only",
                 message_id)
        # Step 2: COPY + relocate local row to Trash + tombstone source.
        # On Gmail-style providers (Proton Bridge included) the source
        # folder ("All Mail" / a virtual label-folder) is read-only
        # for MOVE / EXPUNGE, but COPY still works and is the expected
        # way to add the \\Trash label. We don't touch the source
        # folder. The local row's folder_id is updated to Trash so
        # the message appears in Trash immediately (rather than
        # disappearing until the next fetcher tick). The source-
        # scoped tombstone keeps the fetcher from re-INSERTing it
        # under the original source folder when it sees the still-
        # present copy there.
        try:
            with imap_for_account(msg["account_id"]) as c:
                c.select_folder(msg["folder_name"])
                c.copy([msg["uid"]], trash["name"])
            with get_conn() as conn:
                # UID is unknown in Trash until the fetcher resyncs;
                # clear to 0 (matches move_to_folder).
                conn.execute(
                    "UPDATE email_messages SET folder_id=?, uid=? WHERE id=?",
                    (int(trash["id"]), 0, message_id),
                )
                conn.commit()
            _tombstone(suppress_folder_id=msg_source_folder)
            log.info("delete_message: COPY-to-Trash succeeded for msg %d "
                     "(source folder %r left untouched; local row moved "
                     "to Trash; source-scoped tombstone added)",
                     message_id, msg["folder_name"])
            return True
        except Exception as e:
            log.info("delete_message: COPY-to-Trash failed for msg %d: %s; "
                     "falling back to flag+EXPUNGE",
                     message_id, e)
    # Step 3: no Trash OR both MOVE and COPY failed — last-resort hard
    # delete in source. Works on servers without a Trash folder; will
    # also fail on read-only virtual folders (Gmail "All Mail", Proton
    # Bridge "All Mail"), which is when step 4 kicks in.
    try:
        with imap_for_account(msg["account_id"]) as c:
            c.select_folder(msg["folder_name"])
            c.delete_messages([msg["uid"]])
            c.expunge()
        with get_conn() as conn:
            conn.execute("DELETE FROM email_messages WHERE id=?", (message_id,))
            conn.commit()
        # EXPUNGE removed the message from source on the server;
        # tombstone source-scoped as a belt-and-braces guard against
        # eventual-consistency on rebuild-style servers.
        _tombstone(suppress_folder_id=msg_source_folder)
        return True
    except Exception as e:
        log.warning("IMAP delete failed for msg %d: %s (falling back to "
                    "local-row delete only — message remains on server)",
                    message_id, e)
    # Step 4: local-row-only delete. The IMAP server refused every
    # operation (typical when the source is a read-only virtual
    # folder like Proton Bridge's "All Mail"). The message stays on
    # the server but disappears from Yorik's view — the user gets a
    # successful delete in the UI. The next fetcher tick may re-
    # create the row if it sees the same message again; until the
    # fetcher gains a "user-deleted UIDs" guard, this is the cleanest
    # behaviour. Better than refusing the delete and leaving the user
    # with no way to remove a message from their Yorik mailbox.
    with get_conn() as conn:
        conn.execute("DELETE FROM email_messages WHERE id=?", (message_id,))
        conn.commit()
    # Server refused every IMAP op — message stays on the server in
    # whatever folders it was already in. Tombstone with NULL scope
    # so the fetcher refuses to re-create the row in ANY folder. The
    # user explicitly deleted; we honour that even if the IMAP side
    # can't be made consistent.
    _tombstone(suppress_folder_id=None)
    log.info("delete_message: local-only delete for msg %d (server "
             "refused every IMAP op; global tombstone)", message_id)
    return True


def move_to_junk(message_id: int, user_id: str) -> bool:
    """Move to the account's Junk/Spam folder. Mirrors delete_message:
    locate the folder via IMAP \\Junk flag OR a known-name allowlist
    (English + German variants). Returns False if no Junk folder
    exists — caller decides what to do (most providers create one
    automatically, but a few don't until the user clicks "Spam" in
    webmail at least once)."""
    msg = _msg_lookup(message_id, user_id)
    if not msg:
        return False
    with get_conn() as conn:
        junk = conn.execute(
            "SELECT id FROM email_folders WHERE account_id=? AND "
            "(flags LIKE '%\\\\Junk%' OR LOWER(name) IN "
            " ('junk','spam','junk-e-mail','junk e-mail','junk email',"
            "  'unerwünschte werbung','unerwuenschte werbung')) "
            "LIMIT 1",
            (msg["account_id"],),
        ).fetchone()
    if not junk or not junk["id"]:
        log.info("no junk folder found for account %d — sender will be "
                 "blocked locally but the message stays where it is",
                 msg["account_id"])
        return False
    return move_to_folder(message_id, user_id, junk["id"])


def archive_message(message_id: int, user_id: str) -> bool:
    """Move to Archive folder if one exists; otherwise add \\Seen
    and leave in place (close enough for providers without an
    explicit archive concept like GMX)."""
    msg = _msg_lookup(message_id, user_id)
    if not msg:
        return False
    with get_conn() as conn:
        archive = conn.execute(
            "SELECT id FROM email_folders WHERE account_id=? AND "
            "(flags LIKE '%\\\\Archive%' OR LOWER(name) IN ('archive','archiv')) "
            "LIMIT 1",
            (msg["account_id"],),
        ).fetchone()
    if archive and archive["id"]:
        return move_to_folder(message_id, user_id, archive["id"])
    # No archive folder — fall back to marking read.
    return set_seen(message_id, user_id, True)


# ───────────────────────── attachment fetch ────────────────────────

def fetch_attachment_binary(attachment_id: int, user_id: str) -> Optional[dict]:
    """Fetch the actual bytes for an attachment. We don't pre-download
    on initial sync (saves disk for the 90% nobody opens). When the
    UI requests one, we re-fetch the whole message from IMAP and
    extract the matching MIME part.

    Returns {filename, mimetype, content} or None if not found / failed.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT a.id, a.filename, a.mimetype, a.content_id, a.is_inline, "
            "       m.account_id, m.folder_id, m.uid, m.owner_user_id, "
            "       f.name AS folder_name "
            "FROM email_attachments a "
            "JOIN email_messages m ON m.id = a.message_id "
            "LEFT JOIN email_folders f ON f.id = m.folder_id "
            "WHERE a.id=? AND m.owner_user_id=?",
            (attachment_id, user_id),
        ).fetchone()
    if not row or not row["folder_name"]:
        return None

    try:
        import mailparser as _mp
        with imap_for_account(row["account_id"]) as c:
            c.select_folder(row["folder_name"])
            fetched = c.fetch([row["uid"]], [b"BODY.PEEK[]"])
            data = fetched.get(row["uid"])
            if not data:
                return None
            raw = data.get(b"BODY[]") or data.get(b"BODY.PEEK[]") or b""
        if not raw:
            return None
        parsed = _mp.parse_from_bytes(raw)
        # Match by filename + content-id (filename can collide, so
        # fall back to mimetype if names are missing).
        for att in (parsed.attachments or []):
            af = att.get("filename") or ""
            ac = att.get("content-id") or ""
            if (af and af == (row["filename"] or "")) or (ac and ac == (row["content_id"] or "")):
                payload = att.get("payload")
                if isinstance(payload, bytes):
                    raw_bytes = payload
                elif isinstance(payload, str):
                    # mail-parser sometimes b64-decodes already, sometimes not.
                    import base64 as _b64
                    try:
                        raw_bytes = _b64.b64decode(payload)
                    except Exception:
                        raw_bytes = payload.encode("utf-8", "replace")
                else:
                    continue
                return {
                    "filename": row["filename"] or "attachment",
                    "mimetype": row["mimetype"] or att.get("mail_content_type") or "application/octet-stream",
                    "content":  raw_bytes,
                }
        return None
    except Exception as e:
        log.warning("attachment fetch failed for id %d: %s", attachment_id, e)
        return None
