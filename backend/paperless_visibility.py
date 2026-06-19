"""
Paperless visibility — tag-based separation.

Yorik keeps every document in ONE Paperless instance (no double infra)
and uses Paperless's own per-tag permission model to wall off private
from business from shared. The mapping:

  * private   → no one but the owner + admin sees this. Default for
                new uploads. Implemented as: NO visibility tag at all
                + Paperless owner permission (which Paperless already
                enforces as "owner-only").
  * business  → visible to everyone in the Paperless 'business' group.
                Used by employees in business mode; also the right
                tag for things like invoices, contracts, tax docs the
                household tracks together.
  * shared    → visible to everyone in the Paperless 'household' group.
                For things like the lease agreement, school emergency
                contacts, kids' insurance — anything every adult in
                the box should be able to pull up.

Each visibility level corresponds to a Paperless TAG. The tag carries
view permission to the matching Paperless group. Owner stays the user
who uploaded; tag adds the group-wide visibility on top.

This module is the gateway:
  * ensure_tags()           — idempotent seed on startup
  * resolve_visibility_tag — visibility → tag id (cached after first call)
  * apply_to_upload         — pass-through helper for the upload path
  * downgrade_visibility    — for the "change visibility" UI action
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

import requests

log = logging.getLogger(__name__)


# Cache the resolved tag ids so we don't round-trip Paperless on every
# upload. ensure_tags() repopulates; lookups are O(1) after that.
_TAG_ID_CACHE: Dict[str, int] = {}
_CACHE_LOCK = threading.Lock()


# Group names mirror the tag names so Paperless permissions feel natural
# ("household members can see anything tagged shared").
GROUPS = {
    "business": "business",
    "shared":   "household",
}

VISIBILITY_LEVELS = ("private", "business", "shared")
VISIBILITY_TAG_NAMES = {
    # "private" intentionally has no tag — Paperless owner-only is the
    # default permission and we don't want a stray tag to widen access.
    "business": "business",
    "shared":   "shared",
}
TIMEOUT_S = 15


def _settings() -> Dict[str, Any]:
    """Resolve Paperless admin creds from app_settings. We need the
    admin token to manage tags + groups (those endpoints are admin-only
    in Paperless)."""
    from .connectors.paperless import _settings as _ps
    s = _ps()
    return {
        "base_url": (s.get("base_url") or "http://localhost:8010").rstrip("/"),
        "api_key":  s.get("api_key"),
    }


def _admin_headers() -> Dict[str, str]:
    s = _settings()
    return {"Authorization": f"Token {s['api_key']}", "Accept": "application/json"}


def ensure_tags() -> Dict[str, int]:
    """Idempotent: make sure the visibility tags + groups exist in
    Paperless. Returns {tag_name: tag_id}. Safe to call repeatedly —
    on startup, before any upload, etc. Logs and returns {} if Paperless
    isn't reachable, so the upload path can still proceed (tag-less,
    private-by-default)."""
    s = _settings()
    if not s.get("api_key"):
        return {}
    base = s["base_url"]
    headers = _admin_headers()

    # Ensure groups first — the tag's set_permissions step references
    # group ids, so they need to exist before the tag is created.
    group_ids = _ensure_groups(base, headers)

    # Default-owner machinery, in order:
    #   1. Find the canonical default owner (first active superuser).
    #   2. Backfill any historical owner=None docs to that owner so
    #      delete_document in the baseline doesn't expose them.
    #   3. Register the Paperless Workflow that keeps new consume-folder
    #      docs from going back into the owner=None state.
    # All three are best-effort and idempotent.
    default_owner_id = _first_superuser_id(base, headers)
    if default_owner_id is not None:
        _backfill_ownerless_documents(base, headers, default_owner_id)
        _ensure_default_owner_workflow(base, headers, default_owner_id)

    out: Dict[str, int] = {}
    try:
        for tag_name in VISIBILITY_TAG_NAMES.values():
            tag_id = _get_or_create_tag(base, headers, tag_name)
            if tag_id is None:
                continue
            out[tag_name] = tag_id

            # Grant view permission on the tag to the matching group.
            # That way every document tagged with this tag inherits
            # view-by-group for free (Paperless's default propagation).
            # NOTE: this is a tag-level permission, not a per-doc one —
            # cheap to set, applies to every doc using the tag.
            target_group = (
                GROUPS["business"] if tag_name == "business"
                else GROUPS["shared"] if tag_name == "shared"
                else None
            )
            if target_group and target_group in group_ids:
                _grant_tag_view_to_group(
                    base, headers, tag_id, group_ids[target_group],
                )
    except Exception as exc:  # noqa: BLE001
        log.warning("ensure_tags: failed mid-setup: %s", exc)

    with _CACHE_LOCK:
        _TAG_ID_CACHE.clear()
        _TAG_ID_CACHE.update(out)
    return out


# Baseline Django permissions every Yorik-created Paperless group must
# carry. Without `view_document` (the key one) a non-superuser hits a 403
# on /api/documents/ before any owner-check or per-tag rule runs — that
# was the user-visible bug where a fresh non-admin user saw nothing on
# /documents even though Yorik's space-membership sync had correctly
# placed them in the right group.
#
# Full CRUD on documents (view + add + change + delete). Paperless's
# object-level "only-owner can modify" check kicks in for every doc
# that HAS an owner — a member trying to delete admin's doc gets a
# 403 even with delete_document granted, so we don't need a Yorik-side
# ACL on top. The catch: docs with owner=None bypass that check —
# anyone with the model-level perm can wipe them. Two complementary
# safeguards live alongside this baseline:
#   - backfill_ownerless_documents() runs at startup and PATCHes every
#     legacy owner=None doc to the first superuser, so historical
#     consume-folder imports get a default owner.
#   - _ensure_default_owner_workflow() creates a Paperless Workflow
#     that auto-assigns the same owner to every NEW consume-folder
#     ingest, so the ownerless state can't recur for new docs.
# Together those mean every doc Paperless knows about has a real
# owner, which makes delete_document safe to grant: members can wipe
# their own uploads, admin keeps everything else, the historical pile
# is admin-owned and untouchable by members.
#
# Read-only on tag/correspondent/documenttype: members can use existing
# organisational metadata for their own uploads but can't invent new
# tags or rename/delete shared ones, which would otherwise let one
# user's housekeeping clutter or break everyone else's filters. Admin
# (= Paperless superuser) keeps the full surface for system upkeep.
#
# Codename format here is the bare Django codename (no app_label prefix)
# — Paperless's GroupSerializer accepts strings exactly in this form
# and rejects the dotted "documents.view_document" variant.
_BASELINE_GROUP_PERMS: tuple[str, ...] = (
    "view_document",
    "add_document",
    "change_document",
    "delete_document",
    "view_tag",
    "view_correspondent",
    "view_documenttype",
    "view_note",
    "view_storagepath",
    "add_note",
)


def _patch_group_permissions(
    base: str, headers: Dict[str, str], group_id: int, current: list[str],
) -> bool:
    """Additively reconcile a group's permissions against the baseline.
    Returns True if a PATCH was made (or wasn't needed); False on error.
    Never strips permissions an admin added manually — only adds the
    ones we expect."""
    have = set(current or [])
    want = set(_BASELINE_GROUP_PERMS)
    missing = want - have
    if not missing:
        return True
    merged = sorted(have | want)
    try:
        r = requests.patch(
            f"{base}/api/groups/{group_id}/",
            headers={**headers, "Content-Type": "application/json"},
            json={"permissions": merged},
            timeout=TIMEOUT_S,
        )
        if not r.ok:
            log.warning("patch group %d permissions failed: HTTP %d %s",
                        group_id, r.status_code, r.text[:200])
            return False
        log.info("paperless_visibility: added %d baseline perm(s) to group %d (now has %d)",
                 len(missing), group_id, len(merged))
        return True
    except requests.RequestException as exc:
        log.warning("patch group %d permissions raised: %s", group_id, exc)
        return False


def _ensure_groups(base: str, headers: Dict[str, str]) -> Dict[str, int]:
    """Create the 'business' and 'household' Paperless groups if missing,
    seeded with the baseline permission set. Reconciles existing groups
    additively so installs that pre-date the baseline self-heal on
    next startup. Returns {group_name: group_id}. Best-effort — empty
    dict on failure means subsequent tag→group grants just get skipped."""
    out: Dict[str, int] = {}
    try:
        r = requests.get(f"{base}/api/groups/", headers=headers, timeout=TIMEOUT_S)
        existing = {g["name"]: g for g in (r.json().get("results") or [])} if r.ok else {}
        for group_name in GROUPS.values():
            if group_name in existing:
                g = existing[group_name]
                out[group_name] = int(g["id"])
                # Self-heal: bring an existing group up to the baseline.
                _patch_group_permissions(base, headers, int(g["id"]),
                                          g.get("permissions") or [])
                continue
            cr = requests.post(
                f"{base}/api/groups/", headers=headers,
                json={"name": group_name, "permissions": list(_BASELINE_GROUP_PERMS)},
                timeout=TIMEOUT_S,
            )
            if cr.ok:
                out[group_name] = cr.json()["id"]
                log.info("paperless_visibility: created group '%s' id=%d with %d baseline perm(s)",
                         group_name, out[group_name], len(_BASELINE_GROUP_PERMS))
            else:
                log.warning("create group '%s' failed: HTTP %d %s",
                            group_name, cr.status_code, cr.text[:200])
    except Exception as exc:  # noqa: BLE001
        log.warning("_ensure_groups failed: %s", exc)
    return out


def _first_superuser_id(base: str, headers: Dict[str, str]) -> Optional[int]:
    """Return the id of the first active Paperless superuser, or None.
    Used as the canonical default owner for backfill + the auto-owner
    workflow. We pick the first one because it's stable across restarts
    (Paperless user ids are insertion-order) — admin@yorik.local from
    start.sh's bootstrap always ends up here if it exists, otherwise
    the first Yorik admin who got provisioned."""
    try:
        r = requests.get(f"{base}/api/users/",
                         headers=headers, timeout=TIMEOUT_S)
        if not r.ok:
            return None
        for u in (r.json() or {}).get("results") or []:
            if u.get("is_superuser") and u.get("is_active"):
                return int(u["id"])
    except requests.RequestException:
        pass
    return None


def _backfill_ownerless_documents(
    base: str, headers: Dict[str, str], owner_id: int,
) -> int:
    """PATCH every Paperless doc with owner=None to set owner=<owner_id>.
    Returns the count of docs touched. Idempotent — re-running after a
    full pass returns 0 because the owner__isnull=true filter returns
    nothing.

    Without this, granting non-admin members the delete_document
    permission would let them wipe legacy consume-folder docs (which
    Paperless created with owner=None — no object-level filter applies
    so model-level perm is universal). With the backfill done, every
    historical doc has a real owner and Paperless's ownership check
    keeps members out of admin's pile."""
    backfilled = 0
    next_url = (
        f"{base}/api/documents/?owner__isnull=true"
        "&page_size=100&fields=id&ordering=id"
    )
    try:
        while next_url:
            r = requests.get(next_url, headers=headers, timeout=TIMEOUT_S)
            if not r.ok:
                log.warning("backfill: list returned HTTP %s", r.status_code)
                break
            body = r.json() or {}
            ids = [int(d["id"]) for d in (body.get("results") or [])]
            if not ids:
                break
            patch_headers = {**headers, "Content-Type": "application/json"}
            for did in ids:
                pr = requests.patch(
                    f"{base}/api/documents/{did}/",
                    headers=patch_headers,
                    json={"owner": owner_id},
                    timeout=TIMEOUT_S,
                )
                if pr.ok:
                    backfilled += 1
                else:
                    log.warning("backfill: PATCH doc %d -> HTTP %s",
                                did, pr.status_code)
            # Re-query rather than follow `next` — we just changed the
            # filter's match set, so the cursor would skip pages.
            next_url = (
                f"{base}/api/documents/?owner__isnull=true"
                "&page_size=100&fields=id&ordering=id"
            ) if len(ids) == 100 else None
    except requests.RequestException as exc:
        log.warning("backfill: aborted on RequestException: %s", exc)
    if backfilled:
        log.info("paperless_visibility: backfilled owner=%d on %d ownerless doc(s)",
                 owner_id, backfilled)
    return backfilled


_DEFAULT_OWNER_WORKFLOW_NAME = "Yorik: assign default owner to consume-folder docs"


def _ensure_default_owner_workflow(
    base: str, headers: Dict[str, str], owner_id: int,
) -> Optional[int]:
    """Create the Paperless Workflow that auto-assigns the default
    owner to every newly-consumed doc from the consume folder. Without
    this, fresh consume-folder ingest produces owner=None docs and the
    backfill+delete-perm safety story decays back to broken. Idempotent
    — checks for an existing workflow by name first.

    Scope deliberately limited to source=1 (Consume Folder). API uploads
    (source=2), Mail Fetch (source=3), and Web UI (source=4) all set
    owner from the requesting user's session, so applying a workflow to
    them would silently STEAL ownership from member uploads."""
    try:
        r = requests.get(f"{base}/api/workflows/",
                         headers=headers, timeout=TIMEOUT_S)
        if r.ok:
            for w in (r.json() or {}).get("results") or []:
                if w.get("name") == _DEFAULT_OWNER_WORKFLOW_NAME:
                    return int(w["id"])
        payload = {
            "name":    _DEFAULT_OWNER_WORKFLOW_NAME,
            "order":   0,
            "enabled": True,
            "triggers": [{
                "type":    2,      # Document Added — fires AFTER the doc
                                   # is created, so assign_owner has
                                   # something to write to.
                "sources": [1],    # Consume Folder only — see docstring.
            }],
            "actions": [{
                "type":         1,         # Assignment
                "assign_owner": owner_id,
            }],
        }
        cr = requests.post(
            f"{base}/api/workflows/",
            headers={**headers, "Content-Type": "application/json"},
            json=payload, timeout=TIMEOUT_S,
        )
        if cr.ok:
            wid = int(cr.json()["id"])
            log.info("paperless_visibility: created default-owner workflow id=%d -> user %d",
                     wid, owner_id)
            return wid
        log.warning("create workflow failed: HTTP %d %s",
                    cr.status_code, cr.text[:200])
    except requests.RequestException as exc:
        log.warning("ensure default-owner workflow: %s", exc)
    return None


def _get_or_create_tag(base: str, headers: Dict[str, str], name: str) -> Optional[int]:
    try:
        r = requests.get(
            f"{base}/api/tags/", headers=headers,
            params={"name__iexact": name}, timeout=TIMEOUT_S,
        )
        if r.ok:
            for t in (r.json().get("results") or []):
                if (t.get("name") or "").lower() == name.lower():
                    return int(t["id"])
        # Create. Color matters for the Paperless UI; pick something
        # consistent so the tags are visually distinguishable.
        color = "#ef4444" if name == "private" else (
            "#3b82f6" if name == "business" else "#10b981"
        )
        cr = requests.post(
            f"{base}/api/tags/", headers=headers,
            json={"name": name, "color": color, "matching_algorithm": 0},
            timeout=TIMEOUT_S,
        )
        if cr.ok:
            log.info("paperless_visibility: created tag '%s' id=%d", name, cr.json()["id"])
            return int(cr.json()["id"])
        log.warning("create tag '%s' failed: HTTP %d %s", name, cr.status_code, cr.text[:200])
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("_get_or_create_tag(%s) failed: %s", name, exc)
        return None


def _grant_tag_view_to_group(
    base: str, headers: Dict[str, str], tag_id: int, group_id: int,
) -> None:
    """Set view_groups on the tag's object-level permissions. Best
    effort — if Paperless doesn't have the object-permissions endpoint
    (older versions) we silently skip."""
    try:
        # Paperless 2.x: POST /api/tags/{id}/permissions/ with the full
        # permissions block. We merge into existing rather than blowing
        # them away.
        gp = requests.get(
            f"{base}/api/tags/{tag_id}/permissions/", headers=headers, timeout=TIMEOUT_S,
        )
        current = gp.json() if gp.ok else {}
        view_groups = set(current.get("view", {}).get("groups", []) or [])
        view_groups.add(int(group_id))
        change_groups = set(current.get("change", {}).get("groups", []) or [])
        payload = {
            "view":   {"users": current.get("view", {}).get("users", []) or [],
                       "groups": sorted(view_groups)},
            "change": {"users": current.get("change", {}).get("users", []) or [],
                       "groups": sorted(change_groups)},
        }
        r = requests.post(
            f"{base}/api/tags/{tag_id}/permissions/", headers=headers,
            json=payload, timeout=TIMEOUT_S,
        )
        if not r.ok:
            log.debug("grant tag→group failed (tag=%d group=%d): HTTP %d %s",
                      tag_id, group_id, r.status_code, r.text[:200])
    except Exception as exc:  # noqa: BLE001
        log.debug("_grant_tag_view_to_group failed: %s", exc)


def resolve_visibility_tag_id(visibility: str) -> Optional[int]:
    """Get the Paperless tag id for the given visibility level.
    Returns None for 'private' (no tag) or when Paperless is
    unreachable. Caches after first lookup."""
    visibility = (visibility or "private").lower()
    tag_name = VISIBILITY_TAG_NAMES.get(visibility)
    if not tag_name:
        return None  # private has no tag

    with _CACHE_LOCK:
        if tag_name in _TAG_ID_CACHE:
            return _TAG_ID_CACHE[tag_name]

    # Cache miss — populate (one round-trip) and return.
    ensure_tags()
    with _CACHE_LOCK:
        return _TAG_ID_CACHE.get(tag_name)


def apply_visibility_to_payload(
    visibility: str, payload_data: Dict[str, Any],
) -> Dict[str, Any]:
    """Mutate a Paperless POST_DOCUMENT data dict to include the
    visibility tag id, if applicable. Returns the same dict for
    chainable calls. Private → no-op (Paperless owner-only kicks in)."""
    tag_id = resolve_visibility_tag_id(visibility)
    if tag_id is None:
        return payload_data
    # Paperless accepts repeated `tags` form fields. We append rather
    # than overwriting in case the caller already attached project tags.
    existing = payload_data.get("tags") or []
    if isinstance(existing, str):
        existing = [existing]
    if tag_id not in existing:
        existing.append(tag_id)
    payload_data["tags"] = existing
    return payload_data


def change_document_visibility(
    paperless_doc_id: int, visibility: str,
) -> Dict[str, Any]:
    """Change the visibility of an existing Paperless document. Adds
    the new visibility tag (if any) and removes the others, so toggling
    private → business doesn't leave both tags attached.

    Uses the admin token because tag changes can require permissions
    beyond what a regular user has on someone else's doc. Caller is
    expected to have done its own auth check first.

    Returns {ok, paperless_doc_id, visibility, tag_ids?, error?}.
    """
    visibility = (visibility or "private").lower()
    if visibility not in VISIBILITY_LEVELS:
        return {"ok": False, "error": f"unknown visibility '{visibility}'"}
    s = _settings()
    if not s.get("api_key"):
        return {"ok": False, "error": "Paperless not configured"}
    base = s["base_url"]
    headers = _admin_headers()

    # Compute the target tag set: remove all visibility tags, add the
    # desired one (private adds none).
    visibility_tag_ids = {
        v: resolve_visibility_tag_id(v) for v in ("business", "shared")
    }
    drop_ids = {tid for tid in visibility_tag_ids.values() if tid is not None}
    add_id = resolve_visibility_tag_id(visibility)

    # Fetch current tags, modify the set, PATCH back.
    try:
        gr = requests.get(
            f"{base}/api/documents/{paperless_doc_id}/", headers=headers, timeout=TIMEOUT_S,
        )
        if not gr.ok:
            return {"ok": False, "error": f"Paperless GET failed: HTTP {gr.status_code}"}
        current_tags = set(gr.json().get("tags") or [])
        new_tags = (current_tags - drop_ids)
        if add_id is not None:
            new_tags.add(add_id)
        pr = requests.patch(
            f"{base}/api/documents/{paperless_doc_id}/", headers=headers,
            json={"tags": sorted(new_tags)}, timeout=TIMEOUT_S,
        )
        if not pr.ok:
            return {"ok": False, "error": f"Paperless PATCH failed: HTTP {pr.status_code}"}
        return {"ok": True, "paperless_doc_id": paperless_doc_id,
                "visibility": visibility, "tag_ids": sorted(new_tags)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def visibility_of(doc_tag_ids: list[int]) -> str:
    """Read-back: derive the visibility level from a doc's tag ids.
    Private if no visibility tag present. If both happen to be attached
    (shouldn't, but defensive) the more-public one wins (shared > business)."""
    shared_id   = resolve_visibility_tag_id("shared")
    business_id = resolve_visibility_tag_id("business")
    if shared_id is not None and shared_id in doc_tag_ids:
        return "shared"
    if business_id is not None and business_id in doc_tag_ids:
        return "business"
    return "private"
