# Changelog

All notable changes to Yorik are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased] — robustness pass (August 2026)

No new features. This is the "works on a stranger's box" pass after the
first two alpha months, done with the code audit of 2026-08-24 as the
worklist.

### Added

- **MCP server.** `POST /mcp` exposes the skills registry to outside
  agents (Hermes, Claude Code, scripts) over Streamable HTTP. One tool
  per skill the caller may use, schemas generated from `skill.md`,
  `skill_view` / `pending_confirm` / `pending_cancel` / `whoami` on top.
  Deletions still stage a confirmation the agent has to resolve.
  Personal API tokens (Settings → You → API tokens, shown once, hashed
  at rest) are the only credential; a token acts as its owner and can
  also call `/api/*`. See [docs/MCP.md](docs/MCP.md).
- **Sharing per area.** Settings → You → Sharing: for every household
  member, tick which of your areas they may see (tasks, calendar,
  contacts, documents) and whether they may edit. Stored as a scoped
  membership in your personal space; admins can set it for restricted
  (child) accounts.
- **Admins no longer see other members' private spaces.** Search,
  chat, calendar and tasks apply the same rule to everyone: own space
  plus shared spaces plus explicit memberships. The former
  "platform_admin sees all" behaviour survives only behind an explicit
  flag for administrative tooling.
- **Yorik on your phone, without a messenger.** Web Push: every bell
  notification reaches the installed app while it is closed (Settings →
  You → Notifications on your phone), with two optional daily nudges
  that open the chat with "Plan meinen Tag" and "Wie war mein Tag?".
  A new skill `notify` lets an outside agent (Hermes over MCP) leave a
  message in the bell, so its results arrive in Yorik instead of in a
  chat platform. The chat shows what it is waiting for ("Fragt Hermes…").
- **Compact MCP surface as an option.** `/mcp?tools=compact` offers
  seven tools with the skills behind `invoke_skill` (the one-line index
  in its description), about 11 KB of schemas instead of 64 KB, for
  clients that load every schema into the prompt. The default stays one
  tool per skill: Hermes fetches schemas on demand, and measured turns
  were faster that way. Results of applied creates now say "nothing to
  confirm" so agents stop looking for a confirmation step.
- **Plan your day in conversation.** `plan_my_day` gathers fixed
  appointments, open tasks, carry-over from yesterday, yesterday's
  review and, when the workstation agent is reachable, its briefing;
  the assistant drafts the day and iterates with you. `plan_day` then
  writes it: every item a task, at most six items as time blocks in a
  private "Plan" calendar, linked to their tasks, idempotent per day and
  item key, one card undoes the whole day. `day_review` compares plan
  and reality in the evening (done tasks, estimated vs actual minutes
  from the task timer, carry-over) and feeds the next morning. Tasks now
  record when they were finished.
- **One rule for "same contact".** `backend/contact_identity.py` decides
  identity by email or phone (E.164 via libphonenumber; a WhatsApp id is
  its phone number). The vCard import, the phone picker, `add_contact`,
  the New-contact form and the email/WhatsApp autocapture all go through
  it. Yorik never merges on its own any more: a phone number found in an
  email signature that belongs to another contact becomes a *proposal*
  in the Contacts page; accepting it merges (channels, addresses, blank
  fields), the loser becomes a tombstone, and every merge can be undone.
  On Android the Contacts page picks entries straight from the phone's
  address book.
- **Removed** the contact features that produced or destroyed data
  without a review step: the Paperless document walk, the mailbox
  crosslink, group-by-employer, the LLM dedupe and the name-based dedupe
  that deleted rows. Mass-mailer detection now matches domains, not
  substrings, and `meinhard@…` is no longer treated as a role address.
- **Yorik can ask the agent back.** The skill `ask_agent` hands a
  question to an OpenAI-shaped agent endpoint (Hermes' API server) and
  relays the answer, one session per user and conversation. Configured
  via `HOMEOS_AGENT_URL` / `HOMEOS_AGENT_KEY`; not exposed over MCP.
- **Agents cannot delete on their own.** A deletion an agent stages lands
  as a Delete / Keep card in the owner's notification bell; only that tap
  runs it. Settings → Beta safety → "Let agents confirm deletions" opts an
  account out of the guard.
- **Audit + admin view for tokens.** Skill calls made through a token are
  logged with `source = token:<name>` in `skill_invocations`; admins see
  and revoke every member's tokens under Settings → API tokens.
- **Leaner Supabase.** Kong runs two nginx workers instead of one per CPU
  thread (3.2 GB → ~0.3 GB on a 32-thread box); Studio, pg-meta and the
  edge runtime move to the compose profile `full` and stay off unless
  asked for. The default stack now needs ~1.5 GB.
- **Parakeet speech-to-text.** NVIDIA parakeet-tdt-0.6b (int8 ONNX via
  sherpa-onnx) replaces Whisper as the default engine: CPU only, about
  100 ms per utterance, no torch. German fine-tune or multilingual
  variant, picked by household language, downloaded once by `start.sh`
  or from Settings → Speech-to-text. Whisper is an optional extra.
- **Torch-free backend.** Speaker identification moved from SpeechBrain
  ECAPA to sherpa-onnx WeSpeaker CAM++ (29 MB; enrolled voices need a
  re-enrol), the bundled embedder runs the MiniLM ONNX export through
  onnxruntime (same vectors as before, no re-ingest). torch, torchaudio,
  transformers, sentence-transformers, speechbrain and openai-whisper
  left `requirements.txt`: the venv shrinks from 5.7 GB to 1.1 GB and
  the backend from ~2 GB to well under 1 GB resident.
- **Speech server.** `POST /v1/audio/transcriptions` and
  `POST /v1/audio/speech` speak OpenAI's audio API shape with a personal
  API token, so Dictate, Hermes and scripts use Yorik's Parakeet and
  Supertonic instead of their own copies. Formats: wav, pcm (24 kHz),
  mp3, opus, flac. See [docs/SPEECH.md](docs/SPEECH.md).

### Changed / fixed

- **One database.** Postgres (the bundled Supabase stack) is the only
  backend; the SQLite path, its 64-file migration series and the
  SQLite-only tooling are gone. Startup probes the database and fails
  with one sentence instead of a 30 s pool timeout. `/api/health`
  reports `database` and `credential_key`.
- **One front door.** `/` lands on the React app; the legacy vanilla
  frontend is removed. Old top-level app paths redirect.
- **Installer.** Survives a fresh box where it installs Docker itself
  (re-exec under `sg docker`); Supabase pinned to a commit and fetched
  as `docker/` only; Supavisor moved to 127.0.0.1:5434/6544 so a local
  PostgreSQL no longer breaks pre-flight; torch stack pinned, CPU
  wheels without a GPU; 50 GB disk floor; no-GPU boxes get Ollama +
  Qwen 3.5 9B as the README always said. One systemd unit template.
  `yorik upgrade` runs the migrations through the bootstrap and
  restarts via systemd (SIGTERM, never SIGKILL).
- **Deletes ask first.** `delete_*` skills stage the delete and show a
  Delete / Keep card; nothing is removed until the user taps Delete.
  Creates and updates keep apply-then-undo.
- **Roles.** Title lookups (events, tasks, subtasks) are scoped to what
  the caller may see; `install_connector` is admin-only for real.
- **WhatsApp.** Bridge on loopback behind a shared token; survives boot
  without network, re-pairs with a fresh QR after the phone unlinks,
  reconnects with backoff. Semantic search works on Postgres (pgvector).
- **Doesn't fall over.** Chat history trimmed to the model's context
  (`YORIK_LLM_CTX`); LLM failures reach the user as one readable
  sentence in their language; timeouts are not retried; voice and
  suggestion LLM calls off the event loop; bounded media processing;
  email supervisor restarts.
- **Chat UI.** Expired session → login screen (not "LLM offline");
  Stop keeps the streamed text; 2-minute stall watchdog; toast instead
  of 87 `alert()`s; an error in one app no longer takes the dock down.
- **Tests + CI.** The suite runs on a throwaway Postgres (locally and in
  GitHub Actions); `scripts/smoke-check.sh` walks a live install.

### Removed

- SQLite backend, `migrations/`, `scripts/seed-demo-*.py`,
  `cold-install-check.sh`, `backup-restore-drill.sh` (a Postgres-safe
  restore drill is on the list), the unfinished container-image path
  (parked under `docs/experimental/docker/`).

## [0.1.0-alpha] — June 2026

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

