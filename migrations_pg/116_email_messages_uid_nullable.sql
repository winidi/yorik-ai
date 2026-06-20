-- Allow email_messages.uid to be NULL. Background:
--
-- delete_message + move_to_folder use uid=0 as a "row moved to a
-- folder but real UID not yet known until the fetcher resyncs"
-- sentinel. The table has UNIQUE(account_id, folder_id, uid), so the
-- FIRST move into a folder takes (acct, folder, 0); the SECOND
-- move into the same folder crashes the cleanup endpoint with
-- "duplicate key value violates unique constraint" mid-loop. Bulk
-- cleanups (e.g. "delete all 189 Temu newsletters") therefore deleted
-- only ~the first row per source folder before erroring on every
-- subsequent attempt.
--
-- Postgres treats NULL values in a UNIQUE index as DISTINCT, so
-- switching the sentinel from 0 → NULL lets any number of rows live
-- in the same folder with an unknown UID until the fetcher backfills.
ALTER TABLE email_messages
  ALTER COLUMN uid DROP NOT NULL;

-- Convert existing sentinel rows to NULL so the constraint can re-
-- engage cleanly for new inserts. There SHOULDN'T be more than one
-- per (account, folder) thanks to the old constraint, but be liberal.
UPDATE email_messages SET uid = NULL WHERE uid = 0;
