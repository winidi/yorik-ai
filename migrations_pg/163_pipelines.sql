-- Pipelines: Yorik follows a matter until it is done (stage 1).
-- See docs/plans/2026-09-25-pipelines.md.
--
-- A pipeline belongs to the person who created it, and only to them:
-- no space, no share, no admin exception (the rule since the
-- permissions audit of 2026-09-22). The engine is general; the first
-- kind is `nachfassen` (follow up on a sent mail until an answer comes).
--
-- Times the planner compares are TIMESTAMPTZ, not the TEXT columns
-- older tables use, so "due" never depends on the session time zone.

CREATE TABLE IF NOT EXISTS pipelines (
    id              BIGSERIAL PRIMARY KEY,
    owner_user_id   UUID NOT NULL,
    kind            TEXT NOT NULL,                      -- 'nachfassen', later more
    title           TEXT NOT NULL,
    goal            TEXT,                               -- what counts as done, in words
    -- entwurf | laeuft | pausiert | erledigt | abgebrochen
    state           TEXT NOT NULL DEFAULT 'entwurf',
    mode            TEXT NOT NULL DEFAULT 'begleitet',  -- begleitet | autonom (stage 4)
    -- Why the person is needed right now (NULL = nothing to do):
    -- vielleicht | kann_nicht_pruefen | schritt_faellig | uebergabe |
    -- selbst_geantwortet | versand_unklar | person_aus
    attention       TEXT,
    attention_json  TEXT,                               -- details for the attention card
    origin_json     TEXT NOT NULL DEFAULT '{}',         -- the first message (kind-specific)
    features_json   TEXT NOT NULL DEFAULT '{}',         -- what the answer is recognised by
    config_json     TEXT NOT NULL DEFAULT '{}',         -- send window etc.
    result_json     TEXT,                               -- set when done
    since_at        TIMESTAMPTZ NOT NULL DEFAULT now(), -- answers count from here
    next_run_at     TIMESTAMPTZ,
    locked_until    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_pipelines_owner ON pipelines (owner_user_id, state);
CREATE INDEX IF NOT EXISTS ix_pipelines_due ON pipelines (next_run_at) WHERE state = 'laeuft';

-- The steps after the first message. `after_days` counts from the
-- previous step's send (or from the first message). An approval is
-- bound to the text it approved: editing a step clears it.
CREATE TABLE IF NOT EXISTS pipeline_steps (
    id              BIGSERIAL PRIMARY KEY,
    pipeline_id     BIGINT NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
    position        INTEGER NOT NULL,
    action          TEXT NOT NULL,                      -- 'mail_senden' | 'uebergabe'
    after_days      INTEGER NOT NULL,
    payload_json    TEXT NOT NULL DEFAULT '{}',         -- subject, body, to
    approved_at     TIMESTAMPTZ,
    approved_hash   TEXT,
    status          TEXT NOT NULL DEFAULT 'offen',      -- offen | erledigt
    done_at         TIMESTAMPTZ,
    UNIQUE (pipeline_id, position)
);

-- Every outward action goes through here first, with a fixed key. A
-- restart in the middle of a send never sends twice: a row left in
-- 'sendet' becomes 'unklar' and the person is asked.
CREATE TABLE IF NOT EXISTS pipeline_actions (
    id              BIGSERIAL PRIMARY KEY,
    pipeline_id     BIGINT NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
    step_id         BIGINT REFERENCES pipeline_steps(id) ON DELETE SET NULL,
    idem_key        TEXT NOT NULL UNIQUE,
    kind            TEXT NOT NULL,                      -- 'mail'
    status          TEXT NOT NULL,                      -- sendet | gesendet | fehler | unklar
    message_id      TEXT,                               -- the Message-ID we send with
    error           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    done_at         TIMESTAMPTZ
);

-- History: every check (with what was searched and found), every
-- action, every decision of the person.
CREATE TABLE IF NOT EXISTS pipeline_events (
    id              BIGSERIAL PRIMARY KEY,
    pipeline_id     BIGINT NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
    at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind            TEXT NOT NULL,                      -- pruefung | aktion | mensch | status
    text            TEXT NOT NULL,
    data_json       TEXT
);

CREATE INDEX IF NOT EXISTS ix_pipeline_events_pipeline ON pipeline_events (pipeline_id, at);

-- Pipelines are on for everyone; parents or admins can switch them off
-- for one person. A missing row means on.
CREATE TABLE IF NOT EXISTS pipeline_person_settings (
    user_id         UUID PRIMARY KEY,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    changed_by      UUID,
    changed_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
