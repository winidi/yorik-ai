-- Persons as first-class contacts linked to a business they work for.
--
-- The previous extractor only ever produced one row per document: a
-- business OR a person. That meant a letter from "Bayerische
-- Beamten Versicherung AG" signed by Sachbearbeiterin Maria Schmidt
-- produced ONE contact (the business) and the signer was lost. To
-- model "Maria works for BBV" without a junction table we add:
--
--   first_name + last_name + role: the person's identity columns
--                                  (separate from display_name so
--                                  the UI can render "Maria Schmidt
--                                  · Sachbearbeiterin" cleanly and
--                                  searches work on either part).
--
--   employer_contact_id:           FK back to contacts.id — when
--                                  the person works for / is reached
--                                  through a business contact. NULL
--                                  for independent persons and for
--                                  business rows themselves.
--                                  ON DELETE SET NULL so deleting the
--                                  business doesn't cascade-delete
--                                  every employee (the person row
--                                  stays, just unlinked).
--
-- All four fields are nullable so existing rows are unaffected. A
-- person contact only requires first/last name when the extractor
-- emits one — the existing display_name path keeps working for the
-- non-extracted contacts (WhatsApp autocapture etc.).
--
-- Multiple persons per business handled by simply having multiple
-- person rows with the same employer_contact_id. No junction table
-- needed for the v1 case "list of contacts at this business" —
-- person-with-multiple-employers (consultant cases) is rare enough
-- to postpone.

ALTER TABLE contacts ADD COLUMN first_name TEXT;
ALTER TABLE contacts ADD COLUMN last_name TEXT;
ALTER TABLE contacts ADD COLUMN role TEXT;
ALTER TABLE contacts ADD COLUMN employer_contact_id INTEGER
    REFERENCES contacts(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_contacts_employer
    ON contacts(employer_contact_id)
    WHERE employer_contact_id IS NOT NULL;
