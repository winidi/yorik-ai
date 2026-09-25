-- Where a WhatsApp chat's name came from (2026-09-24).
--
-- WhatsApp picks the name it shows from a fixed order of sources: the
-- address book first, then a verified business name, then the name the
-- other person chose for themselves. Yorik showed whatever arrived
-- last, which was almost always that self-chosen pushName. Keeping the
-- source lets the ingest refuse a weaker one (backend/whatsapp.py,
-- _upsert_chat) and lets the list mark a pushName with a leading "~",
-- the way WhatsApp marks it.
--
-- Values: 'book' | 'business' | 'chat' | 'push' (see
-- whatsapp-bridge/names.js). NULL means we never learned one.
ALTER TABLE wa_chats ADD COLUMN IF NOT EXISTS name_source TEXT;

-- Everything on hand today came from the old last-writer-wins path, so
-- it is no stronger than a pushName. Saying so lets a better name take
-- its place on the next sync instead of being locked out forever.
UPDATE wa_chats SET name_source = 'push'
 WHERE name_source IS NULL AND name IS NOT NULL AND trim(name) <> '';
