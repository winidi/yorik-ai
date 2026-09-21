-- Calendar import (.ics file) and subscribed calendars (a secret iCal
-- address, e.g. Google's, mirrored one way into Yorik).
ALTER TABLE events    ADD COLUMN IF NOT EXISTS ical_uid  TEXT;       -- UID (+ occurrence) of the source event
ALTER TABLE calendars ADD COLUMN IF NOT EXISTS read_only INTEGER NOT NULL DEFAULT 0;  -- 1 = a mirror, nobody edits it here
CREATE INDEX IF NOT EXISTS idx_events_calendar_ical_uid ON events (calendar_id, ical_uid);

CREATE TABLE IF NOT EXISTS calendar_feeds (
    id            BIGSERIAL PRIMARY KEY,
    owner_user_id UUID    NOT NULL,
    calendar_id   INTEGER NOT NULL,
    url_enc       TEXT    NOT NULL,      -- the address is a secret: Fernet-encrypted
    url_host      TEXT    NOT NULL,      -- shown in the UI instead of the address
    etag          TEXT,
    last_sync_at  TEXT,
    last_status   TEXT,                  -- ok | error
    last_error    TEXT,
    event_count   INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL
);
