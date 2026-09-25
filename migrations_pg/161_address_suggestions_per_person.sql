-- Address suggestions belong to the person whose messages or documents
-- they came from (2026-09-25).
--
-- contact_address_suggestions was keyed by contact only. Beate's scan of
-- her own WhatsApp, mail or Paperless documents for a household contact
-- stored address, excerpt and source id there, and the next person to
-- open that contact — or to ask the chat for its address — got her rows
-- back (audit docs/audits/2026-09-25-chat-sichtbarkeit.md, L6).
--
-- Rows are now written and read with the person's id. The rows already
-- there have no known owner; they are left in place and match nobody,
-- so no one sees them again and a fresh search fills the person's own.
ALTER TABLE contact_address_suggestions
  ADD COLUMN IF NOT EXISTS owner_user_id UUID;

CREATE INDEX IF NOT EXISTS contact_address_suggestions_contact_owner
  ON contact_address_suggestions (contact_id, owner_user_id);
