"""External-service account provisioning for Yorik users.

When admin creates a Yorik user (or hits the "Provision" button later),
we automatically:
  1. Create a matching account in Paperless (Django REST + Token auth)
  2. Create a matching account in Immich (REST + per-user API key)
  3. Store each service's user_id + per-user token/key in user_profiles

The point is per-user isolation in the BACKEND: skills like find_document
and find_photo use the calling user's token, so Anna's queries never
touch Bob's documents/photos. The iframe-level login (Paperless/Immich
web UI) still requires the user to enter their service password once
per browser session — both apps use Django/Express cookie sessions and
neither supports a magic-link login. We pre-fill the username field
when we can, but that's the limit without a reverse proxy.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path
from typing import Any, Optional

import requests

from .connectors.paperless import _settings as _paperless_admin_settings
from . import credential_store
from .database import get_conn

log = logging.getLogger("yorik.external_users")

TIMEOUT_S = 10


# ─── host-side internal token ─────────────────────────────────────────
# Per-tenant secret between the host Yorik and EACH tenant Yorik.
# scripts/create-tenant.sh writes data/tenants/<name>/internal_token
# (mode 0600) and registers it in the host's tenant_bearer_tokens
# table. The host's _verify_internal_bearer resolves an incoming
# bearer to its tenant_name; every endpoint that takes a `tenant_name`
# in the body enforces that the body's name matches the resolved
# token's name. Without that binding, any tenant could pass any other
# tenant's name in the body and act on their behalf.
#
# The legacy single data/internal_token is no longer written by
# create-tenant.sh; it's kept as a startup artifact ONLY for hosts
# that haven't migrated their tenants yet (manual recreate clears it).
INTERNAL_TOKEN_PATH = Path(__file__).resolve().parent.parent / "data" / "internal_token"


def get_or_create_internal_token() -> str:
    """Read data/internal_token, or generate one if missing.

    Atomic create: O_CREAT|O_EXCL with mode 0600 so there's no race
    window where a colocated process can read a half-written file or
    an empty world-readable file before chmod. Concurrent calls on
    first boot: one wins the EXCL, others fall through to the read
    path on retry. Same mode (0600) means a colocated tenant process
    running as the same OS user can read it, but a different OS user
    (e.g. nginx, a chrooted container) can't.

    The host calls this at startup so a fresh install has a token
    before the first tenant comes up; tenant Yoriks read it via
    _read_internal_token() below (which is the read-only path —
    never call this from a verify path on the host, or token
    rotation by `rm data/internal_token` silently regenerates).
    """
    INTERNAL_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Optimistic read first — happy path is "already there."
    if INTERNAL_TOKEN_PATH.exists():
        try:
            tok = INTERNAL_TOKEN_PATH.read_text().strip()
            if tok:
                return tok
        except OSError:
            pass
    # Atomic create.
    candidate = secrets.token_urlsafe(32)
    try:
        fd = os.open(
            str(INTERNAL_TOKEN_PATH),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        # Lost the race; whoever won has now written it.
        return INTERNAL_TOKEN_PATH.read_text().strip()
    try:
        with os.fdopen(fd, "w") as f:
            f.write(candidate)
    except Exception:
        # Roll back the partial file so the next caller doesn't read
        # an empty token (which would compare-digest True against an
        # empty bearer header — auth bypass).
        try:
            os.unlink(INTERNAL_TOKEN_PATH)
        except OSError:
            pass
        raise
    log.info("internal token generated at %s", INTERNAL_TOKEN_PATH)
    return candidate


def _read_internal_token() -> Optional[str]:
    """Tenant side. Read THIS tenant's per-tenant token from the path
    declared in YORIK_HOST_INTERNAL_TOKEN_FILE (set by create-tenant.sh
    in the tenant's manifest.env). On the host this falls back to the
    legacy single data/internal_token if it exists — exclusively to
    keep workstations running while migration completes; new installs
    should never read the legacy file."""
    p = os.getenv("YORIK_HOST_INTERNAL_TOKEN_FILE") or str(INTERNAL_TOKEN_PATH)
    try:
        return Path(p).read_text().strip() or None
    except OSError:
        return None


def register_tenant_bearer(tenant_name: str, token: str) -> None:
    """Insert a tenant's bearer into the host's tenant_bearer_tokens
    table. ON CONFLICT replaces — re-running create-tenant.sh rotates
    the tenant's token, which is the right behaviour (old token stops
    working immediately). Called from /api/internal/register-tenant-
    bearer by create-tenant.sh after it writes the token file.
    """
    from .database import conn_ctx, DEFAULT_DB_PATH
    with conn_ctx(DEFAULT_DB_PATH) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS tenant_bearer_tokens ("
            "  token       TEXT PRIMARY KEY,"
            "  tenant_name TEXT NOT NULL UNIQUE,"
            "  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()"
            ")"
        )
        # Rotate: drop any existing row for this tenant_name first,
        # then insert the new (token, tenant_name) pair. Avoids a
        # stale (old_token, tenant) row coexisting with the new one
        # and breaking the UNIQUE(tenant_name) on retry.
        conn.execute(
            "DELETE FROM tenant_bearer_tokens WHERE tenant_name = ?",
            (tenant_name,),
        )
        conn.execute(
            "INSERT INTO tenant_bearer_tokens (token, tenant_name) VALUES (?, ?)",
            (token, tenant_name),
        )
        conn.commit()


def unregister_tenant_bearer(tenant_name: str) -> None:
    """Drop a tenant's bearer from the registry. Called from
    drop-tenant.sh via /api/internal/unregister-tenant-bearer."""
    from .database import conn_ctx, DEFAULT_DB_PATH
    with conn_ctx(DEFAULT_DB_PATH) as conn:
        # Don't ensure-table here — if the table doesn't exist yet,
        # there's nothing to delete and any consumer of the table will
        # bootstrap it themselves.
        try:
            conn.execute(
                "DELETE FROM tenant_bearer_tokens WHERE tenant_name = ?",
                (tenant_name,),
            )
            conn.commit()
        except Exception:  # noqa: BLE001
            pass


def resolve_bearer_to_tenant(token: str) -> Optional[str]:
    """Host side. Look up which tenant a bearer belongs to. Returns
    None for unknown tokens. Used by _verify_internal_bearer to
    enforce body-tenant_name == token-tenant_name on every endpoint."""
    if not token:
        return None
    from .database import conn_ctx, DEFAULT_DB_PATH
    try:
        with conn_ctx(DEFAULT_DB_PATH) as conn:
            row = conn.execute(
                "SELECT tenant_name FROM tenant_bearer_tokens WHERE token = ?",
                (token,),
            ).fetchone()
        if row:
            return row["tenant_name"]
    except Exception:  # noqa: BLE001
        # Table doesn't exist yet (fresh install, no tenants) —
        # bearer can't be resolved. Caller falls back to legacy-token
        # path if configured.
        pass
    return None


def _host_internal_url() -> str:
    return os.getenv("YORIK_HOST_INTERNAL_URL", "http://127.0.0.1:8000").rstrip("/")


def _proxy_provision_via_host(
    service: str, *, yorik_user_id: str, name: str, email: str,
    password: str,
) -> dict[str, Any]:
    """Tenant-side delegation. POST to the host Yorik's
    /api/internal/provision, which has the Immich/Paperless admin
    creds; it provisions a non-admin upstream account and returns the
    per-user secrets we need to store in this tenant's own DB.

    The host namespaces the upstream email/username with the tenant
    name (see `_namespace_upstream` host-side) so two tenants can't
    collide and a tenant can never resurrect the host's own admin
    account by replaying its email.
    """
    tenant = os.getenv("YORIK_DB_NAME", "")
    if not tenant.startswith("yorik_tenant_"):
        raise RuntimeError(
            "proxy provisioning needs YORIK_DB_NAME=yorik_tenant_<name>; "
            f"got {tenant!r}",
        )
    tenant_name = tenant[len("yorik_tenant_"):]
    token = _read_internal_token()
    if not token:
        raise RuntimeError(
            "no host internal token reachable — set YORIK_HOST_INTERNAL_TOKEN_FILE "
            "or run the host Yorik first to generate data/internal_token",
        )
    url = f"{_host_internal_url()}/api/internal/provision"
    body = {
        "tenant_name": tenant_name,
        "service": service,
        "yorik_user_id": yorik_user_id,
        "name": name,
        "email": email,
        "password": password,
    }
    r = requests.post(
        url, json=body, timeout=TIMEOUT_S,
        headers={"Authorization": f"Bearer {token}"},
    )
    if not r.ok:
        body_excerpt = r.text[:300]
        raise RuntimeError(
            f"host proxy {service} provisioning failed: HTTP {r.status_code}: {body_excerpt}",
        )
    return r.json()


def _is_tenant_mode() -> bool:
    """True when this Yorik instance is one of a multi-tenant set
    sharing a single Immich + Paperless. In that mode NO tenant user
    — not even their Yorik platform_admin — gets admin in the
    external services; the host (the maintainer's own Yorik) is the
    only admin.

    Two signals, in priority order:
      1. `YORIK_IS_TENANT=1` — explicit flag set by create-tenant.sh
         in the tenant's manifest.env. The unambiguous declaration.
      2. `YORIK_DB_NAME` set AND != 'postgres' — legacy inference
         from the DB name. Kept for tenants created before the
         explicit flag shipped; new manifests carry the flag.

    The footgun the explicit flag closes: an operator typo writing
    `YORIK_DB_NAME=postgres` (the default cluster DB name) on what
    should be a tenant manifest would silently flip the Yorik into
    "host" mode — disabling the invite gate, letting any caller of
    /api/auth/setup claim admin, and letting tenant code try to use
    Immich/Paperless admin keys it doesn't have.

    Phase F-lite isolation contract: Immich's per-user library access
    is what keeps Mom's photos out of Parents' Yorik. The moment a
    tenant gets admin in the shared Immich, that contract breaks —
    they can list/read every other tenant's library via the admin
    API.
    """
    explicit = os.getenv("YORIK_IS_TENANT", "").strip()
    if explicit in ("1", "true", "yes", "on"):
        return True
    name = os.getenv("YORIK_DB_NAME")
    return bool(name) and name != "postgres"


# ─────────────────── Cross-tenant email collision check ───────────────

def host_lookup_user_by_email(service: str, email: str) -> Optional[dict[str, Any]]:
    """Host-side: ask Immich or Paperless whether an account already
    exists for this email/derived-username. Returns the upstream user
    dict if found (so callers can decide what to do), or None.

    Used by the host's /api/internal/provision proxy as a security
    pre-check on behalf of tenants: without it, a tenant could pass
    an email matching the host admin's account and silently trigger
    the "user exists → reset password" branch inside provision_*,
    taking over the host's external admin. Refusing on collision
    keeps each upstream user identity bound to whoever created it
    first, regardless of which tenant later asks.

    Only meaningful on the host side — `service` admin creds come
    from the host's credential_store. Returns None for unknown
    services or when admin creds are missing (caller logs).
    """
    if service == "immich":
        creds = credential_store.get("immich") or {}
        admin_key = creds.get("api_key")
        if not admin_key:
            return None
        base = (creds.get("base_url") or "http://localhost:2283").rstrip("/")
        r = requests.get(
            f"{base}/api/admin/users",
            params={"withDeleted": "true"},
            headers={"x-api-key": admin_key, "Accept": "application/json"},
            timeout=TIMEOUT_S,
        )
        if not r.ok:
            return None
        for u in r.json():
            if (u.get("email") or "").lower() == email.lower():
                return u
        return None
    if service == "paperless":
        s = _paperless_admin_settings()
        if not s.get("api_key"):
            return None
        base = s["base_url"].rstrip("/")
        # Mirror provision_paperless's username derivation so the
        # collision check matches what provision_paperless would try
        # to create.
        local = (email or "").split("@")[0].lower()
        username = local.replace("+", "_")
        username = "".join(c for c in username if c.isalnum() or c in ".-_")[:30]
        if not username:
            return None
        r = requests.get(
            f"{base}/api/users/",
            headers={"Authorization": f"Token {s['api_key']}", "Accept": "application/json"},
            params={"username": username},
            timeout=TIMEOUT_S,
        )
        if not r.ok:
            return None
        for u in (r.json().get("results") or []):
            if u.get("username") == username:
                return u
        return None
    return None


# ───────────────────────── Paperless ───────────────────────────────────

def _is_paperless_password_complexity_error(r) -> bool:
    """Detect Paperless's Django auth validators rejecting a password
    for being too weak. Body shape:
      {"password": ["The password is too similar to the username.",
                    "This password is too common.", ...]}
    All complexity validators surface under the "password" key."""
    if r.status_code != 400:
        return False
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        return False
    return isinstance(body, dict) and isinstance(body.get("password"), list) and len(body["password"]) > 0


def _strong_paperless_password() -> str:
    """36-char URL-safe token — comfortably passes any reasonable
    password validator (length, common-password check, similarity)."""
    return secrets.token_urlsafe(27)  # ~36 chars


def provision_paperless(yorik_user_id: str, name: str, email: str,
                         password: str, *, is_admin: bool = False,
                         _store: bool = True) -> dict[str, Any]:
    """Create a Paperless user (via the admin Token), then log in as
    them to obtain a per-user token. Stores both in user_profiles.
    Returns {paperless_user_id, paperless_token, paperless_username,
    paperless_password, password_fallback_generated}.

    Password handling: we first try the user's chosen Yorik password
    so the user can log into the Paperless web iframe with the same
    credential they use for Yorik. If Paperless's Django validators
    reject it (too short, too common, too similar to username), we
    generate a strong random password instead — the user keeps their
    weak Yorik password, but Paperless gets one that satisfies it.
    The fallback password is returned in the response (set in the
    paperless_password field and also surfaced via the
    password_fallback_generated boolean) so the caller can show it
    to the user. The encrypted copy in credential_store is the
    authoritative one going forward.

    Idempotent: if the email already exists in Paperless, we re-use
    that user. If credential_store already has a saved Paperless
    password for this user, we try that one first (covers re-runs
    after a successful provision).
    """
    if _is_tenant_mode():
        # Tenant mode: this Yorik does not have the Paperless admin
        # token (and shouldn't — host is the only Paperless admin).
        # Delegate to the host's /api/internal/provision endpoint,
        # which provisions a non-admin upstream user with a
        # tenant-namespaced username + email, then store the returned
        # per-user creds in our own DB exactly like the local path.
        if is_admin:
            log.info("paperless: tenant mode — demoting is_admin=True (host is sole admin)")
        result = _proxy_provision_via_host(
            "paperless", yorik_user_id=yorik_user_id, name=name,
            email=email, password=password,
        )
        _store_paperless_creds(
            yorik_user_id, result["paperless_user_id"],
            result["paperless_token"], result.get("paperless_password"),
        )
        return result
    s = _paperless_admin_settings()
    if not s.get("api_key"):
        raise RuntimeError("Paperless admin token not configured")
    base = s["base_url"].rstrip("/")
    admin_headers = {"Authorization": f"Token {s['api_key']}", "Accept": "application/json"}

    # Paperless usernames must match Django's `username` field — lowercase,
    # no spaces, ≤150 chars. Derive from email's local part.
    # Phase F-lite: the host's _namespace_upstream_for_tenant emits
    # emails like `<tenant>+<localpart>@<domain>` so multiple tenants
    # don't collide on the upstream. Paperless usernames don't allow
    # `+` after the alnum filter, so a raw derive would smash the
    # `+` out and give `<tenant><localpart>` — which breaks the
    # delete-on-drop prefix match (it looks for `<tenant>_`). Replace
    # `+` with `_` BEFORE the filter so the username keeps the
    # tenant boundary visible and matches drop-tenant's namespace
    # scan. Same Django constraint satisfied; same upstream user
    # identity.
    local = (email or "").split("@")[0].lower() or _slug(name)
    username = local.replace("+", "_")
    username = "".join(c for c in username if c.isalnum() or c in ".-_")[:30]

    # Find existing user by username.
    existing = None
    r = requests.get(f"{base}/api/users/", headers=admin_headers,
                     params={"username": username}, timeout=TIMEOUT_S)
    if r.ok:
        for u in (r.json().get("results") or []):
            if u.get("username") == username:
                existing = u; break

    # Password candidate order:
    #   1. Saved password from credential_store (previous successful
    #      provision) — keeps re-provisioning idempotent. If we
    #      generated a strong one last time, REUSE it instead of
    #      generating a fresh one and rotating Paperless on every call.
    #   2. The user's Yorik password — covers the "synced password"
    #      case where direct Paperless web-iframe login should work
    #      with the same credential the user uses for Yorik.
    #   3. A freshly generated strong password — only when 1 and 2
    #      both get rejected on complexity.
    saved_pw = _load_paperless_password(yorik_user_id)
    candidates: list[tuple[str, bool]] = []  # (password, is_fallback)
    if saved_pw and saved_pw != password:
        candidates.append((saved_pw, False))
    candidates.append((password, False))

    effective_password: Optional[str] = None
    fallback_generated = False
    last_resp = None

    if existing:
        paperless_uid = existing["id"]
        log.info("paperless: reusing existing user %s (id=%d)", username, paperless_uid)

        # Re-provision must also reconcile the superuser flag — if a user
        # was provisioned pre-fix (is_superuser=False even for Yorik
        # admins) or had their Yorik role flipped to/from admin, the
        # Paperless flag would otherwise drift. Skip the PATCH when the
        # flag already matches so we don't churn on every re-run.
        if bool(existing.get("is_superuser")) != is_admin:
            try:
                requests.patch(
                    f"{base}/api/users/{paperless_uid}/",
                    headers={**admin_headers, "Content-Type": "application/json"},
                    json={"is_superuser": is_admin, "is_staff": is_admin},
                    timeout=TIMEOUT_S,
                )
                log.info("paperless: synced is_superuser=%s for %s", is_admin, username)
            except requests.RequestException as exc:  # noqa: BLE001
                log.warning("paperless: failed to sync is_superuser for %s: %s", username, exc)

        def _patch_password(pwd: str):
            return requests.patch(
                f"{base}/api/users/{paperless_uid}/",
                headers={**admin_headers, "Content-Type": "application/json"},
                json={"password": pwd},
                timeout=TIMEOUT_S,
            )

        for cand, _is_fallback in candidates:
            r = _patch_password(cand)
            last_resp = r
            if r.ok:
                effective_password = cand
                break
            # Non-complexity 4xx/5xx: stop trying alternates (would be
            # the same error every time). Complexity rejection: try next.
            if not _is_paperless_password_complexity_error(r):
                break
        if not effective_password:
            # All candidates failed; generate a strong one.
            log.info("paperless: existing passwords rejected by validators, "
                     "generating a fresh strong one for user %s", username)
            effective_password = _strong_paperless_password()
            fallback_generated = True
            r = _patch_password(effective_password)
            last_resp = r
        if last_resp is None or not last_resp.ok:
            code = last_resp.status_code if last_resp is not None else "?"
            body = last_resp.text[:200] if last_resp is not None else ""
            log.warning("paperless password reset failed: HTTP %s: %s", code, body)
            # Continue anyway — token fetch below may still succeed if
            # the password already matched (idempotent re-run case).
        else:
            log.info("paperless: reset password for existing user %s", username)
    else:
        first, _, last = name.partition(" ")

        def _create_user(pwd: str):
            # Yorik admins become Paperless superusers so they bypass
            # Paperless's per-row permission system — matches Yorik's
            # "admin sees all" model and lets them read consume-folder
            # ingests (which arrive with `owner: None`). Non-admins
            # currently get is_superuser=False and rely on owner/group
            # grants set by skills + workflows; if that turns out to be
            # too restrictive in practice we'll need to attach a default
            # group with view_document permission too.
            payload = {
                "username": username,
                "email": email,
                "password": pwd,
                "first_name": first or name,
                "last_name": last,
                "is_active": True,
                "is_staff": is_admin,
                "is_superuser": is_admin,
                "groups": [],
                "user_permissions": [],
            }
            return requests.post(f"{base}/api/users/", headers=admin_headers,
                                 json=payload, timeout=TIMEOUT_S)

        for cand, _is_fallback in candidates:
            r = _create_user(cand)
            last_resp = r
            if r.ok:
                effective_password = cand
                break
            if not _is_paperless_password_complexity_error(r):
                break
        if not effective_password:
            log.info("paperless: candidate passwords rejected by validators, "
                     "generating a fresh strong one for new user %s", username)
            effective_password = _strong_paperless_password()
            fallback_generated = True
            r = _create_user(effective_password)
            last_resp = r
        if last_resp is None or not last_resp.ok:
            code = last_resp.status_code if last_resp is not None else "?"
            body = last_resp.text[:200] if last_resp is not None else ""
            raise RuntimeError(f"paperless user create failed: HTTP {code}: {body}")
        created = last_resp.json()
        paperless_uid = created["id"]
        log.info("paperless: created user %s (id=%d)", username, paperless_uid)

    # Fetch the user's API token by logging in as them. If the token
    # fetch fails AND we have a saved password from a prior successful
    # provision, try that one too — covers the case where someone
    # changed the Paperless password out-of-band and re-running
    # provision would otherwise wedge.
    def _fetch_token(pwd: str):
        return requests.post(f"{base}/api/token/",
                             data={"username": username, "password": pwd},
                             timeout=TIMEOUT_S)
    r = _fetch_token(effective_password)
    if not r.ok:
        saved = _load_paperless_password(yorik_user_id)
        if saved and saved != effective_password:
            r2 = _fetch_token(saved)
            if r2.ok:
                effective_password = saved
                r = r2
    if not r.ok:
        raise RuntimeError(f"paperless token fetch failed: HTTP {r.status_code}: {r.text[:200]}")
    token = r.json().get("token")
    if not token:
        raise RuntimeError("paperless: token endpoint returned no token")

    if _store:
        _store_paperless_creds(yorik_user_id, paperless_uid, token, effective_password)
    return {"paperless_user_id": paperless_uid, "paperless_token": token,
            "paperless_username": username,
            "paperless_password": effective_password,
            "password_fallback_generated": fallback_generated}


def _store_paperless_creds(yorik_user_id: str, paperless_uid: int, token: str,
                            password: Optional[str] = None) -> None:
    # Per-user token + password go through credential_store (Fernet at
    # rest). Storing the password lets re-provisioning recover when the
    # token gets revoked / rotated. The paperless_user_id stays in
    # user_profiles — it's not sensitive, and the UI needs it for
    # "show me which Paperless user this maps to" diagnostics in
    # Settings → Connectors.
    with get_conn() as conn:
        conn.execute(
            "UPDATE user_profiles SET paperless_user_id=?, paperless_token=NULL WHERE id=?",
            (paperless_uid, yorik_user_id),
        )
        conn.commit()
    payload: dict[str, Any] = {"token": token}
    if password:
        payload["password"] = password
    credential_store.put(f"paperless_user_{yorik_user_id}", payload)


def _load_paperless_password(yorik_user_id: str) -> Optional[str]:
    """Return the saved Paperless password for this Yorik user, or
    None if no entry exists. Used by re-provision flows so we don't
    forget what we set last time."""
    try:
        creds = credential_store.get(f"paperless_user_{yorik_user_id}")
    except Exception:  # noqa: BLE001
        return None
    if not creds:
        return None
    pw = creds.get("password")
    return pw if isinstance(pw, str) and pw else None


# ───────────────────────── Immich ──────────────────────────────────────

def provision_immich(yorik_user_id: str, name: str, email: str,
                      password: str, *, is_admin: bool = False,
                      _store: bool = True) -> dict[str, Any]:
    """Create an Immich user (via admin API key), log in as them to
    grab a session token, then create a per-user API key. Returns
    {immich_user_id, immich_api_key}.

    Idempotent: re-uses an existing user if the email already exists.

    Yorik admins are provisioned as Immich admins (`isAdmin: true`),
    mirroring the Paperless superuser flow — so a Yorik admin who
    logs into the Immich iframe with their own credentials gets the
    admin UI instead of having to remember the separate
    `admin@yorik.local` bootstrap account. Verified against the
    server's UserAdminCreate/UpdateDto, both of which expose
    `isAdmin?: boolean`.
    """
    if _is_tenant_mode():
        if is_admin:
            log.info("immich: tenant mode — demoting is_admin=True (host is sole admin)")
        result = _proxy_provision_via_host(
            "immich", yorik_user_id=yorik_user_id, name=name,
            email=email, password=password,
        )
        _store_immich_creds(
            yorik_user_id, result["immich_user_id"], result["immich_api_key"],
        )
        return result
    creds = credential_store.get("immich") or {}
    admin_key = creds.get("api_key")
    if not admin_key:
        raise RuntimeError("Immich admin API key not configured")
    base = (creds.get("base_url") or "http://localhost:2283").rstrip("/")
    admin_headers = {"x-api-key": admin_key, "Accept": "application/json"}

    # Check existing — include soft-deleted users (Immich's DELETE only
    # marks deletedAt; the row stays with a UNIQUE constraint on email,
    # so a "fresh" create after a deletion fails the constraint. Find
    # the soft-deleted user + restore.
    immich_uid: Optional[str] = None
    was_soft_deleted = False
    existing_is_admin: Optional[bool] = None
    r = requests.get(
        f"{base}/api/admin/users",
        params={"withDeleted": "true"},
        headers=admin_headers, timeout=TIMEOUT_S,
    )
    if r.ok:
        for u in r.json():
            if (u.get("email") or "").lower() == email.lower():
                immich_uid = u.get("id")
                existing_is_admin = bool(u.get("isAdmin"))
                if u.get("deletedAt"):
                    was_soft_deleted = True
                    log.info("immich: restoring soft-deleted user %s (id=%s)",
                             email, immich_uid[:8])
                else:
                    log.info("immich: reusing existing user %s (id=%s)", email, immich_uid[:8])
                break

    if was_soft_deleted:
        r = requests.post(
            f"{base}/api/admin/users/{immich_uid}/restore",
            headers=admin_headers, timeout=TIMEOUT_S,
        )
        if not r.ok:
            log.warning("immich restore failed: HTTP %d: %s",
                        r.status_code, r.text[:200])
            # Fall through to the password-reset path; if that fails too
            # the login below will surface a clearer error.

    if immich_uid:
        # Reset password to match the one Yorik just set — mirrors the
        # Paperless flow above. Without this the subsequent
        # /api/auth/login fails when re-provisioning over an existing
        # user whose password drifted (e.g. failed previous attempt).
        # Same call also reconciles `isAdmin` if the Yorik role flipped
        # since the last provision, OR if this user pre-dates the
        # admin-mirroring fix and is stuck non-admin.
        update_body: Dict[str, Any] = {"password": password}
        if existing_is_admin is not None and existing_is_admin != is_admin:
            update_body["isAdmin"] = is_admin
            log.info("immich: syncing isAdmin=%s for %s", is_admin, email)
        r = requests.put(
            f"{base}/api/admin/users/{immich_uid}",
            headers={**admin_headers, "Content-Type": "application/json"},
            json=update_body,
            timeout=TIMEOUT_S,
        )
        if not r.ok:
            log.warning("immich password/isAdmin update failed: HTTP %d: %s",
                        r.status_code, r.text[:200])

    if not immich_uid:
        r = requests.post(
            f"{base}/api/admin/users",
            headers={**admin_headers, "Content-Type": "application/json"},
            json={"email": email, "name": name, "password": password,
                  "notify": False, "shouldChangePassword": False,
                  "isAdmin": is_admin},
            timeout=TIMEOUT_S,
        )
        if not r.ok:
            raise RuntimeError(f"immich user create failed: HTTP {r.status_code}: {r.text[:300]}")
        immich_uid = r.json().get("id")
        log.info("immich: created user %s (id=%s, isAdmin=%s)",
                 email, immich_uid[:8], is_admin)

    # Log in as them to obtain a session access token.
    r = requests.post(
        f"{base}/api/auth/login",
        headers={"Content-Type": "application/json"},
        json={"email": email, "password": password},
        timeout=TIMEOUT_S,
    )
    if not r.ok:
        raise RuntimeError(f"immich login failed: HTTP {r.status_code}: {r.text[:200]}")
    access_token = r.json().get("accessToken")
    if not access_token:
        raise RuntimeError("immich login returned no accessToken")

    # Create a per-user API key. Immich rotates session tokens but API
    # keys are stable — this is what Yorik stores long-term.
    r = requests.post(
        f"{base}/api/api-keys",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json={"name": "Yorik integration", "permissions": ["all"]},
        timeout=TIMEOUT_S,
    )
    if not r.ok:
        raise RuntimeError(f"immich api-key create failed: HTTP {r.status_code}: {r.text[:200]}")
    api_key = r.json().get("secret")
    if not api_key:
        raise RuntimeError("immich api-key endpoint returned no secret")

    if _store:
        _store_immich_creds(yorik_user_id, immich_uid, api_key)
    return {"immich_user_id": immich_uid, "immich_api_key": api_key}


def _store_immich_creds(yorik_user_id: str, immich_uid: str, api_key: str) -> None:
    # Same encryption story as paperless above — api_key in
    # credential_store, immich_user_id stays in user_profiles for
    # non-sensitive ops/diagnostics.
    with get_conn() as conn:
        conn.execute(
            "UPDATE user_profiles SET immich_user_id=?, immich_api_key=NULL WHERE id=?",
            (immich_uid, yorik_user_id),
        )
        conn.commit()
    credential_store.put(f"immich_user_{yorik_user_id}", {"api_key": api_key})


# ───────────────────────── tenant teardown ─────────────────────────────

def delete_tenant_upstream_users(tenant_name: str) -> dict[str, Any]:
    """Soft-delete every upstream Immich + Paperless user whose
    namespace prefix matches this tenant. Idempotent — re-runs after a
    partial failure clean up whatever's still there.

    Called by the host's /api/internal/tenant/drop endpoint when the
    operator runs `scripts/drop-tenant.sh <name>`. We rely on the
    namespace convention from _namespace_upstream_for_tenant (Immich
    email = `<tenant>+<localpart>@<domain>`; Paperless username =
    `<tenant>_<localpart>`) so we can find a tenant's users without
    walking the tenant's own DB (the DB is about to be dropped
    anyway, and if the previous drop crashed before this step the DB
    might already be gone).

    Returns counts + per-failure error list so the caller can warn
    the operator about orphans without aborting the drop — a missing
    upstream user is "soft" damage; abandoned tenant Postgres data is
    "hard" damage by comparison.
    """
    out: dict[str, Any] = {
        "immich":    {"deleted": 0, "skipped": 0, "errors": []},
        "paperless": {"deleted": 0, "skipped": 0, "errors": []},
    }

    # ── Immich ──
    immich_creds = credential_store.get("immich") or {}
    admin_key = immich_creds.get("api_key")
    base = (immich_creds.get("base_url") or "http://localhost:2283").rstrip("/")
    if not admin_key:
        out["immich"]["skipped"] = -1
        out["immich"]["errors"].append("admin API key not configured")
    else:
        try:
            r = requests.get(
                f"{base}/api/admin/users",
                params={"withDeleted": "false"},
                headers={"x-api-key": admin_key, "Accept": "application/json"},
                timeout=TIMEOUT_S,
            )
            r.raise_for_status()
            prefix = f"{tenant_name}+"
            for u in r.json():
                email = (u.get("email") or "").lower()
                if not email.startswith(prefix.lower()):
                    continue
                uid = u.get("id")
                try:
                    rd = requests.delete(
                        f"{base}/api/admin/users/{uid}",
                        headers={"x-api-key": admin_key, "Accept": "application/json",
                                 "Content-Type": "application/json"},
                        # Immich's DELETE accepts a body — `force: false`
                        # means "soft delete now, queue purge for later".
                        json={"force": False},
                        timeout=TIMEOUT_S,
                    )
                    if rd.ok:
                        out["immich"]["deleted"] += 1
                        log.info("immich: soft-deleted tenant user %s (id=%s)", email, uid[:8])
                    else:
                        out["immich"]["errors"].append(
                            f"{email}: HTTP {rd.status_code} {rd.text[:120]}"
                        )
                except requests.RequestException as exc:  # noqa: BLE001
                    out["immich"]["errors"].append(f"{email}: {exc}")
        except (requests.RequestException, ValueError) as exc:  # noqa: BLE001
            out["immich"]["errors"].append(f"list users failed: {exc}")

    # ── Paperless ──
    s = _paperless_admin_settings()
    paperless_key = s.get("api_key")
    if not paperless_key:
        out["paperless"]["skipped"] = -1
        out["paperless"]["errors"].append("admin token not configured")
    else:
        pbase = s["base_url"].rstrip("/")
        prefix = f"{tenant_name}_"
        try:
            r = requests.get(
                f"{pbase}/api/users/",
                headers={"Authorization": f"Token {paperless_key}",
                         "Accept": "application/json"},
                params={"page_size": 200},
                timeout=TIMEOUT_S,
            )
            r.raise_for_status()
            for u in (r.json().get("results") or []):
                username = (u.get("username") or "").lower()
                if not username.startswith(prefix.lower()):
                    continue
                try:
                    rd = requests.delete(
                        f"{pbase}/api/users/{u['id']}/",
                        headers={"Authorization": f"Token {paperless_key}",
                                 "Accept": "application/json"},
                        timeout=TIMEOUT_S,
                    )
                    if rd.ok or rd.status_code == 204:
                        out["paperless"]["deleted"] += 1
                        log.info("paperless: deleted tenant user %s (id=%d)",
                                 username, u["id"])
                    else:
                        out["paperless"]["errors"].append(
                            f"{username}: HTTP {rd.status_code} {rd.text[:120]}"
                        )
                except requests.RequestException as exc:  # noqa: BLE001
                    out["paperless"]["errors"].append(f"{username}: {exc}")
        except (requests.RequestException, ValueError) as exc:  # noqa: BLE001
            out["paperless"]["errors"].append(f"list users failed: {exc}")

    return out


# ───────────────────────── lookups for per-user calls ──────────────────

def get_user_paperless_creds(yorik_user_id: str) -> Optional[dict[str, Any]]:
    """Returns {base_url, api_key} for the given user, or None if they
    haven't been provisioned. Skills use this in place of the global
    admin token so per-user data isolation actually happens. Token
    lives in credential_store (Fernet-encrypted at rest) keyed by
    paperless_user_<yorik_user_id>; the legacy user_profiles column
    is read as a fallback for pre-migration installs."""
    s = _paperless_admin_settings()
    base = s.get("base_url")
    if not base:
        return None
    stored = credential_store.get(f"paperless_user_{yorik_user_id}") or {}
    token = stored.get("token")
    if not token:
        # Fallback: read legacy plaintext column. Pre-migration rows
        # have token here; the migration ALSO copies + nulls so this
        # branch only fires when the migration hasn't run yet.
        with get_conn() as conn:
            row = conn.execute(
                "SELECT paperless_token FROM user_profiles WHERE id=?",
                (yorik_user_id,),
            ).fetchone()
        if not row or not row["paperless_token"]:
            return None
        token = row["paperless_token"]
    return {"base_url": base.rstrip("/"), "api_key": token}


def get_user_immich_creds(yorik_user_id: str) -> Optional[dict[str, Any]]:
    """Same encryption-then-legacy-fallback pattern as the paperless
    helper above. See its docstring for the rationale."""
    creds = credential_store.get("immich") or {}
    base = creds.get("base_url") or "http://localhost:2283"
    stored = credential_store.get(f"immich_user_{yorik_user_id}") or {}
    api_key = stored.get("api_key")
    if not api_key:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT immich_api_key FROM user_profiles WHERE id=?",
                (yorik_user_id,),
            ).fetchone()
        if not row or not row["immich_api_key"]:
            return None
        api_key = row["immich_api_key"]
    return {"base_url": base.rstrip("/"), "api_key": api_key}


# ───────────────────────── status lookup ───────────────────────────────

def provisioning_state(yorik_user_id: str) -> dict[str, Any]:
    """Returns {paperless: {linked, user_id?}, immich: {linked, user_id?}}.
    Used by the Users tab to render per-user provisioning status."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT paperless_user_id, paperless_token, immich_user_id, immich_api_key "
            "FROM user_profiles WHERE id=?",
            (yorik_user_id,),
        ).fetchone()
    if not row:
        return {"paperless": {"linked": False}, "immich": {"linked": False}}
    return {
        "paperless": {
            "linked": bool(row["paperless_token"]),
            "user_id": row["paperless_user_id"],
        },
        "immich": {
            "linked": bool(row["immich_api_key"]),
            "user_id": (row["immich_user_id"] or "")[:8] + "…" if row["immich_user_id"] else None,
        },
    }


def _slug(s: str) -> str:
    return "".join(c for c in s.lower() if c.isalnum() or c == "-")[:20] or "user"


# ───────────────────────── opportunistic re-provisioning ───────────────

def ensure_provisioned(
    *, user_id: str, name: str, email: str, password: str, role: str,
) -> dict[str, Any]:
    """Provision any service the user is missing (Paperless / Immich).

    Cheap fast-path when both are already linked (one SQL roundtrip).
    Otherwise per-missing-service: skips silently if the service's
    admin credentials aren't configured (single-app Yorik install
    that doesn't use that bundled app); attempts provisioning with
    the user's just-validated password.

    Failures are LOGGED, not raised — provisioning is best-effort and
    every other call site treats the absence of a per-user token as
    "service not available for this user" and falls back gracefully.

    Returns:
        {
          "paperless": {"linked": bool, "action": "skip"|"ok"|"error",
                        "error"?: str},
          "immich":    {...},
        }
    Used by /api/auth/login so a user whose Yorik account pre-dates
    Paperless/Immich being configured gets provisioned the next time
    they log in — no admin-side dashboard click needed.
    """
    out: dict[str, Any] = {
        "paperless": {"linked": False, "action": "skip"},
        "immich":    {"linked": False, "action": "skip"},
    }

    # One SQL roundtrip — read the current state of both columns.
    with get_conn() as conn:
        row = conn.execute(
            "SELECT paperless_user_id, paperless_token, "
            "       immich_user_id, immich_api_key "
            "FROM user_profiles WHERE id=?",
            (user_id,),
        ).fetchone()
    if not row:
        return out

    is_admin = (role or "").lower() in ("admin", "platform_admin")

    # ── Paperless ──
    if row["paperless_token"]:
        out["paperless"] = {"linked": True, "action": "skip"}
    else:
        try:
            # If the admin-token isn't configured, _settings() logs a
            # warning and provision_paperless raises 'admin token not
            # configured'. Treat that as "this install doesn't have
            # Paperless yet" — don't spam the user log on every login.
            s = _paperless_admin_settings()
            if not s.get("api_key"):
                out["paperless"] = {
                    "linked": False, "action": "skip",
                    "reason": "no admin token configured",
                }
            else:
                provision_paperless(user_id, name, email, password,
                                    is_admin=is_admin)
                out["paperless"] = {"linked": True, "action": "ok"}
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "ensure_provisioned: paperless failed for user=%s: %s",
                user_id, exc,
            )
            out["paperless"] = {
                "linked": False, "action": "error", "error": str(exc),
            }

    # ── Immich ──
    if row["immich_api_key"]:
        out["immich"] = {"linked": True, "action": "skip"}
    else:
        try:
            creds = credential_store.get("immich") or {}
            if not creds.get("api_key"):
                out["immich"] = {
                    "linked": False, "action": "skip",
                    "reason": "no admin api key configured",
                }
            else:
                provision_immich(user_id, name, email, password,
                                 is_admin=is_admin)
                out["immich"] = {"linked": True, "action": "ok"}
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "ensure_provisioned: immich failed for user=%s: %s",
                user_id, exc,
            )
            out["immich"] = {
                "linked": False, "action": "error", "error": str(exc),
            }

    return out
