-- Migration 023: subtasks + recurring tasks
--
-- Two columns on the existing `tasks` table:
--
--   parent_task_id  — self-reference. NULL = top-level task. Non-null =
--                     this row is a sub-step of `parent_task_id`. ON
--                     DELETE CASCADE so removing a parent removes its
--                     checklist (matches the user's mental model:
--                     "I'm done with this whole thing"). The Tasks UI
--                     indents children under their parent and shows a
--                     progress count ("3/5 done") on the parent row.
--
--   recurrence_rule — free-form shorthand the backend understands:
--                     "daily", "weekly", "every 2 weeks", "monthly",
--                     "every Mon,Wed,Fri". When a recurring task is
--                     marked done, the backend materialises the next
--                     instance (new row, new due_date, recurrence_rule
--                     copied over). NULL = one-shot, no recurrence.
--                     Free-form intentionally — the LLM picks values
--                     during natural-language quick-capture and the
--                     parser in backend/tasks_recurrence.py handles
--                     the common forms.
--
-- Both nullable so legacy rows keep working unchanged.

ALTER TABLE tasks ADD COLUMN parent_task_id INTEGER
    REFERENCES tasks(id) ON DELETE CASCADE;

ALTER TABLE tasks ADD COLUMN recurrence_rule TEXT;

-- Index on parent_task_id so the "load subtasks for parent X" query
-- (one per parent shown in the UI) is a single indexed hit.
CREATE INDEX IF NOT EXISTS ix_tasks_parent
    ON tasks (parent_task_id);
