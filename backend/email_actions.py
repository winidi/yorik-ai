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
from typing import Any, Optional

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


def _remove_from_folder(c, uids: list) -> None:
    """Take these UIDs out of the selected folder after they were copied
    elsewhere. Only these: UID EXPUNGE (UIDPLUS). A plain EXPUNGE would
    also erase every mail another program had merely marked as deleted
    in this folder, for good. Without UIDPLUS the copies stay marked
    \\Deleted, which every mail program shows as deleted — nothing is
    lost."""
    c.delete_messages(uids)
    if c.has_capability(b"UIDPLUS"):
        c.uid_expunge(uids)
    else:
        log.info("server without UIDPLUS: %d mail(s) left marked \\Deleted instead of expunged", len(uids))


_TRASH_SQL = ("SELECT id, name FROM email_folders WHERE account_id=? AND "
              "(flags LIKE '%\\\\Trash%' OR LOWER(name) IN ('trash','gelöscht','geloescht','deleted','papierkorb')) "
              "LIMIT 1")


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
                _remove_from_folder(c, [msg["uid"]])
    except Exception as e:
        log.warning("IMAP move failed for msg %d: %s", message_id, e)
        return False

    # Update the local row. The UID is no longer valid in the old
    # folder; we set folder_id and NULL the uid so the next fetcher
    # tick fills in the real UID under the destination folder. NULL
    # (not 0) so the UNIQUE(account_id, folder_id, uid) constraint
    # admits multiple moved-but-not-yet-synced rows — Postgres treats
    # NULL as distinct in unique indexes. uid=0 was the original
    # sentinel and silently broke bulk moves into the same folder.
    with get_conn() as conn:
        conn.execute(
            "UPDATE email_messages SET folder_id=?, uid=NULL WHERE id=?",
            (target["id"], message_id),
        )
        conn.commit()
    return True


def delete_messages_bulk(message_ids: list[int], user_id: str) -> dict:
    """Move many messages to Trash in batches grouped by (account, folder).
    One IMAP login per (account, folder), one MOVE call per group —
    typical bulk-cleanup goes from ~500ms × N to a couple of seconds
    total even for hundreds of messages.

    Falls back per-group to COPY-then-flag-then-EXPUNGE when MOVE
    isn't supported, and to per-message delete_message() when even
    that fails. Returns {deleted, failed, by_group: [...]}."""
    if not message_ids:
        return {"deleted": 0, "failed": 0, "groups": []}

    # Resolve all (msg_id → account_id, folder_id, folder_name, uid,
    # message_id, source_folder_id) up front with one query so we
    # don't N+1 the lookup.
    with get_conn() as conn:
        ph = ",".join(["?"] * len(message_ids))
        rows = conn.execute(
            "SELECT m.id, m.account_id, m.folder_id, m.uid, m.message_id, "
            "       f.name AS folder_name "
            "FROM email_messages m "
            "LEFT JOIN email_folders f ON f.id = m.folder_id "
            "JOIN email_accounts a ON a.id = m.account_id "
            f"WHERE m.id IN ({ph}) AND a.owner_user_id = ?",
            (*message_ids, user_id),
        ).fetchall()

    # Group by (account_id, folder_id, folder_name). UIDs without a
    # folder name (folder row gone — shouldn't happen, defensive) or
    # without a uid (already-moved rows) get individually delete_message'd
    # as a last resort instead of polluting the batch.
    grouped: dict[tuple[int, int, str], list[dict]] = {}
    leftovers: list[int] = []
    for r in rows:
        if not r["folder_name"] or not r["uid"]:
            leftovers.append(int(r["id"]))
            continue
        key = (int(r["account_id"]), int(r["folder_id"]), r["folder_name"])
        grouped.setdefault(key, []).append(dict(r))

    deleted_total = 0
    failed_total = 0
    group_results: list[dict] = []

    for (account_id, folder_id, folder_name), msgs in grouped.items():
        msg_ids_in_group = [m["id"] for m in msgs]
        uids_in_group   = [int(m["uid"]) for m in msgs]

        # Find this account's Trash folder. Empty = no Trash, we hard-
        # delete with STORE+EXPUNGE in the source folder instead.
        with get_conn() as conn:
            trash = conn.execute(
                "SELECT id, name FROM email_folders WHERE account_id=? AND "
                "(flags LIKE '%\\\\Trash%' OR LOWER(name) IN ('trash','gelöscht','geloescht','deleted','papierkorb')) "
                "LIMIT 1",
                (account_id,),
            ).fetchone()

        group_status: dict[str, Any] = {
            "account_id": account_id,
            "folder_id": folder_id,
            "count": len(msgs),
        }

        try:
            with imap_for_account(account_id) as c:
                c.select_folder(folder_name)
                if trash and trash["name"] and folder_id == int(trash["id"]):
                    # Deleting in the Trash is the explicit "delete for
                    # good" — these mails only, never the whole folder.
                    _remove_from_folder(c, uids_in_group)
                    with get_conn() as conn:
                        drop_attachment_files(msg_ids_in_group)
                        conn.execute(
                            f"DELETE FROM email_messages WHERE id IN ({','.join(['?'] * len(msg_ids_in_group))})",
                            tuple(msg_ids_in_group),
                        )
                        conn.commit()
                elif trash and trash["name"]:
                    if c.has_capability(b"MOVE"):
                        c.move(uids_in_group, trash["name"])
                    else:
                        c.copy(uids_in_group, trash["name"])
                        _remove_from_folder(c, uids_in_group)
                    # Update local rows to point at Trash with NULL uid
                    # so the unique constraint admits all of them; the
                    # fetcher's next pass under the Trash folder fills
                    # in real UIDs. No tombstone: the mails really left
                    # the source folder, and one moved back later (from
                    # a phone, say) must show up there again.
                    with get_conn() as conn:
                        for mid in msg_ids_in_group:
                            conn.execute(
                                "UPDATE email_messages SET folder_id=?, uid=NULL WHERE id=?",
                                (int(trash["id"]), mid),
                            )
                        conn.commit()
                else:
                    # No Trash folder: nothing is deleted. The mails stay
                    # where they are and the caller reports a failure —
                    # never a silent delete for good.
                    raise RuntimeError("no Trash folder on this account — nothing deleted")
            deleted_total += len(msgs)
            group_status["ok"] = True
            group_status["deleted"] = len(msgs)
        except Exception as exc:  # noqa: BLE001
            # Group-level failure → fall back to single-message delete
            # for each, which has its own multi-step fallback chain.
            log.warning("bulk delete group acct=%s folder=%s failed (%s); "
                        "falling back per-message", account_id, folder_name, exc)
            fallback_ok = 0
            for mid in msg_ids_in_group:
                if delete_message(mid, user_id):
                    fallback_ok += 1
            deleted_total += fallback_ok
            failed_total += len(msgs) - fallback_ok
            group_status["ok"] = False
            group_status["error"] = str(exc)
            group_status["deleted"] = fallback_ok
            group_status["failed"] = len(msgs) - fallback_ok
        group_results.append(group_status)

    # Anything excluded from grouping (missing folder/uid) — handle
    # one by one. Rare edge case; keeps the bulk path predictable.
    if leftovers:
        for mid in leftovers:
            if delete_message(mid, user_id):
                deleted_total += 1
            else:
                failed_total += 1

    return {"deleted": deleted_total, "failed": failed_total, "groups": group_results}


def delete_message(message_id: int, user_id: str) -> bool:
    """Delete the way a mail program does — nothing is lost by accident:

      1. In any folder but the Trash: MOVE to the Trash (COPY + remove
         these UIDs where MOVE is missing).
      2. Where the source refuses to let go (Gmail's "All Mail", Proton
         Bridge): COPY to the Trash, which is how those providers model
         a delete; the source copy is hidden in Yorik by a tombstone.
      3. In the Trash itself: delete for good, this mail only.

    Anything else — no Trash folder, the server refusing — deletes
    nothing and returns False; the mail stays in Yorik and on the server
    and the user sees the error. (It used to fall back to erasing the
    mail for good, or to hiding it in Yorik while it stayed on the
    server.)"""
    msg = _msg_lookup(message_id, user_id)
    if not msg:
        return False
    with get_conn() as conn:
        trash = conn.execute(_TRASH_SQL, (msg["account_id"],)).fetchone()
        msg_row = conn.execute(
            "SELECT message_id, account_id, folder_id FROM email_messages WHERE id=?",
            (message_id,),
        ).fetchone()
    if not trash or not trash["id"]:
        log.warning("delete_message: account %d has no Trash folder — msg %d not deleted",
                    msg["account_id"], message_id)
        return False
    msg_mid = (msg_row["message_id"] if msg_row else None) or None
    source_folder = int(msg_row["folder_id"]) if (msg_row and msg_row["folder_id"]) else None

    # 3. Already in the Trash: for good, this one only.
    if source_folder == int(trash["id"]):
        if not msg["uid"]:
            return False                      # not on the server yet as far as Yorik knows
        try:
            with imap_for_account(msg["account_id"]) as c:
                c.select_folder(msg["folder_name"])
                _remove_from_folder(c, [msg["uid"]])
        except Exception as e:  # noqa: BLE001
            log.warning("delete for good failed for msg %d: %s", message_id, e)
            return False
        with get_conn() as conn:
            drop_attachment_files([message_id])
            conn.execute("DELETE FROM email_messages WHERE id=?", (message_id,))
            conn.commit()
        return True

    # 1. Move to the Trash.
    if move_to_folder(message_id, user_id, trash["id"]):
        return True

    # 2. COPY only (read-only virtual source folders).
    log.info("delete_message: MOVE failed for msg %d, trying COPY to Trash", message_id)
    try:
        with imap_for_account(msg["account_id"]) as c:
            c.select_folder(msg["folder_name"])
            c.copy([msg["uid"]], trash["name"])
    except Exception as e:  # noqa: BLE001
        log.warning("delete_message: msg %d stays where it is — the server refused MOVE and COPY: %s",
                    message_id, e)
        return False
    with get_conn() as conn:
        # UID is unknown in Trash until the fetcher resyncs; NULL so
        # several deletions into Trash don't collide on
        # UNIQUE(account_id, folder_id, uid).
        conn.execute("UPDATE email_messages SET folder_id=?, uid=NULL WHERE id=?",
                     (int(trash["id"]), message_id))
        if msg_mid and source_folder:
            # The source copy stays on the server by the provider's
            # design; keep it out of that folder in Yorik.
            conn.execute(
                "INSERT OR REPLACE INTO email_deleted_message_ids "
                "(account_id, message_id, suppress_folder_id) VALUES (?, ?, ?)",
                (int(msg["account_id"]), msg_mid, source_folder),
            )
        conn.commit()
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

# One mail, fetched once. The reader asks for every inline image and
# every attachment of a mail at the same moment; each request used to
# open its own IMAP connection and pull the whole message again
# (fifteen parallel downloads for one dunning letter — most of them
# timed out after 20 s and answered 404). The raw message is now
# fetched once per mail, under a lock, and kept for a few minutes.
_RAW_CACHE: "dict[int, tuple[float, bytes]]" = {}
_RAW_LOCKS: "dict[int, Any]" = {}
_RAW_GUARD = __import__("threading").Lock()
_RAW_TTL_S = 300
_RAW_MAX_BYTES = 120 * 1024 * 1024
# …and one login at a time per account. Providers answer a burst of
# logins with a tarpit (freenet: every login hangs for 20 s+ for
# minutes afterwards), which is how one opened mail broke all of them.
_ACCOUNT_LOCKS: "dict[int, Any]" = {}


def _raw_message(message_id: int, account_id: int, folder_name: str, uid: int) -> bytes:
    import threading
    import time
    now = time.time()
    with _RAW_GUARD:
        for k in [k for k, (t, _) in _RAW_CACHE.items() if now - t > _RAW_TTL_S]:
            _RAW_CACHE.pop(k, None)
        hit = _RAW_CACHE.get(message_id)
        if hit:
            return hit[1]
        lock = _RAW_LOCKS.setdefault(message_id, threading.Lock())
    with lock:
        with _RAW_GUARD:
            hit = _RAW_CACHE.get(message_id)
        if hit:
            return hit[1]
        with _RAW_GUARD:
            account_lock = _ACCOUNT_LOCKS.setdefault(int(account_id), threading.Lock())
        with account_lock, imap_for_account(account_id) as c:
            try:
                c._imap.sock.settimeout(90)      # a mail with scans is megabytes; 20 s is for commands, not for this
            except Exception:  # noqa: BLE001
                pass
            c.select_folder(folder_name, readonly=True)
            fetched = c.fetch([uid], [b"BODY.PEEK[]"])
            data = fetched.get(uid) or {}
            raw = data.get(b"BODY[]") or data.get(b"BODY.PEEK[]") or b""
        if raw:
            with _RAW_GUARD:
                while _RAW_CACHE and sum(len(v[1]) for v in _RAW_CACHE.values()) + len(raw) > _RAW_MAX_BYTES:
                    _RAW_CACHE.pop(min(_RAW_CACHE, key=lambda k: _RAW_CACHE[k][0]))
                _RAW_CACHE[message_id] = (time.time(), raw)
        return raw


def _attachment_dir() -> "Any":
    import os
    from pathlib import Path
    return Path(os.getenv("YORIK_DATA_DIR", Path(__file__).resolve().parent.parent / "data")) / "email_attachments"


def drop_attachment_files(message_ids) -> None:
    """A deleted mail takes its kept attachments along."""
    import shutil
    for mid in message_ids or []:
        shutil.rmtree(_attachment_dir() / str(int(mid)), ignore_errors=True)


def _store_attachment(attachment_id: int, message_id: int, content: bytes) -> None:
    """Keep a fetched attachment (real ones; inline images are re-read
    from the cached message). Best-effort."""
    try:
        folder = _attachment_dir() / str(message_id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / str(attachment_id)
        path.write_bytes(content)
        with get_conn() as conn:
            conn.execute("UPDATE email_attachments SET local_path = ?, size_bytes = ? WHERE id = ?",
                         (str(path), len(content), attachment_id))
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        log.debug("attachment %s not cached: %s", attachment_id, exc)


def _cached_attachment(row) -> Optional[dict]:
    from pathlib import Path
    with get_conn() as conn:
        r = conn.execute("SELECT local_path FROM email_attachments WHERE id = ?", (row["id"],)).fetchone()
    path = Path(r["local_path"]) if r and r["local_path"] else None
    if not path or not path.is_file():
        return None
    return {"filename": row["filename"] or "attachment",
            "mimetype": effective_mimetype(row["filename"], row["mimetype"]),
            "content": path.read_bytes()}


def effective_mimetype(filename: Optional[str], stored: Optional[str]) -> str:
    """Senders label attachments carelessly ("2. Mahnung.PDF" as
    application/octet-stream). With a generic label the file name
    decides, case-insensitively — otherwise the browser downloads a PDF
    instead of showing it."""
    import mimetypes
    stored = (stored or "").strip().lower()
    if stored and stored not in ("application/octet-stream", "binary/octet-stream", "application/x-download",
                                 "application/force-download", "application/unknown"):
        return stored
    guessed, _ = mimetypes.guess_type((filename or "").lower())
    return guessed or stored or "application/octet-stream"


def fetch_attachment_binary(attachment_id: int, user_id: str) -> Optional[dict]:
    """Fetch the actual bytes for an attachment. We don't pre-download
    on initial sync (saves disk for the 90% nobody opens). When the
    UI requests one, the message is fetched from IMAP (once per mail,
    see _raw_message) and the matching MIME part extracted.

    Returns {filename, mimetype, content} or None if not found / failed.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT a.id, a.message_id, a.filename, a.mimetype, a.content_id, a.is_inline, "
            "       m.account_id, m.folder_id, m.uid, m.owner_user_id, "
            "       f.name AS folder_name "
            "FROM email_attachments a "
            "JOIN email_messages m ON m.id = a.message_id "
            "LEFT JOIN email_folders f ON f.id = m.folder_id "
            "WHERE a.id=? AND m.owner_user_id=?",
            (attachment_id, user_id),
        ).fetchone()
    if not row:
        return None

    # Opened once, kept on disk: the second look at a dunning letter must
    # not depend on the mail server being in the mood.
    cached = _cached_attachment(row)
    if cached is not None:
        return cached
    if not row["folder_name"]:
        return None

    try:
        import mailparser as _mp
        raw = _raw_message(int(row["message_id"]), row["account_id"], row["folder_name"], row["uid"])
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
                _store_attachment(int(row["id"]), int(row["message_id"]), raw_bytes)
                return {
                    "filename": row["filename"] or "attachment",
                    "mimetype": effective_mimetype(row["filename"], row["mimetype"] or att.get("mail_content_type")),
                    "content":  raw_bytes,
                }
        return None
    except Exception as e:
        log.warning("attachment fetch failed for id %d: %s", attachment_id, e)
        return None
