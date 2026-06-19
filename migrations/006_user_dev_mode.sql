-- Per-user developer-mode toggle. When ON, /api/ask responses include
-- a structured `agent_trace` field (per-iteration timing, tool calls
-- with args + result snippets) which the React chat UI renders as a
-- collapsible "▼ Debug" pane under each assistant message.
--
-- Stored on user_profiles so the preference survives sessions and is
-- per-user (one admin can have it on without showing trace data to
-- another admin sharing the same household account).
--
-- 0 = off (default), 1 = on. SQLite has no real boolean, INTEGER+CHECK
-- is the standard pattern across this codebase.

ALTER TABLE user_profiles
    ADD COLUMN dev_mode INTEGER NOT NULL DEFAULT 0
        CHECK (dev_mode IN (0, 1));
