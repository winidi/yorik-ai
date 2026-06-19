-- Habits app — owned schema for manifest v2 reference.
--
-- Two tables, both per-user. user_id defaults to auth.uid() when the
-- iframe writes via Supabase JS with a JWT; connector @operation
-- writes (which run as the postgres superuser) leave user_id NULL
-- and are visible only to platform_admin via the RLS policies below.

CREATE TABLE habits (
    id              BIGSERIAL PRIMARY KEY,
    user_id         UUID DEFAULT auth.uid()
                      REFERENCES public.user_profiles(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    target_per_week INTEGER NOT NULL DEFAULT 1
                      CHECK (target_per_week BETWEEN 1 AND 21),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE habit_completions (
    id          BIGSERIAL PRIMARY KEY,
    habit_id    BIGINT NOT NULL REFERENCES habits(id) ON DELETE CASCADE,
    user_id     UUID DEFAULT auth.uid()
                  REFERENCES public.user_profiles(id) ON DELETE CASCADE,
    completed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    note         TEXT
);

CREATE INDEX idx_habits_user ON habits (user_id);
CREATE INDEX idx_completions_habit_time
    ON habit_completions (habit_id, completed_at DESC);
