"""Contacts module — the identity hub.

One row per person or business the household has ever interacted with.
Skills, the email fetcher, the WhatsApp bridge, and Compose all use the
same helpers in this file so there's exactly one place that knows how
to normalise an email address, look up a contact by channel value, or
spam-filter a sender.

Tables (migration 008):
  - contacts             (the identity row)
  - contact_channels     (email / phone / whatsapp / …; UNIQUE on kind+value)
  - contact_addresses    (home / work / billing / shipping)

Status lifecycle:
  active   ─ user explicitly created OR auto-promoted (replied/addressed)
  pending  ─ auto-created from inbound channel; awaits user confirmation
  spam     ─ matched a no-reply/transactional rule, or user blocked
  archived ─ user soft-deleted; preserve invoice/email history
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .database import conn_ctx

log = logging.getLogger("yorik.contacts")


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def normalize_email(value: str) -> str:
    """Lower-case + trim. Doesn't validate; that's the caller's job."""
    return (value or "").strip().lower()


_PHONE_KEEP = re.compile(r"[+\d]")


def normalize_phone(value: str) -> str:
    """Strip everything but digits and a leading +. Anything more clever
    (E.164 inference) needs the country code, which we don't always have."""
    s = "".join(c for c in (value or "") if _PHONE_KEEP.match(c))
    return s


def normalize_channel(kind: str, value: str) -> str:
    """Channel-aware normalisation. Used everywhere that writes or
    looks up a contact_channels row so duplicates collapse cleanly.

    WhatsApp is special: the value is the FULL jid (including the
    @s.whatsapp.net or @lid suffix). Earlier versions stored just the
    digits which made @lid pseudo-jids indistinguishable from real
    phone numbers — that's how messages to 'Tom' ended up landing on
    the user's brother. If a bare phone number is passed (no @-suffix),
    we assume it's a real phone and append @s.whatsapp.net.
    """
    if kind == "email":
        return normalize_email(value)
    if kind == "whatsapp":
        v = (value or "").strip()
        if not v:
            return ""
        if "@" in v:
            # Already a jid — keep as-is, just lower-case the suffix.
            local, _, suffix = v.partition("@")
            digits = "".join(c for c in local if c.isdigit())
            return f"{digits}@{suffix.lower()}"
        # Bare phone or digits — assume real phone account.
        digits = normalize_phone(v).lstrip("+")
        return f"{digits}@s.whatsapp.net" if digits else ""
    if kind in ("phone", "sms", "signal"):
        return normalize_phone(value)
    return (value or "").strip()


# ---------------------------------------------------------------------------
# Spam funnel — transactional / no-reply auto-suppression
# ---------------------------------------------------------------------------

# Local-part patterns that are virtually always non-human. If the
# left side of the '@' matches one of these, the sender goes straight
# to status='spam' — they never clutter the Pending tab.
_TRANSACTIONAL_LOCALPART_RE = re.compile(
    r"^(?:"
    r"no[\-_]?reply|noreply|do[\-_]?not[\-_]?reply"
    r"|notifications?|info|support|hello|hi|contact"
    r"|automated|alerts?|news|newsletter|updates?"
    r"|mailer[\-_]?daemon|postmaster|abuse"
    r"|bounce|bounces|return|returns"
    r"|billing|invoice|invoices|receipts?"
    r"|orders?|shipping|tracking|delivery"
    r"|root|daemon|admin|webmaster"
    # Role accounts that aren't a single person — catch the long
    # tail of "service@", "member@", "team@", "marketing@", "sales@",
    # "growth@", "presse@", "careers@", "events@", and the German
    # "mein-*@" prefix (mein-ebay@…) that re-engagement mail uses.
    r"|service|member|account|team"
    r"|marketing|sales|growth|sdr|bdr|biz|partnerships?"
    r"|press|presse|careers|jobs|hr|recruiting"
    r"|events?|webinar|register|subscribe|subscriptions?"
    r"|community|welcome|onboarding|feedback"
    r"|help|helpdesk|help[\-_.]desk|digest"
    r"|daily|weekly|monthly"
    r"|email|mail|mailing|mailings"
    r"|customer[\-_.]reviews?|customer[\-_.]service"
    r"|mein[\-_]?[a-z]+"
    r")(?:@|\+|$)",
    re.IGNORECASE,
)


# Mass-mailer DOMAIN substrings. Any sender whose domain contains one
# of these is treated as a business mass-mailer: a single business
# contact per domain (deduplicated at autocapture time), status='spam'
# so it doesn't clutter Pending. Different rule from
# _TRANSACTIONAL_LOCALPART_RE which skips contact creation entirely —
# here we DO create one contact per domain so future emails from the
# same brand attach to it and the inbox can show "from <brand>" instead
# of "from <raw email>". Conservative list — only domains where 95%+
# of inbound is promotional, transactional, or sales blast. Match is
# substring-on-domain (case insensitive).
_MASS_MAILER_DOMAINS = (
    # Big retail / marketplaces
    "ebay", "amazon", "etsy", "aliexpress", "temu", "wish",
    "bonprix", "otto.de", "zalando", "shein", "boohoo",
    # Payment / financial-marketing (their TRANSACTIONAL mail uses
    # role-account local-parts already filtered by the regex above;
    # this catches the marketing blasts)
    "paypal", "klarna", "stripe.email",
    # Shipping / logistics — same logic as payment
    "dhl", "ups.com", "fedex", "hermesworld", "dpd.",
    # Travel / booking
    "airbnb", "booking.com", "uber.com", "lyft.com", "expedia",
    # Marketing-automation senders (when the sending domain itself
    # is the platform, the message is by definition outbound marketing)
    "mailchimp", "sendgrid", "mailgun", "hubspot.com",
    "intercom", "mandrillapp",
    # Newsletter / creator platforms — the from-domain IS the platform,
    # which means whatever this message is, it's outbound bulk.
    "beehiiv", "substack", "convertkit", "ghost.io",
    "buttondown", "revue.email",
    # Big social — they all send transactional mail but also a lot of
    # "follow suggestions", "look who's posted" promo. Folder into
    # one business+spam contact per platform rather than letting each
    # subdomain pile up.
    "instagram.com", "facebook.com", "facebookmail.com",
    "linkedin.com", "twitter.com", "x.com", "pinterest",
    "tiktok.com", "discord.com", "discord-mail",
    # Subdomain markers — any domain containing these is a
    # marketing/notification mailer, regardless of root domain.
    "newsletter.", "marketing.", "mailing.", "mailings.",
    "notifications.", ".email.", "news.", "info.", "hello.",
    "deals.", "promo.", "offers.", "campaigns.",
)


def is_mass_mailer_email(email: str) -> bool:
    """Heuristic: does this sender look like a marketing/promotional
    mass-mailer (ebay, amazon, paypal, bonprix, …)?

    Used by the autocapture pipeline to consolidate per-domain into a
    single business contact instead of letting each per-address
    variation pile up in Pending. Conservative — when in doubt,
    returns False and the standard pending-person path runs."""
    e = normalize_email(email)
    if not e or "@" not in e:
        return False
    domain = e.split("@", 1)[1]
    return any(pat in domain for pat in _MASS_MAILER_DOMAINS)


def find_business_by_email_domain(domain: str) -> Optional[Dict[str, Any]]:
    """Find an existing business-kind contact whose email channel ends
    in @<domain>. Used to consolidate mass-mailer variants — "ebay@",
    "member@", "noreply@" on ebay.com all attach to the same row.

    Falls back to None when nothing matches. Case-insensitive on the
    domain. Returns the first match (channels are unique per address,
    but multiple per contact)."""
    d = (domain or "").strip().lower()
    if not d:
        return None
    with conn_ctx() as c:
        row = c.execute(
            "SELECT c.* FROM contacts c "
            "JOIN contact_channels ch ON ch.contact_id = c.id "
            "WHERE c.kind = 'business' "
            "  AND ch.kind = 'email' "
            "  AND LOWER(ch.value) LIKE ? "
            "ORDER BY c.id ASC LIMIT 1",
            (f"%@{d}",),
        ).fetchone()
        return _to_contact_dict(row) if row else None


def is_transactional_email(email: str) -> bool:
    """Heuristic: does this email address look automated rather than personal?

    Used by the email autocapture pipeline to decide pending vs spam.
    Conservative: when in doubt, returns False (let the user decide).
    """
    e = normalize_email(email)
    if not e or "@" not in e:
        return False
    local = e.split("@", 1)[0]
    return bool(_TRANSACTIONAL_LOCALPART_RE.match(local))


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


def find_by_channel(kind: str, value: str) -> Optional[Dict[str, Any]]:
    """Return the contact row whose channel matches (kind, normalised value).

    One indexed hit — the UNIQUE(kind, value) constraint makes this O(log n)
    even at 10,000+ channels. Returns None if no match.
    """
    normalised = normalize_channel(kind, value)
    if not normalised:
        return None
    with conn_ctx() as c:
        row = c.execute(
            "SELECT c.* FROM contacts c "
            "JOIN contact_channels ch ON ch.contact_id = c.id "
            "WHERE ch.kind = ? AND ch.value = ?",
            (kind, normalised),
        ).fetchone()
    return _to_contact_dict(row) if row else None


def _visibility_clause(role: Optional[str], user_id: Optional[int]) -> tuple[str, list]:
    """Build the WHERE-fragment + params filtering contacts to rows the
    caller can see. Phase B (2026-06-02): visibility is decided by the
    contact's space membership + row_shares + ownership.

    role=None AND user_id=None → no filter (trusted internal caller).
    role='platform_admin' → no filter (infra admin sees every workspace).
    role='admin' → goes through spaces.row_filter — workspace-scoped to
                   workspaces the user owns (Phase C T10).
    Otherwise → spaces.row_filter does the work, plus a fallback for
    rows that haven't been space_id-backfilled (defence-in-depth; should
    be empty after migration 036).
    """
    if role is None and user_id is None:
        return "", []
    if (role or "").strip().lower() == "platform_admin":
        return "", []
    from backend import spaces as _sp
    frag, params = _sp.row_filter(user_id, role, "contacts", table_alias="contacts")
    # Spaces handles owner + space + row_shares. Anything else without a
    # space_id (i.e. created post-Phase-B-but-pre-default-set) silently
    # disappears, which is correct behaviour — unsaved rows shouldn't
    # leak across users.
    return frag, params


def search(
    query: str = "",
    *,
    kind: Optional[str] = None,
    status: Optional[str] = "active",
    limit: int = 20,
    role: Optional[str] = None,
    user_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Universal substring search across every text field a user might
    type — name, aliases, relation, organisation/legal name, notes,
    tags, AND linked channels (email/phone/whatsapp/website/social) and
    addresses (street/postcode/city/region/country).

    Empty query lists all matching the status/kind filters, ordered by
    pinned-first, last_used_at DESC, then alphabetical.

    Status filter defaults to 'active' so autocomplete never surfaces
    pending or spam rows. Pass status=None to include everything (the
    /r/contacts UI does this when rendering all tabs).
    """
    q = (query or "").strip()
    where: List[str] = []
    params: List[Any] = []
    vis_clause, vis_params = _visibility_clause(role, user_id)
    if vis_clause:
        where.append(vis_clause)
        params.extend(vis_params)
    if status:
        where.append("status = ?")
        params.append(status)
    if kind:
        where.append("kind = ?")
        params.append(kind)
    if q:
        q_lower = q.lower()
        like = f"%{q_lower}%"
        # Phone-style fallback: when the user types "030 123 456", their
        # DB-stored numbers are E.164 ("+4930123456"). Strip every non-
        # digit and try substring on that too so digits-only queries
        # still find the right row. Only kicks in when the input has at
        # least 3 digits — random short numerics shouldn't widen the net.
        digits_only = "".join(ch for ch in q_lower if ch.isdigit())
        digits_like: Optional[str] = (
            f"%{digits_only}%" if len(digits_only) >= 3 else None
        )

        # All conditions are OR'd; any single match across name, channel,
        # or address pulls the row into the result set. EXISTS keeps
        # the channels/addresses lookups from multiplying rows.
        sub_clauses = [
            "LOWER(display_name)            LIKE ?",
            "LOWER(IFNULL(aliases,''))      LIKE ?",
            "LOWER(IFNULL(relation,''))     LIKE ?",
            "LOWER(IFNULL(legal_name,''))   LIKE ?",
            "LOWER(IFNULL(notes,''))        LIKE ?",
            "LOWER(IFNULL(tags,''))         LIKE ?",
            "LOWER(IFNULL(tax_id,''))       LIKE ?",
            "EXISTS (SELECT 1 FROM contact_channels c "
            "        WHERE c.contact_id = contacts.id "
            "          AND LOWER(c.value) LIKE ?)",
            "EXISTS (SELECT 1 FROM contact_addresses a "
            "        WHERE a.contact_id = contacts.id "
            "          AND (LOWER(IFNULL(a.line1,''))    LIKE ? "
            "            OR LOWER(IFNULL(a.line2,''))    LIKE ? "
            "            OR LOWER(IFNULL(a.postcode,'')) LIKE ? "
            "            OR LOWER(IFNULL(a.city,''))     LIKE ? "
            "            OR LOWER(IFNULL(a.region,''))   LIKE ? "
            "            OR LOWER(IFNULL(a.country,''))  LIKE ?))",
        ]
        sub_params: List[Any] = [
            like, like, like, like, like, like, like,
            like,
            like, like, like, like, like, like,
        ]
        if digits_like:
            # Digits-only variant — channels (phone) AND addresses
            # (postcode written without spaces).
            sub_clauses.append(
                "EXISTS (SELECT 1 FROM contact_channels c "
                "        WHERE c.contact_id = contacts.id "
                "          AND REPLACE(REPLACE(REPLACE(REPLACE(c.value,' ',''),'-',''),'(',''),')','') LIKE ?)"
            )
            sub_params.append(digits_like)
        where.append("(" + " OR ".join(sub_clauses) + ")")
        params.extend(sub_params)
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    # Pinned-first (mig 025) — manually-pinned contacts bubble to the
    # top regardless of recency. Then warmest (last_used_at DESC), then
    # alphabetical. Tolerant of pre-025 DBs (COALESCE → 0).
    sql = (
        f"SELECT * FROM contacts {where_sql} "
        f"ORDER BY COALESCE(pinned, 0) DESC, "
        f"         (last_used_at IS NULL), last_used_at DESC, display_name ASC "
        f"LIMIT ?"
    )
    params.append(int(limit))
    with conn_ctx() as c:
        rows = c.execute(sql, params).fetchall()
    return [_to_contact_dict(r) for r in rows]


def get(contact_id: int, *, include_children: bool = True,
        role: Optional[str] = None,
        user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Fetch one contact by id, optionally hydrating channels + addresses.

    Phase 9.4: when role/user_id are passed, returns None for contacts
    the caller can't see (same shape as 'not found') so callers can't
    probe for the existence of private contacts."""
    with conn_ctx() as c:
        row = c.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
        if not row:
            return None
        contact_dict_raw = dict(row)
        # Visibility check — if role/user_id passed, gate by the same
        # rules as search().
        if role is not None or user_id is not None:
            from backend.calendars import require_contact_access, RowOwnerPermissionError
            try:
                require_contact_access(role, user_id, contact_dict_raw, action="view")
            except RowOwnerPermissionError:
                return None
        out = _to_contact_dict(row)
        if include_children:
            out["channels"] = [
                _to_channel_dict(r)
                for r in c.execute(
                    "SELECT * FROM contact_channels WHERE contact_id = ? ORDER BY id",
                    (contact_id,),
                ).fetchall()
            ]
            out["addresses"] = [
                _to_address_dict(r)
                for r in c.execute(
                    "SELECT * FROM contact_addresses WHERE contact_id = ? ORDER BY id",
                    (contact_id,),
                ).fetchall()
            ]
        return out


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


def create(
    *,
    display_name: str,
    kind: str = "person",
    status: str = "active",
    # Person identity (mig 045). first_name is canonical for persons;
    # last_name + role are optional; employer_contact_id links to a
    # kind='business' contact when this person is reached through one.
    # All four stay NULL for kind='business' rows.
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    role: Optional[str] = None,
    employer_contact_id: Optional[int] = None,
    aliases: Optional[List[str]] = None,
    relation: Optional[str] = None,
    birthday: Optional[str] = None,
    language_pref: Optional[str] = None,
    salutation_pref: Optional[str] = None,
    legal_name: Optional[str] = None,
    tax_id: Optional[str] = None,
    iban: Optional[str] = None,
    payment_terms_days: Optional[int] = None,
    default_currency: Optional[str] = None,
    notes: Optional[str] = None,
    tags: Optional[List[str]] = None,
    created_by_user_id: Optional[int] = None,
    source: str = "manual",
    space_id: Optional[int] = None,
) -> int:
    """Insert a contact row. Returns the new id.

    Channels + addresses are added separately via add_channel / add_address
    so the caller can decide how strict to be about duplicate channels
    (which would raise on the UNIQUE constraint).

    `space_id` defaults to the creator's personal space when omitted (Phase B
    contract — contacts must live in some space so the ACL has a handle).
    Pass an explicit shared space (Household, etc.) to make the contact
    visible to that space's members.
    """
    if not (display_name or "").strip():
        raise ValueError("display_name is required")
    if kind not in ("person", "business"):
        raise ValueError(f"invalid kind={kind!r}")
    if status not in ("active", "pending", "spam", "archived"):
        raise ValueError(f"invalid status={status!r}")
    if space_id is None and created_by_user_id is not None:
        from . import spaces as _sp
        space_id = _sp.personal_space_id(created_by_user_id)
    # Business rows shouldn't carry person identity fields. Defensive
    # clear so a caller that passed them through doesn't end up with
    # a "Bayerische Beamten Versicherung AG" row that also has
    # first_name set.
    if kind == "business":
        first_name = last_name = role = None
        employer_contact_id = None
    with conn_ctx() as c:
        cur = c.execute(
            "INSERT INTO contacts ("
            " display_name, aliases, kind, status, relation, birthday,"
            " language_pref, salutation_pref, legal_name, tax_id, iban,"
            " payment_terms_days, default_currency, notes, tags,"
            " created_by_user_id, source, space_id,"
            " first_name, last_name, role, employer_contact_id"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                display_name.strip(),
                json.dumps(aliases) if aliases else None,
                kind, status, relation, birthday,
                language_pref, salutation_pref, legal_name, tax_id, iban,
                payment_terms_days, default_currency, notes,
                json.dumps(tags) if tags else None,
                created_by_user_id, source, space_id,
                first_name, last_name, role, employer_contact_id,
            ),
        )
        new_id = cur.lastrowid
    log.info("contact created id=%d name=%r kind=%s status=%s source=%s",
             new_id, display_name, kind, status, source)
    return int(new_id)


def update(contact_id: int, **fields: Any) -> Dict[str, Any]:
    """Update mutable fields. Returns the row's pre-update snapshot so
    the skill can stage a rollback for the undo machinery."""
    allowed = {
        "display_name", "aliases", "kind", "status", "relation", "birthday",
        "language_pref", "salutation_pref", "legal_name", "tax_id", "iban",
        "payment_terms_days", "default_currency", "notes", "tags",
        "last_used_at", "last_interaction_at", "space_id",
        # mig 045 — person identity columns.
        "first_name", "last_name", "role", "employer_contact_id",
        # NOTE: yorik_assist_enabled migrated to contact_user_prefs in mig 123
        # — set via backend.contact_user_prefs, NOT via update().
    }
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"unknown fields: {sorted(bad)}")
    sets: List[str] = []
    params: List[Any] = []
    for k, v in fields.items():
        if k in ("aliases", "tags") and v is not None and not isinstance(v, str):
            v = json.dumps(v)
        sets.append(f"{k} = ?")
        params.append(v)
    sets.append("updated_at = datetime('now')")
    with conn_ctx() as c:
        before = c.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
        if not before:
            raise ValueError(f"no such contact id={contact_id}")
        c.execute(f"UPDATE contacts SET {', '.join(sets)} WHERE id = ?",
                  [*params, contact_id])
    return _to_contact_dict(before)


def delete(contact_id: int) -> Dict[str, Any]:
    """Hard-delete a contact + its channels + addresses. Returns the
    full pre-delete state so the undo machinery can re-create it on rollback.

    We delete children explicitly because SQLite has ``PRAGMA foreign_keys
    = OFF`` by default — the FK CASCADE declared in the migration doesn't
    fire unless the pragma is enabled, and we don't want to flip it
    globally (other modules rely on the current behaviour). Single
    transaction so a crash mid-way leaves nothing partially deleted.
    """
    snapshot = get(contact_id, include_children=True)
    if not snapshot:
        raise ValueError(f"no such contact id={contact_id}")
    with conn_ctx() as c:
        c.execute("DELETE FROM contact_channels  WHERE contact_id = ?", (contact_id,))
        c.execute("DELETE FROM contact_addresses WHERE contact_id = ?", (contact_id,))
        c.execute("DELETE FROM contacts          WHERE id = ?",         (contact_id,))
    return snapshot


def archive(contact_id: int) -> Dict[str, Any]:
    """Soft-delete: set status='archived'. Recommended over delete() for
    contacts that might still be referenced by old invoices / emails.
    Returns pre-update snapshot for undo."""
    return update(contact_id, status="archived")


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


def add_channel(
    contact_id: int,
    *,
    kind: str,
    value: str,
    label: Optional[str] = None,
    source: str = "manual",
    verified: bool = False,
) -> int:
    """Add a channel to a contact. Raises IntegrityError if (kind, value)
    is already claimed by ANOTHER contact (caller should resolve by linking
    to that contact instead)."""
    normalised = normalize_channel(kind, value)
    if not normalised:
        raise ValueError("channel value required")
    with conn_ctx() as c:
        cur = c.execute(
            "INSERT INTO contact_channels (contact_id, kind, value, label, source, verified_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (contact_id, kind, normalised, label, source,
             datetime.now(timezone.utc).replace(tzinfo=None).isoformat() if verified else None),
        )
        return int(cur.lastrowid)


def remove_channel(channel_id: int) -> Optional[Dict[str, Any]]:
    with conn_ctx() as c:
        before = c.execute(
            "SELECT * FROM contact_channels WHERE id = ?", (channel_id,)
        ).fetchone()
        if not before:
            return None
        c.execute("DELETE FROM contact_channels WHERE id = ?", (channel_id,))
    return _to_channel_dict(before)


# ---------------------------------------------------------------------------
# Addresses
# ---------------------------------------------------------------------------


def add_address(
    contact_id: int,
    *,
    kind: str = "home",
    line1: Optional[str] = None,
    line2: Optional[str] = None,
    postcode: Optional[str] = None,
    city: Optional[str] = None,
    region: Optional[str] = None,
    country: Optional[str] = None,
    label: Optional[str] = None,
    source: str = "manual",
) -> int:
    if not any([line1, line2, postcode, city]):
        raise ValueError("at least one address field is required")
    with conn_ctx() as c:
        cur = c.execute(
            "INSERT INTO contact_addresses ("
            " contact_id, kind, line1, line2, postcode, city, region, country, label, source"
            ") VALUES (?,?,?,?,?,?,?,?,?,?)",
            (contact_id, kind, line1, line2, postcode, city, region, country, label, source),
        )
        return int(cur.lastrowid)


def remove_address(address_id: int) -> Optional[Dict[str, Any]]:
    with conn_ctx() as c:
        before = c.execute(
            "SELECT * FROM contact_addresses WHERE id = ?", (address_id,)
        ).fetchone()
        if not before:
            return None
        c.execute("DELETE FROM contact_addresses WHERE id = ?", (address_id,))
    return _to_address_dict(before)


# ---------------------------------------------------------------------------
# Status transitions — the spam funnel
# ---------------------------------------------------------------------------


def promote_pending(contact_id: int) -> Dict[str, Any]:
    """pending → active. Called explicitly by the user OR by the
    auto-promotion rule (reply detected, event invite sent, draft addressed)."""
    return update(contact_id, status="active", last_used_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat())


def mark_spam(contact_id: int) -> Dict[str, Any]:
    """Anything → spam. Future inbound from this contact's channels gets
    quietly dropped at the email fetcher."""
    return update(contact_id, status="spam")


def bump_interaction(contact_id: int) -> None:
    """Update last_interaction_at without changing anything else. Called by
    the email fetcher when a NEW message lands from an already-known contact."""
    with conn_ctx() as c:
        c.execute(
            "UPDATE contacts SET last_interaction_at = datetime('now') WHERE id = ?",
            (contact_id,),
        )


def bump_use(contact_id: int) -> None:
    """Update last_used_at — bumps the autocomplete ranking. Called when
    the user explicitly references the contact (draft recipient, mention)."""
    with conn_ctx() as c:
        c.execute(
            "UPDATE contacts SET last_used_at = datetime('now') WHERE id = ?",
            (contact_id,),
        )


# ---------------------------------------------------------------------------
# Counts (for the UI tab badges)
# ---------------------------------------------------------------------------


def status_counts(role: Optional[str] = None) -> Dict[str, int]:
    """Return {active, pending, spam, archived} → count. Used by /r/contacts
    to render the tab badges."""
    sql = "SELECT status, COUNT(*) AS n FROM contacts GROUP BY status"
    with conn_ctx() as c:
        rows = c.execute(sql).fetchall()
    out = {"active": 0, "pending": 0, "spam": 0, "archived": 0}
    for r in rows:
        out[r["status"]] = r["n"]
    return out


# ---------------------------------------------------------------------------
# Internal: row → dict
# ---------------------------------------------------------------------------


def _to_contact_dict(row: Any) -> Dict[str, Any]:
    d = dict(row)
    # Parse JSON-encoded list columns
    for col in ("aliases", "tags"):
        raw = d.get(col)
        if raw:
            try:
                d[col] = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                d[col] = []
        else:
            d[col] = []
    # Mig 025: surface `pinned` as a real bool (SQLite gives back 0/1).
    if "pinned" in d:
        d["pinned"] = bool(d["pinned"])
    return d


def _to_channel_dict(row: Any) -> Dict[str, Any]:
    return dict(row)


def _to_address_dict(row: Any) -> Dict[str, Any]:
    return dict(row)


__all__ = [
    "normalize_email", "normalize_phone", "normalize_channel",
    "is_transactional_email", "is_mass_mailer_email",
    "find_business_by_email_domain",
    "find_by_channel", "search", "get",
    "create", "update", "delete", "archive",
    "add_channel", "remove_channel",
    "add_address", "remove_address",
    "promote_pending", "mark_spam",
    "bump_interaction", "bump_use",
    "status_counts",
]
