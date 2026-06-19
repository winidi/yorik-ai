-- Migration 027: per-user email-sender blocklist
--
-- Powers the "Spam" button on email_proposal notifications. When the
-- user blocks a sender, this table records it; the email classifier
-- consults it BEFORE creating the next bill/appointment notification,
-- so future mail from that sender (or domain) silently drops on the
-- floor instead of nagging them again.
--
-- Per-user, not global — what's spam in the admin's inbox isn't
-- automatically spam in the kid's school-account inbox.
--
-- One row per (user_id, sender_address) or (user_id, sender_domain).
-- Both columns are nullable; a row has EITHER an address (exact match)
-- OR a domain (suffix match on @<domain>). Never both — domain alone
-- already covers the address.

CREATE TABLE IF NOT EXISTS email_blocklist (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    sender_address  TEXT,                       -- lowercase, full address
    sender_domain   TEXT,                       -- lowercase, no leading '@'
    reason          TEXT,                       -- free-form, optional
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES user_profiles(id) ON DELETE CASCADE,
    -- Exactly one of the two address columns must be set.
    CHECK ((sender_address IS NOT NULL) <> (sender_domain IS NOT NULL))
);

-- Lookups are always (user_id, address) or (user_id, domain); index
-- both so the per-fetch check stays sub-millisecond even at thousands
-- of blocked entries.
CREATE INDEX IF NOT EXISTS ix_email_blocklist_user_address
    ON email_blocklist (user_id, sender_address);

CREATE INDEX IF NOT EXISTS ix_email_blocklist_user_domain
    ON email_blocklist (user_id, sender_domain);

-- Same (user, address) shouldn't be added twice; same for domain.
CREATE UNIQUE INDEX IF NOT EXISTS ux_email_blocklist_user_address
    ON email_blocklist (user_id, sender_address)
 WHERE sender_address IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS ux_email_blocklist_user_domain
    ON email_blocklist (user_id, sender_domain)
 WHERE sender_domain IS NOT NULL;
