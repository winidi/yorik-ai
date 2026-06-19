"""Email connector via IMAP (read) + SMTP (send).

For generic providers — self-hosted, Fastmail, Posteo, FastMail, mailbox.org,
ProtonMail Bridge, anything that exposes IMAP+SMTP with username/password.
Gmail and Outlook are easier to use via the n8n-backed `email-gmail`
connector (Wave 4b) which handles OAuth, but this connector works for
those too if you set up an app password.

Credentials live in the encrypted store; the user enters them once via
the credentials modal.

Operations:
    {op: "send", to, subject, body, [cc], [bcc]}
    {op: "list_recent", [limit=10], [folder="INBOX"], [unread_only=false]}
    {op: "fetch", uid, [folder="INBOX"]}
    {op: "test_connection"}   — verifies both IMAP and SMTP without sending
"""

from __future__ import annotations

import email
import imaplib
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional

from . import ConnectorSpec, register
from .. import credential_store

log = logging.getLogger("homeos.connectors.email_imap")
CONNECTOR_NAME = "email-imap"
IMAP_TIMEOUT_S = 15
SMTP_TIMEOUT_S = 15


def _creds() -> Optional[Dict[str, Any]]:
    c = credential_store.get(CONNECTOR_NAME)
    if not c:
        return None
    # Fill in sensible defaults that match the credentials_schema.
    c.setdefault("imap_port", 993)
    c.setdefault("smtp_port", 587)
    c.setdefault("use_tls", True)
    c.setdefault("from_address", c.get("username"))
    return c


def _imap_connect(c: Dict[str, Any]) -> imaplib.IMAP4:
    if c.get("use_tls", True):
        conn = imaplib.IMAP4_SSL(c["imap_host"], int(c["imap_port"]), timeout=IMAP_TIMEOUT_S)
    else:
        conn = imaplib.IMAP4(c["imap_host"], int(c["imap_port"]), timeout=IMAP_TIMEOUT_S)
    conn.login(c["username"], c["password"])
    return conn


def _smtp_connect(c: Dict[str, Any]) -> smtplib.SMTP:
    port = int(c["smtp_port"])
    if port == 465:
        ctx = ssl.create_default_context()
        conn: smtplib.SMTP = smtplib.SMTP_SSL(c["smtp_host"], port, timeout=SMTP_TIMEOUT_S, context=ctx)
    else:
        conn = smtplib.SMTP(c["smtp_host"], port, timeout=SMTP_TIMEOUT_S)
        if c.get("use_tls", True):
            conn.starttls(context=ssl.create_default_context())
    conn.login(c["username"], c["password"])
    return conn


def _send(c: Dict[str, Any], to: str, subject: str, body: str,
          cc: str = "", bcc: str = "",
          attachments: Optional[List[Dict[str, Any]]] = None,
          body_html: Optional[str] = None) -> Dict[str, Any]:
    """Send an email via SMTP. `attachments` is a list of
    {filename, mime_type, content_b64} dicts (base64 keeps the JSON payload
    simple). When `body_html` is set, the message is multipart/alternative
    with body as the text/plain fallback and body_html as text/html. Returns
    `ok: True` on success — the caller (compose_send_email, chat agent's
    email skill) MUST check this before reporting success."""
    import base64 as _b64
    from email.utils import make_msgid, formatdate
    msg = EmailMessage()
    msg["From"] = c["from_address"]
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    # Generate Message-ID + Date BEFORE send so the IMAP APPEND below
    # carries the same identifiers the recipient sees — lets the local
    # fetcher dedupe later. Without explicit Message-ID, the SMTP
    # server stamps one we never see, and the APPEND'd copy gets
    # a DIFFERENT id, breaking dedup.
    from_domain = (c["from_address"].split("@", 1)[1]
                    if "@" in (c["from_address"] or "") else "yorik.local")
    message_id = make_msgid(domain=from_domain)
    msg["Message-ID"] = message_id
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(body or "")
    if body_html:
        # Promotes the EmailMessage to multipart/alternative — recipient
        # clients pick the highest-fidelity part they can render. Plain
        # text remains the safe fallback for terminal clients / screen
        # readers / spam-filter previews.
        msg.add_alternative(body_html, subtype="html")
    for att in attachments or []:
        try:
            payload = _b64.b64decode(att.get("content_b64") or "", validate=True)
        except Exception as exc:
            return {"ok": False, "error": f"attachment '{att.get('filename')}' has invalid base64: {exc}"}
        mt = (att.get("mime_type") or "application/octet-stream")
        maintype, _, subtype = mt.partition("/")
        msg.add_attachment(
            payload,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=att.get("filename") or "attachment.bin",
        )
    rcpts = [r.strip() for r in (to + "," + cc + "," + bcc).split(",") if r.strip()]
    to_list = [t.strip() for t in (to or "").split(",") if t.strip()]
    cc_list = [c2.strip() for c2 in (cc or "").split(",") if c2.strip()]
    with _smtp_connect(c) as smtp:
        smtp.send_message(msg, to_addrs=rcpts)

    # IMAP APPEND to Sent — mirrors the email_sender.send fix on the
    # parallel "compose → send-email" path. Most providers (GMX,
    # mail.de, Outlook, Strato) require explicit APPEND; without it,
    # the sender's IMAP Sent folder stays empty even though the
    # recipient got the message. Best-effort: APPEND failures log but
    # don't fail the send.
    sent_folder = _append_to_sent_via_connector(c, msg.as_bytes())

    # Local-mirror parity with email_sender.send(): write the row into
    # email_messages now so the Sent view updates instantly, instead of
    # waiting for the next IMAP fetcher tick (could be minutes on a
    # busy IDLE loop). The connector path doesn't carry an account_id,
    # so we resolve it from email_accounts by from_address. If there's
    # no matching account row (rare — the connector and the account
    # table are normally configured together), skip the mirror; the
    # fetcher will still pick it up eventually.
    try:
        from .. import email_sender as _sender
        acct = _sender.resolve_account_for_from_address(c["from_address"])
        if acct:
            _sender.store_sent_mirror(
                account_id=acct["id"],
                owner_user_id=acct["owner_user_id"],
                from_email=c["from_address"],
                from_name=acct.get("display_name") or "",
                message_id=message_id,
                to=to_list, cc=cc_list,
                subject=subject,
                body_text=body or "",
                body_html=body_html,
                in_reply_to=None, references=None,
                sent_folder_name=sent_folder,
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("email_imap: local-mirror write failed: %s", exc)

    # Outbound contact autocapture: promotes any pending contacts the
    # user just wrote to. Mirrors email_sender.send. Never raises.
    try:
        from .. import contact_autocapture
        contact_autocapture.on_outbound_email(to_addrs=to_list + cc_list)
    except Exception as exc:  # noqa: BLE001
        log.debug("email_imap: contact_autocapture outbound hook failed: %s", exc)

    return {
        "ok": True, "sent": True,
        "to": to, "subject": subject, "from": c["from_address"],
        "message_id": message_id.strip("<>"),
        "sent_folder": sent_folder,
        "attachments": len(attachments or []),
        "body_html": bool(body_html),
    }


# RFC 6154 SPECIAL-USE flag + name fallbacks. Same list as
# email_sender.py — kept in sync there.
_SENT_NAME_CANDIDATES = (
    "Sent", "Sent Items", "Sent Messages", "Sent Mail",
    "INBOX.Sent", "INBOX/Sent",
    "Gesendet", "Gesendete Elemente", "Gesendete Objekte",
    "Posta inviata", "Bandeja de salida", "Elementos enviados",
    "[Gmail]/Sent Mail",
)


def _append_to_sent_via_connector(c: Dict[str, Any], msg_bytes: bytes) -> Optional[str]:
    """Find the Sent folder via SPECIAL-USE or name fallback, then
    IMAP-APPEND the message bytes with \\Seen flag. Returns the folder
    name on success, None on failure (logged). Uses the connector's
    own creds (which already have imap_host / username / password)."""
    import imaplib
    from datetime import datetime
    try:
        conn = _imap_connect(c)
    except Exception as exc:  # noqa: BLE001
        log.warning("email_imap append: IMAP connect failed: %s", exc)
        return None
    try:
        # First pass: list-extended with SPECIAL-USE if the server
        # advertises it. imaplib's list() returns the raw list reply
        # — flags appear in the first parenthesised group of each row.
        typ, items = conn.list()
        if typ != "OK" or not items:
            log.warning("email_imap append: LIST returned %s", typ)
            return None
        sent_folder: Optional[str] = None
        names_seen: List[str] = []
        for raw in items:
            line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
            # Format: (\HasNoChildren \Sent) "/" "Sent"
            # The name is the last quoted token (or unquoted final word).
            try:
                flags_part, _, rest = line.partition(") ")
                flags_part = flags_part.lstrip("(")
                # name is the last token in `rest`, possibly quoted
                rest_stripped = rest.strip()
                # delimiter + name; split off the leading delim quote
                _delim, _, name_token = rest_stripped.partition(" ")
                name = name_token.strip().strip('"')
            except Exception:
                continue
            names_seen.append(name)
            if "\\Sent" in flags_part:
                sent_folder = name
                break
        if sent_folder is None:
            for cand in _SENT_NAME_CANDIDATES:
                if cand in names_seen:
                    sent_folder = cand
                    break
        if sent_folder is None:
            log.warning("email_imap append: no Sent folder found (saw %s)", names_seen[:8])
            return None
        # imaplib.APPEND wants: mailbox, flags, date_time, message
        typ, _ = conn.append(
            sent_folder,
            "(\\Seen)",
            imaplib.Time2Internaldate(datetime.now()),
            msg_bytes,
        )
        if typ != "OK":
            log.warning("email_imap append: APPEND returned %s", typ)
            return None
        log.info("email_imap append: appended to '%s'", sent_folder)
        return sent_folder
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _decode_header(h: Optional[str]) -> str:
    if not h:
        return ""
    parts = email.header.decode_header(h)
    return "".join(
        (p.decode(enc or "utf-8", errors="replace") if isinstance(p, bytes) else p)
        for p, enc in parts
    )


def _list_recent(c: Dict[str, Any], limit: int = 10, folder: str = "INBOX", unread_only: bool = False) -> Dict[str, Any]:
    conn = _imap_connect(c)
    try:
        conn.select(folder, readonly=True)
        criterion = "UNSEEN" if unread_only else "ALL"
        typ, data = conn.search(None, criterion)
        if typ != "OK":
            return {"ok": False, "error": f"IMAP search failed: {typ}"}
        ids = data[0].split()[-int(limit):][::-1]
        out: List[Dict[str, Any]] = []
        for msg_id in ids:
            typ, msg_data = conn.fetch(msg_id, "(RFC822.HEADER FLAGS UID)")
            if typ != "OK":
                continue
            raw_header = b""
            uid = ""
            unread = True
            for part in msg_data:
                if isinstance(part, tuple):
                    raw_header = part[1]
                    desc = part[0].decode(errors="replace") if isinstance(part[0], bytes) else str(part[0])
                    if "\\Seen" in desc:
                        unread = False
                    if "UID " in desc:
                        try:
                            uid = desc.split("UID ", 1)[1].split()[0].rstrip(")")
                        except IndexError:
                            uid = ""
            parsed = email.message_from_bytes(raw_header)
            date_iso: Optional[str] = None
            try:
                date_iso = parsedate_to_datetime(parsed.get("Date", "")).isoformat()
            except Exception:
                pass
            out.append({
                "uid": uid,
                "subject": _decode_header(parsed.get("Subject")),
                "from": _decode_header(parsed.get("From")),
                "to": _decode_header(parsed.get("To")),
                "date": date_iso,
                "unread": unread,
            })
        return {"folder": folder, "count": len(out), "messages": out}
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _fetch(c: Dict[str, Any], uid: str, folder: str = "INBOX") -> Dict[str, Any]:
    conn = _imap_connect(c)
    try:
        conn.select(folder, readonly=True)
        typ, data = conn.uid("fetch", uid, "(RFC822)")
        if typ != "OK" or not data or not data[0]:
            return {"ok": False, "error": f"could not fetch UID {uid}"}
        raw = data[0][1]
        msg = email.message_from_bytes(raw)
        body_text = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    try:
                        body_text = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace")
                    except Exception:
                        body_text = str(part.get_payload(decode=True))
                    break
        else:
            try:
                body_text = msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", errors="replace")
            except Exception:
                body_text = str(msg.get_payload())
        return {
            "uid": uid,
            "subject": _decode_header(msg.get("Subject")),
            "from": _decode_header(msg.get("From")),
            "to": _decode_header(msg.get("To")),
            "date": msg.get("Date"),
            "body": body_text[:5000],  # cap to keep LLM context reasonable
        }
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _test_connection(c: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"imap": "unknown", "smtp": "unknown"}
    try:
        conn = _imap_connect(c)
        conn.list()
        conn.logout()
        out["imap"] = "ok"
    except Exception as exc:
        out["imap"] = f"failed: {type(exc).__name__}: {exc}"
    try:
        with _smtp_connect(c) as smtp:
            smtp.noop()
        out["smtp"] = "ok"
    except Exception as exc:
        out["smtp"] = f"failed: {type(exc).__name__}: {exc}"
    out["all_ok"] = out["imap"] == "ok" and out["smtp"] == "ok"
    return out


def email_imap(op: str, **kw) -> Dict[str, Any]:
    c = _creds()
    if not c:
        return {
            "ok": False,
            "error": "email-imap not configured — run `install_connector(name='email-imap')` to enter your IMAP/SMTP credentials.",
            "needs_install": True,
        }
    op = (op or "").lower().strip()
    try:
        if op == "send":
            to = kw.get("to") or ""
            if not to:
                return {"ok": False, "error": "send requires 'to'"}
            return _send(
                c, to,
                kw.get("subject", ""), kw.get("body", ""),
                kw.get("cc", ""), kw.get("bcc", ""),
                attachments=kw.get("attachments") or None,
                body_html=kw.get("body_html") or None,
            )
        if op == "list_recent":
            return _list_recent(c, int(kw.get("limit", 10)), kw.get("folder", "INBOX"), bool(kw.get("unread_only", False)))
        if op == "fetch":
            uid = kw.get("uid") or ""
            if not uid:
                return {"ok": False, "error": "fetch requires 'uid'"}
            return _fetch(c, uid, kw.get("folder", "INBOX"))
        if op == "test_connection":
            return _test_connection(c)
        return {"ok": False, "error": f"unknown op '{op}'. Try: send, list_recent, fetch, test_connection."}
    except (imaplib.IMAP4.error, smtplib.SMTPException) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


register(ConnectorSpec(
    name=CONNECTOR_NAME,
    description=(
        "Read and send email via any IMAP/SMTP server (self-hosted, Fastmail, Posteo, mailbox.org, etc.). "
        "For Gmail/Outlook, install email-gmail instead — that one uses OAuth so you don't enter a password. "
        "Operations: {op: 'send', to, subject, body}, {op: 'list_recent', limit, folder, unread_only}, "
        "{op: 'fetch', uid}, {op: 'test_connection'}."
    ),
    params_schema={
        "type": "object",
        "properties": {
            "op": {"type": "string", "enum": ["send", "list_recent", "fetch", "test_connection"]},
            "to": {"type": "string"},
            "cc": {"type": "string"},
            "bcc": {"type": "string"},
            "subject": {"type": "string"},
            "body": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10},
            "folder": {"type": "string", "default": "INBOX"},
            "unread_only": {"type": "boolean", "default": False},
            "uid": {"type": "string"},
        },
        "required": ["op"],
    },
    invoke=email_imap,
    requires_auth=True,
    install_hint=(
        "You'll need your IMAP and SMTP server hostnames + an app password (or your account password). "
        "Common defaults: IMAP port 993 SSL, SMTP port 587 STARTTLS."
    ),
    backend="builtin",
    version="1.0",
    tags=["email", "imap", "smtp", "auth"],
    credentials_schema={
        "type": "object",
        "required": ["imap_host", "smtp_host", "username", "password"],
        "properties": {
            "imap_host":     {"type": "string", "title": "IMAP server",   "description": "e.g. imap.fastmail.com or mail.example.com"},
            "imap_port":     {"type": "integer", "title": "IMAP port",    "default": 993},
            "smtp_host":     {"type": "string", "title": "SMTP server",   "description": "e.g. smtp.fastmail.com"},
            "smtp_port":     {"type": "integer", "title": "SMTP port",    "default": 587, "description": "587 = STARTTLS, 465 = SSL"},
            "username":      {"type": "string", "title": "Username",      "description": "Usually your full email address."},
            "password":      {"type": "string", "title": "Password",      "format": "password", "description": "App password recommended over your real password."},
            "from_address":  {"type": "string", "title": "From address",  "description": "Optional. Defaults to your username."},
            "use_tls":       {"type": "boolean", "title": "Use TLS",      "default": True},
        },
    },
))
