-- Flip auto-captured WhatsApp contacts from pending → active.
--
-- Earlier policy ("Pending by default, promote on reply") was the right
-- call for inbound email — there's lots of cold-mail noise to triage.
-- It was the wrong call for WhatsApp: an existing wa_chats row means
-- you've already validated the other party in your head (WhatsApp's
-- pairing model makes cold-spam strangers vanishingly rare). Hiding
-- 527 real contacts behind a triage gate made find_contact useless
-- for everyday "schick Hans eine Nachricht" voice commands.
--
-- This migration promotes everyone whose only sin was "imported from
-- WhatsApp." Email-sourced pending contacts (source LIKE 'email_in')
-- stay pending — the cold-mail-triage value is real there.
--
-- Idempotent: only flips rows currently pending+wa_source. Hand-set
-- spam / archived / active rows stay where they are.

UPDATE contacts
SET status = 'active'
WHERE status = 'pending'
  AND source IN ('wa_sync', 'wa_manual');
