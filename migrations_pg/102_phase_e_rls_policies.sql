-- Yorik Phase E §2 — Row-Level Security policies for every scoped table.
--
-- Each table gets:
--   ENABLE ROW LEVEL SECURITY
--   SELECT policy   — who can read which rows
--   INSERT policy   — what new rows the caller can write
--   UPDATE policy   — same as INSERT plus ownership / membership checks
--   DELETE policy   — same as UPDATE
--
-- All policies share the same shape:
--   * platform_admin is checked first via yorik.role(auth.uid())
--     and short-circuits the rest.
--   * everyone else flows through yorik.visible_spaces(auth.uid()) +
--     owner / created_by_user_id / row_shares matches.
--
-- The Yorik FastAPI backend bypasses these policies by connecting
-- with the service_role key — that's expected and correct
-- (connectors, the agent, background reconcilers all need privileged
-- access). User-facing app traffic via PostgREST + Realtime hits
-- these policies and sees the same data the Yorik UI does today.

-- ─── workspaces ─────────────────────────────────────────────────────
ALTER TABLE workspaces ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS workspaces_select ON workspaces;
CREATE POLICY workspaces_select ON workspaces FOR SELECT
  USING (
    yorik.role(auth.uid()) = 'platform_admin'
    OR owner_user_id = auth.uid()
    OR id IN (
      SELECT s.workspace_id FROM spaces s
      JOIN space_members sm ON sm.space_id = s.id
      WHERE sm.user_id = auth.uid()
    )
  );

DROP POLICY IF EXISTS workspaces_insert ON workspaces;
CREATE POLICY workspaces_insert ON workspaces FOR INSERT
  WITH CHECK (yorik.role(auth.uid()) = 'platform_admin');

DROP POLICY IF EXISTS workspaces_update ON workspaces;
CREATE POLICY workspaces_update ON workspaces FOR UPDATE
  USING (yorik.role(auth.uid()) = 'platform_admin' OR owner_user_id = auth.uid());

DROP POLICY IF EXISTS workspaces_delete ON workspaces;
CREATE POLICY workspaces_delete ON workspaces FOR DELETE
  USING (yorik.role(auth.uid()) = 'platform_admin');


-- ─── spaces ─────────────────────────────────────────────────────────
ALTER TABLE spaces ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS spaces_select ON spaces;
CREATE POLICY spaces_select ON spaces FOR SELECT
  USING (id = ANY (yorik.visible_spaces(auth.uid())));

DROP POLICY IF EXISTS spaces_insert ON spaces;
CREATE POLICY spaces_insert ON spaces FOR INSERT
  WITH CHECK (
    yorik.role(auth.uid()) IN ('platform_admin', 'admin')
    OR owner_user_id = auth.uid()  -- creating own personal space
  );

DROP POLICY IF EXISTS spaces_update ON spaces;
CREATE POLICY spaces_update ON spaces FOR UPDATE
  USING (
    yorik.role(auth.uid()) = 'platform_admin'
    OR owner_user_id = auth.uid()
    OR workspace_id IN (SELECT id FROM workspaces WHERE owner_user_id = auth.uid())
  );

DROP POLICY IF EXISTS spaces_delete ON spaces;
CREATE POLICY spaces_delete ON spaces FOR DELETE
  USING (yorik.role(auth.uid()) IN ('platform_admin', 'admin'));


-- ─── space_members ──────────────────────────────────────────────────
ALTER TABLE space_members ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS space_members_select ON space_members;
CREATE POLICY space_members_select ON space_members FOR SELECT
  USING (
    yorik.role(auth.uid()) = 'platform_admin'
    OR user_id = auth.uid()
    OR space_id = ANY (yorik.visible_spaces(auth.uid()))
  );

DROP POLICY IF EXISTS space_members_insert ON space_members;
CREATE POLICY space_members_insert ON space_members FOR INSERT
  WITH CHECK (yorik.can_write_in_space(auth.uid(), space_id));

DROP POLICY IF EXISTS space_members_update ON space_members;
CREATE POLICY space_members_update ON space_members FOR UPDATE
  USING (yorik.can_write_in_space(auth.uid(), space_id));

DROP POLICY IF EXISTS space_members_delete ON space_members;
CREATE POLICY space_members_delete ON space_members FOR DELETE
  USING (yorik.can_write_in_space(auth.uid(), space_id) OR user_id = auth.uid());


-- ─── row_shares ─────────────────────────────────────────────────────
ALTER TABLE row_shares ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS row_shares_select ON row_shares;
CREATE POLICY row_shares_select ON row_shares FOR SELECT
  USING (
    yorik.role(auth.uid()) = 'platform_admin'
    OR user_id = auth.uid()           -- the grantee
    OR shared_by_user_id = auth.uid() -- the granter
  );

DROP POLICY IF EXISTS row_shares_insert ON row_shares;
CREATE POLICY row_shares_insert ON row_shares FOR INSERT
  WITH CHECK (shared_by_user_id = auth.uid());

DROP POLICY IF EXISTS row_shares_update ON row_shares;
CREATE POLICY row_shares_update ON row_shares FOR UPDATE
  USING (shared_by_user_id = auth.uid());

DROP POLICY IF EXISTS row_shares_delete ON row_shares;
CREATE POLICY row_shares_delete ON row_shares FOR DELETE
  USING (shared_by_user_id = auth.uid() OR user_id = auth.uid());


-- ─── calendars ──────────────────────────────────────────────────────
ALTER TABLE calendars ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS calendars_select ON calendars;
CREATE POLICY calendars_select ON calendars FOR SELECT
  USING (
    yorik.role(auth.uid()) = 'platform_admin'
    OR owner_user_id = auth.uid()
    OR space_id = ANY (yorik.visible_spaces(auth.uid()))
  );

DROP POLICY IF EXISTS calendars_insert ON calendars;
CREATE POLICY calendars_insert ON calendars FOR INSERT
  WITH CHECK (
    yorik.role(auth.uid()) = 'platform_admin'
    OR owner_user_id = auth.uid()
    OR yorik.can_write_in_space(auth.uid(), space_id)
  );

DROP POLICY IF EXISTS calendars_update ON calendars;
CREATE POLICY calendars_update ON calendars FOR UPDATE
  USING (
    yorik.role(auth.uid()) = 'platform_admin'
    OR owner_user_id = auth.uid()
    OR yorik.can_write_in_space(auth.uid(), space_id)
  );

DROP POLICY IF EXISTS calendars_delete ON calendars;
CREATE POLICY calendars_delete ON calendars FOR DELETE
  USING (
    yorik.role(auth.uid()) = 'platform_admin'
    OR owner_user_id = auth.uid()
  );


-- ─── events ─────────────────────────────────────────────────────────
-- Mirrors backend/calendars.py:visible_event_filter. Events inherit
-- visibility through their calendar's space, plus the event_attendees
-- invitation channel (an invite is a separate visibility grant).
ALTER TABLE events ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS events_select ON events;
CREATE POLICY events_select ON events FOR SELECT
  USING (
    yorik.role(auth.uid()) = 'platform_admin'
    OR space_id = ANY (yorik.visible_spaces(auth.uid()))
    OR owner_user_id = auth.uid()
    -- calendar visibility — calendars' RLS doesn't reference events,
    -- so this query is safe (the inner SELECT just hits calendars RLS).
    OR calendar_id IN (
      SELECT c.id FROM calendars c
      WHERE c.space_id = ANY (yorik.visible_spaces(auth.uid()))
    )
    -- attendee check goes through SECURITY DEFINER helper so we don't
    -- recurse into event_attendees' RLS (which references events).
    OR yorik.is_event_attendee(auth.uid(), id)
    OR yorik.row_share_check(auth.uid(), 'events', id, 'read')
  );

DROP POLICY IF EXISTS events_insert ON events;
CREATE POLICY events_insert ON events FOR INSERT
  WITH CHECK (
    yorik.role(auth.uid()) = 'platform_admin'
    OR owner_user_id = auth.uid()
    OR yorik.can_write_in_space(auth.uid(), space_id)
  );

DROP POLICY IF EXISTS events_update ON events;
CREATE POLICY events_update ON events FOR UPDATE
  USING (
    yorik.role(auth.uid()) = 'platform_admin'
    OR owner_user_id = auth.uid()
    OR yorik.can_write_in_space(auth.uid(), space_id)
    OR yorik.row_share_check(auth.uid(), 'events', id, 'write')
  );

DROP POLICY IF EXISTS events_delete ON events;
CREATE POLICY events_delete ON events FOR DELETE
  USING (
    yorik.role(auth.uid()) = 'platform_admin'
    OR owner_user_id = auth.uid()
  );


-- ─── event_attendees ────────────────────────────────────────────────
ALTER TABLE event_attendees ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS event_attendees_select ON event_attendees;
CREATE POLICY event_attendees_select ON event_attendees FOR SELECT
  USING (
    yorik.role(auth.uid()) = 'platform_admin'
    OR user_id = auth.uid()
    OR event_id IN (SELECT id FROM events WHERE owner_user_id = auth.uid())
  );

DROP POLICY IF EXISTS event_attendees_insert ON event_attendees;
CREATE POLICY event_attendees_insert ON event_attendees FOR INSERT
  WITH CHECK (
    yorik.role(auth.uid()) = 'platform_admin'
    OR event_id IN (SELECT id FROM events WHERE owner_user_id = auth.uid())
  );

DROP POLICY IF EXISTS event_attendees_update ON event_attendees;
CREATE POLICY event_attendees_update ON event_attendees FOR UPDATE
  USING (
    yorik.role(auth.uid()) = 'platform_admin'
    OR user_id = auth.uid()  -- attendee responds to invite
    OR event_id IN (SELECT id FROM events WHERE owner_user_id = auth.uid())
  );

DROP POLICY IF EXISTS event_attendees_delete ON event_attendees;
CREATE POLICY event_attendees_delete ON event_attendees FOR DELETE
  USING (
    yorik.role(auth.uid()) = 'platform_admin'
    OR user_id = auth.uid()
    OR event_id IN (SELECT id FROM events WHERE owner_user_id = auth.uid())
  );


-- ─── tasks ──────────────────────────────────────────────────────────
ALTER TABLE tasks ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tasks_select ON tasks;
CREATE POLICY tasks_select ON tasks FOR SELECT
  USING (
    yorik.role(auth.uid()) = 'platform_admin'
    OR space_id = ANY (yorik.visible_spaces(auth.uid()))
    OR created_by_user_id = auth.uid()
    OR yorik.row_share_check(auth.uid(), 'tasks', id, 'read')
  );

DROP POLICY IF EXISTS tasks_insert ON tasks;
CREATE POLICY tasks_insert ON tasks FOR INSERT
  WITH CHECK (
    yorik.role(auth.uid()) = 'platform_admin'
    OR created_by_user_id = auth.uid()
    OR yorik.can_write_in_space(auth.uid(), space_id)
  );

DROP POLICY IF EXISTS tasks_update ON tasks;
CREATE POLICY tasks_update ON tasks FOR UPDATE
  USING (
    yorik.role(auth.uid()) = 'platform_admin'
    OR created_by_user_id = auth.uid()
    OR yorik.can_write_in_space(auth.uid(), space_id)
    OR yorik.row_share_check(auth.uid(), 'tasks', id, 'write')
  );

DROP POLICY IF EXISTS tasks_delete ON tasks;
CREATE POLICY tasks_delete ON tasks FOR DELETE
  USING (
    yorik.role(auth.uid()) = 'platform_admin'
    OR created_by_user_id = auth.uid()
  );


-- ─── contacts ───────────────────────────────────────────────────────
ALTER TABLE contacts ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS contacts_select ON contacts;
CREATE POLICY contacts_select ON contacts FOR SELECT
  USING (
    yorik.role(auth.uid()) = 'platform_admin'
    OR space_id = ANY (yorik.visible_spaces(auth.uid()))
    OR created_by_user_id = auth.uid()
    OR yorik.row_share_check(auth.uid(), 'contacts', id, 'read')
  );

DROP POLICY IF EXISTS contacts_insert ON contacts;
CREATE POLICY contacts_insert ON contacts FOR INSERT
  WITH CHECK (
    yorik.role(auth.uid()) = 'platform_admin'
    OR created_by_user_id = auth.uid()
    OR yorik.can_write_in_space(auth.uid(), space_id)
  );

DROP POLICY IF EXISTS contacts_update ON contacts;
CREATE POLICY contacts_update ON contacts FOR UPDATE
  USING (
    yorik.role(auth.uid()) = 'platform_admin'
    OR created_by_user_id = auth.uid()
    OR yorik.can_write_in_space(auth.uid(), space_id)
    OR yorik.row_share_check(auth.uid(), 'contacts', id, 'write')
  );

DROP POLICY IF EXISTS contacts_delete ON contacts;
CREATE POLICY contacts_delete ON contacts FOR DELETE
  USING (
    yorik.role(auth.uid()) = 'platform_admin'
    OR created_by_user_id = auth.uid()
  );


-- ─── contact_channels + contact_addresses ───────────────────────────
-- Same scope as the parent contact.
ALTER TABLE contact_channels  ENABLE ROW LEVEL SECURITY;
ALTER TABLE contact_addresses ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS contact_channels_all ON contact_channels;
CREATE POLICY contact_channels_all ON contact_channels FOR ALL
  USING (
    contact_id IN (SELECT id FROM contacts)  -- contacts RLS does the heavy lifting
  )
  WITH CHECK (
    contact_id IN (SELECT id FROM contacts)
  );

DROP POLICY IF EXISTS contact_addresses_all ON contact_addresses;
CREATE POLICY contact_addresses_all ON contact_addresses FOR ALL
  USING (
    contact_id IN (SELECT id FROM contacts)
  )
  WITH CHECK (
    contact_id IN (SELECT id FROM contacts)
  );


-- ─── bills ──────────────────────────────────────────────────────────
ALTER TABLE bills ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS bills_select ON bills;
CREATE POLICY bills_select ON bills FOR SELECT
  USING (
    yorik.role(auth.uid()) = 'platform_admin'
    OR space_id = ANY (yorik.visible_spaces(auth.uid()))
  );

DROP POLICY IF EXISTS bills_insert ON bills;
CREATE POLICY bills_insert ON bills FOR INSERT
  WITH CHECK (
    yorik.role(auth.uid()) = 'platform_admin'
    OR yorik.can_write_in_space(auth.uid(), space_id)
  );

DROP POLICY IF EXISTS bills_update ON bills;
CREATE POLICY bills_update ON bills FOR UPDATE
  USING (
    yorik.role(auth.uid()) = 'platform_admin'
    OR yorik.can_write_in_space(auth.uid(), space_id)
  );

DROP POLICY IF EXISTS bills_delete ON bills;
CREATE POLICY bills_delete ON bills FOR DELETE
  USING (
    yorik.role(auth.uid()) = 'platform_admin'
    OR yorik.can_write_in_space(auth.uid(), space_id)
  );


-- ─── agent_conversations ────────────────────────────────────────────
-- Chat conversations are strictly per-user — owners only see their own.
ALTER TABLE agent_conversations ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS agent_conversations_all ON agent_conversations;
CREATE POLICY agent_conversations_all ON agent_conversations FOR ALL
  USING (
    yorik.role(auth.uid()) = 'platform_admin'
    OR user_id = auth.uid()
  )
  WITH CHECK (
    yorik.role(auth.uid()) = 'platform_admin'
    OR user_id = auth.uid()
  );


-- ─── notifications ──────────────────────────────────────────────────
ALTER TABLE notifications ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS notifications_all ON notifications;
CREATE POLICY notifications_all ON notifications FOR ALL
  USING (
    yorik.role(auth.uid()) = 'platform_admin'
    OR user_id = auth.uid()
  )
  WITH CHECK (
    yorik.role(auth.uid()) = 'platform_admin'
    OR user_id = auth.uid()
  );


-- ─── user_profiles ──────────────────────────────────────────────────
-- Mirror find_user/find_person workspace-overlap clause. Users see
-- their own row + everyone they share a workspace with.
ALTER TABLE user_profiles ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS user_profiles_select ON user_profiles;
CREATE POLICY user_profiles_select ON user_profiles FOR SELECT
  USING (
    yorik.role(auth.uid()) = 'platform_admin'
    OR id = auth.uid()
    -- Workspace-overlap visibility via the SECURITY DEFINER helper —
    -- avoids the user_profiles policy referencing user_profiles
    -- (which would recurse the policy on itself).
    OR id = ANY (yorik.users_in_my_workspaces(auth.uid()))
  );

DROP POLICY IF EXISTS user_profiles_insert ON user_profiles;
CREATE POLICY user_profiles_insert ON user_profiles FOR INSERT
  WITH CHECK (yorik.role(auth.uid()) = 'platform_admin');

DROP POLICY IF EXISTS user_profiles_update ON user_profiles;
CREATE POLICY user_profiles_update ON user_profiles FOR UPDATE
  USING (
    yorik.role(auth.uid()) = 'platform_admin'
    OR id = auth.uid()  -- you can update your own profile
  );

DROP POLICY IF EXISTS user_profiles_delete ON user_profiles;
CREATE POLICY user_profiles_delete ON user_profiles FOR DELETE
  USING (yorik.role(auth.uid()) = 'platform_admin');


-- ─── sessions (legacy, owner-only) ──────────────────────────────────
-- These don't go through PostgREST anyway (we'll revoke in §3), but
-- defence-in-depth.
ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS sessions_all ON sessions;
CREATE POLICY sessions_all ON sessions FOR ALL
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());


-- ─── Connector + sync tables (workspace-scoped where applicable) ────
ALTER TABLE connector_credentials ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS connector_credentials_select ON connector_credentials;
CREATE POLICY connector_credentials_select ON connector_credentials FOR SELECT
  USING (
    yorik.role(auth.uid()) = 'platform_admin'
    OR workspace_id IN (SELECT id FROM workspaces WHERE owner_user_id = auth.uid())
  );

DROP POLICY IF EXISTS connector_credentials_modify ON connector_credentials;
CREATE POLICY connector_credentials_modify ON connector_credentials FOR ALL
  USING (yorik.role(auth.uid()) = 'platform_admin')
  WITH CHECK (yorik.role(auth.uid()) = 'platform_admin');


-- ─── Email tables (per-user owner_user_id model) ────────────────────
ALTER TABLE email_accounts    ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_folders     ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_messages    ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_drafts      ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_attachments ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS email_accounts_all ON email_accounts;
CREATE POLICY email_accounts_all  ON email_accounts  FOR ALL
  USING (owner_user_id = auth.uid() OR yorik.role(auth.uid()) = 'platform_admin')
  WITH CHECK (owner_user_id = auth.uid() OR yorik.role(auth.uid()) = 'platform_admin');

DROP POLICY IF EXISTS email_folders_all ON email_folders;
CREATE POLICY email_folders_all   ON email_folders   FOR ALL
  USING (account_id IN (SELECT id FROM email_accounts))
  WITH CHECK (account_id IN (SELECT id FROM email_accounts));

DROP POLICY IF EXISTS email_messages_all ON email_messages;
CREATE POLICY email_messages_all  ON email_messages  FOR ALL
  USING (folder_id IN (SELECT id FROM email_folders))
  WITH CHECK (folder_id IN (SELECT id FROM email_folders));

DROP POLICY IF EXISTS email_drafts_all ON email_drafts;
CREATE POLICY email_drafts_all    ON email_drafts    FOR ALL
  USING (message_id IN (SELECT id FROM email_messages))
  WITH CHECK (message_id IN (SELECT id FROM email_messages));

DROP POLICY IF EXISTS email_attachments_all ON email_attachments;
CREATE POLICY email_attachments_all ON email_attachments FOR ALL
  USING (message_id IN (SELECT id FROM email_messages))
  WITH CHECK (message_id IN (SELECT id FROM email_messages));


-- ─── WhatsApp tables (per-user owner_user_id) ───────────────────────
ALTER TABLE wa_chats           ENABLE ROW LEVEL SECURITY;
ALTER TABLE wa_messages        ENABLE ROW LEVEL SECURITY;
ALTER TABLE wa_drafts          ENABLE ROW LEVEL SECURITY;
ALTER TABLE wa_self_identity   ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS wa_chats_all ON wa_chats;
CREATE POLICY wa_chats_all   ON wa_chats   FOR ALL
  USING (owner_user_id = auth.uid() OR yorik.role(auth.uid()) = 'platform_admin')
  WITH CHECK (owner_user_id = auth.uid() OR yorik.role(auth.uid()) = 'platform_admin');

DROP POLICY IF EXISTS wa_messages_all ON wa_messages;
CREATE POLICY wa_messages_all ON wa_messages FOR ALL
  USING (
    chat_jid IN (SELECT chat_jid FROM wa_chats)
    OR yorik.role(auth.uid()) = 'platform_admin'
  )
  WITH CHECK (
    chat_jid IN (SELECT chat_jid FROM wa_chats)
    OR yorik.role(auth.uid()) = 'platform_admin'
  );

DROP POLICY IF EXISTS wa_drafts_all ON wa_drafts;
CREATE POLICY wa_drafts_all  ON wa_drafts  FOR ALL
  USING (owner_user_id = auth.uid() OR yorik.role(auth.uid()) = 'platform_admin')
  WITH CHECK (owner_user_id = auth.uid() OR yorik.role(auth.uid()) = 'platform_admin');

DROP POLICY IF EXISTS wa_self_identity_all ON wa_self_identity;
CREATE POLICY wa_self_identity_all ON wa_self_identity FOR ALL
  USING (owner_user_id = auth.uid() OR yorik.role(auth.uid()) = 'platform_admin')
  WITH CHECK (owner_user_id = auth.uid() OR yorik.role(auth.uid()) = 'platform_admin');
