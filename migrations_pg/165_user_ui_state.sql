-- Per-person UI memory that must survive reloads and devices: whether
-- the first-run tour ran, whether the Home checklist was hidden, which
-- one-time app hints were seen. JSON text, read and written only by
-- backend/setup_checklist.py. Additive: one nullable column.
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS ui_state TEXT;
