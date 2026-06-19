-- Per-conversation message log for the new agent loop (Phase 1+ of the
-- Vanna replacement). Stores OpenAI-format messages directly so the new
-- loop doesn't need a Vanna Message ↔ OpenAI dict converter — read in,
-- read out, ship straight to chat.completions.create.
--
-- Coexists with the existing `conversations` table (Vanna-format) during
-- the cutover. Phase 4 (Vanna removal) will drop `conversations` and
-- optionally migrate any still-live threads over.
--
-- (id, user_role) uniqueness means a given conversation_id is owned by
-- exactly one role — matches the access-control story the old store had.

CREATE TABLE IF NOT EXISTS agent_conversations (
    id            TEXT PRIMARY KEY,
    user_role     TEXT NOT NULL,
    user_id       INTEGER,                       -- nullable for legacy rows
    messages_json TEXT NOT NULL DEFAULT '[]',    -- OpenAI-format list
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS ix_agent_conversations_updated
    ON agent_conversations (updated_at DESC);

CREATE INDEX IF NOT EXISTS ix_agent_conversations_user
    ON agent_conversations (user_id, updated_at DESC);
