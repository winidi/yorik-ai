-- Yorik 1.0 suggestion engine — storage for what Yorik proposes
-- when it analyses an incoming message in the context of everything
-- it knows about the sender.
--
-- Three tables, lean by design:
--
--   suggestion_runs   one row per analyse_message call, regardless of
--                     how many suggestions it produced (could be 0).
--                     Lets us measure throughput / error rates without
--                     joining through suggestions.
--
--   suggestions       one row per emitted card. type names the
--                     plugin (suggestion type) that produced it.
--                     payload_json is the type-specific args the
--                     handler will dispatch on Accept.
--
--   suggestion_evidence  the "Because:" chips in the UI. The LLM
--                     cites which calendar event / email / task /
--                     etc. it used to justify a suggestion; we
--                     persist those references so the user can
--                     click through to verify in 2 clicks.
--
-- Status machine on suggestions:
--   pending   → emitted by the LLM, awaiting user action
--   accepted  → user clicked Accept; handler ran
--   edited    → user clicked Accept after editing the payload
--   skipped   → user clicked Skip (feedback for prompt iteration)
--   dismissed → suggestion expired or context changed; auto-cleaned

CREATE TABLE IF NOT EXISTS suggestion_runs (
    id            BIGSERIAL PRIMARY KEY,
    owner_user_id UUID NOT NULL REFERENCES user_profiles(id) ON DELETE CASCADE,
    -- Which source / modality triggered this run. 'email' for MVP;
    -- 'whatsapp' / 'telegram' / 'calendar' / etc. follow.
    source_kind   TEXT NOT NULL,
    -- Foreign key into the source's primary table. Generic INT so we
    -- don't need a separate column per modality. The caller knows
    -- which table to JOIN based on source_kind.
    source_id     BIGINT NOT NULL,
    -- Resolved canonical contact at the time of analysis. NULL when
    -- the sender couldn't be matched to a contact (suggestion engine
    -- skips in that case but logs the run for diagnostics).
    contact_id    BIGINT REFERENCES contacts(id) ON DELETE SET NULL,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at   TIMESTAMPTZ,
    -- 'running' | 'done' | 'error' | 'skipped' (toggles off, no contact, etc)
    status        TEXT NOT NULL DEFAULT 'running',
    -- Free-text reason for 'skipped' or 'error' (e.g. "global toggle off",
    -- "LLM unreachable"). Useful for the activity view + debugging.
    skip_reason   TEXT,
    error         TEXT
);

CREATE INDEX IF NOT EXISTS ix_suggestion_runs_owner_started
    ON suggestion_runs (owner_user_id, started_at DESC);
CREATE INDEX IF NOT EXISTS ix_suggestion_runs_source
    ON suggestion_runs (source_kind, source_id);


CREATE TABLE IF NOT EXISTS suggestions (
    id            BIGSERIAL PRIMARY KEY,
    run_id        BIGINT NOT NULL REFERENCES suggestion_runs(id) ON DELETE CASCADE,
    owner_user_id UUID NOT NULL REFERENCES user_profiles(id) ON DELETE CASCADE,
    -- Plugin contract: the type name corresponds to a registered
    -- SuggestionType. Engine looks up the handler from the registry
    -- on Accept. Adding a new type = registering it; no schema change.
    type          TEXT NOT NULL,
    -- Type-specific payload. Validated against the type's payload_schema
    -- BEFORE persisting (the LLM is constrained to emit only fields
    -- the schema allows, but we validate again here for defense).
    payload_json  TEXT NOT NULL,
    -- 'low' | 'medium' | 'high'. Engine drops 'low' by default but
    -- the per-user confidence threshold can lower the bar.
    confidence    TEXT NOT NULL DEFAULT 'medium',
    -- One short sentence the LLM produced explaining the suggestion.
    -- Shown above the evidence chips: "Yorik: ..."
    reason        TEXT,
    -- pending | accepted | edited | skipped | dismissed
    status        TEXT NOT NULL DEFAULT 'pending',
    -- When the user edited the payload before accepting, the final
    -- payload they actually dispatched goes here for the activity log.
    resolved_payload_json TEXT,
    resolved_at   TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_suggestions_run
    ON suggestions (run_id);
CREATE INDEX IF NOT EXISTS ix_suggestions_owner_pending
    ON suggestions (owner_user_id, created_at DESC)
    WHERE status = 'pending';


CREATE TABLE IF NOT EXISTS suggestion_evidence (
    id            BIGSERIAL PRIMARY KEY,
    suggestion_id BIGINT NOT NULL REFERENCES suggestions(id) ON DELETE CASCADE,
    -- Modality-tagged reference. Mirrors source_kind on suggestion_runs
    -- so we can JOIN to the right table. Examples in MVP:
    --   'calendar_event'   → events.id
    --   'email_message'    → email_messages.id
    --   'task'             → tasks.id
    --   'contact'          → contacts.id
    --   'wa_message'       → wa_messages.msg_id  (string, stored as text)
    kind          TEXT NOT NULL,
    -- INT for FK-style refs (most cases). For wa_messages where the
    -- primary key is a text msg_id, we store the digits where possible
    -- and fall back to ref_text. Most evidence will use ref_id.
    ref_id        BIGINT,
    ref_text      TEXT,
    -- Pre-formatted human-readable snippet for the chip label. Saves
    -- a JOIN at render time. ~140 chars max.
    snippet       TEXT
);

CREATE INDEX IF NOT EXISTS ix_suggestion_evidence_suggestion
    ON suggestion_evidence (suggestion_id);
