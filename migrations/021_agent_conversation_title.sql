-- Migration 021: per-conversation title (LLM-generated)
--
-- The Chat app's sidebar has been using the first 60 chars of the
-- first user message as the title, which reads like sentence
-- fragments ("What's on my calend…"). After the second assistant
-- turn, the loop calls the LLM with a tiny prompt to summarise the
-- conversation as 2–5 words ("Hannover-Trip planen", "Brief an
-- Hausverwaltung") and stores it here. Sidebar + thread header
-- prefer this over the preview when present.
--
-- Nullable: legacy rows + brand-new conversations have no title yet
-- and the UI falls back to the preview slice. Updated in place by
-- the loop's auto-title step (never user-edited yet — could be a
-- follow-up).

ALTER TABLE agent_conversations ADD COLUMN title TEXT;
