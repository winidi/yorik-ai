# Phase E — Yorik App SDK specification (draft)

**Status**: DRAFT. No code yet. This document is the artifact we argue with before committing to anything.
**Author**: Claude, 2026-06-15, in response to the user's "privacy OS / apps via yorik-community" vision.
**Audience**: the future-Claude and future-user who will sit down to actually build Phase E.

---

## 1. Mental model

Yorik is two things:

**The kernel** — Yorik's FastAPI backend. Owns the LLM agent, skills, connectors (Paperless / Immich / WhatsApp / email), background reconcilers, the install/uninstall machinery, the consent UI. Has full privileged access to Postgres via a service-role key.

**The platform** — local self-hosted Supabase. Postgres + pgvector + GoTrue (Auth) + PostgREST + Realtime + Storage. Apps see this and only this. Apps never touch the kernel; the kernel never proxies app data requests.

Apps live in `yorik-community/apps/<name>/`. Every app is a self-contained directory the user can install, inspect, and uninstall. The directory contains a manifest, a schema, an RLS policy file, and a UI bundle.

The user installs an app by clicking "Install" in Yorik's marketplace UI. The user uninstalls by clicking "Uninstall." Nothing else.

---

## 2. The manifest — `manifest.yaml`

```yaml
# yorik-app/v1
manifest_version: 1
name: notes-app
display_name: "Notes"
version: 1.4.0
authors:
  - "Jane Smith <jane@example.com>"
license: MIT
homepage: https://github.com/yorik-community/notes-app
description: |
  Personal notebook with handwritten note OCR. Attach notes to
  contacts and events.
icon: icon.svg                       # 512x512, packaged in the app dir

# ── Tables this app owns ────────────────────────────────────────────
# These get created in the app's own Postgres schema (`app_notes_app`).
# The app is the only thing that can touch them. RLS is applied per
# the policies file. The user's normal export-my-data flow includes
# everything in this schema.
owned_schema: app_notes_app
owned_tables:
  - notes
  - note_attachments

# ── Yorik data this app needs ──────────────────────────────────────
# Read scopes the app requests. The user sees these on the consent
# screen and explicitly approves each one. Granular columns matter:
# "contacts.display_name" is much less scary than "all of contacts."
permissions:
  reads:
    - table: contacts
      columns: [id, display_name, kind]
      purpose: "Autocomplete @mentions in notes"
    - table: events
      columns: [id, title, starts_at]
      purpose: "Show notes-per-event timeline"
  writes:
    # Apps can never write to Yorik core tables. If they need to
    # create a contact, they call a Yorik skill (see invokes_skills).
    []
  invokes_skills:
    - find_person                    # for fuzzy contact resolution
  realtime_subscriptions:
    - contacts                       # listen for new contacts
    - app_notes_app.notes            # own table, listen to self

# ── Optional sandboxed backend ─────────────────────────────────────
# Most apps don't need a backend. The ones that do (OCR, ML, third-
# party integrations) declare it here. Yorik runs it under a per-app
# Linux user with no privileged sockets.
backend:
  enabled: false
  # When enabled:
  # runtime: python3.12 | node20 | static
  # entrypoint: app.py | server.js
  # health_check: /health

# ── UI ─────────────────────────────────────────────────────────────
ui:
  type: spa                          # spa | iframe | none
  entry: ui/dist/index.html
  mount_path: /apps/notes            # served by Yorik at this path
```

### Field-by-field justification

- `manifest_version: 1` — explicit so we can evolve the format. Yorik refuses to install a manifest version it doesn't understand.
- `owned_schema` — every app gets its own Postgres schema. Hard boundary: no app can `SELECT FROM app_other_app.secrets`. Default name is `app_<name>` but apps can pick.
- `owned_tables` — listed explicitly so Yorik knows what to drop on uninstall. The `schema.sql` is the source of truth for shape; this list is for lifecycle.
- `permissions.reads.table` + `columns` — column-level scoping is the difference between scary and not-scary. RLS at the table level is what Supabase ships; we add a thin layer that projects only the granted columns into the app's view of the table.
- `permissions.writes: []` — the default. Apps don't write to Yorik core data. If they need to, they call a skill. That skill can have its own validation, audit log, consent flow.
- `invokes_skills` — apps can ask Yorik to do things on their behalf. The skill registry already knows what tables each skill touches; the consent screen surfaces that automatically.
- `realtime_subscriptions` — opt-in per table. Subscribing eats CPU on the Postgres logical replication slot; we won't let an app subscribe to a table it doesn't have read permission on.
- `backend.enabled: false` — start strict. If we permit backends, each one runs as a separate Linux user with cgroup limits + no network egress unless declared.

---

## 3. Schema & policies — `schema.sql` + `policies.sql`

`schema.sql` runs once on install, inside the app's owned schema. Standard Postgres DDL. Migrations between app versions live in `migrations/NNN.sql` — Yorik runs the missing ones on upgrade.

`policies.sql` declares the RLS for the app's owned tables. Every owned table MUST have at least one policy or Yorik refuses the install.

Example for `notes`:

```sql
-- notes are owned by the user who created them.
-- Spaces / workspace scoping is inherited from a `space_id` column
-- per the Yorik-wide convention. The default policy below uses
-- the `yorik_visible_spaces()` SQL function that Phase E will ship
-- — same logic as backend/spaces.py:user_visible_space_ids(), but
-- as a SQL function callable from RLS.

ALTER TABLE app_notes_app.notes ENABLE ROW LEVEL SECURITY;

CREATE POLICY notes_select ON app_notes_app.notes
  FOR SELECT
  USING (space_id = ANY (yorik_visible_spaces()));

CREATE POLICY notes_insert ON app_notes_app.notes
  FOR INSERT
  WITH CHECK (
    owner_user_id = auth.uid()
    AND space_id = ANY (yorik_visible_spaces())
  );

CREATE POLICY notes_update ON app_notes_app.notes
  FOR UPDATE
  USING (owner_user_id = auth.uid())
  WITH CHECK (owner_user_id = auth.uid());

CREATE POLICY notes_delete ON app_notes_app.notes
  FOR DELETE
  USING (owner_user_id = auth.uid());
```

The `yorik_visible_spaces()` SQL function is Yorik's gift to apps: it returns the list of space ids the caller can see, exactly mirroring Phase C's Python logic. Every app's RLS can lean on it.

---

## 4. Install lifecycle

```
1. User clicks "Install Notes" in marketplace.
2. Yorik fetches the manifest from yorik-community/apps/notes-app/manifest.yaml
3. Manifest is parsed + validated against the spec version.
4. Consent screen renders — see §6.
5. User taps "Install" or "Cancel."
6. On install:
   a. CREATE SCHEMA app_notes_app
   b. \i schema.sql            (creates app tables)
   c. \i policies.sql          (RLS on app tables)
   d. For each granted permission scope, Yorik generates a Postgres
      VIEW in the app's schema that projects only the granted columns:
        CREATE VIEW app_notes_app._yorik_contacts AS
        SELECT id, display_name, kind FROM public.contacts;
      Plus a passthrough RLS policy:
        CREATE POLICY _yorik_contacts_select ON _yorik_contacts
        FOR SELECT USING (true);   -- inherits public.contacts's policy
   e. CREATE ROLE app_notes_app_user NOINHERIT LOGIN PASSWORD '<random>'
      with usage on app_notes_app schema only.
   f. Issue a Supabase API key bound to that role.
   g. INSERT INTO yorik_installed_apps (manifest, granted_at, granted_by, scopes…)
   h. Hot-mount UI at /apps/notes.
   i. If realtime subscriptions requested: ALTER PUBLICATION supabase_realtime
      ADD TABLE app_notes_app.notes, public.contacts.
7. On uninstall:
   a. DROP SCHEMA app_notes_app CASCADE  (app tables + views go)
   b. DROP ROLE app_notes_app_user        (key revoked)
   c. ALTER PUBLICATION ... DROP TABLE for the realtime entries
   d. UPDATE yorik_installed_apps SET uninstalled_at = now()
   e. Backup blob of app's owned data offered to user as a download.
```

Idempotent. Install/uninstall/reinstall in any order leaves the system in the right state.

---

## 5. The app's data view

From inside the app's UI, every query goes to PostgREST or Realtime with the per-app API key. RLS does the rest:

```js
import { createClient } from '@supabase/supabase-js'

// The key is provisioned at install time, exposed to the app's UI
// via window.YORIK_APP_KEY (server-rendered injection — never in JS bundle).
const supabase = createClient(window.YORIK_SUPABASE_URL, window.YORIK_APP_KEY)

// Read the app's own tables — full CRUD per the app's policies.
const { data: notes } = await supabase
  .from('notes')             // resolved to app_notes_app.notes via search_path
  .select('*')
  .order('created_at', { ascending: false })

// Read granted Yorik tables — through the view, projected to declared columns.
const { data: contacts } = await supabase
  .from('_yorik_contacts')   // the view created at install time
  .select('id,display_name')

// Realtime — same surface.
supabase.channel('notes')
  .on('postgres_changes',
    { event: 'INSERT', schema: 'app_notes_app', table: 'notes' },
    payload => console.log('new note:', payload.new))
  .subscribe()

// Invoke a Yorik skill — POSTs to Yorik's kernel API.
const resp = await fetch('/api/skills/invoke', {
  method: 'POST',
  headers: { 'Authorization': 'Bearer ' + window.YORIK_APP_KEY },
  body: JSON.stringify({ skill: 'find_person', args: { query: 'Anna' } }),
})
```

The app developer never sees `service_role`, never has cross-app access, can't escape its grant. RLS + role + view scoping is the cage.

---

## 6. Consent UX

The most important screen in the entire OS. It's where trust is earned.

```
┌──────────────────────────────────────────────────────────┐
│  Install Notes 1.4.0?                                    │
│                                                          │
│  By Jane Smith · MIT licensed · github.com/.../notes-app │
│                                                          │
│  Notes wants to:                                         │
│                                                          │
│  ✓ Create its own tables for storing notes               │
│      app_notes_app.notes, app_notes_app.note_attachments │
│                                                          │
│  → Read parts of your contacts                           │
│      display_name, kind (person/business)                │
│      Why: "Autocomplete @mentions in notes"              │
│                                                          │
│  → Read parts of your events                             │
│      title, starts_at                                    │
│      Why: "Show notes-per-event timeline"                │
│                                                          │
│  → Ask Yorik to find people by name                      │
│      (invokes the find_person skill)                     │
│                                                          │
│  → Get realtime notifications when                       │
│      • a new contact is added                            │
│      • a note changes                                    │
│                                                          │
│  This app CANNOT:                                        │
│  ✗ Write to any of your existing data                    │
│  ✗ See data outside the scopes listed above              │
│  ✗ Run background tasks on your computer                 │
│  ✗ Make outbound network connections (no backend)        │
│                                                          │
│  [Show source code]  [Cancel]   [Install]                │
└──────────────────────────────────────────────────────────┘
```

Two rules for this screen, immovable:

1. **Every grant is in plain language.** "Notes wants to read parts of your contacts (display_name, kind) — why: autocomplete @mentions." No `permission_v3.scope.contacts.read` jargon.
2. **The negative space is shown.** Listing what the app CANNOT do is more reassuring than listing what it can. Apple-easy is partly about saying "no" loudly.

The consent record (which scopes were granted, when, by which user) is auditable in Settings → Installed apps → Notes → Permissions. Revoking a scope downgrades the API key in-place (Yorik regenerates the view + role grants).

---

## 7. Security boundaries (sorted by "what an attacker would try")

1. **Reading another app's data**: app_a's role has USAGE only on app_a schema. PostgREST refuses cross-schema queries.
2. **Privilege escalation via SQL injection in the app's own UI**: the API key only has the rights granted at install. Injection can corrupt the app's own data but cannot read Yorik core data outside the declared scopes.
3. **Manifest tampering after install**: Yorik stores a content hash of the manifest at install time. Upgrades compare hash; new permissions require a fresh consent screen.
4. **Network egress from an app's backend**: backends are opt-in. When enabled, run under a per-app Linux user with `iptables` egress rules per the manifest's `network` block (next iteration of the spec).
5. **Replay of the API key after uninstall**: uninstall DROPs the role. Subsequent connections with the old key fail at the auth layer, not RLS.
6. **App lying about its purpose**: the marketplace requires source code in `yorik-community`. Manual review for the first 100 apps; later a static-analysis pass on the manifest + code (does the code actually call only the granted scopes?).
7. **Compromised app vendor pushing a malicious upgrade**: pinned versions + signed manifests (sigstore or our own root). Upgrades default-off if signatures don't match.

---

## 8. What stays in the Yorik kernel

Apps don't touch these — Yorik keeps them as the privileged backbone:

- **LLM agent / skills / chat**. The agent is fundamentally a state machine over privileged tool calls; RLS can't gate it.
- **Connectors**. Paperless ingest, Immich provisioning, WhatsApp bridge, email sync. These read from external services and write to Yorik core tables. They need service-role, not user RLS.
- **App lifecycle**. The install/uninstall/upgrade/consent UX itself.
- **Background reconcilers**. Drift detector, embedding rebuild, paperless poll.
- **Yorik UI**. The chat / dashboard / agenda. It uses the user's JWT against PostgREST — same surface as third-party apps — but it's bundled.

Yorik FastAPI shrinks but doesn't disappear. Its job becomes "the OS kernel" — hardware drivers (connectors), the shell (chat/agent), and the app loader.

---

## 9. Open questions for the next session

- **Backend-enabled apps**: how do we sandbox them? Bubblewrap? containerd? gVisor? Per-app cgroups + iptables is the cheap answer; a real sandbox is a multi-week project on its own.
- **Inter-app communication**: should two installed apps be able to discover each other and request data? Probably not in v1 — every interaction goes through Yorik's skills as the broker.
- **App SDKs in non-JS languages**: the manifest is YAML, the API is PostgREST. Any language with an HTTP client can build an app. Python / Go / Swift / Kotlin all work the same day. Do we ship native SDKs or just docs?
- **App pricing / payments**: out of scope for v1. Marketplace ships free + OSS only. Paid apps need a billing layer (Stripe Connect? local-only?). Defer.
- **Sync / multi-device**: realtime gives us this for free once apps subscribe. But the "your phone shows the same notes as your laptop" UX needs more thought (device auth, conflict resolution).
- **App-to-app data sharing via Yorik**: e.g. "the chat app wants to attach a note from the notes app to a message." Probably modeled as the chat app calling a Yorik skill that mediates the cross-app reference. Defer until a real app surfaces the need.

---

## 10. Recommended next concrete steps

Once you read this and want to move forward:

1. Skim § 2 (manifest) and § 6 (consent UX) — those are the two surfaces that ripple the longest.
2. Pick three real apps to design for, before any code. **Notes**, **Mini-CRM**, **Family-chat** are the canonical trio: one read-only-from-Yorik, one read-write-via-skills, one realtime-heavy. If the spec works for all three, it's the right shape.
3. Write skeleton manifests for those three apps. No code, just YAML + a one-sentence description of the UI. This is the cheapest way to find the bugs in the spec.
4. Decide on Phase E sequencing relative to Phase D cutover. My recommendation in the conversation was: cut over Phase D first, then start Phase E on a fresh branch. The cutover proves the Postgres backing works; Phase E builds the dev story on top.
5. Begin Phase E with the RLS migration of one table (e.g. `contacts`). Verify PostgREST returns workspace-scoped data. That single end-to-end slice de-risks the entire phase.

---

This document will rot the moment we start implementing. Treat it as the inception sketch — fight with it before code, but rewrite it once the first real install happens.
