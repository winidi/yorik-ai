-- Phase 12.1: per-household default visibility for new document uploads.
--
-- The household_settings table already exists (migration 031). This
-- migration seeds the document-default row. The upload path consults
-- it as a fallback when the calling user has no per-user
-- `default_doc_visibility` set.
--
-- Precedence (lowest wins on absence):
--   1. explicit `?visibility=` query param on the upload
--   2. user_profiles.default_doc_visibility (per-user pref)
--   3. household_settings.documents_default_visibility (per-tenant)
--   4. 'private' hardcoded fallback
--
-- Default is 'private' to preserve Phase 12 behaviour. A family flips
-- it to 'shared' so every new upload goes to the household group
-- without anyone having to set it per-user.

INSERT INTO household_settings (key, value) VALUES
    ('documents_default_visibility', 'private');
