-- Habits — RLS. Same simple owner-or-platform-admin pattern as
-- the notes-v2 reference; reproduced verbatim so this app stands
-- alone as a copy-paste starting point for new authors.

ALTER TABLE habits ENABLE ROW LEVEL SECURITY;
ALTER TABLE habit_completions ENABLE ROW LEVEL SECURITY;

CREATE POLICY habits_owner_all ON habits
    FOR ALL
    USING (
        user_id = auth.uid()
        OR yorik.role(auth.uid()) = 'platform_admin'
    )
    WITH CHECK (
        user_id = auth.uid()
        OR yorik.role(auth.uid()) = 'platform_admin'
    );

CREATE POLICY completions_owner_all ON habit_completions
    FOR ALL
    USING (
        user_id = auth.uid()
        OR yorik.role(auth.uid()) = 'platform_admin'
    )
    WITH CHECK (
        user_id = auth.uid()
        OR yorik.role(auth.uid()) = 'platform_admin'
    );
