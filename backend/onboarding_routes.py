"""First-run onboarding state — drives the welcome wizard on the home
screen. Per-user, stored as a key in app_settings so we don't need a
schema change. The wizard itself lives in the React shell; this module
is just persistence + a derived "what's already set up?" snapshot the
frontend uses to skip steps the user has already completed.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from .auth_sessions import current_user
from .database import get_conn

router = APIRouter(prefix="/api/onboarding", tags=["onboarding"])


def _key(user_id: str) -> str:
    return f"onboarding_done_user_{user_id}"


@router.get("/state")
def get_state(user: dict = Depends(current_user)) -> dict:
    """Return whether the user has completed onboarding plus a snapshot
    of what's already wired up. The snapshot lets the wizard skip steps
    a maintainer pre-configured before sharing the box AND drives the
    Home → DemoDataPanel decision: never offer 'load example data' when
    there's already real content of any kind."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (_key(user["id"]),)
        ).fetchone()
        completed = bool(row and row["value"] == "true")

        # "Already set up" probes — cheap COUNT queries against the
        # tables a fresh box would have empty.
        # has_email stays per-user (the wizard asks each user to connect
        # THEIR mailbox); the rest are server-wide signals so anyone's
        # activity hides the demo CTA from the admin.
        email_accounts = conn.execute(
            "SELECT COUNT(*) AS n FROM email_accounts WHERE owner_user_id = ?",
            (user["id"],),
        ).fetchone()["n"]
        events_count = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
        bills_count  = conn.execute("SELECT COUNT(*) AS n FROM bills").fetchone()["n"]
        tasks_count  = conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
        wa_chats     = conn.execute("SELECT COUNT(*) AS n FROM wa_chats").fetchone()["n"]

    # Documents (Paperless mirror) — count distinct mirrored docs in
    # the local vector index. No Paperless round-trip, cheap on every
    # /home load.
    has_documents = False
    try:
        from .documents import DOCS_DB_PATH
        from .database import get_docs_conn
        with get_docs_conn(DOCS_DB_PATH) as dconn:
            n = dconn.execute(
                "SELECT COUNT(DISTINCT paperless_doc_id) AS n FROM paperless_chunks"
            ).fetchone()["n"]
            has_documents = n > 0
    except Exception:  # noqa: BLE001 — docs.db may not exist on first boot
        pass

    return {
        "completed":      completed,
        "has_email":      email_accounts > 0,
        "has_events":     events_count > 0,
        "has_bills":      bills_count > 0,
        "has_tasks":      tasks_count > 0,
        "has_documents":  has_documents,
        "has_photos":     _has_photos_cached(),
        "has_whatsapp":   wa_chats > 0,
    }


# Cache the Immich photo-count answer for a minute so /onboarding/state
# stays cheap on every Home page reload. False is the safe default when
# Immich is unreachable — it only hides a "try example data" CTA, so a
# transient miss just keeps the CTA visible for one extra cycle.
_PHOTOS_CACHE: dict = {"ts": 0.0, "value": False}
_PHOTOS_TTL_S = 60.0


def _has_photos_cached() -> bool:
    import time
    now = time.time()
    if now - _PHOTOS_CACHE["ts"] < _PHOTOS_TTL_S:
        return _PHOTOS_CACHE["value"]
    value = False
    try:
        from . import credential_store
        import requests as _req
        creds = credential_store.get("immich") or {}
        base = (creds.get("base_url") or "").rstrip("/")
        key  = creds.get("api_key") or ""
        if base and key:
            r = _req.get(
                f"{base}/api/server/statistics",
                headers={"x-api-key": key},
                timeout=2.0,
            )
            if r.ok:
                body = r.json() or {}
                value = int(body.get("photos") or 0) > 0 \
                        or int(body.get("videos") or 0) > 0
    except Exception:  # noqa: BLE001
        pass
    _PHOTOS_CACHE["ts"] = now
    _PHOTOS_CACHE["value"] = value
    return value


@router.post("/complete")
def mark_complete(user: dict = Depends(current_user)) -> dict:
    """Called when the user finishes (or dismisses) the wizard. We treat
    'skip' the same as 'done' — the wizard is informational, not
    enforced."""
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value, updated_at) "
            "VALUES (?, 'true', datetime('now'))",
            (_key(user["id"]),),
        )
        conn.commit()
    return {"ok": True}
