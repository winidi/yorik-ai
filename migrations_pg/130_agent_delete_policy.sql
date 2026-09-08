-- Deletions staged by an outside agent (MCP / API token) wait for the
-- account owner in the Yorik app by default. This flag lets a user opt
-- their agents into confirming deletions themselves.
ALTER TABLE user_profiles
    ADD COLUMN IF NOT EXISTS agent_may_confirm_deletes BOOLEAN NOT NULL DEFAULT FALSE;
