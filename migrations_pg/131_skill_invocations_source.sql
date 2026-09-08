-- Where a skill call came from: chat (NULL/'chat'), or 'token:<name>' for
-- calls an outside agent made through /mcp or /api with a personal token.
ALTER TABLE skill_invocations ADD COLUMN IF NOT EXISTS source TEXT;
