-- Yorik Phase E §4 — enable Realtime on curated app-facing tables.
--
-- Apps that include `realtime_subscriptions: [contacts, events, ...]`
-- in their manifest get postgres_changes events for those tables
-- through the supabase_realtime publication. RLS still applies —
-- a subscriber only sees changes to rows they could SELECT.
--
-- Idempotent — each ADD is wrapped in a BEGIN/EXCEPTION so re-runs
-- skip tables already in the publication.

DO $$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY[
    -- Core domain tables apps will commonly subscribe to.
    'contacts', 'events', 'tasks', 'bills',
    'calendars', 'workspaces', 'spaces', 'space_members',
    'event_attendees', 'contact_channels', 'contact_addresses',
    'notifications'
  ] LOOP
    BEGIN
      EXECUTE format('ALTER PUBLICATION supabase_realtime ADD TABLE public.%I', r);
    EXCEPTION
      WHEN duplicate_object THEN NULL;
      WHEN undefined_table THEN NULL;
    END;
  END LOOP;
END $$;
