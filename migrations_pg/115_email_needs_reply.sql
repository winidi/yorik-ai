-- User-flagged "I need to reply to this" — purely local metadata, no
-- IMAP roundtrip. Drives the amber "reply" pill in the inbox row and
-- the toolbar toggle in the Reader. Defaulting to 0 (integer-not-bool
-- so callers using is_starred=0/1 patterns still work) keeps existing
-- rows untouched.
ALTER TABLE email_messages
  ADD COLUMN IF NOT EXISTS needs_reply INTEGER NOT NULL DEFAULT 0;

-- Filter index — most messages have needs_reply=0, so a partial index
-- keeps the "show me everything I owe a reply on" view scan-friendly
-- once we wire a sidebar pseudo-folder for it.
CREATE INDEX IF NOT EXISTS idx_email_messages_needs_reply
    ON email_messages (owner_user_id, needs_reply)
    WHERE needs_reply = 1;
