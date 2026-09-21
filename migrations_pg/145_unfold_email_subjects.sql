-- Subjects were stored with the line breaks of folded headers. They
-- showed as broken lines in the list, and replying to such a mail
-- failed ("Header values may not contain linefeed").
UPDATE email_messages
   SET subject = btrim(regexp_replace(subject, E'\\s*[\\r\\n]+\\s*', ' ', 'g'))
 WHERE subject ~ E'[\\r\\n]';
