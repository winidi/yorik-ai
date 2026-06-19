"""SMTP sender. Builds a proper MIME message and sends through the
account's configured SMTP server.

Supports:
  - SSL on connect (port 465)
  - STARTTLS upgrade (port 587)
  - Multiple to/cc/bcc addresses
  - Plain text + HTML alternative
  - File attachments (uploaded via multipart on the API)
  - Threading via In-Reply-To + References headers (so replies show
    as replies in the recipient's email client)
  - Returns the Message-ID we generated so we can also store the
    sent message in our local DB
"""

from __future__ import annotations

import logging
import mimetypes
import smtplib
import ssl
import uuid
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, formatdate
from typing import Any, Optional

from . import credential_store
from .database import get_conn

log = logging.getLogger("yorik.email.sender")


def send(
    account_id: int,
    to: list[str],
    subject: str,
    body_text: str,
    body_html: Optional[str] = None,
    cc: Optional[list[str]] = None,
    bcc: Optional[list[str]] = None,
    in_reply_to: Optional[str] = None,
    references: Optional[list[str]] = None,
    attachments: Optional[list[dict]] = None,  # [{filename, mimetype, content: bytes}, ...]
) -> dict[str, Any]:
    """Build + send. Returns {ok, message_id, error?}. Idempotent in
    the sense that re-calling generates a NEW message_id — there's no
    de-dup, the caller is responsible for not double-clicking."""
    cfg = _load_account(account_id)
    if not cfg:
        return {"ok": False, "error": "account not found"}
    creds = credential_store.get(cfg["credential_key"]) or {}
    pw = creds.get("smtp_password") if isinstance(creds, dict) else creds
    if not pw:
        return {"ok": False, "error": "no SMTP password in credential store"}

    msg = EmailMessage()
    from_display = cfg.get("display_name") or cfg["email"].split("@")[0]
    msg["From"] = formataddr((from_display, cfg["email"]))
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject or "(no subject)"
    msg["Date"] = formatdate(localtime=True)
    message_id = make_msgid(domain=cfg["email"].split("@", 1)[1])
    msg["Message-ID"] = message_id
    if in_reply_to:
        # In-Reply-To MUST be wrapped in <>; we accept both forms.
        irt = in_reply_to if in_reply_to.startswith("<") else f"<{in_reply_to}>"
        msg["In-Reply-To"] = irt
    if references:
        refs = " ".join(r if r.startswith("<") else f"<{r}>" for r in references)
        msg["References"] = refs

    if body_html:
        msg.set_content(body_text or "This email is best viewed in an HTML-capable client.")
        msg.add_alternative(body_html, subtype="html")
    else:
        msg.set_content(body_text or "")

    for att in (attachments or []):
        content = att.get("content")
        if not content:
            continue
        filename = att.get("filename") or "attachment.bin"
        mimetype = att.get("mimetype")
        if not mimetype:
            mimetype, _ = mimetypes.guess_type(filename)
            mimetype = mimetype or "application/octet-stream"
        maintype, _, subtype = mimetype.partition("/")
        msg.add_attachment(content, maintype=maintype, subtype=subtype, filename=filename)

    # All recipients for envelope, including bcc.
    rcpts = list(to)
    if cc:  rcpts.extend(cc)
    if bcc: rcpts.extend(bcc)

    try:
        from .email_ssl import make_ssl_context
        ssl_ctx = make_ssl_context(cfg["smtp_host"])
        if cfg["smtp_ssl"]:
            with smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"],
                                   context=ssl_ctx, timeout=30) as s:
                s.login(cfg["smtp_username"], pw)
                s.send_message(msg, from_addr=cfg["email"], to_addrs=rcpts)
        else:
            with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=30) as s:
                s.ehlo()
                if cfg["smtp_starttls"]:
                    s.starttls(context=ssl_ctx)
                    s.ehlo()
                s.login(cfg["smtp_username"], pw)
                s.send_message(msg, from_addr=cfg["email"], to_addrs=rcpts)
    except smtplib.SMTPAuthenticationError as e:
        return {"ok": False, "error": f"SMTP auth failed: {e.smtp_error.decode('utf-8', 'replace') if isinstance(e.smtp_error, bytes) else e.smtp_error}"}
    except Exception as e:
        log.exception("SMTP send failed")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # APPEND to the IMAP server's Sent folder so the message shows up
    # in webmail, the user's phone, and any other IMAP client. Most
    # providers (GMX, Outlook, Strato, self-hosted) do NOT auto-mirror
    # SMTP-submitted messages — Gmail is the exception. Without this
    # APPEND the user would only see the message in Yorik's local DB
    # (mirror written below). Best-effort: APPEND failures are logged
    # but don't fail the send.
    sent_folder = _imap_append_to_sent(cfg, msg.as_bytes())

    # Mirror into local DB so the sent message shows up in Yorik's UI
    # right away — the IMAP fetcher will dedupe on Message-ID when it
    # eventually pulls the APPEND'd copy back from the server, so this
    # local row is the truth until the next sync tick.
    _store_sent_copy(cfg, message_id, to, cc, subject, body_text, body_html,
                     in_reply_to, references, sent_folder_name=sent_folder)

    # Contacts autocapture: promote any pending contacts the user just
    # replied to. The "user actually wrote them" signal is the strongest
    # confirmation that they're not spam — flip them to active so they
    # surface in autocomplete and Pending count drops. Never raises.
    try:
        from . import contact_autocapture
        recipients = list(to) + list(cc or [])
        contact_autocapture.on_outbound_email(to_addrs=recipients)
    except Exception as exc:
        log.debug("contact_autocapture outbound hook failed: %s", exc)

    return {"ok": True, "message_id": message_id.strip("<>")}


def _store_sent_copy(cfg, message_id, to, cc, subject, body_text, body_html,
                     in_reply_to, references, sent_folder_name=None):
    """Legacy cfg-shaped wrapper for the email_sender.send path."""
    store_sent_mirror(
        account_id=cfg["id"], owner_user_id=cfg["owner_user_id"],
        from_email=cfg["email"], from_name=cfg.get("display_name") or "",
        message_id=message_id, to=to, cc=cc,
        subject=subject, body_text=body_text, body_html=body_html,
        in_reply_to=in_reply_to, references=references,
        sent_folder_name=sent_folder_name,
    )


def store_sent_mirror(
    *,
    account_id: int,
    owner_user_id: str,
    from_email: str,
    from_name: str,
    message_id: str,
    to: list[str],
    cc: Optional[list[str]],
    subject: str,
    body_text: str,
    body_html: Optional[str],
    in_reply_to: Optional[str],
    references: Optional[list[str]],
    sent_folder_name: Optional[str] = None,
) -> None:
    """Write a local mirror row into email_messages for a just-sent
    message so the Sent view updates instantly, without waiting on the
    next IMAP fetcher tick. The fetcher's message_id dedup (see
    email_fetcher._insert_message) later UPDATE-replaces this row with
    the real IMAP uid/flags when the round-trip completes.

    Used by both send paths:
      - email_sender.send() (direct SMTP via account row)
      - connectors.email_imap._send() (chat/compose path via credential
        store, resolves account_id by from_email)

    Best-effort: catches any error so a botched mirror never breaks the
    actual send result. The dedup gate is account_id + message_id, so
    callers must pass a real Message-ID (caller-generated, same one in
    the SMTP envelope).
    """
    import json as _json
    from datetime import datetime, timezone
    mid_clean = (message_id or "").strip("<>")
    thread_id = (references[0] if references else
                  in_reply_to or mid_clean)
    now = datetime.now(timezone.utc).isoformat()
    snippet = (body_text or "")[:220].replace("\n", " ").strip()
    with get_conn() as conn:
        # Resolve folder_id from the local mirror when the APPEND landed
        # in a folder we know about — lets the Sent view show the local
        # copy under the right folder header until the fetcher dedupes
        # against the server-side row on next sync.
        folder_id = None
        if sent_folder_name:
            r = conn.execute(
                "SELECT id FROM email_folders WHERE account_id=? AND name=?",
                (account_id, sent_folder_name),
            ).fetchone()
            if r:
                folder_id = int(r["id"])
        try:
            conn.execute(
                "INSERT INTO email_messages "
                "(account_id, folder_id, uid, message_id, in_reply_to, references_ids, "
                " thread_id, from_email, from_name, to_addrs, cc_addrs, "
                " subject, snippet, body_text, body_html, date_sent, date_received, "
                " is_unread, is_sent, owner_user_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, ?)",
                (account_id, folder_id, -int(datetime.now().timestamp()),
                 mid_clean, in_reply_to,
                 _json.dumps(references) if references else None,
                 thread_id,
                 from_email, from_name or "",
                 _json.dumps([{"email": a} for a in to]),
                 _json.dumps([{"email": a} for a in (cc or [])]),
                 subject, snippet, body_text or "", body_html,
                 now, now, owner_user_id),
            )
            conn.commit()
        except Exception as e:
            log.warning("sent-copy insert failed: %s", e)


def resolve_account_for_from_address(from_address: str) -> Optional[dict]:
    """Lookup helper for the email_imap connector path: returns
    {id, owner_user_id, display_name} for the email_accounts row whose
    email matches from_address (case-insensitive), or None when the
    connector is talking to an address we don't have an accounts row
    for (in which case the local mirror is skipped and the user waits
    for the fetcher tick — degraded but not broken)."""
    addr = (from_address or "").strip().lower()
    if not addr:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, owner_user_id, display_name "
            "FROM email_accounts WHERE LOWER(email)=? "
            "ORDER BY is_default DESC, id ASC LIMIT 1",
            (addr,),
        ).fetchone()
    return dict(row) if row else None


def _load_account(account_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, owner_user_id, email, display_name, "
            "       smtp_host, smtp_port, smtp_ssl, smtp_starttls, smtp_username, "
            "       imap_host, imap_port, imap_ssl, imap_starttls, imap_username, "
            "       credential_key "
            "FROM email_accounts WHERE id=?",
            (account_id,),
        ).fetchone()
    return dict(row) if row else None


# RFC 6154 SPECIAL-USE attribute markers (case-insensitive) that
# identify the Sent folder regardless of name. IMAPClient surfaces
# them as `\\Sent` in the flags tuple.
_SENT_SPECIAL_USE = "\\sent"

# Fallback folder-name candidates for servers that don't expose
# SPECIAL-USE. Tried in order; first one the server has wins. Covers
# the providers Yorik users tend to land on (GMX, Web.de, T-Online,
# Strato, Gmail's flat namespace, classic INBOX.* hierarchical setups,
# and Outlook/Office365).
_SENT_NAME_CANDIDATES = (
    "Sent",
    "Sent Items",
    "Sent Messages",
    "Sent Mail",
    "INBOX.Sent",
    "INBOX/Sent",
    "Gesendet",
    "Gesendete Elemente",
    "Gesendete Objekte",
    "Posta inviata",
    "Bandeja de salida",
    "Elementos enviados",
    "[Gmail]/Sent Mail",
)


def _find_sent_folder(cfg: dict) -> Optional[str]:
    """Return the IMAP folder name to APPEND to. Prefers the local
    mirror (email_folders.flags contains '\\Sent' after the fetcher
    has enumerated the account); falls back to live IMAP LIST when
    the mirror is empty (e.g. a freshly-configured account whose
    fetcher hasn't run yet)."""
    import json as _json
    # 1. Local mirror — fast path, no network.
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT name, flags FROM email_folders WHERE account_id=?",
            (cfg["id"],),
        ).fetchall()
    for r in rows:
        try:
            flags = _json.loads(r["flags"] or "[]")
        except (TypeError, ValueError):
            flags = []
        if any(str(f).lower() == _SENT_SPECIAL_USE for f in flags):
            return r["name"]

    # 2. Live LIST against the IMAP server.
    try:
        from imapclient import IMAPClient
        from .email_ssl import make_ssl_context
        creds = credential_store.get(cfg["credential_key"]) or {}
        pw = creds.get("imap_password") if isinstance(creds, dict) else creds
        if not pw:
            log.warning("find_sent_folder: no IMAP password — skipping APPEND")
            return None
        ssl_on = bool(cfg.get("imap_ssl"))
        use_starttls = bool(cfg.get("imap_starttls")) and not ssl_on
        ssl_ctx = make_ssl_context(cfg["imap_host"]) if (ssl_on or use_starttls) else None
        with IMAPClient(host=cfg["imap_host"], port=int(cfg["imap_port"]),
                         ssl=ssl_on, ssl_context=(ssl_ctx if ssl_on else None),
                         timeout=15) as c:
            if use_starttls:
                c.starttls(ssl_context=ssl_ctx)
            c.login(cfg["imap_username"], pw)
            # First pass: SPECIAL-USE.
            for flags, _delim, name in c.list_folders():
                if any(str(f).lower() in (_SENT_SPECIAL_USE, b"\\sent")
                       for f in (flags or ())):
                    return name
            # Second pass: name match.
            existing = {n for _f, _d, n in c.list_folders()}
            for candidate in _SENT_NAME_CANDIDATES:
                if candidate in existing:
                    return candidate
    except Exception as exc:  # noqa: BLE001
        log.warning("find_sent_folder: IMAP list failed: %s", exc)
    return None


def _imap_append_to_sent(cfg: dict, msg_bytes: bytes) -> Optional[str]:
    """APPEND the just-sent message to the IMAP server's Sent folder
    with the \\Seen flag (the sender has obviously already 'read' their
    own outbound mail). Returns the folder name on success, None on
    failure. Failures are logged but DON'T abort the send — the SMTP
    side already succeeded and the local DB copy below keeps the UI
    consistent.

    Why we need this: most providers (GMX, Web.de, Outlook, Strato,
    self-hosted) do NOT auto-mirror SMTP-submitted messages to the
    IMAP Sent folder. Gmail does, but it's the exception. Without
    this APPEND the user sees "email sent" in Yorik but can't find
    the message in their phone's mail app or webmail Sent view —
    exactly the bug surfaced 2026-06-02.
    """
    from datetime import datetime
    sent_folder = _find_sent_folder(cfg)
    if not sent_folder:
        log.warning("imap_append_to_sent: no Sent folder found for account %s; "
                    "skipped (local copy still recorded)", cfg["id"])
        return None
    try:
        from imapclient import IMAPClient
        from .email_ssl import make_ssl_context
        creds = credential_store.get(cfg["credential_key"]) or {}
        pw = creds.get("imap_password") if isinstance(creds, dict) else creds
        if not pw:
            return None
        ssl_on = bool(cfg.get("imap_ssl"))
        use_starttls = bool(cfg.get("imap_starttls")) and not ssl_on
        ssl_ctx = make_ssl_context(cfg["imap_host"]) if (ssl_on or use_starttls) else None
        with IMAPClient(host=cfg["imap_host"], port=int(cfg["imap_port"]),
                         ssl=ssl_on, ssl_context=(ssl_ctx if ssl_on else None),
                         timeout=15) as c:
            if use_starttls:
                c.starttls(ssl_context=ssl_ctx)
            c.login(cfg["imap_username"], pw)
            c.append(sent_folder, msg_bytes,
                     flags=("\\Seen",),
                     msg_time=datetime.now())
        log.info("imap_append_to_sent: appended to '%s' for account %s",
                 sent_folder, cfg["id"])
        return sent_folder
    except Exception as exc:  # noqa: BLE001
        log.warning("imap_append_to_sent: APPEND failed (%s); local copy still recorded", exc)
        return None


# ─── connection test helpers (used by /api/email/accounts/test) ──────

def test_imap(host: str, port: int, ssl_on: bool, username: str, password: str,
              starttls: bool = False) -> dict:
    """Three connection modes:
      - ssl_on=True  → implicit TLS (port 993). Most providers.
      - starttls=True → plaintext connect, upgrade via STARTTLS before
        login (port 143 typically; Proton Bridge on 1143).
      - both False → plaintext + plaintext login (test only).
    `ssl_on` wins if both are set (defensive against UI bugs).
    Loopback hosts get an unverified context (Proton Bridge etc.
    ships self-signed certs that strict verification rejects).
    """
    try:
        from .email_ssl import make_ssl_context
        ssl_ctx = make_ssl_context(host) if (ssl_on or starttls) else None
        from imapclient import IMAPClient
        with IMAPClient(host=host, port=port, ssl=ssl_on,
                         ssl_context=ssl_ctx if ssl_on else None,
                         timeout=15) as c:
            if starttls and not ssl_on:
                c.starttls(ssl_context=ssl_ctx)
            c.login(username, password)
            c.list_folders()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def test_smtp(host: str, port: int, ssl_on: bool, starttls: bool,
               username: str, password: str) -> dict:
    try:
        from .email_ssl import make_ssl_context
        ssl_ctx = make_ssl_context(host)
        if ssl_on:
            with smtplib.SMTP_SSL(host, port, context=ssl_ctx, timeout=15) as s:
                s.login(username, password)
        else:
            with smtplib.SMTP(host, port, timeout=15) as s:
                s.ehlo()
                if starttls:
                    s.starttls(context=ssl_ctx)
                    s.ehlo()
                s.login(username, password)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
