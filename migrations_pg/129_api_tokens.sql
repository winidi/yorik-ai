-- Personal API tokens.
--
-- An agent (Hermes, Claude, a script) connects to Yorik with a token
-- that is bound to ONE user account. The token carries no role of its
-- own: every request resolves to the owning user, and the skills apply
-- that user's role and workspace scoping exactly as they do in chat.
-- Only the SHA-256 of the token is stored; the plain value is shown
-- once at creation.
CREATE TABLE IF NOT EXISTS api_tokens (
    id            BIGSERIAL PRIMARY KEY,
    user_id       UUID NOT NULL REFERENCES user_profiles(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    token_hash    TEXT NOT NULL UNIQUE,
    token_prefix  TEXT NOT NULL,                  -- first 8 chars, for the list UI
    created_at    TEXT NOT NULL DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
    last_used_at  TEXT,
    revoked_at    TEXT
);

CREATE INDEX IF NOT EXISTS api_tokens_user_idx ON api_tokens (user_id);
