-- References header stored as single characters. The fetcher iterated
-- the header string ("<a@x> <b@y>") and stripped "<>" from each
-- character, so references_ids became ["", "a", "@", …] and thread_id
-- the empty first element: replies never joined their thread and the
-- Pipelines reply check could not match them (chat rerun 2026-09-27).
-- Only the angle brackets were lost; the spaces between the ids
-- survived, so the ids come back by joining and splitting.
--
-- Rewrites only rows in the broken shape.

WITH fixed AS (
    SELECT id,
           array_remove(regexp_split_to_array(btrim(
               (SELECT string_agg(e, '' ORDER BY n)
                  FROM json_array_elements_text(references_ids::json) WITH ORDINALITY AS t(e, n))
           ), '[\s,]+'), '') AS refs
      FROM email_messages
     WHERE references_ids LIKE '["", %'
)
UPDATE email_messages m
   SET references_ids = to_json(f.refs)::text,
       thread_id = COALESCE(f.refs[1], m.in_reply_to, m.message_id)
  FROM fixed f
 WHERE m.id = f.id;

-- Follow-ups copied the broken list into their origin; the reminder
-- mails would carry it as their References header.
UPDATE pipelines p
   SET origin_json = jsonb_set(p.origin_json::jsonb, '{references}',
                               COALESCE(m.references_ids, '[]')::jsonb)::text
  FROM email_messages m
 WHERE p.origin_json LIKE '%"references": ["", %'
   AND m.id = (p.origin_json::jsonb->>'mail_id')::bigint;
