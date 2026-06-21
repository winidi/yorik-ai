-- Per-user contact preferences. Replaces the global
-- contacts.yorik_assist_enabled flag with a (contact_id, user_id)
-- join so two users sharing a contact can independently opt in or
-- out of AI assistance for messages from that contact.
--
-- Why: in business contexts (e.g. therapist + office manager sharing
-- a patient contact) and any household where two people have
-- different relationships with the same shared contact, a single
-- contact-level flag is the wrong axis. It also makes the audit
-- trail murky ("who enabled AI for this contact?") for GDPR
-- purposes. Per-user prefs solve both.
--
-- Default still OFF — sovereignty story stands.

CREATE TABLE IF NOT EXISTS contact_user_prefs (
    contact_id            BIGINT  NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    user_id               TEXT    NOT NULL,
    yorik_assist_enabled  BOOLEAN NOT NULL DEFAULT FALSE,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (contact_id, user_id)
);

CREATE INDEX IF NOT EXISTS ix_contact_user_prefs_user
    ON contact_user_prefs (user_id, yorik_assist_enabled)
    WHERE yorik_assist_enabled = TRUE;

-- Backfill: every existing contact whose global flag is TRUE gets
-- a row attributed to every admin user. In Phase 0 this is just the
-- single platform_admin; idempotent if the migration re-runs (PK
-- prevents duplicates). Members onboarded later won't inherit —
-- they start with no rows = default off, which is the right
-- privacy default.
INSERT INTO contact_user_prefs (contact_id, user_id, yorik_assist_enabled)
SELECT c.id, u.id, TRUE
  FROM contacts c
  CROSS JOIN user_profiles u
 WHERE c.yorik_assist_enabled = TRUE
   AND u.role IN ('admin', 'platform_admin')
 ON CONFLICT (contact_id, user_id) DO NOTHING;

-- Drop the now-obsolete column. The index referencing it goes
-- with the column automatically.
ALTER TABLE contacts DROP COLUMN IF EXISTS yorik_assist_enabled;
