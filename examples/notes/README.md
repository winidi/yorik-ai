# Notes — Yorik reference app

A minimal community app demonstrating the Yorik App SDK end-to-end:

- Its own SQLite database (`data/apps/notes/data.db`) created from `schema.sql` at install time.
- Three `@operation` functions the main Yorik agent can call via `trigger_connector`:
  - `notes.add_note(body)` — saves a note, optionally infers a one-word mood via the LLM
  - `notes.list_notes(limit=20)` — most recent notes
  - `notes.summarize_today()` — one-paragraph summary of today's notes via the LLM
- `llm.ask(...)` calls from inside an app, scoped to the user's configured model.

No grants needed — notes is fully self-contained. It does not read or write `family.db`.

## Install

From the Yorik repo root:

```bash
curl -X POST "http://127.0.0.1:8000/api/apps/install?source_dir=$(pwd)/examples/notes" \
  -b /path/to/cookies.txt
```

Or via the in-Yorik Marketplace (Settings → Marketplace → Install).

## Try it from chat

- `add a note: had a good run this morning`
- `list my recent notes`
- `summarize today's notes`

## Uninstall

```bash
curl -X DELETE "http://127.0.0.1:8000/api/apps/notes?wipe_data=true" \
  -b /path/to/cookies.txt
```

`wipe_data=false` to keep the SQLite file around.

## Files

- `manifest.json` — app metadata (id, version, icon, schema/connector/ui filenames, min_yorik_version)
- `schema.sql` — DDL applied to `data/apps/notes/data.db` on install
- `connector.py` — three `@operation` functions
- `app.js` — minimal info-screen UI (loaded into the app iframe)
