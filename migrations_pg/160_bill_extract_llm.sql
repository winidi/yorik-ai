-- Who reads amount and due date off a bill mail (2026-09-25).
--
-- The "New bill from …?" bell proposal took the first money figure in
-- the text and read every "." as a thousands mark: an Anthropic receipt
-- for €214.00 came out as €21420.00. The rules now read German and US
-- notation (backend/email_classifier.py, _parse_amount), but only a
-- model reliably tells the charged amount from subtotal, tax and line
-- items, in any language.
--
-- TRUE: the local model reads the bill, and the rules check its answer —
-- an amount that does not appear in the mail is thrown away. FALSE, or
-- no model answering: the rules alone. On by default; it costs one
-- model call per mail that is already classified as a bill.
ALTER TABLE user_profiles
  ADD COLUMN IF NOT EXISTS bill_extract_llm BOOLEAN NOT NULL DEFAULT TRUE;
