-- What a receipt or invoice says, read once by the model and kept:
-- amount, date, paid or open, payment method. payments_to matches these
-- against bank bookings, so "was hab ich für Claude bezahlt" counts the
-- receipt paid by another card and does not count a booked one twice
-- (chat rerun 2026-09-27). Keyed by source and row; text_hash says
-- which text was read, a changed text is read again.
--
-- Additive only: one new table.

CREATE TABLE IF NOT EXISTS receipt_facts (
    source       TEXT NOT NULL,            -- paperless | email
    ref_id       BIGINT NOT NULL,
    text_hash    TEXT NOT NULL,
    is_bill      BOOLEAN NOT NULL DEFAULT FALSE,
    amount_cents BIGINT,                   -- NULL when the amount could not be confirmed in the text
    currency     TEXT,
    bill_date    TEXT,                     -- YYYY-MM-DD
    paid         BOOLEAN,                  -- NULL: the text does not say
    method       TEXT,
    number       TEXT,
    payee        TEXT,
    read_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source, ref_id)
);
