-- Migration 028: capture List-Unsubscribe / List-Unsubscribe-Post headers
--
-- RFC 2369 + RFC 8058 — the standard way a legitimate bulk sender lets
-- a recipient opt out. Gmail/Apple/etc. read these headers and render
-- a one-click "Unsubscribe" button. Yorik can do the same once we
-- store them.
--
-- list_unsubscribe carries one or more comma-separated targets in
-- angle brackets — typically a mailto:list-unsubscribe@example.com
-- AND/OR an https://… URL. Stored verbatim; the unsubscribe module
-- parses it on demand.
--
-- list_unsubscribe_post carries the literal token
-- "List-Unsubscribe=One-Click" when the sender opted into RFC 8058
-- (no consent page, validated POST body). Its presence is the signal
-- that an https URL is safe to fire automatically without opening
-- a browser.

ALTER TABLE email_messages ADD COLUMN list_unsubscribe       TEXT;
ALTER TABLE email_messages ADD COLUMN list_unsubscribe_post  TEXT;
