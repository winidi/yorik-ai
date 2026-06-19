-- Yorik Phase E §3 — PostgREST surface curation.
--
-- Out-of-the-box, PostgREST exposes every table in `public` to the
-- `authenticated` role (we granted SELECT/INSERT/UPDATE/DELETE in
-- §2). Some of those tables are not app-facing — agent telemetry,
-- session state, operator-only logs, connector credentials. Revoke
-- access from authenticated + anon so they're invisible via
-- /rest/v1/* even though they live in public.
--
-- Yorik FastAPI continues to write/read them as the postgres
-- superuser (BYPASSRLS, can read anything regardless of grants).

DO $$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY[
    -- legacy server-side sessions, cookies-only
    'sessions',
    -- operator-only stack traces / telemetry
    'error_log', 'skill_invocations', 'agent_message_traces',
    'turn_feedback', 'briefing_snapshots',
    'pending_actions', 'skill_decisions',
    'web_visits',
    -- holds encrypted credentials (Yorik handles via service role)
    'connector_credentials',
    -- kiosk auth — bypasses normal sessions
    'trusted_kiosk_devices',
    -- migration ledger
    'schema_migrations',
    -- FTS shadow tables (we don't want them in the public API)
    'wa_messages_fts', 'email_messages_fts',
    -- admin/triage tables, not app-facing
    'household_settings',
    'contact_extraction_proposals', 'contact_enrichment_proposals',
    'contact_address_suggestions'
  ] LOOP
    BEGIN
      EXECUTE format('REVOKE ALL ON public.%I FROM authenticated, anon', r);
    EXCEPTION WHEN undefined_table THEN
      -- This particular table doesn't exist on this install — skip
      -- silently so the migration is idempotent across Yorik
      -- variants (some installs may not have whatsapp / email tables).
      NULL;
    END;
  END LOOP;
END $$;
