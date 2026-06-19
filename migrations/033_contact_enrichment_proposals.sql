-- contact_enrichment_proposals
--
-- Per-contact, per-field structured proposals from the LLM-driven
-- contact enricher. The enricher walks emails / WhatsApp / Paperless
-- mentions for each contact and writes one row per (field, candidate)
-- so the edit-contact UI can pre-fill empty fields with the highest-
-- confidence value AND surface alternatives in a small dropdown.
--
-- Re-running the enricher on a contact wipes that contact's pending
-- proposals (status='pending') and re-inserts fresh ones. Accepted /
-- rejected proposals are kept for audit + so the user's choice
-- doesn't get re-proposed next pass.
--
-- field_name: any one of the editable Contact fields the enricher
--   produces — currently:
--     relation        salutation_pref    legal_name
--     birthday        language_pref      tax_id
--     kind            iban               notes
--     address         (special: stores JSON {line1,line2,postcode,city,country})
--   The field_name list is enforced by the enricher, not the schema,
--   so adding a new field doesn't need a migration.
--
-- source_kind: where this candidate was extracted from — used in the
--   UI hover preview. One of:
--     "email_signature" | "email_body" | "whatsapp" |
--     "paperless_doc"   | "vcard_import" | "manual"

CREATE TABLE IF NOT EXISTS contact_enrichment_proposals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id      INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    field_name      TEXT NOT NULL,
    proposed_value  TEXT NOT NULL,
    confidence      REAL NOT NULL DEFAULT 0.5,   -- 0.0..1.0
    source_kind     TEXT NOT NULL,
    source_ref      TEXT,                        -- e.g. paperless doc id, email msg id
    source_snippet  TEXT,                        -- short text the LLM extracted this from
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending | accepted | rejected
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at      TEXT
);

CREATE INDEX IF NOT EXISTS ix_cep_contact     ON contact_enrichment_proposals(contact_id);
CREATE INDEX IF NOT EXISTS ix_cep_contact_fld ON contact_enrichment_proposals(contact_id, field_name);
CREATE INDEX IF NOT EXISTS ix_cep_pending     ON contact_enrichment_proposals(status) WHERE status = 'pending';
