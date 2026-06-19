"""SQLite schema, connection helper, and idempotent seed for HomeOS."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _resolve_db_path(env_var: str, default_filename: str) -> str:
    """Resolve a DB path from env or default. Relative paths (the common
    case via config.env, which ships `HOMEOS_DB_PATH=data/family.db`)
    are anchored to the project root rather than the current working
    directory — otherwise launching uvicorn from a parent dir silently
    creates an empty parallel DB and you wonder why login is broken.
    """
    raw = os.getenv(env_var)
    if raw:
        p = Path(raw)
        return str(p if p.is_absolute() else (_PROJECT_ROOT / p))
    return str(_PROJECT_ROOT / "data" / default_filename)


DEFAULT_DB_PATH      = _resolve_db_path("HOMEOS_DB_PATH",      "family.db")
DEFAULT_DOCS_DB_PATH = _resolve_db_path("HOMEOS_DOCS_DB_PATH", "documents.db")

# Raw string so the `\Sent` / `\HasNoChildren` IMAP flag refs in comments
# don't trip Python's SyntaxWarning (3.12+) about unknown escape sequences.
SCHEMA_SQL = r"""
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT NOT NULL,
    starts_at     TEXT NOT NULL,
    ends_at       TEXT,
    all_day       INTEGER NOT NULL DEFAULT 0,
    color         TEXT,
    person        TEXT,
    notes         TEXT,
    recurring     TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT NOT NULL,
    due_date      TEXT,
    done          INTEGER NOT NULL DEFAULT 0,
    person        TEXT,
    category      TEXT,
    notes         TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- User-defined task categories. Tasks reference these by name (not FK) so
-- renaming a category needs an UPDATE on tasks, but deleting a category
-- doesn't orphan tasks — they just become "uncategorised".
CREATE TABLE IF NOT EXISTS task_categories (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    color      TEXT NOT NULL DEFAULT '#818cf8',
    position   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS bills (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    amount        REAL NOT NULL,
    currency      TEXT NOT NULL DEFAULT 'EUR',
    due_date      TEXT NOT NULL,
    recurring     TEXT,
    paid          INTEGER NOT NULL DEFAULT 0,
    notes         TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- NOTE: the `documents` table moved to its OWN SQLite file (data/documents.db)
-- so heavy reindex jobs don't lock family.db. See documents.py for its
-- schema and the sqlite-vec virtual table. The LLM searches documents via
-- the dedicated search_documents tool, not via run_sql against family.db.

CREATE TABLE IF NOT EXISTS user_profiles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    email           TEXT,
    role            TEXT NOT NULL,
    voice_id        TEXT,
    voice_embedding TEXT,                            -- JSON list[float] (192-dim ECAPA)
    language        TEXT NOT NULL DEFAULT 'en',      -- TTS voice + LLM reply language
    preferred_layout TEXT NOT NULL DEFAULT 'google',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS saved_queries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger_phrase  TEXT NOT NULL UNIQUE,
    sql_query       TEXT NOT NULL,
    view_command    TEXT,
    response_text   TEXT,
    use_count       INTEGER NOT NULL DEFAULT 0,
    last_used       TEXT
);

CREATE TABLE IF NOT EXISTS conversations (
    id          TEXT PRIMARY KEY,
    user_role   TEXT NOT NULL,
    messages    TEXT NOT NULL DEFAULT '[]',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS connector_credentials (
    connector_name  TEXT PRIMARY KEY,
    payload         BLOB NOT NULL,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS connector_grants (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    layout_id       TEXT NOT NULL,
    connector_name  TEXT NOT NULL,
    granted_at      TEXT NOT NULL DEFAULT (datetime('now')),
    granted_by_role TEXT NOT NULL,
    revoked_at      TEXT,
    UNIQUE (layout_id, connector_name)
);

-- App-permission grants: when a community app declares
-- requires_tables_external (e.g. wants to write to family.events) or
-- requires_connectors, the install flow asks the user for consent. The
-- grant lives here and app_sdk.family()/documents()/connector() checks it
-- at call time. Same pattern as connector_grants — but scoped to apps,
-- not layouts.
CREATE TABLE IF NOT EXISTS app_grants (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id          TEXT NOT NULL,
    resource_type   TEXT NOT NULL,    -- 'table' | 'connector'
    resource_db     TEXT,             -- 'family' | 'documents' | NULL for connector grants
    resource_name   TEXT NOT NULL,    -- table name or connector id
    access          TEXT NOT NULL,    -- 'read' | 'write' | 'read+write'
    granted_at      TEXT NOT NULL DEFAULT (datetime('now')),
    granted_by_role TEXT NOT NULL,
    revoked_at      TEXT,
    UNIQUE (app_id, resource_type, resource_db, resource_name)
);

-- Single-row-per-key store for runtime app settings (admin-toggleable
-- without an env-var + restart cycle). Currently used by:
--   voice_id_enabled — "1"/"0", default "1". When "0", /api/ask-voice
--                      skips the SpeechBrain ECAPA identification step
--                      entirely (use case: lone-admin setups where the
--                      embedding extraction adds latency for no benefit).
CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ─── Auth / sessions (multi-user wave) ────────────────────────────────
-- Sessions are server-side records keyed by an opaque session id stored
-- in an HttpOnly cookie on the client. Each row carries the user_id +
-- expiry. We don't store JWTs because they're impossible to revoke
-- without a side table anyway, and session ids let "kick this user out"
-- be a one-row DELETE.
CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,           -- urlsafe random 32-byte token
    user_id         INTEGER NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at      TEXT NOT NULL,              -- datetime('now', '+30 days') by default
    last_seen_at    TEXT NOT NULL DEFAULT (datetime('now')),
    user_agent      TEXT,                       -- first 200 chars of the UA for "your devices" UI
    ip_seen         TEXT,                       -- last IP that used this session
    FOREIGN KEY (user_id) REFERENCES user_profiles(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_sessions_user_expires ON sessions(user_id, expires_at DESC);

CREATE TABLE IF NOT EXISTS template_cache (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    source_url      TEXT,
    body            TEXT NOT NULL,
    fields_json     TEXT,
    fetched_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ─── WhatsApp (Baileys bridge) ────────────────────────────────────────
-- One row per JID seen. owner_user_id will become a real FK to a users
-- table when multi-user lands; defaults to 1 (admin) for the single-user
-- phase so the migration is purely an UPDATE.
CREATE TABLE IF NOT EXISTS wa_chats (
    jid               TEXT PRIMARY KEY,         -- "4915123456789@s.whatsapp.net" or "...@g.us"
    name              TEXT,
    is_group          INTEGER NOT NULL DEFAULT 0,
    last_message_ts   INTEGER,
    last_message_text TEXT,
    unread_count      INTEGER NOT NULL DEFAULT 0,
    owner_user_id     INTEGER NOT NULL DEFAULT 1,
    pinned            INTEGER NOT NULL DEFAULT 0,
    archived          INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_wa_chats_last_ts ON wa_chats(last_message_ts DESC);

-- Per-user "this is me on WhatsApp" record. Captured on every bridge
-- ready event so we can filter phantom self-LID chats — WhatsApp's
-- privacy-LID system creates one ghost @lid chat per contact-pair when
-- you send messages, each named with your own pushName. Without this
-- table we can't tell "real contact named Tom" from "WhatsApp's
-- per-context LID for me, Tom".
CREATE TABLE IF NOT EXISTS wa_self_identity (
    owner_user_id  INTEGER PRIMARY KEY,
    me_jid         TEXT,        -- e.g. "4915xxx:7@s.whatsapp.net" (with device suffix)
    pushname       TEXT,        -- e.g. "Tom"
    updated_at     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS wa_messages (
    msg_id          TEXT NOT NULL,              -- Baileys key.id (unique per chat)
    chat_jid        TEXT NOT NULL,
    from_me         INTEGER NOT NULL DEFAULT 0,
    participant     TEXT,                       -- sender JID for groups
    push_name       TEXT,                       -- contact's display name at send time
    timestamp       INTEGER NOT NULL,
    text            TEXT,                       -- conversation/extendedText/caption
    media_kind      TEXT,                       -- image | video | audio | document | sticker | NULL
    mimetype        TEXT,
    filename        TEXT,                       -- documentMessage.fileName
    media_local_path TEXT,                      -- set once downloaded
    media_paperless_id INTEGER,                 -- once filed to Paperless
    media_immich_id    TEXT,                    -- once uploaded to Immich
    transcript      TEXT,                       -- Whisper output for voice notes
    owner_user_id   INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (chat_jid, msg_id),
    FOREIGN KEY (chat_jid) REFERENCES wa_chats(jid) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_wa_messages_chat_ts ON wa_messages(chat_jid, timestamp DESC);
CREATE INDEX IF NOT EXISTS ix_wa_messages_ts     ON wa_messages(timestamp DESC);

-- FTS5 index over message text so cross-chat search ("any chat that
-- mentioned X") is fast. Maintained via triggers below.
CREATE VIRTUAL TABLE IF NOT EXISTS wa_messages_fts USING fts5(
    text,
    transcript,
    content='wa_messages',
    content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS wa_messages_ai AFTER INSERT ON wa_messages BEGIN
    INSERT INTO wa_messages_fts(rowid, text, transcript)
        VALUES (new.rowid, COALESCE(new.text, ''), COALESCE(new.transcript, ''));
END;
CREATE TRIGGER IF NOT EXISTS wa_messages_au AFTER UPDATE ON wa_messages BEGIN
    INSERT INTO wa_messages_fts(wa_messages_fts, rowid, text, transcript)
        VALUES('delete', old.rowid, COALESCE(old.text, ''), COALESCE(old.transcript, ''));
    INSERT INTO wa_messages_fts(rowid, text, transcript)
        VALUES (new.rowid, COALESCE(new.text, ''), COALESCE(new.transcript, ''));
END;
CREATE TRIGGER IF NOT EXISTS wa_messages_ad AFTER DELETE ON wa_messages BEGIN
    INSERT INTO wa_messages_fts(wa_messages_fts, rowid, text, transcript)
        VALUES('delete', old.rowid, COALESCE(old.text, ''), COALESCE(old.transcript, ''));
END;

-- Generated drafts: kept around even after send so we can show "last
-- draft" + audit what the LLM proposed vs what the user actually sent.
CREATE TABLE IF NOT EXISTS wa_drafts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_jid      TEXT NOT NULL,
    draft_text    TEXT NOT NULL,
    sources_json  TEXT,                         -- JSON list of {kind, ref, snippet}
    sent_msg_id   TEXT,                         -- set once user clicks send
    sent_text     TEXT,                         -- what was actually sent (may differ from draft_text)
    owner_user_id INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (chat_jid) REFERENCES wa_chats(jid) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_wa_drafts_chat ON wa_drafts(chat_jid, created_at DESC);

-- ─── Email (multi-account IMAP + SMTP) ───────────────────────────────
-- One row per (user, email account) — a single user can have N accounts
-- (Gmail + work Outlook + iCloud + …). All credentials are stored as
-- foreign keys into the credential_store table (Fernet-encrypted),
-- never inline in this table.
CREATE TABLE IF NOT EXISTS email_accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id   INTEGER NOT NULL,
    email           TEXT NOT NULL,
    display_name    TEXT,                      -- "Personal Gmail", "Work"
    -- Connection config. ssl=true → port 993/465 implicit TLS.
    -- starttls=true → port 143/587 STARTTLS. We support both.
    imap_host       TEXT NOT NULL,
    imap_port       INTEGER NOT NULL DEFAULT 993,
    imap_ssl        INTEGER NOT NULL DEFAULT 1,
    imap_username   TEXT NOT NULL,
    smtp_host       TEXT NOT NULL,
    smtp_port       INTEGER NOT NULL DEFAULT 465,
    smtp_ssl        INTEGER NOT NULL DEFAULT 1,
    smtp_starttls   INTEGER NOT NULL DEFAULT 0,
    smtp_username   TEXT NOT NULL,
    -- Credential store reference. The actual password lives encrypted
    -- in the credential_store table at key='email:<account_id>'.
    credential_key  TEXT,
    -- Sync controls.
    enabled         INTEGER NOT NULL DEFAULT 1,
    is_default      INTEGER NOT NULL DEFAULT 0,   -- which account /send defaults to
    last_sync_at    TEXT,
    last_error      TEXT,                          -- human-readable last failure
    last_error_at   TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (owner_user_id) REFERENCES user_profiles(id) ON DELETE CASCADE,
    UNIQUE (owner_user_id, email)
);
CREATE INDEX IF NOT EXISTS ix_email_accounts_owner ON email_accounts(owner_user_id, enabled DESC);

-- Folders / labels per account. UIDVALIDITY tracks whether the server's
-- UIDs are still valid; if it changes (rare, but a server can rebuild
-- its DB), we re-sync from scratch.
CREATE TABLE IF NOT EXISTS email_folders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL,
    name            TEXT NOT NULL,             -- raw IMAP folder name (e.g. "[Gmail]/All Mail")
    display_name    TEXT,                      -- prettified for UI
    flags           TEXT,                      -- JSON of \HasNoChildren / \Sent / \All / etc.
    uid_validity    INTEGER,
    uid_next        INTEGER,                   -- highest UID we've seen + 1
    message_count   INTEGER NOT NULL DEFAULT 0,
    unread_count    INTEGER NOT NULL DEFAULT 0,
    last_sync_at    TEXT,
    FOREIGN KEY (account_id) REFERENCES email_accounts(id) ON DELETE CASCADE,
    UNIQUE (account_id, name)
);

-- Messages. Bodies stored as both text and HTML; reader picks HTML
-- when present and sandboxes it. Snippet is the first ~200 chars of
-- the text body for the list-pane preview without parsing HTML.
CREATE TABLE IF NOT EXISTS email_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL,
    folder_id       INTEGER,
    uid             INTEGER NOT NULL,          -- IMAP UID within folder
    message_id      TEXT,                      -- RFC822 Message-ID — primary thread key
    in_reply_to     TEXT,                      -- threading
    references_ids  TEXT,                      -- JSON array of Message-IDs from References header
    thread_id       TEXT,                      -- derived; same for all messages in conversation
    from_email      TEXT,
    from_name       TEXT,
    to_addrs        TEXT,                      -- JSON list of {email, name}
    cc_addrs        TEXT,
    reply_to        TEXT,
    subject         TEXT,
    snippet         TEXT,
    body_text       TEXT,
    body_html       TEXT,
    date_sent       TEXT,
    date_received   TEXT,
    size_bytes      INTEGER,
    is_unread       INTEGER NOT NULL DEFAULT 1,
    is_starred      INTEGER NOT NULL DEFAULT 0,
    is_sent         INTEGER NOT NULL DEFAULT 0,
    is_draft        INTEGER NOT NULL DEFAULT 0,
    has_attachments INTEGER NOT NULL DEFAULT 0,
    category        TEXT,                      -- LLM-assigned: priority / reply_needed / news / promo
    owner_user_id   INTEGER NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (account_id) REFERENCES email_accounts(id) ON DELETE CASCADE,
    FOREIGN KEY (folder_id)  REFERENCES email_folders(id)  ON DELETE SET NULL,
    UNIQUE (account_id, folder_id, uid)
);
CREATE INDEX IF NOT EXISTS ix_email_msgs_owner_date ON email_messages(owner_user_id, date_received DESC);
CREATE INDEX IF NOT EXISTS ix_email_msgs_thread     ON email_messages(thread_id);
CREATE INDEX IF NOT EXISTS ix_email_msgs_unread     ON email_messages(owner_user_id, is_unread, date_received DESC);

CREATE TABLE IF NOT EXISTS email_attachments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id      INTEGER NOT NULL,
    filename        TEXT,
    mimetype        TEXT,
    size_bytes      INTEGER,
    content_id      TEXT,                      -- for inline images referenced from HTML
    is_inline       INTEGER NOT NULL DEFAULT 0,
    local_path      TEXT,                      -- where we stashed the binary
    paperless_id    INTEGER,                   -- if auto-routed (future)
    immich_id       TEXT,                      -- if auto-routed (future)
    FOREIGN KEY (message_id) REFERENCES email_messages(id) ON DELETE CASCADE
);

-- FTS5 for fast full-text search across subject/sender/snippet/body.
-- Triggers keep it in sync with email_messages.
CREATE VIRTUAL TABLE IF NOT EXISTS email_messages_fts USING fts5(
    subject, from_name, from_email, snippet, body_text,
    content='email_messages', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS email_msgs_ai AFTER INSERT ON email_messages BEGIN
    INSERT INTO email_messages_fts(rowid, subject, from_name, from_email, snippet, body_text)
        VALUES (new.rowid,
                COALESCE(new.subject, ''),
                COALESCE(new.from_name, ''),
                COALESCE(new.from_email, ''),
                COALESCE(new.snippet, ''),
                COALESCE(new.body_text, ''));
END;
CREATE TRIGGER IF NOT EXISTS email_msgs_ad AFTER DELETE ON email_messages BEGIN
    INSERT INTO email_messages_fts(email_messages_fts, rowid, subject, from_name, from_email, snippet, body_text)
        VALUES ('delete', old.rowid,
                COALESCE(old.subject, ''),
                COALESCE(old.from_name, ''),
                COALESCE(old.from_email, ''),
                COALESCE(old.snippet, ''),
                COALESCE(old.body_text, ''));
END;

-- Auto-drafted reply variants for incoming email. Mirrors wa_drafts.
CREATE TABLE IF NOT EXISTS email_drafts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id      INTEGER NOT NULL,            -- email_messages.id we're replying to
    thread_id       TEXT,
    draft_text      TEXT NOT NULL,
    variant_label   TEXT,                        -- brief / warm / detailed
    variant_group_id TEXT,                       -- shared across siblings
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending / used / discarded
    sent_msg_id     INTEGER,                     -- the email_messages row created on send
    discard_reason  TEXT,                        -- manual_reply / regenerated / user_dismissed / sibling_used
    discarded_at    TEXT,
    sources_json    TEXT,
    owner_user_id   INTEGER NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (message_id) REFERENCES email_messages(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_email_drafts_msg     ON email_drafts(message_id, status);
CREATE INDEX IF NOT EXISTS ix_email_drafts_pending ON email_drafts(owner_user_id, status, created_at DESC);

-- Backup history. The actual snapshot lives wherever the user
-- configured (external SSD, USB stick, future cloud); this table is
-- just the audit trail so the UI can show "last 10 backups".
-- Config (target path, passphrase, schedule, include-flags) lives in
-- app_settings under keys backup_*.
CREATE TABLE IF NOT EXISTS backups (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    target_path   TEXT NOT NULL,
    filename      TEXT NOT NULL,        -- relative to target_path
    size_bytes    INTEGER,
    status        TEXT NOT NULL,        -- 'ok' | 'failed'
    error         TEXT,                 -- only when status='failed'
    duration_s    REAL,
    includes      TEXT,                 -- JSON array of what was bundled
    started_at    TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at   TEXT
);
CREATE INDEX IF NOT EXISTS ix_backups_started ON backups(started_at DESC);

-- ─── Task assignees (multi-user delegation) ──────────────────────────
-- One row per (task, user). Legacy `tasks.person` (free-text role
-- label like "admin" / "child") stays as a fallback / hint but
-- assignees is the authoritative list once a task has any.
CREATE TABLE IF NOT EXISTS task_assignees (
    task_id    INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',  -- pending | accepted | declined | done
    assigned_at TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at  TEXT,
    PRIMARY KEY (task_id, user_id),
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES user_profiles(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_task_assignees_user ON task_assignees(user_id, status);

-- ─── Notifications (in-app bell) ────────────────────────────────────
-- Generic notification table — kind is a string discriminator so we
-- can grow it (task_assigned / event_invite / wa_message_priority /
-- backup_failed / etc.) without schema changes.
-- payload_json is whatever the kind-handler in the frontend needs to
-- render + navigate; we keep it small (<500 bytes typical).
CREATE TABLE IF NOT EXISTS notifications (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,            -- WHO to notify
    kind         TEXT NOT NULL,                -- 'task_assigned', 'task_status', ...
    title        TEXT NOT NULL,
    body         TEXT,                          -- short preview text
    payload_json TEXT,                          -- JSON for kind-specific extras (ids, deep links)
    navigate_to  TEXT,                          -- relative URL the bell click should open
    is_read      INTEGER NOT NULL DEFAULT 0,
    read_at      TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES user_profiles(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_notifications_user_unread
    ON notifications(user_id, is_read, created_at DESC);

-- Document numbering (Phase 1 of the Lexoffice-replacement compose stack)
-- ───────────────────────────────────────────────────────────────────────
-- Sequential numbering for legally-numbered documents: German Rechnungen
-- (§ 14 UStG mandates unique sequential numbers, no gaps), Polish faktury
-- (similar), US invoices (best practice), quotes/offers, dunning notices.
--
-- Tables are inert until the user creates their first series — that's the
-- "plugin feel". No series = no UI clutter for users who don't need this
-- (American letter-only households, kids using the chat).
--
-- Two-table design separates the *registry* (series settings) from the
-- *audit log* (every consumed number, with timestamp + actor). The audit
-- log is what makes this Steuerprüfung-tauglich: a tax inspector can
-- demand we reproduce every Rechnungsnummer ever issued + when + who.
CREATE TABLE IF NOT EXISTS document_series (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,             -- 'rechnung'|'angebot'|'gutschrift'|'mahnung'|'invoice'|'quote'|...
    name            TEXT NOT NULL,             -- human-readable: "Rechnungen 2026"
    scheme          TEXT NOT NULL,             -- format with {prefix}{year}{seq:N} placeholders
    prefix          TEXT NOT NULL DEFAULT '',  -- 'R-' or '' or 'KU-' (Kundennummer style)
    seq_padding     INTEGER NOT NULL DEFAULT 3, -- zero-pad the sequence: 3 → 001, 4 → 0001
    next_number     INTEGER NOT NULL DEFAULT 1, -- THE next sequence value to be allocated
    year_reset      INTEGER NOT NULL DEFAULT 1, -- reset sequence at new year (most German setups: yes)
    current_year    INTEGER,                    -- the year of the last allocation, for the reset check
    owner_user_id   INTEGER,                    -- per-user series; NULL = household-shared
    is_default      INTEGER NOT NULL DEFAULT 0, -- the auto-pick for templates that match this kind
    notes           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (owner_user_id) REFERENCES user_profiles(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS ix_document_series_kind ON document_series(kind, is_default);

CREATE TABLE IF NOT EXISTS document_series_allocations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    series_id           INTEGER NOT NULL,
    number              INTEGER NOT NULL,        -- the raw integer (without prefix/year padding)
    formatted           TEXT NOT NULL,           -- the resolved string actually printed on the doc
    year                INTEGER NOT NULL,        -- the year at consume-time (for the reset/audit)
    document_kind       TEXT,                    -- 'rechnung' etc — same as series.kind, denormed for fast lookup
    consumed_by_user_id INTEGER,
    paperless_doc_id    INTEGER,                 -- linked Paperless doc if Save-to-Paperless landed
    pdf_sha256          TEXT,                    -- the rendered PDF's hash — GoBD audit chain
    title               TEXT,                    -- what the user titled the document
    notes               TEXT,
    consumed_at         TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (series_id)           REFERENCES document_series(id)  ON DELETE CASCADE,
    FOREIGN KEY (consumed_by_user_id) REFERENCES user_profiles(id)    ON DELETE SET NULL,
    UNIQUE(series_id, year, number)
);
CREATE INDEX IF NOT EXISTS ix_document_series_alloc_series_consumed
    ON document_series_allocations(series_id, consumed_at DESC);

-- Per-turn feedback + skill telemetry. The foundation for the
-- "self-improving Yorik" story: every assistant turn the user can rate
-- up/down, every skill invocation gets logged with its outcome, both keyed
-- by the LLM model used so we can spot e.g. "find_document is 92% on
-- claude but 41% on qwen3.6". Dashboard at /r/settings/quality reads
-- these straight.
CREATE TABLE IF NOT EXISTS turn_feedback (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT,
    message_idx     INTEGER,                       -- index within the conversation
    rating          INTEGER NOT NULL,              -- +1 thumbs up, -1 thumbs down
    note            TEXT,                          -- optional free text
    llm_model       TEXT,                          -- which model produced the turn
    user_id         INTEGER,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES user_profiles(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS ix_turn_feedback_model_created
    ON turn_feedback(llm_model, created_at DESC);

CREATE TABLE IF NOT EXISTS skill_invocations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_id        TEXT NOT NULL,
    llm_model       TEXT,
    success         INTEGER NOT NULL,              -- 1 success / 0 failure
    error           TEXT,                          -- short failure reason if !success
    latency_ms      INTEGER,
    conversation_id TEXT,
    user_id         INTEGER,
    args_json       TEXT,                          -- JSON-encoded skill args, ~1KB cap
    result_summary  TEXT,                          -- str(result), ~1KB cap
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES user_profiles(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS ix_skill_invocations_skill_model
    ON skill_invocations(skill_id, llm_model, created_at DESC);

CREATE TABLE IF NOT EXISTS template_ratings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id TEXT NOT NULL,
    rating      INTEGER NOT NULL,                  -- +1 / -1
    llm_model   TEXT,                              -- which model auto-filled / generated it
    note        TEXT,
    user_id     INTEGER,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES user_profiles(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS ix_template_ratings_template
    ON template_ratings(template_id, created_at DESC);
"""

SEED_EVENTS = [
    # (title, starts_at, ends_at, color, person)
    # Placeholder demo data only — never real names, addresses, or vendor
    # brands. Mustermann / Beispiel / Musterstadt are the German de-facto
    # John-Doe convention; English fallbacks use "Example" / "Acme".
    # Phase B note: seeded rows have no calendar_id; migration 036 backfills
    # them to the Household shared calendar so all members see them.
    ("Family dinner",                "2026-05-18T19:00:00", "2026-05-18T20:30:00", "#a78bfa", "all"),
    ("School pickup",                "2026-05-19T15:00:00", "2026-05-19T15:30:00", "#22c55e", "child"),
    ("Dentist appointment",          "2026-05-21T10:00:00", "2026-05-21T11:00:00", "#f59e0b", "admin"),
    ("Tischler — Mustermann Angebot","2026-05-25T09:00:00", "2026-05-25T10:00:00", "#ef4444", "admin"),
    ("Weekend trip",                 "2026-05-30T08:00:00", "2026-05-31T20:00:00", "#3b82f6", "all"),
]

SEED_TASKS = [
    # (title, due_date, person)
    ("File Q2 quote with Mustermann GmbH", "2026-05-22", "admin"),
    ("Buy groceries",                      "2026-05-19", "member"),
    ("Finish homework — maths",            "2026-05-18", "child"),
]

SEED_BILLS = [
    # (name, amount, due_date, recurring)
    ("Beispiel-Hausratversicherung", 187.40, "2026-06-15", "yearly"),
    ("Internet — Beispiel-Provider",  59.99, "2026-05-28", "monthly"),
]

# No SEED_USER anymore — the AccountWizard / first-run setup flow
# (backend/main.auth_setup) is the only path that creates the first
# admin, so the first AccountWizard signup naturally becomes user id 1.
# Auto-seeding "admin@yorik.local" used to create a phantom user that
# masked context-leak bugs and confused ownership on shared resources.


def _use_postgres() -> bool:
    """Phase D — YORIK_DB_BACKEND=postgres dispatches via db_shim.

    TODO(post-alpha): rip out the SQLite path entirely.
    config.env (shipped) sets YORIK_DB_BACKEND=postgres, so every install
    is on Postgres and the SQLite branches in this module + ~17 other
    backend files are dead code. Scope when we get to it:
      • delete the SQLite halves of init_db / get_conn / conn_ctx / seed
      • delete SCHEMA_SQL, _ensure_columns, _seed_default_categories,
        SEED_EVENTS/TASKS/BILLS, and this _use_postgres() switch
      • delete migrations/ (only migrations_pg/ runs)
      • audit `except sqlite3.IntegrityError` blocks across the backend
        — they never fire under psycopg, masking unique-violation paths
      • remove HOMEOS_DB_PATH + YORIK_DB_BACKEND from config.env(.example)
      • rework SQLite-backed test fixtures (tests/)
    Postponed under the pre-alpha "fix only user-visible failures" rule.
    """
    return (os.getenv("YORIK_DB_BACKEND") or "sqlite").lower() == "postgres"


def _schema_for_path(path: str | None) -> str:
    """Map a SQLite file path to the Postgres schema we'd connect to.
    family.db → main schema; documents.db → docs schema. We use string
    matching rather than the full path so resolution works whether the
    caller passes DEFAULT_DB_PATH, DOCS_DB_PATH, or an explicit override."""
    if path and "documents" in str(path).lower():
        return "docs"
    return "main"


def get_conn(path: str | None = None):
    """Open a sqlite3.Connection (default) or a shimmed psycopg
    connection (`YORIK_DB_BACKEND=postgres`).

    On the Postgres path: returns a `PgConnection` wrapper that supports
    the sqlite3.Connection methods Yorik's call sites use
    (execute/executemany/commit/rollback/cursor/lastrowid). SQL is
    translated on every execute via db_shim.translate_sql so the same
    `?`-style call sites work against both backends."""
    if _use_postgres():
        from .database_pg import _ensure_pool
        from .db_shim import PgConnection
        schema = _schema_for_path(path)
        pool = _ensure_pool("main" if schema == "main" else "docs")
        # Pass `pool=pool` so the wrapper's __exit__ / close() returns
        # the connection. Without that, `with get_conn() as c:` would
        # leak a pool slot every call and exhaust under background
        # load (paperless reconciler etc.).
        raw = pool.getconn()
        return PgConnection(raw, pool=pool)
    db_path = path or DEFAULT_DB_PATH
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


@contextmanager
def conn_ctx(path: str | None = None) -> Iterator:
    """`with conn_ctx() as c:` — yields a connection of the right type.

    On Postgres: yields a `PgConnection` from the pool, commits on
    clean exit, rolls back on exception, returns connection to pool
    on close. Same context-manager contract as SQLite.
    """
    if _use_postgres():
        from .database_pg import _ensure_pool
        from .db_shim import PgConnection
        schema = _schema_for_path(path)
        pool = _ensure_pool("main" if schema == "main" else "docs")
        with pool.connection() as raw:
            wrapped = PgConnection(raw)
            try:
                yield wrapped
                raw.commit()
            except Exception:
                raw.rollback()
                raise
        return
    conn = get_conn(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _ensure_columns(conn: sqlite3.Connection, table: str, expected: dict[str, str]) -> None:
    """ALTER TABLE … ADD COLUMN for any expected column missing on `table`.

    No-op when `table` doesn't exist yet. Some tables (e.g. error_log,
    contact_shares) are created by migrations that run AFTER init_db's
    legacy `_ensure_columns` block — on a fresh install they don't exist
    yet and the ALTER would crash. The migration creates them with the
    final shape anyway, so skipping here is correct."""
    existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if not existing:
        return
    for col, decl in expected.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def init_db(path: str | None = None) -> None:
    # Postgres path: schema already lives in migrations_pg/000_phase_d_init.sql.
    # The legacy executescript / _ensure_columns / per-table CREATE block
    # below is SQLite-specific (sqlite3.Connection.executescript doesn't
    # exist on psycopg). Just run pending migrations and let the
    # bootstrap + any 063+ migrations do the work.
    if _use_postgres():
        from .database_pg import _ensure_pool
        from . import migrations as _migrations
        pool = _ensure_pool("main")
        with pool.connection() as conn:
            applied = _migrations.run_pending_pg(conn)
            if applied:
                __import__("logging").getLogger("yorik.database").info(
                    "init_db(pg): applied %d migration(s): %s", len(applied), applied,
                )
        # Postgres path skips _seed_default_categories + pending_actions
        # init_schema for now — they hit SQLite-specific code paths we
        # haven't ported yet. Will revisit in Section 5 module-by-module.
        return
    with conn_ctx(path) as conn:
        conn.executescript(SCHEMA_SQL)
        # Migrations for tables present in older databases.
        _ensure_columns(conn, "saved_queries", {
            "view_command": "TEXT",
            "response_text": "TEXT",
        })
        _ensure_columns(conn, "user_profiles", {
            "voice_embedding": "TEXT",                # JSON list[float] (192-dim ECAPA)
            "language": "TEXT NOT NULL DEFAULT 'en'", # per-profile TTS + LLM-reply lang
            # Multi-user wave: password hash for login. NULL means "no
            # password set yet" — drives the first-run setup flow where
            # the existing admin is prompted to create one.
            "password_hash":  "TEXT",
            "disabled":       "INTEGER NOT NULL DEFAULT 0",
            "password_set_at": "TEXT",
            "last_login_at":   "TEXT",
            # Wave 3: per-user external service credentials. Backend
            # uses these so each user's skill calls (find_document /
            # find_photo / etc.) only see their own data in Paperless
            # and Immich. NULL = not yet provisioned for that service.
            "paperless_user_id":  "INTEGER",
            "paperless_token":    "TEXT",
            "immich_user_id":     "TEXT",
            "immich_api_key":     "TEXT",
            # Onboarding + letterhead fields. The Compose app reads these
            # to build the sender block on invoices/letters and to power
            # ZUGFeRD seller info. `onboarded_at` is the "did this user
            # complete the welcome wizard" signal — NULL → show wizard.
            "country":         "TEXT",         # ISO code: 'DE' | 'US' | 'PL' | ...
            "address_street":  "TEXT",
            "address_postcode": "TEXT",
            "address_city":    "TEXT",
            "phone":           "TEXT",
            "business_name":   "TEXT",         # NULL = personal use only
            "tax_id":           "TEXT",        # USt-IdNr / Steuernummer / EIN
            "iban":            "TEXT",
            "onboarded_at":    "TEXT",
            # Beta safety: when ON, LLM-initiated create/update/delete
            # actions stage a pending confirmation instead of executing
            # immediately. User clicks the modal to approve/cancel.
            # ON by default during beta — generates per-model telemetry
            # for the Quality dashboard. User can toggle off in Settings.
            "confirm_mutations": "INTEGER NOT NULL DEFAULT 1",
            # Instant voice ack: plays a short "Moment, ich schau"-style
            # audio cue the moment STT finishes, before the LLM call.
            # Makes voice feel like a natural conversation. ON by default.
            "voice_ack_enabled": "INTEGER NOT NULL DEFAULT 1",
        })
        # Tasks gained a category column post-MVP.
        # Priority: 0=low, 1=normal, 2=high. Default normal so existing
        # rows act unchanged. Estimated minutes is freeform; null means
        # "no estimate".
        _ensure_columns(conn, "tasks", {
            "category":           "TEXT",
            "priority":           "INTEGER NOT NULL DEFAULT 1",
            "estimated_minutes":  "INTEGER",
        })
        # Bills imported from email proposals carry a back-reference so
        # the home briefing card can deep-link the user to the source.
        # document_id links to the scanned PDF (Paperless / uploads) so
        # "zeig mir die Rechnung" can resolve to a real document instead
        # of leaving the LLM to guess.
        _ensure_columns(conn, "bills", {
            "email_message_id": "INTEGER",
            "document_id":      "INTEGER",
        })
        # error_log carries the per-request correlation id (set by the
        # CorrelationFilter when emitted inside an HTTP request) so the
        # Settings -> Logs UI can deep-link from a warning back to the
        # full trace in data/logs/yorik.log.
        _ensure_columns(conn, "error_log", {"corr_id": "TEXT"})
        # WhatsApp drafts post-Phase-3: status + variant group + reason for
        # discard. Lets us hold N draft variants per incoming message and
        # auto-discard them when the user replies from their phone.
        _ensure_columns(conn, "wa_drafts", {
            "status":            "TEXT NOT NULL DEFAULT 'pending'",  # pending | used | discarded
            "variant_group_id":  "TEXT",                              # same UUID for all variants in one set
            "variant_label":     "TEXT",                              # 'brief' | 'warm' | 'detailed'
            "discarded_at":      "TEXT",
            "discard_reason":    "TEXT",                              # 'manual_reply' | 'regenerated' | 'user_dismissed'
            "trigger_msg_id":    "TEXT",                              # the incoming msg_id that triggered the draft
        })
        # Persistent LLM path tracing: args + result for every skill call,
        # truncated so we don't bloat the DB. Lets `SELECT * FROM
        # skill_invocations WHERE conversation_id=?` reconstruct what the
        # model actually did days later, without keeping the full chat
        # ledger around.
        _ensure_columns(conn, "skill_invocations", {
            "args_json":         "TEXT",  # JSON-encoded, truncated to ~1KB
            "result_summary":    "TEXT",  # str(result), truncated to ~1KB
        })
        # Compose drafts — letters/invoices the LLM (or user) prepares
        # via chat, then opens in Compose for finalising. Persisted so
        # the chat→compose handoff doesn't require URL-encoding the body.
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS compose_drafts (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL,
                kind         TEXT NOT NULL DEFAULT 'letter',
                template_id  TEXT,
                recipient    TEXT,
                subject      TEXT,
                body_html    TEXT NOT NULL DEFAULT '',
                args_json    TEXT NOT NULL DEFAULT '{}',
                created_at   TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS ix_compose_drafts_user
                ON compose_drafts (user_id, created_at DESC);
        """)
        # Seed default categories so the dropdown isn't empty on first run.
        _seed_default_categories(conn)

        # ── Run any pending schema migrations.
        # Goes AFTER the legacy CREATE/_ensure_columns block so the
        # baseline schema is always present before migrations try to
        # ALTER it. New schema changes (from this commit forward) go
        # into migrations/NNN_*.sql or .py — see migrations/README.md.
        from . import migrations as _migrations
        applied = _migrations.run_pending(conn)
        if applied:
            log = __import__("logging").getLogger("yorik.database")
            log.info("init_db: applied %d migration(s): %s", len(applied), applied)
    # Bring up the pending_actions / skill_decisions tables (beta
    # confirmation modal + per-model quality telemetry).
    from . import pending_actions as _pa
    _pa.init_schema()


_DEFAULT_TASK_CATEGORIES = [
    # (name, color, position)
    ("Home",     "#818cf8", 0),
    ("Work",     "#34d399", 1),
    ("Family",   "#fbbf24", 2),
    ("Shopping", "#60a5fa", 3),
    ("Health",   "#f87171", 4),
]


def _seed_default_categories(conn) -> None:
    """Insert built-in categories once; user can rename/delete or add more."""
    for name, color, position in _DEFAULT_TASK_CATEGORIES:
        conn.execute(
            "INSERT OR IGNORE INTO task_categories (name, color, position) VALUES (?, ?, ?)",
            (name, color, position),
        )


# ─── documents.db ──────────────────────────────────────────────────────────
# Separate SQLite file for the document corpus + vector index. Kept apart
# from family.db so reindex jobs never lock the calendar/tasks DB and so
# users can wipe + rebuild the index without touching their personal data.
DOCS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS documents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT NOT NULL,
    path          TEXT NOT NULL,
    mime_type     TEXT,
    bytes         INTEGER,
    tags          TEXT,
    allowed_roles TEXT NOT NULL DEFAULT 'admin',
    chunk_count   INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    indexed_at    TEXT
);

CREATE TABLE IF NOT EXISTS document_chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id      INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    text        TEXT NOT NULL,
    char_start  INTEGER,
    char_end    INTEGER,
    embedded_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_document_chunks_doc_id ON document_chunks(doc_id);

-- Paperless-mirror chunks. Each row = one ~500-token slice of an OCR'd
-- Paperless document, embedded by Yorik's local model and stored in the
-- paperless_vec virtual table (created separately, needs vec0 loaded).
-- paperless_doc_id is Paperless's primary key — fetch the original via
-- the Paperless REST API for citations.
CREATE TABLE IF NOT EXISTS paperless_chunks (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    paperless_doc_id INTEGER NOT NULL,
    chunk_index      INTEGER NOT NULL,
    text             TEXT NOT NULL,
    char_start       INTEGER,
    char_end         INTEGER,
    ingested_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(paperless_doc_id, chunk_index)
);
CREATE INDEX IF NOT EXISTS idx_paperless_chunks_doc ON paperless_chunks(paperless_doc_id);
"""


def _existing_vec_dim(conn: sqlite3.Connection, table_name: str) -> int | None:
    """Read the dimension from an existing vec0 virtual table's CREATE
    statement (stored verbatim in sqlite_master.sql). Returns None if
    the table doesn't exist or the dim can't be parsed."""
    import re
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
    ).fetchone()
    if not row or not row[0]:
        return None
    m = re.search(r"float\s*\[\s*(\d+)\s*\]", row[0], re.IGNORECASE)
    return int(m.group(1)) if m else None


def _ensure_vec_table(conn: sqlite3.Connection, table_name: str, embed_dim: int) -> None:
    """Create the vec0 virtual table at `embed_dim`. If the table
    already exists at a DIFFERENT dim (e.g. user swapped embedding
    models), drop + recreate it. The chunk rows in `*_chunks` survive,
    so the reconciler can re-embed them on its next pass."""
    import logging
    log = logging.getLogger("homeos.database")
    existing = _existing_vec_dim(conn, table_name)
    if existing is not None and existing != embed_dim:
        log.warning(
            "embedding dim changed (%d → %d); dropping %s — re-ingest needed",
            existing, embed_dim, table_name,
        )
        conn.execute(f"DROP TABLE {table_name}")
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {table_name} USING vec0(embedding float[{embed_dim}])"
    )


def init_docs_db(path: str | None = None, embed_dim: int | None = None) -> None:
    """Initialize the separate documents/vector database.

    Loads the sqlite-vec extension before creating the vec0 virtual table.
    The virtual table can't live in DOCS_SCHEMA_SQL because it requires the
    extension to be loaded on the connection that runs the CREATE.

    `embed_dim` defaults to whatever backend.documents.EMBED_DIM resolves
    to (the bundled sentence-transformers model's output dim, unless the
    user overrode it). Passing it explicitly is supported for tests.

    Postgres path: schema is already in the bootstrap migration; pgvector
    replaces sqlite_vec. Nothing to do here.
    """
    if _use_postgres():
        return
    import sqlite_vec
    if embed_dim is None:
        # Lazy import to avoid an import cycle (documents → database).
        from . import documents as _docs
        embed_dim = _docs.EMBED_DIM
    db_path = path or DEFAULT_DOCS_DB_PATH
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.executescript(DOCS_SCHEMA_SQL)
        # Multi-user wave 2: documents own their uploader. owner_user_id
        # is a soft FK to family.db's user_profiles.id (cross-DB FKs
        # aren't possible in SQLite — application-level enforced).
        # Default 1 = the admin user, so pre-multi-user uploads end up
        # in the admin's namespace on migration.
        _ensure_columns(conn, "documents", {
            "owner_user_id": "INTEGER NOT NULL DEFAULT 1",
        })
        # Vec tables — `_ensure_vec_table` handles the dim-changed
        # migration (drop + recreate, chunk text rows survive).
        _ensure_vec_table(conn, "vec_chunks", embed_dim)
        _ensure_vec_table(conn, "paperless_vec", embed_dim)
        conn.commit()
    finally:
        conn.close()


def get_docs_conn(path: str | None = None):
    """Open a connection to the documents store.

    SQLite: opens `documents.db` with sqlite-vec loaded.
    Postgres (`YORIK_DB_BACKEND=postgres`): returns a `PgConnection`
    pointing at the `docs` schema. pgvector replaces sqlite_vec so
    no extension loading is needed."""
    if _use_postgres():
        from .database_pg import _ensure_pool
        from .db_shim import PgConnection
        pool = _ensure_pool("docs")
        return PgConnection(pool.getconn(), pool=pool)
    import sqlite_vec
    db_path = path or DEFAULT_DOCS_DB_PATH
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def seed(path: str | None = None) -> None:
    """Idempotent seed: re-running does not duplicate rows.

    Alpha policy: demo rows are OPT-IN. A fresh install starts empty —
    so first-time testers see real empty-state UX, not someone else's
    leftover bills. Set YORIK_SEED_DEMO=1 (or pass `start.sh --with-demo`)
    to populate the example events / tasks / bills above. Existing
    databases that already have the seeded rows are untouched either
    way — the INSERTs are no-ops when the rows already exist.
    """
    if not os.getenv("YORIK_SEED_DEMO"):
        import logging as _logging
        _logging.getLogger("yorik.startup").info(
            "seed: skipped (set YORIK_SEED_DEMO=1 or use start.sh --with-demo to include demo rows)"
        )
        return
    with conn_ctx(path) as conn:
        for title, starts_at, ends_at, color, person in SEED_EVENTS:
            conn.execute(
                "INSERT OR IGNORE INTO events (title, starts_at, ends_at, color, person) "
                "SELECT ?, ?, ?, ?, ? "
                "WHERE NOT EXISTS (SELECT 1 FROM events WHERE title = ? AND starts_at = ?)",
                (title, starts_at, ends_at, color, person, title, starts_at),
            )
        for title, due_date, person in SEED_TASKS:
            conn.execute(
                "INSERT INTO tasks (title, due_date, person) "
                "SELECT ?, ?, ? "
                "WHERE NOT EXISTS (SELECT 1 FROM tasks WHERE title = ?)",
                (title, due_date, person, title),
            )
        for name, amount, due_date, recurring in SEED_BILLS:
            conn.execute(
                "INSERT INTO bills (name, amount, due_date, recurring) "
                "SELECT ?, ?, ?, ? "
                "WHERE NOT EXISTS (SELECT 1 FROM bills WHERE name = ?)",
                (name, amount, due_date, recurring, name),
            )
        # First user is created by the AccountWizard via /api/auth/setup,
        # not seeded here. See the comment on SEED_USER's removal above.


if __name__ == "__main__":
    init_db()
    seed()
    print(f"HomeOS DB ready at {DEFAULT_DB_PATH}")
    init_docs_db()
    print(f"HomeOS docs DB ready at {DEFAULT_DOCS_DB_PATH}")
