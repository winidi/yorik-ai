"""read_my_profile skill — return the calling user's own profile fields.

Reads from user_profiles for ctx.user_id. Read-only. Public-safe fields
only: identity, postal address, contact channels, business info, IBAN,
and a boolean indicating whether a signature image is on file. Secrets
(password hash, paperless/immich tokens, voice embedding) never leave
the DB through this skill.
"""
from __future__ import annotations

from typing import Any


_PUBLIC_COLS = (
    "id", "name", "first_name", "last_name",
    "email", "phone",
    "address_street", "address_postcode", "address_city", "country",
    "business_name", "tax_id", "iban",
)


async def execute(ctx) -> dict[str, Any]:
    user_id = getattr(ctx, "user_id", None)
    if not user_id:
        return {
            "_llm_hint": (
                "No logged-in user on this turn — cannot read a profile. "
                "Tell the user they need to sign in for this to work."
            ),
        }

    from backend.database import get_conn

    with get_conn() as conn:
        row = conn.execute(
            f"SELECT {', '.join(_PUBLIC_COLS)}, "
            "       (signature_data_url IS NOT NULL "
            "        AND signature_data_url != '') AS has_signature "
            "FROM user_profiles WHERE id = ?",
            (user_id,),
        ).fetchone()

    if not row:
        return {
            "_llm_hint": (
                f"user_profiles row missing for user_id={user_id}. "
                "This is a data-integrity issue, not a user-facing problem; "
                "tell the user to contact the admin."
            ),
        }

    profile = {k: row[k] for k in _PUBLIC_COLS}
    profile["has_signature"] = bool(row["has_signature"])

    missing = [
        k for k in (
            "address_street", "address_postcode", "address_city",
            "phone", "email",
        )
        if not (profile.get(k) or "").strip()
    ]
    if missing:
        profile["_llm_hint"] = (
            f"Profile loaded; fields not on file: {', '.join(missing)}. "
            "Quote the available values verbatim; for missing ones say "
            "'not on file' rather than guessing or asking again."
        )
    else:
        profile["_llm_hint"] = (
            "Profile loaded. Quote the values verbatim — these come "
            "straight from the user's settings, no inference needed."
        )

    return profile
