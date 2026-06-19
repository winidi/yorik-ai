-- email_deleted_message_ids
--
-- Tombstone table: every time delete_message succeeds at the local-DB
-- level (whether the IMAP MOVE worked, the COPY-to-Trash worked, or
-- the IMAP server refused everything and we deleted the row only),
-- we record the message's Message-ID here. The fetcher's
-- _insert_message checks this table before INSERTing a freshly-fetched
-- message — if its Message-ID is on the list, skip silently.
--
-- Without this, deleting a message from a read-only Gmail/Proton
-- "All Mail" folder (the demo-blocking pattern) "succeeds" but the
-- next fetcher tick re-creates the row because the message is still
-- on the server. The tombstone makes the local delete sticky.
--
-- Scoped per-account so the same Message-ID across two accounts
-- doesn't accidentally cross-delete. (Message-IDs are supposed to be
-- globally unique but real-world mailing-list software reuses them.)

CREATE TABLE IF NOT EXISTS email_deleted_message_ids (
    account_id  INTEGER NOT NULL REFERENCES email_accounts(id) ON DELETE CASCADE,
    message_id  TEXT    NOT NULL,
    deleted_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (account_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_email_deleted_msgid
    ON email_deleted_message_ids (message_id);
