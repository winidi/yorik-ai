# What the existing Yorik App SDK does today (Phase E §0.3)

Read end-to-end (2026-06-15): `backend/app_sdk.py` (290 lines), `backend/app_loader.py` (468 lines), `backend/app_catalog.py` (135 lines), `examples/notes/{manifest.json, schema.sql, connector.py, app.js}`, and the marketplace + per-app endpoints in `backend/main.py` (6563-6720).

Phase E **extends** this SDK; it doesn't replace it. This file is the contract — anything broken here is a regression.

---

## 1. Filesystem layout (must remain valid)

```
yorik-ai/
├── apps/<id>/                     # committed source for community apps
│   ├── manifest.json              # required
│   ├── schema.sql                 # required
│   ├── connector.py               # required (today)
│   ├── app.js                     # required UI bundle
│   ├── README.md                  # optional
│   └── importers/                 # optional CSV mapping presets
├── data/apps/<id>/                # gitignored runtime state
│   └── data.db                    # per-app SQLite (Phase E replaces with Postgres schema)
└── examples/notes/                # canonical reference app, lives outside apps/
```

`APPS_SRC_DIR = REPO_ROOT/apps`, `APPS_DATA_DIR = REPO_ROOT/data/apps`. `examples/` are loaded into `apps/` on first install via `install_app_from_dir_copy`.

---

## 2. Manifest contract (v1, in production today)

Required:
- `id` — `^[a-z0-9][a-z0-9_-]*$` OR namespaced `^author.app$`. Bare ids OK for local; namespaced (`acme.cleaning-crm`) for shared apps.
- `name` — display string
- `icon` — emoji or short text
- `version` — semver-ish
- `schema` — relative path to `schema.sql`
- `connector` — relative path to `connector.py`
- `entry_ui` — relative path to UI bundle (today: `app.js`)

Optional:
- `author`, `author_id` (namespacing handle), `homepage` (http(s) URL)
- `license`, `min_yorik_version` (regex `^\d+(\.\d+){0,2}$`)
- `description`, `tags`, `aliases` (voice trigger phrases)
- `chrome` — `"embedded"` (default; Yorik header visible) or `"fullscreen"`
- `requires_tables_external` — list of `{db: "family"|"documents", table: str, access: "read"|"write"|"read+write"}`
- `requires_connectors` — list of connector names
- `home_icon` — alternate icon for home grid

Builtin reserved ids: `calendar`, `chat`, `docs` — filesystem apps with these names are skipped to avoid double-registration.

Phase E adds `manifest_version: 2`. v1 manifests (no version field) still load.

---

## 3. The `@operation` connector model

`connector.py` imports from `yorik.app_sdk` (which is `backend/app_sdk.py` exposed under a stable namespace via `sys.modules["yorik.app_sdk"] = app_sdk`).

```python
from yorik.app_sdk import operation, db, llm

@operation(role=["admin", "member"], description="Save a note")
def add_note(body: str) -> dict:
    with db() as conn:
        cur = conn.execute("INSERT INTO notes (body) VALUES (?)", (body,))
        return {"id": cur.lastrowid}
```

What `@operation` does:
- Tags the function with `_yorik_operation = True`, `_yorik_op_role`, `_yorik_op_name`, `_yorik_op_doc`
- At load time, `app_loader._wrap_operation_as_connector` discovers tagged functions
- Builds a JSON schema from the function signature (str/int/float/bool/list/dict → string/integer/number/boolean/array/object)
- Registers each as a connector under `<app_id>.<op_name>` with `backend="app:<id>"`
- The wrapper sets `_active_app` ContextVar before calling the user function

**Roles**: `["admin"]` default. Children/viewers etc. listed explicitly. Enforced inside the wrapper.

**Invocation paths**:
- Yorik's LLM calls operations via `trigger_connector("<app_id>.<op_name>", params)` — same plumbing as any other connector
- The app's own iframe calls them via POST `/api/apps/<id>/op/<op_name>` (defence-in-depth check: connector must have `backend == "app:<id>"`)

Phase E keeps `@operation` for backward compat. v2-only apps can omit `connector.py` entirely (frontend-only is the default in v2).

---

## 4. App SDK helpers (the `yorik.app_sdk` surface)

All require an active `@operation` context (`_active_app` ContextVar set). Module-level calls raise.

| Helper | What it returns | Phase E mapping |
|---|---|---|
| `db()` | `sqlite3.Connection` to `data/apps/<id>/data.db`. Auto-creates dir + file. PRAGMA foreign_keys on. row_factory = Row. | Postgres connection to `app_<id>` schema. Same surface via the `db_shim` wrapper from Phase D. |
| `family()` | `sqlite3.Connection` to `family.db`. **Requires a `requires_tables_external` grant** (any one entry for db='family'). Read-only by default (`PRAGMA query_only`); write needs `write` or `read+write` grant. | Two paths: connector.py keeps using `family()` (now routes to Postgres `public` schema with RLS via the app's role); frontend apps query PostgREST directly with the app's JWT. |
| `documents()` | `sqlite3.Connection` to `documents.db`. Requires a `requires_tables_external` grant for db='documents'. | Postgres `docs` schema. Same shape. |
| `connector(name, params)` | calls `connectors.invoke(name, params)`. Requires `requires_connectors` grant. async. | Unchanged. |
| `llm.ask(prompt, system=…)` | LLM completion via Yorik's configured model/base_url. Logs calling app id. | Unchanged. |
| `llm.complete(messages, max_tokens=, temperature=)` | OpenAI-shape multi-message completion. No tool calls (apps don't define mid-completion tools). | Unchanged. |

---

## 5. Grant model (the existing access-control layer)

Table: `app_grants` in family.db.

```sql
CREATE TABLE app_grants (
  id              INTEGER PRIMARY KEY,
  app_id          TEXT NOT NULL,
  resource_type   TEXT NOT NULL,        -- 'table' | 'connector'
  resource_db     TEXT,                 -- 'family' | 'documents' | NULL (for connectors)
  resource_name   TEXT NOT NULL,        -- table name OR connector name
  access          TEXT NOT NULL,        -- 'read' | 'write' | 'read+write'
  granted_at      TEXT NOT NULL,
  revoked_at      TEXT
);
```

`_check_grant(app_id, resource_type, resource_db, resource_name, want_access)` looks up a non-revoked grant and returns bool. `GrantError(PermissionError)` raised on miss.

Phase E maps this to:
- `requires_tables_external` → `permissions.reads/writes` per-column on Yorik tables (RLS-projected views in `app_<id>` schema)
- `requires_connectors` → `permissions.uses_connectors` (no shape change)
- `app_grants` table: keep, used by both v1 `family()/documents()/connector()` paths AND as the consent-record audit log for v2 apps

---

## 6. HTTP endpoints (preserve these surfaces)

All currently in `backend/main.py:6563+`.

| Endpoint | Purpose | Phase E |
|---|---|---|
| `GET /api/apps` | List installed apps the role may see (home grid). | Keep. |
| `GET /api/apps/opt-in` | List + state of opt-in apps. Admin-only. | Keep. |
| `POST /api/apps/{id}/enable` `/disable` | Toggle opt-in flag. Admin-only. | Keep. |
| `GET /api/apps/{id}/manifest` | Read manifest. | Keep. |
| `GET /api/apps/{id}/ui` | Serve UI JS bundle (text/javascript). | Keep — wraps with CSP iframe in Phase E. |
| `POST /api/apps/install` | Install from local dir. Admin. Returns `{app_id, operations, data_dir, requires_tables_external, requires_connectors}`. | Keep; add `permissions` + `realtime_subscriptions` to response. |
| `POST /api/apps/{id}/op/{op}` | App's iframe calls its own operation. Auth gate: connector must have `backend=="app:<id>"`. | Keep. |
| `GET /api/apps/available` | Marketplace listing — annotates installed status. Reads `marketplace/catalog.json`. | Change source from bundled catalog.json → GitHub-hosted `yorik-community/.catalog`. |
| `POST /api/apps/install_from_catalog/{id}` | Marketplace install. Admin. | Keep + extend for new consent flow. |

Phase E adds:
- `POST /api/apps/install/preflight` — returns parsed manifest + "negative space" string + RLS preview
- `POST /api/apps/install/confirm` — actually installs (the user-consented step)
- `POST /api/apps/<id>/permissions/revoke` — tighten a specific grant; regenerate the projection view

---

## 7. Frontend ↔ app communication

Today: app loads in an iframe served by `/api/apps/<id>/ui`. Yorik's shell injects `window.yorik` into the iframe before the script runs. The iframe calls `window.yorik.callOperation(opName, params)` which is a POST to `/api/apps/<id>/op/<opName>` with the user's cookie.

`window.yorik` exposes (from reading `examples/notes/app.js` and the shell wrapper):
- `callOperation(name, params)` — POST to own op endpoint
- DOM-level events for theme changes etc.

Phase E adds:
- The iframe wrapper sets `Content-Security-Policy: default-src 'self' http://localhost:8400; connect-src 'self' http://localhost:8400 <manifest.network.outbound…>`
- `window.YORIK_APP_KEY` injected at iframe mount — the per-app Supabase JS key. App uses `createClient(window.YORIK_SUPABASE_URL, window.YORIK_APP_KEY)` for direct PostgREST + Realtime queries.
- `window.yorik.callOperation` stays (apps still need to invoke their own connectors for compute-heavy ops)
- New `window.yorik.invokeSkill(name, args)` for invoking Yorik skills the manifest declared in `invokes_skills`

---

## 8. The reference app (`examples/notes/`) — what to preserve

This is the canonical example; if it breaks, the SDK has a problem. What it does today:

1. **`schema.sql`** — creates `notes` table (id BIGSERIAL, body, mood, created_at). Index on created_at DESC.
2. **`manifest.json`** — id="notes", icon="📝", points at schema/connector/entry_ui. chrome=embedded. license=Apache-2.0.
3. **`connector.py`** — three @operation functions:
   - `add_note(body)` — inserts; optionally calls `llm.ask` for mood inference; returns `{id, body, mood}`
   - `list_notes(limit=20)` — top N by created_at DESC; returns `{notes: [...], count: n}`
   - `summarize_today()` — pulls today's notes; `llm.ask` for a paragraph summary; returns `{summary, note_count}`
4. **`app.js`** — 790-line vanilla-JS SPA. No build step. Calls `window.yorik.callOperation("add_note", {body})` etc.

Phase E port plan (Section 10.1 of the masterplan):
- Schema → owned_schema `app_notes`, table stays the same shape (BIGSERIAL preserved via `id BIGSERIAL PRIMARY KEY`)
- Manifest → manifest_version 2 + permissions block (no Yorik table reads needed; mood-inference uses llm via the existing path)
- New `policies.sql` — `notes` RLS: owner_user_id matches caller, plus space-scoping if we add it
- `connector.py` stays exactly as-is for the three @operation functions — `db()` now routes to Postgres `app_notes` schema transparently via the Phase D shim
- `app.js` — keep `callOperation` for compute-heavy ops (summarize_today); add direct Supabase JS for list_notes (no LLM needed)

If we can't preserve this app's behaviour through Phase E, we've broken something.

---

## 9. Things Phase E MUST NOT remove

- `@operation` decorator and its discovery/wrapping
- `_active_app` ContextVar
- `llm.ask` / `llm.complete` surface
- `app_grants` table (used by v1 apps in production)
- `requires_tables_external` / `requires_connectors` manifest fields (v1 apps still declare these)
- Filesystem layout `apps/<id>/` (apps/ committed; data/apps/<id>/ gitignored)
- Builtin reserved ids (`calendar`, `chat`, `docs`)
- The `yorik.app_sdk` import alias (apps write `from yorik.app_sdk import …`)

---

## 10. Things Phase E REPLACES or EXTENDS

| v1 | v2 |
|---|---|
| Per-app SQLite at `data/apps/<id>/data.db` | Per-app Postgres schema `app_<id>` |
| `db()` → SQLite | `db()` → Postgres (via the Phase D shim — transparent) |
| `family()` returns full table; PRAGMA query_only for read-only | Frontend reads through PostgREST views (column-projected). `family()` from connector.py stays for back-compat. |
| Grants stored in `app_grants` only | Same table + RLS projection views in `app_<id>` schema + per-app Postgres role |
| Manifest `requires_tables_external: [{db, table, access}]` | Manifest `permissions.reads: [{table, columns, purpose}]` — column-level, with consent-screen purpose |
| Bundled marketplace catalog | GitHub-hosted `yorik-community/.catalog` |
| No realtime | Per-table opt-in via `realtime_subscriptions:` |
| No CSP on iframe | CSP with manifest-declared outbound origins |
| App auth = user's cookie + role check in `@operation` wrapper | App auth = per-app Supabase JS key bound to per-app Postgres role; RLS enforces; role check in wrapper stays for `@operation` |

---

## 11. Open invariant questions (resolve while implementing)

- **Per-app Postgres role provisioning**: When does the role get its password rotated? At install only, or also on permission revoke? Recommendation: rotate on revoke so a leaked key has bounded blast radius.
- **Apps importing each other's data**: v1 has no story (the `app_grants` lookup is per-app). v2 keeps this — apps can only see their own schema. Cross-app data sharing goes through a Yorik skill broker.
- **Migrations within an app**: v1 has none (`schema.sql` runs once). v2 should support `migrations/NNN.sql` so app upgrades can evolve the schema. Defer the design to first real upgrade need.
- **Two apps with the same `owned_schema`**: prevent via the namespaced-id rule (`<author>.<app>` → schema `app_<author>_<app>`) plus an explicit collision check on install.
