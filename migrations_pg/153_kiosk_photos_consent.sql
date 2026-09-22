-- "Show my photos on the household wall" is its own decision, apart from
-- the agenda consent: the wall's today-photos mode used to pull every
-- enabled person's Immich library (audit 2026-09-22, 4.4). People who
-- already put themselves on the wall keep their photos there.
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS kiosk_photos_consent INTEGER NOT NULL DEFAULT 0;
UPDATE user_profiles SET kiosk_photos_consent = COALESCE(kiosk_agenda_consent, 0);
