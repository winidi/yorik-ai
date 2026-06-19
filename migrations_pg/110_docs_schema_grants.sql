-- Phase F.x: grant docs schema to runtime roles.
--
-- The 'docs' schema is created in 100_phase_e_init.sql but no
-- ALTER DEFAULT PRIVILEGES was set for it. Result: every CREATE TABLE
-- in docs (documents, document_chunks, paperless_chunks, wa_chunks)
-- inherits no grants, and the FastAPI pool (connecting as `postgres`)
-- gets `permission denied for table` on every write.
--
-- The public schema works because Supabase pre-configures
-- pg_default_acl for it; we replicate that here for docs.

GRANT USAGE ON SCHEMA docs TO postgres, anon, authenticated, service_role;
GRANT ALL ON ALL TABLES    IN SCHEMA docs TO postgres, anon, authenticated, service_role;
GRANT ALL ON ALL SEQUENCES IN SCHEMA docs TO postgres, anon, authenticated, service_role;
GRANT ALL ON ALL FUNCTIONS IN SCHEMA docs TO postgres, anon, authenticated, service_role;

-- Future tables created in this schema by either privileged role.
ALTER DEFAULT PRIVILEGES IN SCHEMA docs
  GRANT ALL ON TABLES    TO postgres, anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA docs
  GRANT ALL ON SEQUENCES TO postgres, anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA docs
  GRANT ALL ON FUNCTIONS TO postgres, anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA docs
  GRANT ALL ON TABLES    TO postgres, anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA docs
  GRANT ALL ON SEQUENCES TO postgres, anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA docs
  GRANT ALL ON FUNCTIONS TO postgres, anon, authenticated, service_role;
