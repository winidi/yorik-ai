# Changelog

All notable changes to Yorik are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

This release is the alpha-launch set: the agent stops generating SQL,
community apps get a real sandbox with a marketplace UI, and the
network-bind story finally matches how people actually use Yorik.

### Security

- **`start.sh` binds to `0.0.0.0` by default.** A brief detour to
  `127.0.0.1`-only proved wrong in practice: every install required an
  opt-in flag (`YORIK_BIND=0.0.0.0`) to reach Yorik from a phone,
  which is the whole point of a self-hosted personal OS, and users
  silently forgot the flag on every restart. The default is now LAN-
  accessible again, with a prominent printed warning reminding the
  user that login is bcrypt over plain HTTP and anything beyond a
  trusted home LAN needs Tailscale or a reverse proxy with TLS in
  front. To restrict to localhost (laptop you carry around, SSH-tunnel
  workflows): `YORIK_BIND=127.0.0.1 bash start.sh`. The restart helper
  (`scripts/restart-uvicorn.sh`) follows the same default so a restart
  never silently strips LAN access from a running household.
- **The LLM no longer generates SQL.** `run_sql` was removed from the
  agent's tool registry; every read and write now flows through a
  typed skill call that is auditable, named, and role-gated by the
  registry. Closes the prompt-injection-to-SQL surface entirely. The
  saved-queries cache is disabled in the same move (removes a small
  cross-role replay wrinkle in the cache layer).
- **Community apps run in a fully isolated iframe.** Sandbox is now
  `allow-scripts` only (opaque origin — no cookies forward) plus a
  strict CSP (`default-src 'none'`, `connect-src 'none'`, etc.). Apps
  cannot `fetch()` Yorik's API, cannot read the session cookie, and
  cannot reach `window.parent.document`. The single permitted I/O
  path is `window.yorik.callOperation(...)` via postMessage, which
  the parent gates by app-id namespace on both the bridge and backend
  sides. Browser-verified end-to-end (Playwright).

### Added

- **In-Yorik Marketplace** (Settings → Marketplace). Reads from
  `marketplace/catalog.json`, shows installable apps with author,
  version, tags, and a "Verified" badge for first-party entries.
  One-click install/uninstall via dedicated backend endpoints
  (`/api/apps/available`, `/api/apps/install_from_catalog/{id}`).
- **Install confirmation modal** that surfaces what the app will
  access (its own DB, the LLM, any declared `requires_tables_external`
  or `requires_connectors`) and what it's explicitly sandboxed away
  from (your other Yorik data, outbound network, the session cookie)
  before the user clicks Install.
- **Uninstall confirmation modal** that explicitly states the
  `data/apps/<id>/` wipe path. "Keep installed" is the safe default.
- **Notes reference app** (`examples/notes/`). Three `@operation`
  functions (`add_note`, `list_notes`, `summarize_today`), its own
  SQLite at `data/apps/notes/data.db`, and an editorial two-pane UI
  (Bear / Apple Notes lineage). Uses `llm.ask()` for mood inference
  and summarization to demonstrate the SDK end-to-end.
- **`llm` export in the App SDK.** `from yorik.app_sdk import llm`
  gives apps `llm.ask(prompt, system=)` and `llm.complete(messages)`
  against the same model Yorik chat uses. Closes the doc lie where
  the README promised this but the export didn't exist.
- **`yorik` namespace package shim** so `from yorik.app_sdk import
  operation, db, llm` actually works (apps no longer have to know
  Yorik's internal `backend/` layout).
- **App self-call endpoint** (`POST /api/apps/{app_id}/op/{op_name}`).
  Lets an app's iframe invoke its own operations without the per-
  layout connector grant; namespace-checked.
- **`CommunityApp` React shell** at `/r/community-app/:appId`.
  Community apps now render inside the modern Yorik SPA with the
  same Dock and chrome as bundled apps, not the legacy vanilla
  frontend.
- **Marketplace safety banner** explaining the sandbox model up
  front so users have correct expectations before they install.
- **Graceful error state for community apps.** When the manifest
  or UI fetch fails, the iframe area renders a friendly message
  with a "Try again" button (bumps a load nonce so the effect re-
  runs) and "Back to home". Replaces the infinite spinner.

### Changed

- README and `docs/SKILLS.md` skill count corrected (was ~42, is now
  ~60). The catalogue tables in `docs/SKILLS.md` are now framed as
  a curated highlight reel rather than an exhaustive list.
- DELETE-related operating rules moved to the top of the system
  prompt where they conceptually belong, out of the now-deleted
  `run_sql` framework-tool section.

### Removed

- `run_sql` is no longer a tool the LLM can call. The
  `RoleGatedSqliteRunner` class still exists in `backend/ask.py` as
  inert code (no registrations, no callers); future cleanup.
- The cache lookup/save calls in `backend/agent/loop.py` are gone.
  `backend/agent/cache.py` and the `saved_queries` table remain on
  disk for future migration; no new rows will be written.

### Notes for first installers

- `apps/` (the runtime install dir for community apps) is now in
  `.gitignore` — community apps land there when installed via the
  Marketplace.
- The `manifest signatures` story for unvetted third-party apps is
  not in this release. The Verified badge currently keys off the
  `author` string in `marketplace/catalog.json`, which the
  maintainers control. The day outside contributors submit apps to
  the catalog, this becomes the next thing to harden.

