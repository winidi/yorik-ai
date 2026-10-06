# Yorik diagnostics collector

The small receiver for the opt-in reports described in
[docs/DIAGNOSTICS.md](../../docs/DIAGNOSTICS.md). Not part of the Yorik
image; the maintainers run one copy.

## Run

```
python -m venv venv && venv/bin/pip install fastapi uvicorn "psycopg[binary]"
createdb diag
DIAG_DB=postgresql://diag@127.0.0.1/diag venv/bin/uvicorn app:app --host 127.0.0.1 --port 8800
```

Put a web server in front with TLS and **the access log switched off
for this host** (nginx: `access_log off;` in the server block) — the
collector never stores an IP address, so the web server must not
either. A nightly cron runs `python app.py prune` (90 days).

In the install that should send: Settings → Privacy → the switches,
and `diag.collector_url` in `household_settings` (or
`YORIK_DIAG_COLLECTOR_URL=https://collect.example/v1` in config.env).
An empty address means reports stay in the house.

## API

| Route | Body | Answer |
|---|---|---|
| `POST /v1/reports` (header `X-Yorik-Install: <uuid>`) | `{"report": {...}, "delete_token_sha256": "<hex>"}`, ≤ 256 KB | 202 `{"ok": true, "id": ...}` |
| `POST /v1/reports/{id}/delete` | `{"delete_token": "<hex>"}` | 204, or 404 when the token does not match |
| `GET /v1/stats` | — | counts only, from five installations on |

Every report is scrubbed once more on arrival (addresses, IBANs,
phones, IPs, URLs, key-shaped strings → placeholders), then stored as
JSON with the install id and the time.

## From a report to a test case

`to_case.py` turns one error report into a case for the search test
set (`~/yorikai/search-eval/cases.json` shape):

```
venv/bin/python to_case.py <report-id>
```

prints `{"id": ..., "q": "<pseudonymised question>", "want": [], "tags":
[trigger, reason], "note": "facts: person_7 exists, indexed, rank 9 …"}`.
The maintainer then seeds a fixture with the same shape (a mail from a
made-up sender, a contact) and fills `want`.

## What this must never do

- store or log an IP address, a user agent or a TLS fingerprint
- accept a report without an install id, or larger than 256 KB
- show individual reports anywhere public; `GET /v1/stats` is counts only
