"""Mailbox cross-link — enrich contacts with email channels using the
already-indexed IMAP corpus.

Premise: the contact extractor pulls name + address from Paperless docs
but often misses email/phone because SaaS invoices don't print them.
The user's inbox, however, almost certainly contains messages from the
same entities (`billing@calendly.com`, etc.). Cross-linking surfaces
the email as a channel — without web calls, without third parties.

Safety model (the whole reason this module is conservative):

* **Read-only against contacts with existing email channels.** We only
  enrich contacts whose email-channel count is zero. Curated contacts
  with manually-added emails are never touched.
* **High-confidence rules only.** For businesses: the contact's
  normalised name must match the sender's domain root exactly. For
  persons: from_name must fuzzy-match display_name >= 0.85.
* **Source tagging.** Every inserted channel carries
  ``source='mailbox_crosslink'`` so we can audit, query, or undo
  later.
* **Respects UNIQUE (kind, value).** If the email already belongs to
  another contact (e.g. the dedupe-LLM merged into a different row),
  the insert silently no-ops. We never steal channels.
* **Skips role/system addresses for persons.** noreply, notifications,
  mailer-daemon, etc. are never linked to a person contact even if
  the name fuzzy-matches.
"""

from __future__ import annotations

import difflib
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from .database import conn_ctx

log = logging.getLogger(__name__)


# Role / system mailbox prefixes that should never be auto-linked to a
# person contact. We still allow them on business contacts — for a SaaS
# company `billing@calendly.com` IS the channel.
_ROLE_LOCALPARTS = {
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "notifications", "notification", "alerts", "alert",
    "system", "mailer-daemon", "postmaster", "bounce", "bounces",
    "support", "help", "info", "contact", "admin", "service",
    "team", "hello", "auto", "automated", "newsletter",
}

# Business-name normaliser: strip legal forms + punctuation so
# "Calendly LLC" → "calendly", "Mobil Krankenkasse AG" → "mobilkrankenkasse".
_BIZ_SUFFIX_RE = re.compile(
    r"\b("
    r"llc|inc|ltd|limited|corp|corporation|"
    r"gmbh|mbh|ag|kg|ohg|gbr|ug|"
    r"e\.?\s?k\.?|e\.?\s?v\.?|"
    r"se|plc|sa|nv|bv|sarl|spa|srl|sl|"
    r"co|company|holdings?|group|partners"
    r")\b",
    re.IGNORECASE,
)

_ALPHANUM_RE = re.compile(r"[^a-z0-9]+")


def _norm_biz(name: str) -> str:
    """Normalise a business name for domain matching.

    'Calendly LLC' → 'calendly'
    'Mobil Krankenkasse AG' → 'mobilkrankenkasse'
    'BUNDESamt für Strahlenschutz' → 'bundesamtfurstrahlenschutz'

    Strips legal suffixes, lower-cases, removes anything that isn't
    [a-z0-9]. Diacritics are folded to their ASCII base (ü→u, ß→ss,
    etc.) so 'Müller GmbH' and 'mueller-shop.de' line up."""
    if not name:
        return ""
    s = name.lower()
    # Diacritics fold — German-first since that's the main user base,
    # but the rest of the unicode ranges are handled by the alphanum
    # strip below (they just drop).
    s = (s.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
           .replace("ß", "ss").replace("é", "e").replace("è", "e")
           .replace("ê", "e").replace("á", "a").replace("à", "a")
           .replace("ñ", "n").replace("ç", "c"))
    s = _BIZ_SUFFIX_RE.sub("", s)
    s = _ALPHANUM_RE.sub("", s)
    return s


def _domain_root(email: str) -> str:
    """Extract the matchable root from an email's domain.

    'billing@mobil-krankenkasse.de' → 'mobilkrankenkasse'
    'invoices@calendly.com' → 'calendly'
    'no-reply@mail.something.io' → 'something' (strips mail./mta. prefixes)
    """
    if not email or "@" not in email:
        return ""
    host = email.rsplit("@", 1)[-1].lower().strip()
    # Drop common mail-host subdomain prefixes — they're routing
    # infra, not the brand. Keeps 'mail.calendly.com' aligning with
    # 'calendly'.
    host = re.sub(r"^(mail|mta|smtp|email|mailer|bounces?|mg)\.", "", host)
    # Take the second-to-last label as the brand root for typical
    # 2- and 3-segment domains. 'calendly.com' → 'calendly';
    # 'sub.gov.uk' → 'sub'. Imperfect for some ccTLDs but right far
    # more often than wrong, and we're using it as a *signal*, not a
    # ground truth.
    parts = host.split(".")
    if len(parts) >= 2:
        root = parts[-2]
    else:
        root = host
    return _ALPHANUM_RE.sub("", root)


def _localpart(email: str) -> str:
    if not email or "@" not in email:
        return ""
    return email.split("@", 1)[0].lower().strip()


def _is_role_address(email: str) -> bool:
    """True for noreply / system / generic role mailboxes."""
    lp = _localpart(email)
    if not lp:
        return True
    if lp in _ROLE_LOCALPARTS:
        return True
    # Catch '..-noreply', 'noreply-..', 'do_not_reply' variations.
    return any(role in lp for role in ("noreply", "no-reply", "donotreply",
                                       "do-not-reply", "do_not_reply"))


def _fuzzy_person(display_name: str, from_name: str) -> float:
    """Fuzzy ratio for matching a person contact to an email sender's
    display-name. Both names are lower-cased, whitespace-collapsed."""
    a = re.sub(r"\s+", " ", (display_name or "").strip().lower())
    b = re.sub(r"\s+", " ", (from_name or "").strip().lower())
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


# Module-level progress tracker — same pattern as
# contacts_dedupe_llm. UI polls /progress endpoint while running.
_PROGRESS: Dict[str, Dict[str, Any]] = {}


def set_progress(key: str, current: int, total: int, label: str = "") -> None:
    _PROGRESS[key] = {
        "current": int(current),
        "total":   int(total),
        "label":   label[:200],
        "ts":      time.time(),
    }


def get_progress(key: str) -> Dict[str, Any]:
    p = _PROGRESS.get(key)
    if not p:
        return {"current": 0, "total": 0, "label": "", "done": True}
    if time.time() - p.get("ts", 0) > 90:
        _PROGRESS.pop(key, None)
        return {"current": 0, "total": 0, "label": "", "done": True}
    return {**p, "done": False}


def clear_progress(key: str) -> None:
    _PROGRESS.pop(key, None)


def crosslink_once(progress_key: Optional[str] = None) -> Dict[str, Any]:
    """Single pass: scan contacts with no email channel, propose
    high-confidence email links from the IMAP corpus, INSERT the ones
    that pass UNIQUE and confidence rules.

    Idempotent — re-running picks up only contacts that still lack an
    email channel and senders that didn't already get linked.

    Returns ``{scanned, enriched, channels_added, skipped: {reason: n},
    additions: [{contact_id, name, email, sender_name, confidence}]}``."""

    pkey = progress_key or "default"
    started = time.time()

    additions: List[Dict[str, Any]] = []
    skipped: Dict[str, int] = {
        "no_match": 0,            # no candidate sender matched
        "role_for_person": 0,     # match exists but it's a role address
        "fuzzy_below_threshold": 0,
        "already_owned": 0,       # email already belongs to another contact
    }

    with conn_ctx() as c:
        # Step 1: load every distinct sender we know about. Tiny —
        # even a heavy inbox has at most a few thousand distinct
        # senders.
        senders = c.execute(
            # Postgres requires every non-aggregated SELECT column to
            # be in GROUP BY or wrapped in an aggregate. SQLite tolerates
            # it (picks an arbitrary value); wrapping from_email in MIN()
            # gives the same "any value per group" semantics on both.
            "SELECT MIN(from_email) AS from_email, MAX(from_name) AS from_name, "
            "       COUNT(*) AS msg_count "
            "FROM email_messages "
            "WHERE from_email IS NOT NULL AND from_email != '' "
            "GROUP BY LOWER(from_email)"
        ).fetchall()

        # Pre-build the (kind=email, value) lookup so we can short-circuit
        # role-address-already-owned without per-iteration queries.
        owned_emails = {
            r["value"].lower() for r in c.execute(
                "SELECT value FROM contact_channels WHERE kind = 'email'"
            ).fetchall()
        }

        # Index senders for fast lookup. By business-domain-root and by
        # lower-cased from_email. List values handle the (rare) case
        # where the same domain root has multiple distinct senders
        # (info@, billing@, …) — all get considered.
        by_root: Dict[str, List[Dict[str, Any]]] = {}
        for s in senders:
            root = _domain_root(s["from_email"])
            if not root:
                continue
            by_root.setdefault(root, []).append({
                "email": s["from_email"].lower().strip(),
                "name":  (s["from_name"] or "").strip(),
                "count": int(s["msg_count"] or 0),
            })

        # Step 2: enumerate enrichable contacts — no email channel.
        # LEFT JOIN avoids loading the channels table separately.
        rows = c.execute(
            """
            SELECT c.id, c.display_name, c.kind
            FROM contacts c
            LEFT JOIN contact_channels ch
                ON ch.contact_id = c.id AND ch.kind = 'email'
            WHERE ch.id IS NULL
              AND c.status IN ('active', 'pending')
              AND c.display_name IS NOT NULL
              AND TRIM(c.display_name) != ''
            ORDER BY c.id
            """
        ).fetchall()

        total = len(rows)
        set_progress(pkey, 0, total, "starting")

        for i, row in enumerate(rows):
            if i % 25 == 0:  # avoid hammering the progress dict
                set_progress(pkey, i, total,
                             f"checking {row['display_name'][:50]}")

            cid    = int(row["id"])
            name   = row["display_name"]
            kind   = row["kind"]

            # ── Business: name root vs domain root, exact match ──
            if kind == "business":
                norm = _norm_biz(name)
                if not norm:
                    skipped["no_match"] += 1
                    continue

                # Try: contact's normalised name appears as a domain root.
                candidate = by_root.get(norm)
                if not candidate:
                    # Second pass: substring relationships, but only
                    # when one is a clean prefix/suffix of the other
                    # AND the shorter is at least 5 chars (avoid
                    # 'ag', 'co' matches). This catches 'Mobil
                    # Krankenkasse' → 'mobilkrankenkasse' against
                    # domain 'mobilkrankenkassev' (with trailing
                    # suffix from URL) — but the bar is high.
                    matches: List[Dict[str, Any]] = []
                    if len(norm) >= 5:
                        for root, sl in by_root.items():
                            if root == norm:
                                continue  # already missed above
                            if len(root) < 5:
                                continue
                            if root.startswith(norm) or norm.startswith(root):
                                matches.extend(sl)
                    if not matches:
                        skipped["no_match"] += 1
                        continue
                    candidate = matches

                # Pick the highest-message-count sender as the
                # primary — that's the address actually used to
                # contact the user.
                primary = max(candidate, key=lambda s: s["count"])
                email   = primary["email"]

                # Final safety: respect UNIQUE (kind, value). If the
                # email is already attached to another contact, do
                # nothing.
                if email in owned_emails:
                    skipped["already_owned"] += 1
                    continue

                try:
                    c.execute(
                        "INSERT INTO contact_channels "
                        "(contact_id, kind, value, label, source) "
                        "VALUES (?, 'email', ?, 'primary', "
                        "        'mailbox_crosslink')",
                        (cid, email),
                    )
                    owned_emails.add(email)
                    additions.append({
                        "contact_id":   cid,
                        "name":         name,
                        "email":        email,
                        "sender_name":  primary["name"],
                        "confidence":   "high",
                        "kind":         "business",
                    })
                except Exception as exc:  # noqa: BLE001 — IntegrityError mostly
                    log.debug("crosslink: insert skipped %s → %s: %s",
                              cid, email, exc)
                    skipped["already_owned"] += 1
                continue

            # ── Person: fuzzy name match >= 0.85, skip role mailboxes ──
            if kind == "person":
                best_score = 0.0
                best_sender: Optional[Dict[str, Any]] = None

                for senders_for_root in by_root.values():
                    for s in senders_for_root:
                        if _is_role_address(s["email"]):
                            continue
                        score = _fuzzy_person(name, s["name"])
                        if score > best_score:
                            best_score  = score
                            best_sender = s

                if not best_sender or best_score < 0.85:
                    if best_sender and best_score > 0:
                        skipped["fuzzy_below_threshold"] += 1
                    else:
                        skipped["no_match"] += 1
                    continue

                email = best_sender["email"]
                if email in owned_emails:
                    skipped["already_owned"] += 1
                    continue

                try:
                    c.execute(
                        "INSERT INTO contact_channels "
                        "(contact_id, kind, value, label, source) "
                        "VALUES (?, 'email', ?, 'primary', "
                        "        'mailbox_crosslink')",
                        (cid, email),
                    )
                    owned_emails.add(email)
                    additions.append({
                        "contact_id":   cid,
                        "name":         name,
                        "email":        email,
                        "sender_name":  best_sender["name"],
                        "confidence":   f"fuzzy={best_score:.2f}",
                        "kind":         "person",
                    })
                except Exception as exc:  # noqa: BLE001
                    log.debug("crosslink: insert skipped %s → %s: %s",
                              cid, email, exc)
                    skipped["already_owned"] += 1
                continue

            # Other kinds (none today) just count as no_match.
            skipped["no_match"] += 1

    clear_progress(pkey)

    return {
        "scanned":         total,
        "enriched":        len({a["contact_id"] for a in additions}),
        "channels_added":  len(additions),
        "skipped":         skipped,
        "additions":       additions,
        "elapsed_s":       round(time.time() - started, 2),
        "senders_indexed": len(senders),
    }
