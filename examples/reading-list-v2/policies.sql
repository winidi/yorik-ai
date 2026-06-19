-- Reading list — RLS.
--
-- Same simple owner-or-platform-admin pattern as the other
-- references. The `recommended_by_contact_id` field is just a
-- value stored on the row; visibility is gated by user_id, so a
-- user can't see another user's items even if they happened to
-- store the same contact id.

ALTER TABLE reading_items ENABLE ROW LEVEL SECURITY;

CREATE POLICY reading_items_owner_all ON reading_items
    FOR ALL
    USING (
        user_id = auth.uid()
        OR yorik.role(auth.uid()) = 'platform_admin'
    )
    WITH CHECK (
        user_id = auth.uid()
        OR yorik.role(auth.uid()) = 'platform_admin'
    );
