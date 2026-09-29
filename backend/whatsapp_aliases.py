"""One person, one WhatsApp chat.

WhatsApp addresses a person two ways: by phone number
(``4917…@s.whatsapp.net``) and by an opaque "LID" (``2664…@lid``).
Messages arrive under either, so until 2026-09-29 Yorik showed two chats
per person — "Beate <3 · …5916" and "Beate <3 · @lid …1262" — and a
reply sent from Yorik landed in one while the answer came in the other.

The bridge learns which LID belongs to which number from the address
book (``alias-map.json``, 500+ pairs on the live box). This module reads
that map and lets the chat list, a thread and the drafts treat both
addresses as one chat, keyed by the phone number. Nothing stored is
rewritten: the merge happens when reading, so it can be undone by
removing it.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Dict, List

log = logging.getLogger("yorik.whatsapp.aliases")

_TTL_S = 300
_cache: Dict[str, tuple[float, Dict[str, str]]] = {}


def _setting_key(user_id: str) -> str:
    return f"wa_lid_aliases_{user_id}"


def _from_bridge(user_id: str) -> Dict[str, str] | None:
    import requests
    from .whatsapp import _bridge_headers
    base = os.getenv("YORIK_WA_BRIDGE_URL", "http://127.0.0.1:3015")
    try:
        r = requests.get(f"{base}/users/{user_id}/contact-names", headers=_bridge_headers(), timeout=5)
        if r.status_code != 200:
            return None
        raw = (r.json() or {}).get("aliases") or {}
    except Exception as exc:  # noqa: BLE001 — bridge down: use the stored copy
        log.debug("alias map: bridge unreachable: %s", exc)
        return None
    return {str(l).lower(): str(p).lower() for l, p in raw.items()
            if isinstance(l, str) and isinstance(p, str) and l.endswith("@lid") and p.endswith("@s.whatsapp.net")}


def lid_to_number(user_id: str) -> Dict[str, str]:
    """{lid_jid: phone_jid} for this person's WhatsApp. Cached for a few
    minutes; the last good copy is kept in app_settings so a stopped
    bridge does not split every chat again."""
    if not user_id:
        return {}
    uid = str(user_id)
    hit = _cache.get(uid)
    if hit and time.monotonic() - hit[0] < _TTL_S:
        return hit[1]
    from .database import get_conn
    fresh = _from_bridge(uid)
    if fresh:
        try:
            with get_conn() as conn:
                conn.execute("INSERT OR REPLACE INTO app_settings (key, value, updated_at) "
                             "VALUES (?, ?, datetime('now'))", (_setting_key(uid), json.dumps(fresh)))
                conn.commit()
        except Exception as exc:  # noqa: BLE001
            log.debug("alias map: could not store: %s", exc)
        mapping = fresh
    else:
        mapping = {}
        try:
            with get_conn() as conn:
                row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (_setting_key(uid),)).fetchone()
            mapping = json.loads(row["value"]) if row else {}
        except Exception:  # noqa: BLE001
            mapping = {}
    _cache[uid] = (time.monotonic(), mapping)
    return mapping


def forget(user_id: str | None = None) -> None:
    """Drop the cached map (tests; after a re-pairing)."""
    if user_id is None:
        _cache.clear()
    else:
        _cache.pop(str(user_id), None)


def canonical(user_id: str, jid: str) -> str:
    """The phone-number address when this LID has one, else the jid."""
    j = (jid or "").lower()
    return lid_to_number(user_id).get(j, jid)


def same_chat(user_id: str, jid: str) -> List[str]:
    """Every address that belongs to the same chat as `jid`: the jid
    itself, its phone number and every LID mapped to that number."""
    if not jid:
        return []
    m = lid_to_number(user_id)
    base = m.get(jid.lower(), jid)
    out = [jid]
    if base != jid:
        out.append(base)
    out += [lid for lid, pn in m.items() if pn == base.lower() and lid not in out]
    return out
