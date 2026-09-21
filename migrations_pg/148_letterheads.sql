-- A person's letterhead for the "Schreiben" app: how their letters,
-- invoices and quotes look. Several per person are possible (private,
-- business); one is the default. `data` is a JSON document, see
-- backend/documents/letterhead.py (FIELDS).
CREATE TABLE IF NOT EXISTS letterheads (
    id         BIGSERIAL PRIMARY KEY,
    user_id    UUID    NOT NULL,
    name       TEXT    NOT NULL DEFAULT 'Standard',
    is_default INTEGER NOT NULL DEFAULT 0,
    data       TEXT    NOT NULL DEFAULT '{}',
    logo_at    TEXT,
    created_at TEXT NOT NULL DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
    updated_at TEXT NOT NULL DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
);
CREATE INDEX IF NOT EXISTS ix_letterheads_user ON letterheads(user_id);
