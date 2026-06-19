-- Phase 9.3: per-user contact sharing + per-household settings.
--
-- The Phase 9.2 ownership gate was too strict for the family case
-- ("we want all our contacts shared between us"). Two additions
-- here make sharing flexible:
--
--   1. contacts.allowed_roles already exists (text, default 'admin')
--      — role-based ACL. This migration doesn't touch it; the gate
--      now reads it as a second access path.
--
--   2. contact_shares: per-user explicit sharing. Owner shares contact
--      42 with user_id=7 specifically — bypasses both ownership and
--      role-allowlist gates for that user.
--
--   3. household_settings: key/value table for per-tenant config.
--      First user: `contacts_default_allowed_roles` — what
--      allowed_roles a new contact gets when add_contact doesn't
--      specify. Households flip it to 'admin,member,child' so the
--      whole family sees new contacts by default; businesses leave
--      it 'admin' so customer records start private.


-- ── contact_shares ──────────────────────────────────────────────────
-- Composite PK: a user can be on a contact's share list at most once.
-- can_edit=0 means view-only; the gate consults this flag.
CREATE TABLE contact_shares (
    contact_id        INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    user_id           INTEGER NOT NULL REFERENCES user_profiles(id) ON DELETE CASCADE,
    can_edit          INTEGER NOT NULL DEFAULT 1 CHECK (can_edit IN (0, 1)),
    shared_at         TEXT NOT NULL DEFAULT (datetime('now')),
    shared_by_user_id INTEGER REFERENCES user_profiles(id) ON DELETE SET NULL,
    PRIMARY KEY (contact_id, user_id)
);

-- Lookup-by-user is the hot path (find_contact filters "contacts I can
-- see"). Index on contact_id covers ACL checks when editing.
CREATE INDEX ix_contact_shares_user    ON contact_shares (user_id);
CREATE INDEX ix_contact_shares_contact ON contact_shares (contact_id);


-- ── household_settings ──────────────────────────────────────────────
-- Generic key/value store. One row per setting. Values are TEXT so
-- callers parse the type themselves (avoids per-setting columns when
-- new settings show up).
CREATE TABLE household_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by_user_id INTEGER REFERENCES user_profiles(id) ON DELETE SET NULL
);

-- Seed the default contact-sharing setting. 'admin' keeps the Phase
-- 9.2 behaviour (private by default); a family admin can change it
-- via update_household_setting('contacts_default_allowed_roles',
-- 'admin,member,child').
INSERT INTO household_settings (key, value) VALUES
    ('contacts_default_allowed_roles', 'admin');
