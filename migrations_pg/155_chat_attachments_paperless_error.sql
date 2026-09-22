-- Paperless answers an upload with a task; the task can still fail
-- (a duplicate, a broken file). The card in the chat said "filed"
-- regardless (Beate's Kobra.pdf, 2026-09-21). The failure is kept here
-- so the card can say what happened.
ALTER TABLE chat_attachments ADD COLUMN IF NOT EXISTS paperless_error TEXT;
