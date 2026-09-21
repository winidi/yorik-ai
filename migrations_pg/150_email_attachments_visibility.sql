-- Filing a mail attachment in Paperless now asks who may see it, the
-- same question as the chat attachment card ("nur mich / die Eltern /
-- die Familie"). The answer is kept here so the mail app can show it.
ALTER TABLE email_attachments
  ADD COLUMN IF NOT EXISTS paperless_visibility TEXT;
