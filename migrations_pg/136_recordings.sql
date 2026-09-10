-- Recordings: a conversation captured at the table (or in a meeting),
-- kept as audio on disk under data/recordings/<id>/, turned into a
-- transcript with speaker turns, and visible only to the people who
-- were there. The report a skill writes on top of the transcript lives
-- in report_json so the audio can be deleted later without losing it.

CREATE TABLE IF NOT EXISTS recordings (
    id                BIGSERIAL PRIMARY KEY,
    owner_user_id     UUID NOT NULL REFERENCES user_profiles(id) ON DELETE CASCADE,
    space_id          INTEGER,
    title             TEXT NOT NULL DEFAULT '',
    kind              TEXT NOT NULL DEFAULT 'conversation',   -- dinner | meeting | conversation
    status            TEXT NOT NULL DEFAULT 'recording'
                      CHECK (status IN ('recording', 'uploaded', 'processing', 'done', 'failed')),
    participants_json TEXT NOT NULL DEFAULT '[]',              -- [user_id, ...] chosen at start
    started_at        TEXT NOT NULL DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
    stop_requested_at TEXT,                                    -- set by chat/voice; the recording device finishes
    ended_at          TEXT,
    duration_s        REAL,
    chunks            INTEGER NOT NULL DEFAULT 0,
    error             TEXT,
    progress          TEXT,                                    -- last pipeline step, for the status view
    processed_at      TEXT,
    audio_deleted_at  TEXT,
    report_template   TEXT,
    report_json       TEXT,
    report_at         TEXT,
    created_at        TEXT NOT NULL DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
);
CREATE INDEX IF NOT EXISTS recordings_owner_idx ON recordings (owner_user_id, status);

CREATE TABLE IF NOT EXISTS recording_segments (
    id            BIGSERIAL PRIMARY KEY,
    recording_id  BIGINT NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    seq           INTEGER NOT NULL,
    start_s       REAL NOT NULL,
    end_s         REAL NOT NULL,
    speaker_label TEXT NOT NULL,          -- "Dirk" when identified, else "Sprecher 2"
    user_id       UUID REFERENCES user_profiles(id) ON DELETE SET NULL,
    text          TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS recording_segments_rec_idx ON recording_segments (recording_id, seq);
