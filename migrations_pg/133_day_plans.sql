-- Day planning: a task knows when it was finished, can point at the
-- calendar block reserved for it, and remembers which day plan put it
-- there (so re-planning updates instead of duplicating). Drafts of a
-- day live server-side so a plan can be iterated across chat turns.

ALTER TABLE tasks ADD COLUMN IF NOT EXISTS done_at    TEXT;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS event_id   BIGINT;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS plan_date  TEXT;      -- YYYY-MM-DD
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS plan_key   TEXT;      -- stable id of the plan item
CREATE INDEX IF NOT EXISTS tasks_plan_idx ON tasks (plan_date, plan_key);

ALTER TABLE events ADD COLUMN IF NOT EXISTS task_id   BIGINT;
ALTER TABLE events ADD COLUMN IF NOT EXISTS plan_date TEXT;
ALTER TABLE events ADD COLUMN IF NOT EXISTS plan_key  TEXT;
CREATE INDEX IF NOT EXISTS events_plan_idx ON events (plan_date, plan_key);

CREATE TABLE IF NOT EXISTS day_plans (
    id          BIGSERIAL PRIMARY KEY,
    user_id     UUID NOT NULL REFERENCES user_profiles(id) ON DELETE CASCADE,
    plan_date   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'applied', 'reviewed')),
    draft_json  TEXT NOT NULL,          -- {"items": [...], "notes": "..."}
    context_json TEXT,                  -- what the planner saw (fixed events, open tasks, carry-over, outside context)
    review_json TEXT,                   -- evening review, once written
    created_at  TEXT NOT NULL DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
    updated_at  TEXT NOT NULL DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
    applied_at  TEXT,
    UNIQUE (user_id, plan_date)
);
