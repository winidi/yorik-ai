-- A colour and a photo per household member. The colour is what the
-- calendar, the task columns and the family board show for that
-- person; the personal calendar follows it. avatar_at marks that a
-- photo exists under data/avatars/<user_id>.jpg (and busts caches).
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS color     TEXT;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS avatar_at TEXT;
