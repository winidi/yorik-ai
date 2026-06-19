"""Per-user email-sender blocklist — used by the email_classifier to
silently drop notifications from senders the user marked as spam.

Public surface kept narrow: add_sender / add_domain / matches /
list_for_user / remove. The "matches" check is what the classifier
calls per inbound mail — must be cheap, and is (indexed lookups).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .database import get_conn

log = logging.getLogger("yorik.email_blocklist")


def _normalise_address(addr: str | None) -> Optional[str]:
    if not addr:
        return None
    s = addr.strip().lower()
    return s or None


def _normalise_domain(dom: str | None) -> Optional[str]:
    if not dom:
        return None
    s = dom.strip().lower().lstrip("@")
    return s or None


def _domain_of(addr: str | None) -> Optional[str]:
    """Pull the domain out of an email address (lowercase, no '@')."""
    addr_n = _normalise_address(addr)
    if not addr_n or "@" not in addr_n:
        return None
    return addr_n.rsplit("@", 1)[1] or None


def add_sender(user_id: str, sender_address: str, reason: Optional[str] = None) -> int:
    """Block this exact address for this user. Returns the row id, or
    the existing row's id if it's already blocked (idempotent)."""
    addr = _normalise_address(sender_address)
    if not addr:
        raise ValueError("sender_address is required")
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM email_blocklist "
            "WHERE user_id=? AND sender_address=?",
            (user_id, addr),
        ).fetchone()
        if existing:
            return int(existing["id"])
        cur = conn.execute(
            "INSERT INTO email_blocklist (user_id, sender_address, reason) "
            "VALUES (?, ?, ?)",
            (user_id, addr, reason),
        )
        conn.commit()
        return int(cur.lastrowid)


def add_domain(user_id: str, sender_domain: str, reason: Optional[str] = None) -> int:
    """Block the whole domain for this user. Returns the row id."""
    dom = _normalise_domain(sender_domain)
    if not dom:
        raise ValueError("sender_domain is required")
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM email_blocklist "
            "WHERE user_id=? AND sender_domain=?",
            (user_id, dom),
        ).fetchone()
        if existing:
            return int(existing["id"])
        cur = conn.execute(
            "INSERT INTO email_blocklist (user_id, sender_domain, reason) "
            "VALUES (?, ?, ?)",
            (user_id, dom, reason),
        )
        conn.commit()
        return int(cur.lastrowid)


def matches(user_id: str, sender_address: Optional[str]) -> bool:
    """True if the address is blocked for this user, either exactly OR
    via its domain. Called per inbound mail — keep fast."""
    addr = _normalise_address(sender_address)
    if not addr:
        return False
    dom = _domain_of(addr)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM email_blocklist "
            "WHERE user_id=? AND ("
            "    sender_address = ? "
            " OR (sender_domain IS NOT NULL AND sender_domain = ?)"
            ") LIMIT 1",
            (user_id, addr, dom),
        ).fetchone()
    return row is not None


def list_for_user(user_id: str) -> list[dict[str, Any]]:
    """Newest first."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, sender_address, sender_domain, reason, created_at "
            "FROM email_blocklist WHERE user_id=? "
            "ORDER BY id DESC",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def remove(user_id: str, blocklist_id: int) -> bool:
    """Unblock — used by the settings page if/when we build it."""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM email_blocklist WHERE id=? AND user_id=?",
            (blocklist_id, user_id),
        )
        conn.commit()
    return (cur.rowcount or 0) > 0


def domain_of(sender_address: str | None) -> Optional[str]:
    """Public helper — the routes layer needs to derive 'gmx.de' from
    'abrechnung@gmx.de' when the user picked block-the-domain."""
    return _domain_of(sender_address)
