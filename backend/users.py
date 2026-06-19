"""User management endpoints — admin-only CRUD on user_profiles plus
the self-service Account routes (rename, change language)."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field

from .auth_sessions import (
    current_user,
    get_user_by_email,
    hash_password,
    require_admin,
    revoke_all_sessions,
    set_password,
)
from .database import conn_ctx, DEFAULT_DB_PATH, get_conn

router = APIRouter(prefix="/api", tags=["users"])


# ───────────────────────── models ──────────────────────────────────────

class UserCreate(BaseModel):
    email: str
    name: str
    role: str = Field(default="member")
    password: str = Field(..., min_length=8)
    # Phase B.x — default ON now that spaces + provisioning are the
    # supported path. A new family member without paperless+immich
    # accounts can't see Household documents or shared photos even
    # after being added to the space — silently broken UX. Admin can
    # pass an empty list to skip if they're recreating a user whose
    # external accounts already exist with a different password.
    auto_provision: list[str] = Field(
        default_factory=lambda: ["paperless", "immich"],
        description="Subset of ['paperless', 'immich']. Failures are reported but do not abort the Yorik user create. Default both for family/business installs; pass [] to skip.",
    )
    # Phase B — for family workspaces, drop the new user straight into
    # Household so they immediately see shared events/tasks/docs/photos
    # without admin having to add them as a separate step. Skipped for
    # business workspaces (employees join specific spaces explicitly).
    # Pass false to override even on family.
    join_household: Optional[bool] = Field(
        default=None,
        description="None = auto (joins Household if workspace.kind=='family'); true/false to force.",
    )


class ProvisionBody(BaseModel):
    password: str = Field(..., min_length=8,
                          description="The password to set in the upstream service (and to log in there as the user once for token capture).")


class UserPatch(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None
    language: Optional[str] = None
    disabled: Optional[bool] = None


class PasswordReset(BaseModel):
    new_password: str = Field(..., min_length=8)


class SelfProfile(BaseModel):
    name: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    language: Optional[str] = None
    # Onboarding / letterhead fields — used by Compose to render the
    # sender block, by ZUGFeRD for the seller payload, and by the chat
    # agent to localize date/currency/phrasing per the user's country.
    country: Optional[str] = None
    address_street: Optional[str] = None
    address_postcode: Optional[str] = None
    address_city: Optional[str] = None
    phone: Optional[str] = None
    business_name: Optional[str] = None
    tax_id: Optional[str] = None
    iban: Optional[str] = None
    onboarded_at: Optional[str] = None  # accept ISO; usually set via /onboarding/complete
    # Scanned handwritten signature as a data URL (e.g. "data:image/png;base64,…").
    # Used by compose templates above the typed name. Pass null to clear.
    signature_data_url: Optional[str] = None


# ───────────────────────── admin: list / create / patch / delete ───────

def _user_row_to_dict(r) -> dict[str, Any]:
    d = dict(r)
    # Don't ever leak the password hash, even to admin.
    d.pop("password_hash", None)
    d["disabled"] = bool(d.get("disabled"))
    return d


@router.get("/users/assignable")
def list_assignable(user: dict[str, Any] = Depends(current_user)) -> list[dict[str, Any]]:
    """Lightweight list of enabled users any logged-in user can hit —
    used by the task-assignee picker (and future event-invite etc.).
    Only id + name + initial — no email, no role, no password state.
    Disabled users are excluded so you can't accidentally assign them."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name FROM user_profiles WHERE disabled=0 ORDER BY name"
        ).fetchall()
    return [{"id": r["id"], "name": r["name"] or "(no name)"} for r in rows]


@router.get("/users", dependencies=[Depends(require_admin)])
def list_users() -> list[dict[str, Any]]:
    """Admin-only. Returns every user with profile + session-derived
    last-active info (NOT the password hash)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT u.id, u.name, u.email, u.role, u.language, u.disabled, "
            "       u.created_at, u.last_login_at, "
            "       (SELECT COUNT(*) FROM sessions s WHERE s.user_id = u.id) AS active_sessions, "
            "       (u.password_hash IS NOT NULL) AS has_password "
            "FROM user_profiles u ORDER BY u.created_at ASC"
        ).fetchall()
    return [_user_row_to_dict(r) for r in rows]


@router.post("/users", status_code=201, dependencies=[Depends(require_admin)])
def create_user(body: UserCreate) -> dict[str, Any]:
    if get_user_by_email(body.email):
        raise HTTPException(409, "email already in use")
    # Phase B: role floor is 4 (owner/admin/member/restricted). Legacy
    # child/employee/viewer accepted for back-compat then immediately
    # normalised to restricted in user_profiles.
    legal_roles = {"admin", "member", "restricted", "child", "employee", "viewer"}
    if body.role not in legal_roles:
        raise HTTPException(400, f"unknown role: {body.role}")
    normalized_role = "restricted" if body.role in ("child", "employee", "viewer") else body.role
    pw_hash = hash_password(body.password)
    # Phase E user_profiles.id is UUID NOT NULL with no DEFAULT — we
    # have to generate the id client-side. Pre-Phase-E SQLite had a
    # BIGSERIAL default, so the previous "INSERT then lastrowid"
    # path worked. On Postgres it crashed with
    #   NotNullViolation: null value in column "id" of relation "user_profiles"
    # auth_setup already does this dance via GoTrue or local uuid;
    # admin-side create_user was missing the same generation step.
    # datetime('now') is also SQLite-only — current_timestamp works
    # on both backends.
    import uuid as _uuid
    uid = str(_uuid.uuid4())
    with conn_ctx(DEFAULT_DB_PATH) as conn:
        # First insert the auth.users shim row (tenant-mode only; on
        # the host the FK target lives in GoTrue's auth.users which
        # is also satisfied by a local insert when GoTrue isn't in
        # the loop for admin user-create).
        try:
            conn.execute(
                "INSERT INTO auth.users (id, email) VALUES (?, ?) "
                "ON CONFLICT (id) DO NOTHING",
                (uid, body.email),
            )
        except Exception:  # noqa: BLE001
            # Pre-Phase-E SQLite: no auth schema; the user_profiles
            # insert below will succeed without it.
            pass
        conn.execute(
            "INSERT INTO user_profiles (id, name, email, role, password_hash, password_set_at) "
            "VALUES (?, ?, ?, ?, ?, current_timestamp)",
            (uid, body.name, body.email, normalized_role, pw_hash),
        )
        row = conn.execute(
            "SELECT id, name, email, role, language, disabled, created_at "
            "FROM user_profiles WHERE id=?",
            (uid,),
        ).fetchone()
    # Phase B: personal space FIRST — calendars created below resolve
    # their space_id by looking up the owner's personal space, so it
    # has to exist before ensure_calendars_for_user runs. (Workspace
    # must already exist by the time create_user runs — first-run admin
    # is created via auth_setup which seeds it.)
    from . import spaces as _sp
    _sp.ensure_personal_space(uid, body.name)

    from . import calendars as _cal
    _cal.ensure_calendars_for_user(uid, body.name)

    result = _user_row_to_dict(row)
    # Auto-provision external accounts. Failures surfaced but don't
    # abort the Yorik user create — admin can re-run via
    # /users/{id}/provision/{service} after fixing whatever broke.
    result["provisioning"] = {}
    if "paperless" in body.auto_provision:
        try:
            from . import external_users
            external_users.provision_paperless(
                uid, body.name, body.email, body.password,
                is_admin=(normalized_role in ("admin", "platform_admin")),
            )
            result["provisioning"]["paperless"] = {"ok": True}
        except Exception as e:
            result["provisioning"]["paperless"] = {"ok": False, "error": str(e)}
    if "immich" in body.auto_provision:
        try:
            from . import external_users
            external_users.provision_immich(
                uid, body.name, body.email, body.password,
                is_admin=(normalized_role in ("admin", "platform_admin")),
            )
            result["provisioning"]["immich"] = {"ok": True}
        except Exception as e:
            result["provisioning"]["immich"] = {"ok": False, "error": str(e)}

    # Phase B: drop family-workspace users straight into Household so
    # they immediately see shared events / tasks / docs / photos
    # without admin having to walk the spaces UI as a second step.
    # Order matters: provisioning above stored paperless_user_id +
    # immich_user_id; the membership-add hook fires sync_space which
    # only adds users with mapped external accounts. Doing the join
    # AFTER provisioning means the first sync already picks them up.
    if body.join_household is True or (
        body.join_household is None and _workspace_kind() == "family"
    ):
        # Restricted users join Household at READ level — they see
        # everything (shared calendar / tasks / docs / photos) but can't
        # add/edit shared rows. Member/admin join at WRITE. Admin can
        # promote a restricted member to write later via Settings →
        # Spaces if that's wanted (e.g. teenager who can edit chores).
        join_level = "read" if normalized_role == "restricted" else "write"
        space_id = _sp.add_user_to_household(uid, join_level)
        result["joined_household"] = (
            {"space_id": space_id, "level": join_level} if space_id is not None
            else {"error": "no household space (workspace not seeded?)"}
        )

    # Re-sync every space the new user is a member of so that, when
    # provisioning succeeded but the join had already happened above
    # (or any future code path adds the user to a space before/parallel
    # to provisioning), Paperless groups + Immich albums catch up
    # without waiting for the hourly drift tick. Cheap when nothing
    # changes (idempotent at each connector).
    try:
        from . import spaces as _sp
        with conn_ctx(DEFAULT_DB_PATH) as conn:
            for r in conn.execute(
                "SELECT space_id FROM space_members WHERE user_id=?", (uid,)
            ).fetchall():
                _sp.on_space_member_added(int(r["space_id"]), uid, "write")
    except Exception as e:
        import logging as _log
        _log.getLogger("yorik.users").warning(
            "post-provision space resync failed for uid=%s: %s", uid, e,
        )

    return result


def _workspace_kind() -> str:
    """Return the workspace kind; 'family' if no workspace yet (fresh
    install pre-migration shouldn't reach here, but be defensive)."""
    with conn_ctx(DEFAULT_DB_PATH) as conn:
        row = conn.execute("SELECT kind FROM workspaces LIMIT 1").fetchone()
    return (row["kind"] if row else "family") or "family"


# _ensure_personal_space + _add_to_household used to live here; promoted
# to backend.spaces (ensure_personal_space, add_user_to_household) so
# auth_setup can call the same code path. Kept the wrapper above for
# the workspace-kind lookup; the helpers themselves are imported
# directly from spaces.


@router.get("/users/{user_id}/externals", dependencies=[Depends(require_admin)])
def user_external_state(user_id: str) -> dict[str, Any]:
    """Provisioning state for one user across upstream services."""
    from . import external_users
    return external_users.provisioning_state(user_id)


@router.post("/users/{user_id}/provision/{service}", dependencies=[Depends(require_admin)])
def provision_user(user_id: str, service: str, body: ProvisionBody) -> dict[str, Any]:
    """Manually (re-)provision one service for one user. Service must
    be 'paperless' or 'immich'. The body password is what will be set
    in the upstream service AND used to log in there for token capture."""
    if service not in ("paperless", "immich"):
        raise HTTPException(400, "service must be 'paperless' or 'immich'")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT name, email, role FROM user_profiles WHERE id=?", (user_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "user not found")
    if not row["email"]:
        raise HTTPException(400, "user has no email — set one before provisioning external services")
    from . import external_users
    try:
        if service == "paperless":
            out = external_users.provision_paperless(
                user_id, row["name"], row["email"], body.password,
                is_admin=(row["role"] in ("admin", "platform_admin")),
            )
        else:
            out = external_users.provision_immich(
                user_id, row["name"], row["email"], body.password,
                is_admin=(row["role"] in ("admin", "platform_admin")),
            )
    except Exception as e:
        raise HTTPException(502, f"{service} provisioning failed: {e}")
    # Newly-linked external account → re-sync every space this user is in
    # so Paperless groups / Immich albums pick them up immediately.
    try:
        from . import spaces as _sp
        with conn_ctx(DEFAULT_DB_PATH) as conn:
            for r in conn.execute(
                "SELECT space_id, level FROM space_members WHERE user_id=?", (user_id,)
            ).fetchall():
                _sp.on_space_member_added(int(r["space_id"]), user_id, r["level"])
    except Exception as e:
        import logging as _log
        _log.getLogger("yorik.users").warning(
            "post-provision space resync failed for uid=%s service=%s: %s",
            user_id, service, e,
        )
    return {"ok": True, **out}


@router.patch("/users/{user_id}", dependencies=[Depends(require_admin)])
def patch_user(user_id: str, body: UserPatch,
               actor: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    """Admin updates someone's profile. Safety net: admin can't disable
    themselves (otherwise next-render lockout); can't demote themselves
    away from admin if they're the only admin."""
    if user_id == actor["id"]:
        if body.disabled is True:
            raise HTTPException(400, "cannot disable yourself")
        # Phase C T10: platform_admin is the global infra role. Don't let
        # the last active platform_admin demote themselves (would lock
        # the install out of Settings → Users entirely). Workspace admins
        # (role='admin') can demote themselves freely — there's nothing
        # only-they-can-do that demands they stay admin.
        if actor.get("role") == "platform_admin" and body.role and body.role != "platform_admin":
            with get_conn() as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM user_profiles WHERE role='platform_admin' AND disabled=0"
                ).fetchone()[0]
            if count <= 1:
                raise HTTPException(400, "cannot demote the only active platform_admin")

    fields, params = [], []
    if body.name is not None:     fields.append("name=?");     params.append(body.name)
    if body.role is not None:
        if body.role not in ("admin", "member", "child", "employee", "viewer"):
            raise HTTPException(400, f"unknown role: {body.role}")
        fields.append("role=?"); params.append(body.role)
    if body.language is not None: fields.append("language=?"); params.append(body.language)
    if body.disabled is not None: fields.append("disabled=?"); params.append(1 if body.disabled else 0)
    if not fields:
        raise HTTPException(400, "no fields to update")
    params.append(user_id)
    with conn_ctx(DEFAULT_DB_PATH) as conn:
        conn.execute(f"UPDATE user_profiles SET {', '.join(fields)} WHERE id=?", params)
        row = conn.execute(
            "SELECT id, name, email, role, language, disabled, created_at FROM user_profiles WHERE id=?",
            (user_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "user not found")
    # If we just disabled the user, kick their sessions.
    if body.disabled:
        revoke_all_sessions(user_id)
    return _user_row_to_dict(row)


@router.post("/users/{user_id}/reset-password", dependencies=[Depends(require_admin)])
def admin_reset_password(user_id: str, body: PasswordReset) -> dict[str, Any]:
    """Admin overrides a user's password (no current-password check)."""
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM user_profiles WHERE id=?", (user_id,)).fetchone()
    if not row:
        raise HTTPException(404, "user not found")
    set_password(user_id, body.new_password)  # also revokes sessions
    return {"ok": True, "sessions_revoked": True}


@router.delete("/users/{user_id}", dependencies=[Depends(require_admin)])
def delete_user(user_id: str, actor: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    if user_id == actor["id"]:
        raise HTTPException(400, "cannot delete yourself")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, role FROM user_profiles WHERE id=?", (user_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "user not found")
        if row["role"] == "admin":
            others = conn.execute(
                "SELECT COUNT(*) FROM user_profiles WHERE role='admin' AND disabled=0 AND id != ?",
                (user_id,),
            ).fetchone()[0]
            if others == 0:
                raise HTTPException(400, "cannot delete the only active admin")
        conn.execute("DELETE FROM user_profiles WHERE id=?", (user_id,))
        # ON DELETE CASCADE on sessions takes care of the rest.
        conn.commit()
    return {"ok": True}


# ───────────────────────── self-service: profile ───────────────────────

_SELF_FIELDS = (
    "name", "first_name", "last_name", "language",
    "country", "address_street", "address_postcode", "address_city",
    "phone", "business_name", "tax_id", "iban", "onboarded_at",
    "signature_data_url",
)


@router.patch("/profile")
def update_self_profile(body: SelfProfile,
                         user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    """Update the calling user's own profile — name, language, address,
    business info. Anything not in the body is left unchanged.

    Side effect: when an admin sets/changes the country, we also apply
    the matching server-wide locale (timezone + Paperless OCR language)
    via backend.locale.apply_country. Non-admin country changes are
    purely personal — they don't shift the server's locale.
    """
    payload = body.model_dump(exclude_unset=True)
    fields, params = [], []
    for col in _SELF_FIELDS:
        if col in payload:
            fields.append(f"{col}=?")
            params.append(payload[col])
    if not fields:
        raise HTTPException(400, "no fields to update")
    params.append(user["id"])
    with conn_ctx(DEFAULT_DB_PATH) as conn:
        conn.execute(f"UPDATE user_profiles SET {', '.join(fields)} WHERE id=?", params)
        row = conn.execute(
            "SELECT id, name, first_name, last_name, email, role, language, "
            "       country, address_street, address_postcode, address_city, "
            "       phone, business_name, tax_id, iban, onboarded_at, "
            "       signature_data_url "
            "FROM user_profiles WHERE id=?",
            (user["id"],),
        ).fetchone()
    result = _user_row_to_dict(row)

    # Apply server-wide locale derived from country (admin only). Skip
    # on tenants — apply_country writes to the HOST's config.env and
    # restarts the HOST's bundled Paperless, which would clobber shared
    # state across all tenants. Country stays saved on user_profiles
    # for the tenant; only the host-wide side effects are suppressed.
    if user.get("role") == "admin" and payload.get("country"):
        from . import external_users as _eu
        if _eu._is_tenant_mode():
            result["locale_applied"] = {"applied": False, "note": "tenant mode — host locale unchanged"}
        else:
            from . import locale as _locale
            try:
                result["locale_applied"] = _locale.apply_country(payload["country"])
            except Exception as exc:  # noqa: BLE001
                result["locale_applied"] = {"applied": False, "error": str(exc)}

    return result


@router.post("/onboarding/complete")
def complete_onboarding(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    """Mark the calling user as onboarded. Idempotent — the wizard hits
    this after the user finishes the wizard, including 'skip for now'."""
    with conn_ctx(DEFAULT_DB_PATH) as conn:
        conn.execute(
            "UPDATE user_profiles SET onboarded_at=datetime('now') WHERE id=?",
            (user["id"],),
        )
    return {"ok": True}
