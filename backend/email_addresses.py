"""Which mail addresses a person already knows, and the closest ones to
an address they typed.

Chat test 2026-09-27: "schreib Dirk an seine web.de-Adresse" became
an invented firstname.lastname@ address nobody ever used. prepare_email now
checks the recipient against what the person knows; an unknown one gets
a card with the closest known addresses to pick from or correct.
"""

from __future__ import annotations

import json
import re
from contextvars import ContextVar
from difflib import SequenceMatcher
from typing import Any, Iterable, List, Optional, Set

from .database import get_conn

_ADDR = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

# What the person typed in this chat turn (set by the agent loop), so an
# address they wrote themselves counts as known before it is saved.
current_user_text: ContextVar[str] = ContextVar("current_user_text", default="")


def addresses_in(texts: Iterable[str]) -> Set[str]:
    out: Set[str] = set()
    for t in texts:
        out.update(a.lower() for a in _ADDR.findall(t or ""))
    return out


def known(user_id: Any, role: Optional[str] = None, conversation_id: Optional[str] = None) -> Set[str]:
    uid = str(user_id)
    found: Set[str] = set()
    with get_conn() as conn:
        found.update((r["email"] or "").lower() for r in conn.execute(
            "SELECT email FROM email_accounts WHERE owner_user_id = ?", (uid,)).fetchall())
        try:
            from . import spaces
            frag, params = spaces.row_filter(uid, role, "contacts")
            found.update((r["value"] or "").lower() for r in conn.execute(
                "SELECT ch.value FROM contact_channels ch JOIN contacts ON contacts.id = ch.contact_id "
                f"WHERE ch.kind = 'email' AND {frag}", list(params)).fetchall())
        except Exception:  # noqa: BLE001 — contacts are one source among several
            pass
        found.update((r["from_email"] or "").lower() for r in conn.execute(
            "SELECT DISTINCT from_email FROM email_messages WHERE owner_user_id = ? AND from_email IS NOT NULL",
            (uid,)).fetchall())
        for r in conn.execute("SELECT to_addrs FROM email_messages WHERE owner_user_id = ? AND is_sent = 1", (uid,)):
            try:
                found.update((a.get("email") or "").lower() for a in json.loads(r["to_addrs"] or "[]"))
            except (ValueError, AttributeError, TypeError):
                pass
    texts = [current_user_text.get()]
    if conversation_id:
        try:
            from .agent import conversation_io
            texts += [str(m.get("content") or "") for m in conversation_io.load_messages(conversation_id, uid)
                      if m.get("role") == "user"]
        except Exception:  # noqa: BLE001
            pass
    found.update(addresses_in(texts))
    found.discard("")
    return found


def closest(address: str, candidates: Iterable[str], n: int = 3) -> List[str]:
    """The known addresses most like the typed one (whole address, the
    part before @ weighted up), best first."""
    a = (address or "").lower()
    local = a.split("@")[0]

    def score(c: str) -> float:
        return 0.6 * SequenceMatcher(None, local, c.split("@")[0]).ratio() + 0.4 * SequenceMatcher(None, a, c).ratio()

    ranked = sorted((c for c in candidates if c != a), key=score, reverse=True)
    return [c for c in ranked if score(c) >= 0.5][:n]
