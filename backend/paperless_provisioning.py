"""Phase B.3 — Paperless group + membership provisioning for shared spaces.

Bridges the Yorik spaces ACL into Paperless's group/permission model so
documents uploaded under a shared space (Household, Finance, Customers,
etc.) automatically gain `view_groups` / `change_groups` matching the
right Paperless group, and Yorik users mapped to Paperless accounts get
their group memberships kept in sync as they're added to / removed from
spaces.

Design:
- One Paperless group per Yorik shared space, named by the space slug.
  Personal spaces don't get a Paperless group (owner-only via
  `owner = <paperless_user_id>` on the document instead).
- Yorik admin token (app_settings.paperless_api_token) drives every
  provisioning call.
- Users without a `paperless_user_id` mapping are silently skipped with
  a one-line WARN — the existing per-user connector setup flow handles
  account creation; provisioning never silently creates accounts
  itself (would invent passwords + bypass the consent step).
- All operations idempotent: re-running sync_space is safe; the drift
  detector (B.6) will call it hourly.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import requests

from .database import conn_ctx, DEFAULT_DB_PATH as _DB

log = logging.getLogger("yorik.paperless_provisioning")

_TIMEOUT = 8


# ─── HTTP helpers (admin-scoped) ────────────────────────────────────


def _settings() -> Optional[dict[str, str]]:
    with conn_ctx(_DB) as c:
        base = c.execute(
            "SELECT value FROM app_settings WHERE key='paperless_base_url'"
        ).fetchone()
        tok = c.execute(
            "SELECT value FROM app_settings WHERE key='paperless_api_token'"
        ).fetchone()
    if not base or not tok or not tok["value"]:
        log.warning("paperless_provisioning: admin token not configured — skipping")
        return None
    return {
        "base_url": (base["value"] or "http://localhost:8010").rstrip("/"),
        "token":    tok["value"],
    }


def _headers(s: dict[str, str]) -> dict[str, str]:
    return {
        "Authorization": f"Token {s['token']}",
        "Accept":        "application/json",
    }


def _get(s, path, **kw):
    return requests.get(f"{s['base_url']}{path}", headers=_headers(s), timeout=_TIMEOUT, **kw)


def _post(s, path, json_body):
    h = {**_headers(s), "Content-Type": "application/json"}
    return requests.post(f"{s['base_url']}{path}", json=json_body, headers=h, timeout=_TIMEOUT)


def _patch(s, path, json_body):
    h = {**_headers(s), "Content-Type": "application/json"}
    return requests.patch(f"{s['base_url']}{path}", json=json_body, headers=h, timeout=_TIMEOUT)


# ─── Group lifecycle ────────────────────────────────────────────────


def ensure_group_for_space(space_id: int) -> Optional[int]:
    """Return the Paperless group id for the Yorik shared space, creating
    one if it doesn't exist. Returns None for personal spaces (no group
    needed) or when Paperless isn't configured.

    Both the create-path and the existing-group lookup additively apply
    paperless_visibility._BASELINE_GROUP_PERMS so per-space groups grant
    members the same baseline view/CRUD-on-own access as the household
    and business groups. Without this, non-admin members of a
    per-space group land in a 'present but powerless' state — exactly
    the empty-permissions bug we hit on the household group itself.

    Phase C T11: spaces in workspaces beyond the founder's (id=1) get
    a `ws{N}-` prefix on the Paperless group name so cross-workspace
    collisions are impossible. Workspace 1's groups stay unprefixed for
    backwards-compatibility with the existing `household`/`business`/
    per-space groups in single-family installs.
    """
    with conn_ctx(_DB) as c:
        space = c.execute(
            "SELECT id, name, kind, slug, workspace_id FROM spaces WHERE id=?", (space_id,)
        ).fetchone()
    if not space:
        log.warning("ensure_group_for_space: no such space %s", space_id)
        return None
    if space["kind"] != "shared":
        return None
    s = _settings()
    if not s:
        return None

    from . import paperless_visibility as _pv
    base = s["base_url"].rstrip("/")
    headers = _headers(s)

    base_name = (space["slug"] or space["name"] or f"space-{space_id}").lower()
    workspace_id = space["workspace_id"]
    if workspace_id is not None and int(workspace_id) > 1:
        target_name = f"ws{int(workspace_id)}-{base_name}"
    else:
        target_name = base_name
    r = _get(s, "/api/groups/", params={"name": target_name})
    if r.status_code != 200:
        log.warning("ensure_group_for_space: list returned %s", r.status_code)
        return None
    results = (r.json() or {}).get("results") or []
    for g in results:
        if (g.get("name") or "").lower() == target_name:
            gid = int(g["id"])
            _pv._patch_group_permissions(base, headers, gid,
                                          g.get("permissions") or [])
            return gid
    # Not found → create with the baseline already attached.
    r = _post(s, "/api/groups/", {
        "name": target_name,
        "permissions": list(_pv._BASELINE_GROUP_PERMS),
    })
    if r.status_code not in (200, 201):
        log.warning("ensure_group_for_space: create returned %s: %s",
                    r.status_code, r.text[:200])
        return None
    return int(r.json()["id"])


# ─── Membership sync ────────────────────────────────────────────────


def sync_space(space_id: int) -> dict[str, Any]:
    """Reconcile Paperless group membership against Yorik's space_members
    for one space. Idempotent. Returns a summary dict for the drift
    detector + callers that want to surface "n users added / m removed"
    in the UI."""
    s = _settings()
    if not s:
        return {"skipped": "paperless not configured"}
    group_id = ensure_group_for_space(space_id)
    if group_id is None:
        return {"skipped": "personal space or provisioning failed"}

    # Yorik side: Paperless ids of every user in this space (with a
    # mapped paperless_user_id). Users without a mapping are warned +
    # skipped (their access has to be granted manually until they hook
    # up the connector).
    yorik_paperless_ids: set[int] = set()
    skipped_users: list[int] = []
    with conn_ctx(_DB) as c:
        rows = c.execute(
            "SELECT u.id AS user_id, u.name, u.paperless_user_id "
            "FROM space_members sm JOIN user_profiles u ON u.id = sm.user_id "
            "WHERE sm.space_id = ?",
            (space_id,),
        ).fetchall()
    for r in rows:
        if r["paperless_user_id"] is None:
            skipped_users.append(r["user_id"])
            continue
        yorik_paperless_ids.add(int(r["paperless_user_id"]))

    # Paperless side: who currently has the group. Paperless's
    # `groups__id` query filter silently ignores unknown params, so
    # filter client-side against each user's `groups` list instead.
    r = _get(s, "/api/users/", params={"page_size": 200})
    if r.status_code != 200:
        return {"error": f"users list returned {r.status_code}"}
    current_paperless_ids = {
        int(u["id"]) for u in (r.json() or {}).get("results") or []
        if int(group_id) in (u.get("groups") or [])
    }

    to_add    = yorik_paperless_ids - current_paperless_ids
    to_remove = current_paperless_ids - yorik_paperless_ids

    added: list[int] = []
    removed: list[int] = []
    for pid in to_add:
        if _set_user_groups(s, pid, add_group_id=group_id):
            added.append(pid)
    for pid in to_remove:
        if _set_user_groups(s, pid, remove_group_id=group_id):
            removed.append(pid)

    return {
        "space_id":      space_id,
        "paperless_group_id": group_id,
        "added":         added,
        "removed":       removed,
        "skipped_users": skipped_users,
    }


def _set_user_groups(s, paperless_user_id: str,
                     *, add_group_id: Optional[int] = None,
                     remove_group_id: Optional[int] = None) -> bool:
    """PATCH a Paperless user's `groups` list, adding/removing a group.
    Returns True on success, logs + returns False on failure."""
    r = _get(s, f"/api/users/{paperless_user_id}/")
    if r.status_code != 200:
        log.warning("set_user_groups: GET user %s -> %s", paperless_user_id, r.status_code)
        return False
    current = set(r.json().get("groups") or [])
    if add_group_id is not None:
        current.add(int(add_group_id))
    if remove_group_id is not None:
        current.discard(int(remove_group_id))
    r = _patch(s, f"/api/users/{paperless_user_id}/", {"groups": sorted(current)})
    if r.status_code not in (200, 202):
        log.warning("set_user_groups: PATCH user %s -> %s: %s",
                    paperless_user_id, r.status_code, r.text[:200])
        return False
    return True


def sync_all_shared_spaces() -> list[dict[str, Any]]:
    """Reconcile every shared space. Called by the drift detector + on
    startup for a one-shot lazy reconciliation."""
    with conn_ctx(_DB) as c:
        ids = [r["id"] for r in c.execute(
            "SELECT id FROM spaces WHERE kind='shared'"
        ).fetchall()]
    return [sync_space(sid) for sid in ids]


# ─── Per-document permission assignment ─────────────────────────────


def set_document_space(paperless_doc_id: int, space_id: int) -> bool:
    """Pin a Paperless document to a Yorik space — set view_groups +
    change_groups on the document so only that group's members can see
    /edit it. Personal-space docs get owner=<paperless_user> instead;
    pass `space_id` of the owner's personal space to use that path."""
    s = _settings()
    if not s:
        return False
    with conn_ctx(_DB) as c:
        space = c.execute(
            "SELECT id, kind, owner_user_id FROM spaces WHERE id=?", (space_id,)
        ).fetchone()
    if not space:
        return False
    if space["kind"] == "personal":
        # Personal: owner-only. Find paperless_user_id for the owner.
        with conn_ctx(_DB) as c:
            pid = c.execute(
                "SELECT paperless_user_id FROM user_profiles WHERE id=?",
                (space["owner_user_id"],),
            ).fetchone()
        if not pid or pid["paperless_user_id"] is None:
            log.warning(
                "set_document_space: personal-space owner %s has no paperless_user_id",
                space["owner_user_id"],
            )
            return False
        r = _patch(s, f"/api/documents/{paperless_doc_id}/", {
            "owner": int(pid["paperless_user_id"]),
            "permissions": {"view": {"users": [], "groups": []},
                            "change": {"users": [], "groups": []}},
        })
        return r.status_code in (200, 202)
    # Shared: clear owner, set view+change groups to this space's group.
    group_id = ensure_group_for_space(space_id)
    if group_id is None:
        return False
    r = _patch(s, f"/api/documents/{paperless_doc_id}/", {
        "owner": None,
        "permissions": {
            "view":   {"users": [], "groups": [group_id]},
            "change": {"users": [], "groups": [group_id]},
        },
    })
    if r.status_code not in (200, 202):
        log.warning("set_document_space: PATCH doc %s -> %s: %s",
                    paperless_doc_id, r.status_code, r.text[:200])
        return False
    return True
