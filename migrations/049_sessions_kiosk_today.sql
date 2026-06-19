-- Add an opt-in "show today's photos" mode to the kiosk slideshow.
--
-- When enabled, the /api/ambient/slideshow endpoint ALSO fetches the
-- photos taken today from the kiosk-bound user's Immich library and
-- shows them FIRST (newest-first cycling), then the curated album
-- photos. Photos in both sets get deduped by Immich asset id.
--
-- Default 0 = old behaviour (album-only). The family use case
-- ("walk past the wall, see what happened today") flips this to 1
-- and gets the dynamic-wall effect without sacrificing the
-- explicit-album consent model when they want it.
--
-- Per-device because different kiosks in the house can have
-- different policies — the living-room wall might show today's
-- photos automatically (family hangout vibe), while a guest-room
-- kiosk stays album-only (no surprises for visitors).

ALTER TABLE sessions ADD COLUMN kiosk_show_today_photos INTEGER NOT NULL DEFAULT 0;
