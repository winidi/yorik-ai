-- Phase E §13 — service_role needs access to public.* for Realtime.
--
-- Supabase Realtime evaluates row changes by SET ROLE-ing to whatever
-- the WebSocket subscriber's JWT says, then SELECT-ing the row to
-- decide what to push. service_role has BYPASSRLS, which skips
-- policies — but BYPASSRLS doesn't grant USAGE on schemas or SELECT
-- on tables. Without these grants, Realtime returns empty `record`
-- objects with `errors: ["Error 401: Unauthorized"]` — silent for
-- humans, broken for any service-role subscriber.
--
-- This is a Supabase distribution gap (not a Yorik bug); fixing it
-- in our migrations is the simplest path. The grants are
-- conservative: USAGE on public + SELECT on existing + default
-- privileges for future tables. We don't grant INSERT/UPDATE/DELETE
-- to service_role on public.* — Yorik FastAPI does those as
-- postgres (the superuser).

GRANT USAGE ON SCHEMA public TO service_role;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO service_role;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO service_role;
