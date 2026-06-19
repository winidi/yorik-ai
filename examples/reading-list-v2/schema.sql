-- Reading list — owned schema for manifest v2.
--
-- `recommended_by_contact_id` is a soft FK to public.contacts. We
-- don't add a database FOREIGN KEY because public.contacts lives
-- in a different schema and apps shouldn't depend on Yorik core
-- row ids surviving across migrations; the app validates on read.

CREATE TABLE reading_items (
    id              BIGSERIAL PRIMARY KEY,
    user_id         UUID DEFAULT auth.uid()
                      REFERENCES public.user_profiles(id) ON DELETE CASCADE,
    url             TEXT NOT NULL,
    title           TEXT,
    status          TEXT NOT NULL DEFAULT 'unread'
                      CHECK (status IN ('unread', 'reading', 'read', 'skipped')),
    recommended_by_contact_id BIGINT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    read_at         TIMESTAMPTZ
);

CREATE INDEX idx_reading_user_status
    ON reading_items (user_id, status, created_at DESC);
