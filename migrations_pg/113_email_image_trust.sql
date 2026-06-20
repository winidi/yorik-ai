-- Per-user "always show images from this sender" list. Populated by
-- the Reader's "Always show from <sender>" action; consulted by
-- get_message to flip images_auto_allowed in the response so the
-- banner doesn't even appear on subsequent emails from that sender.
--
-- sender_email is stored lower-cased (the upsert normalises). Email
-- addresses are case-insensitive in the local part too per spec; the
-- world has settled on case-folding for trust comparisons.
CREATE TABLE IF NOT EXISTS email_image_trust (
    id            BIGSERIAL PRIMARY KEY,
    owner_user_id UUID NOT NULL REFERENCES user_profiles(id) ON DELETE CASCADE,
    sender_email  TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_user_id, sender_email)
);

CREATE INDEX IF NOT EXISTS idx_email_image_trust_user
    ON email_image_trust (owner_user_id);
