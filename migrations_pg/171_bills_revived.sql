-- Bills come back (Dirk 2026-10-02): a "Rechnungen" tab in Finance, the
-- chat skills again, and a bill proposed from a photographed or scanned
-- letter the way it already is from a mail. The table from the alpha
-- grows the columns the new life needs:
--   owner_user_id  who recorded it (their own mail or document) — row
--                  visibility like events/tasks, besides the space
--   payee          who is paid ("Stadtwerke Peine"), apart from the title
--   number         invoice number, the strongest bank match
--   source         email | paperless | manual | chat
--   source_ref     the mail id or Paperless document id
--   paid_at        the day it was paid (set by hand or by the bank match)
--   paid_by        hand | bank
--   bank_transaction_id  the booking that paid it
--   reminded_at    the last "due soon" notification, one per bill
-- bill_candidates remembers which mails and documents were already read
-- and proposed, so a re-index never proposes the same letter twice.
--
-- Additive only.

ALTER TABLE bills ADD COLUMN IF NOT EXISTS owner_user_id UUID;
ALTER TABLE bills ADD COLUMN IF NOT EXISTS payee TEXT;
ALTER TABLE bills ADD COLUMN IF NOT EXISTS number TEXT;
ALTER TABLE bills ADD COLUMN IF NOT EXISTS source TEXT;
ALTER TABLE bills ADD COLUMN IF NOT EXISTS source_ref BIGINT;
ALTER TABLE bills ADD COLUMN IF NOT EXISTS paid_at TEXT;
ALTER TABLE bills ADD COLUMN IF NOT EXISTS paid_by TEXT;
ALTER TABLE bills ADD COLUMN IF NOT EXISTS bank_transaction_id BIGINT;
ALTER TABLE bills ADD COLUMN IF NOT EXISTS reminded_at TEXT;

CREATE INDEX IF NOT EXISTS bills_open_due_idx ON bills (paid, due_date);

CREATE TABLE IF NOT EXISTS bill_candidates (
    source          TEXT NOT NULL,              -- email | paperless
    ref_id          BIGINT NOT NULL,
    user_id         UUID,
    is_bill         BOOLEAN NOT NULL DEFAULT FALSE,
    notification_id BIGINT,
    bill_id         BIGINT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source, ref_id)
);
