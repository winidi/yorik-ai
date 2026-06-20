-- Paperless returns a task UUID (not the doc id) from
-- /api/documents/post_document/. Old code threw this away — we kept
-- only paperless_id=0 as a "uploaded, no doc id yet" sentinel. Without
-- the task id we can't later resolve the actual document id to DELETE
-- it on Undo (Tier 1 retraction).
ALTER TABLE email_attachments
  ADD COLUMN IF NOT EXISTS paperless_task_id TEXT;
