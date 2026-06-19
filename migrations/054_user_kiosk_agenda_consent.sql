-- Per-user consent for "show my appointments on the household wall."
--
-- The kiosk agenda (swipe-right on /ambient) shows today's events
-- aggregated across every household member who has opted in. Default
-- is OFF — nobody's calendar appears on the wall without the owner
-- explicitly enabling it in Settings → Profile.
--
-- Column name avoids "show_in_kiosk" to keep the intent ("agenda
-- listing on the wall") distinct from the photo-on-kiosk fields.
ALTER TABLE user_profiles
ADD COLUMN kiosk_agenda_consent INTEGER NOT NULL DEFAULT 0;
