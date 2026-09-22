-- The WhatsApp semantic index carried no owner, so a draft for one
-- person was seasoned with snippets from every other person's chats
-- (audit 2026-09-22, 3.4). Each chunk now belongs to the person whose
-- WhatsApp session holds the message.
ALTER TABLE docs.wa_chunks ADD COLUMN IF NOT EXISTS owner_user_id UUID;

UPDATE docs.wa_chunks c
   SET owner_user_id = m.owner_user_id
  FROM wa_messages m
 WHERE m.chat_jid = c.chat_jid AND m.msg_id = c.msg_id AND c.owner_user_id IS NULL;
-- chunks nobody owns are nobody's context
DELETE FROM docs.wa_chunks WHERE owner_user_id IS NULL;

-- the same message id can sit in two people's sessions (a group both are in)
ALTER TABLE docs.wa_chunks DROP CONSTRAINT IF EXISTS wa_chunks_chat_jid_msg_id_key;
ALTER TABLE docs.wa_chunks ADD CONSTRAINT wa_chunks_owner_msg UNIQUE (chat_jid, msg_id, owner_user_id);
CREATE INDEX IF NOT EXISTS idx_wa_chunks_owner ON docs.wa_chunks (owner_user_id);
