-- A day's briefing snapshot is one person's day (their mail, calendar,
-- WhatsApp). It was stored once per (template, date), taken from the
-- first admin's perspective, and served to everyone who asked for a
-- past date (audit 2026-09-22, 2.8). Now one row per person.
ALTER TABLE briefing_snapshots
  ADD COLUMN IF NOT EXISTS user_id UUID;

-- Existing rows were taken as the first enabled 'admin' account (the
-- scheduler's choice); keep them as that person's, drop them if there
-- is no such account.
UPDATE briefing_snapshots
   SET user_id = (SELECT id FROM user_profiles WHERE role = 'admin' AND (disabled = 0 OR disabled IS NULL)
                  ORDER BY created_at LIMIT 1)
 WHERE user_id IS NULL;
DELETE FROM briefing_snapshots WHERE user_id IS NULL;

ALTER TABLE briefing_snapshots DROP CONSTRAINT IF EXISTS briefing_snapshots_template_id_target_date_key;
ALTER TABLE briefing_snapshots
  ADD CONSTRAINT briefing_snapshots_person_day UNIQUE (template_id, target_date, user_id);
