-- First start of the all-in-one database (plain Postgres 16 + pgvector).
-- Yorik's migrations were written against Supabase's Postgres; this
-- provides the handful of things Supabase would: the auth schema that
-- user_profiles points at, the API roles the migrations grant to, the
-- realtime publication, and the extensions. Runs once, on an empty
-- volume (docker-entrypoint-initdb.d).
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon')          THEN CREATE ROLE anon NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN CREATE ROLE authenticated NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role')  THEN CREATE ROLE service_role NOLOGIN BYPASSRLS; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticator') THEN CREATE ROLE authenticator NOLOGIN NOINHERIT; END IF;
  -- Only named by migration 110's default privileges; never logs in.
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'supabase_admin') THEN CREATE ROLE supabase_admin NOLOGIN; END IF;
END $$;

-- The auth schema GoTrue would own. Yorik writes one row per person
-- here; RLS policies call auth.uid(), which is NULL for Yorik's own
-- connection (it bypasses RLS as the owner anyway).
CREATE SCHEMA IF NOT EXISTS auth;
CREATE TABLE IF NOT EXISTS auth.users (
  id UUID PRIMARY KEY,
  email TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE OR REPLACE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE AS $$ SELECT NULL::uuid $$;

-- Migration 104 adds tables to this publication (for third-party apps
-- with realtime); here it simply exists.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_publication WHERE pubname = 'supabase_realtime') THEN
    CREATE PUBLICATION supabase_realtime;
  END IF;
END $$;

-- Migration 000 is the pre-Postgres import bridge; on a fresh database
-- 100 is authoritative (scripts/bootstrap-supabase.sh skips 000 too).
-- Yorik's own runner applies whatever isn't recorded, so record it.
CREATE TABLE IF NOT EXISTS schema_migrations (
  version    INTEGER PRIMARY KEY,
  name       TEXT NOT NULL,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
);
INSERT INTO schema_migrations (version, name) VALUES (0, 'phase_d_init') ON CONFLICT DO NOTHING;
