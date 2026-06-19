-- Scanned handwritten signature for letterheads.
--
-- Stored as a data URL (base64 PNG/JPEG) on the user_profiles row so
-- it's self-contained — no separate file storage, no broken-image
-- problems when the file gets moved, no extra path resolution.
-- A typical scanned signature is 50–150 KB; SQLite handles that fine.
--
-- Used by the Compose templates: when this field is set, the
-- signature block above the typed name renders the image instead of
-- the bare horizontal line. Per-user, so two adults sharing a Yorik
-- install each get their own signature in their letterheads.

ALTER TABLE user_profiles ADD COLUMN signature_data_url TEXT;
