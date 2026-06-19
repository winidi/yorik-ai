-- Per-message agent_trace storage for dev mode.
--
-- Rationale: a dev user wants to look back at a conversation from 3 days
-- ago and ask "did Yorik use the right tools? what did the args look
-- like?". The agent_trace dict (per-iteration timing, tool name + args +
-- result snippets) is built every time the loop runs with dev_mode=on,
-- and was so far only kept in the in-memory ChatMessage. This table
-- persists it so /api/conversations/{id} can hydrate the Debug pane
-- after a refresh.
--
-- Keyed on (conversation_id, message_idx) — message_idx is the position
-- of the final assistant message in agent_conversations.messages_json
-- as the loop appended it. The conversation_id FK + CASCADE means
-- deleting a conversation also drops its traces.
--
-- trace_json is the same dict shape the loop returns and the API
-- already passes through, so the GET endpoint just attaches it to the
-- corresponding message — no transformation, no schema drift.

CREATE TABLE IF NOT EXISTS agent_message_traces (
    conversation_id TEXT NOT NULL,
    message_idx     INTEGER NOT NULL,
    trace_json      TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (conversation_id, message_idx),
    FOREIGN KEY (conversation_id) REFERENCES agent_conversations(id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_agent_message_traces_conv
    ON agent_message_traces (conversation_id);
