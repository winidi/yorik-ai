"""Diagnostics — what Yorik may tell its makers, and only with consent.

Three tiers, each its own switch, all off until the admin says yes
(Settings → Privacy, or the one-time step after setup):

  1  counts   — version, platform, how many people, how often the chat
                is used; numbers in buckets, never content
  2  usage    — which kinds of features are used (by type, not by name
                of anything in the house)
  3  errors   — a report when something went wrong, assembled here,
                pseudonymised (person_7, email_2 …), shown to the person
                whose conversation it was, sent only on their click

Identity: a random install id and a secret, both made once and kept in
household_settings; "reset identity" makes new ones and forgets every
pseudonym. The collector is the small receiver Dirk runs (docs/
DIAGNOSTICS.md); an empty address means "collect locally, send nothing".

Plan and rules: ~/.claude/plans (2026-10-06) and docs/DIAGNOSTICS.md.
Modules: pseudonyms (tokens), registry (the allow-list of fields),
scrub (text → safe text), report (assembling), outbox (sending), usage
(the daily counts), routes (the API).
"""

from __future__ import annotations

import base64
import logging
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

log = logging.getLogger("yorik.diagnostics")

KEY_ASKED = "diag.consent_asked_at"
KEY_TIER = {1: "diag.tier_counts", 2: "diag.tier_usage", 3: "diag.tier_errors"}
KEY_INSTALL_ID = "diag.install_id"
KEY_SECRET = "diag.pseudonym_secret"
KEY_COLLECTOR = "diag.collector_url"
TIERS = {1: "counts", 2: "usage", 3: "errors"}
SCHEMA_VERSION = 1


def _get(key: str, default: str = "") -> str:
    from backend.household_settings import get_setting
    return get_setting(key, default=default)


def _set(key: str, value: str, user_id: Optional[str] = None) -> None:
    from backend.household_settings import set_setting
    set_setting(key, value, updated_by_user_id=user_id)


def consent() -> Dict[str, Any]:
    """The state of the three switches and whether the admin was asked."""
    asked = _get(KEY_ASKED)
    return {
        "asked": bool(asked),
        "asked_at": asked or None,
        "counts": _get(KEY_TIER[1], "0") == "1",
        "usage": _get(KEY_TIER[2], "0") == "1",
        "errors": _get(KEY_TIER[3], "0") == "1",
    }


def enabled(tier: int) -> bool:
    """Is this tier switched on? Tier 2 implies 1 for the daily row,
    but every switch is read on its own, nothing is inferred."""
    return _get(KEY_TIER[tier], "0") == "1"


def any_enabled() -> bool:
    return any(enabled(t) for t in TIERS)


def set_consent(*, counts: bool, usage: bool, errors: bool, user_id: Optional[str]) -> Dict[str, Any]:
    """The admin's answer. Recorded with the time, so the step after
    setup is shown once and the privacy page can say when."""
    _set(KEY_TIER[1], "1" if counts else "0", user_id)
    _set(KEY_TIER[2], "1" if usage else "0", user_id)
    _set(KEY_TIER[3], "1" if errors else "0", user_id)
    _set(KEY_ASKED, datetime.now(timezone.utc).isoformat(timespec="seconds"), user_id)
    if any((counts, usage, errors)):
        install_id()            # make the identity now, so the first report has one
        secret()
    log.info("diagnostics consent: counts=%s usage=%s errors=%s", counts, usage, errors)
    return consent()


def install_id() -> str:
    """A random id for this installation, made once. Says nothing about
    the house; lets the collector tell two reports of one install apart."""
    cur = _get(KEY_INSTALL_ID)
    if cur:
        return cur
    new = str(uuid.uuid4())
    _set(KEY_INSTALL_ID, new)
    return new


def secret() -> bytes:
    """The key behind every pseudonym (HMAC-SHA256). Made once, kept only
    here; the collector never sees it, so no token can be turned back."""
    cur = _get(KEY_SECRET)
    if cur:
        try:
            return base64.b64decode(cur)
        except ValueError:
            pass
    raw = secrets.token_bytes(32)
    _set(KEY_SECRET, base64.b64encode(raw).decode("ascii"))
    return raw


def reset_identity(user_id: Optional[str] = None) -> Dict[str, Any]:
    """Forget: new install id, new secret, every pseudonym dropped, the
    local queue emptied. Reports already sent get a deletion request
    (outbox.request_deletions) before the tokens are gone."""
    from backend.database import get_conn
    dropped = 0
    try:
        from . import outbox
        outbox.request_deletions()
    except Exception as exc:  # noqa: BLE001 — forgetting locally must not wait for the net
        log.info("diagnostics: deletion requests skipped: %s", exc)
    with get_conn() as conn:
        dropped = conn.execute("DELETE FROM diag_pseudonyms").rowcount or 0
        conn.execute("DELETE FROM diag_reports WHERE status IN ('draft', 'queued', 'failed')")
        conn.commit()
    _set(KEY_INSTALL_ID, str(uuid.uuid4()), user_id)
    _set(KEY_SECRET, base64.b64encode(secrets.token_bytes(32)).decode("ascii"), user_id)
    log.info("diagnostics identity reset: %d pseudonym(s) forgotten", dropped)
    return {"ok": True, "pseudonyms_forgotten": int(dropped), "install_id": install_id()}


def collector_url() -> str:
    """Where reports go; empty means they stay in the house."""
    import os
    return (_get(KEY_COLLECTOR) or os.getenv("YORIK_DIAG_COLLECTOR_URL", "")).rstrip("/")
