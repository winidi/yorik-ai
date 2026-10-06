# Diagnostics — what Yorik may tell its makers

Opt-in, pseudonymised error reports and usage counts. The user-facing
promise is in [PRIVACY.md](PRIVACY.md#diagnostics); this page is for
whoever works on the code or runs the collector. Plan: 2026-10-06
(with Dirk); code: `backend/diagnostics/`.

## The rules the code enforces

1. **Allow-list, not scrubbing.** `registry.FIELDS` names every field a
   payload may carry (purpose, tier, type). `registry.serialise` drops
   everything else and counts it. A field that is not listed cannot be
   sent.
2. **Tool results and arguments never as text.** `scrub.result_shape`
   gives size bucket, totals, hits per source, error class.
   `scrub.scrub_args` keeps argument *keys*, turns values of known kinds
   (`to`, `contact`, `chat`, `path` …) into tokens and lets through only
   the words in `scrub.ENUM_VALUES`.
3. **Errors** as class, frames inside `backend/` and a template with
   quotes, numbers, urls and addresses removed (`scrub.scrub_error`).
   Routes as templates (`/api/contacts/{id}`).
4. **No raw ids**: no conversation, user or row ids, no hostnames or
   base URLs. Each payload has its own random `report_id`.
5. **Numbers in buckets**, dates as months, versions as major.minor,
   platform and model as enums.
6. **The name check runs only on a local model** (`scrub.llm_is_local`:
   loopback, RFC1918, a bare Docker service name, `.local`, and not
   OpenRouter). Otherwise free text is dropped and the payload says
   `scrub.free_text_dropped: true`.
7. **The last look**: before a report is queued, `scrub.self_check`
   reads the final bytes with the dictionary and the regexes; any hit
   blocks the send and is shown to the person (kinds only).
8. **Groups and third parties**: WhatsApp groups are `group_n` without
   member tokens; a report holds only rows the reporting person owns
   or was given.
9. **Withdrawal** (`reset_identity`): local drafts and queue deleted,
   pseudonyms forgotten, secret and install id rotated, deletion
   requests for sent reports.
10. **Visible**: every payload at INFO in the log and under Settings →
    Developer → Diagnostics.

## Pseudonyms

`pseudonyms.token_for(kind, value)`: `HMAC-SHA256(secret, kind ‖
normalised value)` → row in `diag_pseudonyms(kind, hmac, token)`, token
`kind_n` counting up per kind. Kinds: person, email, phone, iban, chat,
group, mailbox, file, secret, org. The value is never stored.
`build_dictionary(user_id)` collects what the scrubber replaces first:
household members, contacts with aliases, channels and addresses, the
person's mailboxes, chats and the senders of their mail (400 days).
Name parts point at the full name they belong to, so "Beate" and
"Beate Mayer" share a token; the scrubber matches inflections
("Beates", "bei Mayers"). `who_is(token, user_id)` re-derives the HMAC
of every dictionary value and returns matches — admin only, logged.

## Consent and identity

`household_settings`: `diag.consent_asked_at`, `diag.tier_counts`,
`diag.tier_usage`, `diag.tier_errors` ('0'/'1'), `diag.install_id`,
`diag.pseudonym_secret`, `diag.collector_url` (empty = collect locally,
send nothing; `YORIK_DIAG_COLLECTOR_URL` as environment fallback).
`/api/auth/me` carries `diag_consent_asked`; `AuthGate` shows
`DiagnosticsConsentStep` once to an admin after onboarding.

## API

| Route | Who | What |
|---|---|---|
| `GET /api/diagnostics/consent` | signed in | switches, `is_admin`, `llm_local`, `collector_configured`, `install_id` (admin) |
| `PUT /api/diagnostics/consent` | admin, browser session | `{counts, usage, errors}` — records `asked_at` |
| `POST /api/diagnostics/identity/reset` | admin, browser session | new identity, pseudonyms forgotten |
| `GET /api/diagnostics/registry` | signed in | every field with purpose and tier |
| `GET /api/diagnostics/pseudonyms/resolve?token=` | admin, browser session | local lookup, logged |
| `GET /api/diagnostics/reports` | signed in | own reports; admin: all |
| `GET /api/diagnostics/reports/{id}` | owner or admin | one report with payload and scrub summary |
| `POST /api/diagnostics/reports/draft` | owner of the conversation | (part 2) assemble a report |
| `POST /api/diagnostics/reports/{id}/send` / `decline` | owner | (part 2) after review |

## Payload shapes

Error report (tier 3), after `registry.serialise`:

```json
{"schema": 1, "kind": "error", "report_id": "…uuid…", "trigger": "thumbs_down",
 "env": {"version": "0.3", "platform": "linux-x64", "llm_kind": "local", "llm_family": "qwen", "install_age": "1-6m"},
 "feedback": {"rating": "down", "reason": "found_nothing", "note": "…scrubbed…"},
 "question": {"text": "siehst du meine bestellung bei org_3", "intent": "search", "words": "4-9", "language": "de"},
 "answer": {"said_nothing_found": true, "words": "10-49", "tool_calls": 2},
 "trace": [{"name": "universal_search", "arg_keys": "query,also", "args": {},
            "result": {"len": "1000+", "total": 31, "sources": {"email": 8, "whatsapp": 5}}, "duration": "1-3s"}],
 "facts": [{"token": "org_3", "kind": "org", "exists": true, "indexed": true, "rows": "1-9", "last_seen": "2026-10",
            "search_rank": 9, "recomputed": true}],
 "skills": [], "errors": [],
 "scrub": {"level": "dictionary_regex_llm", "replaced": {"person": 2, "email": 1}, "dropped_fields": 3, "free_text_dropped": false}}
```

Daily counts (tiers 1 and 2):

```json
{"schema": 1, "kind": "usage_daily", "report_id": "…", "day": "2026-10-06",
 "env": {"version": "0.3", "platform": "linux-x64", "llm_kind": "local", "llm_family": "qwen", "install_age": "1-6m"},
 "counts": {"users": "3-4", "chat_turns": "10-49", "feedback_up": 2, "feedback_down": 1, "skill_failures": 0},
 "features": {"email": "50-199", "whatsapp": "10-49", "documents": "1-9"},
 "setup": {"mailboxes": "1-9", "whatsapp": true, "documents": "100-499", "contacts": "100-499", "bank": true}}
```

## Collector (part 3)

`tools/diag-collector/`: `POST /v1/reports` (JSON ≤ 256 KB, header
`X-Yorik-Install`, answers 202), `POST /v1/reports/{id}/delete` with
the report's deletion token (`HMAC(secret, report_id)`, sent along
with the report so the collector can verify later), second-layer
scrubbing with the same regexes, retention 90 days, aggregates only
from ≥ 5 installs, the web server's access log off. `to_case.py` turns
an error report into a case for the search test set.

## Tests

`tests/test_diagnostics_core.py` (pseudonyms, dictionary, scrubber,
errors, shapes, registry, self_check), `tests/test_diagnostics_consent.py`
(consent API, reports visibility). Parts 2 and 3 add report assembly,
outbox and usage tests.
