-- IMAP STARTTLS support.
--
-- Originally we only supported two IMAP modes: implicit SSL (port 993,
-- imap_ssl=1) or fully plaintext (imap_ssl=0). SMTP had three from
-- day one (ssl / starttls / plain) but IMAP got skipped.
--
-- Bites with Proton Mail Bridge specifically: Bridge serves IMAP on
-- 127.0.0.1:1143 with STARTTLS — connect plaintext, upgrade via the
-- STARTTLS command, then login. With our old binary flag a Proton
-- user either got "SSLERROR: WRONG VERSION NUMBER" (we wrapped TLS
-- around a plaintext server) or sent their password in clear
-- (imap_ssl=0, no upgrade).
--
-- Same shape as smtp_starttls. Default 0 so every existing row keeps
-- its current behaviour: SSL accounts (993) stay SSL, plain accounts
-- stay plain. Only manually set true for STARTTLS hosts.

ALTER TABLE email_accounts ADD COLUMN imap_starttls INTEGER NOT NULL DEFAULT 0;
