-- Per-user default document visibility — used by every Paperless upload
-- to auto-apply the right tag (private / business / shared) so users
-- don't have to remember to set it on each save.
--
-- Values:
--   'private'  — only the owner + admin can see (Paperless default)
--   'business' — visible to everyone with the 'business' tag granted
--                (employees in business mode, partners in family mode)
--   'shared'   — visible to the whole household / team
--
-- Default 'private' is the safest fallback — explicit opt-in to wider
-- visibility matches the rest of Yorik's permission posture.

ALTER TABLE user_profiles ADD COLUMN default_doc_visibility TEXT
    NOT NULL DEFAULT 'private';
