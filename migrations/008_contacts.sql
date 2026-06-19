-- Contacts: the identity hub. One row per person or business that the
-- household has ever interacted with — email, WhatsApp, phone, postal,
-- letters drafted. Every app references it instead of inventing its own
-- recipient text fields, so "schick Oma einen Brief" / "wer hat mir das
-- gestern gemailt" / "rechnung für Müller GmbH" all resolve to the same
-- row.
--
-- Three tables instead of one big row:
--   contacts            ← the identity + status + business fields
--   contact_channels    ← email / phone / whatsapp / signal / ...
--                         UNIQUE(kind, value) makes inbound lookups O(1)
--   contact_addresses   ← home / work / billing / shipping
--
-- Status drives the auto-capture funnel: incoming email from an
-- unknown sender lands as 'pending'; user reply or explicit save
-- promotes to 'active'; transactional/no-reply senders are caught
-- by the spam regex and dropped straight to 'spam'.

CREATE TABLE IF NOT EXISTS contacts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name        TEXT NOT NULL,
    aliases             TEXT,                          -- JSON array of strings
    kind                TEXT NOT NULL DEFAULT 'person'
                        CHECK (kind IN ('person', 'business')),
    status              TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'pending', 'spam', 'archived')),

    -- Personal fields (nullable on businesses)
    relation            TEXT,                          -- 'grandmother', 'plumber', 'employer'…
    birthday            TEXT,                          -- YYYY-MM-DD
    language_pref       TEXT,                          -- 'de' / 'en' / …
    salutation_pref     TEXT,                          -- 'du' / 'Sie' / 'first-name' / 'formal'

    -- Business fields (nullable on persons)
    legal_name          TEXT,
    tax_id              TEXT,                          -- USt-IdNr / VAT / etc.
    iban                TEXT,
    payment_terms_days  INTEGER,
    default_currency    TEXT,

    notes               TEXT,
    tags                TEXT,                          -- JSON array
    allowed_roles       TEXT NOT NULL DEFAULT 'admin', -- comma-separated

    -- Provenance + ranking
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    created_by_user_id  INTEGER REFERENCES user_profiles(id) ON DELETE SET NULL,
    last_used_at        TEXT,                          -- "I addressed a draft to them" — ranks autocomplete
    last_interaction_at TEXT,                          -- last inbound (email/WA/etc.)
    source              TEXT NOT NULL DEFAULT 'manual'
                        CHECK (source IN ('manual', 'email_in', 'wa_sync', 'paperless_ocr',
                                          'ics_import', 'vcard', 'auto'))
);

-- Index for the most common queries: filter by status, sort by last-used.
CREATE INDEX IF NOT EXISTS ix_contacts_status_used
    ON contacts (status, last_used_at DESC);
CREATE INDEX IF NOT EXISTS ix_contacts_kind
    ON contacts (kind, status);


CREATE TABLE IF NOT EXISTS contact_channels (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id  INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL
                CHECK (kind IN ('email', 'phone', 'whatsapp', 'signal',
                                'telegram', 'sms', 'website', 'social')),
    value       TEXT NOT NULL,        -- normalised: emails lower-cased, phones E.164
    label       TEXT,                  -- 'primary' / 'work' / 'home' / 'mobile' / …
    verified_at TEXT,                  -- when we last confirmed the channel works
    source      TEXT,                  -- 'manual' / 'email_in' / 'wa_sync' / …
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),

    -- THE killer constraint: inbound email/WA can ask
    -- "is this address known?" in one indexed hit, no JSON scan.
    UNIQUE (kind, value)
);

CREATE INDEX IF NOT EXISTS ix_contact_channels_contact
    ON contact_channels (contact_id);


CREATE TABLE IF NOT EXISTS contact_addresses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id  INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL DEFAULT 'home'
                CHECK (kind IN ('home', 'work', 'billing', 'shipping', 'other')),
    line1       TEXT,
    line2       TEXT,
    postcode    TEXT,
    city        TEXT,
    region      TEXT,                  -- state / canton / Bundesland
    country     TEXT,                  -- ISO 3166-1 alpha-2 ideally
    label       TEXT,
    source      TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS ix_contact_addresses_contact
    ON contact_addresses (contact_id);
