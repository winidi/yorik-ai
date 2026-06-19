-- Contact extraction proposals — Phase 1 of the "walk every Paperless
-- doc and propose contacts" feature. Parallel to
-- contact_enrichment_proposals (per-field proposals on existing
-- contacts, bottom-up); this one is top-down: each row is a
-- candidate NEW contact proposed from one paperless doc.
--
-- One row per (source_paperless_doc_id). The UNIQUE constraint is
-- load-bearing — the background worker uses it for idempotency
-- ("INSERT … ON CONFLICT DO NOTHING") so re-runs don't pile up
-- duplicate proposals when the worker ticks every 6 h.
--
-- match_candidate_id is the existing contact this proposal most
-- plausibly belongs to (NULL = looks brand new). admin's decide
-- endpoint reads it to pre-select the "merge into X" radio.
--
-- proposed_json carries the union of regex-extracted (IBAN, email,
-- phone, tax-id) AND LLM-extracted (display_name, business_name,
-- address_lines, salutation_pref) fields, keyed by `contacts`
-- column names so the accept-handler can INSERT/UPDATE directly
-- without per-field translation.
CREATE TABLE contact_extraction_proposals (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    source_paperless_doc_id INTEGER NOT NULL,
    -- Snapshot of what the extractor produced; immutable once written.
    -- Schema: {display_name, kind, business_name?, legal_name?,
    --          address_street?, address_postcode?, address_city?,
    --          address_country?, email?, phone?, iban?, tax_id?,
    --          salutation_pref?, source_snippet?}
    proposed_json           TEXT NOT NULL,
    -- Match against existing contacts. NULL = looks new.
    match_candidate_id      INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
    match_score             REAL,                       -- 0..1 from the matcher
    match_reason            TEXT,                       -- "iban", "name+city", "name", …
    status                  TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'accepted', 'rejected', 'merged', 'skipped')),
    -- Set when admin accepts/merges. NULL for pending/rejected.
    created_contact_id      INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at              TEXT,
    decided_by_user_id      INTEGER REFERENCES user_profiles(id) ON DELETE SET NULL,
    UNIQUE (source_paperless_doc_id)
);

CREATE INDEX ix_cxp_status   ON contact_extraction_proposals(status) WHERE status = 'pending';
CREATE INDEX ix_cxp_match    ON contact_extraction_proposals(match_candidate_id);
CREATE INDEX ix_cxp_created  ON contact_extraction_proposals(created_at DESC);
