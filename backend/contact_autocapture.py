"""
Contact autocapture — the hooks that turn email + WhatsApp traffic into
contacts in the identity hub without the user having to type them in.

Policy (chosen 2026-05: "Pending by default, promote on reply"):

  * Inbound email from unknown sender → create contact with status='pending',
    source='email_in'. The Pending tab in /r/contacts surfaces it for triage.
    Transactional senders (no-reply / billing / notifications / receipts)
    are skipped — they would clog the Pending tab with one-shot machines.

  * Inbound email from a sender already on the spam list → tag the email
    `category='spam'` so the inbox can hide it (and skip the autodraft).
    The contact's channel stays indexed so future inbound is recognised
    in O(log n).

  * Inbound email from a known active/pending contact → bump
    last_interaction_at for autocomplete ranking.

  * Outbound email from the user TO recipients → for each recipient that
    matches a 'pending' contact, promote them to 'active'. This is the
    "user replied → they're real" signal.

All operations are best-effort: an exception here MUST NOT break the
email pipeline. The caller wraps in try/except and logs.
"""

from __future__ import annotations

import logging
import os
from typing import Iterable, Optional

from . import contacts as _contacts

log = logging.getLogger(__name__)


def on_inbound_email(
    *,
    from_email: str,
    from_name: str,
    message_id: int,
) -> Optional[str]:
    """Run after a new inbound email is committed to email_messages.
    Returns the category override (e.g. 'spam') if one was applied,
    else None. Caller (email_fetcher) should write the category to the
    email_messages row when not None.

    NEVER raises — all paths log + swallow.
    """
    try:
        addr = (from_email or "").strip().lower()
        if not addr:
            return None

        existing = _contacts.find_by_channel("email", addr)

        # Known spam → tag the email and skip everything else.
        if existing and existing.get("status") == "spam":
            try:
                _contacts.bump_use(existing["id"])
            except Exception:  # noqa: BLE001
                pass
            log.info("contact_autocapture: spam sender %s (contact %s) — tagging msg %d",
                     addr, existing["id"], message_id)
            return "spam"

        # Known active/pending → just bump interaction time so
        # autocomplete ranks recent senders higher.
        if existing:
            try:
                _contacts.bump_interaction(existing["id"])
            except Exception as exc:  # noqa: BLE001
                log.debug("bump_interaction failed for %s: %s", existing["id"], exc)
            return None

        # Unknown sender — decide whether to create a Pending row.
        if _contacts.is_transactional_email(addr):
            # no-reply / billing / notifications — don't pollute Pending
            # with one-shot machines. The user can still find the email,
            # but won't be nagged to "save this contact".
            log.debug("contact_autocapture: transactional %s skipped", addr)
            return None

        # New pending contact — for EMAIL specifically we keep this
        # status='pending' because cold-mail noise is real (and the
        # transactional filter above only catches the obvious cases).
        # The user reviews them in /r/contacts before they enter
        # autocomplete.
        contact_id = _contacts.create(
            display_name=(from_name or "").strip() or addr.split("@")[0],
            kind="person",
            status="pending",
            source="email_in",
        )
        try:
            _contacts.add_channel(
                contact_id, kind="email", value=addr, source="email_in",
            )
        except Exception as exc:  # noqa: BLE001
            # Race: same email arriving twice within the same IMAP fetch
            # window could double-create. The UNIQUE(kind, value) on
            # channels catches it — delete the orphan contact so we
            # don't leak.
            log.debug("autocapture channel insert failed (likely race): %s", exc)
            try:
                _contacts.delete(contact_id)
            except Exception:
                pass
            return None

        log.info("contact_autocapture: parked pending contact id=%d email=%s",
                 contact_id, addr)
        return None

    except Exception as exc:  # noqa: BLE001
        log.exception("contact_autocapture: unexpected failure on inbound %s: %s",
                      from_email, exc)
        return None


def on_outbound_email(*, to_addrs: Iterable[str]) -> int:
    """Promote any pending contacts that the user just replied to.
    Returns count of promotions. Never raises."""
    promoted = 0
    try:
        seen_ids: set[int] = set()
        for raw in to_addrs:
            addr = (raw or "").strip().lower()
            if not addr:
                continue
            existing = _contacts.find_by_channel("email", addr)
            if not existing:
                continue
            cid = int(existing["id"])
            if cid in seen_ids:
                continue
            seen_ids.add(cid)
            if existing.get("status") == "pending":
                try:
                    _contacts.promote_pending(cid)
                    promoted += 1
                    log.info("contact_autocapture: promoted contact %s to active "
                             "(user replied to %s)", cid, addr)
                except Exception as exc:  # noqa: BLE001
                    log.debug("promote_pending failed for %s: %s", cid, exc)
            # Active recipients also get an interaction bump — outbound
            # is a strong signal for autocomplete ranking.
            elif existing.get("status") == "active":
                try:
                    _contacts.bump_interaction(cid)
                except Exception:
                    pass
    except Exception as exc:  # noqa: BLE001
        log.exception("contact_autocapture: outbound promotion failed: %s", exc)
    return promoted


# ─── WhatsApp variant — same policy, different channel kind ───────────


def _is_pseudo_jid(jid: str) -> bool:
    """Jids that aren't real reachable accounts.

    NOTE: @lid is NOT pseudo — it's a real person identifier WhatsApp
    uses when the contact has Linked-Devices privacy mode enabled.
    Real-world Hans Becker is on @lid because of his privacy setting.
    The earlier "messages to Tom went to brother" bug was about
    STRIPPING the suffix (causing different @lid jids to collide with
    different @s.whatsapp.net jids on bare digits), not about @lid
    being bad. Storing the FULL jid as the channel value — which we
    do now — keeps @lid contacts uniquely identifiable.

    Truly pseudo: WhatsApp Channels (broadcast-only newsletters) and
    status updates. Both are not 1:1 reachable accounts.
    """
    if not jid:
        return True
    return (
        jid == "status@broadcast"
        or jid.endswith("@newsletter")
        or jid.endswith("@broadcast")
    )


def _phone_match_for_jid(from_jid: str) -> Optional[Dict[str, Any]]:
    """Bridge vCard imports to WhatsApp JIDs: take the digits of the
    JID, try both `+<digits>` and `<digits>` as a phone-channel value
    (vCards may or may not carry a leading +), return the matching
    contact if one exists. Without this, importing your address book
    as `kind=phone, value=+4915xxx` and then getting a WA message
    `kind=whatsapp, value=4915xxx@s.whatsapp.net` would create a
    pending row even though the contact is already on file.
    """
    if not from_jid or "@" not in from_jid:
        return None
    digits = "".join(c for c in from_jid.split("@", 1)[0] if c.isdigit())
    if len(digits) < 6:
        return None
    for candidate in (f"+{digits}", digits):
        try:
            normalized = _contacts.normalize_channel("phone", candidate)
            if not normalized:
                continue
            existing = _contacts.find_by_channel("phone", normalized)
            if existing:
                return existing
        except Exception:
            continue
    return None


def _attach_wa_to_phone_contact(
    phone_contact: Dict[str, Any], norm_wa_jid: str, log_ctx: str = "",
) -> bool:
    """Attach a WhatsApp channel (`norm_wa_jid`) to an existing
    phone-matched contact. If a *different* pending contact already
    owns that WA channel (typical: the previous autocapture ran before
    this code landed), the pending row gets merged in — its WA channel
    is freed when the row is deleted, then attached to the real
    contact. Returns True if anything was changed."""
    cid = int(phone_contact["id"])
    changed = False
    stray = _contacts.find_by_channel("whatsapp", norm_wa_jid)
    if stray and int(stray["id"]) != cid:
        if (stray.get("status") or "pending") == "pending":
            try:
                _contacts.delete(int(stray["id"]))
                log.info(
                    "wa autocapture: merged pending WA contact %s into "
                    "phone-matched contact %s%s",
                    stray["id"], cid, f" ({log_ctx})" if log_ctx else "",
                )
                changed = True
            except Exception as exc:
                log.exception(
                    "wa autocapture: couldn't merge stray %s into %s: %s",
                    stray["id"], cid, exc,
                )
                return False
        else:
            # Non-pending claimant — could be a legit second contact who
            # also happens to share this JID. Bail out rather than
            # touch active/spam rows.
            log.warning(
                "wa autocapture: WA jid %s is claimed by non-pending "
                "contact %s; phone-match contact %s left alone",
                norm_wa_jid, stray["id"], cid,
            )
            return False
    try:
        _contacts.add_channel(cid, kind="whatsapp", value=norm_wa_jid,
                              source="whatsapp_autocapture")
        changed = True
    except Exception:
        pass  # already attached or transient DB error — fine
    try:
        _contacts.bump_interaction(cid)
    except Exception:
        pass
    return changed


def on_inbound_whatsapp(
    *,
    from_jid: str,
    from_name: str,
) -> Optional[str]:
    """Same shape as on_inbound_email but for WhatsApp. Stores the FULL
    jid as the channel value (not the digits) so lookups are exact and
    @lid pseudo-jids can be filtered out cleanly. Returns 'spam' if the
    sender is on the spam list, else None. Never raises."""
    try:
        if _is_pseudo_jid(from_jid):
            return None

        norm = _contacts.normalize_channel("whatsapp", from_jid)
        if not norm:
            return None

        # Cross-channel match: a vCard-imported contact carries a phone
        # channel but no WhatsApp channel yet. On their first WA message
        # we'd otherwise create a pending row. Look up by phone first,
        # and if matched, attach the WA channel to that real contact
        # (merging any pre-existing pending duplicate).
        phone_contact = _phone_match_for_jid(from_jid)
        if phone_contact:
            _attach_wa_to_phone_contact(phone_contact, norm,
                                        log_ctx=from_name or "")
            return None

        existing = _contacts.find_by_channel("whatsapp", norm)
        if existing and existing.get("status") == "spam":
            try:
                _contacts.bump_use(existing["id"])
            except Exception:
                pass
            return "spam"
        if existing:
            try:
                _contacts.bump_interaction(existing["id"])
            except Exception:
                pass
            return None

        # WhatsApp policy: default to active.
        # An incoming 1:1 WhatsApp message from an unknown sender is a
        # strong "real person" signal — WhatsApp's cold-spam friction
        # filters most noise on its own, and hiding 527 real contacts
        # behind a triage gate made the contacts hub feel broken.
        # Only fall back to pending if BOTH the pushName and the
        # number are missing/junky (no human signal at all).
        clean_name = (from_name or "").strip()

        # When the inbound message itself didn't carry a pushName
        # (outbound autocapture, bridge stripped it, etc.), the chat
        # row often still has one — Baileys populates wa_chats.name
        # from the contact's profile name / pushName during the chats
        # sync. Falling back here turns rows like "4915xxx — pending"
        # into "Tom Schmidt — active".
        if not clean_name:
            try:
                from .database import get_conn
                with get_conn() as conn:
                    chat_row = conn.execute(
                        "SELECT name FROM wa_chats WHERE jid = ?",
                        (from_jid,),
                    ).fetchone()
                if chat_row and (chat_row["name"] or "").strip():
                    clean_name = chat_row["name"].strip()
            except Exception:
                pass  # missing wa_chats table on a fresh install — skip

        # @lid without a pushName carries ZERO identifying info — the
        # 15-digit LID is opaque to humans, there's no phone number to
        # recognise, and parking it on the pending list just asks the
        # user to triage rows like "222273835368470 — keep or spam?"
        # with no basis to decide. Skip the autocapture entirely; the
        # message itself still lands in wa_messages keyed by jid, and
        # if a pushName later appears the next inbound will create the
        # contact properly. @s.whatsapp.net contacts without a name
        # still get parked (the digits ARE a phone number the user can
        # potentially recognise).
        if not clean_name and from_jid.endswith("@lid"):
            log.debug("contact_autocapture: skipped nameless @lid sender %s", from_jid)
            return None

        status = "active" if clean_name else "pending"
        contact_id = _contacts.create(
            display_name=clean_name or norm.split("@")[0],
            kind="person",
            status=status,
            source="wa_sync",
        )
        try:
            _contacts.add_channel(
                contact_id, kind="whatsapp", value=norm, source="wa_sync",
            )
        except Exception:
            try:
                _contacts.delete(contact_id)
            except Exception:
                pass
            return None

        log.info("contact_autocapture: parked pending WA contact id=%d number=%s",
                 contact_id, norm)
        return None

    except Exception as exc:  # noqa: BLE001
        log.exception("contact_autocapture: WA inbound failed: %s", exc)
        return None


# ─── Backfill / seed ──────────────────────────────────────────────────


def backfill_whatsapp_display_names(*, owner_user_id: Optional[int] = None) -> dict:
    """Sync WhatsApp names from passive sources into contact_channels
    and (when display_name is the JID-fallback) into contacts.

    Sources, in priority order:
      1. The bridge's persisted nameByJid map (populated from every
         Baileys event with a name — chats.upsert, contacts.upsert,
         messages.upsert, messaging-history.set).
      2. wa_chats.name (older fallback, set per-chat).
      3. wa_messages.push_name (matched on chat_jid OR participant).

    Active per-JID lookup via presenceSubscribe was removed: it didn't
    reliably return data even on a healthy session because Meta only
    sends names through the init-sync / message channels, not in
    response to presence requests. The bridge endpoint still exists
    for future experimentation but this backfill is passive-only.

    A contact's display_name is updated ONLY when it's purely digits
    (the JID-prefix fallback we set at create time when no name was
    available). channel.display_name is updated ALWAYS — that's the
    per-modality field, designed to track the upstream-provided name
    regardless of how the user has renamed the contact.
    """
    from .database import get_conn

    updated_contacts = 0
    updated_channels = 0
    no_source = 0
    inspected = 0
    _BRIDGE_URL = os.getenv("YORIK_WA_BRIDGE_URL", "http://127.0.0.1:3015")
    import requests as _rq

    # Pull every WA contact row + its channel id (we'll update both
    # the channel and the contact rows). Include rows that already
    # have a real contact name — their channel.display_name might
    # still be stale and worth updating from the bridge map.
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT c.id AS contact_id, c.display_name AS contact_name, "
            "       ch.id AS channel_id, ch.value AS jid, "
            "       ch.display_name AS channel_name "
            "FROM contacts c "
            "JOIN contact_channels ch ON ch.contact_id = c.id "
            "WHERE ch.kind = 'whatsapp' "
            "ORDER BY c.id"
        ).fetchall()

    bridge_names: dict[str, str] = {}
    if owner_user_id:
        try:
            r = _rq.get(
                f"{_BRIDGE_URL}/users/{owner_user_id}/contact-names",
                timeout=5,
            )
            if r.status_code == 200:
                data = r.json()
                bridge_names = data.get("contacts") or {}
                log.info("backfill-wa-names: bridge contact-map has %d names", len(bridge_names))
        except Exception as exc:  # noqa: BLE001
            log.info("backfill-wa-names: bridge contact-names fetch failed: %s", exc)

    for row in rows:
        inspected += 1
        cid = int(row["contact_id"])
        chan_id = int(row["channel_id"])
        jid = row["jid"]
        contact_current = row["contact_name"] or ""
        channel_current = row["channel_name"] or ""

        # Three sources, strongest first. Skip the per-row work when
        # all sources are empty so the no_source counter is meaningful.
        best = ""
        if jid in bridge_names and bridge_names[jid].strip():
            best = bridge_names[jid].strip()
        if not best:
            try:
                with get_conn() as conn:
                    wc = conn.execute(
                        "SELECT name FROM wa_chats WHERE jid = ?",
                        (jid,),
                    ).fetchone()
                    if wc and (wc["name"] or "").strip():
                        best = wc["name"].strip()
                    else:
                        # Most recent message's pushName: chat_jid match
                        # (1:1 sender) OR participant match (group).
                        # Participant covers @lid contacts who only
                        # appear via their group activity.
                        wm = conn.execute(
                            "SELECT push_name FROM wa_messages "
                            "WHERE push_name IS NOT NULL AND push_name <> '' "
                            "  AND (chat_jid = ? OR participant = ?) "
                            "ORDER BY timestamp DESC LIMIT 1",
                            (jid, jid),
                        ).fetchone()
                        if wm and (wm["push_name"] or "").strip():
                            best = wm["push_name"].strip()
            except Exception as exc:  # noqa: BLE001
                log.debug("backfill-wa-names: lookup failed for contact %d: %s", cid, exc)
                continue

        if not best:
            no_source += 1
            continue

        try:
            with get_conn() as conn:
                # Always refresh channel.display_name when the discovered
                # name differs — this is the per-modality field, kept
                # current with WhatsApp's latest pushName regardless of
                # what the contact's display_name is.
                if channel_current != best:
                    conn.execute(
                        "UPDATE contact_channels SET display_name=? WHERE id=?",
                        (best, chan_id),
                    )
                    updated_channels += 1
                # Promote contact.display_name only if it's still the
                # numeric JID-prefix fallback — never overwrite a
                # user-set or earlier-promoted name.
                if contact_current.isdigit():
                    conn.execute(
                        "UPDATE contacts SET display_name=?, updated_at=datetime('now') "
                        "WHERE id=?",
                        (best, cid),
                    )
                    updated_contacts += 1
                conn.commit()
        except Exception as exc:  # noqa: BLE001
            log.debug("backfill-wa-names: update failed for contact %d: %s", cid, exc)

    log.info("backfill_whatsapp_display_names: inspected=%d updated_contacts=%d updated_channels=%d no_source=%d",
             inspected, updated_contacts, updated_channels, no_source)
    return {
        "inspected": inspected,
        "updated_contacts": updated_contacts,
        "updated_channels": updated_channels,
        "no_source": no_source,
        # Legacy alias kept so the frontend "updated" count doesn't
        # disappear from existing alerts during the rollout.
        "updated": updated_contacts,
    }


def seed_from_whatsapp_history(*, owner_user_id: Optional[int] = None) -> dict:
    """One-shot backfill — walk every 1:1 chat in wa_chats and create a
    pending contact for each sender that doesn't already have a contact
    record. Idempotent.

    Pseudo-jids (@lid / @newsletter / @broadcast) are skipped — they're
    not real reachable identifiers and including them is what caused
    the "messages to Tom went to brother" bug. See _is_pseudo_jid().
    """
    from .database import get_conn

    created = 0
    skipped_existing = 0
    skipped_groups = 0
    skipped_pseudo = 0

    with get_conn() as conn:
        if owner_user_id is not None:
            rows = conn.execute(
                "SELECT jid, name FROM wa_chats WHERE owner_user_id = ? AND is_group = 0",
                (owner_user_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT jid, name FROM wa_chats WHERE is_group = 0"
            ).fetchall()

    for row in rows:
        jid = row["jid"]
        if not jid or jid.endswith("@g.us"):
            skipped_groups += 1
            continue
        if _is_pseudo_jid(jid):
            skipped_pseudo += 1
            continue
        norm = _contacts.normalize_channel("whatsapp", jid)
        if not norm:
            continue
        existing = _contacts.find_by_channel("whatsapp", norm)
        if existing:
            skipped_existing += 1
            continue
        try:
            # status='active' — an established wa_chats row means you've
            # already validated this person by chatting with them. See
            # contact_autocapture's WhatsApp policy comment for the
            # rationale; the email-side seed (different module) keeps
            # pending defaults.
            cid = _contacts.create(
                display_name=(row["name"] or "").strip() or jid.split("@")[0],
                kind="person",
                status="active",
                source="wa_sync",
            )
            try:
                _contacts.add_channel(cid, kind="whatsapp", value=norm, source="wa_sync")
            except Exception:
                _contacts.delete(cid)
                continue
            created += 1
        except Exception as exc:  # noqa: BLE001
            log.debug("seed: could not create contact for %s: %s", jid, exc)

    log.info("seed_from_whatsapp_history: created=%d existing=%d groups=%d pseudo=%d",
             created, skipped_existing, skipped_groups, skipped_pseudo)
    # Refresh display names for any rows whose display_name is still
    # the raw phone-number fallback. The first seed-run typically
    # creates many of these because wa_chats.name hasn't been synced
    # by the bridge yet at that moment; a second seed-run (or any
    # subsequent call) picks up the now-populated names.
    name_fix = backfill_whatsapp_display_names(owner_user_id=owner_user_id)
    return {
        "created": created,
        "skipped_existing": skipped_existing,
        "skipped_groups": skipped_groups,
        "skipped_pseudo": skipped_pseudo,
        **{f"names_{k}": v for k, v in name_fix.items()},
    }
