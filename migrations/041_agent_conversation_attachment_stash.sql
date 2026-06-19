-- agent_conversations.attachment_stash
--
-- A per-conversation list of attachment pointers the user has built up
-- while chatting. Each entry is just {url, filename, mimetype} — the
-- bytes live in Immich / Paperless / Yorik's documents.db; the stash
-- only holds the URL the email Composer will fetch on send.
--
-- Why per-conversation: the natural mental model is "I was talking
-- with Yorik about photos of Beate — send me those by email." When
-- the user reopens the conversation later, the stash is still there.
--
-- Why NOT NULL DEFAULT '[]': lets load_stash do a plain SELECT with
-- no coalesce. The list is capped client-side (~50 items) so the
-- column stays small even for power users.

ALTER TABLE agent_conversations ADD COLUMN attachment_stash TEXT NOT NULL DEFAULT '[]';
