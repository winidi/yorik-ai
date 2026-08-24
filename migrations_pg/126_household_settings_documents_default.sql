-- Seed the household-level default for new document uploads.
--
-- The SQLite series did this in 032; the Postgres schema (100) created
-- household_settings but never seeded the row, so fresh installs ran on
-- the hard-coded 'private' fallback in the upload path and the Settings
-- page showed the key as unset. Same precedence as before:
--   1. explicit ?visibility= on the upload
--   2. user_profiles.default_doc_visibility
--   3. household_settings.documents_default_visibility  (this row)
--   4. 'private'
INSERT INTO household_settings (key, value)
VALUES ('documents_default_visibility', 'private')
ON CONFLICT (key) DO NOTHING;
