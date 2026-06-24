-- Active-timer columns on tasks.
--
-- started_at:     ISO timestamp string (matches the format SQLite emits
--                 from datetime('now') so app-level string comparisons
--                 work the same as the existing created_at column).
--                 NULL = task is not currently running. At most one row
--                 per user has a non-NULL value; the /api/tasks/{id}/start
--                 endpoint enforces this by stopping any other running
--                 task this user owns.
-- actual_minutes: accumulated wall-clock minutes folded in on every
--                 stop and on the done-flip via PATCH. NULL until the
--                 first stop. Future LLM estimation will learn from
--                 (estimated_minutes, actual_minutes) pairs.

ALTER TABLE tasks ADD COLUMN IF NOT EXISTS started_at TEXT;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS actual_minutes INTEGER;
