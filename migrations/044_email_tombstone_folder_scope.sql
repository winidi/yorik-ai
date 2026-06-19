-- email_deleted_message_ids.suppress_folder_id
--
-- Scope tombstones to a specific folder instead of globally
-- suppressing the Message-ID across every folder. Without this scope,
-- a "delete" that physically COPIES the message to Trash (the only
-- thing Proton Bridge / Gmail allow when the source is a read-only
-- virtual folder like "All Mail") removes the row from Yorik
-- everywhere — including Trash itself — because the fetcher's
-- tombstone gate refuses to (re-)insert the Trash copy on the next
-- tick.
--
-- Semantics after this migration:
--   suppress_folder_id IS NULL  → block insert in ANY folder
--                                 (used by the last-resort local-
--                                 only delete fallback when no
--                                 IMAP operation succeeded)
--   suppress_folder_id = N      → block insert only when the fetcher
--                                 is about to file the message under
--                                 folder N (typically the original
--                                 source folder before the delete)
--
-- The pre-044 backlog of existing tombstones gets reset to NULL,
-- which preserves the "don't bring back" intent but with the broader
-- scope. The fetcher will respect them; if the user wants a globally-
-- suppressed message to come back, they delete the tombstone row by
-- Message-ID.

ALTER TABLE email_deleted_message_ids
    ADD COLUMN suppress_folder_id INTEGER DEFAULT NULL;
