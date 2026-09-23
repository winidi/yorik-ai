"""Multi-account IMAP fetcher.

One asyncio task per enabled email account. Each task:
  1. Connects (TLS), authenticates, selects the inbox folder
  2. Does an initial UID-based catch-up sync (any new UIDs since
     last_uid_next get fetched)
  3. Enters IMAP IDLE — server pushes us a notification when new
     mail arrives (instant, no polling). Drops to polling every
     60 seconds if the server doesn't support IDLE.
  4. On any failure: exponential backoff (5s → 60s cap), reconnect.
  5. On UIDVALIDITY change: drops the cached folder state and
     re-syncs from scratch (IMAP servers reset UIDs sometimes).

Each step writes status to email_accounts.last_sync_at / last_error
so the UI can show "connected" / "auth failed: …" without us logging
to a file the user never sees.

Bodies are stored in two forms — body_text (always) and body_html
(when present). The reader pane picks HTML when available and
sandboxes it; the AI pipeline (drafts, briefing, semantic search)
uses body_text so it doesn't have to strip tags.

Threading: messages with the same Message-ID stay together. New
messages link via In-Reply-To and References headers; thread_id is
derived from the root of that chain (or just the Message-ID for
unthreaded mail).
"""

from __future__ import annotations

import asyncio
import json
import re
import logging
import os
import socket
import ssl
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime, getaddresses
from typing import Any, Optional

import mailparser
from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientError

from . import credential_store
from .database import get_conn

log = logging.getLogger("yorik.email.fetcher")

POLL_FALLBACK_S = 60       # if IDLE unsupported, check this often
IDLE_REFRESH_S  = 25 * 60  # re-issue IDLE every 25 min (RFC 2177 recommends ≤29)
RECONNECT_INITIAL_S = 5
RECONNECT_MAX_S = 60
SNIPPET_LEN = 220

# How much of each folder 'recent' keeps (the default import scope), and
# how many mails one sync pass may bring in before it goes back to
# listening for new mail (a big import continues on the next pass).
RECENT_INBOX = 200
RECENT_OTHER = 50
PASS_BUDGET = 300
FETCH_CHUNK = 50
# A mail that fails this often is parked and shown in the settings; the
# "Repair" action gives it another round.
MAX_ATTEMPTS = 5
# A full compare with the server at most this often per account, unless
# an import is still running or a repair was asked for.
RECONCILE_EVERY_S = 15 * 60
# Mail older than this, found by an import or a repair, arrives quietly:
# no bell proposal, no draft, no suggestion.
QUIET_AFTER_S = 48 * 3600
_SENT_NAMES = {"sent", "sent items", "sent messages", "sent mail", "gesendet", "gesendete elemente",
               "gesendete objekte", "gesendete nachrichten"}
FETCH_ITEMS = [b"INTERNALDATE", b"FLAGS", b"RFC822.SIZE", b"BODY.PEEK[HEADER]", b"BODY.PEEK[TEXT]"]
ATTACHMENT_DIR_DEFAULT = "data/email_attachments"

# Auto-draft debounce — coalesce a burst of messages into a single
# draft pass per thread. Long enough that a multi-message reply
# doesn't generate 3 drafts; short enough that the draft is ready
# by the time the user opens the message.
AUTODRAFT_DEBOUNCE_S = 5.0

# Map of account_id → asyncio.Task so we can cancel cleanly on shutdown
# AND on account disable / delete (the routes module calls reload_account).
_account_tasks: dict[int, asyncio.Task] = {}
_supervisor_task: Optional[asyncio.Task] = None
_supervisor_stop: Optional[asyncio.Event] = None


# ───────────────────────── lifecycle ────────────────────────────────

def start_background(loop: asyncio.AbstractEventLoop) -> None:
    """Spawn the supervisor task. Idempotent."""
    global _supervisor_task, _supervisor_stop
    if _supervisor_task and not _supervisor_task.done():
        return
    _supervisor_stop = asyncio.Event()
    _supervisor_task = loop.create_task(_supervisor(), name="email-supervisor")


async def stop_background() -> None:
    """Cancel every account task and the supervisor."""
    global _supervisor_task, _supervisor_stop
    if _supervisor_stop:
        _supervisor_stop.set()
    for t in list(_account_tasks.values()):
        t.cancel()
    if _supervisor_task and not _supervisor_task.done():
        _supervisor_task.cancel()
        try:
            await _supervisor_task
        except asyncio.CancelledError:
            pass
    _account_tasks.clear()


async def reload_account(account_id: int) -> None:
    """Called by routes when an account is added / updated / disabled.
    Cancels the old task (if any) and starts a fresh one if still enabled."""
    old = _account_tasks.pop(account_id, None)
    if old and not old.done():
        old.cancel()
        try:
            await old
        except asyncio.CancelledError:
            pass
    cfg = _load_account_config(account_id)
    if cfg and cfg["enabled"]:
        _account_tasks[account_id] = asyncio.get_running_loop().create_task(
            _account_loop(account_id), name=f"email-account-{account_id}",
        )


async def _supervisor() -> None:
    """Keeps the account supervisor alive for the life of the process.
    A crash inside (DB hiccup at boot, a bad account row) used to end
    email fetching until the next restart; now it logs and retries."""
    while True:
        try:
            await _supervisor_once()
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("email supervisor crashed: %s — restarting in 30 s", exc)
            await asyncio.sleep(30)


async def _supervisor_once() -> None:
    """Spawns one _account_loop per enabled account at startup, then
    sleeps. Account add/remove is event-driven via reload_account."""
    from . import workers
    # Supervisor heartbeats once at start-up + on account add/remove.
    # No periodic tick — only show stale after several hours of silence.
    workers.register("email_supervisor", kind="supervisor",
                     expected_interval_s=3600)
    try:
        await asyncio.sleep(2)  # let DB init finish on cold start
        accounts = _load_enabled_account_ids()
        loop = asyncio.get_running_loop()
        for aid in accounts:
            _account_tasks[aid] = loop.create_task(_account_loop(aid), name=f"email-account-{aid}")
        log.info("email supervisor: %d active account(s)", len(accounts))
        workers.heartbeat("email_supervisor", "ok",
                          f"{len(accounts)} active account(s)")
        # Stay alive until shutdown.
        if _supervisor_stop:
            await _supervisor_stop.wait()
    except asyncio.CancelledError:
        log.info("email supervisor cancelled")
        workers.report_error("email_supervisor", "cancelled")
        raise
    except Exception as exc:
        workers.report_error("email_supervisor", str(exc)[:100])
        raise


# ───────────────────────── per-account loop ─────────────────────────

async def _account_loop(account_id: int) -> None:
    """One forever-running task per account. Reconnects on any error
    with exponential backoff."""
    from . import workers
    worker_name = f"email_account_{account_id}"
    # IMAP IDLE is event-driven — a quiet mailbox can legitimately go
    # hours between heartbeats. Per-folder INFO logs still surface
    # activity in yorik.log; the worker chip just stops flapping.
    workers.register(worker_name, kind="subscriber", expected_interval_s=3600)
    backoff = RECONNECT_INITIAL_S
    while True:
        cfg = _load_account_config(account_id)
        if not cfg or not cfg["enabled"]:
            log.info("account %d disabled / deleted, exiting loop", account_id)
            workers.heartbeat(worker_name, "warn", "disabled")
            return
        try:
            await asyncio.to_thread(_run_once, cfg)
            # _run_once only returns when IDLE times out cleanly or the
            # account loop is asked to refresh — reset backoff.
            backoff = RECONNECT_INITIAL_S
            workers.heartbeat(worker_name, "ok",
                              f"{cfg.get('email', 'account')} idling")
        except asyncio.CancelledError:
            log.info("account %d loop cancelled", account_id)
            workers.report_error(worker_name, "cancelled")
            raise
        except Exception as e:
            _record_account_error(account_id, str(e))
            log.warning("account %d errored, reconnecting in %ds: %s",
                        account_id, backoff, e)
            workers.heartbeat(worker_name, "warn",
                              f"reconnecting in {backoff}s: {str(e)[:60]}")
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                raise
            backoff = min(int(backoff * 1.7), RECONNECT_MAX_S)


def _run_once(cfg: dict) -> None:
    """Synchronous IMAP work for one connection lifetime. Returns when
    the connection should be re-established (either IDLE timeout for
    a refresh, or after a polling cycle if IDLE unsupported)."""
    account_id = cfg["id"]
    password = credential_store.get(cfg["credential_key"]) or {}
    pw = password.get("imap_password") if isinstance(password, dict) else password
    if not pw:
        raise RuntimeError("no IMAP password in credential store")

    # TLS context: default system CA bundle for public hosts;
    # unverified for loopback (Proton Bridge, Mailpit etc. ship with
    # self-signed certs that strict verification rejects).
    ssl_on = bool(cfg["imap_ssl"])
    use_starttls = bool(cfg.get("imap_starttls")) and not ssl_on
    from .email_ssl import make_ssl_context
    ssl_ctx = make_ssl_context(cfg["imap_host"]) if (ssl_on or use_starttls) else None
    sock_timeout = 30
    socket.setdefaulttimeout(sock_timeout)

    with IMAPClient(host=cfg["imap_host"], port=cfg["imap_port"],
                    ssl=ssl_on, ssl_context=(ssl_ctx if ssl_on else None),
                    timeout=sock_timeout) as c:
        if use_starttls:
            c.starttls(ssl_context=ssl_ctx)
        c.login(cfg["imap_username"], pw)
        log.info("account %d connected as %s", account_id, cfg["imap_username"])

        # Enumerate ALL folders (Inbox, Sent, Drafts, Spam, Trash,
        # any user-created). Each gets a row in email_folders with
        # its SPECIAL-USE flags persisted so the UI can render
        # category icons / pre-fill folder names per locale.
        _enumerate_folders(c, account_id)

        # Sync each folder. INBOX gets the deep treatment + IDLE;
        # the rest get a one-shot "latest 30" so the user sees
        # something in their Sent / Spam / etc. immediately.
        inbox_folder_id = None
        inbox_name = None
        with get_conn() as conn:
            folders = conn.execute(
                "SELECT id, name, flags FROM email_folders WHERE account_id=?",
                (account_id,),
            ).fetchall()
        for f in folders:
            flags = json.loads(f["flags"] or "[]") if f["flags"] else []
            is_inbox = "\\Inbox" in flags or f["name"].upper() == "INBOX"
            try:
                c.select_folder(f["name"])
            except Exception as e:
                log.warning("account %d: cannot select %s: %s", account_id, f["name"], e)
                continue
            if is_inbox:
                inbox_folder_id = f["id"]
                inbox_name = f["name"]
                n = _sync_new_messages(c, cfg, f["id"], initial_cap=200)
                if n:
                    log.info("account %d %s: %d new", account_id, f["name"], n)
            else:
                # Non-inbox: a small first sync; the sync pass fills the
                # rest of the import scope in the background.
                n = _sync_new_messages(c, cfg, f["id"], initial_cap=RECENT_OTHER)
                if n:
                    log.info("account %d %s: %d new", account_id, f["name"], n)

        # The sync pass: fetch what is missing, retry failures, take over
        # read/flagged, drop what the server no longer has. Not on every
        # reconnect (a new mail reconnects), but at least every
        # RECONCILE_EVERY_S, right away after "Repair", and again soon
        # while an import is unfinished.
        idle_timeout = IDLE_REFRESH_S
        if _reconcile_due(cfg):
            state = reconcile_account(c, cfg)
            _last_reconcile[account_id] = __import__("time").monotonic()
            if state.get("pending"):
                idle_timeout = 30

        _record_account_sync(account_id)

        # Re-select INBOX for IDLE. (Some servers reject IDLE on
        # virtual folders like Gmail labels; sticking to INBOX is
        # the safe default.)
        if inbox_folder_id is None:
            log.warning("account %d: no inbox folder found, idle loop skipped", account_id)
            c.logout()
            return
        c.select_folder(inbox_name)

        # IDLE loop (or fall back to polling if unsupported).
        if c.has_capability(b"IDLE"):
            _idle_then_sync(c, cfg, inbox_folder_id, timeout=idle_timeout)
        else:
            log.info("account %d: server lacks IDLE, polling every %ds",
                     account_id, POLL_FALLBACK_S)
            import time
            time.sleep(POLL_FALLBACK_S)
            # Return — outer loop reconnects + syncs again.

        c.logout()


_last_reconcile: dict[int, float] = {}


def _reconcile_due(cfg: dict) -> bool:
    import time
    if cfg.get("repair_requested"):
        return True
    try:
        state = json.loads(cfg.get("sync_state") or "{}")
    except ValueError:
        state = {}
    if state.get("pending"):
        return True
    last = _last_reconcile.get(cfg["id"])
    return last is None or time.monotonic() - last >= RECONCILE_EVERY_S


def _idle_then_sync(c: IMAPClient, cfg: dict, folder_id: int, timeout: int = IDLE_REFRESH_S) -> None:
    """Issue IDLE, wait for server push (or timeout), then sync delta."""
    folder_name = _folder_name(folder_id)
    c.idle()
    try:
        # Wait up to `timeout` for a server push. None timeout would
        # block forever; we want periodic refresh per RFC 2177.
        responses = c.idle_check(timeout=timeout)
    finally:
        c.idle_done()
    if responses:
        # Server pushed at least one EXISTS notification — re-select
        # to refresh state then sync any new UIDs.
        c.select_folder(folder_name)
        n = _sync_new_messages(c, cfg, folder_id)
        if n:
            log.info("account %d IDLE → %d new", cfg["id"], n)
    _record_account_sync(cfg["id"])


def _select_inbox(c: IMAPClient) -> str:
    """Find the IMAP folder name for the inbox. Most servers use
    'INBOX' literally, but a few localise it (e.g. 'Posteingang')."""
    for f in c.list_folders():
        flags, _delim, name = f
        if b"\\Inbox" in flags:
            return name
    return "INBOX"


def _sync_new_messages(c: IMAPClient, cfg: dict, folder_id: int,
                        initial_cap: int = 200,
                        force_start_uid: Optional[int] = None,
                        force_end_uid: Optional[int] = None,
                        freeze_uid_next: bool = False,
                        quiet: bool = False) -> int:
    """Fetch every UID >= last_uid_next and insert into email_messages.
    Returns count actually inserted.

    `initial_cap` is how many mails the FIRST sync of a folder brings in
    — the latest that many mails, not the latest that many UID numbers
    (after years of deleting and moving, the last 200 numbers of a GMX
    inbox held 17 mails). The rest of the import scope follows in the
    background through reconcile_account().

    `force_start_uid` overrides the prev_next high-watermark — used by
    the "Load older" backfill path which scans UIDs *below* the
    locally-known min. Pair with `force_end_uid` to bound the upper
    side; otherwise the search uses `:*`.

    `freeze_uid_next` skips the trailing `UPDATE email_folders SET
    uid_next=…` so a backfill pass doesn't accidentally pretend the
    older UIDs are the new high-watermark.

    A mail that fails to parse or save does not hold the watermark back
    (the mails after it still arrive) and is not lost either: it is
    recorded in email_fetch_failures and retried on every pass."""
    folder_name = _folder_name(folder_id)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT uid_validity, uid_next FROM email_folders WHERE id=?",
            (folder_id,),
        ).fetchone()
    prev_validity = row["uid_validity"] if row else None
    prev_next = int((row["uid_next"] if row else None) or 1)

    sel = c.folder_status(folder_name, [b"UIDVALIDITY", b"UIDNEXT"])
    validity = int(sel[b"UIDVALIDITY"])
    next_uid = int(sel.get(b"UIDNEXT", 1))

    if prev_validity is not None and prev_validity != validity:
        log.warning("account %d UIDVALIDITY changed (%s → %s), full resync",
                    cfg["id"], prev_validity, validity)
        _forget_uids(cfg["id"], folder_id)
        prev_next = 1

    # Transient "SEARCH failed: no such message" / "NO" responses are
    # normal during catch-up against a folder that's actively shifting
    # (Proton Bridge re-indexing, or another client moving messages
    # mid-search). Treat as "nothing to fetch this round" — IDLE will
    # wake us again with fresh state. NOT a real failure, so we don't
    # surface it as last_error to the UI.
    try:
        if force_start_uid is not None:
            end_part = str(force_end_uid) if force_end_uid is not None else "*"
            new_uids = c.search([u"UID", f"{force_start_uid}:{end_part}"])
        elif prev_next == 1:
            new_uids = sorted(u for u in c.search(["UNDELETED"]) if u)[-initial_cap:] if initial_cap else []
        else:
            new_uids = c.search([u"UID", f"{prev_next}:*"])
    except IMAPClientError as exc:
        msg = str(exc).lower()
        if "no such message" in msg or "uidset" in msg or "search failed" in msg:
            log.info("account %d folder %s: transient SEARCH miss (%s) — "
                     "will retry on next IDLE wake", cfg["id"], folder_name, exc)
            return 0
        # Real IMAP error — let the outer reconnect loop handle it.
        raise
    # Backfill mode: keep ALL the returned UIDs (no lower-bound filter
    # against prev_next, because we're explicitly scanning UIDs below
    # it). Optionally clip to force_end_uid so we don't reach past it.
    if force_start_uid is not None:
        new_uids = sorted(u for u in new_uids if u
                          and u >= force_start_uid
                          and (force_end_uid is None or u <= force_end_uid))
    else:
        new_uids = sorted(u for u in new_uids if u and u >= prev_next)

    inserted = 0
    highest_uid = prev_next - 1
    if new_uids:
        # The first sync of a folder brings its past: quiet for old mail.
        q = quiet or ("auto" if prev_next == 1 and force_start_uid is None else False)
        inserted, _failed = _fetch_and_store(c, cfg, folder_id, new_uids, quiet=q)
        highest_uid = max(highest_uid, max(new_uids))

    if not freeze_uid_next:
        with get_conn() as conn:
            conn.execute(
                "UPDATE email_folders SET uid_validity=?, uid_next=?, last_sync_at=datetime('now') WHERE id=?",
                (validity, max(prev_next, highest_uid + 1, next_uid), folder_id),
            )
            conn.commit()
    return inserted


def _fetch_and_store(c: IMAPClient, cfg: dict, folder_id: int, uids: list,
                     quiet: Any = False) -> tuple[int, list]:
    """Fetch these UIDs of the selected folder in chunks and save them.
    Returns (saved, failed uids). A mail that fails is recorded in
    email_fetch_failures (retried later); one that succeeds clears its
    record. `quiet` True/False, or "auto": quiet for mail older than
    QUIET_AFTER_S (an import brings old mail, but a new one that a
    missed IDLE left behind should still notify)."""
    saved, failed = 0, []
    now = datetime.now(timezone.utc)
    for i in range(0, len(uids), FETCH_CHUNK):
        chunk = uids[i:i + FETCH_CHUNK]
        fetched = c.fetch(chunk, FETCH_ITEMS)
        for uid in chunk:
            data = fetched.get(uid)
            if data is None:
                # Gone between SEARCH and FETCH (moved or expunged by
                # another client). Not a failure; the next pass sees
                # the folder as it is.
                continue
            q = quiet
            if quiet == "auto":
                q = True
                try:
                    q = (now - data[b"INTERNALDATE"].astimezone(timezone.utc)).total_seconds() > QUIET_AFTER_S
                except Exception:  # noqa: BLE001
                    pass
            try:
                raw_headers = data[b"BODY[HEADER]"] or b""
                raw_body = data[b"BODY[TEXT]"] or b""
                raw_full = raw_headers + b"\r\n" + raw_body
                parsed = mailparser.parse_from_bytes(raw_full)
                _insert_message(cfg, folder_id, uid, data, parsed, quiet=bool(q))
                saved += 1
                _clear_failure(cfg["id"], folder_id, uid)
            except Exception as e:  # noqa: BLE001
                log.exception("account %d UID %d could not be stored: %s", cfg["id"], uid, e)
                _record_failure(cfg["id"], folder_id, uid, e)
                failed.append(uid)
    return saved, failed


# ───────────────────────── failures ─────────────────────────────────

def _record_failure(account_id: int, folder_id: int, uid: int, error: Exception) -> None:
    text = f"{type(error).__name__}: {error}"[:500]
    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO email_fetch_failures "
                "(account_id, folder_id, uid, attempts, last_error, first_failed_at, last_tried_at, parked) "
                "VALUES (?, ?, ?, 1, ?, datetime('now'), datetime('now'), 0) "
                "ON CONFLICT (account_id, folder_id, uid) DO UPDATE SET "
                "  attempts = email_fetch_failures.attempts + 1, last_error = EXCLUDED.last_error, "
                "  last_tried_at = EXCLUDED.last_tried_at, "
                "  parked = CASE WHEN email_fetch_failures.attempts + 1 >= ? THEN 1 ELSE 0 END",
                (account_id, folder_id, uid, text, MAX_ATTEMPTS),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        log.error("account %d: could not record the failure of UID %d (%s): %s",
                  account_id, uid, text, exc)


def _clear_failure(account_id: int, folder_id: int, uid: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM email_fetch_failures WHERE account_id=? AND folder_id=? AND uid=?",
                     (account_id, folder_id, uid))
        conn.commit()


def _retry_failures(c: IMAPClient, cfg: dict, folder_id: int) -> int:
    """Try the recorded, not yet parked failures of the selected folder
    again. Returns how many are now stored."""
    with get_conn() as conn:
        uids = [int(r["uid"]) for r in conn.execute(
            "SELECT uid FROM email_fetch_failures WHERE account_id=? AND folder_id=? AND parked=0 ORDER BY uid",
            (cfg["id"], folder_id),
        ).fetchall()]
    if not uids:
        return 0
    present = set(c.search(["UID", ",".join(str(u) for u in uids)]))
    gone = [u for u in uids if u not in present]
    for u in gone:                     # deleted or moved on the server meanwhile
        _clear_failure(cfg["id"], folder_id, u)
    saved, _ = _fetch_and_store(c, cfg, folder_id, [u for u in uids if u in present], quiet="auto")
    if saved:
        log.info("account %d folder %d: %d earlier failure(s) now stored", cfg["id"], folder_id, saved)
    return saved


def _forget_uids(account_id: int, folder_id: int) -> None:
    """UIDVALIDITY changed: the stored UIDs of this folder mean nothing
    any more. Keep the rows (drafts, filing, categories hang on them) and
    let the next pass match them to the new UIDs by Message-ID."""
    with get_conn() as conn:
        conn.execute("UPDATE email_messages SET uid=NULL WHERE account_id=? AND folder_id=? AND uid > 0",
                     (account_id, folder_id))
        conn.execute("DELETE FROM email_fetch_failures WHERE account_id=? AND folder_id=?",
                     (account_id, folder_id))
        conn.commit()


# ───────────────────────── keeping Yorik equal to the server ─────────

def _scope_uids(c: IMAPClient, scope: str, is_inbox: bool, server_all: list) -> list:
    """The UIDs of the selected folder that Yorik should hold."""
    scope = (scope or "recent").strip().lower()
    if scope == "all":
        return server_all
    if scope.startswith("days:"):
        try:
            days = max(1, int(scope.split(":", 1)[1]))
        except ValueError:
            days = 90
        from datetime import date, timedelta
        since = date.today() - timedelta(days=days)
        return sorted(u for u in c.search(["SINCE", since, "UNDELETED"]) if u)
    n = RECENT_INBOX if is_inbox else RECENT_OTHER
    return server_all[-n:]


def _tombstoned_uids(c: IMAPClient, account_id: int, folder_id: int, uids: list) -> set:
    """Of these UIDs, the ones whose Message-ID the user deleted in Yorik
    — fetched headers only, so a deleted mail is not downloaded again on
    every pass."""
    with get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM email_deleted_message_ids WHERE account_id=?",
                         (account_id,)).fetchone()["n"]
    if not n or not uids:
        return set()
    out: set = set()
    for i in range(0, len(uids), 200):
        chunk = uids[i:i + 200]
        heads = c.fetch(chunk, [b"BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]"])
        mids = {}
        for uid, data in heads.items():
            raw = data.get(b"BODY[HEADER.FIELDS (MESSAGE-ID)]") or b""
            m = re.search(rb"<([^>]+)>", raw)
            if m:
                mids[uid] = m.group(1).decode("utf-8", "replace")
        if not mids:
            continue
        with get_conn() as conn:
            ph = ",".join("?" * len(mids))
            dead = {r["message_id"] for r in conn.execute(
                f"SELECT message_id FROM email_deleted_message_ids WHERE account_id=? "
                f"  AND message_id IN ({ph}) AND (suppress_folder_id IS NULL OR suppress_folder_id=?)",
                (account_id, *mids.values(), folder_id),
            ).fetchall()}
        out |= {u for u, mid in mids.items() if mid in dead}
    return out


def _reconcile_folder(c: IMAPClient, cfg: dict, folder: dict, budget: int) -> dict:
    """Make the selected folder in Yorik equal to the folder on the server,
    the way a mail program does: fetch what is missing (inside the import
    scope, newest first, at most `budget` mails), take over read / flagged
    from the server, and drop mails that are no longer on the server
    (deleted or moved by another program — a moved mail is fetched in
    its new folder). Returns counts for the progress display."""
    folder_id, name = folder["id"], folder["name"]
    out = {"missing": 0, "fetched": 0, "removed": 0, "flags": 0}
    st = c.folder_status(name, [b"UIDVALIDITY", b"MESSAGES", b"UNSEEN"])
    validity = int(st[b"UIDVALIDITY"])
    with get_conn() as conn:
        row = conn.execute("SELECT uid_validity FROM email_folders WHERE id=?", (folder_id,)).fetchone()
    if row and row["uid_validity"] is not None and int(row["uid_validity"]) != validity:
        # _sync_new_messages handles a new UIDVALIDITY; wait for it.
        return out
    # A mail another program marked \\Deleted counts as deleted, as it
    # does in Thunderbird.
    server_all = sorted(u for u in c.search(["UNDELETED"]) if u)
    messages = int(st.get(b"MESSAGES", 0) or 0)
    if not server_all and messages:
        log.warning("account %d %s: server says %d mails but SEARCH found none — no changes",
                    cfg["id"], name, messages)
        return out
    server_set = set(server_all)
    wanted = set(_scope_uids(c, cfg.get("import_scope") or "recent", _is_inbox(folder), server_all))

    with get_conn() as conn:
        local = {int(r["uid"]): r for r in conn.execute(
            "SELECT id, uid, is_unread, is_starred FROM email_messages "
            "WHERE account_id=? AND folder_id=? AND uid > 0",
            (cfg["id"], folder_id),
        ).fetchall()}
        failing = {int(r["uid"]) for r in conn.execute(
            "SELECT uid FROM email_fetch_failures WHERE account_id=? AND folder_id=?",
            (cfg["id"], folder_id),
        ).fetchall()}

    # 1. What is on the server, in scope, and not in Yorik.
    missing = sorted((wanted - set(local) - failing), reverse=True)
    if missing:
        skip = _tombstoned_uids(c, cfg["id"], folder_id, missing)
        with get_conn() as conn:
            is_collection = folder_id in _collection_folder_ids(conn, cfg["id"])
        if is_collection:
            skip |= _known_message_uids(c, cfg["id"], missing)
        missing = [u for u in missing if u not in skip]
    out["missing"] = len(missing)
    take = sorted(missing[:budget])
    if take:
        saved, _failed = _fetch_and_store(c, cfg, folder_id, take, quiet="auto")
        out["fetched"] = saved

    # 2. What is in Yorik and no longer on the server.
    gone = [uid for uid in local if uid not in server_set]
    if gone:
        # Ask again for exactly these: a cut-off SEARCH answer must never
        # make mails disappear from Yorik.
        still = set()
        for i in range(0, len(gone), 500):
            still |= set(c.search(["UID", ",".join(str(u) for u in gone[i:i + 500]), "UNDELETED"]))
        gone = [u for u in gone if u not in still]
    if gone:
        _drop_rows(cfg["id"], [int(local[u]["id"]) for u in gone])
        out["removed"] = len(gone)

    # 3. Read / flagged as the server has it.
    keep = sorted(u for u in local if u in server_set)
    changes = []
    for i in range(0, len(keep), 500):
        chunk = keep[i:i + 500]
        for uid, data in c.fetch(chunk, [b"FLAGS"]).items():
            flags = data.get(b"FLAGS", ())
            unread = 0 if b"\\Seen" in flags else 1
            starred = 1 if b"\\Flagged" in flags else 0
            r = local.get(uid)
            if r and (int(r["is_unread"] or 0) != unread or int(r["is_starred"] or 0) != starred):
                changes.append((unread, starred, int(r["id"])))
    if changes:
        with get_conn() as conn:
            for unread, starred, mid in changes:
                conn.execute("UPDATE email_messages SET is_unread=?, is_starred=? WHERE id=?",
                             (unread, starred, mid))
            conn.commit()
        out["flags"] = len(changes)

    with get_conn() as conn:
        conn.execute(
            "UPDATE email_folders SET message_count=?, unread_count=?, last_reconcile_at=datetime('now') WHERE id=?",
            (messages, int(st.get(b"UNSEEN", 0) or 0), folder_id),
        )
        conn.commit()
    return out


_COLLECTION_FLAGS = ("\\All", "\\Important", "\\Flagged")


def _collection_folder_ids(conn, account_id: int) -> tuple:
    """Folders that collect copies of mail kept elsewhere (Gmail's All
    Mail, Important, Starred), by their SPECIAL-USE flag."""
    rows = conn.execute("SELECT id, flags FROM email_folders WHERE account_id=?", (account_id,)).fetchall()
    out = []
    for r in rows:
        try:
            flags = json.loads(r["flags"] or "[]")
        except ValueError:
            flags = []
        if any(f in _COLLECTION_FLAGS for f in flags):
            out.append(int(r["id"]))
    return tuple(out)


def _known_message_uids(c: IMAPClient, account_id: int, uids: list) -> set:
    """Of these UIDs of the selected collection folder, the ones whose mail
    Yorik already holds in some folder — headers only."""
    out: set = set()
    for i in range(0, len(uids), 200):
        chunk = uids[i:i + 200]
        mids = {}
        for uid, data in c.fetch(chunk, [b"BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]"]).items():
            m = re.search(rb"<([^>]+)>", data.get(b"BODY[HEADER.FIELDS (MESSAGE-ID)]") or b"")
            if m:
                mids[uid] = m.group(1).decode("utf-8", "replace")
        if not mids:
            continue
        with get_conn() as conn:
            ph = ",".join("?" * len(mids))
            have = {r["message_id"] for r in conn.execute(
                f"SELECT message_id FROM email_messages WHERE account_id=? AND message_id IN ({ph})",
                (account_id, *mids.values()),
            ).fetchall()}
        out |= {u for u, mid in mids.items() if mid in have}
    return out


def _drop_rows(account_id: int, row_ids: list) -> None:
    """Remove mails from Yorik that are gone from the server. Only Yorik's
    copy: the server already let go of them."""
    if not row_ids:
        return
    from . import email_actions as _ea
    with get_conn() as conn:
        ph = ",".join("?" * len(row_ids))
        _ea.drop_attachment_files(row_ids)
        conn.execute(f"DELETE FROM email_attachments WHERE message_id IN ({ph})", tuple(row_ids))
        conn.execute(f"DELETE FROM email_messages WHERE account_id=? AND id IN ({ph})", (account_id, *row_ids))
        conn.commit()


def _is_inbox(folder: dict) -> bool:
    flags = folder.get("flags") or "[]"
    try:
        flag_list = json.loads(flags) if isinstance(flags, str) else list(flags)
    except Exception:  # noqa: BLE001
        flag_list = []
    return "\\Inbox" in flag_list or (folder.get("name") or "").upper() == "INBOX"


def reconcile_account(c: IMAPClient, cfg: dict, budget: int = PASS_BUDGET) -> dict:
    """One sync pass over every folder of a connected account. Returns
    totals; `pending` > 0 means the import is not finished and the next
    pass should come soon."""
    with get_conn() as conn:
        folders = [dict(r) for r in conn.execute(
            "SELECT id, name, flags FROM email_folders WHERE account_id=? ORDER BY id",
            (cfg["id"],),
        ).fetchall()]
    # The inbox first: it is what people look at.
    folders.sort(key=lambda f: 0 if _is_inbox(f) else 1)
    total = {"missing": 0, "fetched": 0, "removed": 0, "flags": 0}
    left = budget
    for f in folders:
        try:
            c.select_folder(f["name"], readonly=True)
        except Exception as e:  # noqa: BLE001
            log.info("account %d: cannot open %s for the sync pass: %s", cfg["id"], f["name"], e)
            continue
        try:
            _retry_failures(c, cfg, f["id"])
            r = _reconcile_folder(c, cfg, f, max(0, left))
        except IMAPClientError as e:
            log.info("account %d %s: sync pass skipped (%s)", cfg["id"], f["name"], e)
            continue
        left -= r["fetched"]
        for k in total:
            total[k] += r[k]
    with get_conn() as conn:
        parked = conn.execute(
            "SELECT COUNT(*) AS n FROM email_fetch_failures WHERE account_id=? AND parked=1",
            (cfg["id"],),
        ).fetchone()["n"]
        failing = conn.execute(
            "SELECT COUNT(*) AS n FROM email_fetch_failures WHERE account_id=? AND parked=0",
            (cfg["id"],),
        ).fetchone()["n"]
    pending = max(0, total["missing"] - total["fetched"])
    state = {"phase": "importing" if pending else "in_sync", "pending": pending,
             "fetched": total["fetched"], "removed": total["removed"], "flags": total["flags"],
             "failing": int(failing), "parked": int(parked),
             "last_run_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    with get_conn() as conn:
        conn.execute("UPDATE email_accounts SET sync_state=?, repair_requested=0 WHERE id=?",
                     (json.dumps(state), cfg["id"]))
        conn.commit()
    if total["fetched"] or total["removed"]:
        log.info("account %d sync pass: %s", cfg["id"], state)
    return state


def backfill_older(account_id: int, count: int = 200) -> dict:
    """Fetch up to `count` IMAP UIDs immediately BELOW the locally-known
    minimum UID for the account's INBOX. Used by the user-triggered
    "Load older messages" action — does NOT touch uid_next (so the
    forward delta-sync isn't affected). Returns a small status dict
    the endpoint can hand back to the UI.

    Synchronous + IMAP-blocking — runners should call it via
    asyncio.to_thread."""
    from . import email_actions as _ea
    # Resolve account's INBOX folder + current local min uid.
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id AS folder_id, name FROM email_folders "
            "WHERE account_id=? "
            "  AND (flags LIKE '%\\\\Inbox%' OR UPPER(name)='INBOX') "
            "ORDER BY id LIMIT 1",
            (account_id,),
        ).fetchone()
        if not row:
            return {"fetched": 0, "error": "no INBOX folder on this account"}
        folder_id = int(row["folder_id"])
        folder_name = row["name"]
        min_row = conn.execute(
            "SELECT MIN(uid) AS min_uid FROM email_messages "
            "WHERE account_id=? AND folder_id=?",
            (account_id, folder_id),
        ).fetchone()
    local_min = int(min_row["min_uid"] or 0)
    if local_min <= 1:
        return {"fetched": 0, "already_at_start": True, "min_uid": local_min}
    start = max(1, local_min - count)
    end = local_min - 1

    # Pull account cfg the same shape _sync_new_messages expects.
    with get_conn() as conn:
        cfg_row = conn.execute(
            "SELECT id, imap_host, imap_port, imap_ssl, imap_starttls, "
            "       imap_username, credential_key, owner_user_id "
            "FROM email_accounts WHERE id=?",
            (account_id,),
        ).fetchone()
    if not cfg_row:
        return {"fetched": 0, "error": "account not found"}
    cfg = dict(cfg_row)

    with _ea.imap_for_account(account_id) as c:
        try:
            c.select_folder(folder_name)
        except Exception as exc:
            return {"fetched": 0, "error": f"cannot select {folder_name}: {exc}"}
        n = _sync_new_messages(
            c, cfg, folder_id,
            force_start_uid=start, force_end_uid=end,
            freeze_uid_next=True, quiet=True,
        )
    log.info("account %d backfill_older: fetched %d msg(s) in UID range %d-%d",
             account_id, n, start, end)
    return {"fetched": n, "uid_range": [start, end], "previous_min_uid": local_min}


def _insert_message(cfg: dict, folder_id: int, uid: int,
                    data: dict, parsed: mailparser.MailParser,
                    quiet: bool = False) -> Optional[int]:
    """Persist one parsed message. Idempotent via (account_id, folder_id, uid).
    Returns the row id, or None when the user deleted this mail in Yorik
    (tombstone). Raises when the mail could not be saved — the caller
    records it as a failure and tries again; it is never skipped silently.

    `quiet` (imports, repairs, old mail): saved and classified, but no
    bell proposal, no draft, no suggestion, no contact capture and no
    automatic filing to Paperless."""
    from_email, from_name = "", ""
    if parsed.from_:
        # parsed.from_ = [(name, email), ...]
        name, addr = parsed.from_[0]
        from_email = (addr or "").lower()
        from_name = name or ""

    to_addrs    = json.dumps([{"name": n, "email": (a or "").lower()} for (n, a) in (parsed.to or [])])
    cc_addrs    = json.dumps([{"name": n, "email": (a or "").lower()} for (n, a) in (parsed.cc or [])])
    reply_to    = (parsed.reply_to[0][1] if parsed.reply_to else None) or None

    message_id = (parsed.message_id or "").strip("<>") or None
    in_reply_to = (parsed.in_reply_to or "").strip("<>") or None
    references_list = parsed.references or []
    refs_json = json.dumps([r.strip("<>") for r in references_list]) if references_list else None
    # Thread id: root of References chain, fall back to In-Reply-To, then Message-ID.
    thread_id = (references_list[0].strip("<>") if references_list
                  else in_reply_to or message_id)

    body_text = (parsed.text_plain or [""])[0] if parsed.text_plain else ""
    body_html = (parsed.text_html or [""])[0] if parsed.text_html else ""
    if not body_text and body_html:
        # Strip tags for a snippet — full text/html stripping is
        # better but for snippet purposes this is fine.
        body_text = re.sub(r"<[^>]+>", " ", body_html)
        body_text = re.sub(r"\s+", " ", body_text).strip()
    snippet = (body_text or "")[:SNIPPET_LEN].replace("\n", " ").strip()

    # a long subject arrives folded over several header lines; store it as the one line it is
    subject = re.sub(r"\s*[\r\n]+\s*", " ", parsed.subject or "").strip()
    has_attachments = 1 if parsed.attachments else 0
    size_bytes = data.get(b"RFC822.SIZE", 0)

    # IMAP flags → our booleans
    flags = data.get(b"FLAGS", ())
    is_unread = 0 if b"\\Seen" in flags else 1
    is_starred = 1 if b"\\Flagged" in flags else 0

    # List-Unsubscribe / List-Unsubscribe-Post (RFC 2369 + RFC 8058).
    # mailparser exposes raw headers as a {lower-name: value} mapping;
    # be defensive in case a future version changes shape.
    list_unsubscribe = None
    list_unsubscribe_post = None
    try:
        hdrs = parsed.headers or {}
        # Case-insensitive lookup — different mailparser versions
        # normalise the keys differently.
        for k, v in (hdrs.items() if hasattr(hdrs, "items") else hdrs):
            kl = (k or "").lower()
            if kl == "list-unsubscribe" and not list_unsubscribe:
                list_unsubscribe = v
            elif kl == "list-unsubscribe-post" and not list_unsubscribe_post:
                list_unsubscribe_post = v
    except Exception:
        # Headers missing or unparseable — just skip; absence is normal.
        pass

    # Dates
    date_sent = parsed.date.isoformat() if parsed.date else None
    date_received = None
    if data.get(b"INTERNALDATE"):
        try:
            date_received = data[b"INTERNALDATE"].astimezone(timezone.utc).isoformat()
        except Exception:
            pass

    inserted_id = None
    # Every document-shaped attachment lands here. After we know the
    # category + sender-trust the loop below decides which become Tier 1
    # (auto-file) vs Tier 2 (suggested, awaiting user click).
    doc_attachment_candidates: list[tuple[int, dict]] = []
    with get_conn() as conn:
        # Tombstone gate: if the user already deleted this message,
        # skip the insert under any folder the tombstone is scoped to.
        # See migrations 043 (the table) + 044 (suppress_folder_id).
        #
        # Scope semantics:
        #   suppress_folder_id IS NULL    → block in ANY folder
        #   suppress_folder_id = folder_id → block only in that folder
        #                                    (typically the source
        #                                    folder before delete;
        #                                    allows the Trash copy
        #                                    that delete_message
        #                                    created to surface on
        #                                    this tick)
        #
        if message_id:
            tomb = conn.execute(
                "SELECT 1 FROM email_deleted_message_ids "
                "WHERE account_id=? AND message_id=? "
                "  AND (suppress_folder_id IS NULL "
                "       OR suppress_folder_id = ?)",
                (cfg["id"], message_id, folder_id),
            ).fetchone()
            if tomb:
                log.debug("tombstoned message %s on account %s folder %s — skipping insert",
                          message_id[:60], cfg["id"], folder_id)
                return None
        try:
            # Is this row landing in the IMAP Sent folder? The /messages
            # endpoint splits inbox vs sent on the `is_sent` column, not
            # on folder membership, so a message pulled by the fetcher
            # from a \Sent-flagged folder MUST get is_sent=1 — otherwise
            # it shows up in the user's inbox view.
            folder_is_sent = 0
            try:
                frow = conn.execute(
                    "SELECT flags, name FROM email_folders WHERE id=?",
                    (folder_id,),
                ).fetchone()
                if frow and frow["flags"]:
                    try:
                        flag_list = json.loads(frow["flags"]) or []
                    except Exception:  # noqa: BLE001
                        flag_list = []
                    if any("\\Sent" in str(f) for f in flag_list):
                        folder_is_sent = 1
                # Servers without SPECIAL-USE flags: the usual names.
                if frow and (frow["name"] or "").strip().lower() in _SENT_NAMES:
                    folder_is_sent = 1
            except Exception:  # noqa: BLE001
                folder_is_sent = 0
            # Dedup on message_id, for the rows that are a placeholder for
            # this very copy: the local mirror of a sent mail (negative
            # uid, written before the server had it), a row Yorik moved
            # into this folder (uid NULL until the server's copy is seen),
            # or the same uid again. A copy of the mail in ANOTHER folder
            # is a mail of its own, as in any mail program — matching it
            # made the one row hop between folders on every pass, and the
            # mail vanished from the folder it had just left.
            existing_id: Optional[int] = None
            existing_is_sent: int = 0
            existing = None
            collections = _collection_folder_ids(conn, cfg["id"])
            if message_id and folder_id in collections:
                # Gmail's "All Mail" / "Important" / "Starred" hold copies
                # of mail that lives in a real folder. Known mail is not
                # a new mail here — one row per mail, as before.
                known = conn.execute(
                    "SELECT id FROM email_messages WHERE account_id=? AND message_id=? LIMIT 1",
                    (cfg["id"], message_id),
                ).fetchone()
                if known:
                    return int(known["id"])
            if message_id:
                ph = ",".join("?" * len(collections)) or "NULL"
                existing = conn.execute(
                    "SELECT id, is_sent FROM email_messages "
                    "WHERE account_id=? AND message_id=? "
                    "  AND (uid < 0 OR (folder_id=? AND (uid IS NULL OR uid=?)) "
                    f"      OR folder_id IN ({ph})) "
                    "ORDER BY (folder_id=?) DESC, id LIMIT 1",
                    (cfg["id"], message_id, folder_id, uid, *collections, folder_id),
                ).fetchone()
            if existing is None:
                same = conn.execute(
                    "SELECT id FROM email_messages WHERE account_id=? AND folder_id=? AND uid=?",
                    (cfg["id"], folder_id, uid),
                ).fetchone()
                if same:
                    return int(same["id"])       # this copy is already stored
            if existing:
                existing_id = int(existing["id"])
                existing_is_sent = int(existing["is_sent"] or 0)
            if existing_id is not None:
                # Preserve is_sent=1 from any prior local mirror; lift it
                # to 1 if the new folder is a Sent folder. Never lower
                # 1 → 0.
                new_is_sent = 1 if (existing_is_sent or folder_is_sent) else 0
                conn.execute(
                    "UPDATE email_messages SET "
                    "  folder_id=?, uid=?, in_reply_to=?, references_ids=?, "
                    "  thread_id=?, from_email=?, from_name=?, to_addrs=?, cc_addrs=?, "
                    "  reply_to=?, subject=?, snippet=?, body_text=?, body_html=?, "
                    "  date_sent=?, date_received=?, size_bytes=?, is_unread=?, "
                    "  is_starred=?, has_attachments=?, is_sent=?, "
                    "  list_unsubscribe=?, list_unsubscribe_post=? "
                    "WHERE id=?",
                    (folder_id, uid, in_reply_to, refs_json,
                     thread_id, from_email, from_name, to_addrs, cc_addrs, reply_to,
                     subject, snippet, body_text, body_html, date_sent, date_received,
                     size_bytes, is_unread, is_starred, has_attachments, new_is_sent,
                     list_unsubscribe, list_unsubscribe_post,
                     existing_id),
                )
                msg_id = existing_id
                log.debug("dedup: updated existing email_messages row id=%s for message_id=%s",
                          existing_id, (message_id or "")[:60])
                # A placeholder that now has its server copy: its
                # attachments, classification and notices happened when
                # it was first written. Nothing more to do.
                conn.commit()
                return msg_id
            else:
                cur = conn.execute(
                    "INSERT INTO email_messages "
                    "(account_id, folder_id, uid, message_id, in_reply_to, references_ids, "
                    " thread_id, from_email, from_name, to_addrs, cc_addrs, reply_to, "
                    " subject, snippet, body_text, body_html, date_sent, date_received, "
                    " size_bytes, is_unread, is_starred, has_attachments, is_sent, "
                    " owner_user_id, list_unsubscribe, list_unsubscribe_post) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (cfg["id"], folder_id, uid, message_id, in_reply_to, refs_json,
                     thread_id, from_email, from_name, to_addrs, cc_addrs, reply_to,
                     subject, snippet, body_text, body_html, date_sent, date_received,
                     size_bytes, is_unread, is_starred, has_attachments, folder_is_sent,
                     cfg["owner_user_id"],
                     list_unsubscribe, list_unsubscribe_post),
                )
                msg_id = cur.lastrowid
            inserted_id = msg_id
            # Attachment metadata only — actual binary lazy-fetched on
            # demand later. Saves disk for the 90% of attachments
            # nobody opens. EXCEPTION: documents (PDF / Office) get
            # auto-routed to Paperless below, which DOES need the
            # binary — we have it in `att` already so collect them.
            # Cap on auto-routing huge attachments to Paperless. The
            # metadata row is recorded regardless (so the UI still
            # shows "this email had a 50MB attachment named …"), but
            # the binary itself stays only in IMAP. Configurable via
            # env so an estate running a high-attachment workflow can
            # raise it deliberately. Catches the OOM-via-malicious-
            # giant-PDF class the security audit flagged.
            max_bytes = int(os.getenv("YORIK_MAX_IMAP_ATTACHMENT_MB", "25")) * 1024 * 1024
            # mailparser occasionally emits the same MIME part twice for
            # messages that wrap an attachment in BOTH multipart/mixed
            # AND multipart/related (netcup invoices, Web.de Terms emails,
            # some signed mail). Both copies arrive with identical
            # filename + mimetype + empty content_id. For inline images
            # (each with a distinct CID, same generic filename "inline")
            # this key keeps them apart correctly.
            seen_att_keys: set[tuple[str, str, str]] = set()
            for att in (parsed.attachments or []):
                payload = att.get("payload")
                size = len(payload) if isinstance(payload, (bytes, bytearray)) else 0
                mt = (att.get("mail_content_type") or "").lower()
                fn = att.get("filename") or ""
                cd = att.get("content-disposition") or ""
                cid = att.get("content-id") or ""
                dedup_key = (fn, mt, cid)
                if dedup_key in seen_att_keys:
                    log.debug("skip duplicate attachment in msg %s: %s (mt=%s cid=%r)",
                              msg_id, fn or "<no-name>", mt, cid)
                    continue
                seen_att_keys.add(dedup_key)
                acur = conn.execute(
                    "INSERT INTO email_attachments (message_id, filename, mimetype, size_bytes, content_id, is_inline) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (msg_id, fn, mt, size, cid or None,
                     1 if cd.startswith("inline") else 0),
                )
                att_id = acur.lastrowid
                if size > max_bytes:
                    log.info("attachment %s on msg %s exceeds %dMB cap (size=%dMB) — "
                             "skipping Paperless auto-route; metadata kept",
                             fn or "<no-name>", msg_id, max_bytes // (1024*1024),
                             size // (1024*1024))
                    continue
                if _is_paperless_route(mt, fn) and isinstance(payload, (bytes, bytearray)) and payload:
                    doc_attachment_candidates.append((att_id, {
                        "bytes": bytes(payload),
                        "filename": fn or f"email-att-{att_id}.pdf",
                        "mimetype": mt or "application/pdf",
                    }))
            conn.commit()
        except Exception as e:
            # A race between IDLE and catch-up inserting the same uid is
            # harmless; anything else means the mail is not in Yorik and
            # must be retried — raise, the caller records it.
            text = str(e)
            if "UNIQUE constraint" in text or "duplicate key" in text:
                log.debug("msg uid %s already stored: %s", uid, text[:120])
                return None
            raise

    # Classify the message (bill / appointment / newsletter / …) so the
    # email list can show a colored badge and downstream features (e.g.
    # "add to bills?" prompt) can act on it. Heuristic-only — cheap and
    # synchronous, no LLM call. The returned category also drives the
    # Tier 1 vs Tier 2 Paperless decision below.
    message_category: Optional[str] = None
    if inserted_id:
        try:
            from . import email_classifier
            message_category = email_classifier.apply_to_message(inserted_id, quiet=quiet)
        except Exception as e:
            log.debug("classify msg %s failed: %s", inserted_id, e)

    # Contacts autocapture — drop unknown senders into the Pending tab
    # of /r/contacts (skipping no-reply/billing etc.), bump
    # last_interaction for known senders, and override category=spam
    # when the sender is on the spam list. Always safe to call — the
    # autocapture module never raises.
    autocapture_category: Optional[str] = None
    if inserted_id and not quiet:
        from . import contact_autocapture
        autocapture_category = contact_autocapture.on_inbound_email(
            from_email=from_email, from_name=from_name, message_id=inserted_id,
        )
        if autocapture_category == "spam":
            try:
                with get_conn() as _c:
                    _c.execute(
                        "UPDATE email_messages SET category = ? WHERE id = ?",
                        ("spam", inserted_id),
                    )
                    _c.commit()
            except Exception as e:
                log.debug("mark-spam category write failed for msg %s: %s",
                          inserted_id, e)

    # Schedule an auto-draft for this incoming message. Runs in the
    # asyncio loop (not the IMAP thread) so the LLM call doesn't
    # block IMAP. Skipped for sent/no-reply/list traffic AND for
    # senders the user has marked as spam.
    if (inserted_id and not quiet
            and autocapture_category != "spam"
            and _should_autodraft(from_email, subject, to_addrs)):
        try:
            from . import email_autodraft
            email_autodraft.schedule_for_message(
                cfg["owner_user_id"], inserted_id, cfg["id"])
        except Exception as e:
            log.debug("autodraft schedule failed: %s", e)

    # Suggestion engine — fire the email.new trigger. Toggle hierarchy
    # (master + per-source + per-contact opt-in) is enforced inside
    # engine.analyse_message; this hook always fires so disabling the
    # engine takes effect immediately without a fetcher restart. Same
    # spam skip as autodraft — no value generating suggestions for
    # senders the user has already rejected.
    #
    # IMPORTANT: this function runs inside asyncio.to_thread, so
    # asyncio.get_event_loop() from here raises RuntimeError. Use the
    # main loop the FastAPI startup hook captured into
    # backend.suggestions.get_main_loop() instead.
    if inserted_id and not quiet and autocapture_category != "spam":
        try:
            from . import suggestions as _suggestions_pkg
            from .suggestions.triggers import email_new as _email_new_trig
            _loop = _suggestions_pkg.get_main_loop()
            if _loop is not None:
                _email_new_trig.fire_from_thread(
                    _loop, cfg["owner_user_id"], inserted_id)
            else:
                log.warning("suggestions trigger: main loop not captured yet — skipping msg %s",
                            inserted_id)
        except Exception as e:
            log.warning("suggestions trigger failed for msg %s: %s", inserted_id, e)

    # Tier 1 vs Tier 2 routing for document attachments. The old code
    # auto-uploaded every PDF/DOCX/XLSX unconditionally, which turned
    # Paperless into a dumping ground for advertising and notification
    # PDFs. Now only emails that meet BOTH tests get the auto-file:
    #
    #   * sender is in the user's contacts with status='active' (i.e.
    #     someone they actually know, not auto-captured "pending"); AND
    #   * classifier says the message is transactional (bill or
    #     appointment) — the categories with the highest signal-to-noise
    #     ratio for "this is a document the user wants to keep".
    #
    # Anything that fails either test is still recorded as a candidate
    # (paperless_state='suggested') so the email UI can show a one-click
    # "File to Paperless" pill. Discarded suggestions stay flagged so
    # the prompt doesn't reappear.
    if doc_attachment_candidates:
        try:
            from . import contacts as _contacts
            contact = _contacts.find_by_channel("email", from_email or "")
        except Exception:
            contact = None
        contact_active = bool(contact and contact.get("status") == "active")
        tier1_categories = {"bill", "appointment"}
        auto_eligible = (
            not quiet
            and contact_active
            and message_category in tier1_categories
            and autocapture_category != "spam"
        )
        for att_id, blob in doc_attachment_candidates:
            if auto_eligible:
                try:
                    _file_to_paperless(
                        att_id, blob, cfg["owner_user_id"],
                        from_name or from_email, subject,
                        new_state="auto_filed",
                    )
                except Exception as e:
                    log.warning("email att %d → Paperless failed: %s", att_id, e)
            else:
                try:
                    with get_conn() as _c:
                        _c.execute(
                            "UPDATE email_attachments SET paperless_state='suggested' WHERE id=?",
                            (att_id,),
                        )
                        _c.commit()
                except Exception as e:
                    log.debug("mark suggested failed for att %s: %s", att_id, e)
    return inserted_id


_PAPERLESS_MIMES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/rtf",
    "text/csv",
}


def _is_paperless_route(mimetype: str, filename: str) -> bool:
    if mimetype in _PAPERLESS_MIMES:
        return True
    fn = (filename or "").lower()
    return fn.endswith((".pdf", ".docx", ".doc", ".xlsx", ".xls", ".rtf", ".csv"))


VISIBILITY_LEVELS = ("private", "parents", "business", "shared")


def _file_to_paperless(att_id: int, blob: dict, user_id: str,
                        sender_label: str, subject: str,
                        new_state: str = "auto_filed",
                        visibility: Optional[str] = None) -> bool:
    """Upload one attachment to Paperless using the per-user token.
    On success, marks the attachment row with paperless_id=0 (sentinel
    meaning "uploaded, real doc id will be backfilled by webhook") and
    paperless_state=new_state ('auto_filed' for Tier 1 fetcher path,
    'filed' for Tier 2 user-confirm). On any failure, sets
    paperless_state='failed' so the UI can offer a retry.

    `visibility` says who may see the document in Paperless (the chat
    card's "nur mich / die Eltern / die Familie"); the user answers it
    on Tier 2, Tier 1 takes the person's default. It is applied to the
    document once Paperless has consumed it, as the chat does.
    Returns True iff the upload succeeded."""
    from .external_users import get_user_paperless_creds
    creds = get_user_paperless_creds(user_id)
    if not creds or not creds.get("api_key"):
        return False  # Paperless not configured for this user — leave state untouched
    import requests as _rq
    vis = (visibility or "").strip().lower()
    if vis not in VISIBILITY_LEVELS:
        from .chat_attachments import _default_visibility
        vis = _default_visibility(user_id)
    headers = {"Authorization": f"Token {creds['api_key']}"}
    files = {"document": (blob["filename"], blob["bytes"], blob["mimetype"])}
    title = f"Email · {sender_label} · {subject[:60] if subject else blob['filename']}"
    data = {"title": title}
    from . import paperless_visibility as _pv
    _pv.apply_visibility_to_payload(vis, data)
    failure_reason: Optional[str] = None
    task_id: Optional[str] = None
    try:
        r = _rq.post(f"{creds['base_url'].rstrip('/')}/api/documents/post_document/",
                      headers=headers, files=files, data=data, timeout=30)
        if not r.ok:
            log.warning("Paperless upload returned %d: %s", r.status_code, r.text[:200])
            failure_reason = f"HTTP {r.status_code}"
        else:
            task_id = r.text.strip().strip('"')
    except _rq.RequestException as e:
        log.warning("Paperless POST failed: %s", e)
        failure_reason = str(e)
    with get_conn() as conn:
        if failure_reason is None:
            conn.execute(
                "UPDATE email_attachments "
                "SET paperless_id=0, paperless_state=?, paperless_task_id=?, paperless_visibility=? "
                "WHERE id=?",
                (new_state, task_id, vis, att_id),
            )
        else:
            conn.execute(
                "UPDATE email_attachments SET paperless_state='failed' WHERE id=?",
                (att_id,),
            )
        conn.commit()
    if failure_reason is None:
        # The tag alone lets nobody open the document; the group's view
        # permission does, and that can only be set once it exists.
        if vis != "private" and task_id:
            _pv.apply_after_consume(task_id, vis)
        log.info("email att %d → Paperless (task=%s, state=%s, %s) from %s",
                 att_id, task_id, new_state, vis, sender_label)
        return True
    return False


def _should_autodraft(from_email: str, subject: str, to_addrs_json: str) -> bool:
    """Skip mailing lists, no-reply senders, promo / notification mail.
    These mass-produce drafts the user will never use and waste tokens."""
    fe = (from_email or "").lower()
    if any(p in fe for p in ("no-reply", "noreply", "donotreply", "notification", "notifications@",
                              "newsletter", "mailer-daemon", "bounce")):
        return False
    sub = (subject or "").lower()
    if any(p in sub for p in ("[newsletter]", "[promotion]", "unsubscribe",
                                "bestätigen sie", "verify your")):
        return False
    return True


# ───────────────────────── helpers ──────────────────────────────────

def _upsert_folder(account_id: int, name: str, c: IMAPClient) -> int:
    """Get or create a folder row, return its id. Used by the simple
    legacy path; _enumerate_folders is the bulk variant."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM email_folders WHERE account_id=? AND name=?",
            (account_id, name),
        ).fetchone()
        if row:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO email_folders (account_id, name, display_name) VALUES (?, ?, ?)",
            (account_id, name, _prettify_folder_name(name)),
        )
        conn.commit()
        return cur.lastrowid


def _enumerate_folders(c: IMAPClient, account_id: int) -> None:
    """List every folder the server exposes, upsert into email_folders
    with SPECIAL-USE flags persisted as JSON. Idempotent; subsequent
    runs UPDATE the flags and display_name in case the server's
    metadata changed."""
    try:
        listing = c.list_folders()
    except Exception as e:
        log.warning("account %d folder list failed: %s", account_id, e)
        return
    seen_names: set[str] = set()
    with get_conn() as conn:
        for flags, _delim, name in listing:
            # Skip non-selectable container folders ("[Gmail]" parent
            # itself, etc.) — listed but can't hold messages.
            if b"\\Noselect" in flags or b"\\NonExistent" in flags:
                continue
            seen_names.add(name)
            flags_json = json.dumps([f.decode("utf-8") if isinstance(f, bytes) else str(f)
                                      for f in flags])
            display = _prettify_folder_name(name, flags)
            existing = conn.execute(
                "SELECT id FROM email_folders WHERE account_id=? AND name=?",
                (account_id, name),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE email_folders SET flags=?, display_name=? WHERE id=?",
                    (flags_json, display, existing["id"]),
                )
            else:
                conn.execute(
                    "INSERT INTO email_folders (account_id, name, display_name, flags) "
                    "VALUES (?, ?, ?, ?)",
                    (account_id, name, display, flags_json),
                )
        conn.commit()


# Map of SPECIAL-USE flag → pretty display name. Falls back to the
# raw IMAP folder name (with the "INBOX" → "Inbox" tweak) if no flag.
_SPECIAL_DISPLAY = {
    b"\\Inbox":   "Inbox",
    b"\\Sent":    "Sent",
    b"\\Drafts":  "Drafts",
    b"\\Trash":   "Trash",
    b"\\Junk":    "Spam",
    b"\\Archive": "Archive",
    b"\\All":     "All Mail",
    b"\\Flagged": "Starred",
}


def _prettify_folder_name(raw: str, flags=()) -> str:
    """Folder name for the sidebar. Prefers SPECIAL-USE when present
    (so 'Gesendet' in German GMX shows as 'Sent'), falls back to a
    cleaned raw name. Strips Gmail's '[Gmail]/' prefix."""
    for flag, label in _SPECIAL_DISPLAY.items():
        if flag in flags:
            return label
    if raw == "INBOX":
        return "Inbox"
    # Strip Gmail-style container prefix.
    if raw.startswith("[Gmail]/"):
        return raw[len("[Gmail]/"):]
    if "/" in raw:
        return raw.rsplit("/", 1)[1]
    return raw


def _folder_name(folder_id: int) -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT name FROM email_folders WHERE id=?", (folder_id,)).fetchone()
    return row["name"] if row else "INBOX"


def _load_enabled_account_ids() -> list[int]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM email_accounts WHERE enabled=1 ORDER BY id"
        ).fetchall()
    return [r["id"] for r in rows]


def _load_account_config(account_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, owner_user_id, email, display_name, "
            "       imap_host, imap_port, imap_ssl, imap_starttls, imap_username, "
            "       smtp_host, smtp_port, smtp_ssl, smtp_starttls, smtp_username, "
            "       credential_key, enabled, is_default, import_scope, sync_state, repair_requested "
            "FROM email_accounts WHERE id=?",
            (account_id,),
        ).fetchone()
    return dict(row) if row else None


def _record_account_sync(account_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE email_accounts SET last_sync_at=datetime('now'), last_error=NULL, last_error_at=NULL "
            "WHERE id=?",
            (account_id,),
        )
        conn.commit()


def _record_account_error(account_id: int, message: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE email_accounts SET last_error=?, last_error_at=datetime('now') WHERE id=?",
            (message[:500], account_id),
        )
        conn.commit()


# ───────────────────────── manual sync trigger ──────────────────────

async def trigger_sync(account_id: int) -> dict:
    """Force an immediate sync of one account (used by the UI's
    "Sync now" button). Returns {ok, new_messages, error?}."""
    cfg = _load_account_config(account_id)
    if not cfg:
        return {"ok": False, "error": "account not found"}
    try:
        # Just kick the supervisor to (re)start the loop — the loop
        # naturally syncs on connect. For a TRUE "do it now" we'd
        # need a separate one-shot sync, but the loop pattern is
        # robust and the user perceives it as instant.
        await reload_account(account_id)
        return {"ok": True, "kicked": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}
