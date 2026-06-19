-- Opt-in "Hey Yorik" wake-word detection on the kiosk wall.
--
-- When enabled, the tablet streams microphone audio continuously over
-- a WebSocket to /api/wakeword/stream. The backend runs an
-- on-prem CLIP-style openWakeWord model trained for "Hey Yorik" and
-- emits {type:"wake"} when the score crosses threshold; the tablet
-- then triggers the existing voice-recording flow.
--
-- Default 0 because "continuously listening for a wake word" is a
-- privacy decision the household has to make explicitly. Per-device
-- because some walls in the house (living room) want hands-free
-- voice while others (guest room) shouldn't have an open mic.
--
-- Audio NEVER hits OpenAI / Google — the wake word is detected on
-- the household's own server, and only the actual command-and-response
-- audio is processed by Whisper after the wake fires.

ALTER TABLE sessions ADD COLUMN kiosk_hotword_enabled INTEGER NOT NULL DEFAULT 0;
