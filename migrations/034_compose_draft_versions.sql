-- compose_draft_versions
--
-- Linear history per draft so the inline-chat compose card can show
-- a version chip row and the user can step back if a refine wandered
-- in the wrong direction. Versions are immutable snapshots of
-- body_html plus the cause that produced them:
--
--   source='initial'   first save (from the compose_draft skill);
--                      always exactly one per draft
--   source='refine'    /api/compose/saved-draft/{id}/refine produced
--                      this; `instruction` holds what the user typed
--                      ("make it shorter", "use Sie form", …)
--   source='manual'    captured when the user explicitly snapshots
--                      after editing in the TipTap editor (not on
--                      every keystroke — too noisy)
--   source='restore'   user clicked a previous version chip; this
--                      row is the swap-back marker so the history
--                      stays auditable
--
-- Picking a previous version updates compose_drafts.body_html in
-- place (so subsequent refine calls work from THAT version) AND
-- appends a new 'restore' row pointing back to the picked one.

CREATE TABLE IF NOT EXISTS compose_draft_versions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id      INTEGER NOT NULL REFERENCES compose_drafts(id) ON DELETE CASCADE,
    body_html     TEXT NOT NULL,
    source        TEXT NOT NULL,                   -- initial | refine | manual | restore
    instruction   TEXT,                            -- refine: the user's instruction text
    restored_from INTEGER REFERENCES compose_draft_versions(id),  -- restore: the version we swapped to
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS ix_cdv_draft ON compose_draft_versions(draft_id, id);
