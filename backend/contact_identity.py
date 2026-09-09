"""Contact identity — the one place that decides "is this the same person?"

Every path that brings a contact into Yorik (inbound email, WhatsApp,
vCard import, the add_contact skill, the New-contact form) asks this
module first. The rule is deliberately narrow:

  * Identity is a channel: an email address, or a phone number in E.164.
    A WhatsApp id is the same identity as the phone number it encodes.
  * Names never merge anything. A matching name with a different channel
    is at most a hint, never a reason.
  * Nothing merges by itself. When two contacts turn out to be one person
    (a phone number in an email signature that belongs to a WhatsApp-only
    contact, say), Yorik writes a *proposal* and a human accepts it. An
    accepted merge is recorded and can be undone.

Phone numbers are normalised with `phonenumbers` (Google's libphonenumber),
region = HOMEOS_PHONE_REGION (default DE), so "0511 / 12 34 56",
"+49 511 123456" and the WhatsApp id 49511123456@s.whatsapp.net all mean
+49511123456.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .database import conn_ctx

log = logging.getLogger("yorik.contact_identity")

IDENTITY_KINDS = ("email", "phone", "whatsapp")


def phone_region() -> str:
    return (os.getenv("HOMEOS_PHONE_REGION") or "DE").upper()


# ─── normalisation ──────────────────────────────────────────────────

def to_e164(value: str, region: Optional[str] = None) -> Optional[str]:
    """'0511 / 123456' → '+49511123456'. None when it is not a phone number.
    Accepts WhatsApp ids too (digits before @s.whatsapp.net); @lid ids are
    not phone numbers and return None."""
    import phonenumbers
    v = (value or "").strip()
    if not v:
        return None
    if "@" in v:
        local, _, suffix = v.partition("@")
        if suffix.lower() != "s.whatsapp.net":
            return None
        v = "+" + "".join(c for c in local if c.isdigit())
    try:
        num = phonenumbers.parse(v, region or phone_region())
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_possible_number(num):
        return None
    return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)


def whatsapp_jid(e164: str) -> str:
    return f"{e164.lstrip('+')}@s.whatsapp.net"


def normalize_identity(kind: str, value: str) -> Optional[Tuple[str, str]]:
    """(kind, stored value) for one identity channel, or None if unusable."""
    if kind == "email":
        v = (value or "").strip().lower()
        return ("email", v) if "@" in v else None
    if kind in ("phone", "sms", "signal"):
        e = to_e164(value)
        return ("phone", e) if e else None
    if kind == "whatsapp":
        v = (value or "").strip()
        if "@" in v:
            local, _, suffix = v.partition("@")
            digits = "".join(c for c in local if c.isdigit())
            return ("whatsapp", f"{digits}@{suffix.lower()}") if digits else None
        e = to_e164(v)
        return ("whatsapp", whatsapp_jid(e)) if e else None
    return None


def extract_phones(text: str, region: Optional[str] = None, limit: int = 5) -> List[str]:
    """Phone numbers found in free text (an email signature), as E.164,
    in order of appearance, deduplicated."""
    import phonenumbers
    out: List[str] = []
    if not text:
        return out
    for m in phonenumbers.PhoneNumberMatcher(text, region or phone_region()):
        e = phonenumbers.format_number(m.number, phonenumbers.PhoneNumberFormat.E164)
        if e not in out:
            out.append(e)
        if len(out) >= limit:
            break
    return out


# ─── lookup ─────────────────────────────────────────────────────────

def owner_of(kind: str, value: str) -> Optional[Dict[str, Any]]:
    """The contact that owns this identity, or None. A phone number is
    looked up as phone AND as WhatsApp id; a WhatsApp id also as phone."""
    from . import contacts as C
    norm = normalize_identity(kind, value)
    if not norm:
        return None
    k, v = norm
    keys: List[Tuple[str, str]] = [(k, v)]
    if k == "phone":
        keys.append(("whatsapp", whatsapp_jid(v)))
    elif k == "whatsapp" and v.endswith("@s.whatsapp.net"):
        e = to_e164(v)
        if e:
            keys.append(("phone", e))
    with conn_ctx() as c:
        for kk, vv in keys:
            row = c.execute(
                "SELECT c.* FROM contacts c JOIN contact_channels ch ON ch.contact_id = c.id "
                "WHERE ch.kind = ? AND ch.value = ? AND c.status <> 'merged'",
                (kk, vv),
            ).fetchone()
            if row:
                return C._to_contact_dict(row)
    return None


@dataclass
class Resolution:
    contact: Optional[Dict[str, Any]] = None          # the contact these channels belong to
    matched: List[Tuple[str, str]] = field(default_factory=list)   # channels that hit `contact`
    unclaimed: List[Tuple[str, str]] = field(default_factory=list) # normalised channels nobody owns
    conflicts: List[Dict[str, Any]] = field(default_factory=list)  # channels owned by OTHER contacts
    name_conflict: bool = False

    @property
    def is_new(self) -> bool:
        return self.contact is None


def resolve(channels: Iterable[Tuple[str, str]], display_name: Optional[str] = None) -> Resolution:
    """Given the channels a source knows about someone, say which contact
    they are. First owner found wins; channels owned by a different
    contact are reported as conflicts (a merge proposal candidate), never
    silently reassigned."""
    res = Resolution()
    seen: set = set()
    for kind, value in channels:
        norm = normalize_identity(kind, value)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        owner = owner_of(*norm)
        if owner is None:
            res.unclaimed.append(norm)
        elif res.contact is None or int(owner["id"]) == int(res.contact["id"]):
            res.contact = res.contact or owner
            res.matched.append(norm)
        else:
            res.conflicts.append({"channel": norm, "contact": owner})
    if res.contact and display_name:
        a = (display_name or "").strip().casefold()
        b = (res.contact.get("display_name") or "").strip().casefold()
        res.name_conflict = bool(a and b and a != b)
    return res


# ─── proposals ──────────────────────────────────────────────────────

PROPOSAL_MERGE = "merge"
PROPOSAL_ADD_CHANNEL = "add_channel"


def propose(*, kind: str, contact_id: int, other_contact_id: Optional[int] = None,
            channel_kind: Optional[str] = None, channel_value: Optional[str] = None,
            reason: str, evidence: Optional[Dict[str, Any]] = None,
            confidence: float = 0.5, user_id: Optional[str] = None) -> Optional[int]:
    """Record a suggestion for a human. Returns the proposal id, or None when
    the same pending suggestion already exists."""
    if kind == PROPOSAL_MERGE and (other_contact_id is None or int(other_contact_id) == int(contact_id)):
        return None
    with conn_ctx() as c:
        dup = c.execute(
            "SELECT id FROM contact_proposals WHERE status = 'pending' AND kind = ? "
            "AND contact_id = ? AND COALESCE(other_contact_id, 0) = COALESCE(?, 0) "
            "AND COALESCE(channel_kind, '') = COALESCE(?, '') "
            "AND COALESCE(channel_value, '') = COALESCE(?, '')",
            (kind, contact_id, other_contact_id, channel_kind, channel_value),
        ).fetchone()
        if dup:
            return None
        if kind == PROPOSAL_MERGE:
            # the mirrored pair counts as the same suggestion
            dup = c.execute(
                "SELECT id FROM contact_proposals WHERE status = 'pending' AND kind = 'merge' "
                "AND contact_id = ? AND other_contact_id = ?",
                (other_contact_id, contact_id),
            ).fetchone()
            if dup:
                return None
        cur = c.execute(
            "INSERT INTO contact_proposals (kind, contact_id, other_contact_id, channel_kind, "
            "channel_value, reason, evidence_json, confidence, user_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, contact_id, other_contact_id, channel_kind, channel_value, reason,
             json.dumps(evidence or {}), float(confidence), user_id),
        )
        pid = int(cur.lastrowid)
    log.info("contact proposal #%d: %s contact=%s other=%s %s", pid, kind, contact_id,
             other_contact_id, reason)
    return pid


def _proposal_row(r: Any) -> Dict[str, Any]:
    d = dict(r)
    try:
        d["evidence"] = json.loads(d.pop("evidence_json") or "{}")
    except Exception:  # noqa: BLE001
        d["evidence"] = {}
    return d


def list_proposals(status: str = "pending", limit: int = 100) -> List[Dict[str, Any]]:
    from . import contacts as C
    with conn_ctx() as c:
        rows = c.execute(
            "SELECT * FROM contact_proposals WHERE status = ? ORDER BY created_at DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    out = []
    for r in rows:
        d = _proposal_row(r)
        d["contact"] = C.get(int(d["contact_id"]), include_children=True)
        d["other_contact"] = C.get(int(d["other_contact_id"]), include_children=True) if d.get("other_contact_id") else None
        out.append(d)
    return out


def get_proposal(pid: int) -> Optional[Dict[str, Any]]:
    with conn_ctx() as c:
        r = c.execute("SELECT * FROM contact_proposals WHERE id = ?", (pid,)).fetchone()
    return _proposal_row(r) if r else None


def _decide(pid: int, status: str, decided_by: Optional[str], extra: Optional[Dict[str, Any]] = None) -> None:
    with conn_ctx() as c:
        c.execute(
            "UPDATE contact_proposals SET status = ?, decided_at = ?, decided_by = ?, result_json = ? "
            "WHERE id = ?",
            (status, _now(), decided_by, json.dumps(extra or {}), pid),
        )


def accept_proposal(pid: int, *, decided_by: Optional[str] = None,
                    keep_id: Optional[int] = None) -> Dict[str, Any]:
    """Apply one proposal. For a merge, `keep_id` picks the survivor
    (default: the proposal's contact_id)."""
    p = get_proposal(pid)
    if not p:
        raise KeyError(f"proposal {pid} not found")
    if p["status"] != "pending":
        raise ValueError(f"proposal {pid} already {p['status']}")
    if p["kind"] == PROPOSAL_MERGE:
        a, b = int(p["contact_id"]), int(p["other_contact_id"])
        keep = int(keep_id) if keep_id else a
        drop = b if keep == a else a
        rec = merge(keep, drop, reason=f"proposal #{pid}: {p['reason']}", decided_by=decided_by)
        _decide(pid, "accepted", decided_by, {"merge_id": rec["id"], "keep_id": keep, "drop_id": drop})
        return {"ok": True, "merge": rec}
    if p["kind"] == PROPOSAL_ADD_CHANNEL:
        from . import contacts as C
        owner = owner_of(p["channel_kind"], p["channel_value"])
        if owner and int(owner["id"]) != int(p["contact_id"]):
            _decide(pid, "rejected", decided_by, {"error": "channel now belongs to another contact"})
            raise ValueError("channel now belongs to another contact")
        if not owner:
            C.add_channel(int(p["contact_id"]), kind=p["channel_kind"], value=p["channel_value"],
                          source=(p.get("evidence") or {}).get("source") or "proposal")
        _decide(pid, "accepted", decided_by, {})
        return {"ok": True}
    raise ValueError(f"unknown proposal kind {p['kind']}")


def reject_proposal(pid: int, *, decided_by: Optional[str] = None) -> None:
    p = get_proposal(pid)
    if not p:
        raise KeyError(f"proposal {pid} not found")
    if p["status"] != "pending":
        raise ValueError(f"proposal {pid} already {p['status']}")
    _decide(pid, "rejected", decided_by, {})


# ─── merge / unmerge ────────────────────────────────────────────────

_FILL_FIELDS = ("first_name", "last_name", "role", "relation", "birthday", "language_pref",
                "salutation_pref", "legal_name", "tax_id", "iban", "payment_terms_days",
                "default_currency", "notes", "employer_contact_id")


def merge(keep_id: int, drop_id: int, *, reason: str = "", decided_by: Optional[str] = None) -> Dict[str, Any]:
    """Fold `drop_id` into `keep_id`: channels and addresses move over,
    blank scalar fields on the survivor are filled from the loser, the
    loser becomes a tombstone (status='merged', merged_into_id). Everything
    moved is recorded in contact_merges so unmerge() can put it back."""
    from . import contacts as C
    keep_id, drop_id = int(keep_id), int(drop_id)
    if keep_id == drop_id:
        raise ValueError("cannot merge a contact into itself")
    keep = C.get(keep_id, include_children=False)
    drop = C.get(drop_id, include_children=False)
    if not keep or not drop:
        raise ValueError("contact not found")
    if drop.get("status") == "merged":
        raise ValueError("contact is already merged")
    moved: Dict[str, Any] = {"channels": [], "addresses": [], "filled": {}, "drop_status": drop.get("status")}
    with conn_ctx() as c:
        for r in c.execute("SELECT id FROM contact_channels WHERE contact_id = ?", (drop_id,)).fetchall():
            moved["channels"].append(int(r["id"]))
        for r in c.execute("SELECT id FROM contact_addresses WHERE contact_id = ?", (drop_id,)).fetchall():
            moved["addresses"].append(int(r["id"]))
        c.execute("UPDATE contact_channels SET contact_id = ? WHERE contact_id = ?", (keep_id, drop_id))
        c.execute("UPDATE contact_addresses SET contact_id = ? WHERE contact_id = ?", (keep_id, drop_id))
        for f in _FILL_FIELDS:
            if not keep.get(f) and drop.get(f):
                c.execute(f"UPDATE contacts SET {f} = ? WHERE id = ?", (drop[f], keep_id))
                moved["filled"][f] = drop[f]
        # a pending survivor inherits an active loser's standing
        if keep.get("status") == "pending" and drop.get("status") == "active":
            c.execute("UPDATE contacts SET status = 'active' WHERE id = ?", (keep_id,))
            moved["keep_status_before"] = "pending"
        c.execute("UPDATE contacts SET employer_contact_id = ? WHERE employer_contact_id = ?", (keep_id, drop_id))
        c.execute("UPDATE contacts SET status = 'merged', merged_into_id = ? WHERE id = ?", (keep_id, drop_id))
        cur = c.execute(
            "INSERT INTO contact_merges (keep_id, drop_id, reason, moved_json, decided_by) VALUES (?, ?, ?, ?, ?)",
            (keep_id, drop_id, reason, json.dumps(moved), decided_by),
        )
        mid = int(cur.lastrowid)
    log.info("contacts merged: #%d into #%d (%s)", drop_id, keep_id, reason)
    return {"id": mid, "keep_id": keep_id, "drop_id": drop_id, "moved": moved}


def unmerge(merge_id: int) -> Dict[str, Any]:
    """Undo one merge: moved rows go back, the tombstone becomes a contact again."""
    with conn_ctx() as c:
        r = c.execute("SELECT * FROM contact_merges WHERE id = ?", (merge_id,)).fetchone()
        if not r:
            raise KeyError(f"merge {merge_id} not found")
        if r["undone_at"]:
            raise ValueError("already undone")
        moved = json.loads(r["moved_json"] or "{}")
        keep_id, drop_id = int(r["keep_id"]), int(r["drop_id"])
        for cid in moved.get("channels", []):
            c.execute("UPDATE contact_channels SET contact_id = ? WHERE id = ?", (drop_id, cid))
        for aid in moved.get("addresses", []):
            c.execute("UPDATE contact_addresses SET contact_id = ? WHERE id = ?", (drop_id, aid))
        for f in moved.get("filled", {}):
            c.execute(f"UPDATE contacts SET {f} = NULL WHERE id = ?", (keep_id,))
        if moved.get("keep_status_before"):
            c.execute("UPDATE contacts SET status = ? WHERE id = ?", (moved["keep_status_before"], keep_id))
        c.execute("UPDATE contacts SET status = ?, merged_into_id = NULL WHERE id = ?",
                  (moved.get("drop_status") or "active", drop_id))
        c.execute("UPDATE contact_merges SET undone_at = ? WHERE id = ?", (_now(), merge_id))
    log.info("contacts unmerged: #%d restored from #%d", drop_id, keep_id)
    return {"ok": True, "keep_id": keep_id, "drop_id": drop_id}


def list_merges(limit: int = 50) -> List[Dict[str, Any]]:
    with conn_ctx() as c:
        rows = c.execute(
            "SELECT m.*, k.display_name AS keep_name, d.display_name AS drop_name "
            "FROM contact_merges m JOIN contacts k ON k.id = m.keep_id JOIN contacts d ON d.id = m.drop_id "
            "ORDER BY m.id DESC LIMIT ?", (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# ─── maintenance ────────────────────────────────────────────────────

def normalize_existing_phone_channels() -> int:
    """One-off at boot: rewrite phone/sms/signal channel values to E.164.
    Values that would collide with another contact are left alone and
    logged; the collision is exactly the merge case a human should see."""
    changed = 0
    with conn_ctx() as c:
        rows = c.execute(
            "SELECT id, contact_id, kind, value FROM contact_channels WHERE kind IN ('phone','sms','signal')"
        ).fetchall()
        for r in rows:
            e = to_e164(r["value"])
            if not e or e == r["value"]:
                continue
            clash = c.execute(
                "SELECT contact_id FROM contact_channels WHERE kind = ? AND value = ? AND id <> ?",
                (r["kind"], e, r["id"]),
            ).fetchone()
            if clash:
                log.warning("phone channel #%s (%s) normalises to %s owned by contact %s — left as is",
                            r["id"], r["value"], e, clash["contact_id"])
                continue
            c.execute("UPDATE contact_channels SET value = ? WHERE id = ?", (e, r["id"]))
            changed += 1
    if changed:
        log.info("normalised %d phone channel(s) to E.164", changed)
    return changed


def _now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


# ─── one-off: mine signatures of mail already on file ────────────────

_scan_state: Dict[str, Any] = {"state": "idle", "contacts": 0, "messages": 0, "proposals": 0, "message": ""}


def scan_status() -> Dict[str, Any]:
    return dict(_scan_state)


def scan_email_signatures(*, per_sender: int = 3, max_messages: int = 5000) -> Dict[str, Any]:
    """Run the signature bridge over mail that is already in the database:
    for every contact with an email address, its latest `per_sender`
    inbound messages. Creates proposals only, exactly like a live inbound
    mail would. Idempotent — existing pending proposals are not duplicated."""
    from .contact_autocapture import _signature_bridge
    with conn_ctx() as c:
        before = c.execute("SELECT COUNT(*) AS n FROM contact_proposals WHERE status = 'pending'").fetchone()["n"]
        rows = c.execute(
            "SELECT ch.contact_id, ch.value AS email, co.status "
            "FROM contact_channels ch JOIN contacts co ON co.id = ch.contact_id "
            "WHERE ch.kind = 'email' AND co.status IN ('active', 'pending')"
        ).fetchall()
        todo: List[Tuple[int, int, bool]] = []
        for r in rows:
            msgs = c.execute(
                "SELECT id FROM email_messages WHERE is_sent = 0 AND from_email = ? "
                "AND body_text IS NOT NULL ORDER BY date_received DESC NULLS LAST LIMIT ?",
                (r["email"], per_sender),
            ).fetchall()
            for m in msgs:
                todo.append((int(r["contact_id"]), int(m["id"]), r["status"] == "active"))
            if len(todo) >= max_messages:
                break
    contacts = {t[0] for t in todo}
    _scan_state.update(state="running", contacts=len(contacts), messages=len(todo), proposals=0, message="")
    # Numbers that show up under several different senders are hotlines
    # or the household's own — never a reason to merge anyone.
    from .contact_autocapture import signature_phones
    seen_by: Dict[str, set] = {}
    with conn_ctx() as c:
        for cid, mid, _ in todo:
            r = c.execute("SELECT body_text FROM email_messages WHERE id = ?", (mid,)).fetchone()
            for e in signature_phones((r["body_text"] if r else "") or ""):
                seen_by.setdefault(e, set()).add(cid)
    ignore = {e for e, who in seen_by.items() if len(who) >= 3}
    for i, (cid, mid, allow_add) in enumerate(todo, 1):
        _signature_bridge(cid, mid, allow_add, ignore_numbers=ignore, min_occurrences=2)
        if i % 50 == 0:
            _scan_state["message"] = f"{i}/{len(todo)}"
    with conn_ctx() as c:
        after = c.execute("SELECT COUNT(*) AS n FROM contact_proposals WHERE status = 'pending'").fetchone()["n"]
    _scan_state.update(state="done", proposals=int(after - before), message="")
    log.info("signature scan: %d contacts, %d messages, %d new proposal(s)", len(contacts), len(todo), after - before)
    return {"contacts": len(contacts), "messages": len(todo), "proposals": int(after - before)}

