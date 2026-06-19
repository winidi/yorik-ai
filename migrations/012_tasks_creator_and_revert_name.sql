-- Two unrelated bits of housekeeping rolled into one migration:
--
-- 1. Revert the household calendar's name back to "Shared" per user
--    preference — they tried "Household" briefly and didn't like it.
--    Idempotent: only flips the still-default "Household" name.
--
-- 2. Add tasks.created_by_user_id so the calendar overlay filter can
--    show "tasks I created" on a user's Personal view, not just "tasks
--    assigned to me". Without this, a task you created without setting
--    assignees doesn't surface on your own view — it falls through into
--    the "unassigned bucket" that only shows when Shared is toggled on.
--
--    Backfill: where any assignee exists, prefer the first assignee.
--    Otherwise default to the lowest-id admin (matches the migration-010
--    pattern for picking a household admin).

UPDATE calendars
SET name = 'Shared'
WHERE kind = 'shared' AND name = 'Household';

ALTER TABLE tasks ADD COLUMN created_by_user_id INTEGER;

UPDATE tasks
SET created_by_user_id = COALESCE(
    (SELECT user_id FROM task_assignees
      WHERE task_id = tasks.id
      ORDER BY rowid LIMIT 1),
    (SELECT id FROM user_profiles WHERE role = 'admin' ORDER BY id LIMIT 1)
)
WHERE created_by_user_id IS NULL;

CREATE INDEX IF NOT EXISTS ix_tasks_created_by ON tasks(created_by_user_id);
