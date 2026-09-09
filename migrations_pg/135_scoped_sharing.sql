-- Scoped sharing: a membership in someone's personal space can be limited
-- to areas (tasks, calendar, contacts, documents). NULL = the whole space,
-- as before. This is how "Beate sees my tasks but not my documents" works.
ALTER TABLE space_members ADD COLUMN IF NOT EXISTS scope TEXT;   -- comma list: tasks,calendar,contacts,documents
