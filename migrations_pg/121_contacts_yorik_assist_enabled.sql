-- Per-contact AI opt-in for the suggestion engine. Default OFF so
-- no contact gets analysed without explicit user consent (the
-- product's sovereignty story collapses if family members were
-- silently profiled without anyone asking).
--
-- Bulk-enable lives in the contacts page header — one click flips
-- every active contact's flag to true. New contacts still default
-- off; the bulk action would need re-clicking after seeding more
-- contacts. Acceptable friction for the privacy guarantee.
ALTER TABLE contacts
  ADD COLUMN IF NOT EXISTS yorik_assist_enabled BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS ix_contacts_yorik_assist
    ON contacts (yorik_assist_enabled)
    WHERE yorik_assist_enabled = TRUE;
