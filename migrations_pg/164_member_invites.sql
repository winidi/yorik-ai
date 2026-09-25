-- Join by QR: an admin invites a person into THIS household (not a new
-- tenant — that's the Households tab). The person scans a code, picks
-- name, colour and a 4-digit PIN, and their phone becomes a trusted
-- device that opens Yorik with that PIN. See
-- docs/plans/2026-09-26-einrichten.md.
--
-- Additive only: two new tables, nothing existing changes.

CREATE TABLE IF NOT EXISTS member_invites (
    id              BIGSERIAL PRIMARY KEY,
    token_hash      TEXT NOT NULL UNIQUE,          -- sha256 of the token; the token itself is never stored
    created_by      UUID NOT NULL,
    name            TEXT NOT NULL,                 -- prefilled, the person can change it
    role            TEXT NOT NULL DEFAULT 'member',-- member | restricted (children)
    color           TEXT,
    tailscale_invite_url TEXT,                     -- device-share link for this person, if Yorik could make one
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,
    used_at         TIMESTAMPTZ,
    used_by         UUID,
    revoked_at      TIMESTAMPTZ
);

-- A phone that joined (or was added later) remembers its person with a
-- long-lived random device token in an httpOnly cookie; opening Yorik
-- on it asks only for the person's PIN.
CREATE TABLE IF NOT EXISTS member_devices (
    id              BIGSERIAL PRIMARY KEY,
    user_id         UUID NOT NULL,
    token_hash      TEXT NOT NULL UNIQUE,
    label           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at    TIMESTAMPTZ,
    revoked_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS member_devices_user_idx ON member_devices (user_id);
