-- Clean up the WhatsApp pseudo-jid contagion that caused messages to
-- "Max" to land on the user's brother.
--
-- Root cause: the original seeder stored channel `value` as the digits
-- before the `@` — so `75904990613645@lid` (a randomized WhatsApp
-- Linked-Device hash) and `4915128811234@s.whatsapp.net` (a real
-- phone) became indistinguishable. Eight different "Max" contacts
-- existed pointing at unrelated jids; the LLM and UI both grabbed
-- whichever came first.
--
-- After this migration:
--   1. Channel values store the FULL jid (digits + suffix).
--   2. Pending contacts that came from pseudo-jids (@lid / @newsletter /
--      @broadcast) are deleted — they were never user-confirmed.
--   3. Real-phone whatsapp channels gain the @s.whatsapp.net suffix.
--
-- The fixed seeder + on_inbound_whatsapp ensure no new pseudo-jid
-- contacts ever land in contacts going forward.

-- Step 1: delete contact_channels rows for pseudo-jid sources. Find them
-- by reverse-matching the channel value against wa_chats: any value that
-- appears as the local-part of a pseudo-jid is itself a pseudo-jid.
-- (Detected via the "long, non-+, non-DE-mobile-shaped" heuristic too:
-- @lid values are typically 13-15 digits with no country-code prefix.)
DELETE FROM contact_channels
WHERE kind = 'whatsapp'
  AND source = 'wa_sync'
  AND value IN (
    SELECT substr(jid, 1, instr(jid, '@') - 1)
    FROM wa_chats
    WHERE jid LIKE '%@lid'
       OR jid LIKE '%@newsletter'
       OR jid LIKE '%@broadcast'
       OR jid = 'status@broadcast'
  );

-- Step 2: delete orphan pending+wa_sync contacts (no channels left).
-- Conservative: only those that originated from auto-capture and never
-- got a manual edit or extra channels attached.
DELETE FROM contacts
WHERE status = 'pending'
  AND source = 'wa_sync'
  AND id NOT IN (SELECT DISTINCT contact_id FROM contact_channels);

-- Step 3: re-normalize remaining whatsapp channel values to the full
-- jid format. After step 1+2 only real-phone channels remain; append
-- @s.whatsapp.net unless the value already has an @-suffix.
UPDATE contact_channels
SET value = value || '@s.whatsapp.net'
WHERE kind = 'whatsapp'
  AND instr(value, '@') = 0;
