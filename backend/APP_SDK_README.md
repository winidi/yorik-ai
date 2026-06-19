# Yorik App SDK — short README

This document is intentionally short. The SDK + app system is
**pre-1.0 and may change**. If you're thinking about building
something significant on top, please open an issue first so we can
flag rough edges and avoid moving APIs out from under you.


## What the SDK is for

A Yorik **app** is a folder with a `manifest.json` + `connector.py` +
`schema.sql` + (optional) `entry_ui.js`. Installing it gives the user
a new dock icon, their own SQLite database at `data/apps/<id>/data.db`,
and a set of operations the LLM can invoke. The SDK
(`backend/app_sdk.py`) is the small surface your `connector.py`
imports from.


## Hello-world app

```
my-app/
├── manifest.json
├── schema.sql
├── connector.py
└── entry_ui.js
```

`manifest.json`:

```json
{
  "id":         "my-app",
  "name":       "My app",
  "icon":       "🧪",
  "version":    "0.1.0",
  "schema":     "schema.sql",
  "connector":  "connector.py",
  "entry_ui":   "entry_ui.js",
  "description": "Try-out app"
}
```

`schema.sql`:

```sql
CREATE TABLE IF NOT EXISTS notes (
  id    INTEGER PRIMARY KEY AUTOINCREMENT,
  body  TEXT NOT NULL,
  ts    TEXT NOT NULL DEFAULT (datetime('now'))
);
```

`connector.py`:

```python
from yorik.app_sdk import operation, db

@operation(role=["admin", "member"])
def add_note(body: str) -> dict:
    with db() as conn:
        cur = conn.execute("INSERT INTO notes (body) VALUES (?)", (body,))
        return {"id": cur.lastrowid}

@operation(role=["admin", "member"])
def list_notes(limit: int = 20) -> dict:
    with db() as conn:
        rows = conn.execute(
            "SELECT id, body, ts FROM notes ORDER BY id DESC LIMIT ?", (limit,),
        ).fetchall()
    return {"notes": [dict(r) for r in rows]}
```

Install it locally:

```bash
curl -X POST 'http://localhost:8000/api/apps/install?source_dir=/abs/path/to/my-app' \
  -H 'cookie: yorik_session=<your-session>'
```

Re-running the same call reloads the app — useful while iterating.


## Licensing

The SDK file (`backend/app_sdk.py`) carries an **AGPL linking
exception** ([LICENSE-EXCEPTION-APP-SDK](../LICENSE-EXCEPTION-APP-SDK)
at the repo root). Translation: your app code can be licensed however
you like — MIT, Apache, proprietary, commercial — without inheriting
Yorik's AGPL. Modifications to the SDK files themselves stay AGPL.

If you're publishing your app anywhere, **declare your license** in
the manifest:

```json
{ "license": "MIT" }
```


## Naming

If your app stays on your own machine, anything goes (`my-app`,
`crm`, whatever). If you plan to share it with anyone else, use a
namespaced id: `<your-handle>.<app-slug>`, e.g. `"acme.cleaning-crm"`.
A bare id will log a warning at install time to nudge you toward
namespacing — no collisions today since there's only your install,
but the next person installing your app + somebody else's `crm`
shouldn't conflict.


## Recommended manifest fields

Everything optional, but worth setting:

```json
{
  "author":            "Your name",
  "author_id":         "your-handle",
  "license":           "MIT",
  "homepage":          "https://your-site.example/yorik-app",
  "min_yorik_version": "0.2",
  "tags":              ["crm", "small-business"]
}
```


## What your app can/can't do

Can:
- Read/write its own SQLite (`data/apps/<id>/data.db`) via `db()`.
- Read/write tables in `family.db` declared in
  `requires_tables_external` IF the user approved at install.
- Call connectors in `requires_connectors` IF approved at install.
- Render arbitrary HTML/JS in a sandboxed iframe via `entry_ui.js`.

Can't:
- Touch tables not declared in the manifest.
- Reach the network from the iframe — CSP blocks it. For outbound
  HTTP, build a connector operation that uses `httpx` server-side.
- Read other apps' SQLite files.


## Things to NOT build yourself

Yorik provides these — using your own version creates lock-in for
your users:

- **Auth.** Read role from `ctx` in the operation; Yorik already
  authenticated the caller.
- **LLM calls.** Use `from yorik.app_sdk import llm` and you get
  the pre-wired OpenAI-compatible client pointing at the user's
  configured model. Two methods:
  - `llm.ask(prompt, system="optional system msg")` -> str — one-shot.
  - `llm.complete([{"role": "user", "content": "..."}, ...])` -> str — multi-turn.
  Must be called from inside an `@operation` function. No tool-use
  surface (apps don't orchestrate Yorik skills; expose your own
  `@operation` functions and let Yorik's main agent call them).
- **Voice / STT.** Already routed to the connector layer via
  Whisper. Any `@operation` is voice-addressable.


## Stability

- SDK function signatures: pre-1.0 and may change. We'll bump the
  version that `min_yorik_version` checks against, so old apps
  refuse to load gracefully rather than crashing.
- Manifest schema: **adding** optional fields is fine and expected.
  **Removing or renaming** required fields is the kind of thing we
  try not to do.
- Your app's stored data: stays put. We won't migrate
  `data/apps/<id>/data.db` for you.

When something concrete enough emerges to be worth a real
developer guide (catalogue, distribution, the rest), we'll write
one. Until then: open an issue.
