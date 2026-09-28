-- Threads the way Thunderbird builds them: a mail joins the thread of
-- the mail it answers (In-Reply-To). The fetcher took the first
-- References entry, so an answer that referenced only the question
-- (Oliver Zehnter, 22.09.) got a thread of its own and stood alone in
-- the inbox (chat rerun 2026-09-28; backend/email_threads.py does it
-- for new mail). Each pass moves whole threads into their parent's;
-- a chain of answers needs one pass per step, capped at 20.

DO $$
DECLARE
    n int;
    i int := 0;
BEGIN
    LOOP
        WITH pairs AS (
            SELECT DISTINCT ON (c.owner_user_id, c.thread_id)
                   c.owner_user_id, c.thread_id AS old_t, p.thread_id AS new_t
              FROM email_messages c
              JOIN email_messages p
                ON p.owner_user_id = c.owner_user_id AND p.message_id = c.in_reply_to
             WHERE COALESCE(c.thread_id, '') <> '' AND COALESCE(p.thread_id, '') <> ''
               AND c.thread_id <> p.thread_id
             ORDER BY c.owner_user_id, c.thread_id, p.date_received
        )
        UPDATE email_messages m SET thread_id = pairs.new_t
          FROM pairs
         WHERE m.owner_user_id = pairs.owner_user_id AND m.thread_id = pairs.old_t;
        GET DIAGNOSTICS n = ROW_COUNT;
        i := i + 1;
        EXIT WHEN n = 0 OR i >= 20;
    END LOOP;
END $$;
