-- Restore the proper jid suffix on bare-digit WhatsApp channels.
--
-- Migration 014 cleared the @lid-sourced contacts and appended
-- @s.whatsapp.net to everything else. But @lid contacts that arrived
-- AFTER migration 014 ran (via an old uvicorn process still using the
-- "strip suffix" autocapture) got stored as bare digits. When the
-- frontend appends @s.whatsapp.net to those for routing it picks the
-- wrong suffix and the message goes to the wrong jid (or fails).
--
-- An @lid contact was the canary: contact value stored as bare digits
-- like '1234567890' with no suffix, but the actual chat is
-- '1234567890@lid' — sending to bare digits routes nowhere.
--
-- This migration joins contact_channels against wa_chats to find each
-- channel's true full jid and rewrites the value. Only touches rows
-- whose value lacks an @-suffix (safe; everything else is left alone).

UPDATE contact_channels
SET value = (
    -- Pick the wa_chats row whose local-part matches this bare-digit
    -- value. If there are multiple (shouldn't happen post-cleanup,
    -- but defensive), prefer @s.whatsapp.net over @lid since real
    -- phones are stabler when both exist.
    SELECT jid FROM wa_chats
    WHERE substr(jid, 1, instr(jid, '@') - 1) = contact_channels.value
    ORDER BY CASE
        WHEN jid LIKE '%@s.whatsapp.net' THEN 0
        WHEN jid LIKE '%@lid'            THEN 1
        ELSE 2
    END
    LIMIT 1
)
WHERE kind = 'whatsapp'
  AND instr(value, '@') = 0
  AND EXISTS (
    SELECT 1 FROM wa_chats
    WHERE substr(jid, 1, instr(jid, '@') - 1) = contact_channels.value
  );
