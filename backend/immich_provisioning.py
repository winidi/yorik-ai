"""Phase B.4 — Immich album provisioning for shared spaces.

Each Yorik shared space maps to an Immich shared album:
- Album owner = the admin who creates it (Immich constraint).
- Album members = the rest of the space's members at level=editor
  (write/admin space level) or level=viewer (read space level).
- Personal spaces don't get an album — photos in personal-space land
  stay in the user's own Immich library.

Honest limits:
- Immich shares whole albums, never individual photos. "Share this one
  photo" is implemented as a one-photo album. Don't pretend otherwise
  in the UI.
- Album owner is fixed at creation — can't be re-owned. We pick the
  workspace admin once; if that admin is removed from Yorik later, the
  album survives but new operations need a fresh admin. Migration of
  ownership is a future concern (Phase C).
- Asset placement (uploading new photos to the right album) happens at
  ingest time, separate from this module. This file owns groups +
  membership only.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import requests

from . import credential_store
from .database import conn_ctx, DEFAULT_DB_PATH as _DB

log = logging.getLogger("yorik.immich_provisioning")

_TIMEOUT = 8


def _settings() -> Optional[dict[str, str]]:
    creds = credential_store.get("immich") or {}
    base = (creds.get("base_url") or "").rstrip("/")
    key = creds.get("api_key") or ""
    if not (base and key):
        log.warning("immich_provisioning: api_key/base_url not configured — skipping")
        return None
    return {"base_url": base, "api_key": key}


def _headers(s) -> dict[str, str]:
    return {
        "x-api-key":   s["api_key"],
        "Accept":      "application/json",
        "Content-Type":"application/json",
    }


def _get(s, path, **kw):
    return requests.get(f"{s['base_url']}{path}", headers=_headers(s), timeout=_TIMEOUT, **kw)


def _post(s, path, body):
    return requests.post(f"{s['base_url']}{path}", json=body, headers=_headers(s), timeout=_TIMEOUT)


def _put(s, path, body):
    return requests.put(f"{s['base_url']}{path}", json=body, headers=_headers(s), timeout=_TIMEOUT)


def _delete(s, path):
    return requests.delete(f"{s['base_url']}{path}", headers=_headers(s), timeout=_TIMEOUT)


# ─── Album lifecycle ────────────────────────────────────────────────


def ensure_album_for_space(space_id: int) -> Optional[str]:
    """Return the Immich album id (UUID) for a Yorik shared space,
    creating it if needed. Returns None for personal spaces (no album)
    or when Immich isn't configured.

    Phase C T12: spaces in workspaces beyond the founder's (id=1) get
    a `ws{N}-` prefix on the Immich album name so cross-workspace
    collisions are impossible (without this, WS1's 'shared' and WS2's
    'shared' would both resolve to the same album because the lookup
    is by name on the admin-scoped /api/albums list). Workspace 1's
    albums stay unprefixed for backwards-compatibility with the
    existing single-family install."""
    with conn_ctx(_DB) as c:
        space = c.execute(
            "SELECT id, name, kind, slug, workspace_id FROM spaces WHERE id=?", (space_id,)
        ).fetchone()
    if not space or space["kind"] != "shared":
        return None
    s = _settings()
    if not s:
        return None

    base_name = (space["slug"] or space["name"] or f"space-{space_id}").lower()
    workspace_id = space["workspace_id"]
    if workspace_id is not None and int(workspace_id) > 1:
        target_name = f"ws{int(workspace_id)}-{base_name}"
    else:
        target_name = base_name
    # Immich /api/albums lists albums the API caller owns. Idempotent
    # match by name.
    r = _get(s, "/api/albums")
    if r.status_code != 200:
        log.warning("ensure_album_for_space: list returned %s", r.status_code)
        return None
    for a in r.json() or []:
        if (a.get("albumName") or "").lower() == target_name:
            return a["id"]
    r = _post(s, "/api/albums", {
        "albumName":   target_name,
        "description": f"Yorik shared space '{space['name']}' (auto-managed)",
    })
    if r.status_code not in (200, 201):
        log.warning("ensure_album_for_space: create returned %s: %s",
                    r.status_code, r.text[:200])
        return None
    return r.json().get("id")


# ─── Membership sync ────────────────────────────────────────────────


def sync_space(space_id: int) -> dict[str, Any]:
    """Reconcile Immich album members against Yorik's space_members for
    one shared space. Idempotent. Returns a summary for the drift
    detector + UI surfacing."""
    s = _settings()
    if not s:
        return {"skipped": "immich not configured"}
    album_id = ensure_album_for_space(space_id)
    if album_id is None:
        return {"skipped": "personal space or provisioning failed"}

    # Fetch current album state — including owner + members.
    r = _get(s, f"/api/albums/{album_id}")
    if r.status_code != 200:
        return {"error": f"album fetch returned {r.status_code}"}
    album = r.json() or {}
    owner_id = album.get("ownerId")
    current_users: dict[str, str] = {
        au["user"]["id"]: au.get("role", "viewer")
        for au in (album.get("albumUsers") or [])
        if au.get("user") and au["user"].get("id")
    }

    # Build Yorik-side desired state. Owner is never re-added (Immich
    # rejects). Users without immich_user_id mapping get skipped (the
    # connector setup flow handles account creation; never auto-make
    # accounts here — would mint passwords + bypass consent).
    desired: dict[str, str] = {}
    skipped_users: list[int] = []
    with conn_ctx(_DB) as c:
        rows = c.execute(
            "SELECT u.id AS user_id, u.immich_user_id, sm.level "
            "FROM space_members sm JOIN user_profiles u ON u.id = sm.user_id "
            "WHERE sm.space_id = ?",
            (space_id,),
        ).fetchall()
    for r in rows:
        if not r["immich_user_id"]:
            skipped_users.append(r["user_id"])
            continue
        if r["immich_user_id"] == owner_id:
            continue
        # Yorik write/admin → Immich editor; Yorik read → Immich viewer.
        desired[r["immich_user_id"]] = (
            "editor" if r["level"] in ("write", "admin") else "viewer"
        )

    to_add    = {uid: role for uid, role in desired.items() if uid not in current_users}
    to_remove = [uid for uid in current_users if uid not in desired]
    to_update = {
        uid: role for uid, role in desired.items()
        if uid in current_users and current_users[uid] != role
    }

    added: list[str] = []
    removed: list[str] = []
    updated: list[str] = []

    if to_add:
        r = _put(s, f"/api/albums/{album_id}/users", {
            "albumUsers": [{"userId": uid, "role": role} for uid, role in to_add.items()],
        })
        if r.status_code in (200, 201, 204):
            added = list(to_add.keys())
        else:
            log.warning("immich add users: %s -> %s: %s",
                        album_id, r.status_code, r.text[:200])

    for uid in to_remove:
        r = _delete(s, f"/api/albums/{album_id}/user/{uid}")
        if r.status_code in (200, 204):
            removed.append(uid)
        else:
            log.warning("immich remove user %s: %s: %s",
                        uid, r.status_code, r.text[:200])

    for uid, role in to_update.items():
        r = _put(s, f"/api/albums/{album_id}/user/{uid}", {"role": role})
        if r.status_code in (200, 204):
            updated.append(uid)
        else:
            log.warning("immich update role %s -> %s: %s: %s",
                        uid, role, r.status_code, r.text[:200])

    return {
        "space_id":   space_id,
        "album_id":   album_id,
        "added":      added,
        "removed":    removed,
        "updated":    updated,
        "skipped_users": skipped_users,
    }


def sync_all_shared_spaces() -> list[dict[str, Any]]:
    with conn_ctx(_DB) as c:
        ids = [r["id"] for r in c.execute(
            "SELECT id FROM spaces WHERE kind='shared'"
        ).fetchall()]
    return [sync_space(sid) for sid in ids]
