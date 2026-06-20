-- Per-attachment Paperless ingestion state. Pre-existing rows have
-- paperless_id populated iff they were auto-uploaded under the old
-- unconditional rule; we backfill them as 'auto_filed' so the new UI
-- doesn't show them as "pending".
--
-- State machine:
--   NULL          — never offered (attachment isn't a document type,
--                   or paperless wasn't configured for the user)
--   'suggested'   — Tier 2: pending user action (file / discard)
--   'auto_filed'  — Tier 1: ingested automatically on email arrival
--   'filed'       — User manually filed it via the Tier 2 prompt
--   'discarded'   — User declined / undid an auto-file
--   'failed'      — Upload attempted but Paperless rejected it
ALTER TABLE email_attachments
  ADD COLUMN IF NOT EXISTS paperless_state TEXT;

UPDATE email_attachments
   SET paperless_state = 'auto_filed'
 WHERE paperless_id IS NOT NULL
   AND paperless_state IS NULL;
