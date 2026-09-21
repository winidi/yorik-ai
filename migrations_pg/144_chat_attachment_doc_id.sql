-- The Paperless document a filed chat attachment became (known once
-- Paperless has consumed the file).
ALTER TABLE chat_attachments ADD COLUMN IF NOT EXISTS paperless_doc_id INTEGER;
