-- A letter is signed with the full name (2026-09-24).
--
-- A letterhead started from the profile's display name ("Beate"), not
-- from first and last name, so every letter carried the first name only.
-- New letterheads take "first last" (backend/writing/letterhead.py);
-- this corrects the ones made that way. Only a sender or signature name
-- that still equals the display name is replaced — a name somebody
-- typed into the letterhead stays as it is.
UPDATE letterheads l
   SET data = (
         (l.data::jsonb)
         || CASE WHEN (l.data::jsonb)->>'sender_name' = u.name
                 THEN jsonb_build_object('sender_name', trim(u.first_name || ' ' || u.last_name))
                 ELSE '{}'::jsonb END
         || CASE WHEN (l.data::jsonb)->>'signature_name' = u.name
                 THEN jsonb_build_object('signature_name', trim(u.first_name || ' ' || u.last_name))
                 ELSE '{}'::jsonb END
       )::text,
       updated_at = to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
  FROM user_profiles u
 WHERE u.id = l.user_id
   AND coalesce(trim(u.first_name), '') <> ''
   AND coalesce(trim(u.last_name), '') <> ''
   AND ((l.data::jsonb)->>'sender_name' = u.name OR (l.data::jsonb)->>'signature_name' = u.name);
