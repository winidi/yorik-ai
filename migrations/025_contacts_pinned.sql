-- Migration 025: pinned flag on contacts
--
-- Drives the "★ Pinned" section at the top of the Contacts list.
-- Manual override on top of the auto "🕐 Recent" bucket (which is
-- derived from `last_interaction_at` / `last_used_at` and needs no
-- new column).
--
-- 0/1 integer rather than a separate `contact_pins` table — pinning
-- is a per-contact boolean, no audit needed.

ALTER TABLE contacts ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0;

-- Index lets the sidebar query (ORDER BY pinned DESC, ...) finish in
-- one pass even at hundreds of contacts.
CREATE INDEX IF NOT EXISTS ix_contacts_pinned
    ON contacts (pinned DESC, status);
