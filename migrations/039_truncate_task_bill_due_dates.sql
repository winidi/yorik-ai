-- Backfill: tasks.due_date and bills.due_date are date-granularity
-- fields. The skill validators (add_task / update_task / add_bill)
-- only checked due_date[:10] against YYYY-MM-DD but stored the full
-- string verbatim. When the LLM produced an ISO timestamp
-- ("2026-06-03T17:00:00"), the full value landed in the DB and the
-- frontend's `new Date(due_date + "T00:00:00")` rendered "Invalid
-- Date" in task cards.
--
-- Trim every existing row to the date prefix. Idempotent — anything
-- already a bare YYYY-MM-DD is unaffected by substr(_, 1, 10).
UPDATE tasks SET due_date = substr(due_date, 1, 10)
 WHERE due_date IS NOT NULL AND length(due_date) > 10;

UPDATE bills SET due_date = substr(due_date, 1, 10)
 WHERE due_date IS NOT NULL AND length(due_date) > 10;
