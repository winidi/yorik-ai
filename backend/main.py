"""HomeOS FastAPI app — serves the dashboard and the /api/* endpoints."""

from __future__ import annotations

# Logging setup must run BEFORE any module-level `getLogger()` call,
# so import + init it as the very first thing in this file. Subsequent
# imports inherit the JSON handlers + secrets filter + SQLite error
# table mirror.
from . import logging_setup as _logging_setup
_logging_setup.setup_logging()

import asyncio
import json
import logging
import os
import re
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Final, List, Optional

import requests
from fastapi import BackgroundTasks, Body, Depends, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .auth import (
    apply_filter,
    normalize_role,
    require_role,
    require_write,
)
from .database import DEFAULT_DB_PATH, conn_ctx, init_db, seed
from . import ask as vanna_agent
# Aliased to `vanna_agent` so we don't have to touch the ~30 call sites
# below — the module was named that until the May 2026 rename.
# NOTE: do NOT `from .ask import LLM_BASE_URL, LLM_MODEL` — that binds
# the names at import time, so /api/llm/config PATCH would update the
# module attribute but this file's locals would stay stale. Always read
# via `vanna_agent.LLM_BASE_URL` / `vanna_agent.LLM_MODEL`.
from . import voice_id
from .wlan_trust import is_trusted_lan_request, trusted_cidrs_for_debug
from . import tts as tts_mod
from . import app_loader
from . import apps as apps_mod
from . import connectors
from . import credential_store
from . import documents as documents_mod
from . import n8n_client
from .ui_tools import LAYOUT_CATALOGUE

ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = ROOT / "frontend"
DB_PATH = os.getenv("HOMEOS_DB_PATH", DEFAULT_DB_PATH)

app = FastAPI(title="HomeOS", version="0.1.0")

# CORS — locked down. We DO serve everything same-origin (the React shell
# lives at /r/* on the same FastAPI host), so cross-origin access isn't
# part of the normal flow. Wildcard `allow_origins=["*"]` is a footgun
# the moment we ever flip `allow_credentials=True`; lock to localhost +
# any Tailscale magic-DNS hostname via env override.
_default_cors_origins = [
    "http://localhost",
    "http://localhost:8000",
    "http://127.0.0.1",
    "http://127.0.0.1:8000",
    "http://localhost:5173",   # Vite dev server (when frontend-react is npm run dev)
    "http://127.0.0.1:5173",
]
_env_cors = [o.strip() for o in os.getenv("YORIK_CORS_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_env_cors or _default_cors_origins,
    # Use regex for Tailscale-served hosts (foo.tail-scale.ts.net + .local).
    allow_origin_regex=r"https://[a-zA-Z0-9-]+\.(ts\.net|tail[a-z0-9-]+\.ts\.net|local)(:[0-9]+)?",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# WhatsApp integration — Baileys bridge proxy + draft generation.
# Lives in its own router so the wa_ prefix is clean and the module can
# be enabled/disabled by env without touching this file.
from . import whatsapp as _wa
app.include_router(_wa.router)

# User management (multi-user wave 2) — admin CRUD + self-service profile.
from . import users as _users
app.include_router(_users.router)

# Spaces / workspace ACL management (Phase B.5).
from . import space_routes as _space_routes
app.include_router(_space_routes.router)

# Email integration — multi-account IMAP fetch + SMTP send.
from . import email_routes as _email_routes
app.include_router(_email_routes.router)

# Universal cross-channel search — fan-out over email + WA + Paperless
# + Immich + calendar. One endpoint, ⌘K palette in the React shell.
from . import search_routes as _search_routes
app.include_router(_search_routes.router)

# Unified person view — resolve email/phone/jid to one human and
# return their cross-channel context (recent emails, WA, events, docs).
from . import people_routes as _people_routes
app.include_router(_people_routes.router)

# Briefing templates — JSON manifests under briefings/, dispatched via
# the skills registry. List/get/run via /api/briefings.
from . import briefings as _briefings_mod
from . import briefing_routes as _briefing_routes
_briefings_mod.load_all()
app.include_router(_briefing_routes.router)

# Suggestion engine — plugin-first architecture. bootstrap() registers
# core retrievers / suggestion types / triggers via side-effect imports
# so the registries are populated before the first analyse_message.
from . import suggestions as _suggestions_pkg  # noqa: F401
from .suggestions import bootstrap as _suggestions_bootstrap
_suggestions_bootstrap.bootstrap()
from . import suggestion_routes as _suggestion_routes
app.include_router(_suggestion_routes.router)

# Backup — age-encrypted snapshots, configurable target, daily schedule.
from . import backup_routes as _backup_routes
app.include_router(_backup_routes.router)

# In-app notifications (bell icon). Created by various features
# (task assignments today, more later).
from . import notification_routes as _notif_routes
app.include_router(_notif_routes.router)

# Dashboard digest — one bundled endpoint that aggregates today's
# events + bills + tasks + unread emails for the home-screen morning
# card. Pure-SQL, no LLM.
from . import dashboard_routes as _dashboard_routes
app.include_router(_dashboard_routes.router)

# Demo data — one-shot seed of realistic example events/tasks/bills so
# fresh installs can poke around without waiting for real IMAP traffic.
# Endpoints under /api/demo, opt-in from the home screen.
from . import demo_routes as _demo_routes
app.include_router(_demo_routes.router)

# Onboarding — first-run wizard state. Per-user, stored as a key in
# app_settings so demoing the box to a family member triggers the tour
# without affecting the maintainer's logged-in state.
from . import onboarding_routes as _onboarding_routes
app.include_router(_onboarding_routes.router)

# Paperless reverse-proxy — gives the Documents app an "Open in Paperless"
# button that drops the user inside Paperless already authenticated as
# their own Paperless account (via Remote-User SSO). See the module
# docstring for the full flow.
from . import paperless_proxy as _paperless_proxy
app.include_router(_paperless_proxy.router)

# n8n reverse-proxy — auto-signs the user into n8n's editor by
# disabling n8n's own user-management and trusting the Yorik session
# cookie at the proxy layer. n8n is bound to 127.0.0.1 so /n8n/* is
# the only way in.
from . import n8n_proxy as _n8n_proxy
app.include_router(_n8n_proxy.router)

# Skills registry — expose the loaded manifests + a dispatch endpoint.
# The dispatch endpoint is primarily for the frontend Settings panel
# and for the Vanna agent's `use_skill` tool (wired separately).
from . import skills as _skills_mod
from .skills import SkillError, get_registry


@app.get("/api/skills", tags=["skills"])
def list_skills(role: str = "admin"):
    """Manifest list of every loaded skill, filtered to those the
    requesting role is permitted to call. Drives the Settings →
    Skills panel + the agent's skill-picker prompt.

    Each entry is augmented with two Settings-UI-only fields:
      ui_category — naming-convention category derived in the registry
                    (Calendar / Tasks / Bills / Contacts / Documents /
                    Photos / Email / WhatsApp / Compose / System).
                    Not used by the LLM — the {skill_index} block in
                    the system prompt continues to come from
                    Registry.index() which sorts by the legacy
                    (empty) `category` field.
      disabled    — true when admin has toggled this skill off via the
                    accordion. Disabled skills are filtered out of the
                    LLM's view (index + list_skills + skill_view) and
                    refused at invoke time.
    """
    from .skills.registry import derive_ui_category, _get_disabled_skills
    disabled = _get_disabled_skills()
    out = []
    for s in get_registry().all():
        if s.permissions and role not in s.permissions and "*" not in s.permissions:
            continue
        m = s.to_manifest()
        m["ui_category"] = derive_ui_category(s.name)
        m["disabled"]    = s.name in disabled
        out.append(m)
    return out


@app.get("/api/skills/stats", tags=["skills"])
def get_skill_stats():
    """Per-skill invocation counts + recent success rate from
    `skill_invocations`. Surfaced in Settings → Skills. No auth
    dependency to match the rest of /api/skills/* — counts are
    aggregate-only, no per-user PII.

    Column is `created_at` (default datetime('now')) — the earlier
    query referenced a non-existent `ts` column and the bare except
    silently returned [], which is why the UI had been showing
    'never called' for every skill since the table existed."""
    with conn_ctx(DB_PATH) as conn:
        try:
            rows = conn.execute("""
                SELECT skill_id AS name,
                       COUNT(*) AS total,
                       SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS successes,
                       SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS failures,
                       MAX(created_at) AS last_used
                  FROM skill_invocations
                 WHERE created_at > datetime('now', '-90 days')
              GROUP BY skill_id
            """).fetchall()
        except Exception as exc:  # noqa: BLE001 — surface so the next
            # bug like the ts/created_at typo doesn't sit undiagnosed
            # for months. Still degrade to empty so the UI loads.
            import logging as _lg
            _lg.getLogger("yorik.skills").warning(
                "get_skill_stats query failed: %s", exc,
            )
            rows = []
    return {r["name"]: {
        "total":      r["total"] or 0,
        "successes":  r["successes"] or 0,
        "failures":   r["failures"] or 0,
        "last_used":  r["last_used"],
    } for r in rows}


@app.get("/api/skills/{name}", tags=["skills"])
def get_skill(name: str):
    """Full manifest + markdown body for one skill. Body is the
    procedural instructions the LLM consumes when invoking."""
    s = get_registry().get(name)
    if not s:
        raise HTTPException(404, f"unknown skill: {name}")
    return {**s.to_manifest(), "body": s.body}


# Admin PATCH endpoints to enable/disable skills and whole categories
# live BELOW the _auth import — placed there so they can use
# _auth.require_admin cleanly without a forward reference.


# ── Authentication (multi-user wave) ──────────────────────────────────
# Lives in main.py because it touches Request/Response cookies, but the
# heavy lifting is in backend/auth_sessions.py.
from . import auth_sessions as _auth

class _LoginBody(BaseModel):
    email: str
    password: str

class _SetupBody(BaseModel):
    email: str
    password: str
    name: Optional[str] = None
    # Phase F-lite: an invite token issued by the host (POST /api/tenants
    # on the maintainer's Yorik). Required in tenant mode unless
    # YORIK_ALLOW_UNATTESTED_SETUP=1 is set in the env (dev/test only).
    # The tenant Yorik validates + consumes this against the host's
    # /api/internal/invites/* endpoints; without it, anyone who reached
    # the tenant URL could claim admin first.
    invite_token: Optional[str] = None

class _PasswordChangeBody(BaseModel):
    current_password: Optional[str] = None  # required for self-change; admin can omit
    new_password: str


# ── Admin: enable/disable individual skills + whole categories ────────
# Drives Settings → Skills accordion (per-skill + per-category toggles).
# Both endpoints persist to app_settings.disabled_skills via the
# registry helpers; disabled skills disappear from the LLM's next-turn
# view (skill_index + list_skills + skill_view) and are refused at
# invoke time. Admin only.

class _SkillToggleBody(BaseModel):
    enabled: bool


@app.patch("/api/skills/{name}", tags=["skills"])
def patch_skill_enabled(
    name: str, body: _SkillToggleBody,
    _user: Dict[str, Any] = Depends(_auth.require_admin),
) -> Dict[str, Any]:
    """Toggle one skill on/off. 404 if the skill doesn't exist (so a
    typo in the URL doesn't silently create a phantom entry in the
    disabled set)."""
    from .skills import get_registry
    from .skills.registry import _get_disabled_skills, _set_disabled_skills
    if get_registry().get(name) is None:
        raise HTTPException(404, f"unknown skill: {name}")
    disabled = _get_disabled_skills()
    if body.enabled:
        disabled.discard(name)
    else:
        disabled.add(name)
    _set_disabled_skills(disabled)
    return {"name": name, "enabled": body.enabled}


@app.patch("/api/skills/categories/{category}", tags=["skills"])
def patch_category_enabled(
    category: str, body: _SkillToggleBody,
    _user: Dict[str, Any] = Depends(_auth.require_admin),
) -> Dict[str, Any]:
    """Toggle every skill in a category. Category match is exact on the
    UI-derived label ('Calendar', 'Contacts', …). Returns the list of
    skills that flipped state — empty list means the category was
    already in the requested state."""
    from .skills import get_registry
    from .skills.registry import (
        derive_ui_category, _get_disabled_skills, _set_disabled_skills,
    )
    members = [
        s.name for s in get_registry().all()
        if derive_ui_category(s.name) == category
    ]
    if not members:
        raise HTTPException(404, f"no skills in category {category!r}")
    disabled = _get_disabled_skills()
    flipped: list[str] = []
    if body.enabled:
        for name in members:
            if name in disabled:
                disabled.discard(name)
                flipped.append(name)
    else:
        for name in members:
            if name not in disabled:
                disabled.add(name)
                flipped.append(name)
    _set_disabled_skills(disabled)
    return {"category": category, "enabled": body.enabled,
            "flipped": flipped, "members": members}


def _provision_supabase_auth_user(
    *, email: str, password: str, name: str, yorik_role: str,
) -> str:
    """Create (or reuse) a Supabase Auth (GoTrue) user. Returns the UUID
    that will become user_profiles.id.

    Reads SERVICE_ROLE_KEY + ANON_KEY + Kong URL from
    infra/supabase/docker/.env. Raises on any unexpected failure;
    the caller decides whether to swallow or surface.
    """
    import requests as _rq
    from pathlib import Path as _P

    env_path = _P(__file__).resolve().parent.parent / "infra/supabase/docker/.env"
    if not env_path.exists():
        raise RuntimeError(f"GoTrue config missing at {env_path}")
    srk = ""
    kong_port = "8400"
    for line in env_path.read_text().splitlines():
        if line.startswith("SERVICE_ROLE_KEY="):
            srk = line.split("=", 1)[1].strip()
        elif line.startswith("KONG_HTTP_PORT="):
            kong_port = line.split("=", 1)[1].strip()
    if not srk:
        raise RuntimeError("SERVICE_ROLE_KEY not in supabase .env")

    base = f"http://localhost:{kong_port}/auth/v1"
    headers = {
        "apikey": srk,
        "Authorization": f"Bearer {srk}",
        "Content-Type": "application/json",
    }
    payload = {
        "email": email,
        "password": password,
        "email_confirm": True,
        "user_metadata": {"name": name, "yorik_role": yorik_role},
    }
    r = _rq.post(f"{base}/admin/users", json=payload, headers=headers, timeout=10)
    if r.status_code in (200, 201):
        return r.json()["id"]
    # Email already taken — recover the id.
    if r.status_code in (400, 409, 422):
        r2 = _rq.get(f"{base}/admin/users", params={"email": email},
                     headers=headers, timeout=10)
        r2.raise_for_status()
        users = (r2.json() or {}).get("users") or []
        if users:
            return users[0]["id"]
    raise RuntimeError(f"GoTrue admin/users -> HTTP {r.status_code}: {r.text[:200]}")


@app.get("/api/auth/me", tags=["auth"])
def auth_me(user: Optional[Dict[str, Any]] = Depends(_auth.current_user_optional)):
    """Who am I? Returns user dict or {logged_in: false, setup_required}.
    setup_required=true means no user has a password yet and the client
    should show the first-run setup flow instead of the login form.

    Also returns `is_tenant`: when this Yorik is one of a multi-tenant
    set (vs. the host), the client uses it to hide host-only UI such
    as storage-location + backup config in the onboarding wizard.
    """
    from . import external_users as _eu
    is_tenant = _eu._is_tenant_mode()
    if user:
        return {"logged_in": True, "user": user, "is_tenant": is_tenant}
    return {
        "logged_in": False,
        "setup_required": not _auth.has_any_password(),
        "is_tenant": is_tenant,
    }


@app.get("/api/users/me/kiosk-agenda-consent", tags=["users"])
def get_my_kiosk_agenda_consent(
    user: Dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Read the current user's consent flag for "show my appointments
    on the household wall." Used by the Settings → Profile toggle to
    render its initial state."""
    with conn_ctx(DB_PATH) as conn:
        row = conn.execute(
            "SELECT COALESCE(kiosk_agenda_consent, 0) AS v "
            "FROM user_profiles WHERE id = ?",
            (user["id"],),
        ).fetchone()
    return {"consent": bool(row["v"]) if row else False}


class _KioskAgendaConsentBody(BaseModel):
    consent: bool


@app.patch("/api/users/me/kiosk-agenda-consent", tags=["users"])
def set_my_kiosk_agenda_consent(
    body: _KioskAgendaConsentBody,
    user: Dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Flip the current user's consent. Each user controls only their
    own flag — admins can't toggle it for someone else, on the theory
    that consenting to have your calendar projected on a household wall
    is a personal decision."""
    with conn_ctx(DB_PATH) as conn:
        conn.execute(
            "UPDATE user_profiles SET kiosk_agenda_consent = ? WHERE id = ?",
            (1 if body.consent else 0, user["id"]),
        )
        conn.commit()
    return {"consent": body.consent}


@app.post("/api/auth/login", tags=["auth"])
def auth_login(body: _LoginBody, request: Request, response: Response):
    from . import security_throttle as _throttle
    _auth_log = logging.getLogger("yorik.auth")
    client_ip = request.client.host if request.client else "unknown"
    # Cheap lockout check BEFORE we touch bcrypt — keeps a locked-out
    # account from burning CPU on every poke.
    allowed, retry, reason = _throttle.check_login_allowed(body.email, client_ip)
    if not allowed:
        _auth_log.warning("auth.deny login throttled email=%s ip=%s reason=%s",
                          body.email, client_ip, reason,
                          extra={"event": "login_throttled", "ip": client_ip})
        headers = {"Retry-After": str(retry or 60)}
        raise HTTPException(429, detail=reason or "too many attempts", headers=headers)

    user = _auth.get_user_by_email(body.email)
    if not user:
        # Same timing as a wrong-password rejection — don't leak whether
        # the email exists. (bcrypt verify on a dummy hash to keep
        # constant-ish time.)
        _auth.verify_password(body.password, "$2b$12$0000000000000000000000.dummyhashformusttakeequaltime")
        _throttle.record_login_failure(body.email, client_ip)
        _auth_log.warning("auth.deny login unknown_email=%s ip=%s",
                          body.email, client_ip,
                          extra={"event": "login_unknown_email", "ip": client_ip})
        raise HTTPException(401, "invalid credentials")
    if user.get("disabled"):
        # Don't count disabled-account hits against the throttle — those
        # are a config issue, not an attack signal. Same 403 surface as
        # before.
        _auth_log.warning("auth.deny login disabled user_id=%s email=%s ip=%s",
                          user["id"], body.email, client_ip,
                          extra={"event": "login_disabled", "user_id": user["id"], "ip": client_ip})
        raise HTTPException(403, "account disabled")
    if not _auth.verify_password(body.password, user.get("password_hash")):
        _throttle.record_login_failure(body.email, client_ip)
        _auth_log.warning("auth.deny login bad_password user_id=%s email=%s ip=%s",
                          user["id"], body.email, client_ip,
                          extra={"event": "login_bad_password", "user_id": user["id"], "ip": client_ip})
        raise HTTPException(401, "invalid credentials")
    # Successful auth → clear account-side counter so a user who fat-
    # fingered 3 times then got it right isn't one attempt from lockout.
    _throttle.clear_login_failures(body.email)
    # X-Yorik-Wall-Device: the Android wrapper's stable per-install
    # UUID. If it's in trusted_kiosk_devices, create_session applies
    # the saved kiosk policy automatically. Header missing on
    # non-wrapper logins; treated as "no trust to apply."
    wall_device = (request.headers.get("x-yorik-wall-device") or "").strip()
    sid = _auth.create_session(
        user["id"],
        user_agent=request.headers.get("user-agent", ""),
        ip=client_ip,
        wall_device_id=wall_device or None,
    )
    _auth.touch_login(user["id"])
    _auth._set_cookie(response, sid, request)
    _auth_log.info("auth.login ok user_id=%s email=%s ip=%s",
                   user["id"], body.email, client_ip,
                   extra={"event": "login_ok", "user_id": user["id"], "ip": client_ip})
    # Opportunistic provisioning. If the user's row is missing the
    # Immich API key or Paperless token (Yorik account pre-dates those
    # services being configured), provision them now with the password
    # they just typed. Best-effort, non-fatal — never blocks login,
    # never reveals provisioning errors to the client (those go to
    # the log). Fast-path when both are already linked.
    try:
        from . import external_users as _ext
        _ext.ensure_provisioned(
            user_id=user["id"], name=user["name"], email=body.email,
            password=body.password, role=user.get("role") or "",
        )
    except Exception as _exc:  # noqa: BLE001
        # Should be unreachable — ensure_provisioned catches per-service.
        # Guard anyway so a typo in the helper can't 500 the login itself.
        _auth_log.warning("ensure_provisioned wrapper raised: %s", _exc)
    return {"ok": True, "user": {
        "id": user["id"], "name": user["name"], "email": user["email"], "role": user["role"],
    }}


@app.post("/api/auth/logout", tags=["auth"])
def auth_logout(request: Request, response: Response):
    sid = request.cookies.get(_auth.COOKIE_NAME)
    if sid:
        _auth.delete_session(sid)
    _auth._clear_cookie(response, request)
    logging.getLogger("yorik.auth").info("auth.logout sid_known=%s",
                                          bool(sid),
                                          extra={"event": "logout"})
    return {"ok": True}


@app.post("/api/auth/setup", tags=["auth"])
def auth_setup(body: _SetupBody, request: Request, response: Response):
    """First-run only: set the initial admin password. Once any user
    has a password set, this endpoint refuses (admin must use the
    user-management flow to add new users instead)."""
    if _auth.has_any_password():
        raise HTTPException(409, "setup already complete — use /api/auth/login")

    # Phase F-lite invite-gate (tenant-mode only): if YORIK_DB_NAME
    # marks us as a tenant, require an invite token issued by the
    # host. Without this, anyone who reached the tenant's URL could
    # claim its admin role first. Validate via the host's
    # /api/internal/invites/<token>; consume after a successful
    # user create at the bottom of this function. Set
    # YORIK_ALLOW_UNATTESTED_SETUP=1 for dev/test (skips the gate
    # entirely; do NOT enable in prod).
    # Tenant detection: prefer the explicit YORIK_IS_TENANT flag
    # (set by create-tenant.sh in the manifest), fall back to the
    # legacy YORIK_DB_NAME inference for installs that pre-date the
    # explicit flag. See external_users._is_tenant_mode for the
    # rationale; we duplicate the check here (instead of importing)
    # to keep auth_setup's module-load order independent of the
    # external_users helpers.
    is_tenant_mode_outer = (
        os.getenv("YORIK_IS_TENANT", "").strip() in ("1", "true", "yes", "on")
        or (bool(os.getenv("YORIK_DB_NAME"))
            and os.getenv("YORIK_DB_NAME") != "postgres")
    )
    invite_consumed_payload: Optional[dict[str, Any]] = None
    if is_tenant_mode_outer and not os.getenv("YORIK_ALLOW_UNATTESTED_SETUP"):
        if not body.invite_token:
            raise HTTPException(400, "tenant setup requires an invite token (?invite=…)")
        try:
            from . import external_users as _eu
            tok = _eu._read_internal_token()
            host = _eu._host_internal_url()
            if not tok:
                raise HTTPException(503, "host internal token unreachable — tenant misconfigured")
            r = requests.get(
                f"{host}/api/internal/invites/{body.invite_token}",
                headers={"Authorization": f"Bearer {tok}"},
                timeout=10,
            )
            if r.status_code == 404:
                raise HTTPException(400, "invite token not found")
            if r.status_code == 410:
                raise HTTPException(400, "invite token already used or expired")
            r.raise_for_status()
            invite_consumed_payload = r.json()
            # Bind the invite to THIS tenant. Without this check, anyone
            # holding a valid invite for tenant X could paste it into
            # tenant Y's setup URL (whose admin slot is still empty)
            # and claim Y's admin. The host knows which tenant each
            # invite was issued to — refuse if it doesn't match the
            # one this Yorik is running for.
            expected = os.getenv("YORIK_DB_NAME", "")
            if expected.startswith("yorik_tenant_"):
                expected_name = expected[len("yorik_tenant_"):]
            else:
                expected_name = ""
            invite_tenant = invite_consumed_payload.get("tenant_name", "")
            if invite_tenant != expected_name:
                raise HTTPException(
                    400,
                    f"invite is for tenant '{invite_tenant}', this is '{expected_name}'",
                )
        except HTTPException:
            raise
        except requests.RequestException as exc:
            raise HTTPException(502, f"could not validate invite with host: {exc}")

    # Find or create the admin user.
    user = _auth.get_user_by_email(body.email)
    if not user:
        # Three paths converging here:
        #   * Main-instance Phase E (single-tenant): we have a
        #     reachable GoTrue at infra/supabase Kong. Create the
        #     auth.users row through GoTrue's admin API so the
        #     federated auth schema stays consistent.
        #   * Tenant-mode Phase E (YORIK_DB_NAME != 'postgres'):
        #     GoTrue points at a DIFFERENT database than the one
        #     this Yorik FastAPI is using. Generate the UUID locally
        #     and INSERT into the tenant's own auth.users shim
        #     (installed by scripts/create-tenant.sh).
        #   * Pre-Phase-E (sqlite installs): no auth.users at all;
        #     pass NULL and let user_profiles' BIGSERIAL default
        #     populate the id.
        name_val = body.name or body.email.split("@")[0]
        new_uid: Any = None
        is_tenant_mode = bool(os.getenv("YORIK_DB_NAME") and
                              os.getenv("YORIK_DB_NAME") != "postgres")

        if is_tenant_mode:
            import uuid as _uuid
            new_uid = str(_uuid.uuid4())
            try:
                with conn_ctx(DEFAULT_DB_PATH) as _auth_conn:
                    _auth_conn.execute(
                        "INSERT INTO auth.users (id, email) VALUES (?, ?) "
                        "ON CONFLICT (id) DO NOTHING",
                        (new_uid, body.email),
                    )
                    _auth_conn.commit()
            except Exception as exc:  # noqa: BLE001
                logging.getLogger("yorik.auth_setup").exception(
                    "tenant auth.users insert failed: %s", exc,
                )
                raise HTTPException(500, f"auth shim missing: {exc}")
        else:
            try:
                new_uid = _provision_supabase_auth_user(
                    email=body.email, password=body.password,
                    name=name_val, yorik_role="platform_admin",
                )
            except Exception as exc:  # noqa: BLE001
                # GoTrue unreachable / not configured (sqlite installs).
                # Use plain BIGSERIAL/INTEGER path: pass NULL and let
                # the DEFAULT (nextval(user_profiles_id_seq)) populate.
                logging.getLogger("yorik.auth_setup").warning(
                    "GoTrue user create failed (%s) — falling back to local id",
                    exc,
                )
                new_uid = None

        with conn_ctx(DEFAULT_DB_PATH) as conn:
            if new_uid is not None:
                cur = conn.execute(
                    "INSERT INTO user_profiles (id, name, email, role) "
                    "VALUES (?, ?, ?, 'platform_admin')",
                    (new_uid, name_val, body.email),
                )
                user_id = new_uid
            else:
                cur = conn.execute(
                    "INSERT INTO user_profiles (name, email, role) "
                    "VALUES (?, ?, 'admin')",
                    (name_val, body.email),
                )
                user_id = cur.lastrowid
    else:
        user_id = user["id"]
    _auth.set_password(user_id, body.password)
    # Phase B fresh-install seed FIRST so calendars created below get
    # the right space_id at INSERT time. Migration 036 only seeds
    # workspace + spaces when users already existed at migration time;
    # on a fresh install no users existed yet, so seed here.
    name = body.name or body.email.split("@")[0]
    from . import spaces as _sp
    try:
        _sp.ensure_workspace_exists(user_id, name)
        _sp.ensure_personal_space(user_id, name)
    except Exception as exc:  # noqa: BLE001
        import logging as _logging
        _logging.getLogger("yorik.auth_setup").exception(
            "phase B seed failed for first-run admin: %s", exc,
        )

    from . import calendars as _cal_mod
    _cal_mod.ensure_calendars_for_user(user_id, name)
    sid = _auth.create_session(
        user_id, user_agent=request.headers.get("user-agent", ""),
        ip=request.client.host if request.client else None,
        wall_device_id=(request.headers.get("x-yorik-wall-device") or "").strip() or None,
    )
    _auth.touch_login(user_id)
    _auth._set_cookie(response, sid, request)

    # Best-effort: provision the first-run admin in the bundled
    # Paperless + Immich so they don't have to do the "log into
    # Paperless web UI, find Profile → API Tokens, paste back into
    # Yorik Settings" dance. Failures are surfaced in the response but
    # NEVER block setup itself — if the bundled services aren't
    # configured (or are down), the admin can manually wire them later
    # via Settings → Users → Provision.
    provisioning: dict[str, Any] = {}
    for service, fn in (
        ("paperless", "provision_paperless"),
        ("immich",    "provision_immich"),
    ):
        try:
            from . import external_users as _ex
            # Setup-flow user is always the first admin; provision both
            # external services with admin privileges so they can see /
            # govern the whole corpus from day one.
            kwargs = {"is_admin": True} if fn in ("provision_paperless", "provision_immich") else {}
            result = getattr(_ex, fn)(user_id, name, body.email, body.password, **kwargs)
            entry: dict[str, Any] = {"ok": True}
            # Surface the generated-strong-password case to the caller
            # so the setup screen can show "Paperless needed a stronger
            # password — here it is, save it now". User keeps their
            # Yorik password; the Paperless password lives in
            # credential_store + this one-time response field.
            if isinstance(result, dict) and result.get("password_fallback_generated"):
                entry["fallback_password"] = result.get("paperless_password")
                entry["fallback_password_note"] = (
                    "Your chosen password didn't meet Paperless's complexity "
                    "rules. Use this stronger password for direct Paperless "
                    "logins; Yorik will use it automatically. Save it now "
                    "— it won't be shown again."
                )
            provisioning[service] = entry
        except Exception as exc:  # noqa: BLE001
            # "admin token not configured" is the common, expected case
            # when no bundled service exists — keep the response small
            # by tagging it as skipped rather than dressed up as an error.
            msg = str(exc)
            if "not configured" in msg.lower():
                provisioning[service] = {"ok": False, "skipped": True, "reason": msg}
            else:
                import logging as _logging
                _logging.getLogger("yorik.auth_setup").warning(
                    "%s provisioning for first-run admin failed: %s", service, exc,
                )
                provisioning[service] = {"ok": False, "error": msg}

    # Sync the just-provisioned admin into the reserved shared spaces
    # (their Paperless + Immich accounts now exist; the hooks fired
    # above would have skipped them because the mappings hadn't been
    # stored yet).
    try:
        from . import spaces as _sp
        with conn_ctx(DB_PATH) as conn:
            for r in conn.execute(
                "SELECT space_id FROM space_members WHERE user_id=?", (user_id,)
            ).fetchall():
                _sp.on_space_member_added(int(r["space_id"]), user_id, "admin")
    except Exception as exc:  # noqa: BLE001
        import logging as _logging
        _logging.getLogger("yorik.auth_setup").warning(
            "post-provision space resync failed for first-run admin: %s", exc,
        )

    # Consume the invite token now that the tenant admin exists. We
    # don't fail setup if the consume call hiccups (admin is already
    # in the tenant DB; the token just sits as un-consumed and will
    # expire by itself). Worst case the operator sees the same invite
    # link as still-active in /api/tenants for a few days.
    if invite_consumed_payload and body.invite_token:
        try:
            from . import external_users as _eu
            tok = _eu._read_internal_token()
            host = _eu._host_internal_url()
            if tok:
                requests.post(
                    f"{host}/api/internal/invites/{body.invite_token}/consume",
                    headers={"Authorization": f"Bearer {tok}"},
                    timeout=10,
                )
        except Exception as exc:  # noqa: BLE001
            import logging as _logging
            _logging.getLogger("yorik.auth_setup").warning(
                "invite consume failed (admin created OK, token may linger): %s",
                exc,
            )

    return {"ok": True, "user_id": user_id, "provisioning": provisioning}


class _ResetViaInviteBody(BaseModel):
    invite_token: str
    new_password: str


@app.post("/api/auth/reset-password-via-invite", tags=["auth"])
def auth_reset_password_via_invite(body: _ResetViaInviteBody, request: Request):
    """Tenant-only: consume a reset invite issued by the host and set
    the target admin's new password. Mirrors auth_setup's invite gate
    (validate via /api/internal/invites + bind to this tenant) but
    operates on an existing user instead of creating one.

    Refuses on the host (the host has its own GoTrue-backed password
    reset story; this endpoint is exclusively for tenant Yoriks whose
    auth is the local shim).
    """
    is_tenant_mode_outer = (
        os.getenv("YORIK_IS_TENANT", "").strip() in ("1", "true", "yes", "on")
        or (bool(os.getenv("YORIK_DB_NAME"))
            and os.getenv("YORIK_DB_NAME") != "postgres")
    )
    if not is_tenant_mode_outer:
        raise HTTPException(400, "reset-via-invite is only for tenant Yoriks")
    if len(body.new_password) < 8:
        raise HTTPException(400, "new_password must be at least 8 characters")

    # Look up the invite via the host. Same code shape as auth_setup;
    # we deliberately don't share the helper because the failure
    # messaging is reset-flow-specific.
    from . import external_users as _eu
    tok = _eu._read_internal_token()
    host = _eu._host_internal_url()
    if not tok:
        raise HTTPException(503, "host internal token unreachable — tenant misconfigured")
    try:
        r = requests.get(
            f"{host}/api/internal/invites/{body.invite_token}",
            headers={"Authorization": f"Bearer {tok}"},
            timeout=10,
        )
    except requests.RequestException as exc:
        raise HTTPException(502, f"could not validate reset invite with host: {exc}")
    if r.status_code == 404:
        raise HTTPException(400, "reset invite not found")
    if r.status_code == 410:
        raise HTTPException(400, "reset invite already used or expired")
    if not r.ok:
        raise HTTPException(502, f"invite lookup failed: HTTP {r.status_code}")
    payload = r.json()

    # Bind to current tenant_name.
    expected = os.getenv("YORIK_DB_NAME", "")
    expected_name = expected[len("yorik_tenant_"):] if expected.startswith("yorik_tenant_") else ""
    if payload.get("tenant_name") != expected_name:
        raise HTTPException(
            400,
            f"invite is for tenant {payload.get('tenant_name')!r}, this is {expected_name!r}",
        )
    target_email = (payload.get("target_email") or "").strip().lower()
    if not target_email:
        raise HTTPException(
            400,
            "this invite is for initial setup, not password reset (no target_email)",
        )

    # Find the target user in this tenant's DB.
    user = _auth.get_user_by_email(target_email)
    if not user:
        raise HTTPException(
            500,
            f"target user {target_email!r} does not exist on this tenant — "
            "host's verification was stale; ask host operator to re-issue",
        )

    # Set the new password. _auth.set_password handles bcrypt hashing
    # + the password_set_at timestamp; revoke_all_sessions kills any
    # active session under the old password.
    _auth.set_password(user["id"], body.new_password)
    _auth.revoke_all_sessions(user["id"])

    # Consume the invite. Soft-failure (admin password already changed
    # successfully; a stuck consume just leaves the token un-burned
    # until it expires, blocked by has_any_password() on retry anyway).
    try:
        requests.post(
            f"{host}/api/internal/invites/{body.invite_token}/consume",
            headers={"Authorization": f"Bearer {tok}"},
            timeout=10,
        )
    except Exception:  # noqa: BLE001
        logging.getLogger("yorik.reset_via_invite").warning(
            "invite consume failed (password reset succeeded, token may linger)",
        )

    return {"ok": True, "user_id": user["id"]}


@app.post("/api/auth/change-password", tags=["auth"])
def auth_change_password(
    body: _PasswordChangeBody,
    user: Dict[str, Any] = Depends(_auth.current_user),
):
    """Self-service password change. Requires current_password. Admin
    can reset others via /api/users/{id}/password (wave 2)."""
    full = _auth.get_user_by_email(user["email"])
    if not _auth.verify_password(body.current_password or "", full.get("password_hash")):
        raise HTTPException(401, "current password incorrect")
    if len(body.new_password) < 8:
        raise HTTPException(400, "new password must be at least 8 characters")
    _auth.set_password(user["id"], body.new_password)
    return {"ok": True}


# ───────────────────────── PIN (kiosk fallback) ────────────────────────
# A 4-digit numeric PIN per user, used ONLY on trusted-device sessions
# (mainly kiosks) to switch the active user without the full password.
# The PIN is a CONVENIENCE, not a strong credential; see the docstrings
# on auth_sessions.set_pin / session_is_trusted for the security
# boundary. Untrusted sessions cannot use PIN at all.

class _PinBody(BaseModel):
    pin: str


class _PinSwitchBody(BaseModel):
    user_id: str
    pin: str


class _VoiceLoginBody(BaseModel):
    swap_token: str
    profile_id: int


@app.post("/api/profile/pin", tags=["auth"])
def profile_set_pin(
    body: _PinBody,
    user: Dict[str, Any] = Depends(_auth.current_user),
):
    """Set or replace the current user's 4-digit kiosk PIN. Returns
    422 on malformed input (wrong length / non-digit). Does NOT
    revoke existing sessions — the PIN is a convenience for trusted
    devices, not a credential change."""
    try:
        _auth.set_pin(user["id"], body.pin)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return {"ok": True, "pin_set": True}


@app.delete("/api/profile/pin", tags=["auth"], status_code=204, response_class=Response)
def profile_clear_pin(
    user: Dict[str, Any] = Depends(_auth.current_user),
):
    """Remove the current user's PIN. Kiosk fallbacks for this user
    will then skip the PIN prompt — fine on a single-family device,
    less fine on a shared one. Settings → Devices surfaces a warning
    when this happens on a kiosk."""
    _auth.clear_pin(user["id"])
    return Response(status_code=204)


@app.post("/api/auth/pin-switch", tags=["auth"])
def auth_pin_switch(
    body: _PinSwitchBody,
    request: Request,
    response: Response,
):
    """Swap the active user on a TRUSTED device or session using a
    4-digit PIN — no password required. Returns {ok: true, user: {…}}
    and sets a cookie pointing at a fresh long-lived session for the
    picked user (same TTL as a password login, so the wall stays
    signed in across app closes).

    Two trust paths, either is sufficient:
      - Cookie session is marked trusted (i.e. is_kiosk OR
        trusted_until in future) — the normal desktop trusted-device
        flow.
      - x-yorik-wall-device header carries a UUID that's in
        trusted_kiosk_devices — the YorikWall wrapper flow. This
        lets pin-switch keep working even when the cookie is gone
        (fresh install, reboot, manual cookie-clear) as long as the
        device itself is still trusted.

    Off-trusted-LAN requests get 403 first, regardless of the trust
    path — the wall surface is deliberately gated to the household's
    local network or tailnet (wlan_trust.py).

    Throttling: same rate-limiter as /api/auth/login (10/min by IP)
    PLUS a stricter per-user attempt counter (5 bad PIN attempts /
    5 minutes locks the user from PIN-switch for an hour; full
    password still works as the recovery path).
    """
    if not is_trusted_lan_request(request):
        raise HTTPException(403, "pin-switch requires trusted-LAN access")
    sid = request.cookies.get(_auth.COOKIE_NAME)
    session_trusted = bool(sid and _auth.session_is_trusted(sid))
    # Read device_id up-front (NOT only on the trust-fallback path)
    # so create_session below can pass it regardless of which trust
    # path got us here — without this, a session-trusted PIN-switch
    # would crash with NameError on device_id.
    device_id = (request.headers.get("x-yorik-wall-device") or "").strip()
    if not session_trusted:
        # Fall back to wall-device trust: a YorikWall whose UUID is
        # in trusted_kiosk_devices can PIN-switch even when the
        # current cookie isn't a kiosk/trusted session (typical
        # after the previous ephemeral expired, or when the tablet
        # passes from one person to the next at dinner).
        if not (device_id and _auth.is_trusted_kiosk_device(device_id)):
            raise HTTPException(
                403, "this device isn't trusted — full password required"
            )
    # Throttle BEFORE the bcrypt call so brute-force attempts don't burn CPU.
    from . import security_throttle as _throttle
    client_ip = request.client.host if request.client else "unknown"
    pin_key = f"pin:{body.user_id}"
    allowed, retry, reason = _throttle.check_login_allowed(pin_key, client_ip)
    if not allowed:
        raise HTTPException(429, reason or "too many PIN attempts",
                             headers={"Retry-After": str(retry or 60)})
    if not _auth.verify_pin(int(body.user_id), body.pin or ""):
        _throttle.record_login_failure(pin_key, client_ip)
        raise HTTPException(401, "wrong PIN")
    target = _auth.get_user_by_id(int(body.user_id))
    if not target or target.get("disabled"):
        raise HTTPException(404, "user not found")
    # Mint a regular long-lived session for the picked user — same
    # TTL as a password login. The wall stays signed in across app
    # closes / reboots so you don't re-enter your PIN every time you
    # pick the tablet up.
    #
    # Pass wall_device_id so create_session auto-applies the device's
    # kiosk policy to the new session (is_kiosk=1, device_label,
    # kiosk_album_id, kiosk_show_today_photos, kiosk_block_phrases).
    # Without this, every PIN-switch leaves the session looking
    # non-kiosk in Settings → Devices and the admin has to re-apply
    # kiosk config on every switch. Ambient routes ALREADY worked via
    # the wall-device-header fallback; this just makes the visible
    # state match what the device is actually doing.
    new_sid = _auth.create_session(
        int(body.user_id),
        user_agent=request.headers.get("user-agent", "") + " (pin-switch)",
        ip=client_ip,
        wall_device_id=device_id or None,
    )
    response.set_cookie(
        key=_auth.COOKIE_NAME, value=new_sid, httponly=True,
        samesite="lax", secure=(request.url.scheme == "https"),
        max_age=_auth.SESSION_TTL_DAYS * 24 * 3600, path="/",
    )
    _auth.touch_login(int(body.user_id))
    return {"ok": True, "user": {"id": target["id"], "name": target["name"],
                                   "role": target["role"]}}


@app.post("/api/auth/voice-login", tags=["auth"])
def auth_voice_login(
    body: _VoiceLoginBody,
    request: Request,
    response: Response,
):
    """Swap the kiosk session over to the user that ECAPA matched in
    /api/ask-voice/stream, redeeming a single-use swap_token.

    Trust shape mirrors /api/auth/pin-switch:
      - LAN-gated (off-tailnet = 403 immediately).
      - x-yorik-wall-device header required and must be in
        trusted_kiosk_devices — voice-login is a kiosk feature only,
        regular browser sessions never go through this path.
      - swap_token must HMAC-verify and be single-use. The token is
        bound to (profile_id, device_uuid) so a captured token can't
        be replayed from a different device or against a different
        profile.

    On success: mints a regular long-lived session cookie for the
    matched user — same TTL as a password login — so the wall stays
    signed in across app closes / reboots until the next person taps
    the avatar grid.
    """
    if not is_trusted_lan_request(request):
        raise HTTPException(403, "voice-login requires trusted-LAN access")
    device_id = (request.headers.get("x-yorik-wall-device") or "").strip()
    if not (device_id and _auth.is_trusted_kiosk_device(device_id)):
        raise HTTPException(403, "voice-login requires a trusted kiosk device")
    # Same throttle bucket as PIN-switch — a flood of failed voice-ID
    # redemptions should lock the same way a flood of bad PINs does,
    # and against the same per-user key so the two surfaces share a
    # single attempt budget.
    from . import voice_login_tokens as _vtoken
    from . import security_throttle as _throttle
    client_ip = request.client.host if request.client else "unknown"
    voice_key = f"pin:{body.profile_id}"
    allowed, retry, reason = _throttle.check_login_allowed(voice_key, client_ip)
    if not allowed:
        raise HTTPException(429, reason or "too many login attempts",
                             headers={"Retry-After": str(retry or 60)})
    payload = _vtoken.verify(
        body.swap_token,
        expected_profile_id=int(body.profile_id),
        expected_device_uuid=device_id,
    )
    if not payload:
        _throttle.record_login_failure(voice_key, client_ip)
        raise HTTPException(401, "voice-login token invalid or expired")
    target = _auth.get_user_by_id(int(body.profile_id))
    if not target or target.get("disabled"):
        raise HTTPException(404, "user not found")
    new_sid = _auth.create_session(
        int(body.profile_id),
        user_agent=request.headers.get("user-agent", "") + " (voice-login)",
        ip=client_ip,
        wall_device_id=device_id or None,
    )
    response.set_cookie(
        key=_auth.COOKIE_NAME, value=new_sid, httponly=True,
        samesite="lax", secure=(request.url.scheme == "https"),
        max_age=_auth.SESSION_TTL_DAYS * 24 * 3600, path="/",
    )
    _auth.touch_login(int(body.profile_id))
    return {"ok": True, "user": {"id": target["id"], "name": target["name"],
                                   "role": target["role"]}}


@app.get("/api/auth/pin-pickable", tags=["auth"])
def auth_pin_pickable(request: Request) -> Dict[str, Any]:
    """Users the kiosk wall can pick + PIN-switch to. Returns
    {users: [{id, name, first_name}]} — every workspace user that has
    a PIN set and isn't disabled. Ordered alphabetically so the
    avatar grid is stable across taps.

    Gated by _require_kiosk_session: only a tablet that's been marked
    as a kiosk (and that's on the trusted LAN) can probe the user
    list. Non-kiosk callers get 403, off-LAN callers get 403 — the
    flow is exclusively for the household-tablet user-picker UX.
    """
    _require_kiosk_session(request)
    with conn_ctx(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, name, COALESCE(first_name, '') AS first_name "
            "FROM user_profiles "
            "WHERE pin_hash IS NOT NULL "
            "  AND (disabled = 0 OR disabled IS NULL) "
            "ORDER BY name ASC"
        ).fetchall()
    return {
        "users": [
            {
                "id":         int(r["id"]),
                "name":       r["name"],
                "first_name": r["first_name"] or (r["name"].split(" ")[0] if r["name"] else ""),
            }
            for r in rows
        ]
    }


# ───────────────────────── Kiosk / ambient routes ──────────────────────
# These are gated to kiosk sessions only. A normal browser session
# hitting /api/ambient/* gets 403 — no accidental kiosk-mode leakage
# into the desktop UI.

def _require_kiosk_session(request: Request) -> dict[str, Any]:
    """Resolve the kiosk policy for this request, or 403. Returns
    {user_id, kiosk_album_id, device_label, kiosk_show_today_photos,
    ...} from EITHER the cookie's kiosk session OR the wall-device's
    trusted_kiosk_devices row.

    Off-LAN requests get 403 unconditionally. The kiosk surface
    (ambient screen, switch-user PIN pad, slideshow) is deliberately
    gated to the household's local network — see wlan_trust.py.

    The wall-device fallback kicks in after a PIN-switch: the cookie
    now points at a short ephemeral session (is_kiosk=0), but the
    wall's UUID is still in trusted_kiosk_devices with the same
    album / show_today policy. Looking up the wall-device
    header lets the slideshow keep running for the picked user with
    the wall's persisted config — same photos, regardless of who
    just signed in via PIN."""
    if not is_trusted_lan_request(request):
        raise HTTPException(403, "kiosk routes require trusted-LAN access")
    sid = request.cookies.get(_auth.COOKIE_NAME)
    if sid:
        meta = _auth.kiosk_session_meta(sid)
        if meta:
            return meta
    device_id = (request.headers.get("x-yorik-wall-device") or "").strip()
    if device_id:
        meta = _auth.trusted_kiosk_device_meta(device_id)
        if meta:
            return meta
    if not sid:
        raise HTTPException(401, "not authenticated")
    raise HTTPException(403, "kiosk-only route — not a kiosk session")


@app.get("/api/ambient/slideshow", tags=["ambient"])
def ambient_slideshow(
    request: Request,
    limit: int = 200,
) -> Dict[str, Any]:
    """Fetch up to `limit` photos from the device's configured Immich
    album, newest first. Returns {photos: [{id, taken_at,
    thumbnail_url}], album_id, configured}. `configured=false` when
    the admin hasn't picked an album yet — the kiosk shows a "configure
    in Settings →" hint instead of a blank wall.

    Read-only. Cached at the HTTP layer (CDN-friendly headers). The
    kiosk frontend refetches every ~5 minutes so newly-added photos
    appear without a tablet reload.
    """
    meta = _require_kiosk_session(request)
    album_id     = meta.get("kiosk_album_id") or ""
    today_mode   = bool(meta.get("kiosk_show_today_photos"))
    uid          = int(meta.get("user_id") or 0)
    # Decode the persisted phrases blob (JSON list) — empty / NULL /
    # malformed all degrade silently to "no filter" so a fat-fingered
    # admin edit doesn't blank the wall.
    block_phrases_raw = meta.get("kiosk_block_phrases") or ""
    block_phrases: list[str] = []
    if block_phrases_raw:
        try:
            parsed = json.loads(block_phrases_raw)
            if isinstance(parsed, list):
                block_phrases = [str(p) for p in parsed if str(p).strip()]
        except (ValueError, TypeError):
            log.warning("ambient slideshow: malformed kiosk_block_phrases=%r", block_phrases_raw[:80])
    # Either signal is enough to consider the slideshow configured —
    # a kiosk can be album-only, today-only, or both.
    if not album_id and not today_mode:
        return {
            "photos":          [],
            "album_id":        None,
            "configured":      False,
            "show_today":      False,
            "block_phrases":   block_phrases,
        }
    from . import ambient as _ambient

    # Resolve the CLIP-based content blocklist once for this request.
    # The set is cached inside ambient.get_blocklist for 10min so the
    # 5-minute slideshow refresh doesn't re-fire CLIP every poll.
    blocklist = _ambient.get_blocklist(uid, block_phrases)

    # Today's photos go FIRST so the wall shows what happened today
    # before cycling into the curated album. Dedupe by Immich asset id
    # in case a today-photo is also in the album.
    #
    # Today's photos AGGREGATE across every active workspace user —
    # the wall is a shared surface, so what every household member
    # shot today should roll past everyone else's eyes. The album
    # path stays scoped to the wall-bound user (the kiosk's
    # configured album is admin's choice and intentionally curated).
    photos: list[dict[str, Any]] = []
    seen: set[str] = set()
    if today_mode:
        with conn_ctx(DB_PATH) as conn:
            uids = [
                int(r["id"])
                for r in conn.execute(
                    "SELECT id FROM user_profiles "
                    "WHERE (disabled = 0 OR disabled IS NULL) "
                    "ORDER BY id ASC"
                ).fetchall()
            ]
        for p in _ambient.get_today_photos_workspace(
            uids, limit=int(limit), exclude_ids=blocklist,
        ):
            if p["id"] in seen:
                continue
            seen.add(p["id"])
            photos.append(p)
    if album_id:
        remaining = max(1, int(limit) - len(photos))
        for p in _ambient.get_album_for_slideshow(
            album_id, uid, limit=remaining, exclude_ids=blocklist,
        ):
            if p["id"] in seen:
                continue
            seen.add(p["id"])
            photos.append(p)
    return {
        "photos":         photos,
        "album_id":       album_id or None,
        "configured":     True,
        "show_today":     today_mode,
        "block_phrases":  block_phrases,
        "blocked_count":  len(blocklist),
    }


@app.get("/api/ambient/idle", tags=["ambient"])
def ambient_idle(
    request: Request,
) -> Dict[str, Any]:
    """One-shot read for the idle overlay (next event + pending count
    + unread count). Returns {next_event, pending_tasks, unread_emails}.
    Each field is None when unavailable (no events scheduled, email
    not configured, etc.) so the overlay renders cleanly with chips
    for whatever data exists. Scope = the kiosk session's bound user.
    """
    meta = _require_kiosk_session(request)
    uid = int(meta.get("user_id") or 0)

    # Next event for the bound user. Joins through calendars to
    # respect spaces (same shape /api/events uses).
    next_event = None
    try:
        with conn_ctx(DB_PATH) as conn:
            row = conn.execute(
                "SELECT e.id, e.title, e.starts_at, e.ends_at, e.location "
                "FROM events e "
                "WHERE e.starts_at >= datetime('now') "
                "  AND (e.owner_user_id = ? OR e.owner_user_id IS NULL) "
                "ORDER BY e.starts_at ASC LIMIT 1",
                (uid,),
            ).fetchone()
        if row:
            next_event = {
                "id":         int(row["id"]),
                "title":      row["title"],
                "starts_at":  row["starts_at"],
                "ends_at":    row["ends_at"],
                "location":   row["location"],
            }
    except Exception as exc:  # noqa: BLE001 — kiosk should never 500
        log.warning("ambient_idle: next_event lookup failed: %s", exc)

    # Pending tasks for the bound user.
    pending_tasks = 0
    try:
        with conn_ctx(DB_PATH) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM tasks "
                "WHERE (done = 0 OR done IS NULL) "
                "  AND (created_by_user_id = ? OR created_by_user_id IS NULL)",
                (uid,),
            ).fetchone()
            pending_tasks = int(row["n"] or 0) if row else 0
    except Exception as exc:  # noqa: BLE001
        log.warning("ambient_idle: tasks count failed: %s", exc)

    # Unread email count — only when the user has any email account.
    unread_emails: Optional[int] = None
    try:
        with conn_ctx(DB_PATH) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM email_messages m "
                "JOIN email_accounts a ON a.id = m.account_id "
                "WHERE a.user_id = ? AND m.is_unread = 1 "
                "  AND m.is_sent = 0",
                (uid,),
            ).fetchone()
            if row:
                unread_emails = int(row["n"] or 0)
    except Exception:
        # Most likely: email tables/columns differ on the install or
        # the user has no email account. None = "no email chip on the
        # idle overlay" — UX correct in both cases.
        unread_emails = None

    return {
        "next_event":     next_event,
        "pending_tasks":  pending_tasks,
        "unread_emails":  unread_emails,
    }


@app.get("/api/ambient/agenda", tags=["ambient"])
def ambient_agenda(request: Request) -> Dict[str, Any]:
    """Today's events from every household member who has opted in
    to "show on kiosk." User-agnostic surface — no PIN, no cookie
    identity beyond the kiosk-scope check. Powers the swipe-right
    pane on /ambient: anyone in the kitchen can glance at the wall
    and see the household's day at a glance.

    Privacy boundary: default OFF (user_profiles.kiosk_agenda_consent
    starts at 0). Each user explicitly enables it in Settings →
    Profile. Off-LAN requests get 403 first; trusted-LAN + kiosk
    session OR trusted wall-device gets through.

    Returns events sorted chronologically; each row carries the
    owning user's id + first_name for the wall to render an avatar
    + label. Personal vs shared space distinction is irrelevant
    here — consent is per-user, not per-space.
    """
    _require_kiosk_session(request)
    out: list[dict[str, Any]] = []
    with conn_ctx(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT e.id, e.title, e.starts_at, e.ends_at, e.location, "
            "       u.id   AS owner_user_id, "
            "       u.name AS owner_name, "
            "       COALESCE(u.first_name, '') AS owner_first_name "
            "FROM events e "
            "JOIN user_profiles u ON u.id = e.owner_user_id "
            "WHERE u.kiosk_agenda_consent = 1 "
            "  AND (u.disabled = 0 OR u.disabled IS NULL) "
            "  AND date(e.starts_at) = date('now', 'localtime') "
            "ORDER BY e.starts_at ASC"
        ).fetchall()
    for r in rows:
        owner_first = r["owner_first_name"] or (
            r["owner_name"].split(" ")[0] if r["owner_name"] else ""
        )
        out.append({
            "id":         int(r["id"]),
            "title":      r["title"],
            "starts_at":  r["starts_at"],
            "ends_at":    r["ends_at"],
            "location":   r["location"],
            "owner": {
                "id":         int(r["owner_user_id"]),
                "name":       r["owner_name"],
                "first_name": owner_first,
            },
        })
    return {"events": out}


# ───────────────────────── Devices (trusted-device admin) ──────────────


class _DeviceKioskBody(BaseModel):
    """Mark a session as a kiosk + configure its slideshow album.
    Setting `is_kiosk=False` un-marks (and clears the album_id). Other
    fields are accepted as None to keep them unchanged on a partial
    update."""
    is_kiosk: bool
    kiosk_album_id: Optional[str] = None
    device_label: Optional[str] = None
    # Opt-in dynamic-wall: when true, the slideshow ALSO surfaces
    # photos taken today from the bound user's Immich library, shown
    # first (newest-first), then the curated album. Default False
    # preserves the explicit-consent model for kiosks that want it.
    show_today: Optional[bool] = None
    # CLIP-based content filter: free-text phrases ("medicine",
    # "prescription bottle", "receipt", "screenshot"). Each phrase is
    # run through Immich's smart search; the union of top-K matches
    # is removed from the slideshow. Pass [] to clear; None to leave
    # the current setting untouched on a partial update.
    block_phrases: Optional[List[str]] = None


@app.get("/api/devices", tags=["devices"])
def list_devices(
    request: Request,
    user: Dict[str, Any] = Depends(_auth.current_user),
) -> List[Dict[str, Any]]:
    """Sessions for the calling user — the Settings → Devices page
    lists these with revoke + (admin) make-kiosk controls. NEVER
    surfaces other users' sessions; admins viewing the workspace use a
    different endpoint (TBD).

    The row whose id matches the caller's cookie gets is_current=True
    so the frontend can both (a) label it "This device" in the UI and
    (b) gate the kiosk-redirect / idle-return behavior to ONLY apply
    on the kiosk device itself, not on the owner's laptop that
    happens to be signed in to the same user.
    """
    current_sid = request.cookies.get(_auth.COOKIE_NAME) or ""
    with conn_ctx(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, created_at, last_seen_at, expires_at, "
            "       user_agent, ip_seen, is_kiosk, kiosk_album_id, "
            "       device_label, trusted_until, kiosk_show_today_photos, "
            "       kiosk_block_phrases "
            "FROM sessions WHERE user_id = ? "
            "ORDER BY last_seen_at DESC",
            (user["id"],),
        ).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        # Decode JSON blob → list[str]; malformed / NULL → [].
        phrases_blob = r["kiosk_block_phrases"] or ""
        phrases: List[str] = []
        if phrases_blob:
            try:
                parsed = json.loads(phrases_blob)
                if isinstance(parsed, list):
                    phrases = [str(p) for p in parsed if str(p).strip()]
            except (ValueError, TypeError):
                pass
        out.append({
            "id":              r["id"],
            "created_at":      r["created_at"],
            "last_seen_at":    r["last_seen_at"],
            "expires_at":      r["expires_at"],
            "user_agent":      r["user_agent"] or "",
            "ip_seen":         r["ip_seen"] or "",
            "is_kiosk":        bool(r["is_kiosk"]),
            "is_current":      bool(current_sid) and r["id"] == current_sid,
            "kiosk_album_id":  r["kiosk_album_id"],
            "device_label":    r["device_label"] or "",
            "trusted_until":   r["trusted_until"],
            "show_today":      bool(r["kiosk_show_today_photos"]),
            "block_phrases":   phrases,
        })
    return out


@app.get("/api/devices/albums", tags=["devices"])
def list_kiosk_album_candidates(
    user: Dict[str, Any] = Depends(_auth.current_user),
) -> List[Dict[str, Any]]:
    """Immich album catalogue for the Settings → Devices kiosk-toggle
    dropdown. Admin-only — non-admins shouldn't see other users'
    sharing surface.

    Scoped to the CALLING user's Immich library, not the admin's, so
    the dropdown surfaces albums the kiosk will actually be able to
    fetch from once configured. (If an admin picks one of their own
    albums for a kiosk bound to a different user, the slideshow would
    return empty — confusing. Per-user scope keeps the catalogue
    honest.)"""
    if (user.get("role") or "") not in ("admin", "platform_admin"):
        raise HTTPException(403, "admin only")
    from . import ambient as _ambient
    return _ambient.list_albums(user_id=user.get("id") or 0)


@app.post("/api/devices/{session_id}/kiosk", tags=["devices"])
def mark_device_as_kiosk(
    session_id: str,
    body: _DeviceKioskBody,
    request: Request,
    user: Dict[str, Any] = Depends(_auth.current_user),
):
    """Toggle a specific session into (or out of) kiosk mode.

    Admin-only AND trusted-LAN only on enable: kiosk capability is
    gated to the household's local network. Disable is allowed from
    anywhere so an admin can clean up a wall remotely if needed.
    Side effects:
      - On enable: sets is_kiosk=1, kiosk_album_id, device_label,
        AND extends trusted_until to now+365d so PIN-switching is
        allowed. The 30-day default TTL gets bumped at the same time.
      - On disable: clears is_kiosk + kiosk_album_id + device_label
        AND clears trusted_until. The session reverts to a normal
        30-day browser session.
    """
    if (user.get("role") or "") not in ("admin", "platform_admin"):
        raise HTTPException(403, "admin only")
    if body.is_kiosk and not is_trusted_lan_request(request):
        raise HTTPException(403, "kiosk can only be enabled from a trusted-LAN device")
    with conn_ctx(DB_PATH) as conn:
        row = conn.execute(
            "SELECT id, user_id FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "session not found")
        if body.is_kiosk:
            # Enable: stamp the kiosk fields AND extend trust + TTL.
            # show_today defaults to False on enable when not provided,
            # so the privacy-conservative default holds; once set, a
            # follow-up partial update can flip it without touching
            # the album.
            # block_phrases: None=leave alone; [] / [...]=write a JSON
            # blob (or NULL when the list is empty after trimming).
            if body.block_phrases is None:
                phrases_arg: Optional[str] = None  # COALESCE keeps existing
            else:
                cleaned = [str(p).strip() for p in body.block_phrases if str(p).strip()]
                phrases_arg = json.dumps(cleaned) if cleaned else None
            conn.execute(
                "UPDATE sessions SET "
                "  is_kiosk = 1, "
                "  kiosk_album_id = ?, "
                "  device_label = COALESCE(?, device_label), "
                "  kiosk_show_today_photos = COALESCE(?, kiosk_show_today_photos, 0), "
                "  kiosk_block_phrases = "
                "    CASE WHEN ? = 1 THEN ? ELSE kiosk_block_phrases END, "
                "  trusted_until = datetime('now', '+365 days'), "
                "  expires_at = datetime('now', '+365 days') "
                "WHERE id = ?",
                (body.kiosk_album_id, body.device_label,
                 1 if body.show_today else (0 if body.show_today is False else None),
                 1 if body.block_phrases is not None else 0,
                 phrases_arg,
                 session_id),
            )
        else:
            conn.execute(
                "UPDATE sessions SET "
                "  is_kiosk = 0, "
                "  kiosk_album_id = NULL, "
                "  device_label = NULL, "
                "  kiosk_show_today_photos = 0, "
                "  kiosk_block_phrases = NULL, "
                "  trusted_until = NULL "
                "WHERE id = ?",
                (session_id,),
            )
        conn.commit()
    return {"ok": True, "session_id": session_id, "is_kiosk": body.is_kiosk}


@app.delete("/api/devices/{session_id}", tags=["devices"], status_code=204, response_class=Response)
def revoke_device(
    session_id: str,
    user: Dict[str, Any] = Depends(_auth.current_user),
):
    """Revoke a session — the device on the other end is logged out
    immediately. Users can revoke their OWN sessions; admins can
    revoke any session in the workspace."""
    with conn_ctx(DB_PATH) as conn:
        row = conn.execute(
            "SELECT id, user_id FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            return Response(status_code=204)  # already gone — idempotent
        if int(row["user_id"]) != user["id"] and (user.get("role") or "") not in ("admin", "platform_admin"):
            raise HTTPException(403, "can only revoke your own sessions")
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        conn.commit()
    return Response(status_code=204)


# ─── Trusted-kiosk device registry ────────────────────────────────────


@app.post("/api/devices/trust", tags=["devices"])
def trust_current_device(
    request: Request,
    user: Dict[str, Any] = Depends(_auth.current_user),
):
    """Mark the YorikWall-wrapper device that issued this request as
    a "trusted kiosk." Every future session that comes in with this
    device's X-Yorik-Wall-Device header is auto-flagged is_kiosk
    with the saved album / show_today / block_phrases —
    no per-login dance.

    Reads the trust config from the CALLING session's existing
    kiosk fields (so the typical flow is: admin marks this session
    as a kiosk in the regular Devices UI, then calls /trust to
    pin the policy to the physical device).
    """
    if (user.get("role") or "") not in ("admin", "platform_admin"):
        raise HTTPException(403, "admin only")
    if not is_trusted_lan_request(request):
        raise HTTPException(403, "device trust can only be established from a trusted-LAN device")
    device_id = (request.headers.get("x-yorik-wall-device") or "").strip()
    if not device_id:
        raise HTTPException(400, "no X-Yorik-Wall-Device header — only the YorikWall app can be trusted")
    sid = request.cookies.get(_auth.COOKIE_NAME) or ""
    if not sid:
        raise HTTPException(401, "no session")
    with conn_ctx(DB_PATH) as conn:
        srow = conn.execute(
            "SELECT device_label, kiosk_album_id, kiosk_show_today_photos, "
            "       kiosk_block_phrases "
            "FROM sessions WHERE id = ?",
            (sid,),
        ).fetchone()
        if not srow:
            raise HTTPException(401, "session not found")
        conn.execute(
            "INSERT INTO trusted_kiosk_devices ("
            "  device_id, user_id, device_label, kiosk_album_id, "
            "  kiosk_show_today, kiosk_block_phrases"
            ") VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(device_id) DO UPDATE SET "
            "  user_id = excluded.user_id, "
            "  device_label = excluded.device_label, "
            "  kiosk_album_id = excluded.kiosk_album_id, "
            "  kiosk_show_today = excluded.kiosk_show_today, "
            "  kiosk_block_phrases = excluded.kiosk_block_phrases, "
            "  last_seen_at = CURRENT_TIMESTAMP",
            (device_id, user["id"], srow["device_label"],
             srow["kiosk_album_id"],
             1 if srow["kiosk_show_today_photos"] else 0,
             srow["kiosk_block_phrases"]),
        )
        conn.commit()
    return {"ok": True, "device_id": device_id[:8] + "…"}


@app.delete("/api/devices/trust/{device_id}", tags=["devices"], status_code=204, response_class=Response)
def revoke_trusted_device(
    device_id: str,
    user: Dict[str, Any] = Depends(_auth.current_user),
):
    """Remove a wall from the trusted-kiosk registry. Existing
    sessions on that device keep their is_kiosk flag (this just
    stops FUTURE logins from auto-applying the policy)."""
    if (user.get("role") or "") not in ("admin", "platform_admin"):
        raise HTTPException(403, "admin only")
    with conn_ctx(DB_PATH) as conn:
        conn.execute("DELETE FROM trusted_kiosk_devices WHERE device_id = ?", (device_id,))
        conn.commit()
    return Response(status_code=204)


class _HotwordToggleBody(BaseModel):
    enabled: bool


@app.patch("/api/devices/trust/{device_id}/hotword", tags=["devices"])
def set_trusted_device_hotword(
    device_id: str,
    body: _HotwordToggleBody,
    request: Request,
    user: Dict[str, Any] = Depends(_auth.current_user),
):
    """Toggle the always-listening "Hey Yorik" wake-word on a trusted
    kiosk device. Admin + trusted-LAN gated.

    The native YorikWall wrapper reads this flag at app start and
    starts / stops the foreground wake-word service accordingly. The
    Settings page that calls this endpoint should ALSO call
    window.YorikNative.setHotwordEnabled() so the service flips
    immediately instead of waiting for the next cold start.
    """
    if (user.get("role") or "") not in ("admin", "platform_admin"):
        raise HTTPException(403, "admin only")
    if not is_trusted_lan_request(request):
        raise HTTPException(403, "hotword toggle requires trusted-LAN access")
    with conn_ctx(DB_PATH) as conn:
        row = conn.execute(
            "SELECT device_id FROM trusted_kiosk_devices WHERE device_id = ?",
            (device_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "device not in trusted registry")
        conn.execute(
            "UPDATE trusted_kiosk_devices "
            "SET kiosk_hotword_enabled = ?, last_seen_at = CURRENT_TIMESTAMP "
            "WHERE device_id = ?",
            (1 if body.enabled else 0, device_id),
        )
        conn.commit()
    return {"ok": True, "device_id": device_id, "kiosk_hotword_enabled": body.enabled}


@app.get("/api/devices/trust", tags=["devices"])
def list_trusted_devices(
    user: Dict[str, Any] = Depends(_auth.current_user),
) -> List[Dict[str, Any]]:
    """Trusted-kiosk devices the admin has marked. Used by the
    Settings → Devices page to render the trust badge + revoke
    button. Empty list for non-admins."""
    if (user.get("role") or "") not in ("admin", "platform_admin"):
        return []
    with conn_ctx(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT device_id, device_label, kiosk_album_id, "
            "       kiosk_show_today, kiosk_hotword_enabled, "
            "       created_at, last_seen_at "
            "FROM trusted_kiosk_devices "
            "WHERE user_id = ? "
            "ORDER BY last_seen_at DESC",
            (user["id"],),
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/skills/{name}/invoke", tags=["skills"])
async def invoke_skill(
    name: str,
    args: dict = None,
    user: Optional[Dict[str, Any]] = Depends(_auth.current_user_optional),
):
    """Direct invocation. SkillContext carries the calling user so
    skills like find_document / find_photo route through the right
    per-user credentials (wave 3)."""
    args = args or {}
    from .skills import SkillContext
    role = (user or {}).get("role", "admin")
    user_id = (user or {}).get("id", 1)
    ctx = SkillContext(get_registry(), role=role, user_id=user_id)
    try:
        return await get_registry().invoke(name, ctx=ctx, **args)
    except SkillError as e:
        raise HTTPException(400, str(e))


# Content-Security-Policy on every dashboard response.
# Why: this is the single biggest safety win for the marketplace story. With
# script/connect/img sources locked to 'self', a malicious community-uploaded
# layout cannot phone home, embed tracking pixels, pull in remote scripts, or
# inject iframes pointing at attacker domains. Worst it can do is mess up the
# UI — annoying but not catastrophic. Audit becomes a *quality* filter, not
# a safety filter.
#
# 'unsafe-inline' for style/script kept ONLY because index.html has tiny
# inline handlers and our modals use inline style attributes. Tighten later
# by extracting those into separate files.
def _external_iframe_origins(request) -> str:
    """CSP frame-src needs to allow external apps that ship as iframes
    (Photos → Immich). Compute the allowed Immich origin per-request from
    the host header so it works on localhost AND through Tailscale without
    a config knob.

    Loopback quirk: CSP treats `localhost` and `127.0.0.1` as DIFFERENT
    origins for matching purposes. The backend's Immich URL builders
    default to `http://localhost:2283`, but a user who opens Yorik at
    `http://127.0.0.1:8000/...` would otherwise have those photo URLs
    blocked. When the request host is any loopback form, allow BOTH
    canonical forms so the embedded `<img src="http://localhost:2283/…">`
    works regardless of which loopback the user typed in their browser.
    """
    # request.url.hostname strips the port; we want the host header verbatim
    # so we can derive the matching Immich port.
    host_header = request.headers.get("host", "")
    hostname = host_header.split(":", 1)[0] if host_header else "localhost"

    # If the user is on any loopback, list both forms so URLs built
    # with the other form still match the CSP.
    hosts: list[str]
    if hostname in ("localhost", "127.0.0.1", "[::1]", "::1"):
        hosts = ["localhost", "127.0.0.1"]
    else:
        hosts = [hostname]

    # Immich on 2283 (host) / 8443 (Tailscale-served).
    # Paperless on 8010 (host) / 8444 (Tailscale-served).
    parts: list[str] = []
    for h in hosts:
        parts.append(f"http://{h}:2283 https://{h}:8443 "
                     f"http://{h}:8010 https://{h}:8444")

    # Operator-configured subdomain proxies (Caddy install pattern).
    # Without these the Photos / Documents iframes get blocked by CSP
    # when YORIK_*_PUBLIC_URL is pointed at a real subdomain rather
    # than a port-on-same-host.
    for env_key in ("YORIK_IMMICH_PUBLIC_URL", "YORIK_PAPERLESS_PUBLIC_URL"):
        raw = (os.getenv(env_key) or "").strip().rstrip("/")
        if not raw:
            continue
        # Strip path so we only contribute the origin to the CSP list.
        from urllib.parse import urlsplit
        u = urlsplit(raw)
        if u.scheme and u.netloc:
            parts.append(f"{u.scheme}://{u.netloc}")
    return " ".join(parts)


# ─── Global auth guard ──────────────────────────────────────────────────────
#
# Closes the historical "?role=admin opens the door" hole: any /api/* path
# that isn't on the whitelist requires a valid session cookie. Rejects with
# 401 before the handler runs, so endpoints that still take role= as a
# query param can't be abused to escalate privilege.
#
# Whitelist matches a path *prefix* unless prefixed with "==" (exact). The
# few entries here are: login/setup/me (need to reach them to log in), the
# health endpoint (used by monitoring), the Paperless ingest webhook (uses
# its own shared-secret), the OpenAPI spec for tooling, and the OAuth
# callback used by the email-gmail connector.

_AUTH_WHITELIST_PREFIX: Final[tuple[str, ...]] = (
    "/api/auth/",                       # login, setup, logout, me, change-password
    "/api/paperless/ingest/",           # Paperless POST_CONSUME_SCRIPT webhook (X-Paperless-Token)
    "/api/internal/",                   # tenant→host RPC, gated by shared bearer token instead of session cookie
)
_AUTH_WHITELIST_EXACT: Final[frozenset[str]] = frozenset({
    "/api/health",                      # health probe
    "/api/openapi.json",                # API schema (also gated separately by FastAPI)
})


def _auth_path_allowed(path: str) -> bool:
    if path in _AUTH_WHITELIST_EXACT:
        return True
    for prefix in _AUTH_WHITELIST_PREFIX:
        if path.startswith(prefix):
            return True
    return False


# Paths that bypass the rate limiter — high-frequency probes and
# unauthenticated calls that need to stay snappy. Health is polled by
# external monitors; the notifications/workers endpoints are polled by
# the React shell every few seconds when open.
_RATE_LIMIT_BYPASS_EXACT: Final[frozenset[str]] = frozenset({
    "/api/health",
})


def _rate_limit_path_bypass(path: str) -> bool:
    if path in _RATE_LIMIT_BYPASS_EXACT:
        return True
    # Static assets served at root, frontend bundle, paperless/n8n
    # reverse proxies — none of those are rate-limited. The 120/min
    # general bucket would hurt PaperlessUI's burst loads.
    if not path.startswith("/api/"):
        return True
    if path.startswith("/api/paperless/ingest/"):  # Paperless webhook, has its own token
        return True
    return False


# State-changing methods we CSRF-check. Read-only methods (GET, HEAD,
# OPTIONS) can't cause side effects so don't need Origin validation.
_CSRF_PROTECTED_METHODS: Final[frozenset[str]] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Endpoints that are intentionally callable cross-origin (webhooks
# from external services bringing their own auth). Bypass the CSRF
# check by path prefix.
_CSRF_BYPASS_PREFIXES: Final[tuple[str, ...]] = (
    "/api/paperless/ingest/",   # Paperless POST_CONSUME webhook (token-auth)
)


def _csrf_origin_allowed(request) -> bool:
    """Origin/Referer header allowlist. Returns True when the header
    matches the request's own Host (same-origin) or an explicitly
    trusted origin from env. Returns True when there's no Origin AND
    no Referer either — that's a non-browser caller (curl, server-side
    script) which cookies-from-a-browser CSRF doesn't apply to."""
    origin = request.headers.get("origin") or ""
    referer = request.headers.get("referer") or ""
    if not origin and not referer:
        return True  # non-browser; cookie-CSRF not a vector

    host_header = request.headers.get("host", "")
    # The browser sends the full origin (scheme://host[:port]). Compute
    # both http+https variants of our own host so http-served boxes and
    # https-fronted ones both work.
    same_origin = {
        f"http://{host_header}",
        f"https://{host_header}",
    }
    trusted_env = os.getenv("YORIK_TRUSTED_ORIGINS", "").strip()
    if trusted_env:
        for o in trusted_env.split(","):
            o = o.strip().rstrip("/")
            if o:
                same_origin.add(o)

    # Origin takes precedence if present; fall back to Referer's origin.
    candidate = origin
    if not candidate and referer:
        # Strip path off the Referer to compare just the origin.
        try:
            from urllib.parse import urlparse
            u = urlparse(referer)
            candidate = f"{u.scheme}://{u.netloc}"
        except Exception:
            return False
    return candidate.rstrip("/") in same_origin


@app.middleware("http")
async def cap_request_body_size(request, call_next):
    """Global request-body size cap. Stops a 10GB POST anywhere
    (including endpoints without their own per-call streaming-size
    check) from chewing through memory before the handler can react.

    Two layers:
      1. Cheap: check Content-Length header up front — covers honest
         clients. Returns 413 immediately.
      2. Robust: wrap the ASGI `receive` so we count bytes as they
         stream in, and abort if we cross the threshold mid-flight
         (catches clients lying about Content-Length).

    Default 100MB so it sits comfortably above the 50MB upload cap
    in /api/documents/upload but well below memory-pressure
    territory. Override via YORIK_MAX_REQUEST_MB.
    """
    max_bytes = int(os.getenv("YORIK_MAX_REQUEST_MB", "100")) * 1024 * 1024
    # Layer 1: declared Content-Length
    cl = request.headers.get("content-length")
    if cl:
        try:
            if int(cl) > max_bytes:
                return JSONResponse(
                    {"detail": f"request body exceeds {max_bytes // (1024*1024)}MB limit"},
                    status_code=413,
                )
        except ValueError:
            pass

    # Layer 2: streamed-body byte counter via wrapped receive
    body_seen = 0
    original_receive = request.receive

    async def counting_receive():
        nonlocal body_seen
        msg = await original_receive()
        if msg["type"] == "http.request":
            body_seen += len(msg.get("body", b""))
            if body_seen > max_bytes:
                # Returning an http.disconnect would be cleanest, but
                # Starlette accepts another http.request with the
                # `more_body=False` flag and an empty body to finish
                # parsing — easier than synthesising a disconnect.
                # We then reject in the response.
                msg = {"type": "http.request", "body": b"", "more_body": False}
        return msg

    request._receive = counting_receive
    response = await call_next(request)
    if body_seen > max_bytes:
        # call_next already ran; the handler saw a truncated body.
        # Override its response with the proper 413.
        return JSONResponse(
            {"detail": f"request body exceeds {max_bytes // (1024*1024)}MB limit"},
            status_code=413,
        )
    return response


@app.middleware("http")
async def csrf_origin_check(request, call_next):
    """CSRF defense in depth.

    SameSite=Lax on the session cookie already blocks cross-origin
    top-level POSTs from triggering with credentials in modern browsers.
    This middleware adds a second check: state-changing requests whose
    Origin or Referer doesn't match the box's own host (or an explicitly
    trusted one) get rejected with 403 BEFORE touching auth or any
    handler.

    For environments where the API is hit from a different origin
    legitimately (a separate frontend, mobile app, etc.), set
    YORIK_TRUSTED_ORIGINS="https://app.example.com,https://other.example.com"
    in config.env / env vars.
    """
    if request.method in _CSRF_PROTECTED_METHODS:
        path = request.url.path
        if not any(path.startswith(p) for p in _CSRF_BYPASS_PREFIXES):
            if not _csrf_origin_allowed(request):
                return JSONResponse(
                    {"detail": "origin not allowed"},
                    status_code=403,
                )
    return await call_next(request)


@app.middleware("http")
async def rate_limit_api(request, call_next):
    """Per-IP, per-bucket sliding-window rate limit on /api/*.

    Bucket caps live in backend/security_throttle.py:
      - /api/ask, /api/ask-voice → 15/min (LLM cost)
      - /api/auth/login          → 10/min (defense in depth; login-guard
                                    also tracks per-account + per-IP fails)
      - /api/documents/upload    → 30/min
      - everything else under /api/ → 500/min

    Runs BEFORE the session-check middleware so an unauthenticated
    burst can't burn bcrypt cycles. CORS preflights bypass — they're
    cheap and CORS middleware needs to answer them.
    """
    if request.method == "OPTIONS":
        return await call_next(request)
    path = request.url.path
    if not _rate_limit_path_bypass(path):
        from . import security_throttle as _throttle
        client_ip = request.client.host if request.client else "unknown"
        allowed, retry = _throttle.check_api_allowed(client_ip, path)
        if not allowed:
            return JSONResponse(
                {"detail": "rate limit exceeded — slow down"},
                status_code=429,
                headers={"Retry-After": str(retry or 60)},
            )
    return await call_next(request)


@app.middleware("http")
async def require_session_for_api(request, call_next):
    """Reject /api/* requests without a valid session, except a small
    whitelist. Endpoints that legitimately need anonymous access (login,
    health, Paperless ingest webhook) opt in via the whitelist above.
    CORS preflights (OPTIONS) bypass the check so the CORS middleware
    can answer them — browsers send preflights without cookies, and
    blocking them at the auth layer would break legitimate cross-origin
    callers we DO want to support (Vite dev server, future PWA shell)."""
    if request.method == "OPTIONS":
        return await call_next(request)
    path = request.url.path
    if path.startswith("/api/") and not _auth_path_allowed(path):
        # The cookie attribute name lives on _auth — read it via the
        # documented helper to avoid drifting if the name changes.
        sid = request.cookies.get(_auth.COOKIE_NAME)
        client_ip = request.client.host if request.client else None
        user = _auth.get_user_for_session(sid, ip=client_ip) if sid else None
        # Phase E §1: also accept the Supabase Auth JWT in the
        # Authorization header. Strategy B: cookie + JWT both work
        # until the frontend has fully moved to Supabase, then the
        # cookie branch above gets deleted (Strategy C).
        if not user:
            auth_header = request.headers.get("authorization") or ""
            if auth_header.lower().startswith("bearer "):
                user = _auth._get_user_from_jwt(auth_header[7:].strip())
        if not user:
            return JSONResponse(
                {"detail": "not authenticated"},
                status_code=401,
                headers={"WWW-Authenticate": 'Session realm="yorik"'},
            )
    return await call_next(request)


@app.middleware("http")
async def add_csp_header(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        return response  # API responses are JSON, CSP irrelevant
    # FastAPI's auto Swagger UI / ReDoc pages load assets from
    # cdn.jsdelivr.net + fastapi.tiangolo.com. They're dev-tooling pages
    # (not customer-facing), so leave them un-policed instead of widening
    # the global CSP for everyone.
    if request.url.path in ("/docs", "/redoc", "/docs/oauth2-redirect"):
        return response
    # Paperless's Angular SPA — proxied through /paperless/* — uses inline
    # scripts/styles and its own asset URLs that our CSP would block. It's
    # served same-origin via the Yorik proxy so leaving it un-policed
    # doesn't widen the attack surface against the rest of the app.
    if request.url.path.startswith("/paperless/"):
        return response
    # Same story for n8n: Vue-based editor with inline assets that our
    # CSP would block.
    if request.url.path.startswith("/n8n/"):
        return response
    frame_origins = _external_iframe_origins(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        # External-app origins (Immich, Paperless) are allowed as
        # image+media sources too — the chat renders inline photo
        # thumbnails directly from Immich (api key embedded in URL),
        # and the Photos app shows previews from the same hosts.
        # Same per-host derivation as frame-src so it works on
        # localhost AND through Tailscale.
        f"img-src 'self' data: blob: {frame_origins}; "
        f"media-src 'self' blob: {frame_origins}; "
        "connect-src 'self'; "
        "font-src 'self' data:; "
        # Layout iframes use srcdoc → about:srcdoc origin. 'self' covers it
        # in Chromium/Firefox but Safari needs the explicit `about:` token.
        # External-iframe apps (Immich) get explicit per-host origins.
        f"frame-src 'self' about: {frame_origins}; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.middleware("http")
async def request_correlation_id(request, call_next):
    # Outermost middleware: stamps every log line emitted during this
    # request with `corr=<id>` and echoes the id back as a response
    # header so a tester hitting "Yorik is broken" can paste one id
    # into an issue and we grep the matching trace out of yorik.log.
    # Honours an inbound X-Correlation-Id (lets external callers thread
    # their own request id through end-to-end).
    import time as _time
    import uuid as _uuid
    cid = request.headers.get("x-correlation-id") or _uuid.uuid4().hex[:12]
    token = _logging_setup.correlation_id.set(cid)
    started = _time.monotonic()
    try:
        response = await call_next(request)
        response.headers["X-Correlation-Id"] = cid
        # Per-request access log into the structured stream. Static asset
        # noise is filtered by path prefix — those don't help debugging.
        path = request.url.path
        if not (path.startswith("/r/") and "." in path.rsplit("/", 1)[-1]) and path != "/favicon.ico":
            dur = int((_time.monotonic() - started) * 1000)
            _access = logging.getLogger("yorik.access")
            _lvl = (logging.WARNING if response.status_code >= 500
                    else logging.INFO if response.status_code < 400
                    else logging.INFO)
            _access.log(_lvl, "%s %s -> %d (%dms)",
                        request.method, path, response.status_code, dur,
                        extra={"method": request.method, "path": path,
                               "http_status": response.status_code,
                               "duration_ms": dur})
        return response
    finally:
        _logging_setup.correlation_id.reset(token)


def _port_already_bound(host: str, port: int) -> bool:
    """Probe whether (host, port) is held by another LISTEN socket.
    Mirrors uvicorn's own bind flags (SO_REUSEADDR) so TIME_WAIT leftovers
    from a recently-killed predecessor don't trip a false positive — only
    a genuine LISTEN holder (e.g. an orphan inheriting the FD from a
    previous `pkill -KILL`) returns True. Caller short-circuits startup
    in that case; see _startup() below."""
    import socket as _sock
    s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
    s.setsockopt(_sock.SOL_SOCKET, _sock.SO_REUSEADDR, 1)
    try:
        s.bind((host, port))
    except OSError:
        return True
    finally:
        s.close()
    return False


@app.on_event("startup")
def _startup() -> None:
    # Bind-probe FIRST. Background: a `pkill -KILL` on the uvicorn parent
    # used to leave a TTS subprocess inheriting the listening FD; systemd
    # would respawn yorik, uvicorn's bind would fail with EADDRINUSE, and
    # the racy app.shutdown() would tear down an ONNX/Piper worker mid-
    # init → `terminate called without an active exception` → SIGABRT.
    # systemd then restart-stormed indefinitely. By exiting BEFORE we
    # spawn any native threads (voice acks, Whisper preload, paperless
    # reconciler), we eliminate the std::terminate path. os._exit skips
    # Python cleanup so even if anything was loaded above this point it
    # cannot fire a destructor that crashes.
    _bind_host = os.environ.get("YORIK_BIND", "0.0.0.0")
    # HOMEOS_PORT is set by tenant manifests (via systemd
    # EnvironmentFile) and overrides the host's YORIK_PORT default —
    # without this, tenant uvicorns probe port 8000 (where the host
    # binds), see it in use, refuse to start, and StartLimit eventually
    # gives up. The host has neither set, so it falls through to 8000.
    try:
        _bind_port = int(
            os.environ.get("HOMEOS_PORT") or os.environ.get("YORIK_PORT", "8000")
        )
    except ValueError:
        _bind_port = 8000
    # YORIK_SKIP_BIND_PROBE — opt-out for single-process uvicorn runs
    # (no --reload). In that mode uvicorn binds BEFORE the startup hook
    # fires, so the probe sees the port as "already in use" by the very
    # uvicorn that's running it and falsely refuses. The real protection
    # against the SIGABRT loop only matters for systemd respawns where
    # an orphan TTS subprocess inherited the listening FD — that path
    # uses --reload and runs the probe in the supervisor before bind.
    if not os.environ.get("YORIK_SKIP_BIND_PROBE") \
            and _port_already_bound(_bind_host, _bind_port):
        logging.getLogger("yorik.startup").error(
            "port %s:%d already in use — refusing to start (would race "
            "uvicorn bind and crash on shutdown). Usually means an "
            "orphan from a previous `pkill -KILL` is still holding it. "
            "Recover with `sudo systemctl stop yorik && sleep 5 && "
            "sudo systemctl start yorik`; reboot if that doesn't free it. "
            "If you're running `uvicorn` directly without --reload "
            "(workstation manual launch), set YORIK_SKIP_BIND_PROBE=1.",
            _bind_host, _bind_port,
        )
        os._exit(1)

    # Storage gate — if a relocated subtree's symlink is dangling
    # (external SSD unplugged), refuse to boot loud + clear. Photos +
    # Documents writes would otherwise silently land on the internal
    # disk under the SSD's mount point, hiding the data behind the
    # mount when the SSD comes back. Q2 policy: fail loudly.
    from . import storage as _storage
    _storage.assert_storage_ready()

    init_db(DB_PATH)
    seed(DB_PATH)
    # Phase F-lite host: stamp data/internal_token so tenant Yoriks have
    # a stable secret to authenticate against /api/internal/provision.
    # Only the host generates; tenants read this file via their manifest
    # (YORIK_HOST_INTERNAL_TOKEN_FILE). No-op when this Yorik is itself
    # a tenant — the host's token file is already in place upstream.
    try:
        from . import external_users as _eu
        if not _eu._is_tenant_mode():
            _eu.get_or_create_internal_token()
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("yorik.startup").warning(
            "internal_token bootstrap skipped: %s", exc,
        )
    # Ensure documents.db schema is current — adds owner_user_id column
    # to documents on multi-user-wave-2-aware boxes. Idempotent.
    try:
        from .database import init_docs_db
        init_docs_db()
    except Exception as e:
        # NB: do NOT add `import logging` here — module-level logging
        # (imported at file top) is already in scope, and a function-
        # local rebinding here would shadow it, breaking the bind-probe
        # error logger at line ~1873 with UnboundLocalError.
        logging.getLogger("yorik.startup").warning("docs db init skipped: %s", e)

    # Migrate connector credentials from SQLite when the operator flipped
    # YORIK_DB_BACKEND=postgres after install. install.sh bootstraps the
    # Immich + Paperless API keys into data/family.db (the default at
    # install time); without this, the next sign-up's auto-provision
    # skips with "admin key not configured" even though the keys are
    # on disk. No-op on SQLite installs, fresh Postgres installs, and
    # tenant Yoriks. Same Fernet key file is shared so the encrypted
    # blob copies over verbatim — plaintext never enters memory.
    try:
        from . import credential_store as _cs
        result = _cs.migrate_from_sqlite_if_needed()
        if result:
            migrated = sorted(k for k, v in result.items() if v == "migrated")
            if migrated:
                logging.getLogger("yorik.startup").info(
                    "credential_store: auto-migrated from SQLite: %s", ", ".join(migrated),
                )
    except Exception as e:
        logging.getLogger("yorik.startup").warning("credential migration skipped: %s", e)
    # Wave 6b: scan apps/ source dir for community-installed apps and load
    # each. Builtin apps (calendar/chat/docs) registered themselves on
    # backend.apps import. Errors on individual apps are logged + skipped
    # so one bad app doesn't break the box.
    apps_mod.load_community_apps()
    # Extensions framework: scan extensions/ for optional add-ons (ZUGFeRD,
    # Swiss QR-Bill, regional tax integrations, etc.). Each extension's
    # Python deps are checked first; if installed, its module is imported
    # and its top-level register_hook() calls wire it into the platform.
    # Missing deps are surfaced via /api/extensions so the user can install
    # them from Settings → Extensions.
    from . import extensions as ext_mod
    ext_mod.load_all()
    # Skills registry — walks backend/skills/*/ and registers each one
    # by its skill.md manifest. Single source of truth for "what can
    # Yorik do?" — HTTP endpoints, the Vanna agent, and the future
    # Settings → Skills panel all read from this registry.
    from . import skills as skills_mod
    skills_mod.load_all()
    # WhatsApp: start the long-lived WS subscriber that ingests messages
    # from the Baileys bridge into wa_messages. No-op if the bridge isn't
    # up — the subscriber retries with backoff and the REST routes just
    # surface "not_connected" until the bridge is reachable.
    from . import whatsapp as wa_mod
    import asyncio as _aio
    wa_mod.start_background(_aio.get_event_loop())
    # Email: one supervisor task that spawns per-account IMAP fetchers.
    # Each account-task does its own reconnect / IDLE handling.
    from . import email_fetcher as ef_mod
    ef_mod.start_background(_aio.get_event_loop())
    # Paperless reconciler — diffs Paperless live ids vs the local
    # chunk mirror and ingests anything missing. Runs once at startup
    # (catches docs added while Yorik was down, webhook misses, or
    # post-restore gaps) and then every RECONCILE_INTERVAL_S thereafter.
    # Heartbeats into the workers registry so the home screen shows
    # silent failures (Paperless unreachable, embedder down, etc.).
    # Paperless visibility tags — idempotent seed of the
    # 'business'/'shared' tags + matching groups so the upload path
    # can attach them without round-tripping every time.
    try:
        from . import paperless_visibility as _pv
        _pv.ensure_tags()
    except Exception as exc:  # noqa: BLE001
        import logging as _lg
        _lg.getLogger("yorik.startup").debug("paperless_visibility seed skipped: %s", exc)
    from . import paperless_ingest as _paperless_recon
    _aio.get_event_loop().create_task(
        _paperless_recon.background_reconciler(),
        name="paperless-reconciler",
    )
    # Contact extraction is admin-triggered (a button in the Contacts
    # app calls /api/contacts/extractions/run) — see
    # backend/contact_extractor.py. No startup task: the full-corpus
    # scan is multi-hour LLM work and shouldn't run silently on every
    # install. The admin pays the cost when they want the queue
    # populated.
    # Backfill category for pre-existing email_messages rows so badges
    # appear immediately for old mail too. Runs in a thread so a large
    # inbox doesn't delay startup.
    import threading as _threading_ec
    def _backfill_categories():
        try:
            from . import email_classifier as ec_mod
            ec_mod.backfill_all()
        except Exception as exc:  # noqa: BLE001
            import logging as _log
            _log.getLogger("homeos.email_classifier").warning("backfill failed: %s", exc)
    _threading_ec.Thread(target=_backfill_categories, daemon=True, name="email-category-backfill").start()
    # Backup scheduler — wakes every 30s, runs the configured backup
    # at the configured HH:MM. No-op until the user configures it.
    from . import backup as bk_mod
    bk_mod.start_scheduler(_aio.get_event_loop())
    # Briefing snapshot scheduler — wakes every minute, fires at 03:00
    # local to capture yesterday's day-recap into briefing_snapshots
    # so the date navigator on /r/briefing can walk back in time.
    from . import briefing_snapshots as _bs_mod
    _bs_mod.start_scheduler(_aio.get_event_loop())
    # Voice acks: pre-synthesize the "klar Moment / on it / ..." pool
    # so the streaming voice endpoint can emit an instant audio reply
    # the moment STT finishes (masking LLM latency). Run in a thread
    # so the FastAPI startup doesn't block for 30s on the synthesis.
    import threading
    from . import voice_acks
    threading.Thread(
        target=voice_acks.warmup, daemon=True, name="voice-acks-warmup",
    ).start()
    # Pre-load Whisper so the FIRST voice turn after restart doesn't
    # pay the ~5-10s model-load tax. Same background-thread pattern
    # as ack warmup. Loads whichever HOMEOS_WHISPER_MODEL is configured.
    # Skipped when STT backend is a cloud endpoint — Whisper is then
    # only the fallback path and we'd rather not pay the GPU/disk cost
    # until it's actually needed.
    def _warm_whisper():
        try:
            from . import voice as _voice
            import logging as _log
            if _voice.STT_BACKEND != "whisper":
                _log.getLogger("homeos.voice").info(
                    "STT backend=%s — skipping Whisper preload (kept as fallback)",
                    _voice.STT_BACKEND,
                )
                return
            _voice._model()  # triggers the lazy load + caches it
            _log.getLogger("homeos.voice").info(
                "Whisper '%s' pre-loaded at boot", _voice.WHISPER_MODEL_NAME,
            )
        except Exception as exc:  # noqa: BLE001
            import logging as _log
            _log.getLogger("homeos.voice").warning("Whisper preload failed: %s", exc)
    threading.Thread(target=_warm_whisper, daemon=True, name="whisper-preload").start()

    # Phase B.6: drift detector — periodic reconcile of Paperless +
    # Immich state against space_members. Catches direct edits in the
    # bundled-service UIs that bypass Yorik. Non-fatal if connectors
    # aren't configured (warn-and-skip inside each provisioning module).
    try:
        from . import drift_detector as _drift
        _drift.start_worker()
    except Exception as exc:  # noqa: BLE001
        import logging as _log
        _log.getLogger("yorik.startup").warning("drift worker not started: %s", exc)


@app.on_event("shutdown")
async def _shutdown() -> None:
    from . import whatsapp as wa_mod
    await wa_mod.stop_background()
    from . import email_fetcher as ef_mod
    await ef_mod.stop_background()
    from . import backup as bk_mod
    await bk_mod.stop_scheduler()
    from . import briefing_snapshots as _bs_mod
    await _bs_mod.stop_scheduler()
    from . import drift_detector as _drift
    _drift.stop_worker()
    # Paperless reconciler: cancel by task name (set on create_task in
    # _startup). Without this, the 6h-sleep loop survives shutdown and
    # can race with destructors of native TTS/Whisper threads being torn
    # down by Python's interpreter exit — the trigger for the SIGABRT
    # crash loop observed 2026-06-07 → 2026-06-10.
    for _t in asyncio.all_tasks():
        if _t.get_name() == "paperless-reconciler" and not _t.done():
            _t.cancel()
            try:
                await _t
            except (asyncio.CancelledError, Exception):
                pass


# ---------------------------------------------------------------------------
# Pydantic models for POST bodies
# ---------------------------------------------------------------------------

class EventIn(BaseModel):
    title: str
    starts_at: str
    ends_at: Optional[str] = None
    all_day: bool = False
    color: Optional[str] = None
    person: Optional[str] = None
    notes: Optional[str] = None
    recurring: Optional[str] = None
    # Calendar overlay model — see backend/calendars.py.
    # Omit `calendar_id` to auto-route: solo events → creator's Personal,
    # multi-user → Shared. `attendee_user_ids` powers invitations + the
    # auto-route decision; `attendee_names` is for kids without logins.
    calendar_id:        Optional[int] = None
    # Phase E migrated user_profiles.id from BIGSERIAL to UUID. The body
    # accepts strings now; downstream code already handles both via the
    # shim. Pre-Phase-E was List[int] which 422'd every UUID attendee.
    attendee_user_ids:  Optional[List[str]] = None
    attendee_names:     Optional[List[str]] = None
    visibility:         str = "default"  # 'default' | 'private'
    # Travel-time integration (migration 019). When set, the backend
    # geocodes the address + computes driving time from the user's home
    # via the maps connector, cached on the event row. The calendar
    # event card renders a travel-time badge from these columns.
    location:           Optional[str] = None
    # Colour category (migration 026). One of family|business|drive|
    # health|personal|social — frontend maps to a subtle palette.
    category:           Optional[str] = None


class TaskIn(BaseModel):
    title: str
    due_date: Optional[str] = None
    person: Optional[str] = None
    category: Optional[str] = None
    notes: Optional[str] = None
    # Priority 0/1/2 = low/normal/high. estimated_minutes is the user's
    # guess for how long the task will take; both are optional and only
    # shown in the UI when set.
    priority: int = 1
    estimated_minutes: Optional[int] = None
    # Wave: real-user assignees. Pass user_ids; "everyone" expands to
    # all enabled users (resolved server-side). If both `person` (legacy
    # role label) and assignee_user_ids are passed, assignees wins.
    assignee_user_ids: Optional[List[int]] = None
    assign_everyone: bool = False
    # Subtask / recurring (migration 023).
    parent_task_id: Optional[int] = None
    recurrence_rule: Optional[str] = None


class EventPatch(BaseModel):
    title: Optional[str] = None
    starts_at: Optional[str] = None
    ends_at: Optional[str] = None
    all_day: Optional[bool] = None
    color: Optional[str] = None
    person: Optional[str] = None
    notes: Optional[str] = None
    recurring: Optional[str] = None
    calendar_id: Optional[int] = None
    visibility:  Optional[str] = None
    location:    Optional[str] = None
    category:    Optional[str] = None


class TaskPatch(BaseModel):
    title: Optional[str] = None
    due_date: Optional[str] = None
    done: Optional[bool] = None
    person: Optional[str] = None
    category: Optional[str] = None
    notes: Optional[str] = None
    priority: Optional[int] = None
    estimated_minutes: Optional[int] = None
    # When provided, replaces the assignee set entirely. Omit to leave
    # assignees unchanged. Pass [] to clear.
    assignee_user_ids: Optional[List[int]] = None
    assign_everyone: Optional[bool] = None
    # Subtask / recurring (migration 023). Pass empty string to clear.
    parent_task_id: Optional[int] = None
    recurrence_rule: Optional[str] = None


class AskIn(BaseModel):
    message: str = Field(min_length=1)
    role: str = "admin"
    conversation_id: Optional[str] = None
    # When True, force the LLM to make a tool call on iteration 1.
    # The Compose inline chat sets this because every message there
    # is an action request ("schreib Hans dass..."); without it the
    # LLM sometimes narrates intent ("Ich muss Hans finden") and the
    # loop ends with no work done. Defaults to False so non-Compose
    # paths (main chat / voice) stay unchanged.
    require_tool_call: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rows_to_dicts(rows) -> List[Dict[str, Any]]:
    return [dict(r) for r in rows]


_llm_probe_cache: Dict[str, Any] = {"checked_at": 0.0, "ok": False, "reason": ""}
_LLM_PROBE_TTL_S = 3.0


def _llm_probe(*, force: bool = False) -> Dict[str, Any]:
    """Cached LLM reachability probe. Hits `${LLM_BASE_URL}/models` with a
    300ms timeout, caches the result for ~3s so the chat / Compose paths
    can short-circuit gracefully without each call paying the probe cost.
    Returns {ok, model, base_url, reason}.

    Reads base_url + model dynamically from vanna_agent so live-reload
    via PATCH /api/llm/config actually takes effect without a restart.
    """
    import time
    base_url = vanna_agent.LLM_BASE_URL
    model    = vanna_agent.LLM_MODEL
    now = time.monotonic()
    if not force and (now - _llm_probe_cache["checked_at"]) < _LLM_PROBE_TTL_S:
        return {
            "ok":       _llm_probe_cache["ok"],
            "reason":   _llm_probe_cache["reason"],
            "model":    model,
            "base_url": base_url,
        }
    ok = False
    reason = ""
    try:
        r = requests.get(f"{base_url.rstrip('/')}/models", timeout=0.3)
        if not r.ok:
            reason = f"HTTP {r.status_code} from /models"
        else:
            ids = {m.get("id") for m in (r.json().get("data") or [])}
            # Ollama exposes Modelfile-created models with an implicit
            # `:latest` tag in /v1/models. Match flexibly so HOMEOS_MODEL
            # configured as either "foo" or "foo:latest" works against
            # served IDs in either form. Same workaround would apply
            # if a user pinned a specific tag like "foo:v2" — that one
            # must match exactly though, which is what they'd want.
            served = lambda m: (
                m in ids
                or f"{m}:latest" in ids
                or (m.endswith(":latest") and m.rsplit(":", 1)[0] in ids)
            )
            if not served(model):
                reason = f"endpoint reachable, but model {model!r} not served (found: {sorted(i for i in ids if i)[:3]})"
            else:
                ok = True
    except requests.exceptions.ConnectTimeout:
        reason = "connect timeout"
    except requests.exceptions.ConnectionError:
        reason = "connection refused"
    except requests.RequestException as exc:
        reason = f"{type(exc).__name__}: {exc}"
    _llm_probe_cache["checked_at"] = now
    _llm_probe_cache["ok"] = ok
    _llm_probe_cache["reason"] = reason
    return {"ok": ok, "reason": reason, "model": model, "base_url": base_url}


def _llm_reachable() -> bool:
    return _llm_probe()["ok"]


def _llm_offline_response(message: str, conversation_id: Optional[str] = None) -> Dict[str, Any]:
    """Friendly user-facing payload when the LLM is unreachable. Same shape
    as a successful /api/ask response so the frontend doesn't need special
    handling beyond the `degraded` flag."""
    probe = _llm_probe()
    return {
        "response": (
            "I can't reach the language model right now — the local LLM "
            f"endpoint at {vanna_agent.LLM_BASE_URL} isn't responding ({probe['reason']}). "
            "Start the llama-swap (or whichever backend you use) and try again. "
            "Your message wasn't lost."
        ),
        "sql_used":      None,
        "rows_preview": None,
        "ui_actions":   [],
        "conversation_id": conversation_id,
        "degraded":     True,
        "llm_status":   probe,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health() -> Dict[str, Any]:
    # Cheap reachable probes for the bundled / BYO services. Each is
    # cached per its own module so calling /api/health from multiple
    # UI cards doesn't multiply HTTP load. Frontend uses these to swap
    # in degraded UI ("Immich unreachable — start the container") before
    # the user clicks into a tab that would otherwise show a blank panel.
    try:
        from .connectors.immich import immich_reachable as _immich_reachable
        immich_up = _immich_reachable()
    except Exception:  # noqa: BLE001
        immich_up = False
    return {
        "status": "ok",
        "model": vanna_agent.LLM_MODEL,
        "base_url": vanna_agent.LLM_BASE_URL,
        "db_path": DB_PATH,
        "llm_reachable": _llm_reachable(),
        "immich_reachable": immich_up,
        # User-visible Immich URL — the photos iframe consumes this.
        # Empty when YORIK_IMMICH_PUBLIC_URL isn't set; the frontend
        # then derives a sensible default from the current hostname.
        "immich_public_url": (os.getenv("YORIK_IMMICH_PUBLIC_URL") or "").rstrip("/"),
        "voice_max_seconds": int(os.getenv("HOMEOS_VOICE_MAX_SECONDS", "60")),
        "default_language": DEFAULT_LANGUAGE,
    }


# ── Recurring-event expansion ────────────────────────────────────────
# `events.recurring` stores a short code; we materialise virtual
# instances inside the visible window so the grid renders them.
#
# Supported codes:
#   daily          — every day
#   weekly         — every 7 days, same weekday as starts_at
#   weekdays       — shorthand for {Mon, Tue, Wed, Thu, Fri}
#   weekdays:1,3,5 — explicit ISO weekday set (1=Mon ... 7=Sun)
#   monthly        — same day-of-month each month (clamped to last day)
#   yearly         — same month/day each year (Feb 29 → Feb 28 on non-leap)
#
# v1: editing a recurring event edits the whole series. Per-instance
# skips/moves would need an `event_exceptions` side-table.

def _parse_weekdays_spec(rec: str) -> Optional[set]:
    if rec == "weekdays":
        return {1, 2, 3, 4, 5}
    if rec.startswith("weekdays:"):
        s: set = set()
        for p in rec.split(":", 1)[1].split(","):
            try:
                n = int(p.strip())
            except ValueError:
                continue
            if 1 <= n <= 7:
                s.add(n)
        return s or None
    return None


def _add_months(dt: "datetime", n: int) -> "datetime":
    import calendar as _cal
    m = dt.month - 1 + n
    y = dt.year + m // 12
    m = m % 12 + 1
    d = min(dt.day, _cal.monthrange(y, m)[1])
    return dt.replace(year=y, month=m, day=d)


def _expand_recurring(
    rows: List[Dict[str, Any]],
    window_start_iso: str,
    window_end_iso: str,
) -> List[Dict[str, Any]]:
    """Materialise virtual occurrences in [window_start, window_end).

    The original-date occurrence is NOT emitted — combine with the base
    SELECT result. Each instance carries `occurrence_date` so React can
    build stable keys like `<id>_<occurrence_date>`.
    """
    from datetime import timedelta as _td
    try:
        ws = datetime.strptime(window_start_iso[:10], "%Y-%m-%d")
        we = datetime.strptime(window_end_iso[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return []

    out: List[Dict[str, Any]] = []
    for r in rows:
        rec = (r.get("recurring") or "").strip().lower()
        if not rec:
            continue
        starts_at = r.get("starts_at") or ""
        if len(starts_at) < 10:
            continue
        try:
            base_start = datetime.strptime(starts_at[:10], "%Y-%m-%d")
        except ValueError:
            continue
        start_tail = starts_at[10:] or "T00:00:00"
        ends_at = r.get("ends_at") or ""
        end_tail = ends_at[10:] if len(ends_at) >= 10 else ""
        end_day_offset = 0
        if len(ends_at) >= 10:
            try:
                end_day_offset = (
                    datetime.strptime(ends_at[:10], "%Y-%m-%d") - base_start
                ).days
            except ValueError:
                end_day_offset = 0

        wd_set = _parse_weekdays_spec(rec)
        dates: List[datetime] = []
        if rec == "daily":
            d = max(base_start + _td(days=1), ws)
            while d < we:
                dates.append(d)
                d += _td(days=1)
        elif rec == "weekly":
            d = base_start + _td(days=7)
            if d < ws:
                gap = (ws - d).days
                d += _td(days=((gap + 6) // 7) * 7)
            while d < we:
                dates.append(d)
                d += _td(days=7)
        elif wd_set is not None:
            d = max(base_start + _td(days=1), ws)
            while d < we:
                if d.isoweekday() in wd_set:
                    dates.append(d)
                d += _td(days=1)
        elif rec == "monthly":
            # Always derive from base_start so a Jan-31 series stays
            # on the 31st (clamped to the last day in shorter months)
            # instead of drifting down to 28 after February.
            n = 1
            while True:
                d = _add_months(base_start, n)
                if d >= we:
                    break
                if d >= ws:
                    dates.append(d)
                n += 1
        elif rec == "yearly":
            n = 1
            while True:
                try:
                    d = base_start.replace(year=base_start.year + n)
                except ValueError:
                    d = base_start.replace(year=base_start.year + n, day=28)
                if d >= we:
                    break
                if d >= ws:
                    dates.append(d)
                n += 1
        else:
            continue

        for od in dates:
            inst = dict(r)
            iso = od.strftime("%Y-%m-%d")
            inst["starts_at"] = iso + start_tail
            if end_tail:
                end_d = od + _td(days=end_day_offset)
                inst["ends_at"] = end_d.strftime("%Y-%m-%d") + end_tail
            inst["occurrence_date"] = iso
            inst["is_recurring_instance"] = True
            out.append(inst)
    return out


@app.get("/api/events")
def list_events(
    role: str = Depends(_auth.current_role),
    user: dict[str, Any] = Depends(_auth.current_user_optional),
    month: Optional[int] = Query(None, ge=1, le=12),
    year: Optional[int] = Query(None, ge=2000, le=2100),
    start_date: Optional[str] = Query(None, description="ISO date (YYYY-MM-DD), inclusive. Takes precedence over month/year."),
    end_date: Optional[str] = Query(None, description="ISO date (YYYY-MM-DD), exclusive."),
    calendar_ids: Optional[str] = Query(None, description="Comma-separated calendar IDs to filter to. Omit to include every calendar the user can see."),
) -> List[Dict[str, Any]]:
    require_role(role, "events")

    # Resolve the window once — reused by the recurring-expansion select.
    window_start: Optional[str] = None
    window_end: Optional[str] = None
    if start_date and end_date:
        window_start, window_end = start_date, end_date
    elif month is not None and year is not None:
        window_start = f"{year:04d}-{month:02d}-01"
        nm = month + 1
        ny = year
        if nm == 13:
            nm = 1
            ny += 1
        window_end = f"{ny:04d}-{nm:02d}-01"

    cal_ids: List[int] = []
    if calendar_ids:
        # lstrip('-') so the frontend's "match nothing" sentinel
        # (calendar_ids=-1, sent when every calendar is hidden)
        # parses through and produces an impossible WHERE clause
        # instead of silently degrading to "no filter → all events".
        cal_ids = [int(x) for x in calendar_ids.split(",") if x.strip().lstrip("-").isdigit()]

    acl_frag, acl_params = "", []
    if user and user.get("id"):
        from . import calendars as _cal
        acl_frag, acl_params = _cal.visible_event_filter(user["id"], role)

    base = "SELECT * FROM events"
    params_list: list = []
    where: List[str] = []
    if window_start and window_end:
        where.append("starts_at >= ? AND starts_at < ?")
        params_list.extend([window_start, window_end])
    if cal_ids:
        placeholders = ",".join("?" * len(cal_ids))
        where.append(f"calendar_id IN ({placeholders})")
        params_list.extend(cal_ids)
    if acl_frag:
        where.append(acl_frag)
        params_list.extend(acl_params)
    if where:
        base += " WHERE " + " AND ".join(where)
    base, params_tuple = apply_filter(role, "events", base, tuple(params_list))
    base += " ORDER BY starts_at ASC"
    with conn_ctx(DB_PATH) as conn:
        rows = conn.execute(base, params_tuple).fetchall()
    out = _rows_to_dicts(rows)

    # Per-event privacy downgrade (free_busy share or visibility=private
    # for non-owners).
    cal_cache: dict = {}
    def _cal_for(cid: int) -> Optional[Dict[str, Any]]:
        from . import calendars as _cal
        if cid not in cal_cache:
            cal_cache[cid] = _cal.get(cid) or {}
        return cal_cache.get(cid) or None

    uid = user["id"] if (user and user.get("id")) else None
    if uid is not None:
        from . import calendars as _cal
        out = [
            _cal.downgrade_for_privacy(ev, uid, role, _cal_for(ev["calendar_id"]))
            if ev.get("calendar_id") else ev
            for ev in out
        ]

    # Recurring expansion — pulls in series whose base date is BEFORE
    # the window but which have instances inside it.
    if window_start and window_end:
        where2: List[str] = ["recurring IS NOT NULL", "recurring <> ''", "starts_at < ?"]
        params2: list = [window_start]
        if cal_ids:
            placeholders = ",".join("?" * len(cal_ids))
            where2.append(f"calendar_id IN ({placeholders})")
            params2.extend(cal_ids)
        if acl_frag:
            where2.append(acl_frag)
            params2.extend(acl_params)
        base2 = "SELECT * FROM events WHERE " + " AND ".join(where2)
        base2, params_tuple2 = apply_filter(role, "events", base2, tuple(params2))
        with conn_ctx(DB_PATH) as conn:
            rows2 = conn.execute(base2, params_tuple2).fetchall()
        extra = _rows_to_dicts(rows2)
        if uid is not None:
            from . import calendars as _cal
            extra = [
                _cal.downgrade_for_privacy(ev, uid, role, _cal_for(ev["calendar_id"]))
                if ev.get("calendar_id") else ev for ev in extra
            ]
        all_recurring = extra + [r for r in out if (r.get("recurring") or "").strip()]
        out.extend(_expand_recurring(all_recurring, window_start, window_end))
        out.sort(key=lambda e: e.get("starts_at") or "")

    return out


# ─────────────── events/parse-natural — one-line LLM capture ───────────────
# Drives the ⌘K quick-add overlay in the Calendar app. The user types
# something like "Lunch with Hans at Tartine tomorrow 12:30 for 90 min"
# and we ask the LLM to extract a structured event. The UI then opens
# the regular EventDialog pre-filled for the user to review.
#
# IMPORTANT: must register BEFORE /api/events/{event_id} below —
# FastAPI matches routes in registration order, so a /api/events/parse-
# natural URL would otherwise get caught by the {event_id} path-param
# and fail with "invalid integer".

class EventParseIn(BaseModel):
    text: str


@app.post("/api/events/parse-natural")
async def events_parse_natural(
    body: EventParseIn,
    role: str = Depends(_auth.current_role),
) -> Dict[str, Any]:
    """Parse free-form text into event fields. Returns the structured
    fields; missing values are simply omitted. Always returns at
    minimum ``{title: <input>}`` so the UI can fall back gracefully
    when the LLM is unreachable."""
    require_role(role, "events")
    text = (body.text or "").strip()
    if not text:
        return {"title": "", "_warnings": ["empty input"]}
    fallback: Dict[str, Any] = {"title": text}

    from datetime import date as _date
    today_iso = _date.today().isoformat()

    system_msg = (
        "You convert free-form event text into a strict JSON object. "
        f"Today is {today_iso}. The JSON keys you may emit:\n"
        '  - "title" (string, required): the cleaned event title, '
        "no time/date phrases.\n"
        '  - "date" (YYYY-MM-DD): resolve "morgen", "tomorrow", '
        '"nächsten Montag" relative to today. Default to today only '
        "if a specific time is present without a date.\n"
        '  - "start_time" (HH:MM, 24h): e.g. "14:00". Omit when '
        "all-day.\n"
        '  - "end_time" (HH:MM, 24h): when explicitly given.\n'
        '  - "duration_minutes" (int): when the user said "for 45 '
        'min", "1h 30m", "an hour". Use this OR end_time, not both.\n'
        '  - "all_day" (boolean, default false): true for things '
        'like "Birthday on Friday".\n'
        '  - "location" (string): "Tartine", "Hannover Hauptbahnhof", '
        '"Bahnhofstr. 18". Strip "at"/"in" prefixes.\n'
        '  - "attendee_names" (array of strings): people mentioned by '
        'name ("Lunch with Hans" → ["Hans"]).\n\n'
        "Output ONLY the JSON object — no markdown fences, no "
        "commentary. Omit keys you cannot extract — do NOT invent."
    )
    user_msg = f'Event text: """{text}"""\n\nJSON:'

    from . import ask as _ask
    await _ask._ensure_agent_singletons()
    llm = getattr(_ask._ask_own_backend, "_llm", None)
    if llm is None:
        return {**fallback, "_warnings": ["LLM unavailable — only title set"]}

    try:
        import asyncio as _asyncio
        resp = await _asyncio.to_thread(
            llm.chat,
            [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            None,
            max_tokens=400,
            temperature=0.0,
        )
    except Exception as exc:  # noqa: BLE001
        return {**fallback, "_warnings": [f"LLM error: {type(exc).__name__}"]}

    raw = ((resp or {}).get("content") or "").strip()
    import json as _json, re as _re
    parsed: Dict[str, Any] = {}
    if raw.startswith("```"):
        raw = _re.sub(r"^```(?:json)?\s*", "", raw)
        raw = _re.sub(r"\s*```$", "", raw)
    try:
        parsed = _json.loads(raw)
        if not isinstance(parsed, dict):
            parsed = {}
    except ValueError:
        m = _re.search(r"\{.*\}", raw, _re.DOTALL)
        if m:
            try:
                parsed = _json.loads(m.group(0))
                if not isinstance(parsed, dict):
                    parsed = {}
            except ValueError:
                pass

    out: Dict[str, Any] = {"title": (parsed.get("title") or "").strip() or text}
    if isinstance(parsed.get("date"), str) and \
       _re.fullmatch(r"\d{4}-\d{2}-\d{2}", parsed["date"].strip()):
        out["date"] = parsed["date"].strip()
    if isinstance(parsed.get("start_time"), str) and \
       _re.fullmatch(r"\d{2}:\d{2}", parsed["start_time"].strip()):
        out["start_time"] = parsed["start_time"].strip()
    if isinstance(parsed.get("end_time"), str) and \
       _re.fullmatch(r"\d{2}:\d{2}", parsed["end_time"].strip()):
        out["end_time"] = parsed["end_time"].strip()
    if isinstance(parsed.get("duration_minutes"), int) and parsed["duration_minutes"] > 0:
        out["duration_minutes"] = parsed["duration_minutes"]
    if isinstance(parsed.get("all_day"), bool):
        out["all_day"] = parsed["all_day"]
    if isinstance(parsed.get("location"), str) and parsed["location"].strip():
        out["location"] = parsed["location"].strip()
    if isinstance(parsed.get("attendee_names"), list):
        names = [str(n).strip() for n in parsed["attendee_names"] if str(n).strip()]
        if names:
            out["attendee_names"] = names
    return out


# ─────────────── events/search — top-bar event finder ───────────────
# Hits title, location, notes, person. Role-gated + calendar-visible
# filtered (same ACL as /api/events). Used by the / search popover.
#
# IMPORTANT: same registration-order constraint as parse-natural above.

@app.get("/api/events/search")
def search_events(
    q: str = Query("", description="Substring across title/location/notes/person."),
    limit: int = Query(20, ge=1, le=80),
    role: str = Depends(_auth.current_role),
    user: dict[str, Any] = Depends(_auth.current_user_optional),
) -> List[Dict[str, Any]]:
    require_role(role, "events")
    q = (q or "").strip()
    if not q:
        return []
    like = f"%{q}%"
    base = ("SELECT id, title, starts_at, ends_at, all_day, location, "
            "       calendar_id "
            "FROM events "
            "WHERE (title LIKE ? OR location LIKE ? OR notes LIKE ? "
            "       OR person LIKE ?)")
    params: list = [like, like, like, like]

    # Layer the calendar ACL on top so a user only finds events they
    # can actually see (matches list_events behaviour).
    if user and user.get("id"):
        from . import calendars as _cal
        frag, p = _cal.visible_event_filter(user["id"], role)
        base += " AND " + frag
        params.extend(p)

    base, params_tuple = apply_filter(role, "events", base, tuple(params))
    # Future events first (most useful), then most-recent past.
    # julianday() is SQLite-only; on Postgres we can't compute "distance
    # from now" in pure SQL portably. Compare against an ISO 8601
    # parameter — both starts_at (TEXT) and the param string sort the
    # same way chronologically and lexicographically. The "upcoming-most
    # first" ordering used to come from ABS(julianday(...)); here we
    # get it for free by sorting starts_at ASC inside the future bucket.
    from datetime import datetime as _dt, timezone as _tz
    _now_iso = _dt.now(_tz.utc).isoformat(timespec="seconds")
    base += (" ORDER BY CASE WHEN starts_at >= ? THEN 0 ELSE 1 END, "
             "         CASE WHEN starts_at >= ? THEN starts_at END ASC, "
             "         starts_at DESC "
             "LIMIT ?")
    params_tuple = params_tuple + (_now_iso, _now_iso, limit)
    with conn_ctx(DB_PATH) as conn:
        rows = conn.execute(base, params_tuple).fetchall()
    return _rows_to_dicts(rows)


@app.get("/api/events/{event_id}")
def get_event(
    event_id: int,
    role: str = Depends(_auth.current_role),
    user: dict[str, Any] = Depends(_auth.current_user_optional),
) -> Dict[str, Any]:
    """Single-event read. Used by the calendar deep-link
    `/r/calendar?event=N` from invitation notifications. Respects the
    calendar ACL and per-event privacy rules."""
    require_role(role, "events")
    with conn_ctx(DB_PATH) as conn:
        row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if not row:
        raise HTTPException(404, "event not found")
    ev = dict(row)
    if user and user.get("id"):
        from . import calendars as _cal
        if ev.get("calendar_id"):
            cal = _cal.get(int(ev["calendar_id"]))
            if cal and not _cal.can_access(user["id"], role, cal, "free_busy"):
                raise HTTPException(404, "event not found")
            ev = _cal.downgrade_for_privacy(ev, user["id"], role, cal)
        elif role not in ("platform_admin", "admin"):
            # Orphan event (no calendar_id) — backfilled to admin in the
            # 2026-06-02 migration. If any new orphan slips through, gate
            # by owner so it doesn't become world-readable.
            owner = ev.get("owner_user_id")
            # Phase E: user ids are UUID strings — compare as strings.
            if owner is not None and str(owner) != str(user["id"]):
                raise HTTPException(404, "event not found")
    return ev


@app.post("/api/events", status_code=201)
def create_event(
    event: EventIn,
    role: str = Depends(_auth.current_role),
    user: dict[str, Any] = Depends(_auth.current_user_optional),
) -> Dict[str, Any]:
    require_role(role, "events")
    require_write(role)

    from . import calendars as _cal
    creator_id = user["id"] if user and user.get("id") else None

    # Resolve target calendar — honor explicit pick, else auto-route.
    target_cal_id = event.calendar_id
    if not target_cal_id and creator_id is not None:
        target_cal_id = _cal.auto_route_calendar(
            creator_id, event.attendee_user_ids or [],
        )

    # Write-check: the creator needs `write` on the chosen calendar.
    # Skip when there's no authenticated user (legacy/no-auth caller —
    # falls back to the existing role gate).
    if target_cal_id and creator_id is not None:
        cal_obj = _cal.get(int(target_cal_id))
        if not cal_obj:
            raise HTTPException(404, f"calendar id={target_cal_id} not found")
        if not _cal.can_access(creator_id, role, cal_obj, "write"):
            raise HTTPException(403, f"no write access on calendar '{cal_obj['name']}'")

    visibility = event.visibility if event.visibility in ("default", "private") else "default"

    # Travel-time computation when a location is set. Best-effort: any
    # failure leaves the travel columns NULL (the UI handles that fine).
    loc_norm = (event.location or "").strip() or None
    loc_lat = loc_lon = travel_s = travel_m = None
    travel_provider = travel_computed_at = None
    if loc_norm and creator_id is not None:
        try:
            from .connectors.maps import maps as _maps_call
            from .skills.calculate_travel_time.skill import _user_home_address_variants
            geo = _maps_call("geocode", query=loc_norm)
            if geo and geo.get("ok") is not False and geo.get("lat") is not None:
                loc_lat = float(geo["lat"]); loc_lon = float(geo["lon"])
                for candidate in _user_home_address_variants(creator_id):
                    route = _maps_call("directions", **{
                        "from": candidate,
                        "to":   f"{loc_lat},{loc_lon}",
                        "mode": "driving",
                    })
                    if route and route.get("ok") is not False:
                        travel_s = int(route.get("duration_s") or 0) or None
                        travel_m = int(route.get("distance_m") or 0) or None
                        travel_provider = route.get("provider")
                        travel_computed_at = datetime.now().isoformat(timespec="seconds")
                        break
        except Exception as exc:  # noqa: BLE001
            log.debug("event location geocode/route failed: %s", exc)

    with conn_ctx(DB_PATH) as conn:
        # Category — normalise via the shared enum module (accepts slug
        # or German synonym; unknown → NULL).
        from .event_categories import normalize_category as _norm_cat
        category_norm = _norm_cat(event.category)
        cur = conn.execute(
            "INSERT INTO events (title, starts_at, ends_at, all_day, color, person, "
            " notes, recurring, calendar_id, owner_user_id, visibility, "
            " location, location_lat, location_lon, "
            " travel_time_s, travel_distance_m, travel_provider, travel_computed_at, "
            " category) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.title, event.starts_at, event.ends_at, int(event.all_day),
                event.color, event.person, event.notes, event.recurring,
                target_cal_id, creator_id, visibility,
                loc_norm, loc_lat, loc_lon,
                travel_s, travel_m, travel_provider, travel_computed_at,
                category_norm,
            ),
        )
        new_id = int(cur.lastrowid)
        row = conn.execute("SELECT * FROM events WHERE id = ?", (new_id,)).fetchone()

    # Attach attendees + send notifications (best-effort; never block creation)
    if event.attendee_user_ids or event.attendee_names:
        _cal.add_attendees(
            new_id,
            user_ids=event.attendee_user_ids or [],
            person_names=event.attendee_names or [],
        )
        # Notify each invited user (skip the creator themselves).
        try:
            from . import notifications  # late import — avoids cycle
            for uid in (event.attendee_user_ids or []):
                if creator_id is not None and uid == creator_id:
                    continue
                notifications.create(
                    uid,
                    kind="event_invitation",
                    title=f"New invitation: {event.title}",
                    body=f"{event.starts_at} — RSVP required",
                    navigate_to=f"/r/calendar?event={new_id}",
                )
        except Exception as exc:  # noqa: BLE001
            log.debug("invite notify failed: %s", exc)

    return dict(row)


def _ensure_row_writable(
    table: str,
    row_id: int,
    role: str,
    user: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Load a row, require_role/require_write, then defer the per-row
    ACL to spaces.can_write_row.

    Phase B (2026-06-02) made spaces the authoritative gate: owner OR
    admin OR write-member-of-row's-space OR write-level row_shares.
    For events whose calendar.space_id resolves through the parent,
    can_write_row handles the lookup.

    `user` should come from _auth.current_user_optional. Without an
    authenticated user we keep the legacy require_role/require_write
    gates only — same as pre-Phase-A behaviour for unauthenticated
    callers (which shouldn't reach here in practice).
    """
    require_role(role, table)
    require_write(role)
    with conn_ctx(DB_PATH) as conn:
        row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"{table[:-1]} id={row_id} not found")
    r = dict(row)
    if user is None or user.get("id") is None or role == "platform_admin":
        return r
    from . import spaces as _sp
    if not _sp.can_write_row(user["id"], role, table, r):
        subject = table[:-1] if table.endswith("s") else table
        raise HTTPException(
            status_code=403,
            detail=(
                f"only the {subject}'s owner, a write-member of its space, "
                f"or someone explicitly shared with can change it."
            ),
        )
    return r


def _apply_patch(table: str, row_id: int, patch: Dict[str, Any]) -> Dict[str, Any]:
    fields = {k: v for k, v in patch.items() if v is not None}
    if not fields:
        with conn_ctx(DB_PATH) as conn:
            return dict(conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone())
    # Pydantic gives bool/int; sqlite stores int for booleans.
    for bool_col in ("all_day", "done"):
        if bool_col in fields:
            fields[bool_col] = int(bool(fields[bool_col]))
    cols = ", ".join(f"{k} = ?" for k in fields)
    params = list(fields.values()) + [row_id]
    with conn_ctx(DB_PATH) as conn:
        conn.execute(f"UPDATE {table} SET {cols} WHERE id = ?", params)
        row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    return dict(row)


@app.patch("/api/events/{event_id}")
def update_event(
    event_id: int,
    patch: EventPatch,
    role: str = Depends(_auth.current_role),
    user: dict[str, Any] = Depends(_auth.current_user_optional),
) -> Dict[str, Any]:
    _ensure_row_writable("events", event_id, role, user=user)
    fields = patch.model_dump(exclude_unset=True)

    # Category: normalise the slug (accepts German synonyms via the
    # shared enum module). Empty string clears the colour.
    if "category" in fields:
        raw = fields["category"]
        if raw is None or raw == "":
            fields["category"] = None
        else:
            from .event_categories import normalize_category as _norm_cat
            fields["category"] = _norm_cat(raw)

    # When `location` was patched, geocode + recompute travel time so
    # the cached columns stay in sync. Empty/null clears them all.
    if "location" in fields:
        loc_norm = (fields["location"] or "").strip() or None
        fields["location"] = loc_norm
        # Read current event to get the owner — we need their home address
        # to compute travel-from.
        with conn_ctx(DB_PATH) as conn:
            cur_row = conn.execute(
                "SELECT owner_user_id FROM events WHERE id = ?", (event_id,),
            ).fetchone()
        owner_id = (cur_row["owner_user_id"] if cur_row else None) or (
            user.get("id") if user else None
        )
        fields["location_lat"] = None
        fields["location_lon"] = None
        fields["travel_time_s"] = None
        fields["travel_distance_m"] = None
        fields["travel_provider"] = None
        fields["travel_computed_at"] = None
        if loc_norm and owner_id:
            try:
                from .connectors.maps import maps as _maps_call
                from .skills.calculate_travel_time.skill import _user_home_address_variants
                geo = _maps_call("geocode", query=loc_norm)
                if geo and geo.get("ok") is not False and geo.get("lat") is not None:
                    fields["location_lat"] = float(geo["lat"])
                    fields["location_lon"] = float(geo["lon"])
                    for candidate in _user_home_address_variants(owner_id):
                        route = _maps_call("directions", **{
                            "from": candidate,
                            "to":   f"{fields['location_lat']},{fields['location_lon']}",
                            "mode": "driving",
                        })
                        if route and route.get("ok") is not False:
                            fields["travel_time_s"] = int(route.get("duration_s") or 0) or None
                            fields["travel_distance_m"] = int(route.get("distance_m") or 0) or None
                            fields["travel_provider"] = route.get("provider")
                            fields["travel_computed_at"] = datetime.now().isoformat(timespec="seconds")
                            break
            except Exception as exc:  # noqa: BLE001
                log.debug("PATCH event location geocode/route failed: %s", exc)

    return _apply_patch("events", event_id, fields)


@app.delete("/api/events/{event_id}", status_code=204, response_class=Response)
def delete_event(
    event_id: int,
    role: str = Depends(_auth.current_role),
    user: dict[str, Any] = Depends(_auth.current_user_optional),
) -> Response:
    _ensure_row_writable("events", event_id, role, user=user)
    with conn_ctx(DB_PATH) as conn:
        conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
    return Response(status_code=204)


def _resolve_assignees(assignee_user_ids: Optional[List[int]],
                        assign_everyone: bool) -> List[int]:
    """Turn the API's assignee_user_ids + assign_everyone flag into a
    deduped list of real user ids. 'everyone' expands to every enabled
    user_profile.id."""
    if assign_everyone:
        with conn_ctx(DB_PATH) as conn:
            rows = conn.execute(
                "SELECT id FROM user_profiles WHERE disabled=0"
            ).fetchall()
        return sorted({r["id"] for r in rows})
    if assignee_user_ids is None:
        return []
    return sorted({int(x) for x in assignee_user_ids if x is not None})


def _replace_task_assignees(task_id: int, assignee_ids: List[int]) -> tuple[List[int], List[int]]:
    """Replace the assignee set for a task. Returns (added_ids, removed_ids)
    so the caller can decide who to notify."""
    with conn_ctx(DB_PATH) as conn:
        existing = {r["user_id"] for r in conn.execute(
            "SELECT user_id FROM task_assignees WHERE task_id=?", (task_id,),
        ).fetchall()}
        target = set(assignee_ids)
        added   = target - existing
        removed = existing - target
        for uid in removed:
            conn.execute("DELETE FROM task_assignees WHERE task_id=? AND user_id=?",
                         (task_id, uid))
        for uid in added:
            conn.execute(
                "INSERT OR IGNORE INTO task_assignees (task_id, user_id) VALUES (?, ?)",
                (task_id, uid),
            )
        conn.commit()
    return sorted(added), sorted(removed)


def _attach_assignees(rows: list[dict]) -> list[dict]:
    """Augment a list of task dicts with `assignees: [{id, name}]`.
    Single query for the whole batch so we don't N+1."""
    if not rows:
        return rows
    ids = [r["id"] for r in rows]
    placeholders = ",".join("?" * len(ids))
    with conn_ctx(DB_PATH) as conn:
        ar = conn.execute(
            f"SELECT a.task_id, a.user_id, a.status, u.name "
            f"FROM task_assignees a JOIN user_profiles u ON u.id = a.user_id "
            f"WHERE a.task_id IN ({placeholders})",
            ids,
        ).fetchall()
    by_task: Dict[int, List[Dict[str, Any]]] = {}
    for r in ar:
        by_task.setdefault(r["task_id"], []).append({
            "user_id": r["user_id"],
            "name":    r["name"],
            "status":  r["status"],
        })
    for row in rows:
        row["assignees"] = by_task.get(row["id"], [])
    return rows


@app.patch("/api/tasks/{task_id}")
def update_task(
    task_id: int,
    patch: TaskPatch,
    role: str = Depends(_auth.current_role),
    actor: Optional[Dict[str, Any]] = Depends(_auth.current_user_optional),
) -> Dict[str, Any]:
    _ensure_row_writable("tasks", task_id, role, user=actor)
    # Capture the pre-update `done` state so we can detect a 0→1
    # transition AFTER the patch lands — that's what triggers the
    # recurring-task materialiser.
    pre_done: Optional[int] = None
    with conn_ctx(DB_PATH) as conn:
        pre = conn.execute("SELECT done FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if pre:
            pre_done = int(pre["done"] or 0)

    fields = patch.model_dump(exclude_unset=True, exclude={"assignee_user_ids", "assign_everyone"})
    # Empty-string recurrence_rule = "clear" — store NULL so the
    # frontend can null the field with a PATCH instead of having to
    # call a delete.
    if "recurrence_rule" in fields and isinstance(fields["recurrence_rule"], str) \
            and not fields["recurrence_rule"].strip():
        fields["recurrence_rule"] = None
    result = _apply_patch("tasks", task_id, fields)

    # Assignees update (only when explicitly passed).
    if patch.assignee_user_ids is not None or patch.assign_everyone is not None:
        ids = _resolve_assignees(patch.assignee_user_ids, bool(patch.assign_everyone))
        added, _removed = _replace_task_assignees(task_id, ids)
        _notify_task_assigned(task_id, added, actor)

    # Recurring task: if this PATCH just flipped done from 0→1, spawn
    # the next instance. Skip when patch.done wasn't actually part of
    # this request (silent reschedules shouldn't trigger).
    if pre_done == 0 and patch.done is True:
        from . import tasks_recurrence as _rec
        try:
            with conn_ctx(DB_PATH) as conn:
                new_id = _rec.materialise_next_instance(conn=conn, task_id=task_id)
                if new_id is not None:
                    conn.commit()
        except Exception:  # noqa: BLE001
            # Recurrence is best-effort — never break the actual done-flip.
            pass

    return _attach_assignees([result])[0]


@app.delete("/api/tasks/{task_id}", status_code=204, response_class=Response)
def delete_task(
    task_id: int,
    role: str = Depends(_auth.current_role),
    user: dict[str, Any] = Depends(_auth.current_user_optional),
) -> Response:
    _ensure_row_writable("tasks", task_id, role, user=user)
    with conn_ctx(DB_PATH) as conn:
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    return Response(status_code=204)


class TasksAskBody(BaseModel):
    query: str = Field(min_length=1, max_length=300)
    group_by: Optional[str] = None  # 'due' | 'category' | 'person' | 'none' | None=let LLM pick


# Fields the magic bar is allowed to propose changes for. Title, notes,
# due_date, done, and assignees are excluded — those are too consequential
# to let an LLM mass-edit without explicit per-row UI.
_TASKS_ASK_WRITABLE_FIELDS = {"priority", "estimated_minutes", "category"}


@app.post("/api/tasks/ask")
async def tasks_ask(
    body: TasksAskBody,
    role: str = Depends(_auth.current_role),
) -> Dict[str, Any]:
    """Magic search bar. Two modes the LLM can pick:
      - mode='view':   filter + group the task list (no writes)
      - mode='update': propose changes to priority / estimated_minutes /
                       category (UI confirms before /tasks/batch-update
                       applies them).
    Title, notes, due_date, done, and assignees stay off-limits via this
    path — those changes go through the dedicated skills or the inline
    editor so a careless prompt can't rewrite them."""
    require_role(role, "tasks")
    base = "SELECT * FROM tasks WHERE done = 0"
    base, params = apply_filter(role, "tasks", base)
    base += " ORDER BY COALESCE(due_date, '9999') ASC, id ASC LIMIT 60"
    with conn_ctx(DB_PATH) as conn:
        rows = conn.execute(base, params).fetchall()
    tasks = _rows_to_dicts(rows)
    if not tasks:
        return {"query": body.query, "mode": "view",
                "filtered_ids": [], "grouping": "none",
                "groups": [], "summary": "No open tasks.", "updates": []}

    compact = [
        {"id": t["id"], "t": t["title"], "due": t.get("due_date"),
         "cat": t.get("category"), "p": t.get("priority"),
         "est": t.get("estimated_minutes"), "n": (t.get("notes") or "")[:80]}
        for t in tasks
    ]
    import json as _json
    prompt = (
        "You are a task assistant. Decide whether the user wants to VIEW the list "
        "(filter/group) or UPDATE it (estimate durations, set priorities, tag with "
        "a category). Reply with ONE JSON object, no markdown.\n\n"
        "For a VIEW request:\n"
        "  {\"mode\": \"view\",\n"
        "   \"filtered_ids\": [ids that match],\n"
        "   \"grouping\": \"due\" | \"category\" | \"person\" | \"none\",\n"
        "   \"groups\": [{\"label\": str, \"ids\": [int]}],\n"
        "   \"summary\": short sentence in the user's language}\n\n"
        "For an UPDATE request:\n"
        "  {\"mode\": \"update\",\n"
        "   \"updates\": [{\"id\": int, \"changes\": {\"estimated_minutes\"?: int, \"priority\"?: 0|1|2, \"category\"?: str}}],\n"
        "   \"summary\": short sentence in the user's language describing the change}\n\n"
        "RULES for updates:\n"
        " - You may ONLY set these fields: estimated_minutes (int, in minutes), priority (0=low/1=normal/2=high), category (string).\n"
        " - NEVER include changes to title, notes, due_date, done, or assignees.\n"
        " - Only include a field in `changes` if the user explicitly asked for it. E.g. for 'estimate durations', only include estimated_minutes.\n"
        " - For estimated_minutes: 5-15min for trivial errands, 30-60min for moderate work, 120-480min for major projects. Use the task title + notes to judge.\n"
        " - If the user said 'estimate the ones without an estimate', only include tasks where est is null.\n\n"
        f"Task list (JSON):\n{_json.dumps(compact, ensure_ascii=False)}\n"
        f"\nUser request: {body.query}\n"
        + (f"\nForce grouping: {body.group_by}\n" if body.group_by else "")
        + "\nReply with the JSON object only."
    )
    from backend.whatsapp import _call_llm
    try:
        raw = await _call_llm(prompt)
    except Exception as e:
        raise HTTPException(502, f"LLM call failed: {e}")
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip().rstrip("`").strip()
    try:
        parsed = _json.loads(raw)
    except _json.JSONDecodeError:
        ql = body.query.lower()
        ids = [t["id"] for t in tasks if ql in (t["title"] or "").lower()]
        return {"query": body.query, "mode": "view",
                "filtered_ids": ids, "grouping": "none",
                "groups": [{"label": "Matches", "ids": ids}],
                "summary": f"{len(ids)} task(s) matching '{body.query}' (LLM unparseable, fell back to text search).",
                "updates": []}

    valid_ids = {t["id"] for t in tasks}
    mode = parsed.get("mode", "view")

    if mode == "update":
        # Validate every proposed update: only known ids, only whitelisted
        # fields, sane value types. Anything weird is dropped silently
        # rather than failing — the user sees the cleaned-up list in the
        # confirm modal.
        cleaned: list[dict] = []
        for u in (parsed.get("updates") or []):
            tid = u.get("id")
            if tid not in valid_ids:
                continue
            changes = u.get("changes") or {}
            ok_changes: dict = {}
            for k, v in changes.items():
                if k not in _TASKS_ASK_WRITABLE_FIELDS:
                    continue
                if k == "estimated_minutes":
                    try:
                        iv = int(v)
                        if 1 <= iv <= 24 * 60:
                            ok_changes[k] = iv
                    except (TypeError, ValueError):
                        pass
                elif k == "priority":
                    try:
                        iv = int(v)
                        if iv in (0, 1, 2):
                            ok_changes[k] = iv
                    except (TypeError, ValueError):
                        pass
                elif k == "category":
                    if isinstance(v, str) and v.strip():
                        ok_changes[k] = v.strip()[:60]
            if ok_changes:
                cleaned.append({"id": tid, "changes": ok_changes})
        return {
            "query":   body.query,
            "mode":    "update",
            "updates": cleaned,
            "summary": parsed.get("summary") or f"Proposing changes to {len(cleaned)} task(s).",
            "filtered_ids": [u["id"] for u in cleaned],
            "groups":  [],
            "grouping": "none",
        }

    # mode == 'view' (default)
    parsed["mode"] = "view"
    parsed["filtered_ids"] = [i for i in (parsed.get("filtered_ids") or []) if i in valid_ids]
    parsed["groups"] = [
        {"label": g.get("label") or "Other",
         "ids": [i for i in (g.get("ids") or []) if i in valid_ids]}
        for g in (parsed.get("groups") or [])
    ]
    parsed["updates"] = []
    parsed["query"] = body.query
    return parsed


class TasksBatchUpdateBody(BaseModel):
    updates: List[Dict[str, Any]]  # [{id, changes: {...}}]


@app.post("/api/tasks/batch-update")
def tasks_batch_update(
    body: TasksBatchUpdateBody,
    role: str = Depends(_auth.current_role),
    user: dict[str, Any] = Depends(_auth.current_user_optional),
) -> Dict[str, Any]:
    """Apply a vetted set of changes produced by /api/tasks/ask (mode=
    update). Same whitelist enforced here so a hand-crafted POST can't
    sneak in extra fields. Returns counts so the UI can toast a result."""
    require_role(role, "tasks")
    require_write(role)
    applied = 0
    rejected = 0
    with conn_ctx(DB_PATH) as conn:
        for u in body.updates:
            tid = u.get("id")
            changes = u.get("changes") or {}
            if not isinstance(tid, int):
                rejected += 1
                continue
            try:
                _ensure_row_writable("tasks", tid, role, user=user)
            except HTTPException:
                rejected += 1
                continue
            cols = []
            vals = []
            for k, v in changes.items():
                if k not in _TASKS_ASK_WRITABLE_FIELDS:
                    continue
                cols.append(f"{k} = ?")
                vals.append(v)
            if not cols:
                rejected += 1
                continue
            vals.append(tid)
            conn.execute(f"UPDATE tasks SET {', '.join(cols)} WHERE id = ?", vals)
            applied += 1
    return {"applied": applied, "rejected": rejected}


@app.get("/api/tasks")
def list_tasks(
    role: str = Depends(_auth.current_role),
    user: dict[str, Any] = Depends(_auth.current_user_optional),
    calendar_ids: Optional[str] = Query(None, description="Comma-separated calendar IDs to filter tasks by (via assignee link)."),
) -> List[Dict[str, Any]]:
    """List tasks, optionally narrowed to ones that 'belong' to the
    visible calendar set. Tasks have no calendar_id of their own — the
    rule is: a task belongs to calendar X if any of its assignees is
    the owner of calendar X. Plus: if the household calendar
    (kind='shared') is among the visible ids, also include tasks with
    no assignees at all — those are the household-level chores nobody's
    been explicitly assigned to.
    """
    require_role(role, "tasks")
    base = "SELECT * FROM tasks"
    where_clauses: list[str] = []
    params_list: list[Any] = []

    if calendar_ids:
        ids = [int(x) for x in calendar_ids.split(",") if x.strip().lstrip("-").isdigit()]
        if ids:
            # Resolve calendar_ids → set of owner user ids + flag for "is
            # the Shared calendar in the visible set?" Tasks belong to a
            # calendar by two routes: (a) any assignee owns the calendar,
            # (b) the creator owns the calendar (covers tasks you made
            # without assigning anyone — they still belong on your view).
            from . import calendars as _cal
            owner_ids: set[int] = set()
            include_unassigned = False
            for cid in ids:
                cal = _cal.get(cid)
                if not cal:
                    continue
                owner_ids.add(int(cal["owner_user_id"]))
                if cal["kind"] == "shared":
                    include_unassigned = True
            sub_parts: list[str] = []
            if owner_ids:
                placeholders = ",".join("?" * len(owner_ids))
                # (a) any assignee owns one of the visible calendars
                sub_parts.append(
                    f"tasks.id IN (SELECT task_id FROM task_assignees WHERE user_id IN ({placeholders}))"
                )
                params_list.extend(owner_ids)
                # (b) creator owns one of the visible calendars — catches
                # tasks the user made without explicit assignees so they
                # still show on their own Personal view (the main use case
                # this column was added for in migration 012).
                placeholders = ",".join("?" * len(owner_ids))
                sub_parts.append(
                    f"tasks.created_by_user_id IN ({placeholders})"
                )
                params_list.extend(owner_ids)
            if include_unassigned:
                # Only counts as a "household chore" if NEITHER an
                # assignee nor a creator owns it — otherwise it'd
                # double-show on Personal AND Shared.
                sub_parts.append(
                    "(tasks.id NOT IN (SELECT task_id FROM task_assignees) "
                    " AND tasks.created_by_user_id IS NULL)"
                )
            if sub_parts:
                where_clauses.append("(" + " OR ".join(sub_parts) + ")")
            else:
                # Caller asked for some calendars but none resolved — return [].
                where_clauses.append("1 = 0")

    # Phase B (2026-06-02): hard gate by space membership. Without this,
    # any logged-in member used to see tasks owned by other users — the
    # old role-allowlist (allowed_roles LIKE '%member%') was too coarse.
    # Phase C T13: only platform_admin truly bypasses. Workspace admins
    # (role='admin') go through user_visible_space_ids so they're
    # scoped to spaces in workspaces they own — otherwise an admin from
    # one workspace would see every other workspace's tasks (this leak
    # was caught when WS3 admin Jane saw 7 WS1+WS2 tasks).
    if role != "platform_admin":
        from . import spaces as _sp
        uid = user.get("id") if user else None
        visible_spaces = _sp.user_visible_space_ids(uid, role) if uid else []
        if visible_spaces:
            placeholders = ",".join("?" * len(visible_spaces))
            where_clauses.append(f"(tasks.space_id IN ({placeholders}) OR tasks.created_by_user_id = ?)")
            params_list.extend(visible_spaces)
            params_list.append(uid)
        else:
            # Logged-out caller (shouldn't happen — require_role gates
            # above) or member with no spaces: return nothing.
            where_clauses.append("1=0")

    if where_clauses:
        base += " WHERE " + " AND ".join(where_clauses)
    base, params_tuple = apply_filter(role, "tasks", base, tuple(params_list))
    base += " ORDER BY COALESCE(due_date, '9999') ASC, id ASC"
    with conn_ctx(DB_PATH) as conn:
        rows = conn.execute(base, params_tuple).fetchall()
    return _attach_assignees(_rows_to_dicts(rows))


@app.post("/api/tasks", status_code=201)
def create_task(
    task: TaskIn,
    role: str = Depends(_auth.current_role),
    actor: Optional[Dict[str, Any]] = Depends(_auth.current_user_optional),
) -> Dict[str, Any]:
    require_role(role, "tasks")
    require_write(role)
    creator_id = actor["id"] if actor and actor.get("id") else None
    # Normalise empty recurrence strings to NULL so the DB stays clean.
    recurrence_rule = task.recurrence_rule
    if isinstance(recurrence_rule, str) and not recurrence_rule.strip():
        recurrence_rule = None
    # Default new tasks to the creator's personal space so the row is
    # immediately visible-to-self and invisible-to-others without
    # needing the creator_id fallback in the list filter. Falls back
    # to NULL only when there's no authenticated creator (legacy /
    # internal seed calls).
    from . import spaces as _sp
    default_space = _sp.personal_space_id(creator_id) if creator_id else None
    with conn_ctx(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO tasks (title, due_date, person, category, notes, "
            " priority, estimated_minutes, created_by_user_id, "
            " parent_task_id, recurrence_rule, space_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task.title, task.due_date, task.person, task.category, task.notes,
             task.priority, task.estimated_minutes, creator_id,
             task.parent_task_id, recurrence_rule, default_space),
        )
        new_id = cur.lastrowid
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (new_id,)).fetchone()

    # Resolve + persist assignees. Default to the calling user if neither
    # `assignee_user_ids` nor `assign_everyone` was provided (so a task
    # created from "I need to do X" lands on the creator without them
    # having to pick themselves out of the dropdown).
    if task.assignee_user_ids is None and not task.assign_everyone:
        ids = [actor["id"]] if actor else []
    else:
        ids = _resolve_assignees(task.assignee_user_ids, task.assign_everyone)
    added, _ = _replace_task_assignees(new_id, ids)
    _notify_task_assigned(new_id, added, actor)
    return _attach_assignees([dict(row)])[0]


def _notify_task_assigned(task_id: int, added_user_ids: List[int],
                           actor: Optional[Dict[str, Any]]) -> None:
    """Create a notification for each newly-added assignee — except the
    actor themselves (who knows they made the task)."""
    if not added_user_ids:
        return
    from . import notifications as _notif
    actor_id = actor.get("id") if actor else None
    actor_name = (actor or {}).get("name", "Someone")
    with conn_ctx(DB_PATH) as conn:
        row = conn.execute(
            "SELECT title, due_date FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
    if not row:
        return
    title = row["title"]
    due = row["due_date"]
    for uid in added_user_ids:
        if uid == actor_id:
            continue  # don't notify the creator
        _notif.create(
            user_id=uid,
            kind="task_assigned",
            title=f"{actor_name} assigned a task to you",
            body=f"{title}" + (f" · due {due}" if due else ""),
            payload={"task_id": task_id, "task_title": title, "from_user_id": actor_id},
            navigate_to=f"/r/calendar?task={task_id}",
        )


# ─────────────── tasks/parse-natural — one-line LLM capture ───────────────
# Drives the new single-input composer in the Tasks app. The user
# types something like "Müll rausbringen morgen 18:00 #haushalt !hoch
# every week" and we ask the LLM to extract a structured task. The
# UI then drops the parsed fields into the regular create form for
# review-and-edit before the actual POST /api/tasks.

class TaskParseIn(BaseModel):
    text: str


@app.post("/api/tasks/parse-natural")
async def tasks_parse_natural(
    body: TaskParseIn,
    role: str = Depends(_auth.current_role),
) -> Dict[str, Any]:
    """Parse free-form text into task fields. Returns the structured
    fields plus a `_warnings` list when the model couldn't confidently
    pull a value the user seemed to expect. Always returns at minimum
    ``{title: <the original text>}`` so the UI can fall back gracefully
    if the LLM is offline or the parse fails."""
    require_role(role, "tasks")
    text = (body.text or "").strip()
    if not text:
        return {"title": "", "_warnings": ["empty input"]}
    fallback: Dict[str, Any] = {"title": text}

    from datetime import date as _date
    today_iso = _date.today().isoformat()

    system_msg = (
        "You convert free-form task text into a strict JSON object. "
        f"Today is {today_iso}. The JSON keys you may emit:\n"
        '  - "title" (string, required): the cleaned task title with '
        "hashtags, priority markers, and date phrases removed.\n"
        '  - "due_date" (YYYY-MM-DD or null): resolve "morgen"/"tomorrow"/'
        '"next Mon" to a real date. Null when unspecified.\n'
        '  - "priority" (0|1|2, default 1): high=2, normal=1, low=0. '
        '"!hoch"/"!high"/"!urgent" → 2. "!low"/"!niedrig" → 0.\n'
        '  - "category" (string or null): hashtag without the #. '
        'Pick one — if multiple, take the first.\n'
        '  - "estimated_minutes" (int or null): parse "30 min", "1h 30m", '
        '"~45m"; convert hours to minutes. Null when unspecified.\n'
        '  - "recurrence_rule" (string or null): only when the user said '
        '"every day", "weekly", "jeden montag", "every Mon,Wed,Fri", '
        '"monthly". Use the literal shorthand the parser understands: '
        '"daily" / "weekly" / "every N days|weeks|months" / "monthly" / '
        '"every Mon" / "every Mon,Wed,Fri".\n\n'
        "Output ONLY the JSON object. No markdown fences, no commentary. "
        "Omit keys you cannot extract — do NOT invent values."
    )
    user_msg = f'Task text: """{text}"""\n\nJSON:'

    from . import ask as _ask
    await _ask._ensure_agent_singletons()
    llm = getattr(_ask._ask_own_backend, "_llm", None)
    if llm is None:
        return {**fallback, "_warnings": ["LLM unavailable — only title set"]}

    try:
        import asyncio as _asyncio
        resp = await _asyncio.to_thread(
            llm.chat,
            [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            None,
            max_tokens=300,
            temperature=0.0,
        )
    except Exception as exc:  # noqa: BLE001
        return {**fallback, "_warnings": [f"LLM error: {type(exc).__name__}"]}

    raw = ((resp or {}).get("content") or "").strip()
    import json as _json, re as _re
    parsed: Dict[str, Any] = {}
    if raw.startswith("```"):
        raw = _re.sub(r"^```(?:json)?\s*", "", raw)
        raw = _re.sub(r"\s*```$", "", raw)
    try:
        parsed = _json.loads(raw)
        if not isinstance(parsed, dict):
            parsed = {}
    except ValueError:
        m = _re.search(r"\{.*\}", raw, _re.DOTALL)
        if m:
            try:
                parsed = _json.loads(m.group(0))
                if not isinstance(parsed, dict):
                    parsed = {}
            except ValueError:
                pass

    # Filter to known keys, coerce types, fall back to the raw text
    # for `title` if the LLM dropped it.
    out: Dict[str, Any] = {"title": (parsed.get("title") or "").strip() or text}
    if isinstance(parsed.get("due_date"), str) and \
       _re.fullmatch(r"\d{4}-\d{2}-\d{2}", parsed["due_date"].strip()):
        out["due_date"] = parsed["due_date"].strip()
    if isinstance(parsed.get("priority"), int) and parsed["priority"] in (0, 1, 2):
        out["priority"] = parsed["priority"]
    if isinstance(parsed.get("category"), str) and parsed["category"].strip():
        out["category"] = parsed["category"].strip()
    if isinstance(parsed.get("estimated_minutes"), int) and parsed["estimated_minutes"] > 0:
        out["estimated_minutes"] = parsed["estimated_minutes"]
    if isinstance(parsed.get("recurrence_rule"), str) and parsed["recurrence_rule"].strip():
        out["recurrence_rule"] = parsed["recurrence_rule"].strip().lower()
    return out


# ── task categories ────────────────────────────────────────────────────────

class TaskCategoryIn(BaseModel):
    name: str
    color: str = "#818cf8"


@app.get("/api/task-categories")
def list_task_categories() -> List[Dict[str, Any]]:
    """All user-defined task categories. Visible to every role (cheap metadata)."""
    with conn_ctx(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, name, color, position FROM task_categories ORDER BY position, name"
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/task-categories", status_code=201)
def create_task_category(body: TaskCategoryIn, role: str = Depends(_auth.current_role)) -> Dict[str, Any]:
    """Add a new category. Admin/member only (creates shared state)."""
    require_role(role, "tasks")
    require_write(role)
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    with conn_ctx(DB_PATH) as conn:
        try:
            cur = conn.execute(
                "INSERT INTO task_categories (name, color, position) "
                "VALUES (?, ?, COALESCE((SELECT MAX(position)+1 FROM task_categories), 0))",
                (name, body.color or "#818cf8"),
            )
            row = conn.execute(
                "SELECT id, name, color, position FROM task_categories WHERE id = ?",
                (cur.lastrowid,),
            ).fetchone()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"could not create: {exc}") from exc
    return dict(row)


@app.delete("/api/task-categories/{cat_id}", status_code=204, response_class=Response)
def delete_task_category(cat_id: int, role: str = Depends(_auth.current_role)) -> Response:
    """Delete a category. Tasks that referenced it become uncategorised."""
    if normalize_role(role) not in ("admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="role required: admin")
    with conn_ctx(DB_PATH) as conn:
        row = conn.execute("SELECT name FROM task_categories WHERE id = ?", (cat_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="category not found")
        # Null out any task that used this category — no FK so it's just a string match.
        conn.execute("UPDATE tasks SET category = NULL WHERE category = ?", (row["name"],))
        conn.execute("DELETE FROM task_categories WHERE id = ?", (cat_id,))
    return Response(status_code=204)


@app.get("/api/photos/{asset_id}/raw")
async def proxy_immich_asset(
    asset_id: str,
    download: bool = Query(False, description="Set Content-Disposition: attachment"),
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Response:
    """Same-origin proxy for an Immich asset.

    Why this exists: Immich runs on a different port (2283), so a direct
    browser fetch of `http://localhost:2283/api/assets/<id>/original`
    from the Yorik origin (:8000) is cross-origin. Two consequences the
    chat photo lightbox tripped on:

      - `fetch()` fails with "Failed to fetch" because Immich doesn't
        send permissive CORS headers (so Clipboard API can't get the
        bytes).
      - `<a download>` is ignored for cross-origin URLs unless the
        server sets `Content-Disposition: attachment` — so the
        Download button just opened the image in a new tab.

    Routing through Yorik fixes both: same-origin bytes for Clipboard,
    and we control Content-Disposition for the download case.

    Auth: tries the calling user's per-user Immich key first (Phase B
    ACL provisions one per Yorik user), falling back to the global
    admin key. Without that fallback Immich 400s when the admin key
    asks for an asset that belongs to a non-admin user — same pattern
    as /api/photos/{id}/thumbnail.
    """
    from . import external_users
    from . import credential_store
    import sqlite3 as _sqlite
    creds: dict[str, Any] | None = None
    uid = user.get("id") if user else None
    if uid:
        creds = external_users.get_user_immich_creds(uid)
    if not creds or not creds.get("api_key"):
        creds = credential_store.get("immich") or {}
    base_url = (creds.get("base_url") or "").rstrip("/")
    api_key  = creds.get("api_key") or ""
    if not (base_url and api_key):
        # Fall back to the legacy app_settings keys (same pattern as
        # paperless_ingest._paperless_settings).
        try:
            with conn_ctx(DB_PATH) as conn:
                for k, dest in (("immich_base_url", "base_url"), ("immich_api_key", "api_key")):
                    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (k,)).fetchone()
                    if row and row["value"]:
                        if dest == "base_url" and not base_url: base_url = row["value"].rstrip("/")
                        elif dest == "api_key" and not api_key: api_key = row["value"]
        except _sqlite.Error:
            pass
    if not (base_url and api_key):
        raise HTTPException(status_code=503, detail="Immich not configured")
    if not re.match(r"^[A-Za-z0-9._-]+$", asset_id):
        # Defence-in-depth — the path param could otherwise smuggle ../
        # or /../api/. Immich asset ids are UUIDs in practice.
        raise HTTPException(status_code=400, detail="invalid asset_id")

    import httpx as _httpx
    upstream = f"{base_url}/api/assets/{asset_id}/original"
    try:
        client = _httpx.AsyncClient(timeout=15.0)
        try:
            r = await client.get(upstream, headers={"x-api-key": api_key})
        finally:
            await client.aclose()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"immich unreachable: {exc}")
    if r.status_code != 200:
        # Surface Immich's own message — same as the thumbnail proxy —
        # so a wrong/expired per-user key shows "Unauthorized" instead
        # of a bare status.
        raise HTTPException(
            status_code=r.status_code,
            detail=f"immich returned {r.status_code}: {r.text[:200] or '(no body)'}",
        )

    headers: Dict[str, str] = {"cache-control": "private, max-age=600"}
    if download:
        # Best-effort filename from the upstream Content-Disposition; falls
        # back to the asset id with a generic extension.
        cd = r.headers.get("content-disposition", "")
        m = re.search(r'filename="?([^";]+)"?', cd)
        filename = m.group(1) if m else f"{asset_id}.jpg"
        headers["content-disposition"] = f'attachment; filename="{filename}"'
    return Response(
        content=r.content,
        media_type=r.headers.get("content-type", "image/jpeg"),
        headers=headers,
    )


class _LabelPersonIn(BaseModel):
    name: str


@app.post("/api/immich/people/{person_id}/name")
async def label_immich_person(
    person_id: str,
    body: _LabelPersonIn,
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Name an Immich face cluster from chat. Called by the
    PeoplePickerCard when the user identifies an unrecognised face that
    blocked a find_photo search ("which one of these is Sara?").

    Uses the calling user's per-user Immich token (wave 3) when
    available so each user labels in their own Immich library — admin
    fallback for legacy installs."""
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    from .connectors.immich import _label_person
    from . import external_users
    creds = None
    if user.get("id"):
        creds = external_users.get_user_immich_creds(user["id"])
    ok = await asyncio.to_thread(_label_person, person_id, name, creds)
    if not ok:
        raise HTTPException(502, "Immich label failed — check the connector logs")
    return {"ok": True, "person_id": person_id, "name": name}


# ─── Immich asset thumbnail proxy ────────────────────────────────────
# Why this exists: the connector previously emitted absolute
# `http://localhost:2283/...` thumbnail URLs that the user's browser
# was supposed to load directly. That breaks the moment Yorik is
# accessed from any device other than the host (tablet on the LAN,
# remote browser over Tailscale, Cloudflare tunnel, etc.) because
# `localhost` resolves to the *client's* machine. We now emit
# Yorik-relative URLs and proxy them server-side, so the tablet only
# needs to talk to the Yorik origin it's already authenticated against.

import httpx as _httpx  # already imported elsewhere; aliased here for clarity

@app.get("/api/photos/{asset_id}/thumbnail")
async def immich_thumbnail_proxy(
    asset_id: str,
    request: Request,
    size: str = Query("preview", description="Immich thumbnail size: 'thumbnail' | 'preview'"),
    u: Optional[int] = Query(None, description="Owner user_id — honoured for kiosk-scope callers so the household wall can fetch any member's asset; ignored for regular browser sessions."),
    user: dict[str, Any] = Depends(_auth.current_user),
):
    """Stream an Immich asset thumbnail through Yorik so the URL works
    from any device that can reach Yorik (not just localhost on the
    host). Uses the calling user's per-user Immich key so library
    permissions stay enforced.

    Cross-user fetch on the kiosk wall: when /api/ambient/slideshow
    aggregates today's photos across all workspace users, individual
    asset IDs belong to whichever user uploaded them — Dirk's key
    can't see Anna's library, etc. To unbreak the wall, slideshow
    URLs include `?u=<owner_uid>`, and we honour that hint here ONLY
    when the caller is on a kiosk-scope session (cookie is a kiosk
    session OR the request carries a trusted x-yorik-wall-device
    header). Regular browser callers stay scoped to their own library.

    Cached on the client for a day — Immich thumbnails are immutable
    once generated, so re-fetching costs us bandwidth for no gain.
    """
    if size not in ("preview", "thumbnail"):
        raise HTTPException(400, "size must be 'preview' or 'thumbnail'")
    # UUID-shaped path component — bail on anything weird so we never
    # forward a path-traversal upstream.
    import re as _re
    if not _re.match(r"^[A-Za-z0-9_\-]{1,64}$", asset_id):
        raise HTTPException(400, "invalid asset id")

    from . import external_users
    from . import credential_store

    # Build the list of user_ids whose Immich key we'll try, in order.
    # The owner hint goes first (when kiosk-scope), then the caller's
    # own id, then admin as the catch-all. Dedup-preserving-order so
    # we don't double-fetch if owner == caller.
    is_kiosk_scope = False
    sid = request.cookies.get(_auth.COOKIE_NAME)
    if sid and _auth.session_is_kiosk(sid):
        is_kiosk_scope = True
    if not is_kiosk_scope:
        device_id = (request.headers.get("x-yorik-wall-device") or "").strip()
        if device_id and _auth.is_trusted_kiosk_device(device_id):
            is_kiosk_scope = True

    uid = user.get("id") if user else None
    candidate_uids: list[int] = []
    if u is not None and is_kiosk_scope:
        candidate_uids.append(int(u))
    if uid:
        candidate_uids.append(uid)
    candidate_uids = list(dict.fromkeys(candidate_uids))

    creds = None
    for cand_uid in candidate_uids:
        c = external_users.get_user_immich_creds(cand_uid)
        if c and c.get("api_key"):
            creds = c
            break
    if not creds:
        # Admin fallback — same shape the connector uses.
        creds = credential_store.get("immich") or None
    if not creds or not creds.get("api_key"):
        raise HTTPException(503, "Immich not configured for this user")

    base = (creds.get("base_url") or "http://localhost:2283").rstrip("/")
    upstream = f"{base}/api/assets/{asset_id}/thumbnail"

    async with _httpx.AsyncClient(timeout=15.0) as client:
        try:
            r = await client.get(
                upstream,
                params={"size": size},
                headers={"x-api-key": creds["api_key"], "Accept": "image/*"},
            )
        except _httpx.RequestError as exc:
            raise HTTPException(502, f"Immich unreachable: {exc}")

    if r.status_code == 404:
        raise HTTPException(404, "asset not found")
    if r.status_code >= 400:
        # Surface Immich's own error message — useful when the user's
        # token lost access to the asset (sharing revoked, soft-delete).
        raise HTTPException(r.status_code, r.text[:200] or "Immich rejected the request")

    return Response(
        content=r.content,
        media_type=r.headers.get("content-type") or "image/jpeg",
        headers={
            # Immich thumbs are immutable per-asset, can be cached hard.
            # `private` because they include the user's own library
            # content — shared proxies must not store them.
            "Cache-Control": "private, max-age=86400",
        },
    )


@app.get("/api/photos/people/{person_id}/thumbnail")
async def immich_person_thumbnail_proxy(
    person_id: str,
    user: dict[str, Any] = Depends(_auth.current_user),
):
    """Same shape as the asset-thumbnail proxy above, but for face-
    cluster thumbnails used by the people-picker card. Without this,
    the picker shows broken images on any non-host client."""
    import re as _re
    if not _re.match(r"^[A-Za-z0-9_\-]{1,64}$", person_id):
        raise HTTPException(400, "invalid person id")

    from . import external_users
    from . import credential_store
    uid = user.get("id") if user else None
    creds = None
    if uid:
        creds = external_users.get_user_immich_creds(uid)
    if not creds:
        creds = credential_store.get("immich") or None
    if not creds or not creds.get("api_key"):
        raise HTTPException(503, "Immich not configured for this user")

    base = (creds.get("base_url") or "http://localhost:2283").rstrip("/")
    upstream = f"{base}/api/people/{person_id}/thumbnail"

    async with _httpx.AsyncClient(timeout=15.0) as client:
        try:
            r = await client.get(
                upstream,
                headers={"x-api-key": creds["api_key"], "Accept": "image/*"},
            )
        except _httpx.RequestError as exc:
            raise HTTPException(502, f"Immich unreachable: {exc}")

    if r.status_code == 404:
        raise HTTPException(404, "person not found")
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text[:200] or "Immich rejected the request")

    return Response(
        content=r.content,
        media_type=r.headers.get("content-type") or "image/jpeg",
        headers={"Cache-Control": "private, max-age=86400"},
    )


@app.get("/api/bills")
def list_bills(
    role: str = Depends(_auth.current_role),
    user: dict[str, Any] = Depends(_auth.current_user_optional),
) -> List[Dict[str, Any]]:
    require_role(role, "bills")
    # Phase B: filter to bills in spaces the user can see (admins see
    # all). Bills land in Finance by default; non-admins only see them
    # when they're explicitly added to Finance (or a custom shared
    # space holding bills).
    from . import spaces as _sp
    uid = user.get("id") if user else None
    frag, params = _sp.row_filter(uid, role, "bills")
    sql = f"SELECT * FROM bills WHERE {frag} ORDER BY due_date ASC"
    with conn_ctx(DB_PATH) as conn:
        rows = conn.execute(sql, params).fetchall()
    return _rows_to_dicts(rows)


class BillPatch(BaseModel):
    """Partial update for a bill. Currently the only writable field from
    the UI is `paid` (Mark paid / Mark unpaid on the home-screen bill
    modal). Schema is open-ended so we can add notes/amount/due_date
    edits later without a second endpoint."""
    paid: Optional[bool] = None
    notes: Optional[str] = None


@app.patch("/api/bills/{bill_id}")
def patch_bill(
    bill_id: int,
    body: BillPatch,
    role: str = Depends(_auth.current_role),
) -> Dict[str, Any]:
    require_role(role, "bills")
    updates: List[str] = []
    params: List[Any] = []
    if body.paid is not None:
        updates.append("paid = ?")
        params.append(1 if body.paid else 0)
    if body.notes is not None:
        updates.append("notes = ?")
        params.append(body.notes)
    if not updates:
        raise HTTPException(status_code=400, detail="no fields to update")
    params.append(bill_id)
    with conn_ctx(DB_PATH) as conn:
        cur = conn.execute(
            f"UPDATE bills SET {', '.join(updates)} WHERE id = ?", params,
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"bill {bill_id} not found")
        conn.commit()
        row = conn.execute("SELECT * FROM bills WHERE id = ?", (bill_id,)).fetchone()
    return dict(row)


# ── Contacts (identity hub) ────────────────────────────────────────────────
# REST endpoints the /r/contacts React app uses for direct edits. The LLM
# uses the matching skills (add_contact, update_contact, …) instead so
# undo + confirm-modal hook in automatically. These endpoints talk to
# backend.contacts directly — immediate writes since the user is in the
# UI making the change with intent.

from . import contacts as _contacts


class _ContactIn(BaseModel):
    display_name: str
    kind: str = "person"
    status: str = "active"
    # Person identity columns (mig 045). All optional — populated for
    # kind='person', NULL for kind='business'. first_name is the
    # canonical identity column; last_name + role are nullable;
    # employer_contact_id points at a kind='business' contact when
    # this person is reached through one.
    first_name:          Optional[str] = None
    last_name:           Optional[str] = None
    role:                Optional[str] = None
    employer_contact_id: Optional[int] = None
    aliases: Optional[List[str]] = None
    relation: Optional[str] = None
    birthday: Optional[str] = None
    language_pref: Optional[str] = None
    salutation_pref: Optional[str] = None
    legal_name: Optional[str] = None
    tax_id: Optional[str] = None
    iban: Optional[str] = None
    payment_terms_days: Optional[int] = None
    default_currency: Optional[str] = None
    notes: Optional[str] = None
    tags: Optional[List[str]] = None
    # Optional: a space slug ('household' / 'finance') or numeric id
    # to override the default-to-creator-personal placement.
    space: Optional[str] = None


class _ContactPatch(BaseModel):
    display_name: Optional[str] = None
    kind: Optional[str] = None
    status: Optional[str] = None
    first_name:          Optional[str] = None
    last_name:           Optional[str] = None
    role:                Optional[str] = None
    employer_contact_id: Optional[int] = None
    aliases: Optional[List[str]] = None
    relation: Optional[str] = None
    birthday: Optional[str] = None
    language_pref: Optional[str] = None
    salutation_pref: Optional[str] = None
    legal_name: Optional[str] = None
    tax_id: Optional[str] = None
    iban: Optional[str] = None
    payment_terms_days: Optional[int] = None
    default_currency: Optional[str] = None
    notes: Optional[str] = None
    tags: Optional[List[str]] = None
    space: Optional[str] = None  # Phase B: slug or numeric id to move the contact
    yorik_assist_enabled: Optional[bool] = None  # per-contact AI opt-in


class _ChannelIn(BaseModel):
    kind: str
    value: str
    label: Optional[str] = None


class _AddressIn(BaseModel):
    kind: str = "home"
    line1: Optional[str] = None
    line2: Optional[str] = None
    postcode: Optional[str] = None
    city: Optional[str] = None
    region: Optional[str] = None
    country: Optional[str] = None
    label: Optional[str] = None


@app.get("/api/contacts")
def list_contacts(
    status: Optional[str] = Query("active"),
    kind: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
    user: dict[str, Any] = Depends(_auth.current_user),
) -> List[Dict[str, Any]]:
    """List contacts filtered by status (default 'active') + optional kind/query.

    Pass status='any' to include all statuses (drives the Pending/Spam tabs
    in /r/contacts — they pass the literal status string they want).
    """
    role = user.get("role")
    uid = user.get("id")
    rows = _contacts.search(
        q or "",
        kind=kind if kind in ("person", "business") else None,
        status=None if status == "any" else (status or "active"),
        limit=int(limit),
        role=role,
        user_id=uid if uid is not None else None,
    )
    # Hydrate channels + addresses for the list view so the UI can show
    # one-line previews ("oma@example.com • +49 30 1234") without an
    # N+1 fetch. get() applies the same visibility gate; rows the search
    # already returned will pass it, but pass it through anyway for the
    # defence-in-depth case where contact_shares changed mid-request.
    hydrated = []
    for r in rows:
        if not r: continue
        c = _contacts.get(r["id"], role=role, user_id=uid if uid is not None else None)
        if c: hydrated.append(c)
    return hydrated


class _ContactDedupeBody(BaseModel):
    # Defaults to 'pending' because that's the only status the user
    # has asked to dedupe — Active is a curated list and Spam is the
    # user's explicit "no" pile. We accept the field so a future
    # button can dedupe other statuses without an API change.
    status: str = "pending"


@app.get("/api/contacts/triage/list")
def triage_list(
    kind: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Compact pending-contacts list for the triage modal.

    Returns each row with the latest source-document summary attached
    inline so the user can decide approve/dismiss without an extra
    round-trip per contact. Newest first (recent extractions tend to
    be the most actionable). Admin-only because triage actions are
    bulk and admin-only at apply time."""
    if (user.get("role") or "").lower() not in ("admin", "platform_admin"):
        raise HTTPException(403, "admin only")
    limit  = max(1, min(int(limit), 500))
    offset = max(0, int(offset))

    sql = (
        "SELECT c.id, c.display_name, c.kind, "
        "  c.triage_verdict, c.triage_reason, c.triage_confidence, "
        "  (SELECT proposed_json FROM contact_extraction_proposals "
        "   WHERE (match_candidate_id = c.id OR created_contact_id = c.id) "
        "   ORDER BY created_at DESC LIMIT 1) AS proposal_json "
        "FROM contacts c "
        "WHERE c.status = 'pending'"
    )
    params: List[Any] = []
    if kind in ("person", "business"):
        sql += " AND c.kind = ?"
        params.append(kind)
    sql += " ORDER BY c.id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    cnt_sql = "SELECT COUNT(*) AS n FROM contacts WHERE status='pending'"
    cnt_params: List[Any] = []
    if kind in ("person", "business"):
        cnt_sql += " AND kind = ?"
        cnt_params.append(kind)

    with conn_ctx() as c:
        rows = c.execute(sql, params).fetchall()
        total = int(c.execute(cnt_sql, cnt_params).fetchone()["n"])

        ids = [int(r["id"]) for r in rows]
        channels_by_contact: Dict[int, List[Dict[str, str]]] = {}
        addresses_by_contact: Dict[int, List[Dict[str, str]]] = {}
        if ids:
            ph = ",".join("?" * len(ids))
            ch_rows = c.execute(
                f"SELECT contact_id, kind, value FROM contact_channels "
                f"WHERE contact_id IN ({ph}) ORDER BY id",
                ids,
            ).fetchall()
            for ch in ch_rows:
                channels_by_contact.setdefault(int(ch["contact_id"]), []).append({
                    "kind":  ch["kind"],
                    "value": ch["value"],
                })
            ad_rows = c.execute(
                f"SELECT contact_id, line1, postcode, city, country "
                f"FROM contact_addresses "
                f"WHERE contact_id IN ({ph}) ORDER BY id",
                ids,
            ).fetchall()
            for a in ad_rows:
                addresses_by_contact.setdefault(int(a["contact_id"]), []).append({
                    "line1":    a["line1"] or "",
                    "postcode": a["postcode"] or "",
                    "city":     a["city"] or "",
                    "country":  a["country"] or "",
                })

    items: List[Dict[str, Any]] = []
    for r in rows:
        try:
            pj = json.loads(r["proposal_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            pj = {}
        cid = int(r["id"])
        items.append({
            "id":        cid,
            "name":      r["display_name"],
            "kind":      r["kind"],
            "summary":   (pj.get("document_summary") or "")[:200],
            "channels":  channels_by_contact.get(cid, []),
            "addresses": addresses_by_contact.get(cid, []),
            "doc_id":    pj.get("source_paperless_doc_id"),
            # LLM-suggested verdicts populated by triage_auto_classify.
            # The modal pre-fills decisions from these so the user can
            # scan + confirm instead of classifying every row by hand.
            "triage_verdict":    r["triage_verdict"],
            "triage_reason":     r["triage_reason"],
            "triage_confidence": r["triage_confidence"],
        })
    return {"items": items, "total": total, "limit": limit, "offset": offset, "kind": kind}


class _TriageApplyBody(BaseModel):
    # Legacy 2-way decisions — kept for backward compat with anything
    # that still posts the old shape.
    approve: List[int] = []
    dismiss: List[int] = []
    # 4-way decisions — the new vocabulary the LLM-triaged modal posts.
    # Each list maps to a (status, kind) outcome:
    #   active_person   → status=active, kind=person
    #   active_business → status=active, kind=business
    #   archived        → status=archived (gentler than spam, no block)
    #   spam            → status=spam (sender-block hook trigger)
    active_person:   List[int] = []
    active_business: List[int] = []
    archived:        List[int] = []
    spam:            List[int] = []


@app.post("/api/contacts/triage/apply")
def triage_apply(
    body: _TriageApplyBody,
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Bulk pending → {active_person, active_business, archived, spam}.
    Legacy approve/dismiss arrays still accepted and map to active_person
    / spam respectively. Multiple lists in one body run as sequential
    UPDATEs so the user can submit a whole modal-page of mixed decisions
    in one round-trip. All updates are filtered to status='pending' so
    a stale id can't accidentally re-flip a row that's already been
    triaged in another tab."""
    if (user.get("role") or "").lower() not in ("admin", "platform_admin"):
        raise HTTPException(403, "admin only")

    # Merge legacy + new fields. Duplicates resolve by "last list wins"
    # in the dict order below; in practice the modal only sends one
    # outcome per id so this matters mostly for legacy callers.
    person_ids   = list({int(i) for i in (body.active_person or []) + (body.approve or []) if int(i)})
    business_ids = list({int(i) for i in (body.active_business or []) if int(i)})
    archived_ids = list({int(i) for i in (body.archived or []) if int(i)})
    spam_ids     = list({int(i) for i in (body.spam or []) + (body.dismiss or []) if int(i)})

    counts = {"active_person": 0, "active_business": 0, "archived": 0, "spam": 0}
    # When a row lands in active_*, also clear triage_verdict so it
    # doesn't ghost-pre-fill on the next pass; same for archived/spam.
    with conn_ctx() as c:
        def _bulk(ids: list[int], set_clause: str) -> int:
            if not ids:
                return 0
            ph = ",".join("?" * len(ids))
            cur = c.execute(
                f"UPDATE contacts "
                f"SET {set_clause}, "
                f"    triage_verdict=NULL, triage_reason=NULL, triage_confidence=NULL, "
                f"    updated_at=datetime('now') "
                f"WHERE status='pending' AND id IN ({ph})",
                ids,
            )
            return cur.rowcount or 0

        counts["active_person"]   = _bulk(person_ids,   "status='active', kind='person', last_used_at=datetime('now')")
        counts["active_business"] = _bulk(business_ids, "status='active', kind='business', last_used_at=datetime('now')")
        counts["archived"]        = _bulk(archived_ids, "status='archived'")
        counts["spam"]            = _bulk(spam_ids,     "status='spam'")

    # Backward-compat: still surface the old approved/dismissed totals
    # so any legacy frontend reading them keeps working until it
    # picks up the new shape.
    return {
        **counts,
        "approved":  counts["active_person"] + counts["active_business"],
        "dismissed": counts["spam"],
    }


# ─── triage auto-classify (LLM pre-pass) ────────────────────────────
# Background pass over every pending contact. For each, the LLM looks
# at engagement signals (incoming count, user replies, sample subjects,
# body excerpt, account-number patterns) and returns one of four
# verdicts: active_person, active_business, archived, spam. Stored on
# the contact row; TriageModal opens with these pre-filled so the user
# becomes the reviewer, not the classifier.

# In-process registry of running auto-classify tasks. Repeated POST
# calls while one is running short-circuit (the DB progress row also
# catches this, but having the handle here lets a future cancel
# endpoint stop it cleanly).
_contacts_triage_tasks: dict[str, asyncio.Task] = {}


# ─── modality signal collectors ─────────────────────────────────────
# One collector per channel kind. Each returns a {} when the contact
# has no presence on that modality, so the LLM prompt can render only
# the sections that have actual data. Adding a new modality (Telegram,
# Signal, paperless when it has enough corpus) is one new function +
# one entry in the dispatch dict below — no other code changes.

def _email_signals(c, channels: list[dict]) -> dict[str, Any]:
    """Engagement signals from email_messages. Counts incoming, user
    replies (via to_addrs LIKE — to_addrs is JSON-as-TEXT, so JSONB
    operators aren't available; substring is a 99%-correct cheap
    proxy), most-recent received, and 5 sample subjects + 1 body
    excerpt for the LLM prompt."""
    email_values = [r["value"] for r in channels if r["kind"] == "email"]
    if not email_values:
        return {}
    lowered = [v.lower() for v in email_values]
    ph = ",".join(["?"] * len(email_values))
    agg = c.execute(
        f"SELECT COUNT(*) AS cnt, MAX(date_received) AS last_ts "
        f"FROM email_messages "
        f"WHERE LOWER(from_email) IN ({ph}) AND is_sent=0",
        lowered,
    ).fetchone()
    received_count = int(agg["cnt"] if agg else 0)
    last_received = agg["last_ts"] if agg else None
    or_clauses = " OR ".join(["LOWER(to_addrs) LIKE ?"] * len(email_values))
    patterns = [f"%{v}%" for v in lowered]
    rep = c.execute(
        f"SELECT COUNT(*) AS cnt FROM email_messages "
        f"WHERE is_sent=1 AND ({or_clauses})",
        patterns,
    ).fetchone()
    sent_count = int(rep["cnt"] if rep else 0)
    subj_rows = c.execute(
        f"SELECT subject, body_text FROM email_messages "
        f"WHERE LOWER(from_email) IN ({ph}) AND is_sent=0 "
        f"ORDER BY date_received DESC NULLS LAST, id DESC LIMIT 5",
        lowered,
    ).fetchall()
    sample_subjects = [r["subject"] or "" for r in subj_rows]
    body_excerpt = (subj_rows[0]["body_text"] or "")[:1200] if subj_rows else ""
    if received_count == 0 and sent_count == 0:
        return {}
    return {
        "received": received_count,
        "user_replies": sent_count,
        "last_received": last_received,
        "sample_subjects": sample_subjects,
        "body_excerpt": body_excerpt,
    }


def _whatsapp_signals(c, channels: list[dict]) -> dict[str, Any]:
    """WhatsApp message engagement per chat_jid join. from_me=0 is
    incoming, from_me=1 is the user replying. Strong signal: a
    contact with 50+ user replies in 1:1 chat is unambiguously a
    real human relationship regardless of email volume."""
    jids = [r["value"] for r in channels if r["kind"] == "whatsapp"]
    if not jids:
        return {}
    ph = ",".join(["?"] * len(jids))
    agg = c.execute(
        f"SELECT COUNT(*) FILTER (WHERE from_me=0) AS incoming, "
        f"       COUNT(*) FILTER (WHERE from_me=1) AS user_replies, "
        f"       MAX(timestamp) AS latest_ts "
        f"FROM wa_messages "
        f"WHERE chat_jid IN ({ph})",
        jids,
    ).fetchone()
    incoming = int(agg["incoming"] if agg else 0)
    replies  = int(agg["user_replies"] if agg else 0)
    if incoming == 0 and replies == 0:
        return {}
    latest_ts = agg["latest_ts"]
    sample_rows = c.execute(
        f"SELECT text FROM wa_messages "
        f"WHERE chat_jid IN ({ph}) AND from_me=0 AND text IS NOT NULL AND text <> '' "
        f"ORDER BY timestamp DESC LIMIT 5",
        jids,
    ).fetchall()
    samples = [r["text"][:140] for r in sample_rows if r["text"]]
    return {
        "incoming": incoming,
        "user_replies": replies,
        "latest_unix_ts": latest_ts,
        "sample_texts": samples,
    }


def _calendar_signals(c, channels: list[dict], display_name: str) -> dict[str, Any]:
    """Calendar attendance by exact-name match (case-insensitive).
    event_attendees.person_name is free-text and there's no FK back
    to contacts.id, so the join is on display_name. Rough but useful:
    a contact appearing in N future or recent events is a strong
    active-person signal independent of email/WA. Returns {} when
    no matches found (current state of most installs)."""
    nm = (display_name or "").strip().lower()
    if not nm:
        return {}
    agg = c.execute(
        "SELECT COUNT(DISTINCT event_id) AS cnt "
        "FROM event_attendees "
        "WHERE LOWER(person_name) = ?",
        (nm,),
    ).fetchone()
    cnt = int(agg["cnt"] if agg else 0)
    if cnt == 0:
        return {}
    titles = c.execute(
        "SELECT e.title FROM events e "
        "JOIN event_attendees a ON a.event_id = e.id "
        "WHERE LOWER(a.person_name) = ? "
        "ORDER BY e.starts_at DESC LIMIT 5",
        (nm,),
    ).fetchall()
    return {
        "event_count": cnt,
        "sample_titles": [r["title"] for r in titles if r["title"]],
    }


# Paperless: skipped for now (corpus too small to derive useful per-
# contact signals on this install). When the contact_extraction_proposals
# table fills out, add _paperless_signals here and the prompt picks it
# up automatically.


def _collect_triage_signals(contact_id: int) -> dict[str, Any]:
    """Modality-keyed engagement signals for one pending contact.
    Cross-modal by design: every active modality's collector runs and
    contributes its block to the returned dict. The LLM prompt
    renders only sections that have data, so a contact who exists
    only on WhatsApp gets WA signals (and no empty email section),
    and a contact with email + calendar gets both. Adding Telegram
    or Signal later is one new collector + one dispatch entry."""
    with conn_ctx() as c:
        crow = c.execute(
            "SELECT display_name FROM contacts WHERE id=?",
            (contact_id,),
        ).fetchone()
        name = (crow["display_name"] if crow else "") or ""
        ch_rows = c.execute(
            "SELECT kind, value FROM contact_channels WHERE contact_id=?",
            (contact_id,),
        ).fetchall()
        channels = [{"kind": r["kind"], "value": r["value"]} for r in ch_rows]

        # Modality dispatch table. Each entry: kind label → callable
        # that returns a dict (empty when no data for this contact).
        modalities: dict[str, dict[str, Any]] = {}
        email_sig = _email_signals(c, channels)
        if email_sig: modalities["email"] = email_sig
        wa_sig = _whatsapp_signals(c, channels)
        if wa_sig:    modalities["whatsapp"] = wa_sig
        cal_sig = _calendar_signals(c, channels, name)
        if cal_sig:   modalities["calendar"] = cal_sig

    return {
        "name": name,
        "channels": channels,
        "modalities": modalities,
    }


@app.post("/api/contacts/triage/auto-classify")
async def triage_auto_classify(
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Kick a background LLM pass over every pending contact. Repeated
    calls while one is running short-circuit. Idempotent — verdicts
    are overwritten on each run (the latest model wins), so re-running
    after tweaking the prompt is safe."""
    if (user.get("role") or "").lower() not in ("admin", "platform_admin"):
        raise HTTPException(403, "admin only")
    uid = user["id"]
    if uid in _contacts_triage_tasks and not _contacts_triage_tasks[uid].done():
        return {"ok": True, "status": "already_running"}

    with conn_ctx() as c:
        total_row = c.execute(
            "SELECT COUNT(*) AS cnt FROM contacts WHERE status='pending'"
        ).fetchone()
        total = int(total_row["cnt"]) if total_row else 0
        # Clear every pending row's triage stamp so the inner loop's
        # "triage_classified_at IS NULL" cursor picks them all up.
        # Without this, repeat clicks of Auto-classify would either
        # report 0 work (rows already stamped) OR — far worse — loop
        # forever because Python's "2026-06-20T12:54:..." ISO string
        # compares as GREATER than Postgres's "2026-06-20 12:54:..." TEXT
        # cast of NOW(): chr('T')=0x54 > chr(' ')=0x20, so the per-run
        # < cursor approach made every row look perpetually eligible.
        # IS-NULL cursor is dialect-proof.
        c.execute(
            "UPDATE contacts "
            "SET triage_verdict=NULL, triage_reason=NULL, "
            "    triage_confidence=NULL, triage_classified_at=NULL "
            "WHERE status='pending'"
        )
        c.execute(
            "INSERT INTO contacts_triage_progress "
            "(owner_user_id, total, done, status, started_at, finished_at, last_error) "
            "VALUES (?, ?, 0, 'running', NOW(), NULL, NULL) "
            "ON CONFLICT (owner_user_id) DO UPDATE SET "
            "  total = EXCLUDED.total, done = 0, status = 'running', "
            "  started_at = NOW(), finished_at = NULL, last_error = NULL",
            (uid, total),
        )
        c.commit()

    async def _run():
        # Local logger handle — main.py doesn't define a module-level
        # `log`, and referencing one inside the task would crash with
        # NameError at the FIRST log.info call (silently, because
        # asyncio task exceptions aren't surfaced unless awaited).
        _log = logging.getLogger("yorik.contacts_triage")
        try:
            from . import contacts_triage_llm as _tl
        except Exception as _imp_exc:
            _log.exception("contact triage import failed: %s", _imp_exc)
            raise
        _log.info("contact triage auto-classify starting for user %s (total=%d)", uid, total)
        done = 0
        last_error: Optional[str] = None
        try:
            while True:
                with conn_ctx() as c:
                    batch = c.execute(
                        "SELECT id FROM contacts "
                        "WHERE status='pending' AND triage_classified_at IS NULL "
                        "ORDER BY id ASC LIMIT 25"
                    ).fetchall()
                if not batch:
                    break
                for r in batch:
                    cid = int(r["id"])
                    try:
                        signals = await asyncio.to_thread(_collect_triage_signals, cid)
                        verdict = await asyncio.to_thread(
                            _tl.classify_contact, **signals,
                        )
                        # Always stamp triage_classified_at so a None
                        # verdict (LLM parse failure) doesn't make the
                        # loop revisit this row forever.
                        with conn_ctx() as c:
                            if verdict is not None:
                                c.execute(
                                    "UPDATE contacts "
                                    "SET triage_verdict=?, triage_reason=?, "
                                    "    triage_confidence=?, triage_classified_at=NOW() "
                                    "WHERE id=?",
                                    (verdict["verdict"], verdict["reason"],
                                     verdict["confidence"], cid),
                                )
                            else:
                                c.execute(
                                    "UPDATE contacts SET triage_classified_at=NOW() WHERE id=?",
                                    (cid,),
                                )
                            c.commit()
                        done += 1
                    except Exception as exc:  # noqa: BLE001
                        last_error = f"contact {cid}: {exc}"
                        _log.warning("contact triage classify error %s", last_error)
                with conn_ctx() as c:
                    c.execute(
                        "UPDATE contacts_triage_progress SET done=?, last_error=? "
                        "WHERE owner_user_id=?",
                        (done, last_error, uid),
                    )
                    c.commit()
            final_status = "done"
        except asyncio.CancelledError:
            final_status = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001
            _log.exception("contact triage backfill failed: %s", exc)
            final_status = "error"
            last_error = str(exc)
        finally:
            with conn_ctx() as c:
                c.execute(
                    "UPDATE contacts_triage_progress "
                    "SET status=?, done=?, finished_at=NOW(), last_error=? "
                    "WHERE owner_user_id=?",
                    (final_status, done, last_error, uid),
                )
                c.commit()
            _log.info("contact triage auto-classify finished for user %s: status=%s, done=%d/%d",
                      uid, final_status, done, total)
            # Post a bell notification on success so the user gets
            # pinged even if they navigated away from the contacts
            # page during the run. navigate_to deep-links into the
            # Triage modal — ContactsApp opens it automatically on
            # ?triage=open. Skipped on error/cancel — those surface
            # in the status polling, no need to interrupt with a bell.
            if final_status == "done" and done > 0:
                try:
                    from . import notifications as _notif
                    _notif.create(
                        user_id=uid,
                        kind="contacts_triage_ready",
                        title=f"Yorik classified {done} contact{'s' if done != 1 else ''}",
                        body="Open Contacts → Triage to review the verdicts.",
                        navigate_to="/r/contacts?triage=open",
                    )
                except Exception as _nx:  # noqa: BLE001
                    _log.warning("triage notification create failed: %s", _nx)

    task = asyncio.create_task(_run())
    _contacts_triage_tasks[uid] = task
    return {"ok": True, "status": "started", "total": total}


@app.post("/api/contacts/triage/reclassify-archived")
def triage_reclassify_archived(
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Move every status='archived' contact back to status='pending'
    so the next auto-classify pass picks them up. Useful after
    extending the signal collectors (e.g. WhatsApp signals weren't
    consulted in the first triage pass) — gives the LLM a second
    look at contacts that were archived blind.

    No LLM call here; just a status flip. The user is expected to
    click Auto-classify afterward to actually run the new pass."""
    if (user.get("role") or "").lower() not in ("admin", "platform_admin"):
        raise HTTPException(403, "admin only")
    with conn_ctx() as c:
        cur = c.execute(
            "UPDATE contacts "
            "SET status='pending', "
            "    triage_verdict=NULL, triage_reason=NULL, "
            "    triage_confidence=NULL, triage_classified_at=NULL, "
            "    updated_at=datetime('now') "
            "WHERE status='archived'"
        )
        moved = cur.rowcount or 0
        c.commit()
    return {"moved": moved}


@app.get("/api/contacts/triage/auto-classify/status")
def triage_auto_classify_status(
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    with conn_ctx() as c:
        row = c.execute(
            "SELECT total, done, status, started_at, finished_at, last_error "
            "FROM contacts_triage_progress WHERE owner_user_id=?",
            (user["id"],),
        ).fetchone()
    if not row:
        return {"status": "idle", "total": 0, "done": 0}
    return {
        "status": row["status"],
        "total": int(row["total"] or 0),
        "done": int(row["done"] or 0),
        "started_at": str(row["started_at"]) if row["started_at"] else None,
        "finished_at": str(row["finished_at"]) if row["finished_at"] else None,
        "last_error": row["last_error"],
    }


@app.post("/api/contacts/crosslink-mailbox")
def crosslink_mailbox(
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Enrich contacts with email channels by cross-linking against the
    IMAP corpus already in email_messages. Admin-only because the write
    surface (contact_channels) is admin-only.

    Conservative by design: only touches contacts with NO existing email
    channel, uses high-confidence rules only (business: name→domain,
    person: fuzzy_ratio >= 0.85 against from_name), tags every
    insertion with source='mailbox_crosslink' so it's auditable, and
    respects the (kind, value) UNIQUE constraint so it can't steal a
    channel that already belongs to a different contact.

    Returns: scan summary + list of additions for the UI."""
    if (user.get("role") or "").lower() not in ("admin", "platform_admin"):
        raise HTTPException(403, "admin only")
    from . import contact_mailbox_crosslink as _xl
    key = str(user.get("id") or "anon")
    return _xl.crosslink_once(progress_key=key)


@app.get("/api/contacts/crosslink-mailbox/progress")
def crosslink_mailbox_progress(
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Live progress for the in-flight cross-link run. Frontend polls
    while the button is loading."""
    if (user.get("role") or "").lower() not in ("admin", "platform_admin"):
        raise HTTPException(403, "admin only")
    from . import contact_mailbox_crosslink as _xl
    key = str(user.get("id") or "anon")
    return _xl.get_progress(key)


class _ContactDedupeLlmBody(BaseModel):
    status: str = "pending"
    kind: Optional[str] = None        # None | 'business' | 'person'
    dry_run: bool = True
    # Optional: a previously-returned plan whose `merge` groups have
    # been filtered by the user in the review modal. When provided
    # alongside dry_run=false, we apply EXACTLY this plan instead of
    # re-running the LLM. Lets the modal show the plan, the user
    # uncheck groups they don't trust, and apply only the approved
    # subset without re-paying the LLM round-trip.
    plan: Optional[Dict[str, Any]] = None


@app.get("/api/contacts/dedupe-llm/progress")
def dedupe_contacts_llm_progress(
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Live progress for an in-flight dedupe build_plan() run.

    Frontend polls this every ~700 ms while the modal is in 'loading'
    state. Returns current/total cluster counts and the display_name of
    the cluster currently being reviewed. `done=true` signals the run
    finished (or never started); the modal then renders the plan it
    received from /dedupe-llm."""
    if (user.get("role") or "").lower() not in ("admin", "platform_admin"):
        raise HTTPException(403, "admin only")
    from . import contacts_dedupe_llm as _dl
    key = str(user.get("id") or "anon")
    return _dl.get_progress(key)


@app.post("/api/contacts/dedupe-llm")
def dedupe_contacts_llm(
    body: _ContactDedupeLlmBody,
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """LLM-assisted dedupe over a pending/spam bucket.

    Two passes: aggressive name normalisation pre-clusters candidates,
    then the LLM picks canonical ids per bucket using each row's
    channels and addresses as context.

    Workflow:
      1. POST with dry_run=true → returns full plan for review
      2. Frontend shows the plan in a modal, user deselects groups
      3. POST with dry_run=false + plan=<filtered> → applies it
    """
    if (user.get("role") or "").lower() not in ("admin", "platform_admin"):
        raise HTTPException(403, "admin only")
    from . import contacts_dedupe_llm as _dl

    # Fast path: user already reviewed; just apply the filtered plan.
    if body.plan is not None and not body.dry_run:
        result = _dl.apply_plan(body.plan)
        return {"dry_run": False, **result}

    try:
        plan = _dl.build_plan(
            role=user.get("role"),
            user_id=user["id"] if user.get("id") is not None else None,
            status=body.status,
            kind=body.kind,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if body.dry_run:
        return {"dry_run": True, **plan}
    result = _dl.apply_plan(plan)
    return {"dry_run": False, "plan_stats": plan.get("stats", {}), **result}


@app.post("/api/contacts/dedupe")
def dedupe_contacts(
    body: _ContactDedupeBody,
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Collapse duplicate contacts within a status bucket.

    Group by lowercased, whitespace-collapsed `display_name`. Within
    each group of size >= 2, keep the row with the highest
    "completeness score" (count of populated fields across the contact
    row + its channels + its addresses) and delete the rest. Ties
    break on lowest id (oldest row wins) so the result is
    deterministic.

    Pre-alpha simplification: we DON'T merge channels/addresses from
    the losers into the winner. The user asked specifically to "keep
    the ones filled with most informations" — if the winner is the
    most-complete row, taking only it preserves the maximum the user
    has — anything else is a heuristic merge that could create the
    wrong final row.
    """
    role = user.get("role")
    uid  = user.get("id")
    status = body.status.strip().lower()
    if status not in ("pending", "spam"):
        raise HTTPException(400, "dedupe is only supported for 'pending' or 'spam'")

    # Pull every row in the bucket — same visibility gate the list
    # endpoint uses. Limit a generous 5000 so a runaway extraction
    # backlog still fits one call.
    rows = _contacts.search(
        "", kind=None, status=status, limit=5000,
        role=role,
        user_id=uid if uid is not None else None,
    )

    # Hydrate so we can score on channel + address counts.
    contacts: list[dict[str, Any]] = []
    for r in rows:
        if not r: continue
        c = _contacts.get(r["id"], role=role,
                          user_id=uid if uid is not None else None)
        if c: contacts.append(c)

    # Group by normalised name. Normalisation: lowercase, collapse
    # whitespace, strip leading/trailing non-alnum. Skip rows with
    # empty names (defensive — search shouldn't return those, but if
    # something stuck "" in we'd group every weird row together).
    import re as _re
    groups: Dict[str, list[dict[str, Any]]] = {}
    for c in contacts:
        name = (c.get("display_name") or "").strip()
        if not name:
            continue
        norm = _re.sub(r"\s+", " ", name).lower().strip()
        norm = _re.sub(r"^[^a-z0-9]+|[^a-z0-9]+$", "", norm)
        if not norm:
            continue
        groups.setdefault(norm, []).append(c)

    deleted_ids: list[int] = []
    kept = 0
    multi_groups = 0
    for norm, members in groups.items():
        if len(members) < 2:
            continue
        multi_groups += 1
        members.sort(key=lambda c: (-_completeness_score(c), int(c["id"])))
        winner = members[0]
        kept += 1
        for loser in members[1:]:
            try:
                _contacts.delete(int(loser["id"]))
                deleted_ids.append(int(loser["id"]))
            except Exception as exc:  # noqa: BLE001
                logging.getLogger("yorik.contacts").warning(
                    "dedupe: couldn't delete contact %s (winner %s, norm %r): %s",
                    loser["id"], winner["id"], norm, exc,
                )

    return {
        "groups":  multi_groups,
        "kept":    kept,
        "deleted": len(deleted_ids),
    }


def _completeness_score(c: dict[str, Any]) -> int:
    """Count populated fields on a contact + its channels/addresses.

    Used by /api/contacts/dedupe to pick the "fullest" row in a
    duplicate group. Each channel and each address row counts as one
    point; top-level scalar fields each count as one when non-empty.
    Notes count for one regardless of length — a long note doesn't
    matter more than a short one, the presence is the signal.
    """
    score = 0
    scalar_fields = (
        "relation", "birthday", "language_pref", "salutation_pref",
        "legal_name", "tax_id", "iban", "payment_terms_days",
        "default_currency", "notes",
    )
    for f in scalar_fields:
        v = c.get(f)
        if v is None: continue
        if isinstance(v, str) and not v.strip(): continue
        score += 1
    score += len(c.get("channels")  or [])
    score += len(c.get("addresses") or [])
    aliases = c.get("aliases") or []
    if aliases: score += 1
    return score


@app.get("/api/contacts/_counts")
def contact_counts(
    user: dict[str, Any] = Depends(_auth.current_user),  # noqa: ARG001
) -> Dict[str, int]:
    """Counts per status — drives the tab badges."""
    return _contacts.status_counts()


@app.post("/api/contacts/yorik-assist/bulk")
def bulk_yorik_assist(
    body: dict[str, Any] = Body(default_factory=dict),
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Bulk-toggle yorik_assist_enabled across a status cohort. Body:
    {scope: 'active'|'pending'|'all', enabled: bool}. Designed for the
    Contacts page header "Enable AI for all active contacts" button —
    saves the user from clicking 200+ rows manually.

    MUST stay declared BEFORE /api/contacts/{contact_id} or FastAPI's
    int-path-param converter will 422 on 'yorik-assist'.
    """
    scope = (body.get("scope") or "active").strip().lower()
    if scope not in ("active", "pending", "all"):
        raise HTTPException(400, "scope must be active|pending|all")
    enabled = bool(body.get("enabled", True))
    role = user.get("role")
    uid = user.get("id")
    # Reuse the same visibility gate the list view uses — never enable
    # AI on a contact the user can't see.
    visible = _contacts.search(
        "", status=None if scope == "all" else scope,
        limit=10000, role=role, user_id=uid if uid is not None else None,
    )
    ids = [int(r["id"]) for r in visible if r and r.get("id") is not None]
    if not ids:
        return {"ok": True, "updated_count": 0, "scope": scope, "enabled": enabled}
    placeholders = ",".join(["?"] * len(ids))
    with conn_ctx(DB_PATH) as c:
        c.execute(
            f"UPDATE contacts SET yorik_assist_enabled=?, updated_at=datetime('now') "
            f"WHERE id IN ({placeholders})",
            (enabled, *ids),
        )
    return {"ok": True, "updated_count": len(ids), "scope": scope, "enabled": enabled}


@app.post("/api/contacts/enrich")
def enrich_contacts(
    user: dict[str, Any] = Depends(_auth.current_user),
    background: BackgroundTasks = None,
) -> Dict[str, Any]:
    """Kick off the LLM-driven contact enricher across every active
    and pending contact. Runs in the background — see Settings →
    Contacts (or the heartbeat returned by /api/dashboard/workers)
    for live progress. Admin-only because it's an expensive LLM job
    that touches every user's contacts.

    MUST stay declared BEFORE /api/contacts/{contact_id} or FastAPI's
    int-path-param converter will 422 on the string "enrich".
    """
    if user.get("role") not in ("admin", "platform_admin"):
        raise HTTPException(403, "admin only")
    from . import contact_enricher as _ce
    background.add_task(_ce.enrich_all)
    return {"queued": True}


@app.post("/api/contacts/enrich-cancel")
def enrich_contacts_cancel(
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Cooperative cancel — sets a flag the enricher checks once per
    contact. The current contact still finishes (in-flight LLM call);
    the walk exits cleanly after that. No-op if nothing is running."""
    if user.get("role") not in ("admin", "platform_admin"):
        raise HTTPException(403, "admin only")
    from . import contact_enricher as _ce
    _ce.request_cancel()
    return {"cancelling": True}


@app.post("/api/contacts/reconcile-whatsapp-pending")
def reconcile_whatsapp_pending(
    user: dict[str, Any] = Depends(_auth.current_user),  # noqa: ARG001
) -> Dict[str, Any]:
    """Walk all pending contacts that own a WhatsApp channel and try
    to merge them into existing phone-channel contacts (from earlier
    vCard imports). Idempotent — runs the same matching logic as the
    live ingest path; nothing to reconcile = no-op.

    MUST stay declared BEFORE /api/contacts/{contact_id} or FastAPI's
    int-path-param converter will 422 on the string "reconcile-...".
    """
    from . import contact_autocapture as _ac
    from .contacts import conn_ctx as _ccx
    # Pull all WhatsApp channels currently parked on pending contacts.
    with _ccx() as c:
        rows = c.execute(
            "SELECT cc.value AS jid, cc.contact_id AS pending_id "
            "FROM contact_channels cc "
            "JOIN contacts ct ON ct.id = cc.contact_id "
            "WHERE cc.kind = 'whatsapp' AND ct.status = 'pending'"
        ).fetchall()
    examined = len(rows)
    merged = 0
    for r in rows:
        jid = r["jid"]
        pending_id = int(r["pending_id"])
        phone_match = _ac._phone_match_for_jid(jid)
        if not phone_match or int(phone_match["id"]) == pending_id:
            continue
        if _ac._attach_wa_to_phone_contact(
            phone_match, jid, log_ctx=f"reconcile pending {pending_id}",
        ):
            merged += 1
    return {"examined": examined, "merged": merged}


@app.get("/api/contacts/{contact_id}")
def get_contact(
    contact_id: int,
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    uid = user.get("id")
    c = _contacts.get(
        contact_id,
        role=user.get("role"),
        user_id=uid if uid is not None else None,
    )
    # contacts.get returns None for both "not found" and "not visible to
    # this user" — same 404 surface so callers can't probe for existence.
    if not c:
        raise HTTPException(status_code=404, detail="contact not found")
    return c


@app.post("/api/contacts/{contact_id}/enrich")
async def enrich_one_contact(
    contact_id: int,
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Run the LLM enricher on a single contact, foreground (synchronous).
    Used when the user wants fresh proposals for the contact they're
    looking at right now — e.g. after a new email arrived from them.
    Cheap: one LLM call. Returns the proposal count so the UI can
    refetch /proposals to pick up the new rows."""
    if user.get("role") not in ("admin", "platform_admin"):
        raise HTTPException(403, "admin only")
    from . import contact_enricher as _ce
    result = await asyncio.to_thread(_ce.enrich_one, contact_id)
    return result


@app.get("/api/contacts/{contact_id}/proposals")
def get_contact_proposals(
    contact_id: int,
    user: dict[str, Any] = Depends(_auth.current_user),  # noqa: ARG001
) -> Dict[str, Any]:
    """Pending LLM proposals for this contact + a cheap scan summary
    so the UI can ALWAYS show "we checked X emails, Y WA, Z docs,
    N events" — even when there are zero proposals. That difference
    ("nothing to suggest" vs "feature is broken") was previously
    invisible to the user; now it's right there next to the Enrich
    button. The summary is SQL-only (no LLM call), ~50ms typically."""
    from .contacts import conn_ctx as _ccx
    from . import contact_enricher as _ce
    with _ccx() as c:
        rows = c.execute(
            "SELECT id, field_name, proposed_value, confidence, source_kind, "
            "       source_ref, source_snippet, created_at "
            "FROM contact_enrichment_proposals "
            "WHERE contact_id=? AND status='pending' "
            "ORDER BY field_name, confidence DESC",
            (contact_id,),
        ).fetchall()
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        d = dict(r)
        grouped.setdefault(d["field_name"], []).append(d)
    sources_available = _ce.count_contact_mentions(contact_id)
    return {
        "contact_id":        contact_id,
        "by_field":          grouped,
        "sources_available": sources_available,
    }


class _ProposalDecision(BaseModel):
    proposal_id: int
    decision:    str  # 'accepted' | 'rejected'


@app.post("/api/contacts/{contact_id}/proposals/decide")
def decide_contact_proposal(
    contact_id: int,
    body: _ProposalDecision,
    user: dict[str, Any] = Depends(_auth.current_user),  # noqa: ARG001
) -> Dict[str, Any]:
    """Mark a proposal accepted or rejected. Called when the user picks
    a dropdown alternative + saves, OR explicitly rejects a suggestion.
    Marking accepted doesn't auto-write the value into the contact —
    the save endpoint does that with whatever's in the form. This is
    purely for audit + "don't re-suggest" on the next enricher run."""
    if body.decision not in ("accepted", "rejected"):
        raise HTTPException(400, "decision must be 'accepted' or 'rejected'")
    from .contacts import conn_ctx as _ccx
    with _ccx() as c:
        cur = c.execute(
            "UPDATE contact_enrichment_proposals "
            "SET status=?, decided_at=datetime('now') "
            "WHERE id=? AND contact_id=?",
            (body.decision, body.proposal_id, contact_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "proposal not found")
    return {"ok": True, "id": body.proposal_id, "decision": body.decision}


# ── Top-down contact extraction (Settings → Contacts → Extracted) ─────
#
# These are the "we walked your Paperless docs and found contacts"
# proposals. Parallel API surface to the per-contact enrichment
# proposals above — different table, different shape (one row per
# source doc, JSON payload of fields, optional match candidate). The
# extractor itself runs as a background worker; these endpoints just
# list / accept / reject the queue. Admin only — proposals reference
# every doc in the user's Paperless library, which is the household's
# private data shape.

# The shared address/channel writer used to live here. It moved to
# backend/contact_extractor.write_extracted_sides() so the new
# scanner-side write_pending_contact_from_doc() and the legacy
# accept-create flow below can both call it without circular imports.


class _ExtractionDecision(BaseModel):
    decision: str  # 'accept_create' | 'accept_merge' | 'reject'
    # When decision=accept_merge, override the proposal's auto-detected
    # match. None means "use the match_candidate_id the extractor stored."
    merge_into_contact_id: Optional[int] = None


@app.get("/api/contact-extractions", tags=["contacts"])
def list_contact_extractions(
    status: str = "pending",
    limit: int = 100,
    user: Dict[str, Any] = Depends(_auth.require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """Return the contact_extraction_proposals queue. Defaults to
    pending only — that's what the admin Settings panel surfaces.

    Each row carries the proposed contact (JSON-decoded for direct
    rendering), the match candidate (if any) joined to the existing
    contact's display_name + business kind, and the source doc id so
    the UI can deep-link into Paperless for the "open the source"
    button."""
    if status not in ("pending", "accepted", "rejected", "merged", "skipped", "all"):
        raise HTTPException(400, "status must be pending/accepted/rejected/merged/skipped/all")
    where = "" if status == "all" else "WHERE p.status = ?"
    params: list[Any] = [] if status == "all" else [status]
    params.append(int(limit))
    with conn_ctx(DB_PATH) as conn:
        rows = conn.execute(
            f"""
            SELECT p.id, p.source_paperless_doc_id, p.proposed_json,
                   p.match_candidate_id, p.match_score, p.match_reason,
                   p.status, p.created_contact_id, p.created_at, p.decided_at,
                   c.display_name AS match_display_name,
                   c.kind         AS match_kind
              FROM contact_extraction_proposals p
              LEFT JOIN contacts c ON c.id = p.match_candidate_id
              {where}
          ORDER BY p.created_at DESC
             LIMIT ?
            """,
            tuple(params),
        ).fetchall()
        # Counts for the admin UI badge.
        counts = {
            r["status"]: int(r["n"])
            for r in conn.execute(
                "SELECT status, COUNT(*) AS n "
                "FROM contact_extraction_proposals GROUP BY status"
            ).fetchall()
        }
    items: list[Dict[str, Any]] = []
    for r in rows:
        try:
            proposed = json.loads(r["proposed_json"])
        except Exception:  # noqa: BLE001
            proposed = {}
        items.append({
            "id":                      int(r["id"]),
            "source_paperless_doc_id": int(r["source_paperless_doc_id"]),
            "proposed":                proposed,
            "match_candidate_id":      r["match_candidate_id"],
            "match_display_name":      r["match_display_name"],
            "match_kind":              r["match_kind"],
            "match_score":             r["match_score"],
            "match_reason":            r["match_reason"],
            "status":                  r["status"],
            "created_contact_id":      r["created_contact_id"],
            "created_at":              r["created_at"],
            "decided_at":              r["decided_at"],
        })
    return {"items": items, "counts": counts}


@app.post("/api/contact-extractions/{proposal_id}/decide", tags=["contacts"])
def decide_contact_extraction(
    proposal_id: int,
    body: _ExtractionDecision,
    user: Dict[str, Any] = Depends(_auth.require_admin),
) -> Dict[str, Any]:
    """Accept (create new contact OR merge into existing) or reject one
    proposal. Returns the resulting contact id when accepted, plus the
    final proposal status.

    accept_create — INSERT a new contacts row populated from
                    proposed_json. emails/phones go into the side
                    tables via the existing contacts module.
    accept_merge  — UPDATE the matched (or override) contact with
                    any missing fields from proposed_json. Never
                    overwrites a non-NULL existing value.
    reject        — status='rejected', no DB write to contacts.
    """
    if body.decision not in ("accept_create", "accept_merge", "reject"):
        raise HTTPException(400, "decision must be accept_create / accept_merge / reject")
    user_id = user["id"] if user and user.get("id") else None
    with conn_ctx(DB_PATH) as conn:
        row = conn.execute(
            "SELECT id, source_paperless_doc_id, proposed_json, "
            "       match_candidate_id, status "
            "FROM contact_extraction_proposals WHERE id = ?",
            (proposal_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "proposal not found")
    if row["status"] != "pending":
        raise HTTPException(409, f"already decided ({row['status']})")
    try:
        proposed = json.loads(row["proposed_json"]) if row["proposed_json"] else {}
    except Exception:  # noqa: BLE001
        proposed = {}

    if body.decision == "reject":
        with conn_ctx(DB_PATH) as conn:
            conn.execute(
                "UPDATE contact_extraction_proposals "
                "SET status='rejected', decided_at=datetime('now'), "
                "    decided_by_user_id=? "
                "WHERE id = ?",
                (user_id, proposal_id),
            )
            conn.commit()
        return {"ok": True, "id": proposal_id, "status": "rejected",
                "contact_id": None}

    # ── accept paths ──
    target_id: Optional[int] = (body.merge_into_contact_id
                                if body.decision == "accept_merge"
                                else None)
    if body.decision == "accept_merge" and target_id is None:
        target_id = row["match_candidate_id"]
    if body.decision == "accept_merge" and target_id is None:
        raise HTTPException(
            400,
            "accept_merge needs merge_into_contact_id (or a match_candidate "
            "on the proposal; this one has none)",
        )

    # Top-level contacts columns the proposal might populate. emails /
    # phones / address all live in side tables and are handled below
    # in _write_extracted_sides — without that helper, addresses and
    # channels were silently dropped when accepting a proposal, which
    # produced the "I accepted Landkreis München but it has no info"
    # bug report.
    CORE_COLS = (
        "display_name", "kind", "legal_name", "tax_id", "iban",
        "salutation_pref",
    )
    from . import contact_extractor as _cx_helpers
    new_contact_id: Optional[int] = None
    with conn_ctx(DB_PATH) as conn:
        if body.decision == "accept_create":
            display_name = proposed.get("display_name") or proposed.get("business_name")
            if not display_name:
                raise HTTPException(
                    400,
                    "proposal lacks display_name — reject or edit it first",
                )
            kind = proposed.get("kind", "business")
            # status='pending' so the new contact lands in the
            # Contacts → Pending tab (the existing review-and-promote
            # surface used for auto-captured email/wa contacts). The
            # extraction queue was confusing the user with a
            # second screen; "Pending" is one screen everyone already
            # knows.
            # Phase C: default new contacts to WS1's Household so
            # member-role users see them. Without an explicit space_id
            # the row gets excluded by spaces.row_filter for everyone
            # except platform_admin.
            hh_row = conn.execute(
                "SELECT id FROM spaces WHERE slug='household' LIMIT 1"
            ).fetchone()
            hh_space_id = int(hh_row[0]) if hh_row else None
            cur = conn.execute(
                "INSERT INTO contacts (display_name, kind, status, space_id) "
                "VALUES (?, ?, 'pending', ?)",
                (display_name, kind, hh_space_id),
            )
            new_contact_id = int(cur.lastrowid)
            target_id = new_contact_id
            # Then patch the top-level columns as if it were a merge.
            for col in CORE_COLS:
                if col in ("display_name", "kind"):
                    continue
                val = proposed.get(col)
                if val:
                    conn.execute(
                        f"UPDATE contacts SET {col} = ? WHERE id = ?",
                        (val, new_contact_id),
                    )
            _cx_helpers.write_extracted_sides(conn, target_id, proposed,
                                    overwrite_address=True)
        else:
            # Merge: only set fields that are currently NULL on the target.
            existing = conn.execute(
                "SELECT " + ", ".join(CORE_COLS) +
                " FROM contacts WHERE id = ?",
                (target_id,),
            ).fetchone()
            if not existing:
                raise HTTPException(404, f"merge target {target_id} not found")
            for col in CORE_COLS:
                val = proposed.get(col)
                if val and not (existing[col] or "").strip():
                    conn.execute(
                        f"UPDATE contacts SET {col} = ? WHERE id = ?",
                        (val, target_id),
                    )
            # For merge, NEVER overwrite an existing address — only add
            # missing channels (emails/phones not already on the target).
            _cx_helpers.write_extracted_sides(conn, target_id, proposed,
                                    overwrite_address=False)

        # Final state: mark the proposal accepted/merged and stamp the
        # contact id so the admin can navigate back from the queue.
        conn.execute(
            "UPDATE contact_extraction_proposals "
            "SET status=?, decided_at=datetime('now'), "
            "    decided_by_user_id=?, created_contact_id=? "
            "WHERE id = ?",
            (
                "accepted" if body.decision == "accept_create" else "merged",
                user_id,
                target_id,
                proposal_id,
            ),
        )
        conn.commit()

    return {
        "ok":         True,
        "id":         proposal_id,
        "status":     "accepted" if body.decision == "accept_create" else "merged",
        "contact_id": target_id,
    }


@app.post("/api/contact-extractions/run", tags=["contacts"])
async def run_contact_extractions(
    _user: Dict[str, Any] = Depends(_auth.require_admin),
) -> Dict[str, Any]:
    """Kick off a full Paperless-corpus scan. Returns immediately —
    the scan runs as a background task on the request loop. Subsequent
    triggers while a run is already in flight return {already_running:
    true} rather than 409, so the UI can treat them as "no-op,
    everything's fine."

    Progress is observable two ways:
      - GET /api/contacts/extractions/status — current is_running
        flag + last summary + pending count, intended for polling.
      - GET /api/contacts/extractions — the queue itself grows as
        proposals are written.
    """
    from . import contact_extractor as _cx
    if _cx.is_running():
        return {"started": False, "already_running": True}
    # Fire-and-forget. The task is scheduled on the same loop the
    # request was served on; the lock inside run_manual handles
    # serialisation if two admins click within the same millisecond.
    import asyncio as _asyncio
    _asyncio.create_task(_cx.run_manual(), name="contact-extractor-manual")
    return {"started": True, "already_running": False}


@app.get("/api/contact-extractions/status", tags=["contacts"])
def contact_extractions_status(
    _user: Dict[str, Any] = Depends(_auth.require_admin),
) -> Dict[str, Any]:
    """Polled by the Contacts-app button so it can render a real
    progress bar while a scan is running AND show the most recent
    summary when idle. Cheap: one COUNT(*) + two in-memory dict
    lookups."""
    from . import contact_extractor as _cx
    with conn_ctx(DB_PATH) as conn:
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM contact_extraction_proposals "
            "WHERE status='pending'"
        ).fetchone()["n"]
    return {
        "running":          _cx.is_running(),
        "pending":          int(pending),
        "progress":         _cx.get_progress(),
        "last_run_summary": _cx.last_run_summary(),
    }


@app.post("/api/contact-extractions/stop", tags=["contacts"])
def stop_contact_extractions(
    _user: Dict[str, Any] = Depends(_auth.require_admin),
) -> Dict[str, Any]:
    """Ask the in-flight scan to stop at the next document boundary.
    Returns immediately with {requested: bool} — bool is False when
    nothing was running. Actual stop is observed via /status as
    `running` flips False."""
    from . import contact_extractor as _cx
    if not _cx.is_running():
        return {"requested": False, "reason": "not_running"}
    _cx.request_cancel()
    return {"requested": True}


@app.post("/api/contacts", status_code=201)
def create_contact(
    body: _ContactIn,
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    # Resolve optional space override (slug like 'household' / 'finance'
    # or a numeric id). Falls back to creator's personal in contacts.create.
    space_id = None
    if body.space:
        s = body.space.strip()
        with conn_ctx(DB_PATH) as _c:
            if s.isdigit():
                r = _c.execute("SELECT id FROM spaces WHERE id=?", (int(s),)).fetchone()
            else:
                r = _c.execute("SELECT id FROM spaces WHERE slug=?", (s.lower(),)).fetchone()
        if r is None:
            raise HTTPException(400, detail=f"unknown space {body.space!r}")
        space_id = int(r["id"])
    try:
        cid = _contacts.create(
            display_name=body.display_name, kind=body.kind, status=body.status,
            first_name=body.first_name, last_name=body.last_name,
            role=body.role, employer_contact_id=body.employer_contact_id,
            aliases=body.aliases, relation=body.relation, birthday=body.birthday,
            language_pref=body.language_pref, salutation_pref=body.salutation_pref,
            legal_name=body.legal_name, tax_id=body.tax_id, iban=body.iban,
            payment_terms_days=body.payment_terms_days,
            default_currency=body.default_currency, notes=body.notes,
            tags=body.tags, space_id=space_id,
            created_by_user_id=user.get("id"), source="manual",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _contacts.get(cid)


@app.post("/api/contacts/parse-blob")
async def contacts_parse_blob(
    text: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Smart-add contact: take free text OR a PDF/image, return the
    extracted contact fields (display_name, kind, address_*, emails,
    phones, ...) ready to pre-fill the add-contact form. The user
    reviews and confirms; the actual contact creation goes through
    POST /api/contacts.

    Inputs — pick one (or both; text wins if both present):
      - text: free-text paste (business card, search result, signature).
      - file: PDF or image. PDF → first try text extract; if empty
              (scanned), rasterize each page as PNG and dispatch to the
              vision LLM. Image → single multimodal LLM call. .txt /
              .docx → text extract then text path.

    Returns: {"extracted": {flat schema}, "source": "text"|"image"|"pdf"|"pdf-vision"}.
    """
    # LLM-offline early refusal. parse-blob always calls the LLM
    # (text path uses the chat model for extraction; PDF/image paths
    # also). Without this guard the 30s connect timeout silently
    # holds the request open before surfacing a 502, which the user
    # reads as "Yorik is broken." Mirrors /api/ask's _llm_probe gate.
    if not _llm_reachable():
        probe = _llm_probe()
        raise HTTPException(
            503,
            f"language model unreachable ({probe['reason']}) — "
            "start the local LLM and retry",
        )

    from . import contact_extractor as _ce
    from . import documents as _docs_mod
    import base64

    blob_text = (text or "").strip()
    source = "text"

    if file is not None:
        raw = await file.read()
        if len(raw) > 50 * 1024 * 1024:
            raise HTTPException(413, "file too large (50 MB max)")
        ct = (file.content_type or "").lower()
        suffix = (Path(file.filename or "upload").suffix or ".bin").lower()

        if ct.startswith("image/") or suffix in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            # Vision path: single image → one multimodal LLM call.
            mime = ct if ct.startswith("image/") else f"image/{suffix.lstrip('.').replace('jpg','jpeg')}"
            b = base64.b64encode(raw).decode()
            image_payloads = [{
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b}"},
            }]
            extracted = _ce._llm_extract_full_vision(image_payloads, hint_text=blob_text)
            return {"extracted": extracted, "source": "image"}

        if "pdf" in ct or suffix == ".pdf":
            # PDF: try text extract first; fall through to vision if no text.
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(raw)
                tmp_path = Path(tmp.name)
            try:
                try:
                    pdf_text = _docs_mod.extract_text(tmp_path, ct) or ""
                except Exception:  # noqa: BLE001
                    pdf_text = ""
                if pdf_text.strip():
                    blob_text = pdf_text
                    source = "pdf"
                else:
                    # Scanned PDF → vision pipeline. Render pages as PNG
                    # mirroring read_document_vision (pdftoppm @ 150 DPI,
                    # cap at first 5 pages to keep the LLM tokens sane).
                    image_payloads = _render_pdf_pages_for_vision(tmp_path, max_pages=5)
                    if not image_payloads:
                        raise HTTPException(
                            422,
                            "could not extract text from PDF and could not "
                            "rasterize it for vision (pdftoppm missing?)",
                        )
                    extracted = _ce._llm_extract_full_vision(
                        image_payloads, hint_text=(text or "")[:500],
                    )
                    return {"extracted": extracted, "source": "pdf-vision"}
            finally:
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

        elif suffix in (".txt", ".md", ".docx", ".doc", ".rtf"):
            # Plain-text-ish file → reuse extract_text then fall into the
            # text path below.
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp.write(raw)
                tmp_path = Path(tmp.name)
            try:
                try:
                    extracted_text = _docs_mod.extract_text(tmp_path, ct) or ""
                except Exception as exc:  # noqa: BLE001
                    raise HTTPException(415, f"could not read {file.filename!r}: {exc}")
                blob_text = (extracted_text or "").strip()
                source = "file-text"
            finally:
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

        else:
            raise HTTPException(
                415,
                f"unsupported file type {ct!r}/{suffix!r}. Send text, "
                "an image (PNG/JPG/WEBP), or a PDF.",
            )

    if not blob_text:
        raise HTTPException(
            400,
            "no text and no parseable file provided. Send `text` form "
            "field, or upload a file as `file`.",
        )

    # Text path: pasted text, txt/md/docx content, or PDF-extracted text.
    extracted = _ce._llm_extract_full_smart_text(blob_text)
    return {"extracted": extracted, "source": source}


def _render_pdf_pages_for_vision(pdf_path: Path, *, max_pages: int = 5) -> List[Dict[str, Any]]:
    """Rasterize the first N pages of a PDF as PNG and wrap each as an
    OpenAI multimodal image_url payload. Returns [] when pdftoppm is
    missing or the PDF has no pages.

    Mirrors backend.skills.read_document_vision's helper but is local
    to main.py since the smart-add endpoint only needs the small
    common case (1-5 pages of a scanned contact source).
    """
    import base64
    import glob
    import shutil
    import subprocess
    import tempfile as _tf
    if not shutil.which("pdftoppm"):
        return []
    payloads: List[Dict[str, Any]] = []
    with _tf.TemporaryDirectory(prefix="ycpv-") as tmp:
        prefix = str(Path(tmp) / "page")
        try:
            subprocess.run(
                ["pdftoppm", "-png", "-r", "150", "-l", str(max_pages),
                 str(pdf_path), prefix],
                check=True, capture_output=True, timeout=60,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return []
        for p in sorted(glob.glob(f"{prefix}-*.png"))[:max_pages]:
            try:
                b = base64.b64encode(open(p, "rb").read()).decode()
            except OSError:
                continue
            payloads.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b}"},
            })
    return payloads


@app.patch("/api/contacts/{contact_id}")
def patch_contact(
    contact_id: int,
    body: _ContactPatch,
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    # Owner / role-allowlist / share gate — must hold can_edit (or be admin)
    # to mutate. Closes the audit finding where any logged-in user could
    # rename / delete admin's contacts.
    from . import calendars as _cal
    from .contacts import get as _get_contact
    raw = _get_contact(contact_id, include_children=False)
    if raw is None:
        raise HTTPException(status_code=404, detail="contact not found")
    try:
        uid = user.get("id")
        _cal.require_contact_access(
            user.get("role"), uid if uid is not None else None, raw, action="edit",
        )
    except _cal.RowOwnerPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    fields = {k: v for k, v in body.model_dump(exclude_unset=True).items()}
    # Phase B: `space` (slug or numeric id) → resolve to space_id before
    # passing to contacts.update (which accepts space_id, not space).
    if "space" in fields:
        s = (fields.pop("space") or "").strip().lower()
        if s:
            with conn_ctx(DB_PATH) as _c:
                if s.isdigit():
                    r = _c.execute("SELECT id FROM spaces WHERE id=?", (int(s),)).fetchone()
                else:
                    r = _c.execute("SELECT id FROM spaces WHERE LOWER(slug)=?", (s,)).fetchone()
            if r is None:
                raise HTTPException(400, detail=f"unknown space {s!r}")
            fields["space_id"] = int(r["id"])
    if not fields:
        return _contacts.get(contact_id) or {}
    try:
        _contacts.update(contact_id, **fields)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _contacts.get(contact_id) or {}


@app.delete("/api/contacts/{contact_id}", status_code=204, response_class=Response)
def delete_contact(
    contact_id: int,
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Response:
    from . import calendars as _cal
    from .contacts import get as _get_contact
    raw = _get_contact(contact_id, include_children=False)
    if raw is not None:
        # Mirror PATCH's gate. Missing rows still 204-no-op so deletes
        # stay idempotent under retry.
        try:
            uid = user.get("id")
            _cal.require_contact_access(
                user.get("role"), uid if uid is not None else None, raw, action="edit",
            )
        except _cal.RowOwnerPermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
    try:
        _contacts.delete(contact_id)
    except ValueError:
        pass
    return Response(status_code=204)


@app.post("/api/contacts/{contact_id}/promote")
def promote_contact(
    contact_id: int,
    user: dict[str, Any] = Depends(_auth.current_user),  # noqa: ARG001
) -> Dict[str, Any]:
    if not _contacts.get(contact_id, include_children=False):
        raise HTTPException(404, "contact not found")
    _contacts.promote_pending(contact_id)
    return _contacts.get(contact_id) or {}


# ───────────────────── Pin / unpin (mig 025) ─────────────────────
# Manual override that bubbles a contact to the top of the sidebar
# regardless of last_interaction_at recency. Drives the "★ Pinned"
# group above "Recent".

class ContactPinIn(BaseModel):
    pinned: bool


@app.post("/api/contacts/{contact_id}/pin")
def pin_contact(
    contact_id: int,
    body: ContactPinIn,
    user: dict[str, Any] = Depends(_auth.current_user),  # noqa: ARG001
) -> Dict[str, Any]:
    if not _contacts.get(contact_id, include_children=False):
        raise HTTPException(404, "contact not found")
    try:
        with conn_ctx(DB_PATH) as conn:
            conn.execute(
                "UPDATE contacts SET pinned = ?, updated_at = datetime('now') "
                "WHERE id = ?",
                (1 if body.pinned else 0, contact_id),
            )
    except sqlite3.OperationalError:
        # Pre-025 DB — column missing.
        raise HTTPException(
            503,
            "pinned column missing — restart uvicorn to run pending migrations.",
        )
    return _contacts.get(contact_id) or {}


# ───────────────────── Contact activity timeline ─────────────────────
# Aggregates everything Yorik knows about this contact: emails (sent
# OR received via any of their email channels), calendar events
# (where they're named in `person` or match an alias), and compose
# drafts (where they're the recipient). Used by the new read-mode
# ContactView card so the user sees "last seen: email reply Tuesday"
# instead of guessing.
#
# Best-effort per source — a missing table or schema variant returns
# an empty array for that source, not a 500.

@app.get("/api/contacts/{contact_id}/employees")
def list_contact_employees(
    contact_id: int,
    user: dict[str, Any] = Depends(_auth.current_user),
) -> List[Dict[str, Any]]:
    """List person contacts whose employer_contact_id points at this row.

    Used by ContactView to render the "People at this business" section.
    Empty list when nothing's linked — caller hides the section. Uses
    _contacts.get() on each row so the visibility gate (role / share)
    applies per person; the requesting user only sees employees they
    have visibility on.
    """
    # First verify the user can see the business itself.
    role = user.get("role")
    uid = user.get("id")
    biz = _contacts.get(
        contact_id, role=role,
        user_id=uid if uid is not None else None,
    )
    if not biz:
        raise HTTPException(404, "contact not found")
    with conn_ctx(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id FROM contacts "
            "WHERE employer_contact_id = ? AND status = 'active' "
            "ORDER BY first_name, last_name, display_name",
            (contact_id,),
        ).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        c = _contacts.get(
            int(r["id"]), role=role,
            user_id=uid if uid is not None else None,
        )
        if c:
            out.append(c)
    return out


@app.get("/api/contacts/{contact_id}/timeline")
def contact_timeline(
    contact_id: int,
    limit: int = Query(20, ge=1, le=80),
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    c = _contacts.get(contact_id, include_children=True)
    if not c:
        raise HTTPException(404, "contact not found")

    display = c.get("display_name") or ""
    aliases = c.get("aliases") or []
    email_addrs = [
        ch.get("value", "").lower()
        for ch in (c.get("channels") or [])
        if ch.get("kind") == "email" and ch.get("value")
    ]
    items: List[Dict[str, Any]] = []

    with conn_ctx(DB_PATH) as conn:
        # ── Emails — match any of the contact's email channels in
        # from_email OR to_addrs (JSON list, scanned with LIKE).
        if email_addrs:
            placeholders = ",".join("?" * len(email_addrs))
            params: list = [u for u in email_addrs]
            like_clauses = []
            for em in email_addrs:
                like_clauses.append("LOWER(IFNULL(to_addrs,'')) LIKE ?")
                params.append(f"%{em}%")
            try:
                rows = conn.execute(
                    f"SELECT id, subject, snippet, from_email, from_name, "
                    f"       to_addrs, date_received, is_sent "
                    f"FROM email_messages "
                    f"WHERE owner_user_id = ? AND ("
                    f"  LOWER(from_email) IN ({placeholders}) OR "
                    f"  {' OR '.join(like_clauses)}"
                    f") "
                    f"ORDER BY date_received DESC LIMIT ?",
                    [user["id"], *params, int(limit)],
                ).fetchall()
                for r in rows:
                    items.append({
                        "kind":      "email",
                        "when":      r["date_received"],
                        "title":     r["subject"] or "(no subject)",
                        "sub":       (r["snippet"] or "")[:120],
                        "link":      f"/email?msg={r['id']}",
                        "direction": "outgoing" if r["is_sent"] else "incoming",
                    })
            except sqlite3.OperationalError:
                pass

        # ── Events — naive LIKE on `person` against display_name + aliases.
        # The `person` field is free text ("Dr. Wiese", "Hans Müller, Tobias"),
        # so substring matching is good enough for v1.
        name_needles = [display, *aliases]
        name_needles = [n.strip() for n in name_needles if n and n.strip()]
        if name_needles:
            ev_clauses = " OR ".join(["LOWER(IFNULL(person,'')) LIKE ?"] * len(name_needles))
            ev_params = [f"%{n.lower()}%" for n in name_needles]
            try:
                rows = conn.execute(
                    f"SELECT id, title, starts_at, location "
                    f"FROM events "
                    f"WHERE ({ev_clauses}) "
                    f"ORDER BY starts_at DESC LIMIT ?",
                    [*ev_params, int(limit)],
                ).fetchall()
                for r in rows:
                    items.append({
                        "kind":  "event",
                        "when":  r["starts_at"],
                        "title": r["title"] or "(no title)",
                        "sub":   r["location"] or "",
                        "link":  f"/calendar?date={(r['starts_at'] or '')[:10]}",
                    })
            except sqlite3.OperationalError:
                pass

        # ── Compose drafts — LIKE on the free-text recipient field.
        if display:
            try:
                rows = conn.execute(
                    "SELECT id, kind AS draft_kind, subject, recipient, "
                    "       created_at "
                    "FROM compose_drafts "
                    "WHERE user_id = ? "
                    "AND LOWER(IFNULL(recipient,'')) LIKE ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (user["id"], f"%{display.lower()}%", int(limit)),
                ).fetchall()
                for r in rows:
                    items.append({
                        "kind":  "draft",
                        "when":  r["created_at"],
                        "title": r["subject"] or f"Draft ({r['draft_kind']})",
                        "sub":   f"to {r['recipient']}" if r['recipient'] else "",
                        "link":  f"/compose?draft_id={r['id']}",
                    })
            except sqlite3.OperationalError:
                pass

    # Merge & sort by `when` DESC. Treat missing dates as oldest.
    items.sort(key=lambda x: (x.get("when") or "0"), reverse=True)
    return {
        "contact_id":  contact_id,
        "items":       items[:limit],
        "total":       len(items),
        "by_kind":     {
            "email":  sum(1 for i in items if i["kind"] == "email"),
            "event":  sum(1 for i in items if i["kind"] == "event"),
            "draft":  sum(1 for i in items if i["kind"] == "draft"),
        },
    }


# ───────────────────── vCard import ─────────────────────
# Two stateless endpoints used by both the Contacts UI's "Import .vcf"
# modal and the chat composer's vCard drop. preview/ parses + builds
# a per-card plan (new / merge / name_conflict); apply/ takes that
# plan back and executes it. No server-side cache between the calls
# — keeps the request lifecycle short and skips the need for a
# vcard_import_plans table.

class VcardImportPreviewIn(BaseModel):
    text: str  # raw .vcf contents


class VcardImportApplyIn(BaseModel):
    plan: Dict[str, Any]              # the blob returned by /preview
    target_status: str = "pending"    # 'pending' | 'active'


@app.post("/api/contacts/import/preview")
def vcard_import_preview(
    body: VcardImportPreviewIn,
    user: dict[str, Any] = Depends(_auth.current_user),  # noqa: ARG001
) -> Dict[str, Any]:
    from . import contacts_import as _ci
    cards = _ci.parse_vcards(body.text or "")
    if not cards:
        raise HTTPException(
            status_code=400,
            detail="no vCards found (or file unreadable)",
        )
    plan = _ci.plan_import(cards)
    return _ci.plan_to_jsonable(plan)


@app.post("/api/contacts/import/apply")
def vcard_import_apply(
    body: VcardImportApplyIn,
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    from . import contacts_import as _ci
    if body.target_status not in ("pending", "active"):
        raise HTTPException(
            status_code=400,
            detail="target_status must be 'pending' or 'active'",
        )
    plan = _ci.plan_from_jsonable(body.plan or {})
    result = _ci.apply_import(
        plan,
        target_status=body.target_status,
        user_id=(user or {}).get("id"),
    )
    return {
        "created":      len(result.created_ids),
        "merged":       len(result.merged_ids),
        "skipped":      result.skipped,
        "errors":       result.errors,
        "created_ids":  result.created_ids,
        "merged_ids":   result.merged_ids,
    }


@app.post("/api/contacts/{contact_id}/spam")
def spam_contact(
    contact_id: int,
    user: dict[str, Any] = Depends(_auth.current_user),  # noqa: ARG001
) -> Dict[str, Any]:
    if not _contacts.get(contact_id, include_children=False):
        raise HTTPException(404, "contact not found")
    _contacts.mark_spam(contact_id)
    return _contacts.get(contact_id) or {}


@app.post("/api/contacts/{contact_id}/channels", status_code=201)
def add_contact_channel_route(
    contact_id: int,
    body: _ChannelIn,
    user: dict[str, Any] = Depends(_auth.current_user),  # noqa: ARG001
) -> Dict[str, Any]:
    if not _contacts.get(contact_id, include_children=False):
        raise HTTPException(404, "contact not found")
    try:
        _contacts.add_channel(contact_id, kind=body.kind, value=body.value,
                              label=body.label, source="manual")
    except sqlite3.IntegrityError:
        existing = _contacts.find_by_channel(body.kind, body.value)
        owner = existing["display_name"] if existing else "another contact"
        raise HTTPException(409, f"channel already linked to {owner}")
    return _contacts.get(contact_id) or {}


@app.delete("/api/contacts/channels/{channel_id}", status_code=204, response_class=Response)
def remove_contact_channel_route(
    channel_id: int,
    user: dict[str, Any] = Depends(_auth.current_user),  # noqa: ARG001
) -> Response:
    _contacts.remove_channel(channel_id)
    return Response(status_code=204)


@app.post("/api/contacts/{contact_id}/addresses", status_code=201)
def add_contact_address_route(
    contact_id: int,
    body: _AddressIn,
    user: dict[str, Any] = Depends(_auth.current_user),  # noqa: ARG001
) -> Dict[str, Any]:
    if not _contacts.get(contact_id, include_children=False):
        raise HTTPException(404, "contact not found")
    try:
        _contacts.add_address(
            contact_id, kind=body.kind, line1=body.line1, line2=body.line2,
            postcode=body.postcode, city=body.city, region=body.region,
            country=body.country, label=body.label, source="manual",
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _contacts.get(contact_id) or {}


@app.delete("/api/contacts/addresses/{address_id}", status_code=204, response_class=Response)
def remove_contact_address_route(
    address_id: int,
    user: dict[str, Any] = Depends(_auth.current_user),  # noqa: ARG001
) -> Response:
    _contacts.remove_address(address_id)
    return Response(status_code=204)


@app.get("/api/contacts/by-channel/{kind}/{value:path}")
def get_contact_by_channel_route(
    kind: str,
    value: str,
    user: dict[str, Any] = Depends(_auth.current_user),  # noqa: ARG001
) -> Dict[str, Any]:
    """Look up a contact by exact channel value. Powers the WhatsApp /
    Email inbound banners ("👥 Anna is a pending contact" → confirm /
    spam). Returns 404 if no contact owns this channel — caller treats
    that as "unknown sender, offer to add."
    """
    hit = _contacts.find_by_channel(kind, value)
    if not hit:
        raise HTTPException(404, "no contact owns this channel")
    return hit


@app.post("/api/contacts/seed-from-whatsapp")
def seed_contacts_from_whatsapp(
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Backfill — turn every existing 1:1 WhatsApp chat into a pending
    contact. Idempotent (skips senders already in contacts). Useful for
    the first run after the contacts hub goes live against a historical
    WhatsApp archive."""
    from . import contact_autocapture
    return contact_autocapture.seed_from_whatsapp_history(owner_user_id=user.get("id"))


@app.post("/api/contacts/backfill-whatsapp-names")
def backfill_whatsapp_names(
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Refresh display_name for contacts that still show a raw phone
    number / JID prefix. Pulls wa_chats.name (preferred) or the latest
    wa_messages.push_name as the new name. Idempotent — only touches
    rows whose display_name is purely digits."""
    from . import contact_autocapture
    return contact_autocapture.backfill_whatsapp_display_names(
        owner_user_id=user.get("id"),
    )


@app.get("/api/contacts/{contact_id}/address-suggestions")
def address_suggestions(
    contact_id: int,
    user: dict[str, Any] = Depends(_auth.current_user),  # noqa: ARG001
) -> Dict[str, Any]:
    """Return cached address suggestions (does NOT run the LLM). Empty
    candidates + null scraped_at means "never scraped — show the
    Search button." Use POST /scrape-addresses to actually run it.
    """
    from . import contact_address_scraper
    return contact_address_scraper.scrape_and_cache(contact_id, use_cache=True)


@app.post("/api/contacts/{contact_id}/scrape-addresses")
def scrape_addresses(
    contact_id: int,
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Run the on-demand LLM address scrape. Burns ~3-5s of qwen time.
    Triggered by the user explicitly clicking "Search messages for
    an address" in the address editor. Subsequent GETs return the
    cached result.
    """
    from . import contact_address_scraper
    return contact_address_scraper.scrape_and_cache(
        contact_id,
        owner_user_id=user.get("id"),
        use_cache=False,
    )


@app.get("/api/contacts/{contact_id}/birthday-suggestion")
def birthday_suggestion_for_contact(
    contact_id: int,
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Detect the contact's birthday from WhatsApp history.

    For each WhatsApp channel on the contact, look at the matching
    wa_chats row and find messages YOU sent containing birthday-greeting
    phrases ("happy birthday", "alles gute zum geburtstag", …). Take
    the (month, day) of each match — the most common pair across all
    years is the birthday. Returns confidence as `evidence_count`.

    Empty result {detected: null} means: not enough data or no WA
    history. Frontend shows the suggestion as a one-click pill.
    """
    c = _contacts.get(contact_id, include_children=True)
    if not c:
        raise HTTPException(404, "contact not found")

    wa_numbers = [ch["value"] for ch in c.get("channels", []) if ch["kind"] == "whatsapp"]
    if not wa_numbers:
        return {"detected": None, "reason": "no_whatsapp_channel"}

    jids = [f"{n}@s.whatsapp.net" for n in wa_numbers]
    owner_id = user.get("id") or 0

    # Birthday-greeting phrases across the languages Yorik commonly
    # sees in this household. Match case-insensitively.
    phrases = [
        "happy birthday", "happy bday",
        "alles gute zum geburtstag", "herzlichen glückwunsch zum geburtstag",
        "alles liebe zum geburtstag", "geburtstagsgrüße",
        "buon compleanno", "tanti auguri",
        "joyeux anniversaire", "bon anniversaire",
        "feliz cumpleaños",
        "wszystkiego najlepszego", "sto lat",
    ]
    like_clauses = " OR ".join(["LOWER(text) LIKE ?"] * len(phrases))
    params: List[Any] = []
    for p in phrases:
        params.append(f"%{p}%")

    rows: List[Any] = []
    with conn_ctx() as conn:
        placeholders = ",".join("?" * len(jids))
        sql = f"""
            SELECT timestamp
            FROM wa_messages
            WHERE chat_jid IN ({placeholders})
              AND from_me = 1
              AND owner_user_id = ?
              AND text IS NOT NULL
              AND ({like_clauses})
        """
        rows = conn.execute(sql, [*jids, owner_id, *params]).fetchall()

    if not rows:
        return {"detected": None, "reason": "no_matches"}

    # Bucket by (month, day) — the year drifts, the day doesn't.
    from collections import Counter
    from datetime import datetime as _dt
    counter: Counter = Counter()
    years_seen: dict[tuple[int, int], set[int]] = {}
    for r in rows:
        ts = int(r["timestamp"] or 0)
        if ts <= 0:
            continue
        try:
            d = _dt.fromtimestamp(ts)
        except (OverflowError, OSError, ValueError):
            continue
        key = (d.month, d.day)
        counter[key] += 1
        years_seen.setdefault(key, set()).add(d.year)

    if not counter:
        return {"detected": None, "reason": "no_dated_matches"}

    (best_month, best_day), best_count = counter.most_common(1)[0]
    years = sorted(years_seen[(best_month, best_day)])
    # If two candidates tie or the winner has only one piece of evidence
    # AND another competing date exists, report low confidence.
    second = counter.most_common(2)[1] if len(counter) > 1 else None
    ambiguous = bool(second and second[1] >= best_count - 0)

    return {
        "detected": {
            "month": best_month,
            "day":   best_day,
            # Year-less ISO so the UI can render "March 23" without
            # implying we know the birth year.
            "month_day": f"{best_month:02d}-{best_day:02d}",
        },
        "evidence_count": int(best_count),
        "years_seen":     years,
        "ambiguous":      ambiguous,
        "alternates":     [
            {"month": m, "day": d, "count": int(n)}
            for ((m, d), n) in counter.most_common(3)[1:]
        ],
    }


@app.get("/api/contacts/{contact_id}/email-suggestions")
def email_suggestions_for_contact(
    contact_id: int,
    q: Optional[str] = None,
    limit: int = Query(8, ge=1, le=20),
    user: dict[str, Any] = Depends(_auth.current_user),
) -> List[Dict[str, Any]]:
    """Find emails in the user's inbox that might belong to this contact.

    Two modes:
      - Auto-suggest (no q): match `from_name` / `from_email` against the
        contact's display_name + aliases. Quick and cheap.
      - Free-text (q given): hit email_messages_fts so the user can search
        their inbox by any term ("rechnung jan", "mietvertrag", etc.).

    Returns one row per unique `from_email`, with sender name, message
    count, most recent subject, and date. Already-attached emails are
    skipped — no point suggesting what we already have.
    """
    c = _contacts.get(contact_id, include_children=True)
    if not c:
        raise HTTPException(404, "contact not found")

    owner_id = user.get("id") or 0
    already_attached = {ch["value"].lower() for ch in c.get("channels", []) if ch["kind"] == "email"}

    rows: List[Any] = []
    with conn_ctx() as conn:
        if q and q.strip():
            # FTS path — feed escaped tokens (quote each so users can
            # type natural language without breaking the parser).
            tokens = [t for t in re.split(r"\s+", q.strip()) if t]
            if not tokens:
                return []
            fts_query = " ".join(f'"{t.replace(chr(34), "")}"' for t in tokens)
            rows = conn.execute(
                """
                SELECT MIN(m.from_email)   AS from_email,
                       MAX(m.from_name)    AS from_name,
                       COUNT(*)             AS msg_count,
                       MAX(m.date_received) AS last_seen,
                       (SELECT subject FROM email_messages m2
                          WHERE m2.from_email = MIN(m.from_email)
                            AND m2.owner_user_id = m.owner_user_id
                          ORDER BY m2.date_received DESC LIMIT 1) AS last_subject
                FROM email_messages_fts fts
                JOIN email_messages m ON m.id = fts.rowid
                WHERE email_messages_fts MATCH ?
                  AND m.owner_user_id = ?
                  AND m.from_email IS NOT NULL AND m.from_email <> ''
                  AND m.is_sent = 0
                GROUP BY m.from_email, m.owner_user_id
                ORDER BY last_seen DESC
                LIMIT ?
                """,
                (fts_query, owner_id, int(limit)),
            ).fetchall()
        else:
            # Auto-suggest from name + aliases.
            terms = [c.get("display_name") or ""] + (c.get("aliases") or [])
            terms = [t.strip() for t in terms if t and len(t.strip()) >= 2]
            if not terms:
                return []
            like_clauses = " OR ".join(
                ["LOWER(m.from_name) LIKE ?"] * len(terms)
                + ["LOWER(m.from_email) LIKE ?"] * len(terms)
            )
            params: List[Any] = []
            for t in terms:
                params.append(f"%{t.lower()}%")
            for t in terms:
                # Match local-part only (avoid matching every gmail.com user)
                # — split the term on whitespace and use the first token.
                first = t.lower().split()[0]
                params.append(f"%{first}%")
            params.append(owner_id)
            params.append(int(limit))
            rows = conn.execute(
                f"""
                SELECT MIN(m.from_email)   AS from_email,
                       MAX(m.from_name)    AS from_name,
                       COUNT(*)             AS msg_count,
                       MAX(m.date_received) AS last_seen,
                       (SELECT subject FROM email_messages m2
                          WHERE m2.from_email = MIN(m.from_email)
                            AND m2.owner_user_id = m.owner_user_id
                          ORDER BY m2.date_received DESC LIMIT 1) AS last_subject
                FROM email_messages m
                WHERE ({like_clauses})
                  AND m.owner_user_id = ?
                  AND m.from_email IS NOT NULL AND m.from_email <> ''
                  AND m.is_sent = 0
                GROUP BY m.from_email, m.owner_user_id
                ORDER BY last_seen DESC
                LIMIT ?
                """,
                params,
            ).fetchall()

    return [
        {
            "from_email":  r["from_email"],
            "from_name":   r["from_name"] or "",
            "msg_count":   int(r["msg_count"] or 0),
            "last_seen":   r["last_seen"],
            "last_subject": r["last_subject"] or "",
        }
        for r in rows
        if r["from_email"] and r["from_email"].lower() not in already_attached
    ]


# ── Calendars + shares + invitations ──────────────────────────────────
# Layered on top of /api/events: a calendar is a collection, each event
# belongs to one calendar, sharing is per-calendar with three levels
# (free_busy / read / write), and attendees power the invitation flow.

from . import calendars as _calendars_mod


class _CalendarIn(BaseModel):
    name: str
    color: str = "#a78bfa"
    kind: str = "personal"  # 'personal' | 'shared' | 'project'
    hide_from_admin: bool = False


class _CalendarPatch(BaseModel):
    name: Optional[str] = None
    color: Optional[str] = None
    hide_from_admin: Optional[bool] = None


class _ShareIn(BaseModel):
    user_id: str
    access_level: str  # 'free_busy' | 'read' | 'write'


class _RsvpIn(BaseModel):
    status: str  # 'accepted' | 'declined' | 'tentative'
    proposed_time_iso: Optional[str] = None


@app.get("/api/calendars")
def list_calendars_route(
    user: dict[str, Any] = Depends(_auth.current_user),
    role: str = Depends(_auth.current_role),
) -> List[Dict[str, Any]]:
    """Every calendar visible to this user, with their effective access
    level on each. Powers the CalendarApp sidebar."""
    return _calendars_mod.visible_calendars_for(user["id"], role)


@app.post("/api/calendars", status_code=201)
def create_calendar_route(
    body: _CalendarIn,
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    if body.kind not in ("personal", "shared", "project"):
        raise HTTPException(400, "kind must be personal | shared | project")
    cid = _calendars_mod.create_calendar(
        name=body.name, owner_user_id=user["id"],
        color=body.color, kind=body.kind,
        hide_from_admin=body.hide_from_admin,
    )
    return _calendars_mod.get(cid) or {}


@app.patch("/api/calendars/{calendar_id}")
def patch_calendar_route(
    calendar_id: int,
    body: _CalendarPatch,
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    cal = _calendars_mod.get(calendar_id)
    if not cal:
        raise HTTPException(404, "calendar not found")
    if cal["owner_user_id"] != user["id"]:
        raise HTTPException(403, "only the owner can edit this calendar")
    fields = body.model_dump(exclude_unset=True)
    _calendars_mod.rename_calendar(calendar_id, **fields)
    return _calendars_mod.get(calendar_id) or {}


@app.delete("/api/calendars/{calendar_id}", status_code=204, response_class=Response)
def archive_calendar_route(
    calendar_id: int,
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Response:
    cal = _calendars_mod.get(calendar_id)
    if not cal:
        return Response(status_code=204)
    if cal["owner_user_id"] != user["id"]:
        raise HTTPException(403, "only the owner can archive this calendar")
    if cal["kind"] == "shared":
        raise HTTPException(400, "the Shared calendar cannot be archived — rename or hide it instead")
    _calendars_mod.archive_calendar(calendar_id)
    return Response(status_code=204)


@app.get("/api/calendars/{calendar_id}/shares")
def list_calendar_shares(
    calendar_id: int,
    user: dict[str, Any] = Depends(_auth.current_user),  # noqa: ARG001
) -> List[Dict[str, Any]]:
    return _calendars_mod.list_shares(calendar_id)


@app.post("/api/calendars/{calendar_id}/shares", status_code=201)
def upsert_calendar_share(
    calendar_id: int,
    body: _ShareIn,
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    cal = _calendars_mod.get(calendar_id)
    if not cal:
        raise HTTPException(404, "calendar not found")
    if cal["owner_user_id"] != user["id"]:
        raise HTTPException(403, "only the owner can change shares")
    if body.access_level not in _calendars_mod.ACCESS_LEVELS:
        raise HTTPException(400, f"access_level must be one of {_calendars_mod.ACCESS_LEVELS}")
    _calendars_mod.upsert_share(calendar_id, body.user_id, body.access_level)
    return {"calendar_id": calendar_id, "user_id": body.user_id, "access_level": body.access_level}


@app.delete("/api/calendars/{calendar_id}/shares/{user_id}",
             status_code=204, response_class=Response)
def remove_calendar_share(
    calendar_id: int, user_id: str,
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Response:
    cal = _calendars_mod.get(calendar_id)
    if not cal:
        raise HTTPException(404, "calendar not found")
    if cal["owner_user_id"] != user["id"]:
        raise HTTPException(403, "only the owner can change shares")
    _calendars_mod.remove_share(calendar_id, user_id)
    return Response(status_code=204)


@app.post("/api/calendar/move-mine-from-shared")
def move_mine_from_shared(
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """One-shot tidy: move every event currently on the Shared
    calendar that was created by (or invited to) the calling user into
    their Personal calendar. Useful after the migration-010 backfill,
    which dumped all legacy events into Shared because there was no
    owner info to attribute them to.

    Events with no creator AND no attendee link stay on Shared — those
    are genuinely household-collective by default.
    """
    uid = user["id"]
    shared = _calendars_mod.shared_calendar()
    personal = _calendars_mod.list_personal_for(uid)
    if not shared or not personal:
        raise HTTPException(400, "no Shared or Personal calendar for this user")
    target_id = int(personal[0]["id"])
    shared_id = int(shared["id"])
    with conn_ctx() as conn:
        # Two paths: events you own (or that we backfill ownership to),
        # and events where you're an attendee. Set owner_user_id on
        # the way out so future filtering knows who owns it.
        cur = conn.execute(
            """
            UPDATE events
            SET calendar_id = ?, owner_user_id = COALESCE(owner_user_id, ?)
            WHERE calendar_id = ?
              AND (
                owner_user_id = ?
                OR id IN (SELECT event_id FROM event_attendees WHERE user_id = ?)
              )
            """,
            (target_id, uid, shared_id, uid, uid),
        )
        moved = cur.rowcount
        conn.commit()
    return {"moved": int(moved), "from_calendar_id": shared_id, "to_calendar_id": target_id}


# ── Storage relocation (external SSD) ─────────────────────────────────
# Surface the move-and-symlink machinery in backend/storage.py so the
# Settings → Storage page + the onboarding step can drive it.

class _StorageMoveIn(BaseModel):
    target_root: str  # absolute path on the external SSD (e.g. /mnt/ssd/yorik)


@app.get("/api/storage")
def storage_status_route(
    user: dict[str, Any] = Depends(_auth.current_user),  # noqa: ARG001
) -> Dict[str, Any]:
    """Current location of the heavy data subtrees + health of any
    relocation. Powers the Settings → Storage card."""
    from . import storage as _st
    return _st.status()


@app.get("/api/storage/volumes")
def storage_volumes_route(
    user: dict[str, Any] = Depends(_auth.current_user),
) -> List[Dict[str, Any]]:
    """List detected external mounts the user could pick as a target.
    Admin-only — exposing every mountpoint to non-admins is needless
    info disclosure."""
    if (user.get("role") or "").lower() not in ("admin", "platform_admin"):
        raise HTTPException(403, "admin only")
    from . import storage as _st
    return _st.detect_volumes()


@app.post("/api/storage/move")
def storage_move_route(
    body: _StorageMoveIn,
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Move the heavy subtrees onto the chosen path + symlink them back.
    Admin only — touches the filesystem at scale. Long-running for large
    photo libraries; client should show a spinner + disable navigation."""
    if (user.get("role") or "").lower() not in ("admin", "platform_admin"):
        raise HTTPException(403, "admin only")
    from . import storage as _st
    try:
        return _st.move_to(body.target_root)
    except _st.StorageError as exc:
        # User-facing error with a concrete remediation hint baked in.
        raise HTTPException(400, str(exc))
    except PermissionError as exc:
        # Unwritable file inside the source tree (typical cause: a
        # bundled Docker container created files as root). Surface the
        # actual path + a SCOPED chown command instead of a bare 500.
        # IMPORTANT: don't chown data/immich/postgres wholesale — it's
        # container-managed (UID 999) and breaking its ownership stops
        # postgres from starting. Paperless dirs are no longer relocated,
        # so they're not part of this chown either.
        log.warning("storage move PermissionError: %s", exc)
        raise HTTPException(
            500,
            f"Permission denied while moving files: {exc}. "
            "Stop the bundled services, then chown the relocatable "
            "data dir (NOT data/immich/postgres — it needs UID 999):\n\n"
            "  sudo chown -R $(id -u):$(id -g) data/immich/library\n\n"
            "Then retry the move.",
        )
    except OSError as exc:
        log.warning("storage move OSError: %s", exc)
        raise HTTPException(
            500,
            f"Filesystem error during move: {exc}. "
            "If the target is on a removable disk, check that it's mounted "
            "and that the filesystem supports symlinks (ext4/btrfs/xfs do; "
            "FAT32 does not).",
        )
    except Exception as exc:  # noqa: BLE001
        # Unknown failure — surface the message + log the full trace
        # so the user gets something actionable instead of bare 500.
        log.exception("storage move unexpected failure")
        raise HTTPException(500, f"Unexpected move failure: {type(exc).__name__}: {exc}")


@app.post("/api/storage/restore")
def storage_restore_route(
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Reverse a previous move — copy the subtrees back into data/.
    The external copy is left intact so the user can verify before
    deleting it manually."""
    if (user.get("role") or "").lower() not in ("admin", "platform_admin"):
        raise HTTPException(403, "admin only")
    from . import storage as _st
    return _st.restore()


@app.get("/api/dashboard/workers")
def dashboard_workers(
    user: dict[str, Any] = Depends(_auth.current_user),  # noqa: ARG001
) -> Dict[str, Any]:
    """Snapshot of background-worker liveness. Powers the WorkersStatus
    pills on the home screen — surfaces silent failures (WhatsApp
    bridge dropped, email IMAP loop dead, Paperless reconciler stuck)
    so the user sees them before noticing "I'm not receiving messages."
    """
    from . import workers
    return {"workers": workers.get_all()}


@app.get("/api/calendar/freebusy")
def calendar_freebusy(
    users: str = Query(..., description="Comma-separated user IDs"),
    from_iso: str = Query(..., alias="from", description="ISO datetime"),
    to_iso:   str = Query(..., alias="to",   description="ISO datetime"),
    user: dict[str, Any] = Depends(_auth.current_user),
    role: str = Depends(_auth.current_role),
) -> Dict[str, List[Dict[str, Any]]]:
    """Opaque busy blocks per user across the requested window.
    Powers the meeting-time finder in the event editor."""
    # Phase E: user IDs are UUID strings. Old code tried int(); on
    # Postgres-backed installs that 400'd every legitimate request.
    # Accept either UUID strings or integers (for SQLite-era installs).
    user_ids: List[Any] = [x.strip() for x in users.split(",") if x.strip()]
    if not user_ids:
        raise HTTPException(400, "users must be comma-separated user IDs")
    blocks = _calendars_mod.freebusy(
        user_ids, from_iso, to_iso,
        requested_by_user_id=user["id"], requested_by_role=role,
    )
    # JSON keys must be strings — coerce.
    return {str(uid): blks for uid, blks in blocks.items()}


@app.get("/api/events/{event_id}/attendees")
def list_event_attendees(
    event_id: int,
    user: dict[str, Any] = Depends(_auth.current_user),  # noqa: ARG001
) -> List[Dict[str, Any]]:
    return _calendars_mod.attendees_for(event_id)


@app.post("/api/events/{event_id}/rsvp")
def rsvp_to_event(
    event_id: int,
    body: _RsvpIn,
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """The invited user accepts / declines / proposes a new time."""
    res = _calendars_mod.rsvp(
        event_id, user["id"],
        status=body.status, proposed_time_iso=body.proposed_time_iso,
    )
    if not res:
        raise HTTPException(404, "you are not an attendee on this event")
    return res


# (events/parse-natural + events/search moved EARLIER in the file —
#  before /api/events/{event_id} — so FastAPI's order-based matching
#  doesn't catch them as path params. See line ~1058.)


@app.post("/api/ask/stream")
async def ask_stream(
    body: AskIn,
    user: dict[str, Any] = Depends(_auth.current_user),
):
    """Server-Sent-Events streaming version of /api/ask.

    Emits per-iteration progress so the chat UI can show "Suche
    nach …" / "Lese site.de …" instead of an opaque spinner.

    Event shapes:

      data: {"phase": "iter_start",  "iteration": N}
      data: {"phase": "tool_start",  "iteration": N, "tool": "web_search", "args": {...}}
      data: {"phase": "tool_done",   "iteration": N, "tool": "web_search", "duration_s": 1.2}
      data: {"phase": "final",       "response": "...", "ui_actions": [...],
              "sql_used": "...", "conversation_id": "...",
              "agent_trace": {...}}        # last event
      data: {"phase": "error",       "error": "..."}

    The frontend reads with fetch + ReadableStream (not native EventSource,
    so we can send the POST body). The agent loop calls our `_emit`
    callback at the same hook points used by the trace recorder.
    """
    from fastapi.responses import StreamingResponse
    role = user.get("role") or "viewer"

    if not _llm_reachable():
        # Render the offline reply as a single 'final' event so the
        # client doesn't have to special-case both transports.
        offline = _llm_offline_response(body.message, conversation_id=body.conversation_id)
        async def _offline_gen():
            yield "data: " + json.dumps({"phase": "final", **offline},
                                          ensure_ascii=False, default=str) + "\n\n"
        return StreamingResponse(_offline_gen(), media_type="text/event-stream",
                                   headers={"X-Accel-Buffering": "no",
                                            "Cache-Control": "no-cache"})

    queue: asyncio.Queue = asyncio.Queue()
    DONE_SENTINEL = object()

    async def _run_agent_and_signal():
        # Stream typed events from the agent loop. Map each to an SSE
        # phase the frontend understands:
        #   IterationStart      → iter_start  (existing)
        #   TextDelta           → text_delta  (NEW — token-level streaming)
        #   ToolCallReady       → tool_start  (existing)
        #   ToolResultEvent     → tool_done   (existing)
        #   FinalResult         → final       (existing; replaces the
        #                                       accumulated text buffer)
        # The chat builds the assistant bubble token-by-token from
        # text_delta and finalizes it on `final`.
        from .agent import streaming as _stream
        try:
            async for ev in vanna_agent.ask_async_stream(
                body.message, role=role,
                conversation_id=body.conversation_id,
                user_language=user.get("language") or "en",
                user_id=user.get("id"),
            ):
                if isinstance(ev, _stream.IterationStart):
                    await queue.put({"phase": "iter_start", "iteration": ev.n})
                elif isinstance(ev, _stream.TextDelta):
                    await queue.put({"phase": "text_delta", "text": ev.text})
                elif isinstance(ev, _stream.ToolCallStart):
                    # Mostly informational — tool args not yet ready.
                    await queue.put({"phase": "tool_call_start",
                                      "id": ev.id, "tool": ev.name})
                elif isinstance(ev, _stream.ToolCallReady):
                    await queue.put({"phase": "tool_start",
                                      "id": ev.id, "tool": ev.name,
                                      "args": ev.arguments})
                elif isinstance(ev, _stream.ToolResultEvent):
                    await queue.put({"phase": "tool_done",
                                      "id": ev.id, "tool": ev.name,
                                      "ui_actions": ev.ui_actions})
                elif isinstance(ev, _stream.FinalResult):
                    await queue.put({"phase": "final", **ev.response})
                else:
                    # Future event types — forward as opaque type/payload
                    # so the front-end ignores rather than breaks.
                    await queue.put({"phase": "unknown",
                                      "event_type": type(ev).__name__})
        except Exception as exc:  # noqa: BLE001
            import logging as _logging
            _logging.getLogger("yorik.ask_stream").exception("ask_stream agent loop failed")
            await queue.put({"phase": "error",
                              "error": f"{type(exc).__name__}: {exc}"})
        finally:
            await queue.put(DONE_SENTINEL)

    async def _event_stream():
        task = asyncio.create_task(_run_agent_and_signal())
        try:
            while True:
                item = await queue.get()
                if item is DONE_SENTINEL:
                    break
                yield "data: " + json.dumps(item, ensure_ascii=False, default=str) + "\n\n"
        finally:
            # Caller disconnected mid-stream — let the background task
            # finish (it'll just write to a drained queue, no harm).
            if not task.done():
                # We don't cancel mid-flight tool calls; that risks
                # half-applied state (a draft created but not surfaced).
                # The task will finish on its own.
                pass

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "X-Accel-Buffering": "no",   # tells nginx + similar not to buffer
            "Cache-Control":     "no-cache",
            "Connection":        "keep-alive",
        },
    )


@app.post("/api/ask")
def ask(
    body: AskIn,
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Chat / ask-Yorik. Role is derived from the session cookie — any
    `role` field on the body is IGNORED. (Was historically trusted; closed
    the escalation hole in the same session that closed the query-param
    bypass.)"""
    role = user.get("role") or "viewer"
    # Short-circuit if the local LLM endpoint is down. Without this the
    # handler waits ~30s on a connect timeout before returning a useless
    # `ConnectError` stack trace to the user.
    if not _llm_reachable():
        return _llm_offline_response(body.message, conversation_id=body.conversation_id)
    return vanna_agent.ask(
        body.message, role,
        conversation_id=body.conversation_id,
        user_language=user.get("language") or "en",
        user_id=user.get("id"),
        dev_mode=bool(user.get("dev_mode")),
        force_first_tool_call=body.require_tool_call,
    )


@app.get("/api/saved-queries")
def list_saved_queries(limit: int = Query(50, ge=1, le=500)) -> List[Dict[str, Any]]:
    """Inspection endpoint for the cache. Ordered by warmest (use_count desc)."""
    with conn_ctx(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, trigger_phrase, sql_query, view_command, response_text, use_count, last_used "
            "FROM saved_queries ORDER BY use_count DESC, last_used DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/connectors")
def list_connectors_endpoint() -> List[Dict[str, Any]]:
    """List installed external-integration connectors with their parameter schemas.

    Mirrors the layout-marketplace shape. Frontend can render a "what services
    are connected" picker from this; the LLM has its own list_connectors tool
    that returns the same data but also opens the modal as a UI action.
    """
    return [connectors.to_catalogue_entry(s) for s in connectors.list_all()]


@app.get("/api/web/visits")
def list_web_visits(
    limit: int = Query(100, ge=1, le=500),
    user: dict[str, Any] = Depends(_auth.current_user),
) -> List[Dict[str, Any]]:
    """Per-user audit log of what Yorik searched / fetched on the open
    web. Powers Settings → Privacy → 'What did Yorik look up?'. Each
    web_lookup + web_fetch call inserts one row."""
    role = user.get("role") or "viewer"
    with conn_ctx(DB_PATH) as conn:
        if role in ("platform_admin", "admin"):
            rows = conn.execute(
                "SELECT id, user_id, action, query, url, provider, ok, "
                "       status, bytes, error, at "
                "FROM web_visits ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, user_id, action, query, url, provider, ok, "
                "       status, bytes, error, at "
                "FROM web_visits WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                (user["id"], int(limit)),
            ).fetchall()
    return [dict(r) for r in rows]


@app.delete("/api/web/visits", status_code=204, response_class=Response)
def clear_web_visits(
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Response:
    """Clear THIS user's web audit log (admin doesn't get a 'clear all'
    button — auditability matters more than convenience there)."""
    with conn_ctx(DB_PATH) as conn:
        conn.execute("DELETE FROM web_visits WHERE user_id = ?", (user["id"],))
        conn.commit()
    return Response(status_code=204)


class ConnectorInvokeIn(BaseModel):
    params: Dict[str, Any] = Field(default_factory=dict)


# Layouts shipped with Yorik are trusted code (we wrote them, reviewed them,
# they pass through Tier-0+1+2 review before being added). They're exempt
# from the per-connector grant prompt. `__system__` is the synthetic layout
# id used by trigger_connector when the LLM (not a layout) invokes a connector.
BUNDLED_LAYOUT_IDS = {"yorik-calendar", "apple", "google", "google-classic", "apple-minimal", "__system__"}


def _has_grant(layout_id: str, connector_name: str) -> bool:
    if not layout_id or layout_id in BUNDLED_LAYOUT_IDS:
        return True
    with conn_ctx(DB_PATH) as conn:
        row = conn.execute(
            "SELECT 1 FROM connector_grants "
            "WHERE layout_id = ? AND connector_name = ? AND revoked_at IS NULL",
            (layout_id, connector_name),
        ).fetchone()
    return row is not None


@app.post("/api/connectors/{name}/invoke")
async def invoke_connector_endpoint(
    name: str,
    body: ConnectorInvokeIn,
    role: str = Depends(_auth.current_role),
    layout_id: Optional[str] = Query(None, description="ID of the layout making the call, for permission enforcement"),
) -> Dict[str, Any]:
    """Invoke a connector. Per-layout permission grants enforced here."""
    require_write(role)
    if not _has_grant(layout_id or "", name):
        # 403 with the info the frontend needs to prompt the user.
        spec = connectors.get(name)
        raise HTTPException(
            status_code=403,
            detail={
                "error": "connector_not_granted",
                "layout_id": layout_id,
                "connector_name": name,
                "connector_description": spec.description if spec else "(unknown connector)",
            },
        )
    return await connectors.invoke(name, body.params or {})


# ── connector credentials (Wave 4) ─────────────────────────────────────────

class CredentialsIn(BaseModel):
    credentials: Dict[str, Any]


@app.post("/api/connectors/{name}/test")
def test_connector_credentials(name: str, body: CredentialsIn, role: str = Depends(_auth.current_role)) -> Dict[str, Any]:
    """Try a `test_connection` op against the given credentials WITHOUT
    storing them. The Settings → Connectors UI uses this so the user
    can verify before committing. Returns whatever the connector's
    test_connection op returns ({all_ok, imap, smtp, error, ...})."""
    if normalize_role(role) not in ("admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="role required: admin")
    spec = connectors.get(name)
    if not spec:
        raise HTTPException(status_code=404, detail=f"unknown connector '{name}'")
    if spec.backend != "builtin":
        raise HTTPException(status_code=400, detail=f"connector '{name}' cannot be tested in-line")
    # The connector reads credentials via credential_store.get(name). For
    # a no-persist test we temporarily monkey-patch the store's get for the
    # duration of one call. This is safer than threading creds through the
    # invoke path (every connector would need a sentinel param).
    original = credential_store.get
    def _override(connector_name: str):
        if connector_name == name:
            return body.credentials
        return original(connector_name)
    credential_store.get = _override  # type: ignore[assignment]
    try:
        import asyncio
        result = asyncio.run(connectors.invoke(name, {"op": "test_connection"}))
    except Exception as exc:  # noqa: BLE001
        return {"all_ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        credential_store.get = original  # type: ignore[assignment]
    return result if isinstance(result, dict) else {"all_ok": False, "error": "unexpected result"}


@app.post("/api/connectors/{name}/credentials")
def save_connector_credentials(name: str, body: CredentialsIn, role: str = Depends(_auth.current_role)) -> Dict[str, Any]:
    """Encrypt + persist credentials for a Python connector. Admin only."""
    if normalize_role(role) not in ("admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="role required: admin")
    spec = connectors.get(name)
    if not spec:
        raise HTTPException(status_code=404, detail=f"unknown connector '{name}'")
    # Some connectors are usable WITHOUT credentials but expose OPTIONAL
    # config via credentials_schema (e.g. maps — works with the free OSRM
    # demo, but accepts an OpenRouteService API key for higher quota).
    # Reject only if there's literally nothing to configure.
    if not spec.requires_auth and not (spec.credentials_schema or {}).get("properties"):
        raise HTTPException(status_code=400, detail=f"connector '{name}' does not accept credentials")
    if spec.backend != "builtin":
        raise HTTPException(
            status_code=400,
            detail=f"connector '{name}' uses n8n for credentials — configure it at {n8n_client.N8N_BASE_URL}",
        )
    # Trust the schema's required fields; do a soft check so the user gets
    # a useful error before the connector itself tries.
    required = (spec.credentials_schema or {}).get("required") or []
    missing = [k for k in required if not body.credentials.get(k)]
    if missing:
        raise HTTPException(status_code=400, detail=f"missing required fields: {missing}")
    credential_store.put(name, body.credentials)
    return {"connector_name": name, "configured": True}


@app.delete("/api/connectors/{name}/credentials", status_code=204, response_class=Response)
def delete_connector_credentials(name: str, role: str = Depends(_auth.current_role)) -> Response:
    if normalize_role(role) not in ("admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="role required: admin")
    credential_store.delete(name)
    return Response(status_code=204)


@app.get("/api/connectors/credentialed")
def list_credentialed_connectors() -> List[Dict[str, Any]]:
    """Which connectors currently have credentials stored. Payloads never leave the server."""
    return credential_store.list_configured()


@app.get("/api/n8n/status")
def n8n_status() -> Dict[str, Any]:
    """Frontend pings this to decide whether to expose 'Open in n8n' affordances."""
    return n8n_client.is_reachable()


# ── tenant→host internal provisioning proxy ───────────────────────────────
# Why this endpoint exists: Phase F-lite tenants share the host's Immich
# and Paperless. Tenants must NOT carry the Immich/Paperless admin keys
# (a single bug in one tenant's FastAPI would leak admin to all tenants'
# LLM contexts). Instead the host accepts authenticated provisioning
# requests over loopback, runs the admin-keyed user-create with
# `is_admin=False` + a tenant-namespaced upstream email/username, and
# returns the per-user secrets the tenant stores in its own DB. The
# host is the trust anchor; the bearer token gates who's allowed to ask.

class InternalProvisionIn(BaseModel):
    tenant_name: str
    service: str  # "paperless" | "immich"
    yorik_user_id: str
    name: str
    email: str
    password: str


def _namespace_upstream_for_tenant(tenant_name: str, email: str) -> tuple[str, str]:
    """Return (upstream_email, display_name_hint) for a tenant's user.

    Pass-through: we used to prefix the localpart with the tenant name
    (e.g. `mom@household.local` → `momyorik+mom@household.local`) so two
    tenants couldn't collide on Immich/Paperless `UNIQUE(email)` and so
    a tenant couldn't replay the host admin's email to trigger the
    "exists → reset password" branch. The prefix turned out to be a
    confusing UX — users typed their real email into Immich and got
    "incorrect email" with no hint that the upstream account lived at
    a different address.

    The security guard now lives in the host proxy itself (see
    internal_provision_endpoint): before any provision, the host asks
    Immich/Paperless whether the email already exists and refuses
    with a clear collision error if so. So the namespace prefix is
    no longer load-bearing — return the original email unchanged.

    Synthetic fallback (email with no '@') still routes to a stable
    local address so Immich/Paperless accept it.
    """
    if "@" not in email:
        return f"{email or 'user'}@yorik.local", email
    return email, email


def _verify_internal_bearer(request: Request) -> Optional[str]:
    """Resolve the `Authorization: Bearer <token>` header to a tenant
    name via the tenant_bearer_tokens registry. Raises 401 on
    unknown token, 401 on missing/malformed header. Returns the
    resolved tenant_name — callers compare against the body's
    `tenant_name` to enforce that one tenant can't act on behalf of
    another.

    Two paths:
      1. Per-tenant bearer (modern): lookup in tenant_bearer_tokens,
         return the tenant_name on hit.
      2. Legacy single-token fallback: if data/internal_token exists
         AND the provided bearer matches it, return None. Callers
         treat None as "host-level legacy bearer, skip tenant-name
         binding." Only intended to keep workstation installs running
         while operators recreate their tenants under the new flow.

    Read-only on the file: never regenerates data/internal_token in
    the verify path. Token regeneration happens at startup via the
    host's _startup hook only.
    """
    header = request.headers.get("authorization") or ""
    if not header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    provided = header[len("Bearer "):].strip()
    if not provided:
        raise HTTPException(status_code=401, detail="missing bearer token")

    from . import external_users as _eu
    # Path 1: per-tenant token.
    tenant_name = _eu.resolve_bearer_to_tenant(provided)
    if tenant_name:
        return tenant_name

    # Path 2: legacy single-token fallback.
    try:
        legacy = _eu._read_internal_token()
    except Exception:
        legacy = None
    if legacy:
        import hmac as _hmac
        if _hmac.compare_digest(provided, legacy):
            return None  # legacy bearer; caller skips tenant-name binding

    raise HTTPException(status_code=401, detail="bad bearer token")


def _enforce_tenant_match(verified_tenant: Optional[str], body_tenant: str) -> None:
    """Helper: if the bearer resolved to a specific tenant (modern
    per-tenant token), the request's body MUST claim that same
    tenant — otherwise it's a cross-tenant impersonation attempt.
    Legacy bearer (verified_tenant=None) skips this check; that's
    the documented bypass that lets host-side scripts call the
    internal endpoints during initial setup."""
    if verified_tenant is not None and verified_tenant != body_tenant:
        raise HTTPException(
            status_code=403,
            detail=(
                f"bearer is bound to tenant '{verified_tenant}' but request "
                f"targets '{body_tenant}'"
            ),
        )


@app.post("/api/internal/provision")
def internal_provision_endpoint(
    body: InternalProvisionIn,
    request: Request,
) -> Dict[str, Any]:
    """Host-side proxy: provision a non-admin upstream user on behalf
    of a tenant Yorik. The endpoint is loopback-callable only by code
    that can read `data/internal_token`; treat it as effectively
    same-machine RPC, not a public API.

    Refuses if `tenant_name` doesn't have a manifest under
    `data/tenants/<name>/` — defense-in-depth against a compromised
    tenant requesting accounts for nonexistent tenants. Also enforces
    that the bearer's bound tenant matches the body's tenant_name.
    """
    verified_tenant = _verify_internal_bearer(request)

    tenant_name = body.tenant_name.strip().lower()
    import re as _re
    if not _re.match(r"^[a-z][a-z0-9_]{0,23}$", tenant_name):
        raise HTTPException(400, "invalid tenant_name")
    _enforce_tenant_match(verified_tenant, tenant_name)
    manifest = (
        Path(__file__).resolve().parent.parent
        / "data" / "tenants" / tenant_name / "manifest.env"
    )
    if not manifest.exists():
        raise HTTPException(404, f"no tenant manifest for {tenant_name!r}")

    if body.service not in ("paperless", "immich"):
        raise HTTPException(400, "service must be 'paperless' or 'immich'")

    # Serialise against scripts/{create,drop}-tenant.sh. Same flock
    # file (data/locks/tenant-<name>.lock) the scripts use; if drop
    # is in flight we 503 with Retry-After rather than racing it and
    # leaving an orphan upstream user under a doomed tenant DB.
    import fcntl as _fcntl
    locks_dir = Path(__file__).resolve().parent.parent / "data" / "locks"
    locks_dir.mkdir(exist_ok=True)
    lock_path = locks_dir / f"tenant-{tenant_name}.lock"
    lock_fp = open(lock_path, "w")
    try:
        try:
            _fcntl.flock(lock_fp, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except BlockingIOError:
            raise HTTPException(
                503,
                f"tenant '{tenant_name}' is busy with a create/drop operation, retry in a moment",
            )
        # Re-check manifest under the lock — drop could have started
        # between the earlier check and the lock acquisition.
        if not manifest.exists():
            raise HTTPException(404, f"tenant {tenant_name!r} dropped mid-flight")

        upstream_email, _hint = _namespace_upstream_for_tenant(
            tenant_name, body.email,
        )

        from . import external_users as _eu
        # Email-collision guard. Now that we no longer namespace the
        # upstream email per tenant, the same email being claimed by
        # two principals (host admin + tenant, or two tenants) would
        # otherwise hit provision_*'s "user exists → reset password"
        # branch and silently hijack the existing account. Refuse here
        # with a clear 409 so the operator picks a different email.
        existing = _eu.host_lookup_user_by_email(body.service, upstream_email)
        if existing:
            raise HTTPException(
                409,
                f"{body.service}: an account with email '{upstream_email}' already "
                "exists on this household server — pick a different email for this "
                "tenant admin.",
            )
        # is_admin=False because tenants must never get external-service
        # admin (Phase F-lite isolation contract). _store=False because the
        # caller is a TENANT — the per-user creds belong in the tenant's
        # DB, not the host's. The tenant Yorik writes them via its own
        # _store_*_creds after the proxy returns.
        try:
            if body.service == "paperless":
                result = _eu.provision_paperless(
                    body.yorik_user_id, body.name, upstream_email,
                    body.password, is_admin=False, _store=False,
                )
            else:
                result = _eu.provision_immich(
                    body.yorik_user_id, body.name, upstream_email,
                    body.password, is_admin=False, _store=False,
                )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"{body.service} provisioning failed: {exc}")
        return result
    finally:
        lock_fp.close()


# ── tenant lifecycle + invites (host-only) ────────────────────────────────
# The host's operator UI ("Add household → Mom") drives this. POST
# /api/tenants creates a tenant + generates an invite link; the
# household admin opens that link to land on the tenant's setup
# wizard. Tenant Yoriks validate + consume invites via the bearer-
# authed /api/internal/invites/* endpoints.

def _build_invite_url(tenant_name: str, port: int, token: str,
                      kind: str, host_header: Optional[str]) -> str:
    """Build the invite (or reset) URL the operator hands to the new
    household admin.

    Three URL shapes depending on YORIK_TENANT_ROOT:
      * 'localhost' (the dev default): browsers auto-resolve any
        *.localhost name to 127.0.0.1, so we can hand out
            http://<tenant>.localhost:<port>/?<kind>=<token>
        with no DNS or cert setup. Port matters here because the
        tenant uvicorn is bound to <port>, not 80/443.
      * Real domain (production via Caddy):
            https://<tenant>.<root>/?<kind>=<token>
        Caddy on 443 routes the subdomain to the tenant uvicorn on
        127.0.0.1:<port> using the snippet create-tenant.sh emits.
        No port in the URL — operator runs wildcard DNS + TLS.
      * Empty / unset (legacy fallback): bare host:port over HTTP,
        derived from the request's Host header. Lets the operator
        click through invites on a fresh box before they've decided
        what root domain to use.

    `kind` is 'invite' (initial setup) or 'reset' (password reset).
    """
    root = (os.getenv("YORIK_TENANT_ROOT") or "").strip()
    # Strip any scheme/port the operator might have set by accident.
    root = root.replace("https://", "").replace("http://", "")
    root = root.split("/")[0].split(":")[0]
    if root == "localhost":
        return f"http://{tenant_name}.localhost:{port}/?{kind}={token}"
    if root:
        return f"https://{tenant_name}.{root}/?{kind}={token}"
    host_part = (host_header or "localhost").split(":")[0]
    return f"http://{host_part}:{port}/?{kind}={token}"


def _ensure_tenant_invites_table() -> None:
    """Lazy-bootstrap the host's tenant_invites table. Postgres-only:
    if this Yorik is running on SQLite there's no Phase F-lite multi-
    tenant story, so the table doesn't need to exist. Cheaper than a
    startup hook because most installs never touch the invite flow.

    target_email is populated for reset-type invites; the tenant
    Yorik uses it to scope a password-reset to a specific existing
    admin. NULL for initial setup invites (the default)."""
    if (os.getenv("YORIK_DB_BACKEND") or "sqlite").lower() != "postgres":
        return
    with conn_ctx(DB_PATH) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS tenant_invites ("
            "  token         TEXT PRIMARY KEY,"
            "  tenant_name   TEXT NOT NULL,"
            "  port          INTEGER NOT NULL,"
            "  display_label TEXT,"
            "  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),"
            "  expires_at    TIMESTAMPTZ NOT NULL,"
            "  consumed_at   TIMESTAMPTZ,"
            "  created_by    UUID,"
            "  target_email  TEXT"
            ")"
        )
        # Forward-compat ALTER for installs that bootstrapped the table
        # before the target_email column existed.
        try:
            conn.execute("ALTER TABLE tenant_invites ADD COLUMN IF NOT EXISTS target_email TEXT")
        except Exception:  # noqa: BLE001
            pass
        conn.commit()


class TenantCreateIn(BaseModel):
    name: str = Field(..., description="Tenant slug (lowercase, ≤24 chars). Becomes yorik_tenant_<name>.")
    display_label: Optional[str] = Field(None, description="Human label shown on the setup page ('Mom', 'Parents').")
    invite_host: Optional[str] = Field(None, description="Host part for the invite URL (default: request Host header).")
    invite_expires_hours: int = Field(168, ge=1, le=24 * 30, description="Invite TTL in hours (default 7 days).")


@app.post("/api/tenants")
def create_tenant_endpoint(
    body: TenantCreateIn,
    request: Request,
    role: str = Depends(_auth.current_role),
) -> Dict[str, Any]:
    """Host-only: create a new tenant + return an invite link the
    operator can hand to the new household admin. The link points at
    the tenant's port with `?invite=<token>` so the tenant Yorik can
    validate + consume the invite during the setup wizard.

    Refuses when this Yorik is itself a tenant (YORIK_DB_NAME != 'postgres');
    nested multi-tenancy is out of scope.
    """
    if normalize_role(role) not in ("admin", "platform_admin"):
        raise HTTPException(403, "role required: admin")
    if os.getenv("YORIK_IS_TENANT", "").strip() in ("1", "true", "yes", "on") \
       or (os.getenv("YORIK_DB_NAME") and os.getenv("YORIK_DB_NAME") != "postgres"):
        raise HTTPException(
            400,
            "this Yorik is itself a tenant — only the host can create tenants",
        )
    # The multi-tenant story requires a Postgres backend: the tenant
    # DB is a database inside the shared supabase-db cluster, and the
    # tenant_invites tracking table is Postgres-flavoured. On a SQLite
    # host install create-tenant.sh would actually succeed (it talks
    # to the supabase-db container directly, not through the host
    # FastAPI), but the subsequent INSERT INTO tenant_invites here
    # would hit "no such table" — leaving a half-created tenant on
    # disk with no invite. Refuse loudly instead.
    if (os.getenv("YORIK_DB_BACKEND") or "sqlite").lower() != "postgres":
        raise HTTPException(
            400,
            "multi-tenant requires YORIK_DB_BACKEND=postgres on the host "
            "(this install is sqlite-only)",
        )
    import re as _re
    if not _re.match(r"^[a-z][a-z0-9_]{0,23}$", body.name):
        raise HTTPException(400, "tenant name must be lowercase letters + digits + underscore (≤24 chars)")

    _ensure_tenant_invites_table()

    # Run scripts/create-tenant.sh as a subprocess. The script is the
    # source of truth for tenant DB layout (migrations, grants, port
    # allocation); duplicating its logic in Python would drift.
    import subprocess
    repo_root = Path(__file__).resolve().parent.parent
    try:
        proc = subprocess.run(
            ["bash", "scripts/create-tenant.sh", body.name],
            cwd=str(repo_root),
            capture_output=True, text=True, timeout=120, check=False,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "create-tenant.sh timed out after 120s")
    if proc.returncode != 0:
        raise HTTPException(
            502,
            f"create-tenant.sh failed (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout)[:400]}",
        )

    # Read the tenant's freshly-written manifest to get its allocated port.
    manifest = repo_root / "data" / "tenants" / body.name / "manifest.env"
    if not manifest.exists():
        raise HTTPException(500, f"create-tenant.sh succeeded but {manifest} missing")
    port = None
    for line in manifest.read_text().splitlines():
        if line.startswith("HOMEOS_PORT="):
            try:
                port = int(line.split("=", 1)[1].strip())
            except ValueError:
                pass
            break
    if port is None:
        raise HTTPException(500, "manifest has no HOMEOS_PORT — create-tenant.sh broken?")

    import secrets as _secrets
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    token = _secrets.token_urlsafe(32)
    expires_at = _dt.now(_tz.utc) + _td(hours=body.invite_expires_hours)
    user = _auth.current_user_from_request(request) if hasattr(_auth, "current_user_from_request") else None
    created_by = (user or {}).get("id") if isinstance(user, dict) else None

    with conn_ctx(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO tenant_invites (token, tenant_name, port, display_label, "
            "                            expires_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (token, body.name, port, body.display_label, expires_at, created_by),
        )
        conn.commit()

    # Build the invite URL — shape depends on YORIK_TENANT_ROOT
    # (see _build_invite_url docstring). invite_host overrides the
    # host-header fallback used when the env is unset.
    invite_url = _build_invite_url(
        body.name, port, token, "invite",
        body.invite_host or request.headers.get("host"),
    )

    # Auto-start the tenant uvicorn so the invite link is live the
    # moment the operator hands it over. Goes through systemd over
    # D-Bus (no sudo) — yorik.service runs with NoNewPrivileges=true
    # which blocks setuid, so authorization comes from the polkit rule
    # at /etc/polkit-1/rules.d/50-yorik-tenant.rules installed by
    # install.sh. Failure is non-fatal — DB and manifest are already
    # on disk, so the operator can retry the start manually.
    unit_started = False
    unit_warning: Optional[str] = None
    try:
        start_proc = subprocess.run(
            ["systemctl", "enable", "--now",
             f"yorik-tenant@{body.name}.service"],
            capture_output=True, text=True, timeout=20, check=False,
        )
        if start_proc.returncode == 0:
            unit_started = True
        else:
            unit_warning = (start_proc.stderr or start_proc.stdout or "").strip()[:200]
            logging.getLogger("yorik.tenants").warning(
                "auto-start failed for tenant=%s: %s", body.name, unit_warning,
            )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        unit_warning = str(exc)[:200]
        logging.getLogger("yorik.tenants").warning(
            "auto-start crashed for tenant=%s: %s", body.name, exc,
        )

    return {
        "tenant_name": body.name,
        "port": port,
        "invite_url": invite_url,
        "invite_token": token,
        "expires_at": expires_at.isoformat(),
        "display_label": body.display_label,
        "unit_started": unit_started,
        "unit_warning": unit_warning,
    }


@app.get("/api/tenants")
def list_tenants_endpoint(
    role: str = Depends(_auth.current_role),
) -> List[Dict[str, Any]]:
    """Host-only: list every tenant on this box with its current
    invite status. Used by the Settings → Households page."""
    if normalize_role(role) not in ("admin", "platform_admin"):
        raise HTTPException(403, "role required: admin")
    # Refuse on tenant Yoriks. The filesystem path data/tenants/ is
    # shared between the host and every tenant uvicorn (they all run
    # from the same repo root), so without this guard a tenant admin
    # could list every other household's name + port — and worse, the
    # frontend Settings → Households tab would render a misleading
    # "all-tenants" view even though their /api/tenants/POST gets
    # refused (existing check). Match the same gate the POST uses.
    if os.getenv("YORIK_IS_TENANT", "").strip() in ("1", "true", "yes", "on") \
       or (os.getenv("YORIK_DB_NAME") and os.getenv("YORIK_DB_NAME") != "postgres"):
        raise HTTPException(
            400,
            "this Yorik is itself a tenant — only the host can list tenants",
        )
    _ensure_tenant_invites_table()

    out: List[Dict[str, Any]] = []
    repo_root = Path(__file__).resolve().parent.parent
    tenants_dir = repo_root / "data" / "tenants"
    if not tenants_dir.exists():
        return out

    with conn_ctx(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT token, tenant_name, port, display_label, expires_at, "
            "       consumed_at, created_at "
            "FROM tenant_invites "
            "ORDER BY created_at DESC"
        ).fetchall()
    invites_by_name: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        invites_by_name.setdefault(r["tenant_name"], []).append(dict(r))

    for child in sorted(tenants_dir.iterdir()):
        if not child.is_dir() or not (child / "manifest.env").exists():
            continue
        name = child.name
        port = None
        for line in (child / "manifest.env").read_text().splitlines():
            if line.startswith("HOMEOS_PORT="):
                try:
                    port = int(line.split("=", 1)[1].strip())
                except ValueError:
                    pass
                break
        # Most recent invite for this tenant (if any).
        latest = (invites_by_name.get(name) or [None])[0]
        out.append({
            "name": name,
            "port": port,
            "invite": latest,  # None when no invite was ever issued
        })
    return out


class TenantResetInviteIn(BaseModel):
    target_email: str = Field(..., description="Email of the existing tenant admin to reset")
    invite_host: Optional[str] = Field(None, description="Host part for the invite URL (default: request Host header).")
    invite_expires_hours: int = Field(48, ge=1, le=24 * 7, description="Reset invite TTL in hours (default 48h, max 7 days — tighter than initial-setup because reset capability is destructive).")


@app.post("/api/tenants/{name}/issue-reset-invite")
def issue_tenant_reset_invite_endpoint(
    name: str,
    body: TenantResetInviteIn,
    request: Request,
    role: str = Depends(_auth.current_role),
) -> Dict[str, Any]:
    """Host-only: generate a one-time invite link the tenant admin can
    open to reset their password.

    Use case: tenant admin (Mom) forgot her password. Operator goes
    to Settings → Households → Mom → "Issue reset link". Without this
    endpoint the only recovery path was drop-and-recreate the tenant,
    which loses all of Mom's data.

    The invite ships `target_email` so the tenant Yorik knows which
    user to reset; the email must already exist as an admin in the
    tenant's DB (we verify this synchronously via the shared
    supabase-db cluster). Default TTL 48 hours — tighter than initial
    setup because reset capability is destructive (replaces an
    existing password without knowledge of the old one).
    """
    if normalize_role(role) not in ("admin", "platform_admin"):
        raise HTTPException(403, "role required: admin")
    if os.getenv("YORIK_IS_TENANT", "").strip() in ("1", "true", "yes", "on") \
       or (os.getenv("YORIK_DB_NAME") and os.getenv("YORIK_DB_NAME") != "postgres"):
        raise HTTPException(400, "this Yorik is itself a tenant — only the host can issue resets")
    if (os.getenv("YORIK_DB_BACKEND") or "sqlite").lower() != "postgres":
        raise HTTPException(400, "multi-tenant requires YORIK_DB_BACKEND=postgres on the host")

    import re as _re
    if not _re.match(r"^[a-z][a-z0-9_]{0,23}$", name):
        raise HTTPException(400, "invalid tenant name")
    target_email = body.target_email.strip().lower()
    if "@" not in target_email or len(target_email) > 254:
        raise HTTPException(400, "invalid target_email")

    _ensure_tenant_invites_table()

    # Read the tenant's port from its manifest (same as the initial
    # invite path — we need it for the invite URL).
    repo_root = Path(__file__).resolve().parent.parent
    manifest = repo_root / "data" / "tenants" / name / "manifest.env"
    if not manifest.exists():
        raise HTTPException(404, f"no tenant manifest for {name!r}")
    port = None
    for line in manifest.read_text().splitlines():
        if line.startswith("HOMEOS_PORT="):
            try:
                port = int(line.split("=", 1)[1].strip())
            except ValueError:
                pass
            break
    if port is None:
        raise HTTPException(500, "manifest has no HOMEOS_PORT")

    # Verify the target user actually exists in the tenant's DB and
    # has admin role. We have direct Postgres access via the host's
    # admin connection — no need to round-trip through the tenant
    # FastAPI for this.
    db_pw = os.getenv("YORIK_DB_PASSWORD") or ""
    if not db_pw:
        env_file = repo_root / "infra/supabase/docker/.env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("POSTGRES_PASSWORD="):
                    db_pw = line.split("=", 1)[1].strip()
                    break
    if not db_pw:
        raise HTTPException(500, "POSTGRES_PASSWORD unreachable")

    import psycopg as _psycopg
    db_host = os.getenv("YORIK_DB_HOST", "127.0.0.1")
    db_port = os.getenv("YORIK_DB_PORT", "5435")
    try:
        with _psycopg.connect(
            f"postgresql://postgres:{db_pw}@{db_host}:{db_port}/yorik_tenant_{name}",
            connect_timeout=5,
        ) as conn:
            row = conn.execute(
                "SELECT role FROM user_profiles WHERE lower(email) = %s LIMIT 1",
                (target_email,),
            ).fetchone()
    except _psycopg.OperationalError as exc:
        raise HTTPException(502, f"could not reach tenant database: {exc}")
    if not row:
        raise HTTPException(404, f"no user with email {target_email!r} in tenant {name!r}")
    user_role = row[0] if not isinstance(row, dict) else row["role"]
    if user_role not in ("admin", "platform_admin"):
        raise HTTPException(403, f"user {target_email!r} is not an admin in tenant {name!r}")

    # Issue the invite.
    import secrets as _secrets
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    token = _secrets.token_urlsafe(32)
    expires_at = _dt.now(_tz.utc) + _td(hours=body.invite_expires_hours)
    with conn_ctx(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO tenant_invites (token, tenant_name, port, display_label, "
            "                            expires_at, target_email) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (token, name, port, f"Reset for {target_email}",
             expires_at, target_email),
        )
        conn.commit()

    # Reset links use ?reset= (not ?invite=) so the tenant SetupScreen
    # can switch to the "set a new password for <email>" UX instead
    # of "create your first admin." Server-side both forms hit the
    # invite_lookup endpoint; the URL param is purely a frontend hint.
    invite_url = _build_invite_url(
        name, port, token, "reset",
        body.invite_host or request.headers.get("host"),
    )

    return {
        "tenant_name":   name,
        "target_email":  target_email,
        "invite_url":    invite_url,
        "invite_token":  token,
        "expires_at":    expires_at.isoformat(),
    }


@app.delete("/api/tenants/{name}", status_code=204, response_class=Response)
def drop_tenant_endpoint(
    name: str,
    role: str = Depends(_auth.current_role),
) -> Response:
    """Host-only: drop a tenant. Wraps scripts/drop-tenant.sh which
    handles upstream cleanup + systemd stop + DB drop + dir removal.
    The --yes flag suppresses the interactive confirm — caller already
    decided via the UI/API."""
    if normalize_role(role) not in ("admin", "platform_admin"):
        raise HTTPException(403, "role required: admin")
    # Refuse on tenant Yoriks (same reason as list_tenants_endpoint —
    # tenant admin must not be able to drop other tenants by addressing
    # them by name through the host endpoint replicated to each
    # tenant's FastAPI surface).
    if os.getenv("YORIK_IS_TENANT", "").strip() in ("1", "true", "yes", "on") \
       or (os.getenv("YORIK_DB_NAME") and os.getenv("YORIK_DB_NAME") != "postgres"):
        raise HTTPException(
            400,
            "this Yorik is itself a tenant — only the host can drop tenants",
        )
    import re as _re
    if not _re.match(r"^[a-z][a-z0-9_]{0,23}$", name):
        raise HTTPException(400, "invalid tenant name")

    import subprocess
    repo_root = Path(__file__).resolve().parent.parent
    try:
        proc = subprocess.run(
            ["bash", "scripts/drop-tenant.sh", name],
            cwd=str(repo_root),
            input="y\n",
            capture_output=True, text=True, timeout=120, check=False,
        )
    except FileNotFoundError as exc:
        # `bash` missing on PATH, or the script vanished from the
        # repo. Return 502 (gateway-style: dependency missing) rather
        # than 500 so the operator can distinguish "Yorik bug" from
        # "deployment broken."
        raise HTTPException(502, f"drop-tenant.sh unavailable: {exc}")
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "drop-tenant.sh timed out after 120s")
    if proc.returncode != 0:
        raise HTTPException(
            502,
            f"drop-tenant.sh failed (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout)[:400]}",
        )
    # Also clean tenant_invites rows so the next list call doesn't
    # surface stale entries pointing at a nonexistent tenant.
    _ensure_tenant_invites_table()
    with conn_ctx(DB_PATH) as conn:
        conn.execute("DELETE FROM tenant_invites WHERE tenant_name = ?", (name,))
        conn.commit()
    return Response(status_code=204)


@app.get("/api/internal/invites/{token}")
def invite_lookup_endpoint(token: str, request: Request) -> Dict[str, Any]:
    """Bearer-gated tenant→host RPC. Tenant calls this during setup
    wizard with the `?invite=<token>` query param to confirm the
    invite is real + not expired/consumed, and to learn the display
    label to show on the welcome screen.

    Returns 410 instead of 404 for consumed/expired so the tenant
    can distinguish "invalid token" from "stale link" in the UI.
    Enforces that the bearer's tenant matches the invite's tenant —
    prevents one tenant from probing another's invite state.
    """
    verified_tenant = _verify_internal_bearer(request)
    _ensure_tenant_invites_table()
    with conn_ctx(DB_PATH) as conn:
        row = conn.execute(
            "SELECT tenant_name, port, display_label, expires_at, consumed_at, target_email "
            "FROM tenant_invites WHERE token = ?",
            (token,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "invite not found")
    _enforce_tenant_match(verified_tenant, row["tenant_name"])
    if row["consumed_at"]:
        raise HTTPException(410, "invite already consumed")
    # expires_at comes back as a datetime on Postgres; compare in UTC.
    from datetime import datetime as _dt, timezone as _tz
    exp = row["expires_at"]
    if isinstance(exp, str):
        exp = _dt.fromisoformat(exp.replace("Z", "+00:00"))
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=_tz.utc)
    if exp < _dt.now(_tz.utc):
        raise HTTPException(410, "invite expired")
    return {
        "tenant_name":   row["tenant_name"],
        "port":          row["port"],
        "display_label": row["display_label"],
        "expires_at":    exp.isoformat(),
        # Populated for reset invites; tenant Yorik uses this to scope
        # a password reset to a specific existing admin. NULL/None for
        # initial-setup invites.
        "target_email":  row.get("target_email") if isinstance(row, dict) else row["target_email"],
    }


@app.post("/api/internal/invites/{token}/consume")
def invite_consume_endpoint(token: str, request: Request) -> Dict[str, Any]:
    """Bearer-gated. Marks an invite as consumed. Idempotent on
    already-consumed (returns the same shape so a retried setup
    doesn't 410 mid-flow). Called by the tenant's /api/auth/setup
    after it successfully creates the admin user. Enforces
    bearer-tenant == invite-tenant so a hostile tenant can't burn
    another tenant's invite preemptively."""
    verified_tenant = _verify_internal_bearer(request)
    _ensure_tenant_invites_table()
    from datetime import datetime as _dt, timezone as _tz
    now = _dt.now(_tz.utc)
    with conn_ctx(DB_PATH) as conn:
        row = conn.execute(
            "SELECT tenant_name, consumed_at FROM tenant_invites WHERE token = ?",
            (token,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "invite not found")
        _enforce_tenant_match(verified_tenant, row["tenant_name"])
        if not row["consumed_at"]:
            conn.execute(
                "UPDATE tenant_invites SET consumed_at = ? WHERE token = ?",
                (now, token),
            )
            conn.commit()
    return {"tenant_name": row["tenant_name"], "consumed_at": now.isoformat()}


class InternalTenantDropIn(BaseModel):
    tenant_name: str


class InternalRegisterBearerIn(BaseModel):
    tenant_name: str
    token: str


@app.post("/api/internal/register-tenant-bearer")
def internal_register_bearer_endpoint(
    body: InternalRegisterBearerIn,
    request: Request,
) -> Dict[str, Any]:
    """Register a per-tenant bearer token in the host's
    tenant_bearer_tokens table. Called by scripts/create-tenant.sh
    after it generates and writes data/tenants/<name>/internal_token.

    Bootstrap chicken/egg: this endpoint accepts the LEGACY single
    data/internal_token bearer ONLY. New per-tenant tokens are
    issued through it; tenants themselves never call this. After a
    tenant is registered, all its OTHER /api/internal/* calls use
    the per-tenant bearer instead.
    """
    verified_tenant = _verify_internal_bearer(request)
    if verified_tenant is not None:
        # Per-tenant bearer must NOT be able to register new tenants —
        # would let a compromised tenant grant itself new identities.
        raise HTTPException(
            403,
            "register-tenant-bearer requires the host's legacy bearer, "
            "not a per-tenant token",
        )
    import re as _re
    tenant_name = body.tenant_name.strip().lower()
    if not _re.match(r"^[a-z][a-z0-9_]{0,23}$", tenant_name):
        raise HTTPException(400, "invalid tenant_name")
    if not (32 <= len(body.token) <= 200):
        raise HTTPException(400, "token length out of range")
    from . import external_users as _eu
    _eu.register_tenant_bearer(tenant_name, body.token)
    return {"ok": True, "tenant_name": tenant_name}


@app.post("/api/internal/unregister-tenant-bearer")
def internal_unregister_bearer_endpoint(
    body: InternalTenantDropIn,  # reuse the same {tenant_name} shape
    request: Request,
) -> Dict[str, Any]:
    """Drop a tenant's bearer from the registry. Called by
    scripts/drop-tenant.sh after the upstream cleanup. Like
    register, legacy-bearer-only — a per-tenant token shouldn't be
    able to deregister other tenants (or itself: drop is the
    operator's call, not the tenant's)."""
    verified_tenant = _verify_internal_bearer(request)
    if verified_tenant is not None:
        raise HTTPException(
            403,
            "unregister-tenant-bearer requires the host's legacy bearer",
        )
    import re as _re
    tenant_name = body.tenant_name.strip().lower()
    if not _re.match(r"^[a-z][a-z0-9_]{0,23}$", tenant_name):
        raise HTTPException(400, "invalid tenant_name")
    from . import external_users as _eu
    _eu.unregister_tenant_bearer(tenant_name)
    return {"ok": True, "tenant_name": tenant_name}


@app.post("/api/internal/tenant/drop")
def internal_tenant_drop_endpoint(
    body: InternalTenantDropIn,
    request: Request,
) -> Dict[str, Any]:
    """Soft-delete every upstream Immich + Paperless user that belongs
    to this tenant (matched by the `<tenant>+` / `<tenant>_` namespace
    convention). Called by `scripts/drop-tenant.sh` before it drops
    the tenant's Postgres database.

    Idempotent: re-running after a partial failure clean up whatever's
    still there. Returns per-service counts + the list of any errors
    so the operator gets a clear picture of what got cleaned vs left
    behind.

    Unlike /api/internal/provision this endpoint does NOT require the
    tenant manifest to exist — the typical call order is "drop
    upstream first, then drop manifest + DB," and we also want a way
    to clean orphans after the manifest is gone (e.g. a botched
    previous drop). Tenant_name format is still validated to keep
    the namespace-prefix lookup safe.
    """
    verified_tenant = _verify_internal_bearer(request)

    tenant_name = body.tenant_name.strip().lower()
    import re as _re
    if not _re.match(r"^[a-z][a-z0-9_]{0,23}$", tenant_name):
        raise HTTPException(400, "invalid tenant_name")
    # Per-tenant bearers can only drop their OWN upstream users.
    # Legacy bearer (host-side drop-tenant.sh) can drop any tenant —
    # the operator script needs that, and operators are the trust
    # anchor anyway.
    _enforce_tenant_match(verified_tenant, tenant_name)

    from . import external_users as _eu
    return _eu.delete_tenant_upstream_users(tenant_name)


# ── permission grant CRUD ──────────────────────────────────────────────────

class GrantIn(BaseModel):
    layout_id: str
    connector_name: str


@app.get("/api/permissions")
def list_permissions() -> List[Dict[str, Any]]:
    """All active connector grants. Frontend renders this in a settings panel."""
    with conn_ctx(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, layout_id, connector_name, granted_at, granted_by_role "
            "FROM connector_grants WHERE revoked_at IS NULL ORDER BY granted_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/permissions", status_code=201)
def grant_permission(body: GrantIn, role: str = Depends(_auth.current_role)) -> Dict[str, Any]:
    """Grant a layout permission to use a connector. Admin only."""
    if normalize_role(role) not in ("admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="role required: admin")
    if not connectors.get(body.connector_name):
        raise HTTPException(status_code=404, detail=f"unknown connector '{body.connector_name}'")
    with conn_ctx(DB_PATH) as conn:
        # Upsert: if revoked previously, reactivate by clearing revoked_at.
        existing = conn.execute(
            "SELECT id FROM connector_grants WHERE layout_id = ? AND connector_name = ?",
            (body.layout_id, body.connector_name),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE connector_grants SET revoked_at = NULL, granted_at = datetime('now'), granted_by_role = ? WHERE id = ?",
                (role, existing["id"]),
            )
            gid = existing["id"]
        else:
            cur = conn.execute(
                "INSERT INTO connector_grants (layout_id, connector_name, granted_by_role) VALUES (?, ?, ?)",
                (body.layout_id, body.connector_name, role),
            )
            gid = cur.lastrowid
        row = conn.execute("SELECT * FROM connector_grants WHERE id = ?", (gid,)).fetchone()
    return dict(row)


@app.delete("/api/permissions/{layout_id}/{connector_name}", status_code=204, response_class=Response)
def revoke_permission(layout_id: str, connector_name: str, role: str = Depends(_auth.current_role)) -> Response:
    if normalize_role(role) not in ("admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="role required: admin")
    with conn_ctx(DB_PATH) as conn:
        conn.execute(
            "UPDATE connector_grants SET revoked_at = datetime('now') "
            "WHERE layout_id = ? AND connector_name = ? AND revoked_at IS NULL",
            (layout_id, connector_name),
        )
    return Response(status_code=204)


# ── apps registry ──────────────────────────────────────────────────────────

@app.get("/api/apps")
def list_apps_endpoint(role: str = Depends(_auth.current_role)) -> List[Dict[str, Any]]:
    """List installed apps the role may see — home screen reads this."""
    normalize_role(role)
    return [apps_mod.to_dict(a) for a in apps_mod.list_all(role=role)]


@app.get("/api/apps/opt-in")
def list_opt_in_apps_endpoint(role: str = Depends(_auth.current_role)) -> List[Dict[str, Any]]:
    """List every opt-in app + current enabled state. Powers Settings → Apps.
    Admin-only because flipping these affects every user on the box."""
    if normalize_role(role) not in ("admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="role required: admin")
    return apps_mod.list_opt_in_apps()


@app.post("/api/apps/{app_id}/enable")
def enable_app(app_id: str, role: str = Depends(_auth.current_role)) -> Dict[str, Any]:
    """Flip an opt-in app on. Next /api/apps call shows it in the dock."""
    if normalize_role(role) not in ("admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="role required: admin")
    target = apps_mod.get(app_id)
    if not target:
        raise HTTPException(status_code=404, detail=f"app '{app_id}' not registered")
    if not target.opt_in:
        # Non-opt-in apps are always on; toggling them is a no-op that
        # would confuse the UI. Reject explicitly so a buggy client can't
        # silently persist a meaningless flag.
        raise HTTPException(status_code=400, detail=f"app '{app_id}' is not opt-in")
    apps_mod.set_opt_in_enabled(app_id, True)
    return {"app_id": app_id, "enabled": True}


@app.post("/api/apps/{app_id}/disable")
def disable_app(app_id: str, role: str = Depends(_auth.current_role)) -> Dict[str, Any]:
    """Flip an opt-in app off. Hides it from /api/apps; data is untouched."""
    if normalize_role(role) not in ("admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="role required: admin")
    target = apps_mod.get(app_id)
    if not target:
        raise HTTPException(status_code=404, detail=f"app '{app_id}' not registered")
    if not target.opt_in:
        raise HTTPException(status_code=400, detail=f"app '{app_id}' is not opt-in")
    apps_mod.set_opt_in_enabled(app_id, False)
    return {"app_id": app_id, "enabled": False}


@app.get("/api/apps/{app_id}/manifest")
def get_app_manifest(app_id: str, role: str = Depends(_auth.current_role)) -> Dict[str, Any]:
    """Return the manifest of an installed community app (read-only)."""
    normalize_role(role)
    loaded = app_loader.get_loaded(app_id)
    if not loaded:
        raise HTTPException(status_code=404, detail=f"app '{app_id}' not loaded (or is a builtin without a manifest)")
    return loaded.manifest


@app.get("/api/apps/{app_id}/ui", response_class=Response)
def get_app_ui(app_id: str, role: str = Depends(_auth.current_role)) -> Response:
    """Serve the app's UI JS bundle. Sandboxed iframe loads this in srcdoc.

    Returns raw JavaScript; the frontend wraps it in the iframe shell with
    window.yorik already injected.
    """
    normalize_role(role)
    loaded = app_loader.get_loaded(app_id)
    if not loaded:
        raise HTTPException(status_code=404, detail=f"app '{app_id}' not loaded")
    ui_path = loaded.source_dir / loaded.manifest["entry_ui"]
    if not ui_path.exists():
        raise HTTPException(status_code=404, detail=f"UI file {loaded.manifest['entry_ui']!r} missing")
    headers: Dict[str, str] = {}
    # Phase E §7 — v2 manifests get the iframe Content-Security-Policy
    # header derived from network.outbound. v1 manifests don't (they
    # pre-date the manifest's outbound declaration and would break).
    if loaded.manifest.get("manifest_version") == 2:
        from . import app_consent
        headers["Content-Security-Policy"] = app_consent.iframe_csp_for(
            loaded.manifest,
            supabase_origin=os.getenv("YORIK_SUPABASE_PUBLIC_URL", "http://localhost:8400"),
        )
    return Response(
        content=ui_path.read_text(),
        media_type="application/javascript",
        headers=headers,
    )


@app.post("/api/apps/install")
def install_app(
    source_dir: str = Query(..., description="Absolute path to the app source directory"),
    role: str = Depends(_auth.current_role),
) -> Dict[str, Any]:
    """Install (or reinstall) an app from a local directory. Admin only."""
    if normalize_role(role) not in ("admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="role required: admin")
    try:
        loaded = app_loader.install_app_from_dir(Path(source_dir))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "app_id": loaded.app_id,
        "operations": loaded.operation_connector_names,
        "data_dir": str(loaded.data_dir),
        "requires_tables_external": loaded.manifest.get("requires_tables_external", []),
        "requires_connectors": loaded.manifest.get("requires_connectors", []),
    }


# ── Phase E §7 consent: preflight + confirm ───────────────────────────────

class _PreflightIn(BaseModel):
    # Source for the manifest to preview. The simplest form is "give
    # me the manifest in the request body"; the other form references
    # a local source directory (matches /api/apps/install's style).
    manifest: Optional[Dict[str, Any]] = None
    source_dir: Optional[str] = None


@app.post("/api/apps/install/preflight")
def apps_install_preflight(
    body: _PreflightIn,
    role: str = Depends(_auth.current_role),
) -> Dict[str, Any]:
    """Render a manifest into the structured consent payload.

    Doesn't install or write anything. The frontend POSTs the manifest
    (or a path to one) and gets back the scopes/cannot summary plus
    the validator's errors so the dialog can refuse early.
    """
    if normalize_role(role) not in ("admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="role required: admin")
    if not body.manifest and not body.source_dir:
        raise HTTPException(status_code=400, detail="manifest or source_dir required")

    from . import app_consent

    if body.manifest:
        manifest = body.manifest
    else:
        import json as _json
        mp = Path(body.source_dir) / "manifest.json"
        if not mp.exists():
            raise HTTPException(status_code=404, detail=f"manifest.json missing at {mp}")
        try:
            manifest = _json.loads(mp.read_text())
        except _json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"manifest.json invalid: {exc}")

    errs = app_loader._validate_manifest(manifest)
    summary = app_consent.summarize_for_consent(manifest)
    return {
        "manifest": manifest,
        "errors": errs,
        "summary": summary,
        "iframe_csp": app_consent.iframe_csp_for(
            manifest,
            supabase_origin=os.getenv("YORIK_SUPABASE_PUBLIC_URL", "http://localhost:8400"),
        ),
    }


class _ConfirmIn(BaseModel):
    source_dir: str
    # The frontend posts back the scope summary it actually showed.
    # We snapshot it into the audit row so the consent record matches
    # what the user saw — important for disputes ("the app says I
    # granted X, but I never saw X on the screen").
    shown_summary: Optional[Dict[str, Any]] = None


@app.post("/api/apps/install/confirm")
def apps_install_confirm(
    body: _ConfirmIn,
    role: str = Depends(_auth.current_role),
    user: Dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Run the actual Phase E §6 install after the user confirms the dialog.

    Uses install_app_from_dir_copy so the source is persisted into
    apps/<id>/ and survives a uvicorn restart. The caller's user id
    is recorded in installed_apps.granted_by_user_id.
    """
    if normalize_role(role) not in ("admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="role required: admin")
    src = Path(body.source_dir)
    if not src.exists():
        raise HTTPException(status_code=404, detail=f"source_dir missing: {src}")
    try:
        loaded = app_loader.install_app_from_dir_copy(src)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "app_id": loaded.app_id,
        "operations": loaded.operation_connector_names,
        "manifest_version": loaded.manifest.get("manifest_version", 1),
    }


# ── Phase E §7 — installed-apps ledger (Settings → Installed apps) ─────────

@app.get("/api/apps/installed-v2")
def list_installed_v2_apps(
    role: str = Depends(_auth.current_role),
) -> List[Dict[str, Any]]:
    """Active v2 installs from the installed_apps ledger.

    Powers Settings → Installed apps so the admin can see granted
    scopes per app and uninstall. Includes the manifest snapshot +
    granted_permissions exactly as recorded at install time.
    """
    if normalize_role(role) not in ("admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="role required: admin")
    from . import app_schema_lifecycle as _lifecycle
    return [
        {
            "app_id": r["app_id"],
            "owned_schema": r["owned_schema"],
            "manifest": r["manifest"],
            "granted_permissions": r["manifest"].get("permissions") or {},
            "source_dir": r["source_dir"],
        }
        for r in _lifecycle.list_active_v2_installs()
    ]


# ── Phase E §6.6 — per-app scoped JWT ──────────────────────────────────────

@app.get("/api/apps/{app_id}/jwt")
def get_app_jwt(
    app_id: str,
    user: Dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Mint an app-scoped JWT for the current user.

    The iframe calls this once on load (and again on expiry) and
    hands the token to supabase-js. PostgREST validates the signature
    against the shared JWT_SECRET, switches into the per-app role
    declared in `role`, and exposes the user's UUID via auth.uid()
    for RLS. The role only sees the app's own schema + projection
    views — every other table responds 403 to that role.

    Auth: requires a logged-in user (cookie or Supabase JWT). The
    installed_apps row must be active.
    """
    from . import app_jwt as _appjwt, app_schema_lifecycle as _lifecycle
    installed = _lifecycle.get_installed_app(app_id)
    if not installed or installed["manifest"].get("manifest_version") != 2:
        raise HTTPException(status_code=404, detail=f"v2 app {app_id!r} not installed")
    try:
        return _appjwt.mint_app_jwt(app_id=app_id, user_uuid=user["id"])
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── app self-call (iframe -> own operations) ──────────────────────────────

@app.post("/api/apps/{app_id}/op/{op_name}")
async def invoke_own_operation(
    app_id: str,
    op_name: str,
    body: ConnectorInvokeIn,
    role: str = Depends(_auth.current_role),
) -> Dict[str, Any]:
    """Let an installed app's iframe call ITS OWN operation by name.

    Bypasses the per-layout connector grant (apps are user-installed and
    self-authorize). The op_name is concatenated to app_id, so an app
    can never reach another app's operations through this endpoint.

    The operation's own `permissions` field (admin/member/...) is still
    enforced inside the @operation wrapper installed by app_loader.
    """
    full_name = f"{app_id}.{op_name}"
    spec = connectors.get(full_name)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"operation {full_name!r} not installed")
    if spec.backend != f"app:{app_id}":
        # Defence in depth: if the connector exists under that name but
        # wasn't registered by this app, refuse.
        raise HTTPException(status_code=403, detail="operation does not belong to this app")
    try:
        return await connectors.invoke(full_name, body.params or {})
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"operation failed: {exc}")


# ── marketplace ────────────────────────────────────────────────────────────

@app.get("/api/apps/available")
def list_available_apps(
    role: str = Depends(_auth.current_role),
) -> List[Dict[str, Any]]:
    """Marketplace listing: every entry in marketplace/catalog.json, each
    annotated with whether it's already installed locally."""
    from . import app_catalog
    installed_ids = set(app_loader._LOADED.keys()) | app_loader._BUILTIN_APP_IDS
    try:
        entries = app_catalog.load_catalog()
    except app_catalog.CatalogError as exc:
        raise HTTPException(status_code=500, detail=f"catalog malformed: {exc}")
    return [app_catalog.entry_to_dict(e, installed=(e.id in installed_ids)) for e in entries]


@app.post("/api/apps/install_from_catalog/{app_id}")
def install_from_catalog(
    app_id: str,
    role: str = Depends(_auth.current_role),
) -> Dict[str, Any]:
    """Install an app by its marketplace catalog id. Admin only.

    Copies the bundled source into apps/<id>/ (so the install persists
    across uvicorn restarts) and runs the normal app-loader lifecycle.
    """
    if normalize_role(role) not in ("admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="role required: admin")
    from . import app_catalog
    entry = app_catalog.find_entry(app_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"app {app_id!r} not in catalog")
    try:
        source = app_catalog.resolve_source_dir(entry)
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc))
    if not source.exists():
        raise HTTPException(status_code=500, detail=f"catalog source dir missing: {source}")
    try:
        loaded = app_loader.install_app_from_dir_copy(source)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "app_id": loaded.app_id,
        "operations": loaded.operation_connector_names,
        "data_dir": str(loaded.data_dir),
        "requires_tables_external": loaded.manifest.get("requires_tables_external", []),
        "requires_connectors": loaded.manifest.get("requires_connectors", []),
    }


@app.delete("/api/apps/{app_id}", status_code=204, response_class=Response)
def uninstall_app_endpoint(
    app_id: str,
    wipe_data: bool = Query(True),
    role: str = Depends(_auth.current_role),
) -> Response:
    """Uninstall a community app. Admin only. By default wipes the app's
    data dir; pass wipe_data=false to preserve it (e.g. before reinstalling)."""
    if normalize_role(role) not in ("admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="role required: admin")
    if app_id in {"calendar", "chat", "docs"}:
        raise HTTPException(status_code=400, detail="cannot uninstall a builtin app")
    if not app_loader.uninstall_app(app_id, wipe_data=wipe_data):
        raise HTTPException(status_code=404, detail=f"app '{app_id}' not installed")
    return Response(status_code=204)


# ── app permission grants ──────────────────────────────────────────────────

class AppGrantIn(BaseModel):
    app_id: str
    resource_type: str   # 'table' | 'connector'
    resource_db: Optional[str] = None   # 'family' | 'documents' for table grants; null for connectors
    resource_name: str
    access: str = "read"  # 'read' | 'write' | 'read+write'


@app.get("/api/app-grants")
def list_app_grants() -> List[Dict[str, Any]]:
    with conn_ctx(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, app_id, resource_type, resource_db, resource_name, access, granted_at, granted_by_role "
            "FROM app_grants WHERE revoked_at IS NULL ORDER BY granted_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/app-grants", status_code=201)
def grant_app_permission(body: AppGrantIn, role: str = Depends(_auth.current_role)) -> Dict[str, Any]:
    if normalize_role(role) not in ("admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="role required: admin")
    if body.resource_type not in ("table", "connector"):
        raise HTTPException(status_code=400, detail="resource_type must be 'table' or 'connector'")
    if body.access not in ("read", "write", "read+write"):
        raise HTTPException(status_code=400, detail="access must be 'read', 'write', or 'read+write'")
    with conn_ctx(DB_PATH) as conn:
        existing = conn.execute(
            "SELECT id FROM app_grants WHERE app_id=? AND resource_type=? AND "
            "(resource_db=? OR (resource_db IS NULL AND ? IS NULL)) AND resource_name=?",
            (body.app_id, body.resource_type, body.resource_db, body.resource_db, body.resource_name),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE app_grants SET access=?, revoked_at=NULL, granted_at=datetime('now'), granted_by_role=? WHERE id=?",
                (body.access, role, existing["id"]),
            )
            gid = existing["id"]
        else:
            cur = conn.execute(
                "INSERT INTO app_grants (app_id, resource_type, resource_db, resource_name, access, granted_by_role) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (body.app_id, body.resource_type, body.resource_db, body.resource_name, body.access, role),
            )
            gid = cur.lastrowid
        row = conn.execute("SELECT * FROM app_grants WHERE id=?", (gid,)).fetchone()
    return dict(row)


@app.delete("/api/app-grants/{grant_id}", status_code=204, response_class=Response)
def revoke_app_permission(grant_id: int, role: str = Depends(_auth.current_role)) -> Response:
    if normalize_role(role) not in ("admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="role required: admin")
    with conn_ctx(DB_PATH) as conn:
        conn.execute("UPDATE app_grants SET revoked_at=datetime('now') WHERE id=?", (grant_id,))
    return Response(status_code=204)


# ── CSV import (works for any installed app's schema) ──────────────────────

class CsvImportIn(BaseModel):
    table: str
    columns: Dict[str, str]   # CSV column → app column
    skip_first_row: bool = True
    on_duplicate: str = "skip"  # 'skip' | 'update' | 'error'
    dry_run: bool = True
    delimiter: str = ","


@app.post("/api/apps/{app_id}/import")
async def import_csv_to_app(
    app_id: str,
    mapping: str = Query(..., description="JSON-encoded CsvImportIn"),
    file: UploadFile = File(...),
    role: str = Depends(_auth.current_role),
) -> Dict[str, Any]:
    """Import a CSV into one of the app's tables. Admin only.

    Frontend wizard sends mapping as a JSON string in the query so we
    sidestep multipart-of-JSON gymnastics. Dry-run validates without
    committing; the user reviews preview/errors then re-sends with
    dry_run=false.
    """
    if normalize_role(role) not in ("admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="role required: admin")
    loaded = app_loader.get_loaded(app_id)
    if not loaded:
        raise HTTPException(status_code=404, detail=f"app '{app_id}' not loaded")
    try:
        mapping_obj = CsvImportIn.model_validate_json(mapping)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid mapping JSON: {exc}")

    # Read CSV
    import csv as csv_mod
    import io as io_mod
    raw = await file.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1", errors="replace")
    reader = csv_mod.reader(io_mod.StringIO(text), delimiter=mapping_obj.delimiter)
    rows = list(reader)
    if not rows:
        return {"imported": 0, "errors": [], "preview": [], "rows_in_file": 0}
    headers = rows[0]
    data_rows = rows[1:] if mapping_obj.skip_first_row else rows

    # Map CSV columns → app columns. Validate the target table exists in the app's DB.
    from .app_sdk import db as app_db
    import sqlite3 as sqlite3_mod
    conn: sqlite3_mod.Connection = app_db(app_id=app_id)
    try:
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({mapping_obj.table})")}
        if not existing:
            raise HTTPException(status_code=400, detail=f"table {mapping_obj.table!r} doesn't exist in app {app_id!r}")

        app_columns = list(mapping_obj.columns.values())
        for col in app_columns:
            if col not in existing:
                raise HTTPException(status_code=400, detail=f"target column {col!r} not in table {mapping_obj.table!r}")

        # Build column index list — CSV positions → app columns
        csv_indices: Dict[str, int] = {}
        for csv_col in mapping_obj.columns:
            if csv_col not in headers:
                raise HTTPException(status_code=400, detail=f"CSV column {csv_col!r} not found in header row {headers}")
            csv_indices[csv_col] = headers.index(csv_col)

        # Walk rows
        preview: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []
        imported = 0
        placeholders = ",".join("?" * len(app_columns))
        col_list = ",".join(app_columns)
        for i, raw_row in enumerate(data_rows, start=(2 if mapping_obj.skip_first_row else 1)):
            try:
                values = [raw_row[csv_indices[csv_col]] for csv_col in mapping_obj.columns]
            except IndexError:
                errors.append({"row": i, "error": "row has fewer columns than CSV header"})
                continue
            if len(preview) < 5:
                preview.append(dict(zip(app_columns, values)))
            if mapping_obj.dry_run:
                imported += 1
                continue
            try:
                conn.execute(
                    f"INSERT OR {'IGNORE' if mapping_obj.on_duplicate == 'skip' else 'REPLACE' if mapping_obj.on_duplicate == 'update' else 'ABORT'} "
                    f"INTO {mapping_obj.table} ({col_list}) VALUES ({placeholders})",
                    values,
                )
                imported += 1
            except sqlite3_mod.IntegrityError as exc:
                if mapping_obj.on_duplicate == "skip":
                    pass  # silently skipped via IGNORE; shouldn't happen
                else:
                    errors.append({"row": i, "error": f"integrity error: {exc}"})
            except Exception as exc:
                errors.append({"row": i, "error": str(exc)[:200]})
        if not mapping_obj.dry_run:
            conn.commit()
        return {
            "rows_in_file": len(data_rows),
            "imported": imported,
            "errors_count": len(errors),
            "errors": errors[:20],   # cap to keep response small
            "preview": preview,
            "dry_run": mapping_obj.dry_run,
        }
    finally:
        conn.close()


# ── conversations (for the Chat app's sidebar) ─────────────────────────────

@app.get("/api/conversations")
def list_conversations(
    user: Dict[str, Any] = Depends(_auth.current_user),
    limit: int = Query(50, ge=1, le=200),
) -> List[Dict[str, Any]]:
    """Recent conversations for the current user. Used by the Chat app sidebar.

    Filter: user_id (preferred) OR user_role (back-compat for legacy rows
    where user_id IS NULL). The user_id branch survives role changes —
    promoting a member to admin no longer hides their chat history.

    Returns the first 80 chars of the first user message as a preview so the
    sidebar can show something readable without loading the full transcript.

    Reads from BOTH the new ``agent_conversations`` table (current
    agent backend) and the legacy ``conversations`` table (pre-Phase-4
    rows that nobody writes to anymore but still hold valid history).
    Dedup keys on conversation id, preferring the new table when both
    exist.
    """
    role = user["role"]
    user_id = user["id"]
    normalize_role(role)
    import json as _json
    by_id: Dict[str, Dict[str, Any]] = {}

    def _add(row: Any, messages_col: str, role_col: str,
             title: Optional[str] = None, pinned: bool = False) -> None:
        try:
            msgs = _json.loads(row[messages_col] or "[]")
        except (ValueError, TypeError):
            msgs = []
        if not isinstance(msgs, list):
            msgs = []
        # Skip the system message when looking for a preview.
        first_user = next(
            (m for m in msgs if isinstance(m, dict) and m.get("role") == "user"),
            None,
        )
        preview = (first_user or {}).get("content", "") or ""
        by_id[row["id"]] = {
            "id":            row["id"],
            "title":         title or None,  # null when not generated yet
            "preview":       preview[:80] + ("…" if len(preview) > 80 else ""),
            "message_count": len(msgs),
            "pinned":        bool(pinned),
            "created_at":    row["created_at"],
            "updated_at":    row["updated_at"],
        }

    with conn_ctx(DB_PATH) as conn:
        # NEW table first (preferred — overwrites legacy on collision).
        # `title` (021) + `pinned` (022) are recent columns; tolerate
        # both missing so this endpoint stays alive across upgrades.
        # WHERE filter: prefer user_id (survives role changes); fall
        # back to user_role for legacy rows where user_id IS NULL.
        _owner_where = "WHERE (user_id = ? OR (user_id IS NULL AND user_role = ?))"
        _owner_params = (user_id, role)
        try:
            new_rows = conn.execute(
                "SELECT id, user_role, messages_json AS messages, title, pinned, "
                "       created_at, updated_at "
                f"FROM agent_conversations {_owner_where} "
                "ORDER BY pinned DESC, updated_at DESC LIMIT ?",
                (*_owner_params, limit),
            ).fetchall()
            has_title_col = True
            has_pinned_col = True
        except sqlite3.OperationalError:
            try:
                new_rows = conn.execute(
                    "SELECT id, user_role, messages_json AS messages, title, "
                    "       created_at, updated_at "
                    f"FROM agent_conversations {_owner_where} "
                    "ORDER BY updated_at DESC LIMIT ?",
                    (*_owner_params, limit),
                ).fetchall()
                has_title_col = True
                has_pinned_col = False
            except sqlite3.OperationalError:
                try:
                    new_rows = conn.execute(
                        "SELECT id, user_role, messages_json AS messages, "
                        "       created_at, updated_at "
                        f"FROM agent_conversations {_owner_where} "
                        "ORDER BY updated_at DESC LIMIT ?",
                        (*_owner_params, limit),
                    ).fetchall()
                    has_title_col = False
                    has_pinned_col = False
                except sqlite3.OperationalError:
                    new_rows = []
                    has_title_col = False
                    has_pinned_col = False
        for r in new_rows:
            _add(r, "messages", "user_role",
                 title=r["title"] if has_title_col else None,
                 pinned=bool(r["pinned"]) if has_pinned_col else False)

        # LEGACY table — fill in anything the new one doesn't cover.
        legacy_rows = conn.execute(
            "SELECT id, user_role, messages, created_at, updated_at "
            "FROM conversations WHERE user_role = ? "
            "ORDER BY updated_at DESC LIMIT ?",
            (role, limit),
        ).fetchall()
        for r in legacy_rows:
            if r["id"] not in by_id:
                _add(r, "messages", "user_role")

    out = list(by_id.values())
    out.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
    return out[:limit]


@app.get("/api/conversations/{conversation_id}")
def get_conversation(conversation_id: str, role: str = Depends(_auth.current_role)) -> Dict[str, Any]:
    """Full message history for a single conversation. The Chat app loads this
    when the user clicks a past conversation.

    Reads from the new ``agent_conversations`` table first; falls back
    to the legacy ``conversations`` table for pre-cutover rows. Hydrates
    persisted per-turn agent_trace blobs (dev mode) onto each assistant
    message so the chat UI's Debug pane survives a page reload.
    """
    normalize_role(role)
    import json as _json
    row = None
    source: str = "legacy"
    msgs: List[Dict[str, Any]] = []

    title: Optional[str] = None
    with conn_ctx(DB_PATH) as conn:
        # NEW table first. Tolerate `title` column missing (pre-021).
        try:
            row = conn.execute(
                "SELECT id, user_role, messages_json AS messages, title, "
                "       created_at, updated_at "
                "FROM agent_conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if row:
                source = "agent"
                title = row["title"]
        except sqlite3.OperationalError:
            try:
                row = conn.execute(
                    "SELECT id, user_role, messages_json AS messages, "
                    "       created_at, updated_at "
                    "FROM agent_conversations WHERE id = ?",
                    (conversation_id,),
                ).fetchone()
                if row:
                    source = "agent"
            except sqlite3.OperationalError:
                row = None
        # Fall back to legacy if not found in new table.
        if row is None:
            row = conn.execute(
                "SELECT id, user_role, messages, created_at, updated_at "
                "FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            source = "legacy"

    if not row:
        raise HTTPException(status_code=404, detail="conversation not found")
    if row["user_role"] != role and normalize_role(role) not in ("admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="not yours to view")

    try:
        msgs = _json.loads(row["messages"] or "[]")
    except (ValueError, TypeError):
        msgs = []
    if not isinstance(msgs, list):
        msgs = []

    # Hydrate trace + metadata BEFORE filtering so we can map blobs
    # to messages by their full-list index (that's how they were stored).
    if source == "agent":
        try:
            from .agent.conversation_io import load_traces_for
            traces = load_traces_for(conversation_id)
        except Exception:
            traces = {}
        for idx, m in enumerate(msgs):
            if isinstance(m, dict) and idx in traces:
                m["agent_trace"] = traces[idx]

    # Surface persisted extras (photos / documents / ui_actions /
    # tool_trace) from `metadata` to top-level keys — legacy-format
    # messages stored these in a metadata sub-dict that the frontend
    # doesn't read directly.
    for m in msgs:
        meta = m.get("metadata") if isinstance(m, dict) else None
        if not isinstance(meta, dict):
            continue
        for k in ("photos", "documents", "ui_actions", "tool_trace"):
            if k in meta and k not in m:
                m[k] = meta[k]

    # Filter to what the chat UI is built to render. The agent_conversations
    # table stores the full OpenAI message list (system + user + assistant
    # + tool + assistant…) because the loop needs every line for the next
    # turn's context. The chat UI only wants user messages and the FINAL
    # assistant reply per turn — not the system prompt, not tool results,
    # not intermediate "thinking-then-tool-calls" assistant turns.
    #
    # Heuristic: keep user messages always; keep assistant messages that
    # have non-empty content (skip the assistant-with-only-tool_calls
    # intermediates); drop system + tool roles entirely. Trace + metadata
    # hydration already happened above, so the kept assistant messages
    # carry their agent_trace + photos + documents.
    if source == "agent":
        msgs = [
            m for m in msgs
            if isinstance(m, dict)
            and m.get("role") in ("user", "assistant")
            and (m.get("role") == "user" or (m.get("content") or "").strip())
        ]

    return {
        "id":         row["id"],
        "title":      title,
        "user_role":  row["user_role"],
        "messages":   msgs,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@app.delete("/api/conversations/{conversation_id}", status_code=204, response_class=Response)
def delete_conversation(conversation_id: str, role: str = Depends(_auth.current_role)) -> Response:
    normalize_role(role)
    with conn_ctx(DB_PATH) as conn:
        # The active table is `agent_conversations` (since the agent
        # rebuild). `conversations` is the legacy table the list
        # endpoint still UNIONs for back-compat; check + delete from
        # BOTH so a delete works whichever table the row lives in.
        # The ownership check uses whichever row exists; admin
        # bypasses the role match either way.
        row = conn.execute(
            "SELECT user_role FROM agent_conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT user_role FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
        if row is None:
            # Idempotent: nothing to delete, succeed silently.
            return Response(status_code=204)
        if row["user_role"] != role and role not in ("platform_admin", "admin"):
            raise HTTPException(status_code=403, detail="not yours to delete")
        conn.execute("DELETE FROM agent_conversations WHERE id = ?", (conversation_id,))
        conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
    return Response(status_code=204)


# ─────────────── pin / unpin a conversation ───────────────
# Drives the "📌 Pin" toggle in the chat sidebar. Pinned threads bubble
# above the date groupings ("Today / Yesterday / Earlier") so the
# user can always reach a long-running planning thread in one click.

class ConversationPinIn(BaseModel):
    pinned: bool


@app.post("/api/conversations/{conversation_id}/pin")
def pin_conversation(
    conversation_id: str,
    body: ConversationPinIn,
    role: str = Depends(_auth.current_role),
) -> Dict[str, Any]:
    normalize_role(role)
    with conn_ctx(DB_PATH) as conn:
        try:
            row = conn.execute(
                "SELECT user_role FROM agent_conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            raise HTTPException(404, "conversation not found")
        if not row:
            raise HTTPException(404, "conversation not found")
        if row["user_role"] != role and role not in ("platform_admin", "admin"):
            raise HTTPException(403, "not yours to pin")
        try:
            conn.execute(
                "UPDATE agent_conversations SET pinned = ? WHERE id = ?",
                (1 if body.pinned else 0, conversation_id),
            )
        except sqlite3.OperationalError:
            # `pinned` column missing — pre-022 DB. Surface a clear
            # error so the UI doesn't silently believe the pin stuck.
            raise HTTPException(
                503,
                "pin column missing — restart the backend to run pending "
                "migrations (021/022).",
            )
    return {"ok": True, "pinned": body.pinned}


# ─────────────── per-conversation attachment stash ───────────────
# A scratch pad of attachment pointers a user builds up while chatting.
# Each item is a tiny {url, filename, mimetype} triple — the bytes
# stay in Immich / Paperless / Yorik's documents.db. The email
# Composer reads the array on send and fetches each URL via
# pendingAttachments (Composer.tsx).

class StashItemIn(BaseModel):
    url:      str = Field(..., min_length=1, max_length=2048)
    filename: str = Field(..., min_length=1, max_length=256)
    mimetype: Optional[str] = Field(None, max_length=128)


def _stash_url_allowed(url: str) -> bool:
    # Only same-origin Yorik proxy URLs. Blocks any attempt to seed the
    # Composer with an external URL (the Composer's fetch is cookie-
    # authed, so an external URL would just fail, but rejecting up
    # front gives a cleaner error than a later 4xx in the browser).
    return (
        url.startswith("/api/photos/")
        or url.startswith("/api/documents/")
        or url.startswith("/paperless/api/documents/")
    )


@app.get("/api/conversations/{conversation_id}/stash")
def get_conversation_stash(
    conversation_id: str,
    role: str = Depends(_auth.current_role),
) -> Dict[str, Any]:
    normalize_role(role)
    from .agent.conversation_io import load_stash as _load_stash
    return {"items": _load_stash(conversation_id, role)}


@app.post("/api/conversations/{conversation_id}/stash")
def append_conversation_stash(
    conversation_id: str,
    body: StashItemIn,
    role: str = Depends(_auth.current_role),
) -> Dict[str, Any]:
    normalize_role(role)
    if not _stash_url_allowed(body.url):
        raise HTTPException(400, "url must be a Yorik proxy path")
    from .agent.conversation_io import load_stash as _load_stash, save_stash as _save_stash
    items = _load_stash(conversation_id, role)
    # Dedupe by (url, filename) — repeat-clicking "Attach" on the same
    # photo shouldn't stack duplicates that all fetch the same bytes.
    new_item = {
        "url": body.url,
        "filename": body.filename,
        "mimetype": body.mimetype or "",
    }
    if not any(
        i.get("url") == new_item["url"] and i.get("filename") == new_item["filename"]
        for i in items
    ):
        items.append(new_item)
    _save_stash(conversation_id, role, items)
    return {"ok": True, "items": items}


@app.delete("/api/conversations/{conversation_id}/stash/{index}", status_code=200)
def remove_conversation_stash_item(
    conversation_id: str,
    index: int,
    role: str = Depends(_auth.current_role),
) -> Dict[str, Any]:
    normalize_role(role)
    from .agent.conversation_io import load_stash as _load_stash, save_stash as _save_stash
    items = _load_stash(conversation_id, role)
    if 0 <= index < len(items):
        items.pop(index)
    _save_stash(conversation_id, role, items)
    return {"ok": True, "items": items}


@app.delete("/api/conversations/{conversation_id}/stash", status_code=200)
def clear_conversation_stash(
    conversation_id: str,
    role: str = Depends(_auth.current_role),
) -> Dict[str, Any]:
    normalize_role(role)
    from .agent.conversation_io import save_stash as _save_stash
    _save_stash(conversation_id, role, [])
    return {"ok": True, "items": []}


# ─────────────── mention / slash autocomplete ───────────────
# Drives the @-mention popover (contacts/events/docs) AND the slash
# command picker in the chat composer. One endpoint, multiple types,
# tight per-type limits so the LLM context stays manageable.

@app.get("/api/chat/mentions")
def chat_mentions(
    prefix: str = Query("", description="Substring to match on names/titles. Empty = recent N."),
    types: str = Query("contact,event,doc", description="CSV of types to search."),
    limit: int = Query(8, ge=1, le=30),
    role: str = Depends(_auth.current_role),
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Return matches for the chat's @-mention popover.

    Response shape:
        {
            "contact": [{"id": 5, "label": "Hans Müller", "sub": "Schulfreund"}, ...],
            "event":   [{"id": 12, "label": "Zahnarzt", "sub": "Fri 10:00"}, ...],
            "doc":     [{"id": 7, "label": "Mietvertrag.pdf", "sub": "2024-03-12"}],
        }

    Per-type best-effort — a missing table or schema variant returns
    an empty list for that type, not a 500.
    """
    normalize_role(role)
    requested = {t.strip() for t in (types or "").split(",") if t.strip()}
    q = (prefix or "").strip()
    like = f"%{q}%"
    out: Dict[str, List[Dict[str, Any]]] = {"contact": [], "event": [], "doc": []}

    with conn_ctx(DB_PATH) as conn:
        if "contact" in requested:
            try:
                if q:
                    rows = conn.execute(
                        "SELECT id, display_name, relation, kind, status "
                        "FROM contacts "
                        "WHERE status = 'active' "
                        "AND (display_name LIKE ? OR aliases LIKE ?) "
                        "ORDER BY (last_used_at IS NULL), last_used_at DESC, "
                        "         display_name ASC "
                        "LIMIT ?",
                        (like, like, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT id, display_name, relation, kind, status "
                        "FROM contacts WHERE status = 'active' "
                        "ORDER BY (last_used_at IS NULL), last_used_at DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
                out["contact"] = [{
                    "id":    r["id"],
                    "label": r["display_name"],
                    "sub":   r["relation"] or r["kind"] or "",
                } for r in rows]
            except sqlite3.OperationalError:
                pass

        if "event" in requested:
            try:
                if q:
                    rows = conn.execute(
                        "SELECT id, title, starts_at, location "
                        "FROM events "
                        "WHERE title LIKE ? "
                        "ORDER BY starts_at DESC LIMIT ?",
                        (like, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT id, title, starts_at, location "
                        "FROM events "
                        "ORDER BY starts_at DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
                out["event"] = [{
                    "id":    r["id"],
                    "label": r["title"],
                    "sub":   (r["starts_at"] or "")[:16].replace("T", " "),
                } for r in rows]
            except sqlite3.OperationalError:
                pass

        if "doc" in requested:
            # Documents live in a separate DB; query via documents module
            # so we inherit role filtering.
            try:
                from . import documents as _doc
                docs = _doc.list_documents(role=role)
                if q:
                    needle = q.lower()
                    docs = [d for d in docs
                            if needle in (d.get("title") or "").lower()]
                out["doc"] = [{
                    "id":    d["id"],
                    "label": d.get("title") or f"Document #{d['id']}",
                    "sub":   (d.get("created_at") or "")[:10],
                } for d in docs[:limit]]
            except Exception:  # noqa: BLE001
                pass

    return out


# ─────────────── regenerate the most-recent assistant reply ───────────────
# Drives the "↻ Regenerate" button under every assistant bubble. Truncates
# the conversation back to just before the most recent user message, then
# re-runs the loop with that same user message — no duplicate user turn,
# no drift across the rest of the history.

@app.post("/api/conversations/{conversation_id}/regenerate")
async def regenerate_assistant_reply(
    conversation_id: str,
    role: str = Depends(_auth.current_role),
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    normalize_role(role)
    import json as _json
    from .agent import conversation_io as _ci

    with conn_ctx(DB_PATH) as conn:
        try:
            row = conn.execute(
                "SELECT id, user_role, user_id, messages_json "
                "FROM agent_conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
    if not row:
        raise HTTPException(404, "conversation not found")
    if row["user_role"] != role and role not in ("platform_admin", "admin"):
        raise HTTPException(403, "not yours")

    try:
        msgs = _json.loads(row["messages_json"] or "[]")
        if not isinstance(msgs, list):
            msgs = []
    except (ValueError, TypeError):
        msgs = []

    # Walk backwards: find the index of the most recent user message.
    # Truncate to just BEFORE it (the loop will re-add it via the
    # `message` argument). System messages (if any) stay.
    last_user_idx = None
    for i in range(len(msgs) - 1, -1, -1):
        m = msgs[i]
        if isinstance(m, dict) and m.get("role") == "user":
            last_user_idx = i
            break
    if last_user_idx is None:
        raise HTTPException(400, "no user message to regenerate from")
    user_text = msgs[last_user_idx].get("content") or ""
    if not isinstance(user_text, str) or not user_text.strip():
        raise HTTPException(400, "previous user message is empty")

    truncated = msgs[:last_user_idx]
    # Re-save the truncated history before invoking the loop. The loop
    # loads history fresh from the DB, so this guarantees the old
    # assistant reply (and any tool messages after it) are gone before
    # the new turn runs.
    _ci.save_messages(
        conversation_id, role,
        (user or {}).get("id"),
        truncated,
    )

    # Run a fresh turn using the same user text. ask_async handles the
    # cache check, message append, loop, persist.
    from . import ask as _ask
    user_lang = (user or {}).get("language") or "en"
    result = await _ask.ask_async(
        user_text,
        role=role,
        conversation_id=conversation_id,
        user_language=user_lang,
        identified_name=(user or {}).get("name"),
        user_id=(user or {}).get("id"),
        dev_mode=bool((user or {}).get("dev_mode")),
    )
    return result


# ─────────────── today digest (Chat empty-state cards) ───────────────
# A single aggregate the chat's empty thread shows instead of the four
# static suggestion chips. Pulls today's events, overdue tasks,
# pending contacts, and recent emails so the user lands on something
# real and personal instead of generic "Try one of these:" prompts.

@app.get("/api/today")
def today_digest(
    role: str = Depends(_auth.current_role),
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Snapshot used by the Chat empty-state to invite real next-steps.

    Best-effort: every section catches its own failures so a missing
    table or schema variant can never blank the whole digest.
    """
    normalize_role(role)
    from datetime import datetime as _dt, timedelta as _td
    user_id = (user or {}).get("id")
    today = _dt.now().date()
    today_iso = today.isoformat()
    tomorrow_iso = (today + _td(days=1)).isoformat()

    out: Dict[str, Any] = {
        "today_date":          today_iso,
        "events_today":        [],
        "tasks_overdue_count": 0,
        "tasks_overdue_sample": [],
        "contacts_pending_count": 0,
        "birthdays_this_week": [],
        "saved_query_count":   0,
    }

    with conn_ctx(DB_PATH) as conn:
        # Events today
        try:
            rows = conn.execute(
                "SELECT id, title, starts_at, ends_at, all_day, location "
                "FROM events "
                "WHERE starts_at >= ? AND starts_at < ? "
                "ORDER BY starts_at ASC LIMIT 8",
                (today_iso, tomorrow_iso),
            ).fetchall()
            out["events_today"] = [dict(r) for r in rows]
        except sqlite3.OperationalError:
            pass

        # Tasks overdue (due_date < today AND not done)
        try:
            cnt = conn.execute(
                "SELECT COUNT(*) AS n FROM tasks "
                "WHERE done = 0 AND due_date IS NOT NULL "
                "AND due_date < ?",
                (today_iso,),
            ).fetchone()
            out["tasks_overdue_count"] = int(cnt["n"]) if cnt else 0
            if out["tasks_overdue_count"] > 0:
                sample = conn.execute(
                    "SELECT id, title, due_date FROM tasks "
                    "WHERE done = 0 AND due_date IS NOT NULL "
                    "AND due_date < ? "
                    "ORDER BY due_date ASC LIMIT 3",
                    (today_iso,),
                ).fetchall()
                out["tasks_overdue_sample"] = [dict(r) for r in sample]
        except sqlite3.OperationalError:
            pass

        # Pending contacts (mostly seeded by email_in / wa_sync; vCard
        # imports land here too when the user picks "Import to Pending")
        try:
            cnt = conn.execute(
                "SELECT COUNT(*) AS n FROM contacts WHERE status = 'pending'",
            ).fetchone()
            out["contacts_pending_count"] = int(cnt["n"]) if cnt else 0
        except sqlite3.OperationalError:
            pass

        # Birthdays in the next 7 days. The birthday column is YYYY-MM-DD;
        # match on month+day so it works year-over-year.
        try:
            upcoming = []
            for offset in range(0, 7):
                d = today + _td(days=offset)
                mmdd = d.strftime("%m-%d")
                rows = conn.execute(
                    "SELECT id, display_name, birthday FROM contacts "
                    "WHERE status = 'active' AND birthday IS NOT NULL "
                    "AND substr(birthday, 6, 5) = ? "
                    "LIMIT 4",
                    (mmdd,),
                ).fetchall()
                for r in rows:
                    upcoming.append({
                        "id":           r["id"],
                        "display_name": r["display_name"],
                        "birthday":     r["birthday"],
                        "days_away":    offset,
                    })
            out["birthdays_this_week"] = upcoming
        except sqlite3.OperationalError:
            pass

        # Saved-queries count (an unused-feature nudge for early users)
        try:
            cnt = conn.execute(
                "SELECT COUNT(*) AS n FROM saved_queries",
            ).fetchone()
            out["saved_query_count"] = int(cnt["n"]) if cnt else 0
        except sqlite3.OperationalError:
            pass

    return out


# ── compose (AI-first document drafting) ───────────────────────────────────
# Templates live in templates/*.json. Each one declares connector ops to
# call for data, plus a Jinja2 body_html. /draft runs the data_query +
# template render; /render-pdf shells out to Gotenberg for the PDF.

@app.get("/api/compose/templates")
def list_compose_templates(role: str = Depends(_auth.current_role)) -> List[Dict[str, Any]]:
    normalize_role(role)
    from .compose import templates as tpl
    return [tpl.public_dict(t) for t in tpl.load_all()]


# ─────────────── compose form auto-fill from pasted text ───────────────
# Drives the NeedsInputCard's "Aus Text füllen" panel. The user pastes
# an arbitrary blob (an email, a contact card, a note), names the
# fields the form needs, and we ask the LLM to map values across.
# Stateless — the form's field schema travels in the request so we
# don't depend on any server-side template lookup.

class ComposeExtractFieldIn(BaseModel):
    key: str
    label: Optional[str] = None
    pattern: Optional[str] = None  # surfaced to LLM as a soft hint


class ComposeExtractIn(BaseModel):
    text: str
    fields: List[ComposeExtractFieldIn]


@app.post("/api/compose/extract-fields")
async def compose_extract_fields(
    body: ComposeExtractIn,
    role: str = Depends(_auth.current_role),
) -> Dict[str, Any]:
    """Pull values for `fields` out of `text` using the LLM.

    Returns ``{"values": {key: str}}``. Keys the model couldn't find
    confidently are simply absent. Anything the model returns that
    isn't in the requested field schema is dropped server-side, so it
    can't sneak extra keys into the form.
    """
    normalize_role(role)
    text = (body.text or "").strip()
    if not text:
        return {"values": {}}
    if len(text) > 20_000:
        # Hard cap so a paste of "war and peace" can't blow the prompt.
        text = text[:20_000]
    if not body.fields:
        return {"values": {}}

    # Build a focused prompt. Asking for strict JSON + "omit unknown"
    # is what stops the model from inventing values for fields the
    # text doesn't mention.
    allowed_keys = {f.key for f in body.fields if f.key}
    schema_lines = []
    for f in body.fields:
        if not f.key:
            continue
        hint = f.label or f.key
        if f.pattern:
            hint += f" (must match: {f.pattern})"
        schema_lines.append(f'  - "{f.key}": {hint}')
    schema_block = "\n".join(schema_lines)

    system_msg = (
        "You extract form-field values from user-pasted free text. "
        "Return ONLY a JSON object mapping field keys (exactly as "
        "given) to extracted string values. OMIT any key you cannot "
        "find with high confidence — do not invent. No markdown, no "
        "commentary, no code fences. If no fields can be extracted, "
        "return {}."
    )
    user_msg = (
        f"Field schema:\n{schema_block}\n\n"
        f"Pasted text:\n\"\"\"\n{text}\n\"\"\"\n\n"
        "JSON object only:"
    )

    # Reuse the agent loop's singleton LlmClient (same model, same
    # base_url, same retry config). The lazy-init helper is async so
    # we await it cleanly instead of fighting the event loop.
    from . import ask as _ask
    await _ask._ensure_agent_singletons()
    llm = getattr(_ask._ask_own_backend, "_llm", None)
    if llm is None:
        raise HTTPException(503, "LLM client not available")

    try:
        # llm.chat is sync (blocking HTTP). Push it off the event loop
        # so we don't starve other requests while qwen3 thinks.
        import asyncio
        resp = await asyncio.to_thread(
            llm.chat,
            [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            None,                  # tools
            max_tokens=600,
            temperature=0.0,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=f"extraction failed: {type(exc).__name__}: {exc}",
        )

    raw = (resp or {}).get("content") or ""
    extracted = _parse_extracted_json(raw)
    # Filter to known keys + coerce to strings. Drop empty values.
    values: Dict[str, str] = {}
    for k, v in extracted.items():
        if k not in allowed_keys:
            continue
        if v is None:
            continue
        s = str(v).strip()
        if s:
            values[k] = s
    return {"values": values}


@app.post("/api/compose/extract-from-upload")
async def compose_extract_from_upload(
    file: UploadFile = File(...),
    fields_json: str = Form(...),
    role: str = Depends(_auth.current_role),
) -> Dict[str, Any]:
    """Upload a file → extract its text → run the same field extraction
    as /api/compose/extract-fields. Used by the per-slot "Upload doc"
    button in the Compose args panel so the user can drop a PDF / Word /
    text file and let the LLM fill the matching slots.

    Supports PDF / DOCX / plain text via backend.documents.extract_text.
    Scanned-only PDFs need OCR'd text upstream (Paperless does this on
    its own; this endpoint takes whatever extract_text returns)."""
    normalize_role(role)

    # LLM-offline early refusal — see contacts_parse_blob's note for
    # the rationale; this endpoint shares the same downstream LLM
    # hop and the same 30s connect-timeout failure mode.
    if not _llm_reachable():
        probe = _llm_probe()
        raise HTTPException(
            503,
            f"language model unreachable ({probe['reason']}) — "
            "start the local LLM and retry",
        )

    import json as _json
    try:
        raw_fields = _json.loads(fields_json or "[]")
    except _json.JSONDecodeError:
        raise HTTPException(400, "fields_json must be valid JSON array")
    if not isinstance(raw_fields, list):
        raise HTTPException(400, "fields_json must be a JSON array")
    fields = [
        ComposeExtractFieldIn(**f) for f in raw_fields
        if isinstance(f, dict) and f.get("key")
    ]
    if not fields:
        return {"values": {}, "text_chars": 0}

    # Write the upload to a temp file so extract_text (path-based) can
    # work. Bounded 50MB to keep memory predictable — PDFs above that
    # are almost certainly a wrong-file mistake.
    raw = await file.read()
    if len(raw) > 50 * 1024 * 1024:
        raise HTTPException(413, "file too large (50 MB max)")
    suffix = Path(file.filename or "upload").suffix or ".bin"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(raw)
        tmp_path = Path(tmp.name)
    try:
        from . import documents as documents_mod
        try:
            text = documents_mod.extract_text(tmp_path, file.content_type) or ""
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("yorik.compose.upload").warning(
                "extract_text failed for %s (%s): %s",
                file.filename, file.content_type, exc,
            )
            raise HTTPException(
                400,
                f"Could not read text from {file.filename!r}: {type(exc).__name__}. "
                "Try copying the relevant section and pasting it instead.",
            )
        if not text.strip():
            return {"values": {}, "text_chars": 0, "note": "no extractable text"}

        # Reuse the same extraction logic by calling the JSON endpoint's
        # core inline. We can't just await compose_extract_fields() (it
        # takes a Pydantic body); replicate the few lines that build the
        # prompt + parse the response.
        body = ComposeExtractIn(text=text, fields=fields)
        return await compose_extract_fields(body, role=role)  # type: ignore[arg-type]
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


def _parse_extracted_json(raw: str) -> Dict[str, Any]:
    """Best-effort JSON extraction from an LLM reply.

    Handles the common failure modes: stray markdown fence, leading
    commentary, trailing prose. Returns {} on irrecoverable input
    rather than raising — the UI just shows "nothing extracted".
    """
    import json as _json, re as _re
    s = (raw or "").strip()
    if not s:
        return {}
    # Strip ``` / ```json fences if present.
    if s.startswith("```"):
        s = _re.sub(r"^```(?:json)?\s*", "", s)
        s = _re.sub(r"\s*```$", "", s)
    try:
        obj = _json.loads(s)
        return obj if isinstance(obj, dict) else {}
    except ValueError:
        pass
    # Fallback: take the first {...} block.
    m = _re.search(r"\{.*\}", s, _re.DOTALL)
    if m:
        try:
            obj = _json.loads(m.group(0))
            return obj if isinstance(obj, dict) else {}
        except ValueError:
            return {}
    return {}


class ComposeDraftIn(BaseModel):
    template_id: str
    args: Dict[str, Any] = Field(default_factory=dict)
    # Editor preview hint — when true, empty arg slots fall back to the
    # template's preview_args so users see what a filled letter looks
    # like (Muster* names + addresses) before they've typed anything.
    # The returned `args` is still the original (empty) — only the
    # rendered HTML gets the overlay, so input fields stay empty for
    # the user to fill. The persisted compose_draft path NEVER uses
    # this — it always renders with real data.
    preview: bool = True


class ComposePolishIn(BaseModel):
    """Input for the "Yorik formuliert für mich" sparkle button on
    NeedsInputCard. The user types freetalk in the inline panel; this
    endpoint runs one short LLM call to polish it into a template-
    respecting body_text (and optionally a Betreff)."""
    intent: str
    template_id: str
    field_key: str = "body_text"
    contact_id: Optional[int] = None
    suggest_betreff: bool = True
    language: Optional[str] = None  # falls back to user_profile language


@app.post("/api/compose/polish")
async def compose_polish(
    body: ComposePolishIn,
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Convert user freetalk into a polished body_text following the
    template's per-field llm_hint. Returns {body_text, betreff?}.

    Auth-only; no role gating beyond logged-in. The polish has no DB
    side effects — it's a pure transform the form-submit handler then
    applies. 400 for missing template / non-intent field / empty intent.
    """
    from backend.compose import polish as _polish_mod

    # Language preference: explicit > profile row > auth session > English.
    # Hard-coding "German" as the silent fallback was wrong — users on an
    # English-configured instance got their English intent rewritten in
    # German whenever user_profiles.language was NULL / missing / unmappable.
    iso_to_name = {
        "de": "German", "en": "English", "it": "Italian",
        "fr": "French", "es": "Spanish", "pl": "Polish",
    }
    lang = (body.language or "").strip()
    if not lang:
        try:
            with conn_ctx(DB_PATH) as conn:
                row = conn.execute(
                    "SELECT language FROM user_profiles WHERE id = ? LIMIT 1",
                    (user.get("id") or 0,),
                ).fetchone()
            if row and row["language"]:
                lang_code = (row["language"] or "").lower().strip()
                lang = iso_to_name.get(lang_code, "")
        except Exception:
            pass
    if not lang:
        # Final fallback: the auth session's language (same source the rest
        # of the codebase uses — main.py:6025, 6105, 9845). English wins
        # the ultimate tie because it is the codebase lingua franca; the
        # old "German" default was the actual bug.
        session_code = (user.get("language") or "en").lower().strip()
        lang = iso_to_name.get(session_code, "English")

    try:
        result = _polish_mod.polish(
            intent=body.intent,
            template_id=body.template_id,
            field_key=body.field_key,
            contact_id=body.contact_id,
            suggest_betreff=bool(body.suggest_betreff),
            language=lang,
        )
    except _polish_mod.PolishError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception("compose_polish failed: %s", exc)
        raise HTTPException(500, "polish failed — try again or rephrase")
    return result


@app.post("/api/compose/draft")
async def compose_draft(
    body: ComposeDraftIn,
    role: str = Depends(_auth.current_role),
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Run a template's data_query, render the body_html with Jinja, return
    {html, data, numbering, args}. The frontend loads the html into the
    TipTap editor and shows `data` + `numbering` in the AI panel for
    transparency. `args` echoes back what we used to render — important
    when auto-numbering replaced placeholders so the UI stays in sync.
    """
    normalize_role(role)
    from .compose import templates as tpl
    from .compose import render as rdr
    try:
        template = tpl.get(body.template_id)
    except tpl.TemplateError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    rendered = await rdr.render_template(
        template, body.args,
        owner_user_id=user.get("id"),
        use_preview_args=body.preview,
    )
    return {
        "template": tpl.public_dict(template),
        "html": rendered["html"],
        "data": rendered["data"],
        "numbering": rendered.get("numbering", {}),
        "args": rendered.get("args", body.args),
    }


# ─── Saved Compose drafts (chat → compose handoff) ─────────────────
# Letters/invoices/offers the LLM prepares via the compose_draft skill
# land here. The chat shows a card; the user clicks "Bearbeiten →" and
# Compose loads the draft pre-filled.

class ComposeSavedDraftIn(BaseModel):
    kind:        str = "letter"   # letter | invoice | offer | email | …
    template_id: Optional[str] = None
    recipient:   Optional[str] = None
    subject:     Optional[str] = None
    body_html:   str = ""
    args:        Dict[str, Any] = Field(default_factory=dict)


@app.post("/api/compose/saved-draft", status_code=201)
def compose_saved_draft_create(
    body: ComposeSavedDraftIn,
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Create a persisted Compose draft. Called by the compose_draft
    skill when the LLM prepares a letter/invoice for the user via chat."""
    import json as _json
    with conn_ctx(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO compose_drafts "
            "(user_id, kind, template_id, recipient, subject, body_html, args_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user["id"], body.kind, body.template_id, body.recipient,
             body.subject, body.body_html or "", _json.dumps(body.args, default=str)),
        )
        draft_id = cur.lastrowid
        # Seed v1 of the version history with the LLM's first draft so
        # the chat card's version chips always have at least one entry
        # and users can step back to "as the LLM first wrote it".
        conn.execute(
            "INSERT INTO compose_draft_versions (draft_id, body_html, source) "
            "VALUES (?, ?, 'initial')",
            (draft_id, body.body_html or ""),
        )
    return {"id": draft_id}


@app.get("/api/compose/saved-drafts")
def compose_saved_drafts_list(
    limit: int = Query(50, ge=1, le=200),
    user: dict[str, Any] = Depends(_auth.current_user),
) -> List[Dict[str, Any]]:
    """List the calling user's compose drafts, newest first. Slim shape —
    no body_html (the sidebar only needs recipient/subject/kind/time).
    The full record loads via GET /api/compose/saved-draft/{id} when the
    user clicks one."""
    role = user.get("role") or "viewer"
    with conn_ctx(DB_PATH) as conn:
        if role in ("platform_admin", "admin"):
            rows = conn.execute(
                "SELECT id, user_id, kind, template_id, recipient, subject, "
                "       created_at, updated_at "
                "FROM compose_drafts ORDER BY updated_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, user_id, kind, template_id, recipient, subject, "
                "       created_at, updated_at "
                "FROM compose_drafts WHERE user_id = ? "
                "ORDER BY updated_at DESC LIMIT ?",
                (user["id"], int(limit)),
            ).fetchall()
    return [dict(r) for r in rows]


@app.delete("/api/compose/saved-draft/{draft_id}")
def compose_saved_draft_delete(
    draft_id: int,
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Delete a draft. Only the owner (or admin) can delete. Idempotent —
    a missing draft returns ok=true (the user's intent is satisfied)."""
    with conn_ctx(DB_PATH) as conn:
        owner = conn.execute(
            "SELECT user_id FROM compose_drafts WHERE id=?", (draft_id,),
        ).fetchone()
        if not owner:
            return {"ok": True, "deleted": False}
        if owner["user_id"] != user["id"] and user.get("role") not in ("admin", "platform_admin"):
            raise HTTPException(status_code=403, detail="not your draft")
        conn.execute("DELETE FROM compose_drafts WHERE id=?", (draft_id,))
        conn.commit()
    return {"ok": True, "deleted": True, "id": draft_id}


@app.get("/api/compose/saved-draft/{draft_id}")
def compose_saved_draft_get(
    draft_id: int,
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    import json as _json
    with conn_ctx(DB_PATH) as conn:
        row = conn.execute(
            "SELECT id, user_id, kind, template_id, recipient, subject, "
            "       body_html, args_json, created_at, updated_at "
            "FROM compose_drafts WHERE id = ?",
            (draft_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="draft not found")
    # Only the owner OR an admin can load.
    if row["user_id"] != user["id"] and user.get("role") not in ("admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="not your draft")
    return {
        "id":          row["id"],
        "kind":        row["kind"],
        "template_id": row["template_id"],
        "recipient":   row["recipient"],
        "subject":     row["subject"],
        "body_html":   row["body_html"],
        "args":        _json.loads(row["args_json"] or "{}"),
        "created_at":  row["created_at"],
        "updated_at":  row["updated_at"],
    }


@app.patch("/api/compose/saved-draft/{draft_id}")
def compose_saved_draft_patch(
    draft_id: int,
    body: ComposeSavedDraftIn,
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Update an existing draft (e.g. user edited in Compose then auto-saved)."""
    import json as _json
    with conn_ctx(DB_PATH) as conn:
        owner = conn.execute("SELECT user_id FROM compose_drafts WHERE id=?", (draft_id,)).fetchone()
        if not owner:
            raise HTTPException(status_code=404, detail="draft not found")
        if owner["user_id"] != user["id"] and user.get("role") not in ("admin", "platform_admin"):
            raise HTTPException(status_code=403, detail="not your draft")
        conn.execute(
            "UPDATE compose_drafts SET "
            "kind=?, template_id=?, recipient=?, subject=?, body_html=?, args_json=?, "
            "updated_at=datetime('now') WHERE id=?",
            (body.kind, body.template_id, body.recipient, body.subject,
             body.body_html or "", _json.dumps(body.args, default=str), draft_id),
        )
    return {"id": draft_id, "ok": True}


class ComposeRefineIn(BaseModel):
    instruction: str  # e.g. "make it shorter", "more formal", "remove the Friday part"


@app.get("/api/compose/saved-draft/{draft_id}/versions")
def compose_saved_draft_versions(
    draft_id: int,
    user: dict[str, Any] = Depends(_auth.current_user),
) -> List[Dict[str, Any]]:
    """List the history chips for the inline-compose card. Returns
    oldest-first so the UI renders left-to-right as the user expects
    (v1 → v2 → v3 …)."""
    with conn_ctx(DB_PATH) as conn:
        owner = conn.execute("SELECT user_id FROM compose_drafts WHERE id=?", (draft_id,)).fetchone()
        if not owner:
            raise HTTPException(404, "draft not found")
        if owner["user_id"] != user["id"] and user.get("role") not in ("admin", "platform_admin"):
            raise HTTPException(403, "not your draft")
        rows = conn.execute(
            "SELECT id, source, instruction, restored_from, created_at "
            "FROM compose_draft_versions WHERE draft_id=? ORDER BY id ASC",
            (draft_id,),
        ).fetchall()
    return [dict(r) for r in rows]


class ComposeRestoreIn(BaseModel):
    version_id: int


@app.post("/api/compose/saved-draft/{draft_id}/restore")
def compose_saved_draft_restore(
    draft_id: int,
    body: ComposeRestoreIn,
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Swap the draft's live body to a previous version's content.
    Subsequent refine calls work from THAT content. The restore itself
    is recorded as a new version row (source='restore', restored_from=
    <picked id>) so history stays linear + auditable — the user can
    always step forward again."""
    with conn_ctx(DB_PATH) as conn:
        owner = conn.execute("SELECT user_id FROM compose_drafts WHERE id=?", (draft_id,)).fetchone()
        if not owner:
            raise HTTPException(404, "draft not found")
        if owner["user_id"] != user["id"] and user.get("role") not in ("admin", "platform_admin"):
            raise HTTPException(403, "not your draft")
        version = conn.execute(
            "SELECT body_html FROM compose_draft_versions WHERE id=? AND draft_id=?",
            (body.version_id, draft_id),
        ).fetchone()
        if not version:
            raise HTTPException(404, "version not found for this draft")
        # Append a restore marker so the UI can render it as a new chip;
        # the user can navigate forward+back across these too.
        cur = conn.execute(
            "INSERT INTO compose_draft_versions "
            "(draft_id, body_html, source, restored_from) VALUES (?, ?, 'restore', ?)",
            (draft_id, version["body_html"], body.version_id),
        )
        new_version_id = cur.lastrowid
        conn.execute(
            "UPDATE compose_drafts SET body_html=?, updated_at=datetime('now') WHERE id=?",
            (version["body_html"], draft_id),
        )
        conn.commit()
    return {"ok": True, "new_version_id": new_version_id, "body_html": version["body_html"]}


@app.post("/api/compose/saved-draft/{draft_id}/refine")
async def compose_saved_draft_refine(
    draft_id: int,
    body: ComposeRefineIn,
    user: dict[str, Any] = Depends(_auth.current_user),
):
    """STREAMING LLM rewrite — tokens land in the chat editor as they
    generate. Reuses the SSE pattern the agent loop uses for
    /api/ask-stream:
      data: {"phase": "text_delta", "text": "Hi Han"}
      data: {"phase": "text_delta", "text": "s,"}
      …
      data: {"phase": "final", "body_html": "<p>…</p>", "version_id": 42}

    Each refine creates a new version row (source='refine',
    instruction=<user input>) so users can step back if the LLM
    wandered. The user's instruction is the chat message they typed
    ("make it shorter", "remove the Friday part", "use Sie form")."""
    if not body.instruction or not body.instruction.strip():
        raise HTTPException(400, "empty instruction")
    with conn_ctx(DB_PATH) as conn:
        row = conn.execute(
            "SELECT user_id, kind, recipient, subject, body_html "
            "FROM compose_drafts WHERE id=?",
            (draft_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "draft not found")
    if row["user_id"] != user["id"] and user.get("role") not in ("admin", "platform_admin"):
        raise HTTPException(403, "not your draft")

    instruction = body.instruction.strip()
    snapshot = {
        "kind":      row["kind"],
        "recipient": row["recipient"],
        "subject":   row["subject"],
        "body_html": row["body_html"] or "<p></p>",
    }
    messages = [
        {"role": "system", "content": (
            "You rewrite document drafts in place. Preserve the HTML "
            "structure (paragraphs, lists, bold/italic). Keep the same "
            "language as the original. Don't add greetings/closings the "
            "user didn't ask for. Return ONLY the new body HTML — no "
            "commentary, no markdown fences."
        )},
        {"role": "user", "content": (
            f"Document kind: {snapshot['kind']}\n"
            f"Recipient: {snapshot['recipient'] or '(unknown)'}\n"
            f"Subject: {snapshot['subject'] or '(none)'}\n\n"
            f"Current body (HTML):\n{snapshot['body_html']}\n\n"
            f"Rewrite instruction from the user:\n{instruction}\n\n"
            f"Return ONLY the rewritten body HTML."
        )},
    ]

    async def event_stream():
        # Pre-flight LLM reachable check — without this, an offline llama-swap
        # makes the SSE channel close mid-handshake and the React chat shows
        # a "Yorik thinks…" spinner that never resolves. The non-streaming
        # /api/ask path already does this; mirror it here. Surface a friendly
        # final event so the UI renders a normal error bubble instead of
        # hanging forever.
        if not _llm_reachable():
            offline = _llm_offline_response("(refine streaming)")
            yield "data: " + json.dumps(
                {"phase": "final", "error": offline["response"],
                 "degraded": True, "llm_status": offline["llm_status"]},
                ensure_ascii=False,
            ) + "\n\n"
            return
        from .agent.llm import LlmClient
        from . import ask as _ask
        client = LlmClient(model=_ask.LLM_MODEL, base_url=_ask.LLM_BASE_URL)
        accumulated: list[str] = []
        try:
            # client.chat_stream returns a SYNC generator; drive it via
            # asyncio.to_thread(next, ...) so we don't block the event loop.
            stream_iter = iter(client.chat_stream(
                messages, temperature=0.2, max_tokens=2500,
            ))
            def _next():
                try:
                    return next(stream_iter)
                except StopIteration:
                    return None
            while True:
                chunk = await asyncio.to_thread(_next)
                if chunk is None:
                    break
                try:
                    delta = chunk.choices[0].delta.content if chunk.choices else None
                except Exception:  # noqa: BLE001
                    delta = None
                if delta:
                    accumulated.append(delta)
                    yield "data: " + json.dumps(
                        {"phase": "text_delta", "text": delta},
                        ensure_ascii=False,
                    ) + "\n\n"
        except Exception as exc:  # noqa: BLE001
            yield "data: " + json.dumps(
                {"phase": "error", "error": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            ) + "\n\n"
            return

        final_html = "".join(accumulated).strip()
        # Strip stray markdown fences the LLM sometimes adds despite the
        # prompt rule.
        if final_html.startswith("```"):
            final_html = re.sub(r"^```[a-z]*\n?", "", final_html, flags=re.IGNORECASE)
            final_html = re.sub(r"\n?```\s*$", "", final_html)
        if not final_html:
            yield "data: " + json.dumps(
                {"phase": "error", "error": "LLM returned empty content"},
            ) + "\n\n"
            return
        version_id: Optional[int] = None
        with conn_ctx(DB_PATH) as conn:
            cur = conn.execute(
                "INSERT INTO compose_draft_versions "
                "(draft_id, body_html, source, instruction) "
                "VALUES (?, ?, 'refine', ?)",
                (draft_id, final_html, instruction),
            )
            version_id = cur.lastrowid
            conn.execute(
                "UPDATE compose_drafts SET body_html=?, updated_at=datetime('now') WHERE id=?",
                (final_html, draft_id),
            )
            conn.commit()
        yield "data: " + json.dumps(
            {"phase": "final", "body_html": final_html, "version_id": version_id},
            ensure_ascii=False,
        ) + "\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control":     "no-cache",
            "Connection":        "keep-alive",
        },
    )


class ComposeSendIn(BaseModel):
    method:       str    # "email" | "whatsapp" | "pdf"
    recipient_id: Optional[int] = None  # contact id (required for email/whatsapp)
    subject:      Optional[str] = None  # override (email only)
    # Optional override for which channel value to send to when the
    # contact has multiple (e.g. two email addresses). Falls back to
    # the first matching channel.
    channel_id:   Optional[int] = None


@app.post("/api/compose/saved-draft/{draft_id}/send")
async def compose_saved_draft_send(
    draft_id: int,
    body: ComposeSendIn,
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Send the draft via the chosen channel. Returns enough metadata
    for the UI to confirm + flip into "sent" state. Methods:

      - email     → uses backend/email_sender.send_email(...) with the
                    contact's first email channel (or channel_id if set).
                    Plain-text fallback derived from the HTML.
      - whatsapp  → POSTs to the bridge with the body text (HTML stripped).
                    Limited to short bodies; the UI should warn before send.
      - pdf       → renders body via /api/compose/render-pdf and returns
                    a download URL. Doesn't physically mail anything —
                    the user prints + posts themselves.

    Idempotency: this endpoint always sends. The frontend is responsible
    for confirming + disabling the Send button while in-flight.
    """
    with conn_ctx(DB_PATH) as conn:
        row = conn.execute(
            "SELECT user_id, kind, recipient, subject, body_html "
            "FROM compose_drafts WHERE id=?",
            (draft_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "draft not found")
    if row["user_id"] != user["id"] and user.get("role") not in ("admin", "platform_admin"):
        raise HTTPException(403, "not your draft")
    if not (row["body_html"] or "").strip():
        raise HTTPException(400, "draft body is empty")
    method = body.method.lower().strip()
    if method not in ("email", "whatsapp", "pdf"):
        raise HTTPException(400, "method must be 'email' | 'whatsapp' | 'pdf'")

    body_html = row["body_html"]
    subject = body.subject or row["subject"] or "(no subject)"

    # ── Plain-text fallback derived from the HTML. Cheap regex-based;
    #    fine for short email bodies. Compose-app's email path uses the
    #    same shape.
    def _html_to_text(h: str) -> str:
        s = re.sub(r"<br\s*/?>", "\n", h or "", flags=re.I)
        s = re.sub(r"</p>\s*<p[^>]*>", "\n\n", s, flags=re.I)
        s = re.sub(r"<[^>]+>", "", s)
        s = re.sub(r"\n{3,}", "\n\n", s)
        return s.strip()
    body_text = _html_to_text(body_html)

    if method == "pdf":
        # Render → save under data/exports → return a path the UI can
        # download from. No outbound network.
        from .compose import pdf as pdf_mod
        try:
            pdf_bytes = pdf_mod.render_pdf(body_html, page_size="A4",
                                            margins_mm=(20, 18, 25, 18),
                                            filename=f"compose-{draft_id}.pdf")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"PDF render failed: {exc}")
        from pathlib import Path as _P
        import time as _time
        out_dir = _P("data/exports"); out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"compose-{draft_id}-{int(_time.time())}.pdf"
        out_path.write_bytes(pdf_bytes)
        # URL goes through an authenticated /api endpoint — the data/
        # directory itself isn't statically mounted, so the previous
        # /data/exports/... URL fell through to the SPA catch-all and
        # the React app tried to navigate to a non-existent "data" app.
        return {"ok": True, "method": "pdf",
                "pdf_url": f"/api/compose/saved-draft/{draft_id}/pdf?file={out_path.name}",
                "filename": out_path.name}

    if body.recipient_id is None:
        raise HTTPException(400, f"recipient_id required for method='{method}'")

    # Resolve the contact's channel for the chosen method.
    from . import contacts as _ct
    contact = _ct.get(body.recipient_id, include_children=True)
    if not contact:
        raise HTTPException(404, f"contact {body.recipient_id} not found")
    want_kind = "email" if method == "email" else "whatsapp"
    channels = [c for c in (contact.get("channels") or []) if c.get("kind") == want_kind]
    if not channels:
        raise HTTPException(400, f"contact has no {want_kind} channel — add one in Contacts first")
    target = channels[0]
    if body.channel_id is not None:
        match = next((c for c in channels if int(c.get("id") or -1) == body.channel_id), None)
        if match:
            target = match
    target_value = target["value"]

    if method == "email":
        from . import email_sender
        # Find an outbound email account for this user (first one).
        with conn_ctx(DB_PATH) as conn:
            acc = conn.execute(
                "SELECT id FROM email_accounts WHERE owner_user_id=? "
                "ORDER BY is_default DESC, id LIMIT 1",
                (user["id"],),
            ).fetchone()
        if not acc:
            raise HTTPException(400, "no email account configured — set one up in /email first")
        result = email_sender.send(
            account_id=int(acc["id"]),
            to=[target_value],
            subject=subject,
            body_text=body_text,
            body_html=body_html,
        )
        if not result.get("ok"):
            raise HTTPException(502, f"email send failed: {result.get('error', 'unknown')}")
        return {"ok": True, "method": "email", "to": target_value, "subject": subject,
                "message_id": result.get("message_id")}

    if method == "whatsapp":
        # Bridge expects plain text. Strip HTML.
        if len(body_text) > 2000:
            raise HTTPException(400, "WhatsApp body too long (>2000 chars) — switch to email or PDF")
        import httpx
        try:
            async with httpx.AsyncClient(timeout=15.0) as c:
                from .whatsapp import _bridge_url
                r = await c.post(_bridge_url(f"/chats/{target_value}/send", user["id"]),
                                  json={"text": body_text})
                if r.status_code != 200:
                    raise HTTPException(502, f"bridge {r.status_code}: {r.text[:200]}")
        except httpx.RequestError as exc:
            raise HTTPException(502, f"WhatsApp send failed: {exc}")
        return {"ok": True, "method": "whatsapp", "to": target_value}

    raise HTTPException(500, "unreachable")


@app.get("/api/compose/saved-draft/{draft_id}/pdf")
def compose_saved_draft_pdf(
    draft_id: int,
    file: str = Query(..., description="PDF basename from the send response"),
    user: dict[str, Any] = Depends(_auth.current_user),
) -> FileResponse:
    """Stream a previously-rendered PDF (created by .../send with
    method='pdf') back to the browser. Authenticated + ownership-
    gated; basename is validated to prevent path traversal."""
    # Auth: only the draft's owner (or admin) can pull the PDF
    with conn_ctx(DB_PATH) as conn:
        owner = conn.execute("SELECT user_id FROM compose_drafts WHERE id=?", (draft_id,)).fetchone()
        if not owner:
            raise HTTPException(404, "draft not found")
        if owner["user_id"] != user["id"] and user.get("role") not in ("admin", "platform_admin"):
            raise HTTPException(403, "not your draft")
    # Sanitize: must be a bare basename starting with compose-{id}- so
    # there's no way to coax this into serving anything else from disk.
    safe = (file or "").strip()
    if "/" in safe or ".." in safe or not safe.startswith(f"compose-{draft_id}-") or not safe.endswith(".pdf"):
        raise HTTPException(400, "invalid file parameter")
    from pathlib import Path as _P
    path = _P("data/exports") / safe
    if not path.is_file():
        raise HTTPException(404, "pdf not found — may have been cleaned up; re-render via Send")
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=safe,
        headers={"Content-Disposition": f'inline; filename="{safe}"'},
    )


class ComposeRenderPdfIn(BaseModel):
    body_html: str
    page_size: str = "A4"
    margins_mm: Optional[List[int]] = None
    filename: str = "document.pdf"
    # If set, the renderer attempts to embed a ZUGFeRD XML payload into the
    # PDF (PDF/A-3 hybrid). The template + rendered data MUST provide an
    # invoice_fields mapping (see backend/compose/zugferd.py).
    template_id: Optional[str] = None
    args: Optional[Dict[str, Any]] = None


@app.post("/api/compose/render-pdf")
async def compose_render_pdf(body: ComposeRenderPdfIn, role: str = Depends(_auth.current_role)) -> Response:
    """HTML → PDF via the local Gotenberg container. Returns the PDF bytes
    inline so the frontend can either download or POST to Paperless."""
    normalize_role(role)
    from .compose import pdf as pdf_mod
    margins = tuple(body.margins_mm or [20, 18, 25, 18])
    pdf_bytes = pdf_mod.render_pdf(body.body_html, page_size=body.page_size, margins_mm=margins,
                                   filename=body.filename)
    if not pdf_bytes:
        raise HTTPException(status_code=502, detail="PDF render failed (Gotenberg unreachable?)")

    # Give every loaded extension a chance to post-process the PDF (e.g.
    # ZUGFeRD embeds CrossIndustryInvoice XML for German B2B compliance).
    # Each registered hook receives the pdf_bytes + template + args and
    # returns the new pdf_bytes (or None to pass through). When no extension
    # is loaded for an output format the user doesn't need, this is a no-op.
    if body.template_id:
        try:
            from . import extensions as ext_mod
            from .compose import templates as tpl_mod
            from .compose import render as rdr_mod
            template = tpl_mod.get(body.template_id)
            rendered = await rdr_mod.render_template(template, body.args or {})
            transformed = ext_mod.invoke_hooks(
                "compose.pdf_post_process",
                pdf_bytes,
                template=template,
                args=body.args or {},
                rendered_data=rendered["data"],
            )
            if transformed:
                pdf_bytes = transformed
        except Exception as exc:  # noqa: BLE001 — never block a download
            logging.getLogger("homeos.compose").warning("PDF post-process skipped: %s", exc)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f"inline; filename=\"{body.filename}\""},
    )


# ── extensions (regional / optional add-ons) ───────────────────────────────

@app.get("/api/extensions")
def list_extensions(role: str = Depends(_auth.current_role)) -> List[Dict[str, Any]]:
    """List every extension on disk with installed-status + dep info.
    Shown in Settings → Extensions so the admin can one-click install."""
    normalize_role(role)
    from . import extensions as ext_mod
    return [ext_mod.public_dict(e) for e in ext_mod.load_all()]


@app.post("/api/extensions/{extension_id}/install")
def install_extension(extension_id: str, role: str = Depends(_auth.current_role)) -> Dict[str, Any]:
    """pip install -r extensions/<id>/requirements.txt inside Yorik's venv.
    Admin-gated. After install, the extension's modules are imported and
    its hooks register. Restart not required for most extensions."""
    if normalize_role(role) not in ("admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="role required: admin")
    from . import extensions as ext_mod
    res = ext_mod.install_dependencies(extension_id)
    if not res.get("ok"):
        # Bubble the pip stderr up so the UI can show the user what went wrong.
        return res
    # Re-scan so the just-installed extension's module gets imported now.
    ext_mod.load_all()
    return res


# ─── LLM config — view + change endpoint/model from the UI ────────────────

# Conventional endpoints we sweep when the user clicks "Auto-detect".
# Order matters only insofar as the response renders in this order.
_LLM_DETECT_CANDIDATES: Final[List[Dict[str, str]]] = [
    {"label": "Yorik llama-server (chat)",  "base_url": "http://127.0.0.1:8082/v1"},
    {"label": "Yorik llama-server (embed)", "base_url": "http://127.0.0.1:8083/v1"},
    {"label": "llama-swap",                 "base_url": "http://127.0.0.1:8080/v1"},
    {"label": "llama.cpp (custom)",         "base_url": "http://127.0.0.1:8081/v1"},
    {"label": "LM Studio",                  "base_url": "http://127.0.0.1:1234/v1"},
    {"label": "Ollama (legacy)",            "base_url": "http://127.0.0.1:11434/v1"},
    {"label": "vLLM",                       "base_url": "http://127.0.0.1:8001/v1"},
]


class LlmConfigIn(BaseModel):
    base_url: str = Field(min_length=4)
    model:    str = Field(min_length=1)
    # Optional cloud-provider API key. Empty string clears the stored key
    # (revert to local-style "not-used"). Never echoed back via GET — only
    # has_api_key flag.
    api_key:  Optional[str] = None


class LlmProbeIn(BaseModel):
    base_url: str = Field(min_length=4)
    api_key:  Optional[str] = None


def _probe_url_for_models(base_url: str, timeout: float = 1.0,
                          api_key: Optional[str] = None) -> Dict[str, Any]:
    """Hit `<base_url>/models` once, return what's served. Never raises —
    failures come back as {ok: False, reason: ...}.

    `api_key` is sent as a Bearer header when provided. Required for
    cloud endpoints (OpenAI, Anthropic-via-litellm, etc.); ignored by
    local servers (Ollama, llama-swap) that don't auth."""
    headers = {}
    if api_key and api_key.strip():
        headers["Authorization"] = f"Bearer {api_key.strip()}"
    try:
        r = requests.get(f"{base_url.rstrip('/')}/models", timeout=timeout, headers=headers)
        if not r.ok:
            return {"ok": False, "reason": f"HTTP {r.status_code}", "models": []}
        body = r.json()
        ids = sorted({m.get("id") for m in (body.get("data") or []) if m.get("id")})
        return {"ok": True, "models": ids}
    except requests.exceptions.ConnectionError:
        return {"ok": False, "reason": "connection refused", "models": []}
    except requests.exceptions.ConnectTimeout:
        return {"ok": False, "reason": "connect timeout", "models": []}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}", "models": []}


# ─── Pending action endpoints (beta confirmation modal) ────────────
# These are called by the ConfirmationModal frontend component:
#   POST /api/pending/{id}/confirm  → run the action, record success
#   POST /api/pending/{id}/cancel   → drop it, record cancellation
#   POST /api/pending/{id}/test     → run it but mark as test (excluded
#                                      from per-model success rate)
# All three drop the pending row from the DB. Telemetry goes to
# skill_decisions so the Quality dashboard can rank LLMs by reliability.

from . import pending_actions as _pa


def _user_owns_pending(user: dict[str, Any], row: dict[str, Any]) -> bool:
    """A user can only resolve their own pending actions. Admins can
    resolve anyone's (matches the rest of Yorik's permission model)."""
    return row["user_id"] == user["id"] or user.get("role") == "admin"


@app.post("/api/pending/{pending_id}/confirm")
async def pending_confirm(
    pending_id: str,
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Confirm path: skill already applied its action when staged. We just
    drop the pending row and log telemetry. No further DB write."""
    row = _pa.get(pending_id)
    if not row:
        raise HTTPException(status_code=404, detail="pending action not found or expired")
    if not _user_owns_pending(user, row):
        raise HTTPException(status_code=403, detail="not your pending action")
    _pa.drop(pending_id)
    _pa.record_decision(
        skill=row["skill"], decision="confirmed",
        user_id=row["user_id"], llm_model=row["llm_model"],
        language=row["language"], params=row["params"],
    )
    return {"ok": True, "confirmed": True, "ui_actions": []}


@app.post("/api/pending/{pending_id}/cancel")
async def pending_cancel(
    pending_id: str,
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Cancel path: run the rollback recorded by the skill (delete the
    just-inserted event, restore old field values, etc.). Drop the
    pending row and log telemetry."""
    row = _pa.get(pending_id)
    if not row:
        raise HTTPException(status_code=404, detail="pending action not found or expired")
    if not _user_owns_pending(user, row):
        raise HTTPException(status_code=403, detail="not your pending action")
    from .ui_tools import reset_ui_actions, get_ui_actions
    reset_ui_actions()
    try:
        rollback_result = _pa.rollback(pending_id)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("homeos.pending").warning(
            "rollback failed for %s: %s", pending_id, exc,
        )
        rollback_result = {"error": str(exc)}
    _pa.drop(pending_id)
    _pa.record_decision(
        skill=row["skill"], decision="cancelled",
        user_id=row["user_id"], llm_model=row["llm_model"],
        language=row["language"], params=row["params"],
    )
    return {
        "ok":         True,
        "cancelled":  True,
        "rollback":   rollback_result,
        "ui_actions": get_ui_actions(),
    }


@app.post("/api/pending/{pending_id}/test")
async def pending_test(
    pending_id: str,
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Just-testing path: roll back the action (same as cancel) so the
    DB ends up clean, but log telemetry as 'test' so the per-model
    success rate isn't polluted by experimental queries."""
    row = _pa.get(pending_id)
    if not row:
        raise HTTPException(status_code=404, detail="pending action not found or expired")
    if not _user_owns_pending(user, row):
        raise HTTPException(status_code=403, detail="not your pending action")
    from .ui_tools import reset_ui_actions, get_ui_actions
    reset_ui_actions()
    try:
        rollback_result = _pa.rollback(pending_id)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("homeos.pending").warning(
            "rollback failed for %s: %s", pending_id, exc,
        )
        rollback_result = {"error": str(exc)}
    _pa.drop(pending_id)
    _pa.record_decision(
        skill=row["skill"], decision="test",
        user_id=row["user_id"], llm_model=row["llm_model"],
        language=row["language"], params=row["params"],
    )
    return {
        "ok":         True,
        "test":       True,
        "rollback":   rollback_result,
        "ui_actions": get_ui_actions(),
    }


@app.get("/api/pending")
def pending_list(
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Return the calling user's open pending actions. Used by the
    frontend to re-show the modal after a page reload."""
    with conn_ctx(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, skill, preview_json, llm_model, language, created_at "
            "FROM pending_actions WHERE user_id=? "
            "ORDER BY created_at DESC LIMIT 10",
            (user["id"],),
        ).fetchall()
    import json as _json
    return {"pending": [{
        "id":         r["id"],
        "skill":      r["skill"],
        "preview":    _json.loads(r["preview_json"]),
        "llm_model":  r["llm_model"],
        "language":   r["language"],
        "created_at": r["created_at"],
    } for r in rows]}


@app.get("/api/skills/quality")
def skills_quality(
    _user: dict[str, Any] = Depends(_auth.require_admin),
) -> Dict[str, Any]:
    """Per-(skill, llm_model) success rate matrix for Settings → Quality.
    Powered by the skill_decisions telemetry. Admin only — exposes
    aggregate user behavior."""
    return {"matrix": _pa.quality_matrix()}


# ─── Voice (STT) config ────────────────────────────────────────────
# GET returns the current STT backend + Whisper model + catalogues;
# PATCH switches at runtime and persists to config.env. The endpoint
# covers TWO independent settings:
#   1. STT engine (whisper / groq / openai-compatible) + its URL/key
#   2. Whisper model size (only relevant when engine == whisper or
#      when cloud backends fall back to local)
# Both are admin-only — global backend settings, not per-user.

@app.get("/api/voice/config")
def voice_config_get(
    _user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    from . import voice as _voice
    return {
        # Whisper model picker (existing UI surface)
        "stt_model":   _voice.WHISPER_MODEL_NAME,
        "catalogue":   _voice.WHISPER_CATALOGUE,
        # Engine picker (new — cloud backends with auto-fallback)
        "stt_backend":      _voice.STT_BACKEND,
        "stt_url":          _voice.STT_URL,
        "stt_api_key_set":  bool(_voice.STT_API_KEY),  # never return the key itself
        "stt_model_name":   _voice.STT_MODEL_NAME,
        "backends":         _voice.STT_BACKEND_CATALOGUE,
    }


@app.patch("/api/voice/config")
def voice_config_patch(
    body: Dict[str, Any] = Body(...),
    user: dict[str, Any] = Depends(_auth.require_admin),
) -> Dict[str, Any]:
    """Switch the Whisper model and/or STT engine at runtime. Admin
    only — these are global backend settings, not per-user. All
    fields are optional; sending {stt_model: "..."} alone is the
    legacy whisper-only flow and still works unchanged."""
    from . import voice as _voice
    config_path = Path(os.getenv("HOMEOS_CONFIG_FILE", "config.env"))
    persist_keys: list[tuple[str, str]] = []
    log = logging.getLogger("homeos.voice")

    # 1) Whisper model swap (legacy field).
    new_model = (body.get("stt_model") or "").strip()
    if new_model:
        if new_model not in _voice.VALID_WHISPER_MODELS:
            raise HTTPException(
                status_code=400,
                detail=f"unknown model {new_model!r}; valid: {sorted(_voice.VALID_WHISPER_MODELS)}",
            )
        _voice.set_model(new_model)
        persist_keys.append(("HOMEOS_WHISPER_MODEL", new_model))

    # 2) Cloud-backend swap. Any of {stt_backend, stt_url,
    # stt_api_key, stt_model_name} may be present; missing fields
    # keep their current value. To CLEAR the API key, pass an empty
    # string explicitly.
    backend_touched = any(
        k in body for k in ("stt_backend", "stt_url", "stt_api_key", "stt_model_name")
    )
    if backend_touched:
        new_backend = (body.get("stt_backend") or _voice.STT_BACKEND).strip().lower()
        if new_backend not in _voice.VALID_STT_BACKENDS:
            raise HTTPException(
                status_code=400,
                detail=f"unknown STT backend {new_backend!r}; valid: {sorted(_voice.VALID_STT_BACKENDS)}",
            )
        # `None` means "don't change" — that's why we read the body's
        # `.get(k)` with a sentinel rather than coalescing to "".
        new_url = body["stt_url"].strip() if "stt_url" in body else None
        new_key = body["stt_api_key"] if "stt_api_key" in body else None
        new_model_name = body["stt_model_name"].strip() if "stt_model_name" in body else None

        # Guard: switching to a cloud backend without a key set
        # anywhere (neither stored nor in this PATCH body) is almost
        # certainly a UI bug we want to catch loudly.
        catalogue_entry = next(
            (b for b in _voice.STT_BACKEND_CATALOGUE if b["id"] == new_backend), None,
        )
        if catalogue_entry and catalogue_entry["requires_key"]:
            effective_key = new_key if new_key is not None else _voice.STT_API_KEY
            if not effective_key:
                raise HTTPException(
                    status_code=400,
                    detail=f"backend {new_backend!r} needs an API key (HOMEOS_STT_API_KEY)",
                )

        _voice.set_backend(
            new_backend, url=new_url, api_key=new_key, model_name=new_model_name,
        )
        persist_keys.append(("HOMEOS_STT_BACKEND", new_backend))
        if new_url is not None:
            persist_keys.append(("HOMEOS_STT_URL", new_url))
        if new_key is not None:
            persist_keys.append(("HOMEOS_STT_API_KEY", new_key))
        if new_model_name is not None:
            persist_keys.append(("HOMEOS_STT_MODEL_NAME", new_model_name))

    # Persist all touched keys to config.env so the next process
    # boot reads the same state. Same write-and-swallow pattern as
    # the LLM config endpoint — if persistence fails, the in-memory
    # swap still applies for this process lifetime.
    if persist_keys:
        try:
            text = config_path.read_text() if config_path.exists() else ""
            for k, v in persist_keys:
                text = _replace_or_append_env(text, k, v)
            config_path.write_text(text)
        except OSError as exc:
            log.warning(
                "couldn't persist voice config: %s (in-memory swap still applied)", exc,
            )

    return {
        "ok": True,
        "stt_model":       _voice.WHISPER_MODEL_NAME,
        "stt_backend":     _voice.STT_BACKEND,
        "stt_url":         _voice.STT_URL,
        "stt_api_key_set": bool(_voice.STT_API_KEY),
        "stt_model_name":  _voice.STT_MODEL_NAME,
    }


@app.post("/api/voice/test-connection")
def voice_test_connection(
    body: Dict[str, Any] = Body(default={}),
    user: dict[str, Any] = Depends(_auth.require_admin),
) -> Dict[str, Any]:
    """Verify a cloud STT endpoint actually works. Synthesizes a 0.5s
    silent WAV in memory and POSTs it through the same code path the
    real /api/ask-voice handler uses. Returns {ok: true} on success,
    {ok: false, error: "..."} on any failure. Admin-only — exposes
    credential status."""
    from . import voice as _voice
    import io
    import struct
    import wave
    import tempfile as _tf

    # Allow ad-hoc test with override fields (so the UI can validate
    # a key BEFORE clicking save). Defaults come from current state.
    backend = (body.get("backend") or _voice.STT_BACKEND).strip().lower()
    url = body.get("url") if body.get("url") is not None else _voice.STT_URL
    api_key = body.get("api_key") if body.get("api_key") is not None else _voice.STT_API_KEY
    model_name = body.get("model_name") if body.get("model_name") is not None else _voice.STT_MODEL_NAME

    if backend == "whisper":
        # No connection to test — Whisper is in-process.
        return {"ok": True, "note": "Local Whisper has no remote endpoint to test."}

    # Build a 0.5s silent 16 kHz mono WAV. Cheap (~16 KB), real
    # enough to exercise the full multipart-upload code path.
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x00" * 8000)
    silent_wav = buf.getvalue()

    # Save the current voice module state, temporarily apply the
    # test fields, run _transcribe_cloud, then restore. We do this
    # rather than passing args through because _transcribe_cloud is
    # the actual code path we want to validate.
    prev = (_voice.STT_BACKEND, _voice.STT_URL, _voice.STT_API_KEY, _voice.STT_MODEL_NAME)
    try:
        _voice.STT_BACKEND = backend
        _voice.STT_URL = (url or "").strip()
        _voice.STT_API_KEY = api_key or ""
        _voice.STT_MODEL_NAME = (model_name or "").strip()
        with _tf.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(silent_wav)
            tmp_path = tmp.name
        try:
            _voice._transcribe_cloud(tmp_path)  # noqa: SLF001 — internal but stable
            return {"ok": True}
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        (_voice.STT_BACKEND, _voice.STT_URL, _voice.STT_API_KEY, _voice.STT_MODEL_NAME) = prev


# Voice ack helper — reads the per-user toggle. We can't reliably
# identify the speaker until AFTER the speaker-ID step (which is too
# slow for the ack to fire before it). So for the ack-enabled check
# we use the role-derived admin user as a proxy. Good enough — if the
# admin disables acks, the household stays quiet.
def _voice_ack_enabled_for_role(role: str) -> bool:
    try:
        with conn_ctx(DB_PATH) as conn:
            row = conn.execute(
                "SELECT voice_ack_enabled FROM user_profiles "
                "WHERE role = ? ORDER BY id ASC LIMIT 1",
                (role or "admin",),
            ).fetchone()
        if not row:
            return True
        return bool(row["voice_ack_enabled"])
    except Exception:
        return True  # fail-open: ack is harmless


@app.patch("/api/profile/voice-ack")
def patch_voice_ack(
    body: Dict[str, Any] = Body(...),
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Toggle the instant-ack voice cue ('Moment, ich schau…') on/off
    for the calling user. ON by default."""
    enabled = bool(body.get("enabled", True))
    with conn_ctx(DB_PATH) as conn:
        conn.execute(
            "UPDATE user_profiles SET voice_ack_enabled=? WHERE id=?",
            (1 if enabled else 0, user["id"]),
        )
    return {"ok": True, "voice_ack_enabled": enabled}


def _user_dev_mode(user_id: str) -> bool:
    """Look up the per-user dev_mode toggle. Fail-safe to OFF."""
    try:
        with conn_ctx(DB_PATH) as conn:
            row = conn.execute(
                "SELECT dev_mode FROM user_profiles WHERE id = ?", (user_id,),
            ).fetchone()
        return bool(row and row["dev_mode"])
    except Exception:
        return False


@app.patch("/api/profile/default-doc-visibility")
def patch_default_doc_visibility(
    body: Dict[str, Any] = Body(...),
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Set the user's default visibility for new document uploads
    (private / business / shared). Subsequent uploads from this user
    auto-apply the matching Paperless tag unless overridden per-upload."""
    val = (body.get("visibility") or "").strip().lower()
    if val not in ("private", "business", "shared"):
        raise HTTPException(400, "visibility must be private | business | shared")
    with conn_ctx(DB_PATH) as conn:
        conn.execute(
            "UPDATE user_profiles SET default_doc_visibility=? WHERE id=?",
            (val, user["id"]),
        )
    return {"ok": True, "default_doc_visibility": val}


@app.patch("/api/profile/dev-mode")
def patch_dev_mode(
    body: Dict[str, Any] = Body(...),
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Toggle developer mode for the calling user. When ON, /api/ask
    responses include an ``agent_trace`` field (per-iteration timing +
    tool calls + result snippets) which the chat UI renders as a
    collapsible '▼ Debug' pane below each Yorik reply. OFF by default —
    end-users see no debug noise, no extra payload."""
    enabled = bool(body.get("enabled", False))
    with conn_ctx(DB_PATH) as conn:
        conn.execute(
            "UPDATE user_profiles SET dev_mode=? WHERE id=?",
            (1 if enabled else 0, user["id"]),
        )
    return {"ok": True, "dev_mode": enabled}


@app.patch("/api/profile/confirm-mutations")
def patch_confirm_mutations(
    body: Dict[str, Any] = Body(...),
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Toggle the confirmation modal on/off for the calling user.
    Setting it OFF means create/update/delete skills run immediately
    without staging (decision is logged as 'auto' for telemetry)."""
    enabled = bool(body.get("enabled", True))
    with conn_ctx(DB_PATH) as conn:
        conn.execute(
            "UPDATE user_profiles SET confirm_mutations=? WHERE id=?",
            (1 if enabled else 0, user["id"]),
        )
    return {"ok": True, "confirm_mutations": enabled}


# ─── /api/llm/config (existing) ────────────────────────────────────


@app.get("/api/llm/config")
def llm_config_get(user: dict[str, Any] = Depends(_auth.require_admin)) -> Dict[str, Any]:
    """Current LLM endpoint + model, plus the served-model list. Drives
    the Settings → LLM tab.

    `has_api_key` indicates whether a household-wide API key is stored
    (for cloud providers). The actual key is never returned."""
    stored_key = vanna_agent.get_stored_llm_api_key()
    probe = _probe_url_for_models(vanna_agent.LLM_BASE_URL, api_key=stored_key)
    return {
        "base_url":      vanna_agent.LLM_BASE_URL,
        "model":         vanna_agent.LLM_MODEL,
        "reachable":     probe["ok"],
        "reason":        probe.get("reason", ""),
        "served_models": probe.get("models", []),
        "has_api_key":   bool(stored_key),
    }


@app.post("/api/llm/probe")
def llm_config_probe(
    body: LlmProbeIn,
    _user: dict[str, Any] = Depends(_auth.require_admin),
) -> Dict[str, Any]:
    """Test an arbitrary endpoint URL without persisting anything. Returns
    {ok, models, reason}. Used by the 'Test' button next to the URL
    field in Settings → LLM.

    `api_key` (optional) sent as Bearer for cloud endpoints. If not
    provided, falls back to the stored household key — so the Test
    button still works after the user enters a key without saving."""
    key = body.api_key if body.api_key is not None else vanna_agent.get_stored_llm_api_key()
    return _probe_url_for_models(body.base_url, timeout=2.0, api_key=key)


@app.get("/api/llm/detect")
def llm_config_detect(_user: dict[str, Any] = Depends(_auth.require_admin)) -> Dict[str, Any]:
    """Sweep the common LLM endpoints on localhost so a user with an
    existing setup gets a one-click 'use this' button. ~300ms each
    in parallel via concurrent futures (fast enough to keep inline)."""
    from concurrent.futures import ThreadPoolExecutor
    out = []
    with ThreadPoolExecutor(max_workers=len(_LLM_DETECT_CANDIDATES)) as pool:
        futures = {
            cand["base_url"]: pool.submit(_probe_url_for_models, cand["base_url"], 0.4)
            for cand in _LLM_DETECT_CANDIDATES
        }
        for cand in _LLM_DETECT_CANDIDATES:
            result = futures[cand["base_url"]].result()
            out.append({
                "label":    cand["label"],
                "base_url": cand["base_url"],
                **result,
            })
    return {"candidates": out}


@app.patch("/api/llm/config")
def llm_config_patch(
    body: LlmConfigIn,
    _user: dict[str, Any] = Depends(_auth.require_admin),
) -> Dict[str, Any]:
    """Save base_url + model into config.env on disk and update the
    running process's in-memory references. The OpenAI SDK client is
    rebuilt with the new base_url so the change takes effect on the
    very next chat turn — no restart required.

    Validation: we probe the URL + verify the model is served before
    persisting. If you really want to save a config the server can't
    reach right now (e.g. you're about to start your LLM), pass an
    obviously-served model name — the UI's 'Save anyway' path posts
    with the model id you picked from served_models so this is a
    self-consistent flow.
    """
    # If api_key was provided in the body, persist it BEFORE the probe so
    # the probe can authenticate against cloud endpoints. Empty string
    # clears the stored key (revert to local-style "not-used").
    if body.api_key is not None:
        vanna_agent.set_stored_llm_api_key(body.api_key or None)

    # Validate the target (uses the just-stored or previously-stored key).
    effective_key = vanna_agent.get_stored_llm_api_key()
    probe = _probe_url_for_models(body.base_url, timeout=2.0, api_key=effective_key)
    if not probe["ok"]:
        raise HTTPException(
            status_code=400,
            detail=f"Can't reach {body.base_url}: {probe['reason']}. Start the LLM first or fix the URL.",
        )
    served = set(probe["models"])
    if not (body.model in served
            or f"{body.model}:latest" in served
            or (body.model.endswith(":latest") and body.model.rsplit(":", 1)[0] in served)):
        raise HTTPException(
            status_code=400,
            detail=f"Model {body.model!r} not served by {body.base_url}. Served: {sorted(served)[:5]}",
        )

    # Persist URL + model to config.env. The api_key lives in the
    # encrypted credential_store (separate file, owner-only 0600 mode)
    # to keep secrets out of plain-text config.env.
    config_path = Path(os.getenv("HOMEOS_CONFIG_FILE", "config.env"))
    try:
        if config_path.exists():
            text = config_path.read_text()
        else:
            text = ""
        text = _replace_or_append_env(text, "HOMEOS_LLM_BASE_URL", body.base_url)
        text = _replace_or_append_env(text, "HOMEOS_MODEL",        body.model)
        config_path.write_text(text)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"couldn't write config.env: {exc}")

    # Live-reload: rebuild both the legacy LLM client AND the new agent
    # backend's LlmClient. Pre-2026-06-01 only the legacy path was
    # rebuilt, so /api/ask (which routes through the new backend) would
    # keep using the old endpoint until restart.
    os.environ["HOMEOS_LLM_BASE_URL"] = body.base_url
    os.environ["HOMEOS_MODEL"]        = body.model
    try:
        vanna_agent.rebuild_llm(body.base_url, body.model, api_key=effective_key)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("homeos.llm").warning(
            "live LLM rebuild failed (config saved; restart Yorik to pick up new URL): %s", exc,
        )

    # Bust the probe cache so the response shows the new endpoint's status.
    _llm_probe_cache["checked_at"] = 0.0
    return llm_config_get(user={})  # type: ignore[arg-type]


def _replace_or_append_env(text: str, key: str, value: str) -> str:
    """Set `KEY=value` in a dotenv string. Preserves comments + ordering."""
    import re as _re
    pattern = _re.compile(rf"^{_re.escape(key)}=.*$", _re.MULTILINE)
    if pattern.search(text):
        return pattern.sub(f"{key}={value}", text)
    if text and not text.endswith("\n"):
        text += "\n"
    return text + f"{key}={value}\n"


# ─── System status — fed to the React /r/home dashboard ────────────────────

@app.get("/api/system/errors")
def system_errors(
    limit: int = Query(50, ge=1, le=500),
    level: Optional[str] = Query(None, description="WARNING | ERROR | CRITICAL"),
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Recent WARNING+ records from the persistent error_log table.
    Admin-only — secrets filter is in front but error messages can
    still hint at infra (LLM URL, IMAP host) we don't want surfaced
    to lower-privilege roles."""
    if normalize_role(user.get("role", "")) not in ("admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="admin only")
    from . import error_log as _el
    return {"errors": _el.recent(limit=limit, level=level),
            "summary": _el.summary()}


@app.get("/api/debug-bundle")
def debug_bundle(
    conversation_id: str = Query(..., description="agent_conversations.id"),
    redact: bool = Query(True, description="Apply best-effort regex redaction"),
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Owner-initiated debug export for one conversation. Returns the
    full message trace + environment fingerprint, with email / phone /
    IBAN / IP / secret tokens auto-redacted by default. The caller
    decides where to send it; Yorik does not phone home."""
    from . import debug_bundle as _db
    try:
        return _db.build_bundle(
            conversation_id,
            user_id=user["id"],
            role=str(user.get("role") or ""),
            redact=redact,
        )
    except _db.BundleError as e:
        msg = str(e)
        status = 403 if "owner or an admin" in msg else 404
        raise HTTPException(status_code=status, detail=msg)


@app.get("/api/system/status")
def system_status(
    role: str = Depends(_auth.current_role),
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """One-shot health view for the home screen. Covers LLM, connectors,
    Paperless, last backup, and counts (docs, events, tasks, conversations).
    Everything is best-effort — failures degrade individual chips, never
    bring the home screen down."""
    normalize_role(role)

    # LLM
    llm = {
        "model": vanna_agent.LLM_MODEL,
        "base_url": vanna_agent.LLM_BASE_URL,
        "reachable": _llm_reachable(),
    }

    # Connectors that have credentials stored
    try:
        configured = credential_store.list_configured()
    except Exception:  # noqa: BLE001
        configured = []
    connector_names = {c.get("name") for c in configured if isinstance(c, dict) and c.get("name")}

    # Email connector (combined view of imap + gmail)
    email_kinds = sorted(n for n in connector_names if n and n.startswith("email-"))
    email = {
        "configured": ("email-imap" in connector_names) or ("email-gmail" in connector_names),
        "kinds":      email_kinds,
    }

    # Paperless — admin token present → mirror running.
    # IMPORTANT: use the same resolver paperless_ingest._paperless_settings()
    # uses, which checks BOTH the connector credential store AND the legacy
    # app_settings keys (paperless_api_token / paperless_base_url) that
    # start.sh writes on first-run setup. Reading only credential_store
    # made the home screen say "Not linked" for any box set up via
    # start.sh, even though the actual integration worked fine.
    from . import paperless_ingest as _paperless_mod
    _p_settings = _paperless_mod._paperless_settings()
    paperless = {
        "admin_token_set": bool(_p_settings.get("api_key")),
        "url":             _p_settings.get("base_url"),
    }

    # Backup
    backup = {"last": None, "configured": False}
    try:
        from . import backup as _backup_mod
        history = _backup_mod.list_history(limit=1)
        if history:
            backup["last"] = history[0]
            backup["configured"] = True
        else:
            # Even with no runs yet, if a target path is set in env, treat as configured.
            backup["configured"] = bool(os.getenv("HOMEOS_BACKUP_TARGET"))
    except Exception:  # noqa: BLE001
        pass

    # Counts (cheap)
    counts: Dict[str, int] = {}
    try:
        with conn_ctx(DB_PATH) as conn:
            for label, sql in [
                ("events",        "SELECT COUNT(*) FROM events"),
                ("tasks",         "SELECT COUNT(*) FROM tasks WHERE done = 0"),
                ("bills_unpaid",  "SELECT COUNT(*) FROM bills WHERE paid = 0"),
                ("conversations", "SELECT COUNT(*) FROM conversations"),
                ("users",         "SELECT COUNT(*) FROM user_profiles WHERE disabled = 0"),
            ]:
                try:
                    counts[label] = conn.execute(sql).fetchone()[0]
                except Exception:  # noqa: BLE001
                    counts[label] = 0
    except Exception:  # noqa: BLE001
        pass

    # Document counts come from a separate DB
    try:
        from . import documents as _docs_mod
        counts["documents"] = len(_docs_mod.list_documents(role=role))
    except Exception:  # noqa: BLE001
        counts["documents"] = 0

    # Compose templates + numbering series
    try:
        from .compose import templates as _tpl_mod
        counts["templates"] = len(_tpl_mod.load_all())
    except Exception:  # noqa: BLE001
        counts["templates"] = 0

    try:
        from .compose import series as _series_mod
        counts["numbering_series"] = len(_series_mod.list_series(owner_user_id=user.get("id")))
    except Exception:  # noqa: BLE001
        counts["numbering_series"] = 0

    return {
        "llm":       llm,
        "email":     email,
        "paperless": paperless,
        "backup":    backup,
        "counts":    counts,
        "user":      {"name": user.get("name"), "role": user.get("role"), "language": user.get("language")},
        "configured_connectors": sorted(connector_names),
    }


# ─── Quality signals: per-turn feedback + skill telemetry + dashboard ──────

class TurnFeedbackIn(BaseModel):
    conversation_id: Optional[str] = None
    message_idx: Optional[int] = None
    rating: int = Field(ge=-1, le=1)   # -1, 0 (clear), +1
    note: Optional[str] = None
    llm_model: Optional[str] = None    # falls back to the configured default


@app.post("/api/feedback/turn", status_code=201)
def feedback_turn(
    body: TurnFeedbackIn,
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Record a thumbs-up/thumbs-down on a chat turn. Tagged with the LLM model so the
    quality dashboard can separate 'qwen3.6 struggles here' from 'the
    skill itself is broken'."""
    model = body.llm_model or vanna_agent.LLM_MODEL
    with conn_ctx(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO turn_feedback (conversation_id, message_idx, rating, note, llm_model, user_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (body.conversation_id, body.message_idx, body.rating, body.note, model, user.get("id")),
        )
    return {"ok": True, "id": cur.lastrowid}


class TemplateRatingIn(BaseModel):
    template_id: str
    rating: int = Field(ge=-1, le=1)
    note: Optional[str] = None
    llm_model: Optional[str] = None


@app.post("/api/feedback/template", status_code=201)
def feedback_template(
    body: TemplateRatingIn,
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Thumbs-up/down on a Compose template — useful for spotting which AI-generated
    templates need cleanup. Same model-tag rationale as turn feedback."""
    model = body.llm_model or vanna_agent.LLM_MODEL
    with conn_ctx(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO template_ratings (template_id, rating, note, llm_model, user_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (body.template_id, body.rating, body.note, model, user.get("id")),
        )
    return {"ok": True, "id": cur.lastrowid}


@app.get("/api/quality/summary")
def quality_summary(
    role: str = Depends(_auth.current_role),
    days: int = Query(30, ge=1, le=365),
) -> Dict[str, Any]:
    """Aggregated quality metrics for the local dashboard. Per-skill /
    per-template / per-turn, broken down by LLM model so qwen-vs-claude
    differences become obvious. Anyone with a Yorik account can view —
    this is the user's own data."""
    normalize_role(role)
    # Pre-compute the cutoff timestamp in Python so the SQL is portable
    # across SQLite and Postgres — the previous datetime('now', '-N days')
    # form is SQLite-only and crashed on the Postgres backend with
    # `function datetime(unknown, unknown) does not exist`.
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    _cutoff = (_dt.now(_tz.utc) - _td(days=days)).isoformat()
    with conn_ctx(DB_PATH) as conn:
        skills = conn.execute(
            "SELECT skill_id, llm_model, "
            "       COUNT(*) AS n, "
            "       SUM(success) AS successes, "
            "       AVG(latency_ms) AS avg_latency_ms "
            "FROM skill_invocations "
            "WHERE created_at > ? "
            "GROUP BY skill_id, llm_model "
            "ORDER BY n DESC",
            (_cutoff,),
        ).fetchall()
        turns = conn.execute(
            "SELECT llm_model, "
            "       COUNT(*) AS n, "
            "       SUM(CASE WHEN rating = 1 THEN 1 ELSE 0 END) AS up, "
            "       SUM(CASE WHEN rating = -1 THEN 1 ELSE 0 END) AS down "
            "FROM turn_feedback "
            "WHERE created_at > ? "
            "GROUP BY llm_model "
            "ORDER BY n DESC",
            (_cutoff,),
        ).fetchall()
        templates = conn.execute(
            "SELECT template_id, llm_model, "
            "       COUNT(*) AS n, "
            "       SUM(CASE WHEN rating = 1 THEN 1 ELSE 0 END) AS up, "
            "       SUM(CASE WHEN rating = -1 THEN 1 ELSE 0 END) AS down "
            "FROM template_ratings "
            "WHERE created_at > ? "
            "GROUP BY template_id, llm_model "
            "ORDER BY n DESC",
            (_cutoff,),
        ).fetchall()
        recent_failures = conn.execute(
            "SELECT skill_id, llm_model, error, created_at "
            "FROM skill_invocations "
            "WHERE success = 0 AND error IS NOT NULL "
            "ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
    return {
        "window_days": days,
        "current_model": vanna_agent.LLM_MODEL,
        "skills":           [dict(r) for r in skills],
        "turns_by_model":   [dict(r) for r in turns],
        "templates":        [dict(r) for r in templates],
        "recent_failures":  [dict(r) for r in recent_failures],
    }


# ─── Document numbering (Compose Phase 1 — Lexoffice-replacement) ──────────

class SeriesCreateIn(BaseModel):
    kind: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=80)
    scheme: str = Field(default="{year}-{seq}")
    prefix: str = ""
    seq_padding: int = Field(default=3, ge=1, le=10)
    starting_number: int = Field(default=1, ge=1)
    year_reset: bool = True
    is_default: bool = True
    notes: Optional[str] = None


class SeriesPatchIn(BaseModel):
    name: Optional[str] = None
    scheme: Optional[str] = None
    prefix: Optional[str] = None
    seq_padding: Optional[int] = Field(default=None, ge=1, le=10)
    next_number: Optional[int] = Field(default=None, ge=1)
    year_reset: Optional[bool] = None
    is_default: Optional[bool] = None
    notes: Optional[str] = None


class SeriesPresetIn(BaseModel):
    preset: str  # 'de' | 'us' | 'pl'


@app.get("/api/compose/series/presets")
def compose_series_presets(role: str = Depends(_auth.current_role)) -> Dict[str, Any]:
    """List regional presets the first-run wizard can offer."""
    normalize_role(role)
    from .compose import series as ser
    out = {}
    for k, v in ser.REGIONAL_PRESETS.items():
        out[k] = {
            "label": v["label"],
            "description": v["description"],
            "series": v["series"],
        }
    return {"presets": out}


@app.get("/api/compose/series")
def compose_series_list(
    role: str = Depends(_auth.current_role),
    kind: Optional[str] = Query(None),
    user: dict[str, Any] = Depends(_auth.current_user),
) -> List[Dict[str, Any]]:
    normalize_role(role)
    from .compose import series as ser
    items = ser.list_series(kind=kind, owner_user_id=user.get("id"))
    # Decorate with the next-number preview so the UI doesn't have to
    # round-trip an extra endpoint per row.
    for it in items:
        try:
            it["preview"] = ser.preview_next(it["id"])
        except Exception:  # noqa: BLE001
            it["preview"] = None
    return items


@app.post("/api/compose/series", status_code=201)
def compose_series_create(
    body: SeriesCreateIn,
    role: str = Depends(_auth.current_role),
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    if normalize_role(role) not in ("admin", "platform_admin"):
        raise HTTPException(403, "role required: admin")
    from .compose import series as ser
    try:
        return ser.create_series(
            kind=body.kind, name=body.name, scheme=body.scheme, prefix=body.prefix,
            seq_padding=body.seq_padding, starting_number=body.starting_number,
            year_reset=body.year_reset, is_default=body.is_default,
            owner_user_id=user.get("id"), notes=body.notes,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.patch("/api/compose/series/{series_id}")
def compose_series_patch(
    series_id: int,
    body: SeriesPatchIn,
    role: str = Depends(_auth.current_role),
) -> Dict[str, Any]:
    if normalize_role(role) not in ("admin", "platform_admin"):
        raise HTTPException(403, "role required: admin")
    from .compose import series as ser
    try:
        return ser.update_series(
            series_id,
            name=body.name, scheme=body.scheme, prefix=body.prefix,
            seq_padding=body.seq_padding, next_number=body.next_number,
            year_reset=body.year_reset, is_default=body.is_default, notes=body.notes,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/compose/series/{series_id}", status_code=204, response_class=Response)
def compose_series_delete(series_id: int, role: str = Depends(_auth.current_role)) -> Response:
    if normalize_role(role) not in ("admin", "platform_admin"):
        raise HTTPException(403, "role required: admin")
    from .compose import series as ser
    try:
        if not ser.delete_series(series_id):
            raise HTTPException(404, f"series {series_id} not found")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return Response(status_code=204)


@app.get("/api/compose/series/{series_id}/preview")
def compose_series_preview(series_id: int, role: str = Depends(_auth.current_role)) -> Dict[str, Any]:
    normalize_role(role)
    from .compose import series as ser
    try:
        return ser.preview_next(series_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.get("/api/compose/series/{series_id}/allocations")
def compose_series_allocations(
    series_id: int,
    role: str = Depends(_auth.current_role),
    limit: int = Query(50, ge=1, le=500),
) -> List[Dict[str, Any]]:
    normalize_role(role)
    from .compose import series as ser
    return ser.list_allocations(series_id, limit=limit)


@app.post("/api/compose/series/install-preset", status_code=201)
def compose_series_install_preset(
    body: SeriesPresetIn,
    role: str = Depends(_auth.current_role),
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """First-run wizard: install a regional preset (creates the relevant
    series in one click). Idempotent — skips series whose kind already
    has a default."""
    if normalize_role(role) not in ("admin", "platform_admin"):
        raise HTTPException(403, "role required: admin")
    from .compose import series as ser
    try:
        created = ser.install_preset(body.preset, owner_user_id=user.get("id"))
        return {"created": created, "count": len(created)}
    except ValueError as exc:
        raise HTTPException(400, str(exc))


class ComposeTemplateInstallIn(BaseModel):
    """Install via file path (admin pastes /path/to/template.json) or
    inline JSON content. Either is fine; one of the two must be set."""
    path: Optional[str] = None
    content: Optional[Dict[str, Any]] = None


@app.post("/api/compose/templates/install")
def compose_template_install(body: ComposeTemplateInstallIn, role: str = Depends(_auth.current_role)) -> Dict[str, Any]:
    """Install a new Compose template. Two modes:
      1. path  — read JSON from a local file (admin's filesystem)
      2. content — inline JSON object (paste from clipboard or upload)
    Both run through the same Tier-0 validator as the built-in templates.
    """
    if normalize_role(role) not in ("admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="role required: admin")
    from .compose import templates as tpl
    import json as _json
    import shutil

    if body.path:
        src = Path(body.path)
        if not src.is_file():
            raise HTTPException(status_code=400, detail=f"path not found: {src}")
        try:
            data = _json.loads(src.read_text())
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}")
    elif body.content:
        data = body.content
    else:
        raise HTTPException(status_code=400, detail="provide 'path' or 'content'")

    errs = tpl._validate(data, source="install")
    if errs:
        raise HTTPException(status_code=400, detail=f"template invalid: {errs}")

    dst = tpl.TEMPLATES_DIR / f"{data['id']}.json"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(_json.dumps(data, indent=2, ensure_ascii=False))
    return {"ok": True, "id": data["id"], "path": str(dst)}


class ComposeCommunityInstallIn(BaseModel):
    """Pull a template from the community catalogue by id and install it
    via the same Tier-0 validator as manual installs."""
    id: str


@app.get("/api/compose/community/templates")
def compose_community_list(
    role: str = Depends(_auth.current_role),
    refresh: bool = False,
) -> Dict[str, Any]:
    """List templates from the configured community catalogue.
    Catalogue URL is overridable via YORIK_COMMUNITY_TEMPLATES_URL env;
    default is the public yorik-community GitHub raw URL.

    Returns {templates, source, fetched_at, cached, error}. On fetch
    failure (network down, repo private, 404), `templates` is empty
    and `error` carries a short reason so the UI can surface a
    "couldn't reach community" banner without 500'ing the page."""
    normalize_role(role)
    from .compose import community
    return community.fetch_catalogue(force=bool(refresh))


@app.post("/api/compose/community/install")
def compose_community_install(
    body: ComposeCommunityInstallIn,
    role: str = Depends(_auth.current_role),
) -> Dict[str, Any]:
    """Install a community template by its catalogue id. Re-uses the
    same validator + write path as the manual install endpoint so a
    malicious community entry can't bypass safety checks."""
    if normalize_role(role) not in ("admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="role required: admin")
    from .compose import community, templates as tpl
    import json as _json
    data = community.get_full_template(body.id)
    if not data:
        raise HTTPException(status_code=404,
                            detail=f"template id '{body.id}' not in community catalogue")
    errs = tpl._validate(data, source=f"community:{body.id}")
    if errs:
        raise HTTPException(status_code=400, detail=f"template invalid: {errs}")
    dst = tpl.TEMPLATES_DIR / f"{data['id']}.json"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(_json.dumps(data, indent=2, ensure_ascii=False))
    return {"ok": True, "id": data["id"], "path": str(dst)}


@app.delete("/api/compose/templates/{template_id}", status_code=204, response_class=Response)
def compose_template_delete(template_id: str, role: str = Depends(_auth.current_role)) -> Response:
    """Remove a template from disk. Admin only. Does not affect documents
    already rendered from this template."""
    if normalize_role(role) not in ("admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="role required: admin")
    from .compose import templates as tpl
    path = tpl.TEMPLATES_DIR / f"{template_id}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"template {template_id} not found")
    path.unlink()
    return Response(status_code=204)


class ComposeSaveIn(BaseModel):
    body_html: str
    title: str
    tags: Optional[List[str]] = None
    correspondent: Optional[str] = None
    page_size: str = "A4"
    margins_mm: Optional[List[int]] = None
    # Series IDs whose number was used in this document. Consumed (with
    # audit log) exactly here, not at draft time — so abandoned drafts
    # don't burn invoice numbers.
    series_consumes: Optional[List[int]] = None


@app.post("/api/compose/save")
def compose_save(
    body: ComposeSaveIn,
    role: str = Depends(_auth.current_role),
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Render the edited HTML to PDF and post it to Paperless. Paperless
    OCR's it + fires the consume webhook, so Yorik's vector index picks it
    up automatically and the doc becomes voice-searchable within seconds.

    Also consumes any series IDs the frontend passed (e.g. the
    Rechnungsnummer the user just allocated) and links the audit entry
    to the resulting Paperless doc + PDF hash. Order matters: Paperless
    upload first, then consume — if upload fails we never burn a number,
    so the user never sees a "gap" in their Rechnungsnummern.
    """
    if normalize_role(role) not in ("admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="role required: admin")
    from .compose import save as save_mod
    margins = tuple(body.margins_mm or [20, 18, 25, 18])
    res = save_mod.save_to_paperless(
        body.body_html, title=body.title, tags=body.tags or ["compose"],
        correspondent=body.correspondent, page_size=body.page_size,
        margins_mm=margins,
    )
    if not res.get("ok"):
        raise HTTPException(status_code=502, detail=res.get("error") or "save failed")

    # Consume series numbers AFTER paperless succeeded — Paperless upload
    # failures must not burn invoice numbers.
    consumed = []
    if body.series_consumes:
        from .compose import series as ser
        for sid in body.series_consumes:
            try:
                alloc = ser.consume(
                    sid,
                    consumed_by_user_id=user.get("id"),
                    title=body.title,
                    paperless_doc_id=res.get("paperless_doc_id"),
                    pdf_bytes=res.get("pdf_bytes"),
                )
                consumed.append(alloc)
            except Exception as exc:  # noqa: BLE001
                logging.getLogger("homeos.compose").warning(
                    "series %s consume failed after save: %s", sid, exc,
                )
    if consumed:
        res = {**res, "series_allocations": consumed}
    # Don't leak the PDF bytes in the JSON response.
    res.pop("pdf_bytes", None)
    return res


class ComposeSendIn(BaseModel):
    body_html: str
    to: str
    subject: str
    body_text: str = ""               # email body; the PDF is attached
    title: str = "document"           # PDF filename + Paperless title
    tags: Optional[List[str]] = None
    correspondent: Optional[str] = None
    also_save: bool = True            # auto-save the sent doc to Paperless
    series_consumes: Optional[List[int]] = None  # see ComposeSaveIn
    # Delivery mode:
    #   "attachment" — render PDF via Gotenberg, attach to email (default; what
    #                  Kündigungen / Rechnungen / formale Briefe want)
    #   "inline"     — send body_html AS the email body, no PDF. Useful for
    #                  short informal mails where the recipient just wants
    #                  the text in their inbox, not a downloadable attachment.
    # Templates can hint the default via their `delivery_default` field.
    delivery: str = "attachment"
    # Pick which configured email_accounts row sends the mail. When set,
    # routes via email_sender.send() (per-account creds, APPEND-to-Sent,
    # local mirror). When None, falls back to the legacy email-imap
    # connector path so installs without email_accounts rows still work.
    account_id: Optional[int] = None


@app.post("/api/compose/send-email")
async def compose_send_email(
    body: ComposeSendIn,
    role: str = Depends(_auth.current_role),
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Render PDF → attach to a new email via the email_imap connector
    (or whichever email connector is configured). Optionally also save a
    copy to Paperless tagged `gesendet`. Series consumes happen ONLY
    after a successful send (and optionally after a successful Paperless
    save), so a failed SMTP server doesn't burn an invoice number."""
    if normalize_role(role) not in ("admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="role required: admin")
    from .compose import pdf as pdf_mod
    from . import connectors as connectors_mod

    delivery = (body.delivery or "attachment").lower().strip()
    if delivery not in ("attachment", "inline"):
        raise HTTPException(status_code=400, detail=f"unknown delivery mode: {body.delivery!r}")

    pdf_bytes: bytes = b""
    if delivery == "attachment":
        pdf_bytes = pdf_mod.render_pdf(body.body_html, filename=f"{body.title}.pdf")
        if not pdf_bytes:
            raise HTTPException(status_code=502, detail="PDF render failed (Gotenberg unreachable?)")

    # Build the email payload differently per delivery mode.
    #   - attachment: plain-text body + PDF attachment (the historical shape)
    #   - inline:     HTML body (the rendered letter), no attachment.
    #                 We also pass body_text as a multipart-alternative for
    #                 mail clients that can't render HTML.
    import base64
    import re as _re
    if delivery == "attachment":
        send_payload: dict[str, Any] = {
            "op": "send",
            "to": body.to,
            "subject": body.subject,
            "body": body.body_text or f"(See attached: {body.title}.pdf)",
            "attachments": [{"filename": f"{body.title}.pdf",
                             "mime_type": "application/pdf",
                             "content_b64": base64.b64encode(pdf_bytes).decode("ascii")}],
        }
    else:
        # Plaintext fallback: strip tags from the rendered HTML so clients
        # without HTML rendering still see the letter. body_text (if the user
        # typed one) wins — it's the user's intentional plaintext.
        plain_fallback = body.body_text or _re.sub(
            r"\s+", " ", _re.sub(r"<[^>]+>", " ", body.body_html)
        ).strip()
        send_payload = {
            "op":         "send",
            "to":         body.to,
            "subject":    body.subject,
            "body":       plain_fallback,
            "body_html":  body.body_html,
        }
    # Two send paths:
    #   - account_id set → email_sender.send() (per-account creds from the
    #     email_accounts table; handles APPEND-to-Sent + local mirror +
    #     contact autocapture). This is the path the user sees when they
    #     pick a "From" account in the Send dialog.
    #   - account_id unset → legacy email-imap connector path (single
    #     credential_store creds). Back-compat for installs that never
    #     went through the EmailApp account-wizard.
    if body.account_id is not None:
        from . import email_sender as _sender
        to_list = [t.strip() for t in (body.to or "").split(",") if t.strip()]
        attachments_arg: list[dict[str, Any]] = []
        if delivery == "attachment":
            attachments_arg = [{
                "filename": f"{body.title}.pdf",
                "mimetype": "application/pdf",
                "content":  pdf_bytes,
            }]
            body_text_arg = body.body_text or f"(See attached: {body.title}.pdf)"
            body_html_arg = None
        else:
            body_text_arg = plain_fallback
            body_html_arg = body.body_html
        try:
            send_result = _sender.send(
                account_id=int(body.account_id),
                to=to_list,
                subject=body.subject,
                body_text=body_text_arg,
                body_html=body_html_arg,
                attachments=attachments_arg or None,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"email send failed: {exc}")
    else:
        try:
            send_result = await connectors_mod.invoke("email-imap", send_payload)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"email connector failed: {exc}")
    # Both paths signal failure in-band (return {"ok": False, "error": …}
    # rather than raising) — without this check we would 200-OK a silent
    # failure and the user would never know their invoice didn't go out.
    if not (send_result or {}).get("ok"):
        err = (send_result or {}).get("error") or "email send returned no result"
        needs_install = (send_result or {}).get("needs_install")
        raise HTTPException(
            status_code=502,
            detail=(
                f"email send failed: {err}"
                + (" — open Settings → Connectors → Email (IMAP) to configure SMTP." if needs_install else "")
            ),
        )
    save_result = None
    if body.also_save:
        from .compose import save as save_mod
        save_result = save_mod.save_to_paperless(
            body.body_html, title=body.title,
            tags=(body.tags or []) + ["gesendet"],
            correspondent=body.correspondent,
        )

    # Consume series numbers AFTER the email was successfully sent. If
    # the user wanted "also save to Paperless" but the Paperless leg
    # failed, we still consume — the email went out, so the number was
    # *used*. The audit row records this without a paperless link.
    consumed = []
    if body.series_consumes:
        from .compose import series as ser
        for sid in body.series_consumes:
            try:
                alloc = ser.consume(
                    sid,
                    consumed_by_user_id=user.get("id"),
                    title=body.title,
                    paperless_doc_id=(save_result or {}).get("paperless_doc_id"),
                    pdf_bytes=pdf_bytes or None,
                )
                consumed.append(alloc)
            except Exception as exc:  # noqa: BLE001
                logging.getLogger("homeos.compose").warning(
                    "series %s consume failed after send: %s", sid, exc,
                )

    # Strip PDF bytes from response — fine to leak the size only.
    if save_result and "pdf_bytes" in save_result:
        save_result = {**save_result}
        save_result.pop("pdf_bytes", None)

    return {"ok": True, "email": send_result, "saved": save_result,
            "series_allocations": consumed}


# ── documents / RAG ────────────────────────────────────────────────────────

# Paperless-mirror endpoints. The Paperless container's POST_CONSUME_SCRIPT
# fires the ingest webhook every time a doc finishes processing; reindex
# is for manual backfill; search is what the LLM tool ultimately calls.

def _verify_paperless_token(provided: Optional[str]) -> bool:
    """Compare the X-Paperless-Token header against PAPERLESS_YORIK_TOKEN
    (set via .env / docker-compose). Empty env value means 'auth disabled' —
    only acceptable in dev/single-user."""
    expected = os.getenv("PAPERLESS_YORIK_TOKEN", "").strip()
    if not expected:
        return True
    return bool(provided and provided.strip() == expected)


@app.post("/api/paperless/ingest/{doc_id}")
async def paperless_ingest(
    doc_id: int,
    request: Request,
    background: BackgroundTasks,
):
    """Webhook target for Paperless's POST_CONSUME_SCRIPT.

    Returns 202 immediately and runs the embedding work in the background
    so Paperless doesn't time out waiting for us (ingestion can take a few
    seconds per doc when the embedder is cold).
    """
    if not _verify_paperless_token(request.headers.get("X-Paperless-Token")):
        raise HTTPException(status_code=401, detail="invalid paperless token")
    from . import paperless_ingest as pi
    background.add_task(pi.ingest_one, doc_id)
    return {"queued": True, "id": doc_id}


@app.post("/api/paperless/reindex-all")
def paperless_reindex_all(role: str = Depends(_auth.current_role), background: BackgroundTasks = None):
    """Walk every Paperless document and (re)build the vector index.
    Admin only — runs in the background since it can take many minutes."""
    if normalize_role(role) not in ("admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="role required: admin")
    from . import paperless_ingest as pi
    background.add_task(pi.reindex_all)
    return {"queued": True}


@app.get("/api/embeddings/status")
def embeddings_status(role: str = Depends(_auth.current_role)) -> Dict[str, Any]:
    """Snapshot of the embedding pipeline for Settings → Embeddings.

    Surfaces three things the user cares about when semantic search
    "doesn't work":
      1. Is the embedder reachable AT ALL (bundled local? external URL?)
      2. How much of the Paperless corpus has actually been embedded
         (vec_count vs chunk_count — the gap is the backlog)
      3. Is the background reconciler alive and recently active

    Admin-gated; the panel is install-troubleshooting territory.
    """
    if normalize_role(role) not in ("admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="role required: admin")
    from . import paperless_ingest as pi
    from . import documents as _docs
    from . import workers as _w
    from .database import get_docs_conn

    # Chunk + vec population. chunk_count is how many doc chunks Yorik
    # has extracted; vec_count is how many of those have a vector. The
    # gap = embeddings backlog.
    chunk_count = 0
    vec_count = 0
    try:
        with get_docs_conn(_docs.DOCS_DB_PATH) as conn:
            r1 = conn.execute("SELECT COUNT(*) AS n FROM paperless_chunks").fetchone()
            chunk_count = int(r1["n"] if r1 else 0)
            r2 = conn.execute("SELECT COUNT(*) AS n FROM paperless_vec").fetchone()
            vec_count = int(r2["n"] if r2 else 0)
    except Exception as exc:  # noqa: BLE001
        # Fresh-install / no docs yet: the tables don't exist → COUNT
        # blows up. Treat as "0 of 0" rather than 500ing the panel.
        logging.getLogger("yorik.embeddings").exception(
            "embeddings status: chunk/vec count failed: %s", exc,
        )

    # How many distinct paperless docs have been ingested at all
    # (helps distinguish "no ingest yet" from "partial ingest in flight").
    doc_count = 0
    try:
        with get_docs_conn(_docs.DOCS_DB_PATH) as conn:
            r = conn.execute(
                "SELECT COUNT(DISTINCT paperless_doc_id) AS n FROM paperless_chunks"
            ).fetchone()
            doc_count = int(r["n"] if r else 0)
    except Exception:  # noqa: BLE001
        pass

    # Worker heartbeats. background_reconciler ticks every 6h; autotagger
    # ticks per-doc while a batch job runs. None when never registered.
    reconciler = next(
        (w for w in _w.get_all() if w.get("name") == "paperless_reconciler"),
        None,
    )
    autotagger = next(
        (w for w in _w.get_all() if w.get("name") == "autotagger"),
        None,
    )

    # Taxonomy tag counts — join Paperless's per-tag doc counts with the
    # taxonomy YAML so the Settings panel can render "Rechnung: 142,
    # Strom: 18" without the user's non-taxonomy tags polluting the list.
    taxonomy_tag_counts: List[Dict[str, Any]] = []
    try:
        from . import autotagger as _at
        tax = _at.load_taxonomy()
        name_to_meta = {
            t["de"]: {"id": t["id"], "label_de": t["de"], "label_en": t["en"],
                      "category_id": t["category_id"]}
            for t in _at.all_tags(tax)
        }
        from .connectors.paperless import _settings as _ps
        s = _ps()
        if s.get("api_key"):
            base = (s.get("base_url") or "http://localhost:8010").rstrip("/")
            headers = {"Authorization": f"Token {s['api_key']}", "Accept": "application/json"}
            url: Optional[str] = f"{base}/api/tags/"
            params: Dict[str, Any] = {"page_size": 500, "ordering": "-document_count"}
            while url:
                r = requests.get(url, headers=headers, params=params, timeout=10)
                if not r.ok:
                    break
                body = r.json() or {}
                for t in body.get("results") or []:
                    nm = (t.get("name") or "").strip()
                    if nm in name_to_meta:
                        cnt = int(t.get("document_count") or 0)
                        if cnt > 0:
                            taxonomy_tag_counts.append({
                                **name_to_meta[nm],
                                "count": cnt,
                            })
                url = body.get("next")
                params = {}
            taxonomy_tag_counts.sort(key=lambda x: -x["count"])
    except Exception as exc:  # noqa: BLE001
        log.exception("embeddings_status: taxonomy tag counts failed: %s", exc)

    return {
        "vec_count":     vec_count,
        "chunk_count":   chunk_count,
        "doc_count":     doc_count,
        "embedder": {
            "backend":          _docs.EMBED_BACKEND,
            "reachable":        _docs.embedder_reachable(),
            "external_url":     _docs.EMBED_BASE_URL or None,
            "external_model":   _docs.EMBED_MODEL,
            "local_model":      __import__("os").getenv(
                "HOMEOS_EMBED_LOCAL_MODEL",
                "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
            ),
            "dim":              _docs.EMBED_DIM,
        },
        "reconciler":           reconciler,  # None or worker dict
        "autotagger":           autotagger,  # None or worker dict
        "taxonomy_tag_counts":  taxonomy_tag_counts,
    }


class _AutotagAllIn(BaseModel):
    force_retag: bool = False


@app.post("/api/embeddings/autotag-all")
def embeddings_autotag_all(
    body: _AutotagAllIn = _AutotagAllIn(),
    role: str = Depends(_auth.current_role),
    user: dict[str, Any] = Depends(_auth.current_user),
    background: BackgroundTasks = None,
):
    """Kick off the autotagger across every Paperless document. Admin-
    only, runs in background — see Settings → Embeddings → Autotagger
    for live progress (heartbeats every doc).

    By default skips docs that already carry at least one taxonomy tag
    (idempotent re-run). Set force_retag=true to walk the whole corpus
    again — useful if you edited the taxonomy and want fresh picks.

    New tags are created in the admin's `user_profiles.language` (so an
    English-set Yorik creates "Invoice", a German-set one creates
    "Rechnung"). Paperless tags are global, so the admin's language
    wins across all users of a single install.
    """
    if normalize_role(role) not in ("admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="role required: admin")
    from . import autotagger as _at
    lang = (user.get("language") or "de").lower().strip() or "de"
    background.add_task(_at.autotag_all, force_retag=body.force_retag, lang=lang)
    return {"queued": True, "lang": lang}


@app.post("/api/embeddings/autotag-cancel")
def embeddings_autotag_cancel(role: str = Depends(_auth.current_role)) -> Dict[str, Any]:
    """Cooperative cancel — sets a flag the autotagger checks once per
    doc. The current doc still finishes (in-flight LLM call); the
    walk exits cleanly after that. Heartbeat updates to "STOPPED at N".
    No-op if nothing is running."""
    if normalize_role(role) not in ("admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="role required: admin")
    from . import autotagger as _at
    _at.request_cancel()
    return {"cancelling": True}


@app.get("/api/paperless/search")
def paperless_search(q: str = Query(...), k: int = Query(8, ge=1, le=20),
                     role: str = Depends(_auth.current_role)) -> Dict[str, Any]:
    """Semantic search over the Paperless mirror. Returns chunks + citations."""
    normalize_role(role)
    from . import paperless_ingest as pi
    return {"query": q, "results": pi.search(q, k=k)}


@app.get("/api/documents")
def list_documents_endpoint(
    role: str = Depends(_auth.current_role),
    user: dict[str, Any] = Depends(_auth.current_user_optional),
    tag: Optional[int] = Query(None, description="Filter by Paperless tag id"),
    correspondent: Optional[int] = Query(None, description="Filter by Paperless correspondent id"),
    document_type: Optional[int] = Query(None, description="Filter by Paperless document_type id"),
    year: Optional[int] = Query(None, description="Filter by created year (YYYY)"),
    page: int = Query(1, ge=1, description="1-based page index"),
    page_size: int = Query(50, ge=1, le=500, description="Max docs per page"),
) -> Dict[str, Any]:
    """Merged document list: Yorik-native uploads + Paperless live
    catalogue. Paperless docs come back with `source='paperless'` and
    a NEGATIVE id (= -paperless_id) so they don't collide with local
    ids in the frontend's React key + so the preview/download routes
    can branch on the sign. Frontend uses Math.abs() to recover the
    real paperless id when calling /paperless/* URLs.

    Paperless docs are fetched using the calling user's own Paperless
    token (provisioned at onboarding) so Paperless's per-user permission
    filtering enforces who can see what — owner sees own docs, anyone
    with a tag-granted permission sees those docs too, admin sees all.
    Falls back to admin token only when the user hasn't been provisioned
    yet (legacy accounts, or first-load before onboarding).
    """
    normalize_role(role)
    # When the user is browsing a Paperless facet OR on page > 1, suppress
    # the Yorik-native uploads — they don't carry Paperless metadata
    # (would mix in confusingly when filtered) and are page-1-only by
    # convention (small set, no need to paginate alongside Paperless).
    is_filtered = any(x is not None for x in (tag, correspondent, document_type, year))
    # Local rows are populated by both (a) Yorik's upload write-through
    # and (b) the paperless_ingest mirror — i.e. they're always a copy
    # of what's in Paperless, never independent originals. Listing them
    # alongside the Paperless live results would double every doc in
    # the UI. We materialise them here for the fallback case below
    # (Paperless unreachable / no token) but suppress them in the
    # normal merge.
    local_mirror = [] if (is_filtered or page > 1) else [
        dict(d, source="local") for d in documents_mod.list_documents(role=role)
    ]
    local: list[dict] = []

    # Resolve which Paperless token to use for this request.
    user_id = user["id"] if user and user.get("id") else None
    used_admin_token = False
    pl_token: Optional[str] = None
    if user_id is not None:
        try:
            from . import external_users
            creds = external_users.get_user_paperless_creds(user_id)
            if creds and creds.get("api_key"):
                pl_token = creds["api_key"]
        except Exception:  # noqa: BLE001
            pass
    if pl_token is None:
        # Admin fallback only — gives the legacy admin-sees-all behaviour.
        if normalize_role(role) == "admin":
            from .connectors.paperless import _settings as _ps
            pl_token = (_ps().get("api_key"))
            used_admin_token = True

    if not pl_token:
        # No Paperless reachable — surface the local mirror as a
        # fallback so the user at least sees their uploads.
        return {
            "results":   local_mirror,
            "page":      page,
            "page_size": page_size,
            "total":     len(local_mirror),
            "has_next":  False,
            "has_prev":  page > 1,
        }

    # Pull live from Paperless using the resolved token. Paperless's
    # /api/documents/ returns ONLY docs the token's user can see (owner
    # + permission grants); admin-token gets everything.
    paperless: list[dict] = []
    pp_total = 0
    pp_has_next = False
    try:
        from .connectors.paperless import _settings as _ps
        from . import paperless_visibility as _pv
        base = (_ps().get("base_url") or "http://localhost:8010").rstrip("/")
        headers = {"Authorization": f"Token {pl_token}"}
        pp_params: Dict[str, Any] = {
            "ordering": "-created", "page_size": page_size, "page": page,
        }
        if tag is not None:           pp_params["tags__id__in"] = tag
        if correspondent is not None: pp_params["correspondent__id__in"] = correspondent
        if document_type is not None: pp_params["document_type__id__in"] = document_type
        if year is not None:          pp_params["created__year"] = year
        r = requests.get(
            f"{base}/api/documents/",
            headers=headers,
            params=pp_params,
            timeout=15,
        )
        if r.ok:
            body = r.json() or {}
            pp_total = int(body.get("count") or 0)
            pp_has_next = bool(body.get("next"))
            for d in body.get("results") or []:
                tag_ids = d.get("tags") or []
                paperless.append({
                    "id":            -int(d["id"]),
                    "title":         d.get("title") or f"Document {d['id']}",
                    "mime_type":     d.get("mime_type") or "application/pdf",
                    "bytes":         0,
                    "tags":          tag_ids,
                    "allowed_roles": "admin",
                    "chunk_count":   0,
                    "created_at":    d.get("created") or "",
                    "indexed_at":    None,
                    "source":        "paperless",
                    "owner":         d.get("owner"),
                    "visibility":    _pv.visibility_of(tag_ids),
                    # Flag the difference between "you own this" vs
                    # "this is shared with you" so the UI can dim
                    # other-people's docs.
                    "via_admin_token": used_admin_token,
                })
    except Exception:  # noqa: BLE001
        pass

    # Paperless live is the source of truth; the local table is just a
    # mirror. Show local rows only when Paperless came back empty (e.g.
    # transient fetch failure caught by the try-except above) so the
    # user still sees something rather than a blanked-out list.
    if not paperless and local_mirror:
        local = local_mirror
    merged = local + paperless
    merged.sort(key=lambda d: (d.get("created_at") or "", d.get("id", 0)), reverse=True)
    return {
        "results":   merged,
        "page":      page,
        "page_size": page_size,
        "total":     len(local) + pp_total,
        "has_next":  pp_has_next,
        "has_prev":  page > 1,
    }


class _VisibilityIn(BaseModel):
    visibility: str  # 'private' | 'business' | 'shared'


@app.post("/api/documents/-{paperless_doc_id}/visibility")
def change_document_visibility_route(
    paperless_doc_id: int,
    body: _VisibilityIn,
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Change a Paperless document's visibility (private / business /
    shared). The negative-id convention matches /api/documents — the
    URL takes the Paperless doc id directly (positive), but lives under
    /api/documents/- to mirror the negative-id frontend keys.

    Only the document owner OR admin may change visibility — checked
    against Paperless's GET /api/documents/{id}/.
    """
    from . import paperless_visibility as _pv
    from .connectors.paperless import _settings as _ps
    s = _ps()
    if not s.get("api_key"):
        raise HTTPException(503, "Paperless not configured")
    base = (s.get("base_url") or "http://localhost:8010").rstrip("/")

    # Resolve the calling user's Paperless uid for the owner check.
    me_paperless_uid: Optional[int] = None
    try:
        from . import external_users
        creds = external_users.get_user_paperless_creds(user["id"])
        if creds:
            me_paperless_uid = creds.get("paperless_user_id")
    except Exception:
        pass

    # Owner check via Paperless GET (admin token; cheap and authoritative).
    try:
        r = requests.get(
            f"{base}/api/documents/{paperless_doc_id}/",
            headers={"Authorization": f"Token {s['api_key']}"},
            timeout=10,
        )
        if not r.ok:
            raise HTTPException(404, "document not found")
        owner = r.json().get("owner")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"Paperless lookup failed: {exc}")
    is_admin = (user.get("role") == "admin")
    if not is_admin and (me_paperless_uid is None or owner != me_paperless_uid):
        raise HTTPException(403, "only the owner or admin may change visibility")

    res = _pv.change_document_visibility(int(paperless_doc_id), body.visibility)
    if not res.get("ok"):
        raise HTTPException(502, res.get("error") or "visibility update failed")
    return res


@app.post("/api/documents/upload", status_code=201)
async def upload_document(
    file: UploadFile = File(...),
    title: Optional[str] = Query(None),
    tags: str = Query("", description="Comma-separated"),
    allowed_roles: str = Query("admin"),
    visibility: Optional[str] = Query(None, description="private | business | shared. Omit to use the user's default."),
    space: Optional[str] = Query(None, description="Phase B: pin the doc to a Yorik space (slug like 'household' or numeric id). When set, Paperless view/change groups are restricted to that space's group (shared) or the user as owner (personal). Eventually-consistent: takes effect when the Paperless post-consume webhook fires."),
    role: str = Depends(_auth.current_role),
    user: dict[str, Any] = Depends(_auth.current_user_optional),
) -> Dict[str, Any]:
    """Upload + auto-index. Any logged-in user can upload — Paperless
    attributes ownership to the calling user (via their per-user token)
    and the visibility tag controls who else can see it.

    Write-through: the file lands in Yorik's local store (which drives
    the in-app preview + vector search) AND gets posted to Paperless
    via the user's own token, so Paperless's ownership + permission
    model kicks in naturally. The Paperless POST_CONSUME webhook then
    re-fires Yorik's ingest path, which is idempotent at the chunk
    level so the local index doesn't get duplicated.

    Visibility tag is auto-applied to enforce private/business/shared
    separation (see backend/paperless_visibility.py). Default visibility
    comes from the user's profile (`default_doc_visibility`) and can
    be overridden per-upload via the query param.

    Paperless write-through is best-effort — if Paperless is down or
    unconfigured, the local upload still succeeds and `paperless_task_id`
    comes back null in the response. The frontend can surface a soft
    warning if needed.
    """
    # No more admin gate — any logged-in user can upload their own docs.
    # Role check still applies to the local `documents` table via
    # `allowed_roles`, but that's a separate concern from Paperless.

    # ── Filename sanitation: strip ANY path components from the client-
    # supplied filename before we touch the filesystem. "../etc/passwd"
    # or "C:\\...\\evil.exe" should reduce to "passwd" / "evil.exe".
    # We only use the suffix on disk; the original name is logged.
    raw_name = file.filename or "upload.bin"
    safe_name = Path(raw_name).name  # strips any directory components
    suffix = Path(safe_name).suffix
    # Belt + braces: reject suffix containing path separators or null.
    if any(ch in suffix for ch in ("/", "\\", "\x00")):
        raise HTTPException(status_code=400, detail="filename contains invalid characters")

    # ── MIME allowlist: refuse anything we can't index. Browser-supplied
    # content_type is informational; we also re-check by extension below.
    supplied_mime = (file.content_type or "").lower().strip()
    if supplied_mime and supplied_mime not in documents_mod.SUPPORTED_MIME:
        raise HTTPException(
            status_code=415,
            detail=f"unsupported file type '{supplied_mime}' — allowed: {sorted(documents_mod.SUPPORTED_MIME)}",
        )

    # ── Size cap: read in chunks against a hard ceiling so a 10GB upload
    # is rejected before it hits memory or disk. Default 50MB,
    # configurable via env. UploadFile streams from disk-backed
    # SpooledTemporaryFile so this read loop is bounded.
    max_bytes = int(os.getenv("YORIK_MAX_UPLOAD_MB", "50")) * 1024 * 1024
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"file exceeds {max_bytes // (1024*1024)}MB limit (got {total // (1024*1024)}MB)",
            )
        chunks.append(chunk)
    raw_bytes = b"".join(chunks)
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(raw_bytes)
        tmp_path = Path(tmp.name)
    try:
        # Use the SANITIZED filename for the title fallback (never the
        # raw client-supplied name — that may contain path components).
        effective_title = title or Path(safe_name).stem
        meta = documents_mod.add_document(
            title=effective_title,
            src_path=tmp_path,
            mime_type=supplied_mime or None,
            tags=[t.strip() for t in tags.split(",") if t.strip()] or None,
            allowed_roles=allowed_roles,
            owner_user_id=(user.get("id") if user else None),
        )
        try:
            idx_result = documents_mod.index_document(meta["id"])
        except Exception:
            # Indexing crashed (text extraction, embedder, or a Postgres-
            # vs-SQLite path bug). Don't leave a zombie row behind — those
            # poison read_document and the doc-list view forever.
            try:
                documents_mod.delete_document(meta["id"])
            except Exception:
                pass
            raise
        meta.update({"index_result": idx_result})

        # Write-through to Paperless. Best-effort, fully isolated try.
        # Resolve the effective visibility (Phase 12.1):
        #   1. explicit query-param wins
        #   2. user_profiles.default_doc_visibility (per-user)
        #   3. household_settings.documents_default_visibility (per-tenant)
        #   4. 'private' hardcoded fallback
        effective_visibility = (visibility or "").strip().lower()
        if effective_visibility not in ("private", "business", "shared"):
            with conn_ctx(DB_PATH) as conn:
                row = conn.execute(
                    "SELECT default_doc_visibility FROM user_profiles WHERE id=?",
                    (user.get("id") if user else None,),
                ).fetchone() if user else None
            per_user = (row["default_doc_visibility"] if row else None) or ""
            if per_user in ("private", "business", "shared"):
                effective_visibility = per_user
            else:
                from .household_settings import get_setting
                tenant_default = get_setting(
                    "documents_default_visibility", default="private"
                )
                effective_visibility = (
                    tenant_default if tenant_default in ("private", "business", "shared")
                    else "private"
                )
        # Phase B: resolve `space` param to a space_id. Pinning happens
        # later in paperless_ingest.ingest_one (post-consume webhook)
        # via the yorik-space-<id> tag we attach here.
        space_id_for_paperless: Optional[int] = None
        if space:
            sval = space.strip().lower()
            with conn_ctx(DB_PATH) as conn:
                if sval.isdigit():
                    r = conn.execute("SELECT id FROM spaces WHERE id=?", (int(sval),)).fetchone()
                else:
                    r = conn.execute("SELECT id FROM spaces WHERE LOWER(slug)=?", (sval,)).fetchone()
            if r is None:
                raise HTTPException(400, detail=f"unknown space {space!r}")
            space_id_for_paperless = int(r["id"])

        extra_tags = [t.strip() for t in tags.split(",") if t.strip()]
        if space_id_for_paperless is not None:
            extra_tags.append(f"yorik-space-{space_id_for_paperless}")

        meta["paperless"] = _push_to_paperless(
            raw_bytes,
            filename=safe_name,
            title=effective_title,
            mime_type=supplied_mime or None,
            tags=extra_tags,
            user_id=user["id"] if user and user.get("id") else None,
            visibility=effective_visibility,
        )
        meta["visibility"] = effective_visibility
        if space_id_for_paperless is not None:
            meta["space_id"] = space_id_for_paperless
        return meta
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


def _push_to_paperless(
    pdf_or_doc_bytes: bytes,
    *,
    filename: str,
    title: str,
    mime_type: Optional[str],
    tags: List[str],
    user_id: Optional[int] = None,
    visibility: str = "private",
) -> Dict[str, Any]:
    """Forward an uploaded doc to Paperless. Uses the calling user's
    per-user token if they have one (so Paperless attributes ownership
    correctly + applies their per-user permission scope), otherwise
    falls back to the admin token. Auto-attaches the visibility tag
    (private = no tag, business/shared = matching tag) so cross-user
    sharing follows the tag-based separation model.

    Returns {ok, task_id?, error?, skipped?, visibility, owner_user_id?}.
    NEVER raises — Yorik's local upload must not fail because Paperless
    is offline."""
    try:
        from .connectors.paperless import _settings as _paperless_settings
        s = _paperless_settings()
        admin_key = s.get("api_key")
        if not admin_key:
            return {"ok": False, "skipped": True, "reason": "Paperless not configured (no admin token)"}
        base_url = (s.get("base_url") or "http://localhost:8010").rstrip("/")

        # Prefer the calling user's own token — Paperless then attributes
        # the doc's `owner` to them, and per-user permission filtering
        # on /api/documents/ Just Works for everyone else. Fall back to
        # admin if the user hasn't been provisioned yet.
        used_token = admin_key
        token_owner = "admin"
        if user_id is not None:
            try:
                from . import external_users
                creds = external_users.get_user_paperless_creds(user_id)
                if creds and creds.get("api_key"):
                    used_token = creds["api_key"]
                    token_owner = f"user:{user_id}"
            except Exception as exc:  # noqa: BLE001
                log.debug("user paperless creds lookup failed (uid=%s): %s", user_id, exc)

        headers = {"Authorization": f"Token {used_token}"}
        files = {"document": (filename, pdf_or_doc_bytes, mime_type or "application/octet-stream")}
        data: Dict[str, Any] = {"title": title}
        # Apply the visibility tag (no-op for 'private' — Paperless's
        # owner-only enforcement does the work). Done BEFORE tag-name
        # resolution below so the tag ids merge cleanly.
        from . import paperless_visibility as _pv
        _pv.apply_visibility_to_payload(visibility, data)
        if tags:
            from .compose.save import _ensure_tag_ids
            tag_ids = _ensure_tag_ids(s, tags)
            for tid in tag_ids:
                data.setdefault("tags", []).append(tid)
        r = requests.post(
            f"{base_url}/api/documents/post_document/",
            headers=headers, files=files, data=data, timeout=15,
        )
        if not r.ok:
            return {"ok": False, "error": f"paperless HTTP {r.status_code}: {r.text[:150]}"}
        return {
            "ok": True,
            "task_id": r.text.strip().strip('"'),
            "token_used": token_owner,
            "visibility": visibility,
        }
    except Exception as exc:  # noqa: BLE001
        log = logging.getLogger("homeos.docs.upload")
        log.exception("paperless write-through failed: %s", exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@app.get("/api/documents/facets")
def documents_facets(
    role: str = Depends(_auth.current_role),
    user: dict[str, Any] = Depends(_auth.current_user_optional),
) -> Dict[str, Any]:
    """Faceted browse of the Paperless corpus — tags / correspondents /
    document-types / years, each with document counts. Powers the
    folder-tree sidebar in /documents so users can browse the whole
    library (not just the most-recent 50 that the flat list returns).

    MUST stay declared BEFORE /api/documents/{doc_id} — FastAPI matches
    routes in declaration order, and the dynamic route would otherwise
    try to parse "facets" as an int and 422.

    Uses the calling user's Paperless token (per-user wave 3) so the
    counts reflect what THAT user can actually see, falling back to the
    admin token for legacy installs.
    """
    normalize_role(role)
    user_id = user["id"] if user and user.get("id") else None
    pl_token: Optional[str] = None
    if user_id is not None:
        try:
            from . import external_users
            creds = external_users.get_user_paperless_creds(user_id)
            if creds and creds.get("api_key"):
                pl_token = creds["api_key"]
        except Exception:  # noqa: BLE001
            pass
    if pl_token is None and normalize_role(role) == "admin":
        from .connectors.paperless import _settings as _ps
        pl_token = (_ps().get("api_key"))
    if not pl_token:
        return {"tags": [], "correspondents": [], "document_types": [], "years": []}

    from .connectors.paperless import _settings as _ps
    base = (_ps().get("base_url") or "http://localhost:8010").rstrip("/")
    headers = {"Authorization": f"Token {pl_token}", "Accept": "application/json"}

    def _list_facet(path: str, page_size: int = 500) -> list[dict]:
        """Fetch a Paperless facet endpoint (/api/tags/ etc). Each
        result row carries `document_count` natively. We skip the
        `ordering=-document_count` Paperless supports because some
        Paperless versions 400 on it — sort client-side instead."""
        try:
            r = requests.get(
                f"{base}{path}",
                headers=headers,
                params={"page_size": page_size},
                timeout=10,
            )
            if not r.ok:
                log.warning("documents_facets %s returned %s: %s", path, r.status_code, r.text[:200])
                return []
            return (r.json() or {}).get("results", []) or []
        except requests.RequestException as exc:
            log.warning("documents_facets %s failed: %s", path, exc)
            return []

    def _shape(rows: list[dict], extra: dict | None = None) -> list[dict]:
        out = []
        for x in rows:
            cnt = int(x.get("document_count") or 0)
            if cnt == 0:
                continue  # hide empty facets — clutter, no docs to click into
            shaped = {
                "id":            x.get("id"),
                "name":          x.get("name") or "",
                "slug":          x.get("slug") or "",
                "document_count": cnt,
            }
            if extra:
                for k in extra:
                    shaped[k] = x.get(k)
            out.append(shaped)
        # Highest count first — better UX than alphabetical for "browse what I have"
        out.sort(key=lambda r: -r["document_count"])
        return out

    # Wrap the whole faceting in a try/except — a 500 here blanks the
    # entire Documents app (the sidebar AND the list call gated on a
    # successful facets render). User saw exactly that on the notebook
    # install: 600 docs synced fine, sidebar's facet call 500'd, no
    # docs shown. Each individual fetcher already catches network
    # errors; this catch covers everything else (Paperless API shape
    # drift, unexpected `None`s, etc.) so the app keeps rendering even
    # when facets degrade.
    try:
        tags           = _shape(_list_facet("/api/tags/"),            extra={"color": None})
        correspondents = _shape(_list_facet("/api/correspondents/"))
        document_types = _shape(_list_facet("/api/document_types/"))

        # Years: walk the corpus's `created` field. Paginate a few pages so
        # corpora up to ~5000 docs get exact counts; bigger ones get
        # undercounted on the oldest years (acceptable for a browse UI).
        years_count: Dict[int, int] = {}
        try:
            url: Optional[str] = f"{base}/api/documents/"
            params: Dict[str, Any] = {
                "page_size": 200, "ordering": "-created", "fields": "id,created",
            }
            pages_walked = 0
            while url and pages_walked < 25:  # cap at 5000 docs
                r = requests.get(url, headers=headers, params=params, timeout=15)
                if not r.ok:
                    break
                body = r.json() or {}
                for d in body.get("results") or []:
                    c = d.get("created") or d.get("created_date") or ""
                    if len(c) >= 4 and c[:4].isdigit():
                        yr = int(c[:4])
                        years_count[yr] = years_count.get(yr, 0) + 1
                url = body.get("next")
                params = {}
                pages_walked += 1
        except requests.RequestException as exc:
            log.warning("documents_facets years pass failed: %s", exc)
        years = [{"year": y, "document_count": n}
                 for y, n in sorted(years_count.items(), reverse=True)]

        return {
            "tags":           tags,
            "correspondents": correspondents,
            "document_types": document_types,
            "years":          years,
        }
    except Exception as exc:  # noqa: BLE001
        # Surface the failure via logs + a typed error string in the
        # response, but keep the shape stable + the HTTP 200 so the
        # Documents app still renders and lists docs.
        log.exception("documents_facets failed completely: %s", exc)
        return {
            "tags": [], "correspondents": [], "document_types": [], "years": [],
            "error": f"facets unavailable ({type(exc).__name__}: {str(exc)[:160]})",
        }


@app.get("/api/documents/{doc_id}")
def get_document_endpoint(
    doc_id: int,
    role: str = Depends(_auth.current_role),
    user: dict[str, Any] = Depends(_auth.current_user_optional),
) -> Dict[str, Any]:
    """Single-document metadata. Used by the Chat app's document cards
    AND by /documents when the user clicks a search result whose doc
    isn't in the current 50-row list. Handles both native (positive id)
    and Paperless-mirror (negative id, abs() = real Paperless id) docs.
    """
    normalize_role(role)
    # Negative id = Paperless-mirror convention. Fetch live from
    # Paperless using the calling user's token (per-user wave 3) so
    # ACLs filter what's visible the same way the flat list does.
    if doc_id < 0:
        paperless_id = abs(doc_id)
        user_id = user["id"] if user and user.get("id") else None
        pl_token: Optional[str] = None
        if user_id is not None:
            try:
                from . import external_users
                creds = external_users.get_user_paperless_creds(user_id)
                if creds and creds.get("api_key"):
                    pl_token = creds["api_key"]
            except Exception:  # noqa: BLE001
                pass
        if pl_token is None and normalize_role(role) == "admin":
            from .connectors.paperless import _settings as _ps
            pl_token = (_ps().get("api_key"))
        if not pl_token:
            raise HTTPException(status_code=404, detail=f"paperless doc {paperless_id} not visible to this user")
        from .connectors.paperless import _settings as _ps
        from . import paperless_visibility as _pv
        base = (_ps().get("base_url") or "http://localhost:8010").rstrip("/")
        try:
            r = requests.get(
                f"{base}/api/documents/{paperless_id}/",
                headers={"Authorization": f"Token {pl_token}", "Accept": "application/json"},
                timeout=15,
            )
            if r.status_code == 404:
                raise HTTPException(status_code=404, detail=f"paperless doc {paperless_id} not found")
            r.raise_for_status()
            d = r.json() or {}
        except requests.RequestException as exc:
            raise HTTPException(status_code=502, detail=f"paperless fetch failed: {exc}")
        tag_ids = d.get("tags") or []
        return {
            "id":            -int(d["id"]),
            "title":         d.get("title") or f"Document {d['id']}",
            "mime_type":     d.get("mime_type") or "application/pdf",
            "bytes":         0,
            "tags":          tag_ids,
            "allowed_roles": "admin",
            "chunk_count":   0,
            "created_at":    d.get("created") or "",
            "indexed_at":    None,
            "source":        "paperless",
            "owner":         d.get("owner"),
            "visibility":    _pv.visibility_of(tag_ids),
        }
    # Local doc — original code path.
    doc = documents_mod.get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail=f"document {doc_id} not found")
    if role not in ("platform_admin", "admin"):
        allowed = [r.strip() for r in (doc.get("allowed_roles") or "").split(",")]
        if role not in allowed:
            raise HTTPException(status_code=403, detail="not visible to this role")
    # Don't leak the on-disk path — the file is reached via /raw.
    doc.pop("path", None)
    return doc


@app.get("/api/documents/{doc_id}/raw")
def get_document_raw(
    doc_id: int,
    role: str = Depends(_auth.current_role),
    download: int = Query(0, description="1 = force download, 0 = inline preview"),
) -> FileResponse:
    """Stream the original file bytes. `download=1` triggers a save dialog;
    otherwise the browser renders inline (PDFs, images, text)."""
    normalize_role(role)
    doc = documents_mod.get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail=f"document {doc_id} not found")
    if role not in ("platform_admin", "admin"):
        allowed = [r.strip() for r in (doc.get("allowed_roles") or "").split(",")]
        if role not in allowed:
            raise HTTPException(status_code=403, detail="not visible to this role")
    path = Path(doc.get("path") or "")
    if not path.exists():
        raise HTTPException(status_code=410, detail="file missing on disk")
    headers: Dict[str, str] = {}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{path.name}"'
    return FileResponse(
        path,
        media_type=doc.get("mime_type") or "application/octet-stream",
        filename=path.name if download else None,
        headers=headers,
    )


@app.delete("/api/documents/{doc_id}", status_code=204, response_class=Response)
def delete_document_endpoint(doc_id: int, role: str = Depends(_auth.current_role)) -> Response:
    if normalize_role(role) not in ("admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="role required: admin")
    if not documents_mod.delete_document(doc_id):
        raise HTTPException(status_code=404, detail=f"document {doc_id} not found")
    return Response(status_code=204)


@app.post("/api/documents/{doc_id}/reindex")
def reindex_document_endpoint(doc_id: int, role: str = Depends(_auth.current_role)) -> Dict[str, Any]:
    if normalize_role(role) not in ("admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="role required: admin")
    result = documents_mod.index_document(doc_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "indexing failed"))
    return result


def _paperless_token_probe(base_url: str, token: str) -> int:
    """One-shot HTTP probe against the Paperless API. Returns the HTTP
    status code (0 on network error). Used by the sync endpoint to
    decide whether a stored token is still alive before reconciling."""
    import requests
    try:
        r = requests.get(
            f"{base_url.rstrip('/')}/api/documents/?page_size=1",
            headers={"Authorization": f"Token {token}", "Accept": "application/json"},
            timeout=5,
        )
        return r.status_code
    except requests.RequestException:
        return 0


def _paperless_grab_bootstrap_token() -> Optional[str]:
    """Mint a fresh API token for the Paperless bootstrap admin via
    docker exec — same path start.sh's first-run block uses. Used as
    a self-heal when the stored token is missing/dead. Returns the
    new token string, or None if the docker exec didn't produce one
    (BYO Paperless, container missing, postgres still booting, etc.)."""
    import subprocess
    admin_user = os.getenv("PAPERLESS_ADMIN_USER", "admin")
    script = (
        "from django.contrib.auth import get_user_model\n"
        "from rest_framework.authtoken.models import Token\n"
        f"u = get_user_model().objects.get(username='{admin_user}')\n"
        "t, _ = Token.objects.get_or_create(user=u)\n"
        "print(t.key)\n"
    )
    try:
        out = subprocess.run(
            ["docker", "exec", "yorik-paperless-web",
             "python", "manage.py", "shell", "-c", script],
            capture_output=True, text=True, timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    # Token = last whitespace-stripped line; Django shell prints
    # warnings + the token together, the value is always last.
    token = (out.stdout or "").strip().splitlines()[-1].strip() if out.stdout else ""
    return token if (token and len(token) == 40 and token.isalnum()) else None


def _paperless_store_token(token: str) -> None:
    """Persist a freshly-minted token into app_settings so subsequent
    _paperless_settings() calls pick it up."""
    with conn_ctx(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES ('paperless_api_token', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = datetime('now')",
            (token,),
        )
        conn.execute(
            "INSERT INTO app_settings (key, value) "
            "VALUES ('paperless_base_url', 'http://localhost:8010') "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = datetime('now')",
        )


@app.post("/api/documents/sync-paperless")
def sync_paperless_now(role: str = Depends(_auth.current_role)) -> Dict[str, Any]:
    """Force an immediate Paperless reconcile so docs that landed in the
    consume folder (or were uploaded via the Paperless UI directly) show
    up in Yorik's Documents app right away — rather than waiting up to
    six hours for the scheduled reconciler tick.

    Self-heals the stored token: if it's missing or HTTP-401s against
    Paperless (Paperless got rebuilt, postgres lost the user row, etc.),
    re-mint via docker exec on the bundled container, store, and retry.
    Means the user doesn't have to know about the install-time token-
    grab failure mode — clicking Sync just works."""
    if normalize_role(role) not in ("admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="role required: admin")
    from . import paperless_ingest

    healed = False

    def _try_heal(reason: str) -> Optional[str]:
        """Attempt one self-heal. Returns the new token or None.
        Logs the reason so we can see why it kicked in."""
        nonlocal healed
        if healed:
            return None  # only ever heal once per call
        healed = True
        logging.getLogger("yorik.paperless").info(
            "sync-paperless: attempting token self-heal (reason: %s)", reason,
        )
        return _paperless_grab_bootstrap_token()

    # ── Step 1: ensure we have *a* token at all.
    settings = paperless_ingest._paperless_settings()
    if not settings.get("api_key"):
        new_token = _try_heal("no token stored")
        if new_token:
            _paperless_store_token(new_token)
            settings = paperless_ingest._paperless_settings()
        else:
            return {
                "ok": False,
                "error": (
                    "No Paperless API token configured and auto-heal failed "
                    "(bundled Paperless container not running, or BYO setup). "
                    "Open Settings → Connectors → Paperless and paste a token."
                ),
                "checked": 0, "missing": 0, "ingested": 0, "skipped": 0, "failed": 0,
            }

    # ── Step 2: validate the token actually works before we trust it.
    probe = _paperless_token_probe(settings["base_url"], settings["api_key"])
    if probe == 401:
        new_token = _try_heal("stored token returned 401")
        if new_token:
            _paperless_store_token(new_token)
            settings = paperless_ingest._paperless_settings()
            # Re-probe the new one so we don't quietly retry a broken
            # heal — if THIS also 401s the install is fundamentally
            # broken, not a token-staleness problem.
            probe = _paperless_token_probe(settings["base_url"], settings["api_key"])
        if probe == 401:
            return {
                "ok": False,
                "error": (
                    "Paperless rejected the stored token (HTTP 401) and "
                    "auto-heal couldn't mint a replacement. Check that "
                    "yorik-paperless-web is healthy and the admin user "
                    "exists; otherwise paste a token manually under "
                    "Settings → Connectors → Paperless."
                ),
                "checked": 0, "missing": 0, "ingested": 0, "skipped": 0, "failed": 0,
            }

    # ── Step 3: the real reconcile.
    try:
        result = paperless_ingest.reconcile_once()
        result.setdefault("ok", "error" not in result and result.get("checked", 0) > 0)
        if healed:
            result["token_self_healed"] = True
        return result
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Paperless sync failed: {exc}")


class SearchDocumentsIn(BaseModel):
    query: str
    k: int = Field(default=5, ge=1, le=20)


@app.post("/api/documents/search")
def search_documents_endpoint(body: SearchDocumentsIn, role: str = Depends(_auth.current_role)) -> Dict[str, Any]:
    """Direct document search for the /documents UI's query field.

    Searches BOTH Yorik-native uploads (vec_chunks) AND Paperless
    (hybrid: semantic + FTS via RRF — same fusion the LLM paths use).
    Paperless rows are normalized to the DocumentSearchHit shape with
    the negative-id convention so the UI can route preview/download
    through the Paperless reverse proxy.

    Returns {hits: [...], legs: {...}} so the UI can show which engines
    fired (semantic / keyword) and why one is down when only one runs.
    """
    normalize_role(role)
    from . import paperless_ingest as _pp

    native = documents_mod.search(body.query, k=body.k, role=role)

    pp_result: Dict[str, Any]
    try:
        pp_result = _pp.search_hybrid(body.query, k=body.k)
    except Exception as exc:  # noqa: BLE001
        log.exception("paperless hybrid search failed in /api/documents/search: %s", exc)
        pp_result = {
            "hits": [],
            "legs": {
                "semantic": {"count": 0, "error": f"Hybrid search crashed: {exc}", "vec_count": 0},
                "fts":      {"count": 0, "error": f"Hybrid search crashed: {exc}"},
            },
        }

    pp_raw = pp_result.get("hits", [])

    # Normalize Paperless rows → DocumentSearchHit shape. Negative id
    # convention matches /api/documents (the flat list) so the UI's
    # preview/download routing branches consistently on the sign.
    pp = [{
        "chunk_id":    0,
        "doc_id":      -int(r["paperless_doc_id"]),
        "doc_title":   r.get("doc_title") or f"Document {r.get('paperless_doc_id')}",
        "doc_mime":    "application/pdf",
        "chunk_index": int(r.get("chunk_index") or 0),
        "chunk_text":  r.get("text") or "",
        "char_start":  0,
        "char_end":    0,
        "distance":    float(r.get("distance") or 0.0),
        "match_type":  r.get("match_type") or "paperless",
    } for r in pp_raw if r.get("paperless_doc_id") is not None]

    # Rank-fuse the two lists with RRF. Native (cosine distance) and
    # Paperless (already-RRF-fused hybrid + title-boost) live on
    # incompatible score scales, so we re-score each hit by its rank in
    # its own source list — score = 1/(60 + rank + 1). On ties, the
    # Paperless leg wins because we feed it into the sort first and
    # Python's sort is stable. That matches the product reality:
    # Paperless is the document-of-record, native upload is an extra.
    RRF_K = 60
    def _rrf(rank: int) -> float:
        return 1.0 / (RRF_K + rank + 1)
    fused = (
        [(_rrf(r), h) for r, h in enumerate(pp)]
        + [(_rrf(r), h) for r, h in enumerate(native)]
    )
    fused.sort(key=lambda x: -x[0])
    # Normalize the raw RRF scores to 0–1 so the UI can show a single
    # comparable "match" badge. Native cosine distance and Paperless
    # RRF live on different scales; this is the only number on every
    # hit that actually reflects the order the list was sorted by.
    max_rrf = fused[0][0] if fused else 0.0
    hits: List[Dict[str, Any]] = []
    for score, h in fused:
        h["match_score"] = round(score / max_rrf, 4) if max_rrf > 0 else 0.0
        hits.append(h)

    return {
        "hits": hits,
        "legs": pp_result.get("legs", {}),
        "native_count": len(native),
    }


@app.get("/api/marketplace/layouts")
def list_marketplace_layouts() -> List[Dict[str, Any]]:
    """Return the calendar layout catalogue.

    MVP stub: returns the hardcoded bundled layouts. In v2 this will fetch from
    a remote community marketplace and support installing new layouts.
    """
    return LAYOUT_CATALOGUE


# ---------------------------------------------------------------------------
# Voice profiles + TTS
# ---------------------------------------------------------------------------

import secrets
import threading

DEFAULT_LANGUAGE = os.getenv("HOMEOS_DEFAULT_LANGUAGE", "en")

# tts_audio_store keeps generated WAVs alive long enough for the browser to
# fetch them via /api/tts-audio/{token}. Token is one-shot and short-TTL.
_tts_store: Dict[str, bytes] = {}
_tts_lock = threading.Lock()


def _stash_tts(wav_bytes: bytes) -> str:
    token = secrets.token_urlsafe(18)
    with _tts_lock:
        _tts_store[token] = wav_bytes
        # Trim if too many — keep last 20.
        if len(_tts_store) > 20:
            for old in list(_tts_store)[:-20]:
                _tts_store.pop(old, None)
    return token


def _humanize_for_tts(text: str, language: str) -> str:
    """Rewrite digit-only times AND dates into spoken-language form for TTS.

    German only for now — "18:00" / "01.06.2026" read stilted, "sechs Uhr
    abends" / "am ersten Juni zweitausendsechsundzwanzig" don't (and
    "01.06.2026" collapses to "morgen" or "am Montag" when in window).
    Other languages pass through unchanged. The chat-page response text
    is NOT touched (this is the TTS-only path).
    """
    if not text or (language or "").lower() != "de":
        return text
    try:
        from .agent.humanize import humanize_times_de, humanize_dates_de
        # Dates BEFORE times — a date pattern like "01.06." with a trailing
        # dot doesn't collide with HH:MM, but doing dates first protects
        # against any future overlap.
        out = humanize_dates_de(text)
        out = humanize_times_de(out)
        return out
    except Exception:  # noqa: BLE001
        # Never break TTS over a humanizer bug — fall back to raw text.
        logging.getLogger("homeos.tts").warning(
            "humanize_for_tts failed — using raw text", exc_info=True,
        )
        return text


@app.get("/api/tts-audio/{token}", response_class=Response)
def get_tts_audio(token: str) -> Response:
    with _tts_lock:
        wav = _tts_store.pop(token, None)
    if not wav:
        raise HTTPException(status_code=404, detail="audio expired or already consumed")
    return Response(content=wav, media_type="audio/wav")


class LanguagePatch(BaseModel):
    language: str


# ── app settings (admin-toggleable runtime flags) ──────────────────────────
# Tiny single-row-per-key store backing things like the voice_id toggle.
# Schema lives in database.py: app_settings(key TEXT PK, value TEXT, updated_at).

@app.get("/api/settings/{key}")
def get_setting(key: str) -> Dict[str, Any]:
    """Return the current value (str) of a setting, or null if unset."""
    with conn_ctx(DB_PATH) as conn:
        row = conn.execute("SELECT value, updated_at FROM app_settings WHERE key = ?", (key,)).fetchone()
    return {"key": key, "value": row["value"] if row else None, "updated_at": row["updated_at"] if row else None}


class SettingPut(BaseModel):
    value: str


@app.put("/api/settings/{key}")
def put_setting(key: str, body: SettingPut, role: str = Depends(_auth.current_role)) -> Dict[str, Any]:
    """Set a setting. Admin only — these are global, not per-user."""
    if normalize_role(role) not in ("admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="role required: admin")
    with conn_ctx(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = datetime('now')",
            (key, body.value),
        )
    return {"key": key, "value": body.value, "ok": True}


@app.get("/api/voice-profiles")
def list_voice_profiles() -> List[Dict[str, Any]]:
    """List user_profiles with their enrollment + language state."""
    with conn_ctx(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, name, email, role, language, "
            "(voice_embedding IS NOT NULL AND voice_embedding != '') AS enrolled "
            "FROM user_profiles ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/voice-profile/{profile_id}/enroll")
async def enroll_voice(
    profile_id: int,
    audio: UploadFile = File(...),
    user: Dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Compute and persist a SpeechBrain embedding for this profile.

    A user can enroll their OWN voice (profile_id == their user id).
    Admins can enroll anyone — useful when sitting down with a
    family member who can't / won't drive Settings themselves.
    Anyone else gets 403.
    """
    is_admin = normalize_role(user.get("role") or "") == "admin"
    is_self  = user.get("id") or 0 == int(profile_id)
    if not (is_admin or is_self):
        raise HTTPException(status_code=403, detail="can only enroll your own voice")
    suffix = Path(audio.filename or "voice.webm").suffix or ".webm"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await audio.read())
        tmp_path = tmp.name
    try:
        return voice_id.enroll(profile_id, tmp_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"enrollment failed: {exc}") from exc
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


@app.patch("/api/voice-profile/{profile_id}/language")
def set_profile_language(profile_id: int, body: LanguagePatch, role: str = Depends(_auth.current_role)) -> Dict[str, Any]:
    if normalize_role(role) not in ("admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="role required: admin")
    lang = (body.language or "en").lower().strip()
    if lang not in {"en", "de", "fr", "es", "it"}:
        raise HTTPException(status_code=400, detail=f"unsupported language '{lang}'")
    with conn_ctx(DB_PATH) as conn:
        if not conn.execute("SELECT 1 FROM user_profiles WHERE id = ?", (profile_id,)).fetchone():
            raise HTTPException(status_code=404, detail=f"profile id={profile_id} not found")
        conn.execute("UPDATE user_profiles SET language = ? WHERE id = ?", (lang, profile_id))
        row = conn.execute("SELECT id, name, language FROM user_profiles WHERE id = ?", (profile_id,)).fetchone()
    return dict(row)


@app.delete("/api/voice-profile/{profile_id}/enrollment", status_code=204, response_class=Response)
def clear_enrollment(
    profile_id: int,
    user: Dict[str, Any] = Depends(_auth.current_user),
) -> Response:
    is_admin = normalize_role(user.get("role") or "") == "admin"
    is_self  = user.get("id") or 0 == int(profile_id)
    if not (is_admin or is_self):
        raise HTTPException(status_code=403, detail="can only clear your own voice enrollment")
    with conn_ctx(DB_PATH) as conn:
        conn.execute("UPDATE user_profiles SET voice_embedding = NULL WHERE id = ?", (profile_id,))
    return Response(status_code=204)


@app.post("/api/voice/transcribe")
async def voice_transcribe(
    audio: UploadFile = File(...),
    user: Dict[str, Any] = Depends(_auth.current_user),
) -> Dict[str, Any]:
    """Transcribe-only endpoint. Same Whisper pipeline as /api/ask-voice
    but stops after the transcript — no LLM call, no TTS, no speaker
    identification. Used by inline UI controls (e.g. the "speak your
    instruction" mic in Compose's Ask Yorik panel) where the caller
    just wants text to drop into an input field.

    Returns {text, language}. Empty audio / failed transcription
    surfaces as a 400 with a remediation hint."""
    suffix = Path(audio.filename or "audio.webm").suffix or ".webm"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await audio.read())
        tmp_path = tmp.name
    try:
        from .voice import transcribe_detailed
        detail = await asyncio.to_thread(transcribe_detailed, tmp_path)
        text = (detail.get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="Empty transcript — please retry")
        return {"text": text, "language": detail.get("language") or ""}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            status_code=500,
            content={"error": f"Whisper transcription failed: {type(exc).__name__}: {exc}"},
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# Stop-phrases that end a hands-free voice conversation immediately.
# Lowercase, punctuation stripped. Kept narrow on purpose — "danke"
# / "thanks" are deliberately NOT here because they're ambiguous
# mid-conversation. The /api/ask-voice/stream handler matches against
# the normalised transcript and bails before the ack + LLM call.
_VOICE_STOP_PHRASES: frozenset[str] = frozenset({
    "stop", "stopp", "halt", "ende",
    "stop yorik", "stopp yorik", "halt yorik", "yorik stop",
    "yorik stopp", "yorik halt",
})


# Per-user "voice session" suffix on the conversation_id. Voice turns
# that come in close together share the same id (full short-term
# context), but a gap longer than VOICE_IDLE_BREAK_SECS forces a fresh
# suffix — preventing a stale "what's on my calendar?" → 15 minutes
# of silence → "ok do that" from carrying confusing leftover context.
# Browser /chat uses an explicit conversation_id from the URL and
# never goes through this helper, so /chat continuity is untouched.
VOICE_IDLE_BREAK_SECS = 60
_voice_last_at: Dict[int, float] = {}
_voice_window:  Dict[int, str]   = {}


def _voice_conv_id_with_idle_break(user_id: str) -> str:
    import time as _t
    base = vanna_agent.voice_conversation_id(user_id)
    now = _t.monotonic()
    last = _voice_last_at.get(user_id, 0.0)
    if (now - last) > VOICE_IDLE_BREAK_SECS or user_id not in _voice_window:
        # Wall-clock seconds — short, unique per break, sorts naturally.
        _voice_window[user_id] = str(int(_t.time()))
    _voice_last_at[user_id] = now
    return f"{base}:{_voice_window[user_id]}"


@app.post("/api/ask-voice")
async def ask_voice(
    audio: UploadFile = File(...),
    role: str = Depends(_auth.current_role),
    user: Dict[str, Any] = Depends(_auth.current_user),
    conversation_id: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """Whisper → optional speaker-ID → Vanna ask → optional Piper TTS.

    Identification is best-effort. If voice_id.identify returns None for any
    reason (no enrolled profiles, audio too short, torch error), we fall back
    to the `role` query param and the default language. The endpoint still
    transcribes, answers, and (if a Piper voice is configured) synthesizes.
    """
    normalize_role(role)
    suffix = Path(audio.filename or "audio.webm").suffix or ".webm"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await audio.read())
        tmp_path = tmp.name
    try:
        from .voice import transcribe_detailed
        detail = transcribe_detailed(tmp_path)
        transcript = detail["text"].strip()
        detected_language = detail["language"]  # 'en', 'de', etc.
        if not transcript:
            raise HTTPException(status_code=400, detail="Empty transcript — please retry")

        identified = voice_id.identify(tmp_path)  # never raises
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            status_code=500,
            content={"error": f"Whisper transcription failed: {type(exc).__name__}: {exc}"},
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    # Decide effective role + language.
    # Language priority: identified profile's preference > Whisper-detected > DEFAULT.
    # Rationale: an identified user has set their preference explicitly; otherwise
    # whoever is speaking should hear themselves answered in the language they
    # spoke in (a German visitor on a default-en box still gets German TTS).
    if identified:
        eff_role = identified["role"]
        eff_language = identified.get("language") or detected_language or DEFAULT_LANGUAGE
    else:
        eff_role = role
        eff_language = detected_language or DEFAULT_LANGUAGE

    # Default to the user's daily voice thread so back-references work
    # across calls ("undo that", "den Termin von heute morgen"). The client
    # can still pass an explicit conversation_id to override.
    eff_user_id = (identified or {}).get("id") or user.get("id")
    if not conversation_id and eff_user_id is not None:
        conversation_id = vanna_agent.voice_conversation_id(eff_user_id)

    # Short-circuit on LLM outage so the user doesn't wait 30s for a
    # connect timeout after Whisper already spent a few seconds.
    if not _llm_reachable():
        result = _llm_offline_response(transcript, conversation_id=conversation_id)
    else:
        result = await vanna_agent.ask_async(
            transcript,
            eff_role,
            conversation_id=conversation_id,
            user_language=eff_language,
            identified_name=(identified or {}).get("name"),
            user_id=((identified or {}).get("id") or user.get("id")),
            dev_mode=_user_dev_mode((identified or {}).get("id") or user.get("id")),
        )
    result["transcript"] = transcript
    result["identified"] = identified  # may be None
    result["language"] = eff_language
    result["effective_role"] = eff_role

    # Synthesize TTS audio (best-effort). Stash bytes, return one-shot URL.
    # Humanize times for German voice — "18:00" reads stilted out of any TTS;
    # "sechs Uhr abends" is what a person actually says. Chat-page response
    # text (in result['response']) stays unchanged so the on-screen "18:00"
    # remains clickable / sortable / copy-pasteable.
    tts_text = _humanize_for_tts(result.get("response") or "", eff_language)
    wav = tts_mod.synthesize(tts_text, eff_language)
    if wav:
        token = _stash_tts(wav)
        result["audio_url"] = f"/api/tts-audio/{token}"
    else:
        result["audio_url"] = None
    return result


# ── streaming voice ask ─────────────────────────────────────────────────────
# Same logic as /api/ask-voice but emits NDJSON events over a chunked response
# so the frontend can:
#   1. show the transcript immediately after Whisper finishes,
#   2. start playing the FIRST sentence as soon as its TTS completes (instead
#      of waiting for the whole response to be synthesized),
#   3. apply UI actions when the agent is fully done.
#
# Event shapes (one JSON object per line):
#   {"type":"transcript", "text":"…", "language":"de"}
#   {"type":"identification", "identified":{…}|null, "effective_role":"…", "language":"…"}
#   {"type":"audio", "url":"/api/tts-audio/<tok>", "text":"sentence", "index":0}
#   {"type":"done", "response":"…", "ui_actions":[…], "sql_used":"…", "conversation_id":"…"}
#   {"type":"error", "error":"…"}

_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[\.\?\!…])\s+|\n+")


def _split_sentences(text: str) -> List[str]:
    """Split a response into utterance-sized chunks for progressive TTS.

    Splits on .?!… followed by whitespace, or any newline. Empty/whitespace
    chunks are dropped. Single-word "Yes." is kept as-is — short first chunks
    are exactly what we want for time-to-first-audio.
    """
    if not text:
        return []
    pieces = [p.strip() for p in _SENTENCE_BOUNDARY_RE.split(text) if p and p.strip()]
    return pieces or [text.strip()]


@app.post("/api/ask-voice/stream")
async def ask_voice_stream(
    request: Request,
    audio: UploadFile = File(...),
    role: str = Depends(_auth.current_role),
    user: Dict[str, Any] = Depends(_auth.current_user),
    conversation_id: Optional[str] = Query(None),
    continuous: int = Query(0),
) -> StreamingResponse:
    normalize_role(role)
    # Cookie identifies the user. is_kiosk_turn is informational only
    # (the frontend renders differently on a wall vs a laptop) — it
    # does NOT gate behaviour anymore. Voice-ID was previously used to
    # attribute kiosk turns to whoever was speaking; that's been
    # replaced by PIN-pad switch-user on the wall, where the user
    # identifies themselves explicitly.
    _sid = request.cookies.get(_auth.COOKIE_NAME) or ""
    is_kiosk_turn = bool(_sid and _auth.session_is_kiosk(_sid))
    # Trusted-device fallback. After a PIN-switch, the cookie session is
    # deliberately is_kiosk=0 (so the picked user gets a regular session),
    # but the physical tablet is still a kiosk wall — the wall-device
    # header identifies it as one. Without this fallback, voice-ID would
    # silently stop running the moment anyone PIN-switches, which defeats
    # the whole "auto-login next person" UX.
    if not is_kiosk_turn:
        _wall_dev = (request.headers.get("x-yorik-wall-device") or "").strip()
        if _wall_dev and _auth.is_trusted_kiosk_device(_wall_dev):
            is_kiosk_turn = True

    suffix = Path(audio.filename or "audio.webm").suffix or ".webm"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await audio.read())
        tmp_path = tmp.name

    async def event_stream():
        def emit(obj: Dict[str, Any]) -> bytes:
            return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")

        try:
            # 1) STT
            from .voice import transcribe_detailed
            detail = await asyncio.to_thread(transcribe_detailed, tmp_path)
            transcript = (detail["text"] or "").strip()
            detected_language = detail["language"]
            if not transcript:
                yield emit({"type": "error", "error": "Empty transcript — please retry"})
                return
            yield emit({"type": "transcript", "text": transcript, "language": detected_language})

            # 1.25) Stop-phrase shortcut. In hands-free continuous mode
            # the user can end the conversation by saying "stop" /
            # "stopp" / "halt" / "ende" — same intent as tapping the
            # "end" pill. Emit a fast done event with early_exit so
            # the frontend exits ping-pong without us burning a
            # round-trip on the ack, LLM, and TTS. Only short
            # transcripts qualify so a real question that happens to
            # contain "stop" (e.g. "stop the alarm at seven") goes
            # through the normal path.
            normalized = transcript.lower().strip(" .!?,;:").strip()
            if normalized in _VOICE_STOP_PHRASES:
                logging.getLogger("homeos.voice").info(
                    "voice stop-phrase: %r — early exit", normalized,
                )
                yield emit({
                    "type":            "done",
                    "response":        "",
                    "transcript":      transcript,
                    "early_exit":      True,
                    "language":        detected_language or DEFAULT_LANGUAGE,
                    "ui_actions":      [],
                    "conversation_id": None,
                })
                return

            # 1.5) Instant acknowledgement audio — fires the MOMENT STT
            #      finishes, BEFORE speaker-ID and BEFORE the LLM call.
            #      The whole magic of "natural conversation" is this gap:
            #      user sees transcript + immediately hears "klar, Moment"
            #      while Yorik thinks. Skipped if the user toggled it off.
            #
            #      The WAV bytes are embedded INLINE as base64 in the
            #      event, NOT served via /api/tts-audio/{token}. Reason:
            #      a separate fetch to a streaming-busy server takes 5-10s
            #      due to HTTP/1.1 head-of-line / connection-pool effects;
            #      inlining means the audio plays the millisecond the JSON
            #      line is parsed. Acks are tiny (~50KB) so this is fine.
            ack_enabled = _voice_ack_enabled_for_role(role)
            if ack_enabled:
                from . import voice_acks
                import base64 as _b64
                ack = voice_acks.random_phrase(detected_language or DEFAULT_LANGUAGE)
                if ack:
                    phrase, idx, wav = ack
                    yield emit({
                        "type":      "ack",
                        "audio_b64": _b64.b64encode(wav).decode("ascii"),
                        "mime":      "audio/wav",
                        "text":      phrase,
                    })

            # 2) Speaker identification — kiosk turns only.
            #
            # When the request comes from a YorikWall tablet (kiosk
            # session + trusted device header), run ECAPA against any
            # enrolled profiles. On a confident match we mint a
            # single-use swap_token; the frontend redeems it at
            # /api/auth/voice-login to swap the cookie over to the
            # identified user — same effect as the PIN pad but without
            # the typing. The agent below also runs as that user
            # because we set eff_role/eff_language/eff_user_id from
            # the match.
            #
            # On a NO-match (voice-ID ran but couldn't recognise the
            # speaker), we bail to the PIN-picker via the
            # identify_needed event rather than answering as the
            # device-owner's session — attributing one household
            # member's question to another would silently leak
            # whichever person's email/calendar/etc the answer
            # touches. The frontend listens for identify_needed and
            # shows AvatarPinFallback.
            #
            # Non-kiosk turns never run voice-ID — laptops and phones
            # are identified by their cookie, which is bound to the
            # user that PIN-switched on it.
            identified: Optional[Dict[str, Any]] = None
            voice_id_ran = False
            wall_device_id = (request.headers.get("x-yorik-wall-device") or "").strip()

            if is_kiosk_turn and wall_device_id:
                try:
                    from . import voice_id as _vid
                    from . import voice_login_tokens as _vtoken
                    match = await asyncio.to_thread(_vid.identify, tmp_path)
                    voice_id_ran = True
                    if match:
                        identified = {
                            **match,
                            "swap_token": _vtoken.mint(
                                profile_id=int(match["profile_id"]),
                                device_uuid=wall_device_id,
                                source_sid=_sid,
                            ),
                        }
                except Exception as exc:  # noqa: BLE001
                    logging.getLogger("homeos.voice").warning(
                        "voice-id on kiosk turn failed: %s", exc,
                    )

            # If voice-ID actually ran but couldn't match the speaker
            # AND there's at least one enrolled profile (so the
            # identify_on_kiosk feature is meant to be live), bail to
            # the avatar+PIN picker. Without enrolled profiles, fall
            # through to the cookie-attributed path so a fresh install
            # doesn't lock everyone out of voice.
            #
            # `continuous=1` (set by the frontend on ping-pong follow-
            # ups) suppresses this branch — once the user has been
            # identified for the conversation (wake-fire turn), a
            # threshold miss on a follow-up turn should NOT yank them
            # back to PIN. ChatApp doesn't mount the picker anyway, so
            # without this gate the user just sees the ack and then
            # silence. The current session keeps owning the turn.
            if is_kiosk_turn and voice_id_ran and not identified and not continuous:
                with conn_ctx(DB_PATH) as conn:
                    any_enrolled = conn.execute(
                        "SELECT COUNT(*) AS n FROM user_profiles "
                        "WHERE voice_embedding IS NOT NULL AND voice_embedding != ''"
                    ).fetchone()["n"]
                    if any_enrolled:
                        rows = conn.execute(
                            "SELECT id, name, COALESCE(first_name, '') AS first_name "
                            "FROM user_profiles "
                            "WHERE pin_hash IS NOT NULL "
                            "  AND (disabled = 0 OR disabled IS NULL) "
                            "ORDER BY name ASC"
                        ).fetchall()
                        pickable = [
                            {
                                "id":         int(r["id"]),
                                "name":       r["name"],
                                "first_name": r["first_name"] or (
                                    r["name"].split(" ")[0] if r["name"] else ""
                                ),
                            }
                            for r in rows
                        ]
                        yield emit({
                            "type":          "identify_needed",
                            "users":         pickable,
                            "transcript":    transcript,
                            "retry_message": transcript,
                        })
                        return

            if identified:
                eff_role = identified["role"]
                eff_language = identified.get("language") or detected_language or DEFAULT_LANGUAGE
            else:
                eff_role = role
                eff_language = detected_language or DEFAULT_LANGUAGE
            yield emit({
                "type": "identification",
                "identified": identified,
                "effective_role": eff_role,
                "language": eff_language,
                # Surface so the frontend can render
                # different UX on a tablet wall vs a laptop.
                "is_kiosk": is_kiosk_turn,
            })

            # 3) Streamed agent ask — TextDelta events feed a sentence
            #    chunker; each completed sentence ships to TTS and the
            #    audio event flies out the door BEFORE the LLM has
            #    finished generating. User hears the response start
            #    while Yorik is still thinking about the middle of it.
            #    Per-sentence audio is base64-inlined (same reason as
            #    the ack above: separate /api/tts-audio fetches contend
            #    with this streaming connection's HTTP/1.1 slot).
            #
            # eff_user_id: the session cookie's user. Used to be a
            # voice-ID-vs-cookie merge (see Speaker identification
            # comment above) but voice-ID is off by default now, so
            # this just reads the cookie. Kept as `(identified or
            # {}).get("profile_id") or user.get("id")` so the future
            # opt-in voice-ID path doesn't need to refactor this.
            eff_user_id = (identified or {}).get("profile_id") or user.get("id")
            local_conv_id = conversation_id
            if not local_conv_id and eff_user_id is not None:
                local_conv_id = _voice_conv_id_with_idle_break(eff_user_id)

            from .agent import streaming as _stream
            import base64 as _b64

            response_text = ""           # accumulate for the final "done" event
            audio_idx = 0                # increments per emitted audio sentence
            chunker = _stream.SentenceChunker(min_chars=20)
            final_payload: Dict[str, Any] = {}

            async def _ship_sentence(sent: str) -> bytes:
                """Humanise + synthesise + base64-pack a sentence for emit()."""
                nonlocal audio_idx
                tts_text = _humanize_for_tts(sent, eff_language)
                wav = await asyncio.to_thread(tts_mod.synthesize, tts_text, eff_language)
                if not wav:
                    return b""
                evt = emit({
                    "type":      "audio",
                    "audio_b64": _b64.b64encode(wav).decode("ascii"),
                    "mime":      "audio/wav",
                    "text":      sent,
                    "index":     audio_idx,
                })
                audio_idx += 1
                return evt

            async for ev in vanna_agent.ask_async_stream(
                transcript, role=eff_role,
                conversation_id=local_conv_id,
                user_language=eff_language,
                identified_name=(identified or {}).get("name"),
                user_id=eff_user_id,
                voice_mode=True,
            ):
                if isinstance(ev, _stream.TextDelta):
                    response_text += ev.text
                    # Surface the raw delta to the voice modal so the
                    # transcript-style text appears as Yorik thinks.
                    # (TTS is still per-sentence via the chunker below.)
                    yield emit({"type": "text_delta", "text": ev.text})
                    for sent in chunker.feed(ev.text):
                        out = await _ship_sentence(sent)
                        if out: yield out
                elif isinstance(ev, _stream.FinalResult):
                    final_payload = ev.response or {}
                    # If the final response_text differs (e.g. agent
                    # post-processed the visible response), prefer it.
                    final_text = final_payload.get("response") or ""
                    if final_text and final_text != response_text:
                        # Sync to the canonical text — the chunker may
                        # have missed a final punctuation that the
                        # post-processor added.
                        response_text = final_text
                # Tool events are not surfaced as audio in voice; they
                # influence the response via in-loop side effects.

            # Flush whatever's left in the chunker (trailing fragment).
            for sent in chunker.flush():
                out = await _ship_sentence(sent)
                if out: yield out

            # If the final response carried text we never streamed (e.g.
            # cache hit with no per-token deltas), synthesize it now in
            # one shot. Otherwise the user hears the ack + nothing.
            if not audio_idx and response_text:
                tts_text = _humanize_for_tts(response_text, eff_language)
                sentences = _split_sentences(tts_text)
                for i, sent in enumerate(sentences):
                    out = await _ship_sentence(sent)
                    if out: yield out

            result = final_payload  # for the "done" event below

            # 5) Done — full response + UI actions for the client to apply.
            # Include agent_trace when the user has dev mode on so the chat
            # UI can render the same Debug pane for voice-driven turns. The
            # field is absent (not null) when dev_mode is off, matching the
            # /api/ask shape.
            done_event = {
                "type": "done",
                "response": response_text,
                "transcript": transcript,
                "identified": identified,
                "effective_role": eff_role,
                "language": eff_language,
                "ui_actions": result.get("ui_actions") or [],
                "sql_used": result.get("sql_used"),
                "from_cache": result.get("from_cache", False),
                "conversation_id": result.get("conversation_id"),
            }
            if result.get("agent_trace") is not None:
                done_event["agent_trace"] = result["agent_trace"]
            yield emit(done_event)
        except Exception as exc:  # noqa: BLE001
            yield emit({"type": "error", "error": f"{type(exc).__name__}: {exc}"})
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    return StreamingResponse(event_stream(), media_type="application/x-ndjson")


# ─── Voice resume after PIN switch ────────────────────────────────────
#
# The kiosk wall flow: user speaks → /api/ask-voice/stream → voice ID
# fails → tablet shows the PIN picker → user enters PIN → 5-min ephemeral
# session is issued → ... and historically we either lost the original
# question or had to navigate to /chat and re-show it in a text UI.
#
# This endpoint closes that loop: the tablet calls it with the
# previously-captured transcript and gets back the SAME ndjson voice
# stream (text_delta + audio + ui_actions + done) that the audio-input
# endpoint produces. Frontend can pipe it through the existing VoiceFab
# stream handler unchanged.
#
# Skips Whisper + ack + speaker-ID — the user just identified themselves
# with the PIN; we trust the session cookie.


class _AskVoiceResumeBody(BaseModel):
    transcript:      str
    conversation_id: Optional[str] = None
    language:        Optional[str] = None


@app.post("/api/ask-voice/resume")
async def ask_voice_resume(
    body: _AskVoiceResumeBody,
    role: str = Depends(_auth.current_role),
    user: Dict[str, Any] = Depends(_auth.current_user),
) -> StreamingResponse:
    """Re-run the agent + TTS pipeline for a transcript captured by a
    previous /api/ask-voice/stream call. Originally introduced to
    resume after an `identify_needed` PIN-picker bailout; now used
    whenever the frontend wants to re-attribute a captured transcript
    to a different user after switch-user (i.e. the typical dinner-
    table flow on a wall: user A speaks, taps switch, user B types
    their PIN, the same transcript runs as user B).

    Same ndjson shape as /api/ask-voice/stream from the
    `text_delta`/`audio`/`done` phase onward (no `transcript`/`ack`
    events — caller already has those from the first turn). The
    frontend treats the events identically; only the entry point
    differs."""
    transcript = (body.transcript or "").strip()
    if not transcript:
        async def _bad():
            yield (json.dumps({"type": "error", "error": "Empty transcript"}) + "\n").encode("utf-8")
        return StreamingResponse(_bad(), media_type="application/x-ndjson")

    normalize_role(role)
    eff_language = (body.language or "").strip() or DEFAULT_LANGUAGE
    conversation_id = body.conversation_id
    eff_user_id = user.get("id")
    if not conversation_id and eff_user_id is not None:
        conversation_id = _voice_conv_id_with_idle_break(eff_user_id)

    async def event_stream():
        def emit(obj: Dict[str, Any]) -> bytes:
            return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        from .agent import streaming as _stream
        import base64 as _b64
        try:
            response_text = ""
            audio_idx = 0
            chunker = _stream.SentenceChunker(min_chars=20)
            final_payload: Dict[str, Any] = {}

            async def _ship_sentence(sent: str) -> bytes:
                nonlocal audio_idx
                tts_text = _humanize_for_tts(sent, eff_language)
                wav = await asyncio.to_thread(tts_mod.synthesize, tts_text, eff_language)
                if not wav:
                    return b""
                evt = emit({
                    "type": "audio", "audio_b64": _b64.b64encode(wav).decode("ascii"),
                    "mime": "audio/wav", "text": sent, "index": audio_idx,
                })
                audio_idx += 1
                return evt

            async for ev in vanna_agent.ask_async_stream(
                transcript, role=role,
                conversation_id=conversation_id,
                user_language=eff_language,
                identified_name=user.get("name"),
                user_id=eff_user_id,
                voice_mode=True,
            ):
                if isinstance(ev, _stream.TextDelta):
                    response_text += ev.text
                    yield emit({"type": "text_delta", "text": ev.text})
                    for sent in chunker.feed(ev.text):
                        out = await _ship_sentence(sent)
                        if out: yield out
                elif isinstance(ev, _stream.FinalResult):
                    final_payload = ev.response or {}
                    final_text = final_payload.get("response") or ""
                    if final_text and final_text != response_text:
                        response_text = final_text

            for sent in chunker.flush():
                out = await _ship_sentence(sent)
                if out: yield out

            if not audio_idx and response_text:
                tts_text = _humanize_for_tts(response_text, eff_language)
                for sent in _split_sentences(tts_text):
                    out = await _ship_sentence(sent)
                    if out: yield out

            done_event = {
                "type": "done",
                "response": response_text,
                "transcript": transcript,
                "effective_role": role,
                "language": eff_language,
                "ui_actions": final_payload.get("ui_actions") or [],
                "sql_used": final_payload.get("sql_used"),
                "from_cache": final_payload.get("from_cache", False),
                "conversation_id": final_payload.get("conversation_id"),
            }
            if final_payload.get("agent_trace") is not None:
                done_event["agent_trace"] = final_payload["agent_trace"]
            yield emit(done_event)
        except Exception as exc:  # noqa: BLE001
            yield emit({"type": "error", "error": f"{type(exc).__name__}: {exc}"})

    return StreamingResponse(event_stream(), media_type="application/x-ndjson")



# React frontend (frontend-react/dist) served at /r/* — same-origin so
# the session cookie travels and the React app's API calls share
# auth with the legacy vanilla frontend.
_REACT_DIST = Path(__file__).resolve().parent.parent / "frontend-react" / "dist"
if _REACT_DIST.exists():
    _REACT_INDEX = _REACT_DIST / "index.html"

    @app.get("/r", include_in_schema=False)
    @app.get("/r/", include_in_schema=False)
    @app.get("/r/{full_path:path}", include_in_schema=False)
    def react_spa(full_path: str = ""):
        # Serve a real file from dist/ if it exists; otherwise fall
        # back to index.html so react-router can take over. We try
        # the file first (catches /r/manifest.webmanifest, /r/yorik.svg,
        # robots.txt, etc.), so we don't have to whitelist every
        # static path Vite copies out of public/.
        if full_path:
            candidate = (_REACT_DIST / full_path).resolve()
            try:
                candidate.relative_to(_REACT_DIST.resolve())
            except ValueError:
                candidate = _REACT_INDEX
            if candidate.is_file():
                headers = ({"Cache-Control": "public, max-age=31536000, immutable"}
                           if "assets/" in str(candidate) else {"Cache-Control": "no-cache"})
                return FileResponse(candidate, headers=headers)
        return FileResponse(_REACT_INDEX, headers={"Cache-Control": "no-cache"})


# SPA-aware static serving — MUST be registered last so /api/* routes win.
# Direct file hits under /frontend/* (app.js, styles.css, layouts/google.js,
# index.html, …) are served as-is. Unknown paths like /chat, /carpenter-crm,
# /calendar fall back to index.html so the frontend router can take over;
# without this, hard-refreshing /chat would 404.
if FRONTEND_DIR.exists():
    _INDEX_HTML = FRONTEND_DIR / "index.html"

    # Frontend assets we serve directly through this catch-all need explicit
    # no-cache headers, because Brave (and to a lesser extent Chrome) cache
    # aggressively enough that even Ctrl+Shift+R sometimes serves stale JS
    # while we're iterating. ETag-based revalidation alone isn't enough —
    # this forces a fresh fetch every time, at the cost of one round trip.
    _NO_CACHE_HEADERS = {
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str):
        # Strip any leading slash and resolve safely under FRONTEND_DIR.
        rel = full_path.lstrip("/") or "index.html"
        candidate = (FRONTEND_DIR / rel).resolve()
        try:
            candidate.relative_to(FRONTEND_DIR.resolve())
        except ValueError:
            # Path tried to escape via .. — refuse and fall through to index.
            candidate = _INDEX_HTML
        if candidate.is_file():
            return FileResponse(candidate, headers=_NO_CACHE_HEADERS)
        # Anything else (client-side route like /chat) → index.html
        return FileResponse(_INDEX_HTML, headers=_NO_CACHE_HEADERS)




