"""Phase E §6.6 — per-app scoped JWTs.

Every installed v2 app gets its own Postgres role (`app_<id>_role`)
created at install time with:
  * USAGE on its owned schema only
  * CRUD on its owned tables only
  * SELECT on the projection views (_yorik_<table>) only
  * NO grants on public.* tables

When the app's iframe needs to talk to Supabase directly
(supabase-js → PostgREST / Realtime), Yorik mints an app-scoped JWT
with this role and the end user's UUID as `sub`. RLS policies that
reference `auth.uid()` still work because `sub` is preserved.

Security properties:
  * A malicious app can't read tables outside its schema even if
    it pries the JWT out of its iframe — the role can't SELECT them.
  * The end user's regular JWT is NEVER exposed to the iframe.
    Only the app-scoped JWT is. So an exploited app can't
    impersonate the user against the rest of Supabase.
  * Short-lived (default 1 hour). The /api/apps/<id>/jwt endpoint
    re-mints on demand from the user's authenticated session.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

import jwt as _pyjwt

log = logging.getLogger("yorik.app_jwt")

DEFAULT_TTL_SECONDS = 3600  # 1 hour


def _load_jwt_secret() -> Optional[str]:
    """Same fallback chain as auth_sessions._SUPABASE_JWT_SECRET."""
    secret = os.getenv("YORIK_SUPABASE_JWT_SECRET")
    if secret:
        return secret
    env_file = Path(__file__).resolve().parent.parent / "infra/supabase/docker/.env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("JWT_SECRET="):
                return line.split("=", 1)[1].strip()
    return None


_JWT_SECRET = _load_jwt_secret()


def role_name_for(app_id: str) -> str:
    """The Postgres role name for a given app_id.

    Sanitization: lowercase, dots/hyphens to underscores, append _role.
    Matches the owned_schema convention in app_schema_lifecycle so
    `acme.notes` ↦ `app_acme_notes_role` (alongside `app_acme_notes`
    schema).
    """
    import re
    safe = re.sub(r"[.\-]+", "_", app_id).lower()
    return f"app_{safe}_role"


def mint_app_jwt(
    *,
    app_id: str,
    user_uuid: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> Dict[str, Any]:
    """Sign an HS256 JWT scoped to one app + one end user.

    Returns {"token": str, "expires_at": int (unix epoch seconds)}.

    PostgREST validates the signature against the same JWT_SECRET
    GoTrue uses (the Supabase install's shared symmetric key), then
    SETs the database session role to `claims.role` and exposes
    `claims.sub` to `auth.uid()`. RLS policies see the same user
    UUID as if the user had logged in via the regular JWT — but the
    role is the narrower app role.
    """
    if not _JWT_SECRET:
        raise RuntimeError(
            "JWT_SECRET not configured — cannot mint app JWTs. "
            "Set YORIK_SUPABASE_JWT_SECRET or place JWT_SECRET in "
            "infra/supabase/docker/.env."
        )
    role = role_name_for(app_id)
    now = int(time.time())
    claims: Dict[str, Any] = {
        "iss": "yorik",
        # role: PostgREST switches to this Postgres role for the query
        "role": role,
        # sub: auth.uid() returns this — RLS policies still work
        "sub": user_uuid,
        # aud must be "authenticated" or PostgREST rejects (it checks
        # the JWT aud against its PGRST_JWT_AUD = "authenticated")
        "aud": "authenticated",
        # app_id: surfaced to RLS via current_setting('request.jwt.claims')
        # so a policy can constrain by which app is calling
        "app_id": app_id,
        "iat": now,
        "exp": now + ttl_seconds,
    }
    token = _pyjwt.encode(claims, _JWT_SECRET, algorithm="HS256")
    return {"token": token, "expires_at": claims["exp"], "role": role}
