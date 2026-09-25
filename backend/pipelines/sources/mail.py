"""Mail as a pipeline source.

Three questions a pipeline asks of the owner's mail:

* `freshness` — can we trust what is (not) in the database right now?
  A mailbox whose fetch is failing, has not synced for a while, is in
  the middle of an import, or has a mail it could not read since the
  pipeline started, makes every "nothing came" worthless.
* `search` — mails received since the start that might be the answer:
  in the thread, from a known address or domain, from a domain that
  carries the company's name, or mentioning one of the numbers. The
  answer often comes from another address (noreply@service-anbieter.com
  for a mail to kuendigung@anbieter.de), so the thread alone is never
  enough. Every folder counts, spam included.
* `own_replies` — mails the owner sent to the other side themselves,
  not through the pipeline: then the person has taken over.

Only the owner's own accounts are read (`owner_user_id`).
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Optional

from ...database import conn_ctx

# A mailbox counts as current when its last successful sync is younger
# than this. The fetcher reconnects (and records a sync) at least every
# IDLE_REFRESH_S = 25 min; the margin covers one slow reconnect.
FRESH_MAX_AGE_S = 40 * 60

FREEMAIL = {
    "gmail.com", "googlemail.com", "gmx.de", "gmx.net", "gmx.at", "gmx.ch", "web.de",
    "t-online.de", "freenet.de", "outlook.com", "outlook.de", "hotmail.com", "hotmail.de",
    "live.com", "live.de", "yahoo.com", "yahoo.de", "icloud.com", "me.com", "mac.com",
    "posteo.de", "mailbox.org", "aol.com", "aol.de", "protonmail.com", "proton.me", "arcor.de",
}
# Second-level labels that say nothing about the company.
_GENERIC_LABELS = {"mail", "email", "info", "service", "kundenservice", "online", "support",
                   "news", "newsletter", "noreply", "kontakt", "portal", "mein", "my", "shop"}

_NUMBER_RE = re.compile(r"(?<![\w/.-])([A-Za-z0-9][A-Za-z0-9./-]{4,29})(?![\w/-])")
_DATE_RE = re.compile(r"^\d{1,2}[./-]\d{1,2}[./-]\d{2,4}$|^\d{4}-\d{2}-\d{2}$|^\d{1,2}:\d{2}")


def _domain(addr: str) -> str:
    return addr.rsplit("@", 1)[-1].lower().strip() if "@" in addr else ""


def _stem(domain: str) -> Optional[str]:
    """'kundenservice.stadtwerke-musterstadt.de' → 'stadtwerke-musterstadt'."""
    parts = [p for p in domain.split(".") if p]
    if len(parts) < 2:
        return None
    label = parts[-2]
    if len(label) < 4 or label in _GENERIC_LABELS:
        return None
    return label


def extract_numbers(*texts: str, limit: int = 8) -> list[str]:
    """Customer, contract, invoice numbers and the like: tokens of 6+
    characters with at least 4 digits. Dates, times and 5-digit postcodes
    are left out; they match far too much."""
    seen: list[str] = []
    for text in texts:
        for m in _NUMBER_RE.finditer(text or ""):
            tok = m.group(1).strip(".-/")
            digits = sum(ch.isdigit() for ch in tok)
            if len(tok) < 6 or digits < 4 or _DATE_RE.match(tok):
                continue
            # a plain amount like 1.234,56 or a year range is not an identifier
            if re.fullmatch(r"[\d.,]+", tok) and ("," in tok or tok.count(".") > 1):
                continue
            if tok not in seen:
                seen.append(tok)
            if len(seen) >= limit:
                return seen
    return seen


def features_from_mail(mail: dict[str, Any]) -> dict[str, Any]:
    """What the answer to this sent mail will be recognised by."""
    addrs: list[str] = []
    for key in ("to_addrs", "cc_addrs"):
        try:
            for a in json.loads(mail.get(key) or "[]"):
                e = (a.get("email") or "").lower().strip()
                if e and e not in addrs:
                    addrs.append(e)
        except (ValueError, AttributeError):
            pass
    domains, stems = [], []
    for a in addrs:
        d = _domain(a)
        if d and d not in FREEMAIL and d not in domains:
            domains.append(d)
            s = _stem(d)
            if s and s not in stems:
                stems.append(s)
    body = mail.get("body_text") or ""
    return {
        "addresses": addrs,
        "domains": domains,
        "names": stems,
        "numbers": extract_numbers(mail.get("subject") or "", body),
    }


def _owner_accounts(owner: str) -> list[dict[str, Any]]:
    with conn_ctx() as c:
        rows = c.execute(
            "SELECT id, email, display_name, enabled, last_error, sync_state, repair_requested, "
            "       EXTRACT(EPOCH FROM (localtimestamp - NULLIF(last_sync_at, '')::timestamp)) AS sync_age, "
            "       (NULLIF(last_error_at, '')::timestamp >= COALESCE(NULLIF(last_sync_at, '')::timestamp, "
            "        '-infinity'::timestamp)) AS error_newer "
            "FROM email_accounts WHERE owner_user_id = ?",
            (owner,),
        ).fetchall()
    return [dict(r) for r in rows]


def freshness(owner: str, since: datetime) -> dict[str, Any]:
    """{ok, accounts:[{email, ok, problem, sync_age_s}], problems:[...]}."""
    accounts = []
    problems = []
    for a in _owner_accounts(owner):
        if not a["enabled"]:
            continue
        problem = None
        age = a["sync_age"]
        state = {}
        try:
            state = json.loads(a["sync_state"] or "{}")
        except ValueError:
            pass
        if a["error_newer"]:
            problem = f"Abruf meldet einen Fehler: {(a['last_error'] or '')[:120]}"
        elif age is None:
            problem = "noch nie abgerufen"
        elif float(age) > FRESH_MAX_AGE_S:
            problem = f"zuletzt vor {int(float(age) // 60)} Minuten abgerufen"
        elif state.get("pending") or a["repair_requested"]:
            problem = "Abgleich mit dem Server läuft noch"
        if problem is None:
            with conn_ctx() as c:
                bad = c.execute(
                    "SELECT count(*) AS n FROM email_fetch_failures WHERE account_id = ? "
                    "AND NULLIF(first_failed_at, '')::timestamp >= (?::timestamptz AT TIME ZONE current_setting('TimeZone'))",
                    (a["id"], since),
                ).fetchone()
            # A mail that first failed after the start could be the answer.
            if bad and int(bad["n"]):
                problem = f"{int(bad['n'])} Mail(s) konnten nicht gelesen werden"
        accounts.append({"id": int(a["id"]), "email": a["email"], "ok": problem is None,
                         "problem": problem, "sync_age_s": None if age is None else int(float(age))})
        if problem:
            problems.append(f"{a['email']}: {problem}")
    if not accounts:
        problems.append("kein aktives Mailkonto")
    return {"ok": not problems, "accounts": accounts, "problems": problems}


_ISO = r"'^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}'"


def _received_since_sql() -> str:
    # Only well-formed values are cast: one odd legacy string must not
    # fail the whole search (that would read as "nothing came").
    return (f"COALESCE(CASE WHEN m.date_received ~ {_ISO} THEN m.date_received::timestamptz END, "
            f"CASE WHEN m.created_at ~ {_ISO} THEN (m.created_at::timestamp AT TIME ZONE "
            "current_setting('TimeZone')) END) >= ?")


def _addr_list(raw: Optional[str]) -> list[str]:
    try:
        return [(a.get("email") or "").lower() for a in json.loads(raw or "[]")]
    except (ValueError, AttributeError):
        return []


def search(owner: str, since: datetime, features: dict[str, Any], own_ids: list[str],
           exclude_ids: Optional[list[int]] = None, limit: int = 40) -> list[dict[str, Any]]:
    """Received mails since `since` that might be the answer, newest first,
    each with the reasons it was picked."""
    own_ids = [i for i in own_ids if i]
    addresses = [a.lower() for a in features.get("addresses") or [] if a]
    domains = [d.lower() for d in features.get("domains") or [] if d]
    names = [n.lower() for n in features.get("names") or [] if n]
    numbers = [n for n in features.get("numbers") or [] if n]
    words = [w for w in features.get("words") or [] if w]

    ors, params = [], []
    if own_ids:
        ors.append("m.in_reply_to = ANY(?)"); params.append(own_ids)
        ors.append("m.thread_id = ANY(?)"); params.append(own_ids)
        for mid in own_ids:
            ors.append("m.references_ids ILIKE ?"); params.append(f"%{mid}%")
    if addresses:
        ors.append("lower(m.from_email) = ANY(?)"); params.append(addresses)
        ors.append("lower(COALESCE(m.reply_to, '')) = ANY(?)"); params.append(addresses)
    for d in domains:
        ors.append("lower(split_part(m.from_email, '@', 2)) = ?"); params.append(d)
        ors.append("lower(split_part(m.from_email, '@', 2)) LIKE ?"); params.append(f"%.{d}")
    for n in names:
        ors.append("lower(split_part(m.from_email, '@', 2)) LIKE ?"); params.append(f"%{n}%")
    for n in numbers + words:
        ors.append("m.subject ILIKE ?"); params.append(f"%{n}%")
        ors.append("m.body_text ILIKE ?"); params.append(f"%{n}%")
    if not ors:
        return []

    sql = (
        "SELECT m.id, m.account_id, m.message_id, m.in_reply_to, m.references_ids, m.thread_id, "
        "       m.from_email, m.from_name, m.reply_to, m.subject, m.snippet, m.body_text, "
        "       m.date_received, m.category, f.name AS folder "
        "FROM email_messages m LEFT JOIN email_folders f ON f.id = m.folder_id "
        "WHERE m.owner_user_id = ? AND COALESCE(m.is_sent, 0) = 0 AND COALESCE(m.is_draft, 0) = 0 "
        f"AND {_received_since_sql()} "
        f"AND ({' OR '.join(ors)}) "
    )
    head = [owner, since]
    if exclude_ids:
        sql += "AND NOT (m.id = ANY(?)) "
        params.append([int(i) for i in exclude_ids])
    sql += "ORDER BY m.date_received DESC NULLS LAST LIMIT ?"
    params.append(limit)
    with conn_ctx() as c:
        rows = c.execute(sql, (*head, *params)).fetchall()

    out = []
    for r in rows:
        why, strong = [], False
        frm = (r["from_email"] or "").lower()
        dom = _domain(frm)
        refs = r["references_ids"] or ""
        if own_ids and (r["in_reply_to"] in own_ids or r["thread_id"] in own_ids
                        or any(i in refs for i in own_ids)):
            why.append("Antwort im selben Verlauf"); strong = True
        if frm in addresses or (r["reply_to"] or "").lower() in addresses:
            why.append("von der angeschriebenen Adresse"); strong = True
        elif any(dom == d or dom.endswith("." + d) for d in domains):
            why.append(f"von der Domain {dom}"); strong = True
        elif any(n in dom for n in names):
            why.append(f"Absender-Domain {dom} passt zum Namen"); strong = True
        text = f"{r['subject'] or ''}\n{r['body_text'] or ''}"
        for n in numbers:
            if n.lower() in text.lower():
                why.append(f"nennt {n}"); strong = True
        for w in words:
            if w.lower() in text.lower():
                why.append(f"enthält „{w}“")
        out.append({
            "source": "mail",
            "id": int(r["id"]),
            "from": r["from_email"],
            "from_name": r["from_name"],
            "subject": r["subject"],
            "snippet": r["snippet"],
            "date": r["date_received"],
            "folder": r["folder"],
            "category": r["category"],
            "why": why or ["passt zu einem Merkmal"],
            "strong": strong,
        })
    return out


def own_replies(owner: str, since: datetime, features: dict[str, Any],
                own_ids: list[str], origin_message_id: Optional[str]) -> list[dict[str, Any]]:
    """Mails the owner sent to the other side since the start that did
    not come from this pipeline."""
    addresses = [a.lower() for a in features.get("addresses") or [] if a]
    domains = [d.lower() for d in features.get("domains") or [] if d]
    if not addresses and not domains:
        return []
    skip = [i for i in own_ids + [origin_message_id or ""] if i]
    with conn_ctx() as c:
        rows = c.execute(
            "SELECT m.id, m.message_id, m.to_addrs, m.cc_addrs, m.subject, m.date_received "
            "FROM email_messages m WHERE m.owner_user_id = ? AND COALESCE(m.is_sent, 0) = 1 "
            "AND COALESCE(m.is_draft, 0) = 0 "
            f"AND {_received_since_sql()} "
            "AND NOT (COALESCE(m.message_id, '') = ANY(?)) "
            "ORDER BY m.date_received DESC NULLS LAST LIMIT 50",
            (owner, since, skip),
        ).fetchall()
    out = []
    for r in rows:
        rcpts = _addr_list(r["to_addrs"]) + _addr_list(r["cc_addrs"])
        if any(a in addresses for a in rcpts) or any(
                _domain(a) == d or _domain(a).endswith("." + d) for a in rcpts for d in domains):
            out.append({"id": int(r["id"]), "subject": r["subject"], "date": r["date_received"],
                        "to": rcpts})
    return out


def load_sent_mail(owner: str, mail_id: int) -> Optional[dict[str, Any]]:
    with conn_ctx() as c:
        r = c.execute(
            "SELECT m.*, a.email AS account_email FROM email_messages m "
            "JOIN email_accounts a ON a.id = m.account_id "
            "WHERE m.id = ? AND m.owner_user_id = ? AND a.owner_user_id = ?",
            (mail_id, owner, owner),
        ).fetchone()
    return dict(r) if r else None


def message_was_sent(owner: str, message_id: str) -> bool:
    """Our sent mirror (or the server's copy) carries this Message-ID."""
    with conn_ctx() as c:
        r = c.execute("SELECT 1 FROM email_messages WHERE owner_user_id = ? AND message_id = ? LIMIT 1",
                      (owner, message_id.strip("<>"))).fetchone()
    return r is not None


def recent_sent(owner: str, limit: int = 30) -> list[dict[str, Any]]:
    with conn_ctx() as c:
        rows = c.execute(
            "SELECT id, subject, to_addrs, date_received, snippet FROM email_messages "
            "WHERE owner_user_id = ? AND COALESCE(is_sent, 0) = 1 AND COALESCE(is_draft, 0) = 0 "
            "ORDER BY date_received DESC NULLS LAST LIMIT ?",
            (owner, limit),
        ).fetchall()
    return [{"id": int(r["id"]), "subject": r["subject"], "to": _addr_list(r["to_addrs"]),
             "date": r["date_received"], "snippet": r["snippet"]} for r in rows]


def count_since(owner: str, since: datetime) -> int:
    """How many received mails the search looked through (for the report)."""
    with conn_ctx() as c:
        r = c.execute(
            "SELECT count(*) AS n FROM email_messages m WHERE m.owner_user_id = ? "
            f"AND COALESCE(m.is_sent, 0) = 0 AND {_received_since_sql()}",
            (owner, since),
        ).fetchone()
    return int(r["n"]) if r else 0
