"""Pseudonyms: a person, address, phone, chat or file becomes a token.

"Beate Mayer" → person_7, "beate@gmx.net" → email_3, a WhatsApp group →
group_2. The token is the same every time this installation sees the
same value (HMAC-SHA256 with the installation's secret, see
diagnostics.secret), so a report can say "the search for person_7 found
nothing again" without carrying a name. The value is never stored; the
admin can still ask "who is person_7?" locally, which re-derives the
HMAC of every known value and compares (who_is).

The dictionary (build_dictionary) is what the scrubber replaces first:
the people of the house, every contact with their addresses and
channels, the mailboxes, the chats, the senders of the person's mail.
Inflections ("Beates", "bei Mayers") are matched by the scrubber, the
token belongs to the base value.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Tuple

from backend.database import get_conn

log = logging.getLogger("yorik.diagnostics")

KINDS = ("person", "email", "phone", "iban", "chat", "group", "mailbox", "file", "secret", "org")
TOKEN_RE = re.compile(r"^(?:" + "|".join(KINDS) + r")_\d+$")
MIN_NAME_LEN = 3            # "Jo" is not worth a token; "Mai" is (a person, not the month, in a contact list)


def _fold(s: str) -> str:
    """Lowercase without diacritics: Müller, Mueller and MULLER share a token."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", s.replace("ß", "ss").casefold()).strip()


def normalise(kind: str, value: str) -> str:
    v = (value or "").strip()
    if not v:
        return ""
    if kind in ("email", "mailbox"):
        return v.casefold()
    if kind == "phone":
        digits = re.sub(r"\D", "", v)
        if digits.startswith("00"):
            digits = digits[2:]
        elif digits.startswith("0") and len(digits) > 4:
            from backend.household_settings import get_setting
            cc = {"DE": "49", "AT": "43", "CH": "41"}.get((get_setting("locale.country") or "DE").upper(), "49")
            digits = cc + digits[1:]
        return digits
    if kind == "iban":
        return re.sub(r"\s+", "", v).upper()
    if kind in ("chat", "group"):
        return v.casefold()
    if kind == "file":
        return os.path.basename(v).casefold()
    if kind == "secret":
        return v
    return _fold(v)


def _hmac(kind: str, norm: str) -> str:
    from . import secret
    return hmac.new(secret(), f"{kind}\0{norm}".encode("utf-8"), hashlib.sha256).hexdigest()


def token_for(kind: str, value: str, conn=None) -> Optional[str]:
    """The token of this value, made on first sight (kind_n, n counting
    up per kind). None for an empty value or an unknown kind."""
    if kind not in KINDS:
        raise ValueError(f"unknown pseudonym kind {kind!r}")
    norm = normalise(kind, value)
    if not norm:
        return None
    digest = _hmac(kind, norm)
    if conn is not None:
        return _token_in(conn, kind, digest)
    with get_conn() as c:
        tok = _token_in(c, kind, digest)
        c.commit()
        return tok


def _token_in(c, kind: str, digest: str) -> str:
    row = c.execute("SELECT token FROM diag_pseudonyms WHERE kind = ? AND hmac = ?", (kind, digest)).fetchone()
    if row:
        return row["token"]
    for _ in range(5):
        n = (c.execute("SELECT count(*) AS n FROM diag_pseudonyms WHERE kind = ?", (kind,)).fetchone()["n"] or 0) + 1
        token = f"{kind}_{n}"
        r = c.execute("INSERT INTO diag_pseudonyms (kind, hmac, token) VALUES (?, ?, ?) "
                      "ON CONFLICT DO NOTHING RETURNING token", (kind, digest, token)).fetchone()
        if r:
            return r["token"]
        row = c.execute("SELECT token FROM diag_pseudonyms WHERE kind = ? AND hmac = ?", (kind, digest)).fetchone()
        if row:
            return row["token"]
    raise RuntimeError("could not assign a pseudonym")


def build_dictionary(user_id: str) -> List[Tuple[str, str, str]]:
    """Every value the scrubber must replace for this person: (kind,
    value), longest first. Their own rows only where rows have an owner
    (mail accounts, chats, senders); contacts and household members are
    shared by nature and go in for everyone."""
    out: Dict[Tuple[str, str], str] = {}        # (kind, value) → canonical value

    def add(kind: str, value: Any, canonical: Optional[str] = None) -> None:
        v = str(value or "").strip()
        if not v:
            return
        if kind == "person" and len(v) < MIN_NAME_LEN:
            return
        key = (kind, v)
        if key not in out or (canonical and out[key] == v and canonical != v):
            out[key] = canonical or v

    def person_parts(*names: Any) -> None:
        """Full names and their parts; a part points at the longest full
        name it was seen in, so first name and full name share a token."""
        full = [str(n).strip() for n in names if n and str(n).strip()]
        canonical = max(full, key=len) if full else ""
        for n in full:
            add("person", n, canonical if " " in canonical else None)
            for part in re.split(r"[\s,/]+", n):
                if len(part) >= MIN_NAME_LEN and not part.lower() in _NAME_STOP:
                    add("person", part, canonical)

    with get_conn() as conn:
        for r in conn.execute("SELECT name, first_name, last_name, email, phone, iban, address_street, business_name "
                              "FROM user_profiles").fetchall():
            person_parts(r["name"], r["first_name"], r["last_name"],
                         " ".join(x for x in (r["first_name"], r["last_name"]) if x))
            add("email", r["email"]); add("phone", r["phone"]); add("iban", r["iban"])
            add("org", r["business_name"])
            if r["address_street"]:
                add("org", r["address_street"])          # a street with a number is as telling as a name
        for r in conn.execute("SELECT id, display_name, first_name, last_name, legal_name, aliases, iban FROM contacts "
                              "WHERE status IN ('active', 'pending') AND merged_into_id IS NULL").fetchall():
            person_parts(r["display_name"], r["first_name"], r["last_name"], r["legal_name"])
            add("iban", r["iban"])
            try:
                import json
                for a in json.loads(r["aliases"] or "[]"):
                    person_parts(a)
            except ValueError:
                pass
        for r in conn.execute("SELECT kind, value FROM contact_channels").fetchall():
            k = {"email": "email", "phone": "phone", "whatsapp": "phone", "sms": "phone", "signal": "phone",
                 "telegram": "chat"}.get(r["kind"])
            if k:
                add(k, r["value"])
        for r in conn.execute("SELECT line1, line2 FROM contact_addresses").fetchall():
            add("org", r["line1"]); add("org", r["line2"])
        for r in conn.execute("SELECT email, display_name FROM email_accounts WHERE owner_user_id = ?", (user_id,)).fetchall():
            add("mailbox", r["email"]); person_parts(r["display_name"])
        for r in conn.execute("SELECT jid, name, is_group FROM wa_chats WHERE owner_user_id = ?", (user_id,)).fetchall():
            add("group" if r["is_group"] else "chat", r["jid"])
            if r["is_group"]:
                add("group", r["name"])
            else:
                person_parts(r["name"])
        try:
            for r in conn.execute("SELECT me_jid, pushname FROM wa_self_identity WHERE owner_user_id = ?", (user_id,)).fetchall():
                add("chat", r["me_jid"]); person_parts(r["pushname"])
        except Exception:  # noqa: BLE001 — table shape differs on old installs
            pass
        for r in conn.execute("SELECT DISTINCT from_name, from_email FROM email_messages WHERE owner_user_id = ? "
                              "AND date_received > to_char(now() - interval '400 days', 'YYYY-MM-DD')", (user_id,)).fetchall():
            add("email", r["from_email"]); person_parts(r["from_name"])
    # longest first, so "Beate Mayer" is replaced before "Beate"
    return sorted(((k, v, c) for (k, v), c in out.items()), key=lambda e: -len(e[1]))


# Name parts that are words, not people: a contact "Praxis Dr. Weiss"
# must not make every "Praxis" in a report a person.
_NAME_STOP = set("""
praxis dr. dr prof gmbh ag kg ohg ug e.v ev inc ltd llc und and von van der die das the mr mrs ms herr frau
familie family team support service info kundenservice shop store online news newsletter noreply no-reply
""".split())


def who_is(token: str, user_id: str) -> List[Dict[str, str]]:
    """Which known value has this token — computed on the spot from the
    dictionary, nothing stored in clear. For the admin's own screen only;
    the call is logged."""
    if not TOKEN_RE.match(token or ""):
        return []
    kind = token.rsplit("_", 1)[0]
    with get_conn() as conn:
        row = conn.execute("SELECT hmac FROM diag_pseudonyms WHERE token = ?", (token,)).fetchone()
    if not row:
        return []
    target = row["hmac"]
    hits = []
    for k, v, canonical in build_dictionary(user_id):
        if k == kind and _hmac(k, normalise(k, canonical)) == target and canonical == v:
            hits.append({"kind": k, "value": v})
    log.info("diagnostics: pseudonym %s resolved locally by user %s (%d match)", token, user_id, len(hits))
    return hits


def tokens_in(text: str) -> List[str]:
    return sorted(set(re.findall(r"\b(?:" + "|".join(KINDS) + r")_\d+\b", text or "")))
