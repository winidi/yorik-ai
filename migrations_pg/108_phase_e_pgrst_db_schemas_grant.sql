-- Phase E #36 — let postgres set pgrst.db_schemas dynamically.
--
-- PostgREST's db-schemas list is a custom GUC. Custom GUCs in
-- Postgres are by default settable only by superusers, and the
-- `postgres` role in Supabase is NOT a true superuser (it only has
-- BYPASSRLS). Without these grants, the Phase E app-install
-- lifecycle gets "permission denied to set parameter
-- pgrst.db_schemas" when it tries to ALTER ROLE authenticator SET
-- pgrst.db_schemas at install/uninstall time.
--
-- Granting SET (per-role / per-database) + ALTER SYSTEM (cluster
-- default) keeps the lifecycle entirely in the postgres role —
-- no privileged side-connection as supabase_admin needed.
--
-- Postgres 15+ syntax. Supabase ships 15.x.

GRANT SET ON PARAMETER pgrst.db_schemas TO postgres;
GRANT ALTER SYSTEM ON PARAMETER pgrst.db_schemas TO postgres;
