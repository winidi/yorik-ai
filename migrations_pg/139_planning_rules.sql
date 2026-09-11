-- Personal planning rules ("Küche macht Beate", "vormittags Deep Work",
-- "höchstens sechs Punkte"), free text, fed into every day-planning run.
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS planning_rules TEXT;
