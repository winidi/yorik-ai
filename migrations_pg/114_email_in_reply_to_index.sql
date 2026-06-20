-- Supports the has_my_reply EXISTS subquery on the inbox list:
--   "is there a sent message owned by this user whose in_reply_to
--    matches THIS row's message_id?"
-- Partial index (WHERE is_sent=1 AND in_reply_to IS NOT NULL) keeps it
-- tiny: most messages are incoming, and the lookup only ever looks at
-- sent replies that actually carry an In-Reply-To header.
CREATE INDEX IF NOT EXISTS idx_email_messages_my_reply
    ON email_messages (owner_user_id, in_reply_to)
    WHERE is_sent = 1 AND in_reply_to IS NOT NULL;
