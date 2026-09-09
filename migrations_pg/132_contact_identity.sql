-- Contact identity: proposals a human accepts, merges that can be undone,
-- and a tombstone status so a merged contact keeps its id.

ALTER TABLE contacts ADD COLUMN IF NOT EXISTS merged_into_id BIGINT REFERENCES contacts(id);

ALTER TABLE contacts DROP CONSTRAINT IF EXISTS contacts_status_check;
ALTER TABLE contacts ADD CONSTRAINT contacts_status_check
    CHECK (status IN ('active', 'pending', 'spam', 'archived', 'merged'));

CREATE TABLE IF NOT EXISTS contact_proposals (
    id               BIGSERIAL PRIMARY KEY,
    kind             TEXT NOT NULL CHECK (kind IN ('merge', 'add_channel')),
    contact_id       BIGINT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    other_contact_id BIGINT REFERENCES contacts(id) ON DELETE CASCADE,
    channel_kind     TEXT,
    channel_value    TEXT,
    reason           TEXT NOT NULL,
    evidence_json    TEXT,
    confidence       DOUBLE PRECISION NOT NULL DEFAULT 0.5,
    user_id          UUID,
    status           TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'rejected')),
    created_at       TEXT NOT NULL DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
    decided_at       TEXT,
    decided_by       TEXT,
    result_json      TEXT
);
CREATE INDEX IF NOT EXISTS contact_proposals_status_idx ON contact_proposals (status, created_at DESC);

CREATE TABLE IF NOT EXISTS contact_merges (
    id          BIGSERIAL PRIMARY KEY,
    keep_id     BIGINT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    drop_id     BIGINT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    reason      TEXT,
    moved_json  TEXT NOT NULL,
    decided_by  TEXT,
    created_at  TEXT NOT NULL DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
    undone_at   TEXT
);
