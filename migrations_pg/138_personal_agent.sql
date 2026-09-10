-- Each person can point Yorik at their own agent (their Hermes). What
-- Yorik hands over on someone's behalf goes to that person's agent,
-- never to another household member's machine.
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS agent_url  TEXT;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS agent_key  TEXT;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS agent_name TEXT;
