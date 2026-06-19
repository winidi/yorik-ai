-- Cache for on-demand LLM address scrapes.
-- The contact_address_suggestions table holds candidate addresses
-- extracted from the user's WhatsApp + email history for a specific
-- contact, so repeat opens of the "add address" form are instant and
-- a second LLM run isn't needed unless the user explicitly re-scrapes.

CREATE TABLE IF NOT EXISTS contact_address_suggestions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id      INTEGER NOT NULL,
    -- Source channel the candidate came from ('email' / 'whatsapp').
    source_kind     TEXT NOT NULL,
    -- A short pointer into source — message id or thread id — so the
    -- user can verify "where did Yorik find this?" later if needed.
    source_ref      TEXT,
    line1           TEXT,
    line2           TEXT,
    postcode        TEXT,
    city            TEXT,
    region          TEXT,
    country         TEXT,
    -- LLM confidence 0-1 (or null if heuristic-only).
    confidence      REAL,
    -- The original passage the address was extracted from, kept so
    -- the UI can show "found in: 'we live at Hauptstr. 5, Berlin'".
    excerpt         TEXT,
    scraped_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_contact_addr_sugg_contact
    ON contact_address_suggestions(contact_id, scraped_at DESC);

-- Per-contact scrape status so the UI can show "last scraped 2 days ago"
-- without re-querying when there are zero results.
ALTER TABLE contacts ADD COLUMN address_scraped_at TEXT;
