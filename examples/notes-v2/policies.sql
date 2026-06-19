-- Notes app — RLS for owned tables.
--
-- Pattern: each user sees only their own notes; admins (platform_admin
-- + workspace admin) see everything in their scope. We don't share
-- notes between users in v0.2 — that would mean adding row_shares
-- semantics in the policy, which we save for a follow-up.

ALTER TABLE notes ENABLE ROW LEVEL SECURITY;

CREATE POLICY notes_owner_all ON notes
    FOR ALL
    USING (
        user_id = auth.uid()
        OR yorik.role(auth.uid()) = 'platform_admin'
    )
    WITH CHECK (
        user_id = auth.uid()
        OR yorik.role(auth.uid()) = 'platform_admin'
    );
