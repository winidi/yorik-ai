-- Finance app: bank accounts (FinTS, read-only) + synced transactions.
-- See docs/plans/2026-09-25-finanzen.md.
--
-- Visibility follows the existing spaces model (backend/spaces.py):
--   space_id NULL          -> private, only owner_user_id (+ row_shares)
--   space_id = Finance space -> shared with everyone in that space
-- bank_transactions has no owner/space columns of its own; visibility
-- is always resolved by joining through its bank_accounts row, same
-- as recordings resolve through their parent row rather than copying
-- ACL columns onto every child row.

CREATE TABLE IF NOT EXISTS bank_accounts (
    id                BIGSERIAL PRIMARY KEY,
    owner_user_id     UUID NOT NULL,
    space_id          INTEGER,                 -- NULL = private; else a shared space (e.g. the seeded "Finance" space)
    display_name      TEXT NOT NULL,            -- "Sparkasse", "ING gemeinsam", user-chosen
    bank_url          TEXT NOT NULL,            -- FinTS server URL (hbci-zka.de lookup)
    blz               TEXT NOT NULL,
    login_name        TEXT NOT NULL,            -- online-banking username/Zugangsnummer
    iban              TEXT,                     -- filled in after the first successful sync
    product_id        TEXT,                     -- registered FinTS Produkt-ID once it arrives
    credential_key    TEXT NOT NULL,            -- credential_store key, e.g. 'bank:<id>' (holds the PIN)
    enabled           INTEGER NOT NULL DEFAULT 1,
    last_synced_at    TEXT,
    last_sync_error   TEXT,
    created_at        TEXT NOT NULL DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
);

CREATE INDEX IF NOT EXISTS idx_bank_accounts_owner ON bank_accounts(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_bank_accounts_space ON bank_accounts(space_id);

CREATE TABLE IF NOT EXISTS bank_transactions (
    id                BIGSERIAL PRIMARY KEY,
    account_id        BIGINT NOT NULL REFERENCES bank_accounts(id) ON DELETE CASCADE,
    booking_date      TEXT NOT NULL,            -- ISO date
    amount            NUMERIC NOT NULL,          -- negative = outgoing, positive = incoming
    currency          TEXT NOT NULL DEFAULT 'EUR',
    counterparty      TEXT,
    purpose           TEXT,
    posting_text      TEXT,
    category          TEXT,                     -- filled by rules/LLM later; NULL = uncategorised
    -- FinTS gives no stable transaction id, so duplicates are caught by
    -- hashing the fields that make a booking unique in practice.
    dedup_hash        TEXT NOT NULL,
    raw_json          TEXT,                     -- the connector's serialized row, for debugging/re-categorising
    created_at        TEXT NOT NULL DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
    UNIQUE (account_id, dedup_hash)
);

CREATE INDEX IF NOT EXISTS idx_bank_transactions_account_date ON bank_transactions(account_id, booking_date);
