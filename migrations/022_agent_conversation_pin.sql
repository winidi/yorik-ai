-- Migration 022: pinned column on agent_conversations
--
-- Drives the Chat sidebar's "📌 Pinned" section at the top of the
-- conversation list. Stars a thread so it sticks above the
-- Today / Yesterday / Earlier date groupings — useful for keeping
-- a long-running planning thread (Hannover trip, Q3 strategy)
-- always reachable in one click.
--
-- 0/1 integer rather than a separate pins table — pinning is a
-- 1:1 boolean per conversation, no audit needed.

ALTER TABLE agent_conversations ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0;

-- Index lets the sidebar query (ORDER BY pinned DESC, updated_at DESC)
-- finish in one pass even at hundreds of threads.
CREATE INDEX IF NOT EXISTS ix_agent_conversations_pinned
    ON agent_conversations (pinned DESC, updated_at DESC);
