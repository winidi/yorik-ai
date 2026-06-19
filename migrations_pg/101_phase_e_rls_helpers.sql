-- Yorik Phase E §2 — RLS helper functions
--
-- These functions mirror backend/spaces.py + backend/auth.py one for one,
-- so PostgREST + Realtime + any third-party client hitting the database
-- with a user JWT sees exactly what the Yorik FastAPI endpoint would
-- return after applying spaces.row_filter().
--
-- Design notes:
--   * Functions live in a `yorik` schema. They're Yorik's contribution to
--     the database; app developers writing their own RLS policies on app
--     schemas reference `yorik.visible_spaces(auth.uid())` etc.
--   * `SECURITY DEFINER` is REQUIRED — without it, calling these from
--     inside an RLS policy on `contacts` would recurse on `contacts`
--     visibility itself (since the function reads `spaces` and
--     `space_members`, which would also be RLS-gated).
--   * `STABLE` lets Postgres cache the result within a single query plan.
--   * The functions take a `caller_uuid` argument rather than reading
--     `auth.uid()` directly so they're also callable from the Yorik
--     FastAPI backend with the service_role key.

CREATE SCHEMA IF NOT EXISTS yorik;
GRANT USAGE ON SCHEMA yorik TO anon, authenticated, postgres;


-- ─── yorik.role(uuid) → text ────────────────────────────────────────
-- The Yorik role string ('platform_admin' / 'admin' / 'member' /
-- 'restricted'). Reads user_profiles.role directly. Empty string when
-- the user doesn't exist (e.g. a JWT for a deleted user).

CREATE OR REPLACE FUNCTION yorik.role(caller_uuid uuid)
RETURNS text
LANGUAGE sql
SECURITY DEFINER
STABLE
SET search_path = public
AS $$
    SELECT COALESCE(role, '') FROM user_profiles WHERE id = caller_uuid;
$$;


-- ─── yorik.visible_spaces(uuid) → int[] ─────────────────────────────
-- Spaces the user can see. Mirrors
-- backend/spaces.py:user_visible_space_ids exactly:
--   * platform_admin → every space in every workspace
--   * admin (workspace admin) → spaces in workspaces the user owns,
--     PLUS personal space + space_members memberships
--   * everyone else → personal space + space_members memberships
-- Returns an empty array for an unknown / disabled user.

CREATE OR REPLACE FUNCTION yorik.visible_spaces(caller_uuid uuid)
RETURNS integer[]
LANGUAGE plpgsql
SECURITY DEFINER
STABLE
SET search_path = public
AS $$
DECLARE
    r text;
    result integer[];
BEGIN
    IF caller_uuid IS NULL THEN
        RETURN ARRAY[]::integer[];
    END IF;
    r := yorik.role(caller_uuid);
    IF r = 'platform_admin' THEN
        SELECT array_agg(id) INTO result FROM spaces;
    ELSIF r = 'admin' THEN
        SELECT array_agg(DISTINCT id) INTO result FROM (
            SELECT id FROM spaces
              WHERE workspace_id IN
                (SELECT id FROM workspaces WHERE owner_user_id = caller_uuid)
            UNION
            SELECT id FROM spaces WHERE owner_user_id = caller_uuid
            UNION
            SELECT space_id AS id FROM space_members WHERE user_id = caller_uuid
        ) sub;
    ELSE
        SELECT array_agg(DISTINCT id) INTO result FROM (
            SELECT id FROM spaces WHERE owner_user_id = caller_uuid
            UNION
            SELECT space_id AS id FROM space_members WHERE user_id = caller_uuid
        ) sub;
    END IF;
    RETURN COALESCE(result, ARRAY[]::integer[]);
END;
$$;


-- ─── yorik.can_write_in_space(uuid, integer) → boolean ──────────────
-- Whether the user has write access to a specific space. Mirrors
-- spaces.user_space_level + has_level('write'):
--   * platform_admin → always true
--   * admin → true for spaces in workspaces they own
--   * owner of a personal space → true
--   * space_members.level in ('write', 'admin') → true

CREATE OR REPLACE FUNCTION yorik.can_write_in_space(caller_uuid uuid, target_space_id integer)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
STABLE
SET search_path = public
AS $$
DECLARE
    r text;
BEGIN
    IF caller_uuid IS NULL OR target_space_id IS NULL THEN
        RETURN false;
    END IF;
    r := yorik.role(caller_uuid);
    IF r = 'platform_admin' THEN
        RETURN true;
    END IF;
    -- Workspace admin → admin on every space inside their workspaces.
    IF r = 'admin' AND EXISTS (
        SELECT 1 FROM spaces
        WHERE id = target_space_id
          AND workspace_id IN
            (SELECT id FROM workspaces WHERE owner_user_id = caller_uuid)
    ) THEN
        RETURN true;
    END IF;
    -- Personal-space owner.
    IF EXISTS (
        SELECT 1 FROM spaces WHERE id = target_space_id AND owner_user_id = caller_uuid
    ) THEN
        RETURN true;
    END IF;
    -- Explicit write-or-admin membership.
    RETURN EXISTS (
        SELECT 1 FROM space_members
        WHERE space_id = target_space_id AND user_id = caller_uuid
          AND level IN ('write', 'admin')
    );
END;
$$;


-- ─── yorik.row_share_check(uuid, text, integer, text) → boolean ────
-- True if there's an unrevoked row_shares entry granting the caller
-- the requested access on (table_name, row_id). Used to broaden
-- visibility past the space-based model — e.g. "share my doctor's
-- contact with Mom even though she's not in my space."
-- `want_access` is 'read' or 'write'.

-- row_id is BIGINT because the parent tables (contacts, events, tasks,
-- etc.) all have BIGSERIAL PKs in the Phase E bootstrap. Calls from
-- policies pass `id` directly — Postgres won't auto-coerce BIGINT to
-- INTEGER so the function must accept the wider type.
CREATE OR REPLACE FUNCTION yorik.row_share_check(
    caller_uuid uuid, t_name text, r_id bigint, want_access text
)
RETURNS boolean
LANGUAGE sql
SECURITY DEFINER
STABLE
SET search_path = public
AS $$
    SELECT EXISTS (
        SELECT 1 FROM row_shares
        WHERE user_id = caller_uuid
          AND table_name = t_name
          AND row_id = r_id
          AND (
              want_access = 'read'  AND level IN ('read', 'write', 'admin')
           OR want_access = 'write' AND level IN ('write', 'admin')
          )
    );
$$;


-- ─── yorik.is_event_attendee(uuid, bigint) → boolean ───────────────
-- Whether the caller is invited to a specific event. SECURITY DEFINER
-- so the lookup bypasses event_attendees' own RLS — otherwise the
-- events policy + event_attendees policy form a recursion cycle
-- (each references the other).
CREATE OR REPLACE FUNCTION yorik.is_event_attendee(caller_uuid uuid, ev_id bigint)
RETURNS boolean
LANGUAGE sql
SECURITY DEFINER
STABLE
SET search_path = public
AS $$
    SELECT EXISTS (
        SELECT 1 FROM event_attendees
        WHERE event_id = ev_id AND user_id = caller_uuid
    );
$$;


-- ─── yorik.users_in_my_workspaces(uuid) → uuid[] ───────────────────
-- All user_profiles.id values that share at least one workspace with
-- the caller (owner OR space member). SECURITY DEFINER so the
-- user_profiles RLS policy doesn't recurse on itself (the policy
-- needs to SELECT from user_profiles + workspaces + space_members
-- and we'd hit RLS on all of them again).
CREATE OR REPLACE FUNCTION yorik.users_in_my_workspaces(caller_uuid uuid)
RETURNS uuid[]
LANGUAGE plpgsql
SECURITY DEFINER
STABLE
SET search_path = public
AS $$
DECLARE
    result uuid[];
BEGIN
    IF caller_uuid IS NULL THEN
        RETURN ARRAY[]::uuid[];
    END IF;
    SELECT array_agg(DISTINCT u.id) INTO result FROM (
        -- Workspace owners whose workspace I'm in
        SELECT owner_user_id AS id FROM workspaces
        WHERE id IN (
            SELECT id FROM workspaces WHERE owner_user_id = caller_uuid
            UNION
            SELECT s.workspace_id FROM spaces s
            JOIN space_members sm ON sm.space_id = s.id
            WHERE sm.user_id = caller_uuid
        )
        UNION
        -- Space members whose space is in my workspaces
        SELECT sm.user_id AS id FROM space_members sm
        JOIN spaces s ON s.id = sm.space_id
        WHERE s.workspace_id IN (
            SELECT id FROM workspaces WHERE owner_user_id = caller_uuid
            UNION
            SELECT s2.workspace_id FROM spaces s2
            JOIN space_members sm2 ON sm2.space_id = s2.id
            WHERE sm2.user_id = caller_uuid
        )
        UNION
        -- Always include myself
        SELECT caller_uuid AS id
    ) u;
    RETURN COALESCE(result, ARRAY[]::uuid[]);
END;
$$;


-- ─── Make every function callable from anywhere ────────────────────
GRANT EXECUTE ON FUNCTION yorik.role(uuid)                                  TO anon, authenticated;
GRANT EXECUTE ON FUNCTION yorik.visible_spaces(uuid)                        TO anon, authenticated;
GRANT EXECUTE ON FUNCTION yorik.can_write_in_space(uuid, integer)           TO anon, authenticated;
GRANT EXECUTE ON FUNCTION yorik.row_share_check(uuid, text, bigint, text)   TO anon, authenticated;
GRANT EXECUTE ON FUNCTION yorik.is_event_attendee(uuid, bigint)             TO anon, authenticated;
GRANT EXECUTE ON FUNCTION yorik.users_in_my_workspaces(uuid)                TO anon, authenticated;
