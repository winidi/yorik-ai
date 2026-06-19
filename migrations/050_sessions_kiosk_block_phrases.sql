-- Per-kiosk content filter for the slideshow.
--
-- Stores a JSON array of free-text phrases like
--   ["medicine", "prescription bottle", "receipt", "screenshot"]
-- that get fed one-by-one into Immich's CLIP-backed smart search
-- (/api/search/smart). The union of matching asset IDs is removed
-- from the slideshow before it's served. Empty / NULL = no filter.
--
-- TEXT (not JSON1) because SQLite's JSON support is build-flag
-- dependent and we read this once per slideshow refresh — parsing
-- a short string in Python is cheaper than depending on JSON1.
--
-- Per-device (sessions row) because different walls in the house
-- want different filters: kitchen wall hides receipts + medicine;
-- guest-room wall hides anything personal; office wall doesn't filter
-- at all. The phrases stay on the session row alongside album +
-- show_today so the kiosk policy lives in one place.

ALTER TABLE sessions ADD COLUMN kiosk_block_phrases TEXT;
