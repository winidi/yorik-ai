-- Surrogate BIGINT id for wa_messages.
--
-- The suggestion engine uses a single source_id BIGINT column on
-- suggestion_runs to reference the originating message, regardless of
-- modality. email_messages already has a BIGINT id; wa_messages does
-- not — its PK is the composite (chat_jid, msg_id, owner_user_id) and
-- msg_id itself is a TEXT WhatsApp-side identifier ("3EB0...").
-- Adding a serial id lets the engine treat WA messages with the same
-- shape as email, no special casing in the dispatch loop.
--
-- Backfill: BIGSERIAL stamps every existing row at column-add time.
-- UNIQUE so the engine can rely on it being a stable identifier.

ALTER TABLE wa_messages ADD COLUMN IF NOT EXISTS id BIGSERIAL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_wa_messages_id ON wa_messages (id);
