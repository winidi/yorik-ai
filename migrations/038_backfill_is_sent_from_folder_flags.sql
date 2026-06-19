-- Backfill is_sent for rows the fetcher pulled from a \Sent-flagged
-- folder before commit 5823373. Those rows have is_sent=0 (column
-- default) even though they live in the IMAP Sent folder, which makes
-- them show up in the user's inbox view (since /api/email/messages
-- splits inbox vs sent on the is_sent column).
--
-- Idempotent: only touches rows where is_sent=0 AND folder is Sent.
-- email_folders.flags is a JSON array stored as TEXT, e.g.
--   ["\\HasNoChildren", "\\Sent"]
-- LIKE '%\Sent%' matches reliably because the surrounding quotes/escapes
-- are deterministic.
UPDATE email_messages
   SET is_sent = 1
 WHERE is_sent = 0
   AND folder_id IN (
        SELECT id FROM email_folders
         WHERE flags LIKE '%\Sent%'
   );
