# Building a Yorik app

An **app** is a UI you install onto a Yorik box. Notes, Calendar, Reading list, Habits — those are all apps. A community author writes one, ships a folder, and a Yorik admin clicks Install. The app gets:

- Its own Postgres schema (private data, with Row-Level Security so users can only see their own rows).
- A scoped role + JWT (when its iframe talks to Supabase directly, it can't read anything outside its schema).
- A sandboxed iframe with a Content-Security-Policy the manifest dictated.
- Optional access to Yorik core data through declared `permissions.reads`, projected as narrow columns the user explicitly approved.
- Optional access to Yorik skills (the LLM-callable capabilities) through `permissions.invokes_skills`.

This guide gets you from "nothing" to "installed and working" in under an hour.

## The contract

An app is **five files** in one folder:

```
your-app/
├── manifest.json     ← what the app is + what it wants
├── schema.sql        ← Postgres DDL for its own tables
├── policies.sql      ← Row-Level Security for those tables
├── connector.py      ← @operation functions the iframe can call
└── app.js            ← the iframe UI
```

Drop the folder anywhere on disk and tell Yorik where it is — the admin pastes the path into **Settings → Installed apps → Install from source directory**, reviews the consent dialog, clicks Install. The app appears in the home screen / dock.

There are three reference apps in `examples/`. Each one is small enough to read in one sitting:

| App | Pattern shown | Lines of code |
| --- | --- | --- |
| `examples/habits-v2/` | Hello-world: one schema, two tables, no Yorik core access. | ~270 |
| `examples/notes-v2/` | Calls the Yorik LLM for mood inference. | ~870 |
| `examples/reading-list-v2/` | Reads Yorik contacts, declares realtime subscriptions. | ~290 |

Copy whichever is closest to what you want to build and edit from there.

## The simplest possible app

Try this right now. Create a new folder with these five files:

```
my-app/
├── manifest.json
├── schema.sql
├── policies.sql
├── connector.py
└── app.js
```

### `manifest.json`

```json
{
  "manifest_version": 2,
  "id": "you.first-app",
  "name": "First App",
  "icon": "✨",
  "version": "0.1.0",
  "author": "Your Name",
  "license": "MIT",
  "description": "My first Yorik app.",
  "schema": "schema.sql",
  "connector": "connector.py",
  "entry_ui": "app.js",
  "min_yorik_version": "0.3",
  "owned_schema": "app_you_first_app",
  "owned_tables": ["items"],
  "permissions": {},
  "network": { "outbound": [] },
  "ui": { "type": "iframe", "mount_path": "/apps/first", "entry": "app.js" }
}
```

The `id` field is namespaced as `<author>.<app>`. Pick something unique — it determines the URL mount and the Postgres schema name.

### `schema.sql`

```sql
CREATE TABLE items (
    id         BIGSERIAL PRIMARY KEY,
    user_id    UUID DEFAULT auth.uid()
                 REFERENCES public.user_profiles(id) ON DELETE CASCADE,
    text       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Two things to note:

1. **`DEFAULT auth.uid()`** populates the row's owner with the calling JWT's subject. When the iframe writes through Supabase, it lands tagged with the right user. When your connector writes (running as the postgres superuser, no JWT), `user_id` is `NULL` — those rows are "agent notes," visible only to platform admins via the RLS below.
2. **`REFERENCES public.user_profiles(id)`** with `ON DELETE CASCADE` means a deleted user takes their rows with them. You almost always want this.

### `policies.sql`

```sql
ALTER TABLE items ENABLE ROW LEVEL SECURITY;

CREATE POLICY items_owner ON items
    FOR ALL
    USING (
        user_id = auth.uid()
        OR yorik.role(auth.uid()) = 'platform_admin'
    )
    WITH CHECK (
        user_id = auth.uid()
        OR yorik.role(auth.uid()) = 'platform_admin'
    );
```

The `yorik.role()` helper lives in the Yorik core; it's the same function the rest of the platform uses to gate visibility. The pattern above (owner-or-platform-admin) is the simplest correct policy. Copy it.

### `connector.py`

```python
from yorik.app_sdk import operation, pg_db


@operation(role=["admin", "member"], description="Add a new item.")
def add_item(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        return {"error": "text required"}
    with pg_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO items (text) VALUES (%s) RETURNING id, text",
                (text,),
            )
            row = cur.fetchone()
            return {"id": row[0], "text": row[1]}


@operation(role=["admin", "member"], description="List the most recent items.")
def list_items() -> dict:
    with pg_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, text, created_at FROM items "
                "ORDER BY created_at DESC LIMIT 50"
            )
            rows = cur.fetchall()
    return {"items": [
        {"id": r[0], "text": r[1], "created_at": r[2].isoformat()}
        for r in rows
    ]}
```

`pg_db()` is the v2 SDK function. It yields a psycopg connection with `search_path` set to your app's owned schema, so `INSERT INTO items` writes to `app_you_first_app.items` without you spelling out the prefix.

### `app.js`

```javascript
(function () {
  var root = document.getElementById("app") || document.body;
  var state = { items: [], loading: true };

  function callOp(name, params) {
    return window.yorik.callOperation("you.first-app." + name, params || {});
  }

  function render() {
    root.innerHTML = "<h1>First App</h1>";

    var form = document.createElement("form");
    form.innerHTML = "<input id='t' placeholder='New item…'><button>Add</button>";
    form.onsubmit = function (e) {
      e.preventDefault();
      var t = form.querySelector("#t").value.trim();
      if (t) callOp("add_item", { text: t }).then(load);
    };
    root.appendChild(form);

    if (state.loading) { root.innerHTML += "<p>Loading…</p>"; return; }
    state.items.forEach(function (it) {
      var li = document.createElement("div");
      li.textContent = it.text;
      root.appendChild(li);
    });
  }

  function load() {
    callOp("list_items").then(function (r) {
      state.items = r.items || []; state.loading = false; render();
    });
  }

  load();
})();
```

The contract here is simple: `window.yorik.callOperation(fullName, params)` returns a promise. `fullName` must start with your app's id — that's enforced at the iframe boundary.

## Installing it

Open **Settings → Installed apps**. Paste the absolute path to your folder into the "Install from source directory" box and click **Preview & install**.

The consent dialog parses your `manifest.json`, validates it, and shows the admin every scope it declared. Anything missing or malformed shows up as an error — the Install button stays disabled until you fix the manifest.

When you click Install:

1. Yorik copies your folder into `data/apps-src/<id>/` (bundled apps in `apps/` stay untouched).
2. Creates the Postgres schema `app_you_first_app`, runs `schema.sql`, then `policies.sql`.
3. Creates the per-app Postgres role `app_you_first_app_role` with USAGE on your schema only.
4. Imports `connector.py` and registers your `@operation` functions.
5. Writes a row to `installed_apps` with the manifest snapshot + scopes the admin saw.

Your app now appears in the home screen. Click its icon — the iframe loads `app.js` with `window.yorik` already wired up.

## Adding scopes

The simplest app declares `"permissions": {}` and gets the locked-down default: no Yorik core access, no skills, no network. Most useful apps need to read something.

### Reading from Yorik core

Want autocomplete from the user's contacts? Add this to `manifest.json`:

```json
"permissions": {
  "reads": [
    {
      "table": "contacts",
      "columns": ["id", "display_name", "kind"],
      "purpose": "Autocomplete @mentions when composing."
    }
  ]
}
```

Two things happen on install:

1. The consent dialog shows the admin **what** (which columns of which table) and **why** (your purpose string). Without an admin clicking through with that purpose visible, the read does not happen.
2. Your owned schema gets a projection view `app_you_first_app._yorik_contacts` that selects only those three columns. RLS on the underlying `public.contacts` still applies — your app sees only the rows the user can see.

Read the view from `connector.py`:

```python
with pg_db() as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT id, display_name FROM _yorik_contacts LIMIT 50")
        contacts = cur.fetchall()
```

You can't write to projection views; they're read-only.

### Calling a Yorik skill

Skills are LLM-callable capabilities like `find_person` or `compose_draft`. Declare which ones you'll invoke:

```json
"permissions": {
  "invokes_skills": ["find_person"]
}
```

Invoke from `connector.py` (skill invocation lives in a separate module of the SDK; see `examples/reading-list-v2/` for the canonical example).

### Realtime subscriptions

If the user has multiple tabs of your app open and you want a write in one to update the others without polling, declare:

```json
"permissions": {
  "realtime_subscriptions": ["items"]
}
```

This adds `app_you_first_app.items` to the `supabase_realtime` publication. Use `@supabase/supabase-js` in your iframe to subscribe; fetch the app-scoped JWT from `GET /api/apps/<id>/jwt` first. RLS still applies — your subscriber sees changes to rows it could SELECT.

### Outbound network

If your app needs to call an external API (Stripe, a weather service, etc.) declare it:

```json
"network": {
  "outbound": [
    { "url": "https://api.example.com", "purpose": "Sync invoices." }
  ]
}
```

The consent dialog shows this verbatim. The iframe's `Content-Security-Policy` adds the origin to `connect-src` — every other origin is blocked by the browser. If you `fetch('https://evil.com')`, the browser refuses.

## What apps cannot do (yet)

By design, in this v1 of the platform:

- **No writes to Yorik core tables.** If you need to add a contact or create an event, call a skill (`compose_draft`, etc.) — declaring `permissions.writes` is accepted as a no-op and logged so reviewers see it.
- **No background processes.** Apps don't run a long-lived server. Use scheduled skills (declared via `permissions.scheduled`) when you need cron-style work.
- **No raw filesystem.** No access to `data/` or anything outside your owned schema.

These boundaries are intentional and load-bearing for the install-and-trust story. Work with them, not around them.

## Iterating

Edit your files, then in **Settings → Installed apps** click Uninstall + reinstall. The schema is dropped and recreated; your data is wiped each time. (Yes, this is destructive — `wipe_data=false` on uninstall preserves the schema if you need it during dev.)

If you only changed `app.js` or `connector.py` (not `schema.sql` / `policies.sql`), you can also `sudo systemctl restart yorik` — the boot loader re-registers your install from the `installed_apps` ledger without touching Postgres state.

## Reading the code

Three reference apps walk through three patterns side by side:

- **`examples/habits-v2/`** — minimal. No Yorik core, no skills. Read this first.
- **`examples/notes-v2/`** — calls the Yorik LLM (`llm.ask`) for mood inference. Read for the AI-in-the-loop pattern.
- **`examples/reading-list-v2/`** — declares reads on contacts, realtime on its own table. Read for the "real app talking to real Yorik data" pattern.

The Phase E architecture spec lives at `docs/PHASE_E_SDK_SPEC.md` if you want the full background.

## Getting unstuck

- **Install button is grey** — the manifest has validation errors. The consent dialog lists each one; fix the manifest and reload.
- **`row violates row-level security policy`** on insert — your `policies.sql` `WITH CHECK` clause didn't accept the row. Most often: you wrote `WITH CHECK (user_id = auth.uid())` but your connector is running as the superuser (so `auth.uid()` is `NULL`). Either drop the `WITH CHECK`, or always pass `user_id` explicitly from the connector.
- **`relation "items" does not exist`** in `connector.py` — `pg_db()` sets `search_path` to your owned schema, so unqualified names work. If you renamed your `owned_schema` and didn't reinstall, the connector is talking to the old schema. Uninstall + reinstall.
- **iframe shows nothing / blank** — open the browser dev console. `Content-Security-Policy` errors point at undeclared `connect-src` origins; add them to `network.outbound` and reinstall.

Open an issue at [winidi/yorik-ai](https://github.com/winidi/yorik-ai) if you hit something this guide didn't cover. Patches that make this doc clearer are especially welcome.
