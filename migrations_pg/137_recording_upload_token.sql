-- A per-recording secret the recording device sends with its chunks and
-- the finish call, so a shared tablet keeps uploading even when the
-- session cookie changes hands (PIN switch) during the dinner.
ALTER TABLE recordings ADD COLUMN IF NOT EXISTS upload_token TEXT;
