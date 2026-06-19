-- agent_conversations.ledger_json
--
-- Per-conversation entity ledger: a compact JSON dict of recently-
-- mentioned entities (events, tasks, contacts, drafts, documents)
-- with their ids + display labels. Injected as a second system
-- message every turn so the LLM can resolve "the appointment I just
-- made" / "make it friendlier" to the right row id without having
-- to fish through 30 raw tool_result blobs.
--
-- Default '{}' so the column is non-NULL and load_messages can read
-- it without a coalesce. The Python ledger module (entity_ledger.py)
-- caps each bucket so this column stays small — typically under 1 KB
-- per conversation.

ALTER TABLE agent_conversations ADD COLUMN ledger_json TEXT NOT NULL DEFAULT '{}';
