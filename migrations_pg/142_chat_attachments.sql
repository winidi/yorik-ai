-- Files shown to Yorik in a chat. They belong to the conversation, not
-- to a library: visible as a card where they were uploaded, gone with
-- the conversation or after the retention period, unless the person
-- files them in Paperless — the one place documents live.
CREATE TABLE IF NOT EXISTS chat_attachments (
    id                BIGSERIAL PRIMARY KEY,
    owner_user_id     UUID    NOT NULL,
    conversation_id   TEXT,
    filename          TEXT    NOT NULL,
    mime_type         TEXT    NOT NULL,
    bytes             INTEGER NOT NULL,
    path              TEXT    NOT NULL,          -- under data/chat_attachments/
    text              TEXT,                      -- extracted once, for the assistant
    created_at        TEXT    NOT NULL,
    expires_at        TEXT    NOT NULL,
    filed_at          TEXT,                      -- set when it went to Paperless
    paperless_task_id TEXT,
    visibility        TEXT
);
CREATE INDEX IF NOT EXISTS idx_chat_attachments_owner ON chat_attachments (owner_user_id);
CREATE INDEX IF NOT EXISTS idx_chat_attachments_conversation ON chat_attachments (conversation_id);
