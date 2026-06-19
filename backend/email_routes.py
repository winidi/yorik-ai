"""Email REST endpoints: account CRUD, message browse, send."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from typing import Any, Optional

log = logging.getLogger("yorik.email_routes")

from fastapi import APIRouter, Body, Depends, HTTPException, Query, UploadFile, File
from pydantic import BaseModel, Field

from fastapi.responses import Response
from . import credential_store
from .auth_sessions import current_user
from .database import get_conn
from .email_providers import generic_preset, lookup_provider
from .email_sender import send, test_imap, test_smtp
from . import email_actions

router = APIRouter(prefix="/api/email", tags=["email"])


# ──────────────────────────── models ────────────────────────────────

class ProviderProbe(BaseModel):
    email: str


class AccountCreate(BaseModel):
    email: str
    display_name: Optional[str] = None
    password: str  # used for both IMAP and SMTP unless they differ
    smtp_password: Optional[str] = None  # explicit override
    imap_host: str
    imap_port: int = 993
    imap_ssl: bool = True
    imap_starttls: bool = False     # mutually exclusive with imap_ssl; for STARTTLS hosts (Proton Bridge 1143, generic port 143)
    imap_username: Optional[str] = None  # defaults to email
    smtp_host: str
    smtp_port: int = 465
    smtp_ssl: bool = True
    smtp_starttls: bool = False
    smtp_username: Optional[str] = None
    is_default: bool = False


class AccountUpdate(BaseModel):
    display_name: Optional[str] = None
    enabled: Optional[bool] = None
    is_default: Optional[bool] = None
    password: Optional[str] = None     # if set, replaces IMAP+SMTP password
    smtp_password: Optional[str] = None


class SendAttachmentIn(BaseModel):
    filename: str
    mimetype: Optional[str] = None
    # Base64-encoded bytes. Cap enforced server-side so a runaway
    # composer can't try to pump 100MB through the request.
    content_b64: str


class SendBody(BaseModel):
    account_id: int
    to: list[str] = Field(..., min_length=1)
    cc: list[str] = Field(default_factory=list)
    bcc: list[str] = Field(default_factory=list)
    subject: str = ""
    body_text: str = ""
    body_html: Optional[str] = None
    in_reply_to: Optional[str] = None
    references: list[str] = Field(default_factory=list)
    draft_id: Optional[int] = None  # if this send came from an auto-draft variant
    # Inline attachments uploaded with the send call. The composer
    # base64-encodes files on drop; the backend decodes here.
    attachments: list[SendAttachmentIn] = Field(default_factory=list)


# ──────────────────────────── helpers ───────────────────────────────

def _account_row_to_dict(r) -> dict:
    d = dict(r)
    d["enabled"] = bool(d.get("enabled"))
    d["is_default"] = bool(d.get("is_default"))
    d["imap_ssl"] = bool(d.get("imap_ssl"))
    d["imap_starttls"] = bool(d.get("imap_starttls"))
    d["smtp_ssl"] = bool(d.get("smtp_ssl"))
    d["smtp_starttls"] = bool(d.get("smtp_starttls"))
    d.pop("credential_key", None)  # don't leak
    return d


# ──────────────────────────── provider probe ────────────────────────

@router.post("/providers/probe")
def probe(body: ProviderProbe):
    """Suggest provider config for an email address. Used by the
    Add Account wizard to pre-fill hosts."""
    preset = lookup_provider(body.email) or generic_preset()
    return {"preset": preset, "email": body.email}


# ──────────────────────────── account CRUD ──────────────────────────

@router.get("/accounts")
def list_accounts(user: dict = Depends(current_user)):
    """All accounts the current user owns. Admin sees own only (this
    is per-user data, even admin shouldn't see Anna's mailbox)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, owner_user_id, email, display_name, "
            "       imap_host, imap_port, imap_ssl, imap_starttls, imap_username, "
            "       smtp_host, smtp_port, smtp_ssl, smtp_starttls, smtp_username, "
            "       enabled, is_default, last_sync_at, last_error, last_error_at, created_at "
            "FROM email_accounts WHERE owner_user_id=? ORDER BY id",
            (user["id"],),
        ).fetchall()
    return [_account_row_to_dict(r) for r in rows]


@router.post("/accounts", status_code=201)
async def create_account(body: AccountCreate, user: dict = Depends(current_user)):
    """Add a new email account for the current user. Tests both IMAP
    and SMTP login before persisting — fails fast if creds are wrong
    instead of silently storing bad config."""
    imap_user = body.imap_username or body.email
    smtp_user = body.smtp_username or body.email
    smtp_pw = body.smtp_password or body.password

    # Test BOTH connections before saving. Better to surface auth/host
    # errors now than discover them on the next sync cycle.
    t1 = await asyncio.to_thread(test_imap, body.imap_host, body.imap_port,
                                  body.imap_ssl, imap_user, body.password,
                                  body.imap_starttls)
    if not t1["ok"]:
        raise HTTPException(400, f"IMAP test failed: {t1['error']}")
    t2 = await asyncio.to_thread(test_smtp, body.smtp_host, body.smtp_port,
                                  body.smtp_ssl, body.smtp_starttls, smtp_user, smtp_pw)
    if not t2["ok"]:
        raise HTTPException(400, f"SMTP test failed: {t2['error']}")

    cred_key = f"email:{secrets.token_urlsafe(12)}"
    credential_store.put(cred_key, {
        "imap_password": body.password,
        "smtp_password": smtp_pw,
    })

    with get_conn() as conn:
        # Enforce single default: if this is being set default, unset others.
        if body.is_default:
            conn.execute("UPDATE email_accounts SET is_default=0 WHERE owner_user_id=?",
                          (user["id"],))
        try:
            cur = conn.execute(
                "INSERT INTO email_accounts "
                "(owner_user_id, email, display_name, "
                " imap_host, imap_port, imap_ssl, imap_starttls, imap_username, "
                " smtp_host, smtp_port, smtp_ssl, smtp_starttls, smtp_username, "
                " credential_key, is_default) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user["id"], body.email, body.display_name,
                 body.imap_host, body.imap_port, 1 if body.imap_ssl else 0,
                 1 if body.imap_starttls else 0, imap_user,
                 body.smtp_host, body.smtp_port, 1 if body.smtp_ssl else 0,
                 1 if body.smtp_starttls else 0, smtp_user,
                 cred_key, 1 if body.is_default else 0),
            )
            aid = cur.lastrowid
        except Exception as e:
            credential_store.delete(cred_key)
            if "UNIQUE constraint" in str(e):
                raise HTTPException(409, "you already have an account with this email address")
            raise HTTPException(500, f"DB insert failed: {e}")
        conn.commit()
        row = conn.execute(
            "SELECT * FROM email_accounts WHERE id=?", (aid,)
        ).fetchone()

    # Kick the fetcher to start syncing this new account immediately.
    from . import email_fetcher
    try:
        await email_fetcher.reload_account(aid)
    except Exception as e:
        # Account is persisted; failed reload just means it'll start
        # on next supervisor cycle / restart.
        pass

    return _account_row_to_dict(row)


@router.patch("/accounts/{account_id}")
async def update_account(account_id: int, body: AccountUpdate,
                          user: dict = Depends(current_user)):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, owner_user_id, credential_key FROM email_accounts WHERE id=?",
            (account_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "account not found")
        if row["owner_user_id"] != user["id"]:
            raise HTTPException(403, "not your account")

        fields, params = [], []
        if body.display_name is not None: fields.append("display_name=?"); params.append(body.display_name)
        if body.enabled is not None:      fields.append("enabled=?");      params.append(1 if body.enabled else 0)
        if body.is_default is not None:
            if body.is_default:
                conn.execute("UPDATE email_accounts SET is_default=0 WHERE owner_user_id=?",
                              (user["id"],))
            fields.append("is_default=?"); params.append(1 if body.is_default else 0)
        if fields:
            params.append(account_id)
            conn.execute(f"UPDATE email_accounts SET {', '.join(fields)} WHERE id=?", params)

        # Password rotation if provided.
        if body.password is not None or body.smtp_password is not None:
            existing = credential_store.get(row["credential_key"]) or {}
            new_creds = dict(existing)
            if body.password is not None:
                new_creds["imap_password"] = body.password
                new_creds.setdefault("smtp_password", body.password)
            if body.smtp_password is not None:
                new_creds["smtp_password"] = body.smtp_password
            credential_store.put(row["credential_key"], new_creds)
        conn.commit()

    from . import email_fetcher
    await email_fetcher.reload_account(account_id)
    return {"ok": True}


@router.delete("/accounts/{account_id}")
async def delete_account(account_id: int, user: dict = Depends(current_user)):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT owner_user_id, credential_key FROM email_accounts WHERE id=?",
            (account_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "account not found")
        if row["owner_user_id"] != user["id"]:
            raise HTTPException(403, "not your account")
        conn.execute("DELETE FROM email_accounts WHERE id=?", (account_id,))
        conn.commit()
    if row["credential_key"]:
        credential_store.delete(row["credential_key"])
    from . import email_fetcher
    await email_fetcher.reload_account(account_id)  # noop — config returns None
    return {"ok": True}


@router.get("/inbox-summary")
def inbox_summary(user: dict = Depends(current_user)) -> dict:
    """Aggregate counts for the "All inboxes" sidebar badge.

    Semantic inbox = every message across the user's accounts where
    is_sent=0. Matches the filter the /messages endpoint applies for
    folder='inbox' — so the badge always equals what the user would
    see when they click the link.
    """
    # Same exclusion as the semantic-inbox filter on /messages so the
    # badge equals what the user sees on click. Trash / Junk / Drafts
    # folders are deliberately excluded.
    not_in_buckets = (
        " AND m.folder_id NOT IN ("
        "   SELECT id FROM email_folders WHERE"
        "     flags LIKE '%\\\\Trash%'"
        "  OR flags LIKE '%\\\\Junk%'"
        "  OR flags LIKE '%\\\\Drafts%'"
        " )"
    )
    with get_conn() as conn:
        unread = conn.execute(
            "SELECT COUNT(*) FROM email_messages m "
            "JOIN email_accounts a ON a.id = m.account_id "
            "WHERE a.owner_user_id = ? AND m.is_sent = 0 AND m.is_unread = 1"
            + not_in_buckets,
            (user["id"],),
        ).fetchone()[0]
        total = conn.execute(
            "SELECT COUNT(*) FROM email_messages m "
            "JOIN email_accounts a ON a.id = m.account_id "
            "WHERE a.owner_user_id = ? AND m.is_sent = 0"
            + not_in_buckets,
            (user["id"],),
        ).fetchone()[0]
    return {"unread": int(unread), "total": int(total)}


@router.get("/accounts/{account_id}/folders")
def list_account_folders(account_id: int, user: dict = Depends(current_user)):
    """Folders we've discovered on this account, with unread + total
    message counts pulled live from email_messages. Driven by the
    sidebar's per-account folder list."""
    with get_conn() as conn:
        # Ownership check.
        row = conn.execute(
            "SELECT owner_user_id FROM email_accounts WHERE id=?", (account_id,)
        ).fetchone()
        if not row or row["owner_user_id"] != user["id"]:
            raise HTTPException(404, "account not found")
        rows = conn.execute(
            "SELECT f.id, f.name, f.display_name, f.flags, "
            "       (SELECT COUNT(*) FROM email_messages m "
            "        WHERE m.folder_id=f.id) AS total, "
            "       (SELECT COUNT(*) FROM email_messages m "
            "        WHERE m.folder_id=f.id AND m.is_unread=1) AS unread "
            "FROM email_folders f WHERE f.account_id=? "
            "ORDER BY f.id ASC",
            (account_id,),
        ).fetchall()
    out = []
    semantic_inbox_unread: Optional[int] = None
    semantic_inbox_total:  Optional[int] = None
    for r in rows:
        d = dict(r)
        try:
            d["flags"] = json.loads(d["flags"] or "[]")
        except json.JSONDecodeError:
            d["flags"] = []
        # Derive a category for the sidebar icon.
        d["category"] = _folder_category(d["flags"], d["name"])
        # On Gmail / Proton Bridge accounts the strict INBOX folder is
        # empty (inbound mail lives in "All Mail"), so the badge that
        # the sidebar paints from `d["unread"]` would read 0 even when
        # the user has new mail. Override INBOX with the semantic
        # count the message-list endpoint uses (is_sent=0) so the
        # badge matches what the user sees on click.
        if d["category"] == "inbox":
            if semantic_inbox_unread is None:
                # Match the semantic-inbox filter on /messages — Trash
                # / Junk / Drafts excluded so a "deleted" (moved-to-
                # Trash) message doesn't keep inflating the badge.
                not_in_buckets = (
                    " AND folder_id NOT IN ("
                    "   SELECT id FROM email_folders WHERE"
                    "     flags LIKE '%\\\\Trash%'"
                    "  OR flags LIKE '%\\\\Junk%'"
                    "  OR flags LIKE '%\\\\Drafts%'"
                    " )"
                )
                with get_conn() as conn:
                    semantic_inbox_unread = conn.execute(
                        "SELECT COUNT(*) FROM email_messages "
                        "WHERE account_id=? AND is_sent=0 AND is_unread=1"
                        + not_in_buckets,
                        (account_id,),
                    ).fetchone()[0]
                    semantic_inbox_total = conn.execute(
                        "SELECT COUNT(*) FROM email_messages "
                        "WHERE account_id=? AND is_sent=0"
                        + not_in_buckets,
                        (account_id,),
                    ).fetchone()[0]
            d["unread"] = semantic_inbox_unread
            d["total"]  = semantic_inbox_total
        out.append(d)
    return out


def _folder_category(flags: list, name: str) -> str:
    """Map IMAP SPECIAL-USE flags + naming heuristics to a category
    we can show with an icon in the UI."""
    if "\\Inbox" in flags or name.upper() == "INBOX":          return "inbox"
    if "\\Sent" in flags:                                       return "sent"
    if "\\Drafts" in flags:                                     return "drafts"
    if "\\Trash" in flags or "trash" in name.lower():           return "trash"
    if "\\Junk" in flags or any(x in name.lower() for x in ("spam", "junk")): return "spam"
    if "\\Archive" in flags:                                    return "archive"
    if "\\All" in flags:                                        return "all"
    if "\\Flagged" in flags:                                    return "starred"
    return "custom"


@router.post("/accounts/{account_id}/sync")
async def sync_now(account_id: int, user: dict = Depends(current_user)):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT owner_user_id FROM email_accounts WHERE id=?", (account_id,)
        ).fetchone()
    if not row or row["owner_user_id"] != user["id"]:
        raise HTTPException(404, "account not found")
    from . import email_fetcher
    return await email_fetcher.trigger_sync(account_id)


# ──────────────────────────── messages ──────────────────────────────

@router.get("/messages")
def list_messages(
    account_id: Optional[int] = Query(None),
    folder_id: Optional[int] = Query(None),
    folder: str = Query("inbox"),
    unread_only: bool = Query(False),
    starred_only: bool = Query(False, description="Show only starred messages (cross-folder)."),
    snoozed_view: bool = Query(False, description="Show currently-snoozed messages instead of hiding them."),
    group_by_thread: bool = Query(True, description="Collapse messages with the same thread_id; return only the latest."),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: dict = Depends(current_user),
):
    """List messages across one or all accounts. If folder_id is
    given, filters to that specific IMAP folder (preferred); otherwise
    falls back to the semantic `folder` param (inbox/sent/all/starred).

    Snooze (mig 024): the normal list HIDES messages where
    `snoozed_until` is in the future. Pass ``snoozed_view=true`` to
    flip that — useful for the dedicated "Snoozed" sidebar shortcut.

    Threading (existing thread_id column): with `group_by_thread=true`
    (default), only the latest message per thread is returned, with
    `thread_count` (how many messages in the thread) and
    `thread_has_unread` rolled up. Messages with NULL thread_id are
    treated as singletons.
    """
    # ── snooze filter (mig 024) ──
    # Tolerate the column missing on pre-024 DBs by feature-detecting once.
    # PRAGMA is SQLite-only; on Postgres we ask information_schema instead.
    # current_timestamp / NULL-comparison is portable across both backends.
    has_snooze = False
    try:
        with get_conn() as conn:
            try:
                # Postgres path (also works on newer SQLite via the shim)
                rows = conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'email_messages'"
                ).fetchall()
                cols = {(r["column_name"] if isinstance(r, dict) else r[0]).lower() for r in rows}
            except Exception:  # noqa: BLE001
                # SQLite fallback
                rows = conn.execute("PRAGMA table_info(email_messages)").fetchall()
                cols = {(r["name"] if isinstance(r, dict) else r[1]).lower() for r in rows}
            has_snooze = "snoozed_until" in cols
    except Exception:  # noqa: BLE001
        # If neither lookup works, assume no snooze column rather than
        # crashing the inbox listing entirely.
        has_snooze = False

    snooze_select = ", m.snoozed_until" if has_snooze else ", NULL AS snoozed_until"
    snooze_where = ""
    if has_snooze:
        # snoozed_until column is TEXT (ISO 8601 strings) on Postgres
        # because the migration that added it didn't cast to TIMESTAMPTZ.
        # Compare as text → "YYYY-MM-DDTHH:MM:SS..." sorts lexicographically
        # the same as chronologically, so a literal string compare works
        # on both backends without a CAST.
        from datetime import datetime as _dt, timezone as _tz
        _now_iso = _dt.now(_tz.utc).isoformat(timespec="seconds")
        if snoozed_view:
            snooze_where = f" AND m.snoozed_until IS NOT NULL AND m.snoozed_until > '{_now_iso}'"
        else:
            snooze_where = f" AND (m.snoozed_until IS NULL OR m.snoozed_until <= '{_now_iso}')"

    sql = (
        "SELECT m.id, m.account_id, a.email AS account_email, a.display_name AS account_display_name, "
        "       m.message_id, m.thread_id, m.from_email, m.from_name, m.to_addrs, "
        "       m.subject, m.snippet, m.date_received, m.is_unread, m.is_starred, "
        "       m.is_sent, m.has_attachments, m.category"
        + snooze_select +
        " FROM email_messages m JOIN email_accounts a ON a.id = m.account_id "
        "WHERE m.owner_user_id = ?"
        + snooze_where
    )
    params: list[Any] = [user["id"]]
    if account_id:
        sql += " AND m.account_id = ?"
        params.append(account_id)
    if unread_only:
        sql += " AND m.is_unread = 1"
    if starred_only:
        sql += " AND m.is_starred = 1"
    if folder_id is not None:
        # Specific folder selected — most precise filter.
        sql += " AND m.folder_id = ?"
        params.append(folder_id)
    elif folder == "inbox":
        # Semantic inbox = received messages NOT physically filed
        # under Trash / Junk / Drafts. is_sent=0 alone isn't enough:
        # a message moved to Trash via delete_message keeps
        # is_sent=0, so it would otherwise still surface here. The
        # subquery excludes any folder whose IMAP flags mark it as
        # one of those destination buckets.
        sql += (
            " AND m.is_sent = 0"
            " AND m.folder_id NOT IN ("
            "   SELECT id FROM email_folders WHERE"
            "     flags LIKE '%\\\\Trash%'"
            "  OR flags LIKE '%\\\\Junk%'"
            "  OR flags LIKE '%\\\\Drafts%'"
            " )"
        )
    elif folder == "sent":
        sql += " AND m.is_sent = 1"
    sql += " ORDER BY m.date_received DESC NULLS LAST, m.id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    flat = []
    for r in rows:
        d = dict(r)
        try:
            d["to_addrs"] = json.loads(d["to_addrs"]) if d.get("to_addrs") else []
        except json.JSONDecodeError:
            d["to_addrs"] = []
        d["is_unread"] = bool(d["is_unread"])
        d["is_starred"] = bool(d["is_starred"])
        d["is_sent"] = bool(d["is_sent"])
        d["has_attachments"] = bool(d["has_attachments"])
        flat.append(d)

    if not group_by_thread:
        return flat

    # ── thread collapse ──
    # Keep order from the SQL (already date-desc). For each thread_id
    # the first row seen wins (= latest message). Singletons (NULL
    # thread_id) are kept as-is so we don't accidentally merge them.
    # The list of thread_ids we need a count for is gathered in one
    # pass; the rollup query runs once.
    seen: dict[str, dict] = {}
    singletons: list[dict] = []
    for d in flat:
        tid = d.get("thread_id")
        if not tid:
            singletons.append(d)
            continue
        if tid not in seen:
            seen[tid] = d

    if seen:
        thread_ids = list(seen.keys())
        placeholders = ",".join("?" * len(thread_ids))
        with get_conn() as conn:
            stats = conn.execute(
                f"SELECT thread_id, COUNT(*) AS n, "
                f"       SUM(is_unread) AS unread_n "
                f"FROM email_messages "
                f"WHERE owner_user_id = ? AND thread_id IN ({placeholders}) "
                f"GROUP BY thread_id",
                [user["id"], *thread_ids],
            ).fetchall()
        for s in stats:
            tid = s["thread_id"]
            if tid in seen:
                seen[tid]["thread_count"]      = int(s["n"] or 0)
                seen[tid]["thread_has_unread"] = bool(int(s["unread_n"] or 0))

    # Merge singletons + thread-heads back in date-desc order. Use the
    # SQL ordering as a stable reference rather than re-sorting.
    by_id = {d["id"]: d for d in [*seen.values(), *singletons]}
    return [by_id[d["id"]] for d in flat if d["id"] in by_id]


@router.get("/messages/{msg_id}")
def get_message(msg_id: int, user: dict = Depends(current_user)):
    """Single message with full body + attachment list."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT m.*, a.email AS account_email "
            "FROM email_messages m JOIN email_accounts a ON a.id = m.account_id "
            "WHERE m.id=? AND m.owner_user_id=?",
            (msg_id, user["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(404, "message not found")
        atts = conn.execute(
            "SELECT id, filename, mimetype, size_bytes, content_id, is_inline, "
            "       paperless_id, immich_id "
            "FROM email_attachments WHERE message_id=?",
            (msg_id,),
        ).fetchall()
        # Mark as read on open.
        if row["is_unread"]:
            conn.execute("UPDATE email_messages SET is_unread=0 WHERE id=?", (msg_id,))
            conn.commit()
    d = dict(row)
    for col in ("to_addrs", "cc_addrs", "references_ids"):
        try:
            d[col] = json.loads(d.get(col) or "[]")
        except json.JSONDecodeError:
            d[col] = []
    d["attachments"] = [dict(a) for a in atts]
    # Unsubscribe analysis (RFC 2369 + 8058). Always include even when
    # method='none' so the frontend doesn't have to do its own absence
    # check. Strip the raw headers from the response — they're not
    # useful to the client.
    from . import email_unsubscribe as _ue
    d["unsubscribe"] = _ue.analyse(
        d.pop("list_unsubscribe",      None),
        d.pop("list_unsubscribe_post", None),
    )
    return d


class MessagePatch(BaseModel):
    is_unread:  Optional[bool] = None
    is_starred: Optional[bool] = None


@router.patch("/messages/{msg_id}")
async def patch_message(msg_id: int, body: MessagePatch,
                         user: dict = Depends(current_user)):
    """Update message flags (read/unread, starred). Issues the IMAP
    STORE command + updates local SQLite."""
    if body.is_unread is None and body.is_starred is None:
        raise HTTPException(400, "nothing to update")
    if body.is_unread is not None:
        # is_unread=True → mark UNSEEN, is_unread=False → mark SEEN.
        ok = await asyncio.to_thread(email_actions.set_seen, msg_id, user["id"], not body.is_unread)
        if not ok:
            raise HTTPException(502, "IMAP flag update failed")
    if body.is_starred is not None:
        ok = await asyncio.to_thread(email_actions.set_starred, msg_id, user["id"], body.is_starred)
        if not ok:
            raise HTTPException(502, "IMAP flag update failed")
    return {"ok": True}


@router.post("/messages/{msg_id}/move")
async def move_message(msg_id: int, target_folder_id: int = Body(..., embed=True),
                        user: dict = Depends(current_user)):
    ok = await asyncio.to_thread(email_actions.move_to_folder, msg_id, user["id"], target_folder_id)
    if not ok:
        raise HTTPException(502, "IMAP move failed")
    return {"ok": True}


# ─── snooze (mig 024) ───
# Push a message out of the inbox until the chosen time. The list
# endpoint hides snoozed messages by default; the "Snoozed" sidebar
# shortcut surfaces them.

class SnoozeBody(BaseModel):
    until: str  # ISO datetime, e.g. "2026-05-28T08:00:00"


@router.post("/messages/{msg_id}/snooze")
def snooze_message(msg_id: int, body: SnoozeBody,
                   user: dict = Depends(current_user)):
    if not (body.until or "").strip():
        raise HTTPException(400, "until is required (ISO datetime)")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM email_messages WHERE id=? AND owner_user_id=?",
            (msg_id, user["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(404, "message not found")
        try:
            conn.execute(
                "UPDATE email_messages SET snoozed_until=? WHERE id=?",
                (body.until, msg_id),
            )
            conn.commit()
        except Exception as exc:
            # Pre-024 DB — column missing. Surface a clear error so the
            # UI doesn't silently believe the snooze stuck.
            raise HTTPException(
                503,
                "snooze column missing — restart uvicorn to run migration 024",
            ) from exc
    return {"ok": True, "snoozed_until": body.until}


@router.post("/messages/{msg_id}/unsnooze")
def unsnooze_message(msg_id: int, user: dict = Depends(current_user)):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM email_messages WHERE id=? AND owner_user_id=?",
            (msg_id, user["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(404, "message not found")
        try:
            conn.execute(
                "UPDATE email_messages SET snoozed_until=NULL WHERE id=?",
                (msg_id,),
            )
            conn.commit()
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "snoozed_until": None}


@router.post("/messages/{msg_id}/archive")
async def archive_message_route(msg_id: int, user: dict = Depends(current_user)):
    ok = await asyncio.to_thread(email_actions.archive_message, msg_id, user["id"])
    if not ok:
        raise HTTPException(502, "archive failed")
    return {"ok": True}


@router.delete("/messages/{msg_id}")
async def delete_message_route(msg_id: int, user: dict = Depends(current_user)):
    """Move to Trash (or hard-delete if no Trash exists)."""
    ok = await asyncio.to_thread(email_actions.delete_message, msg_id, user["id"])
    if not ok:
        raise HTTPException(502, "delete failed")
    return {"ok": True}


@router.post("/messages/{msg_id}/unsubscribe")
async def unsubscribe_route(msg_id: int, user: dict = Depends(current_user)):
    """Perform a List-Unsubscribe (RFC 2369 + 8058).

    Two methods land server-side and complete here:
      - one_click → HTTPS POST per RFC 8058
      - mailto    → empty email via existing SMTP path

    The third method (legacy http URL without one-click) is returned
    to the caller — the browser opens it in a new tab so the user can
    finish on the sender's unsub page. We don't pretend to handle
    CAPTCHAs or login walls.

    On success we ALSO add the sender to the user's email_blocklist
    so any future mail from them stops generating bill/appointment
    notifications. Unsubscribing once and seeing "Add as bill?" for
    the same vendor tomorrow would be the worst possible UX.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, account_id, from_email, list_unsubscribe, list_unsubscribe_post "
            "FROM email_messages WHERE id=? AND owner_user_id=?",
            (msg_id, user["id"]),
        ).fetchone()
    if not row:
        raise HTTPException(404, "message not found")

    from . import email_unsubscribe as _ue
    analysis = _ue.analyse(row["list_unsubscribe"], row["list_unsubscribe_post"])
    method  = analysis["method"]
    target  = analysis["target"]

    if method == "none":
        raise HTTPException(400, "no List-Unsubscribe header on this message")

    if method == "http":
        # Caller opens it in a tab; we just report the URL.
        return {"ok": True, "method": "http", "target": target}

    if method == "one_click":
        result = await asyncio.to_thread(_ue.execute_one_click, target)
        ok = bool(result.get("ok"))
    elif method == "mailto":
        result = await asyncio.to_thread(_ue.execute_mailto, int(row["account_id"]), target)
        ok = bool(result.get("ok"))
    else:
        raise HTTPException(500, f"unsupported method: {method}")

    # Even when the unsubscribe POST/mail fails, we still record the
    # sender on the blocklist — the user's intent was clear ("don't
    # bother me with this anymore"), and the blocklist is the load-
    # bearing part of the don't-bother-me guarantee.
    blocked = False
    try:
        from . import email_blocklist
        sender = (row["from_email"] or "").strip()
        if sender:
            email_blocklist.add_sender(
                user["id"], sender,
                reason=f"unsubscribed via List-Unsubscribe (msg #{msg_id})",
            )
            blocked = True
    except Exception as exc:  # noqa: BLE001
        log.warning("post-unsubscribe blocklist add failed for msg %d: %s", msg_id, exc)

    return {
        "ok":      ok,
        "method":  method,
        "target":  target,
        "blocked": blocked,
        "detail":  result,
    }


@router.get("/attachments/{att_id}/download")
async def download_attachment(att_id: int, user: dict = Depends(current_user)):
    """Fetch and stream an attachment's binary. Lazy — IMAP re-fetches
    on demand instead of pre-downloading every attachment on sync."""
    blob = await asyncio.to_thread(email_actions.fetch_attachment_binary, att_id, user["id"])
    if not blob:
        raise HTTPException(404, "attachment not available")
    safe_name = (blob["filename"] or "attachment").replace('"', "_")
    return Response(
        content=blob["content"],
        media_type=blob["mimetype"],
        headers={"content-disposition": f'attachment; filename="{safe_name}"'},
    )


@router.get("/search")
def search_email(q: str = Query(..., min_length=2),
                  limit: int = Query(30, ge=1, le=100),
                  user: dict = Depends(current_user)):
    """FTS5 across subject + sender + snippet + body. Same triggers
    that maintain wa_messages_fts maintain this one — no separate
    indexing pass needed."""
    # FTS5 query: tokenise the user's input and require ALL words
    # so 'müller invoice' doesn't match either alone.
    terms = [t for t in q.split() if t]
    if not terms:
        return []
    match = " ".join(f'"{t.replace(chr(34), "")}"*' for t in terms)
    with get_conn() as conn:
        try:
            rows = conn.execute(
                "SELECT m.id, m.account_id, a.email AS account_email, "
                "       m.from_email, m.from_name, m.subject, m.snippet, "
                "       m.date_received, m.is_unread, m.is_starred, m.has_attachments "
                "FROM email_messages_fts f "
                "JOIN email_messages m ON m.rowid = f.rowid "
                "JOIN email_accounts a ON a.id = m.account_id "
                "WHERE f MATCH ? AND m.owner_user_id = ? "
                "ORDER BY m.date_received DESC LIMIT ?",
                (match, user["id"], limit),
            ).fetchall()
        except Exception as e:
            raise HTTPException(400, f"search query invalid: {e}")
    return [{
        **dict(r),
        "is_unread": bool(r["is_unread"]),
        "is_starred": bool(r["is_starred"]),
        "has_attachments": bool(r["has_attachments"]),
        "to_addrs": [],  # FTS rows don't carry to_addrs; UI handles
        "is_sent": False,
        "account_display_name": None,
    } for r in rows]


@router.get("/threads/{thread_id}")
def get_thread(thread_id: str, user: dict = Depends(current_user)):
    """Every message in this thread, oldest-first (so the reader can
    render the conversation chronologically)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, from_email, from_name, subject, snippet, body_html, body_text, "
            "       date_received, is_sent, is_unread, has_attachments "
            "FROM email_messages "
            "WHERE thread_id=? AND owner_user_id=? "
            "ORDER BY date_received ASC, id ASC",
            (thread_id, user["id"]),
        ).fetchall()
    return [dict(r) for r in rows]


# ──────────────────────────── send ──────────────────────────────────

@router.get("/messages/{msg_id}/drafts")
def list_pending_drafts(msg_id: int, user: dict = Depends(current_user)):
    """Pending auto-drafted variants for one message. UI calls this
    when the message is opened."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, draft_text, variant_label, variant_group_id, sources_json "
            "FROM email_drafts WHERE message_id=? AND owner_user_id=? AND status='pending' "
            "ORDER BY id ASC",
            (msg_id, user["id"]),
        ).fetchall()
    if not rows:
        return {"group_id": None, "variants": [], "sources": []}
    variants = []
    sources: list[Any] = []
    for r in rows:
        try:
            srcs = json.loads(r["sources_json"] or "[]")
        except json.JSONDecodeError:
            srcs = []
        if not sources and srcs:
            sources = srcs
        variants.append({
            "id": r["id"],
            "label": r["variant_label"] or "draft",
            "text": r["draft_text"],
        })
    return {"group_id": rows[0]["variant_group_id"], "variants": variants, "sources": sources}


class _RegenerateDraftsBody(BaseModel):
    # Free-text intent the user wants the drafts to express ("decline
    # politely, say I'm on holiday until next Tuesday"). Forwarded to
    # the email_draft skill as extra_instructions. None / empty = let
    # the LLM pick from the thread context as before.
    instructions: Optional[str] = None
    # Tone key (friendly/formal/quick/warm/firm — see
    # /api/email/draft-states). When set, all 3 variants share that
    # tone and only vary in angle. Mirrors the WhatsApp DraftPanel UX.
    state: Optional[str] = None


@router.post("/messages/{msg_id}/drafts/regenerate")
async def regenerate_drafts(
    msg_id: int,
    body: Optional[_RegenerateDraftsBody] = None,
    user: dict = Depends(current_user),
):
    """Force-rerun the email_draft skill for this message NOW.
    Optionally accepts free-text user instructions describing what
    the reply should say + a tone state. Without either, the skill
    guesses the angle from the thread context as before."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT owner_user_id FROM email_messages WHERE id=?", (msg_id,)
        ).fetchone()
    if not row or row["owner_user_id"] != user["id"]:
        raise HTTPException(404, "message not found")
    instructions = (body.instructions or "").strip() if body else ""
    state = (body.state or "").strip() if body else ""
    from . import email_autodraft
    await email_autodraft._generate_and_store(
        user["id"], msg_id,
        extra_instructions=instructions or None,
        state=state or None,
    )
    return {"ok": True}


class _ComposeAttachmentMeta(BaseModel):
    filename: str
    mimetype: Optional[str] = None
    # Where the file came from when it was server-fetched. Lets the
    # draft pipeline pull a real content snippet for PDFs / docs so
    # the LLM doesn't have to invent context. Optional — files the
    # user dragged in from disk have no URL.
    source_url: Optional[str] = None


class _ComposeDraftsBody(BaseModel):
    # Free-text brief describing what the user wants to say. Required —
    # without it there's nothing to draft. The Composer's UI gates the
    # "Generate" button on a non-empty intent.
    intent:  str = Field(..., min_length=1, max_length=4000)
    to:      Optional[str] = Field(None, max_length=512)
    subject: Optional[str] = Field(None, max_length=512)
    # Tone key (friendly/formal/quick/warm/firm — see /draft-states).
    state:   Optional[str] = Field(None, max_length=32)
    # Attachment metadata so the LLM can reference what's being sent
    # ("the attached price list", "a few photos from the weekend").
    # No content here — just filename + mimetype. The Composer already
    # base64-encodes the bytes for /send; this is purely prompt context.
    attachments: list[_ComposeAttachmentMeta] = Field(default_factory=list)


@router.post("/compose/drafts")
async def compose_drafts(
    body: _ComposeDraftsBody,
    user: dict = Depends(current_user),
):
    """Three draft variants for a NEW (non-reply) email.

    Unlike /messages/{id}/drafts these aren't persisted to email_drafts
    (there's no message to tie them to) — they're returned inline and
    the Composer holds them in React state. Also returns a
    `suggested_subject` when the user hasn't typed one yet, so the
    Composer can pre-fill the Subject field.
    """
    from . import email_autodraft
    result = await email_autodraft.generate_compose_drafts(
        user["id"],
        intent=body.intent,
        to_address=body.to,
        subject=body.subject,
        state=body.state,
        attachments=[a.model_dump() for a in body.attachments],
    )
    # No persistence — synthesise ids from index so the React key
    # prop stays stable across the request/response.
    return {
        "variants": [
            {"id": i, "label": v["label"], "text": v["text"]}
            for i, v in enumerate(result.get("variants", []))
        ],
        "suggested_subject": result.get("suggested_subject", ""),
    }


@router.get("/draft-states")
def list_draft_states(
    user: dict = Depends(current_user),  # noqa: ARG001
) -> list[dict[str, str]]:
    """Catalogue of available draft tone states + bilingual labels.
    Shared with the WhatsApp DraftPanel — same 5 tones, same keys,
    so the UI patterns stay symmetric."""
    from . import whatsapp_autodraft as _ad
    return [
        {"key": key, **spec}
        for key, spec in _ad.STATE_SPECS.items()
    ]


@router.post("/messages/{msg_id}/drafts/discard")
def discard_drafts(msg_id: int, user: dict = Depends(current_user)):
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE email_drafts SET status='discarded', discarded_at=datetime('now'), "
            "discard_reason='user_dismissed' WHERE message_id=? AND owner_user_id=? AND status='pending'",
            (msg_id, user["id"]),
        )
        conn.commit()
    return {"discarded": cur.rowcount or 0}


@router.get("/briefing")
async def briefing(hours: int = Query(24, ge=1, le=168),
                    user: dict = Depends(current_user)):
    """Natural-language summary of the user's email inbox. Dispatches
    to the email_briefing skill so the same code path is used by the
    chat agent's use_skill calls."""
    from .skills import get_registry, SkillContext
    reg = get_registry()
    if not reg.get("email_briefing"):
        raise HTTPException(503, "email_briefing skill not loaded")
    ctx = SkillContext(reg, role=user["role"], user_id=user["id"])
    return await reg.invoke("email_briefing", ctx=ctx, hours=hours)


@router.post("/send")
async def send_message(body: SendBody, user: dict = Depends(current_user)):
    # Account ownership check.
    with get_conn() as conn:
        row = conn.execute(
            "SELECT owner_user_id FROM email_accounts WHERE id=?", (body.account_id,)
        ).fetchone()
    if not row or row["owner_user_id"] != user["id"]:
        raise HTTPException(403, "not your account")

    # Decode attachments from base64. Total cap = 25MB (typical SMTP
    # limit for most providers; Gmail = 25, Outlook = 20, ProtonMail = 25).
    # Anything bigger and we 413 BEFORE handing it to SMTP so the user
    # gets a clear "too large" instead of a cryptic SMTP timeout.
    import base64 as _b64
    decoded_attachments: list[dict] = []
    total_bytes = 0
    MAX_TOTAL = 25 * 1024 * 1024
    for att in (body.attachments or []):
        try:
            raw = _b64.b64decode(att.content_b64 or "", validate=False)
        except Exception:
            raise HTTPException(400, f"invalid base64 for attachment {att.filename!r}")
        total_bytes += len(raw)
        if total_bytes > MAX_TOTAL:
            raise HTTPException(
                413,
                f"attachments exceed {MAX_TOTAL // (1024 * 1024)} MB total — "
                "split into multiple emails or send a download link instead.",
            )
        decoded_attachments.append({
            "filename": att.filename or "attachment.bin",
            "mimetype": att.mimetype or None,
            "content":  raw,
        })

    result = await asyncio.to_thread(
        send, body.account_id, body.to, body.subject, body.body_text,
        body.body_html, body.cc, body.bcc, body.in_reply_to, body.references,
        decoded_attachments or None,
    )
    if not result.get("ok"):
        raise HTTPException(502, result.get("error", "send failed"))
    # Mark the source draft as 'used' + siblings as 'discarded'.
    if body.draft_id is not None:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT variant_group_id FROM email_drafts WHERE id=? AND owner_user_id=?",
                (body.draft_id, user["id"]),
            ).fetchone()
            if row:
                gid = row["variant_group_id"]
                conn.execute("UPDATE email_drafts SET status='used' WHERE id=?", (body.draft_id,))
                if gid:
                    conn.execute(
                        "UPDATE email_drafts SET status='discarded', discarded_at=datetime('now'), "
                        "discard_reason='sibling_used' WHERE variant_group_id=? AND id!=? AND status='pending'",
                        (gid, body.draft_id),
                    )
                conn.commit()
    return result
