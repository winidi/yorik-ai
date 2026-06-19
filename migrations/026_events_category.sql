-- Migration 026: category column on events
--
-- A closed enum (family|business|drive|health|personal|social) the
-- LLM picks when creating/updating an event, so the calendar UI can
-- colour-code by category instead of by per-event hex.
--
-- Why not reuse the existing `color` column? `color` is free-form
-- hex (user can override per event). `category` is semantic — the
-- frontend maps category → palette, and the palette can be swapped
-- in one place without touching skills or stored data.
--
-- Drive-time buffer events (Anfahrt: …) inserted by block_travel_time
-- always get category='drive' so they're amber by default — matches
-- the "yellow for travel" ask. Backfilled below.

ALTER TABLE events ADD COLUMN category TEXT;

-- Backfill: any existing Anfahrt buffer (created by block_travel_time
-- before this migration) gets category='drive' so the colour kicks in
-- retroactively.
UPDATE events
   SET category = 'drive'
 WHERE category IS NULL
   AND (title LIKE 'Anfahrt:%' OR title LIKE 'Rückfahrt:%')
   AND notes LIKE '%[LINKED_TO=%';

-- No index needed — calendar queries already filter by date+calendar;
-- category is read out of the row, not searched.
