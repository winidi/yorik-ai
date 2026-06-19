-- Notes app — owned schema for manifest v2.
--
-- The installer (backend/app_schema_lifecycle.py) creates the
-- `app_yorik_notes` schema and runs this file with search_path set to
-- that schema, so `notes` here means `app_yorik_notes.notes`.
--
-- user_id defaults to auth.uid() so JWT-scoped clients writing through
-- PostgREST get the right owner without having to fill it in. RLS
-- (policies.sql) enforces that the writer can only see/write rows
-- where user_id matches their JWT.

CREATE TABLE notes (
    id          BIGSERIAL PRIMARY KEY,
    -- DEFAULT auth.uid() populates owner when the iframe writes via
    -- Supabase JS with the user's JWT. Connector-written notes (the
    -- @operation entrypoints below) run as the postgres superuser
    -- and leave user_id NULL — those are "agent notes," visible to
    -- platform_admin only via the RLS policy in policies.sql.
    user_id     UUID DEFAULT auth.uid()
                  REFERENCES public.user_profiles(id) ON DELETE CASCADE,
    body        TEXT NOT NULL,
    mood        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_notes_user_created ON notes (user_id, created_at DESC);
