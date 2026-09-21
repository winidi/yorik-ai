# Changelog

All notable changes to Yorik are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased] — robustness pass (August 2026)

No new features. This is the "works on a stranger's box" pass after the
first two alpha months, done with the code audit of 2026-08-24 as the
worklist.

### Added

- **Schreiben: invoices, quotes and a valid e-invoice (third stage).**
  Line items as data (any number of them), sums in `Decimal` by the
  app, never by the model; § 14 UStG checklist beside the sheet;
  "Fertigstellen" takes the number (per person, made on first use) only
  after the PDF and the e-invoice have worked, so a failure leaves no
  gap; quote → invoice in one click; chat skill `write_invoice`.
  E-invoices are PDF/A-3b with Factur-X EN 16931 XML from the same
  figures, checked before delivery (XSD plus the sums re-added from the
  XML) and confirmed with the Mustang reference validator for standard,
  two-rate and small-business invoices (`scripts/validate_einvoice.sh`).
  The old Compose path never produced a valid one (no PDF/A-3, profile
  label mismatch, silent fallback); facturx's own schematron check
  silently skips without a Saxon server and is not relied on.
- **Schreiben: letters (second stage).** A small app at `/r/write`,
  switched on under Settings → Apps. The chat skill `write_letter`
  writes the whole letter as a draft and never asks back; the address
  comes from the contacts, what is missing is marked on the sheet.
  Editor on the sheet with a live page preview, "Yorik, überarbeite"
  for a marked passage or the whole text, PDF, send from your own mail
  account, file in Paperless with a visibility; sent or filed letters
  are final and can be copied into a new draft. While Schreiben is on,
  Compose's chat skills rest. See
  [docs/help/19-schreiben.md](docs/help/19-schreiben.md).
- **Letterhead (first stage of "Schreiben").** Settings → You → Your
  letterhead: logo, accent colour, font, sender, footer with bank and
  tax numbers, standard sentences, with a live preview of a letter, an
  invoice and a quote and a sample PDF. Started from the profile; each
  person has their own. It is the look of the small documents app that
  will replace Compose (`docs/plans/2026-09-21-dokumente-neubau.md`);
  Compose is untouched. The PDF function can now ask for PDF/A-3b, which
  an e-invoice needs.
- **Family board: timetable, to-do details, appointment details.** A
  fourth view shows the children's school timetables, filled in on the
  board by a parent or the child. "+ Aufgabe" asks for the day; a double
  tap opens a to-do (title, day, repetition: daily, weekdays, weekly …);
  a tap on a calendar entry shows the appointment. The "Als Nächstes"
  line is gone. Fixed on the way: the next instance of a repeating task
  lost its assignees, equally named routines of two people blocked each
  other, and a task's repetition or day could not be cleared.
- **Family board: sort and reword.** A tile in a column you may tick
  has a grip on the right: drag it to put the to-dos (or the routines)
  in your own order; the order is kept per person, new to-dos land at
  the end. A double tap on a tile rewords the to-do in place. Parents
  do both in the children's columns.
- **Family board on the wall.** The kiosk tablet has four modes per
  device (photos, board, calendar, tasks), switched with a button on
  the tablet: the week in everyone's colour and photo, today's tasks
  and routines per person, tap to tick after a PIN sign-in. Each
  person decides whether they appear ("Show me on the household wall").
  See [docs/help/18-family-board.md](docs/help/18-family-board.md).
- **Optional apps that are really off.** Recordings is an opt-in app
  (Settings → Apps, or `YORIK_ENABLE_RECORDINGS=1`); off means no dock
  entry, no kiosk tile, and its skills leave the chat menu and the MCP
  tool list (skills tagged `app:<id>`). Each person has a colour and a
  photo (Settings → You); the personal calendar takes the colour, and
  the calendar, the kiosk sign-in and the kiosk agenda show the photo.
- **My agent, per person.** Settings → You → My agent: the Hermes (or
  any OpenAI-shaped endpoint) Yorik hands your questions to. What one
  member asks never reaches another member's machine; the household
  agent in `config.env` serves people without their own only with
  `HOMEOS_AGENT_SHARED=1`.
- **Recordings.** A conversation at the table (dinner, meeting) is
  recorded by the device in front of the user, uploaded in chunks, and
  turned into a transcript with speaker turns on the CPU: sherpa-onnx
  speaker segmentation (pyannote) + 3D-Speaker clustering, enrolled voice
  profiles put names on the speakers, Parakeet transcribes each turn.
  Only the people named at start can see it (row shares; no admin
  exception); everyone at the table gets a bell entry and a push when
  the transcript is ready. Audio is deleted after
  `HOMEOS_RECORDING_RETENTION_DAYS`, the transcript stays. Skills
  `start_recording`, `finish_recording`, `recording_status`,
  `recording_report`; routes under `/api/recordings`. Dinner and
  meeting recordings get a report right after the transcript (summary,
  decisions, tasks with a person, nice moments, friction, open
  questions, dates); tasks stay proposals until someone adopts them.
  `add_task` now also assigns a named household member, and day
  planning includes tasks assigned to you, not only ones you created.
  UI: a **Recordings** app (dock, phone and desktop) with the report,
  Adopt per task (adopting means "mine"; tick the others for a joint
  errand) and the transcript; a "Record dinner" tile on
  the kiosk wall (sign in, tick who is here, done); one recorder pill
  that survives route changes and the kiosk's idle return, uploads a
  chunk every two minutes with retries, resumes after a reload, and
  keeps uploading with a per-recording token when the shared tablet's
  session changes hands. "Yorik, the dinner is over" ends it by voice.
- **MCP server.** `POST /mcp` exposes the skills registry to outside
  agents (Hermes, Claude Code, scripts) over Streamable HTTP. One tool
  per skill the caller may use, schemas generated from `skill.md`,
  `skill_view` / `pending_confirm` / `pending_cancel` / `whoami` on top.
  Deletions still stage a confirmation the agent has to resolve.
  Personal API tokens (Settings → You → API tokens, shown once, hashed
  at rest) are the only credential; a token acts as its owner and can
  also call `/api/*`. See [docs/MCP.md](docs/MCP.md).
- **Search everywhere, by meaning too.** The search palette and the
  `universal_search` skill (chat, voice, MCP) now cover tasks,
  contacts, recordings (title, report, transcript) and letters next to
  mail, WhatsApp, documents, photos and calendar. A background indexer
  (`backend/search_index.py`, table `search_chunks`, worker
  "search-index" on the home screen) embeds all of it with the bundled
  multilingual model; every source answers by keyword first, then by
  meaning. Visibility is checked at query time with the app's own
  rule, so a changed share needs no rebuild. **Fixed:** the calendar
  branch of the search had no visibility filter and found everybody's
  events, private ones included.
  Optional stronger embedder for it: Qwen3-Embedding-4B in a llama.cpp
  container on the CPU (`scripts/install-search-embedder.sh`), no GPU
  memory; the index records the model per row and rebuilds after a
  switch.
  Settings → Embeddings has the switch (on/off), the choice of model,
  the index progress per source and a rebuild button.
- **Mail: sending a reply no longer fails on a long subject.** Subjects
  were stored with the line breaks of folded headers (550 mails on the
  maintainer's box); "Re: …" on such a mail made Python refuse the
  header and the send answered HTTP 500. Every header value is
  flattened to one line before sending (which also rules out header
  injection through an address), new mails are stored unfolded, a
  migration cleans the stored ones.
- **Mail attachments open reliably.** Opening a mail asked for every
  inline image and attachment at once, and each request logged in to
  the mail server and pulled the whole message again — fifteen logins
  for one letter, which providers answer with a login tarpit (most
  requests ended in 404 after 20 s). The message is now fetched once
  per mail, one login at a time per account, real attachments are kept
  on disk after the first open (removed with the mail), and an
  attachment labelled application/octet-stream is served by its file
  name ("2. Mahnung.PDF" shows as a PDF instead of downloading).
- **Mail drafts survive.** The composer saves what you type 1.5 s after
  the last change and when the window goes away — body text too
  (typing alone never triggered the old autosave) and replies too, one
  slot per mail being answered. Kept on the device until sent or
  discarded; a failed send keeps everything.
- **Mail list** shows sender and subject; two preview lines are a
  toggle. **Composer** sits above the Dock instead of under it.
- **Documents for the parents only.** A fourth visibility, "parents":
  the adults of the household see the document, the children's
  accounts do not. Backed by a Paperless group "parents" that follows
  the roles (every enabled account that is not restricted). In the
  chat card, the Documents app and the skills.
- **"Shared" documents are really shared.** In Paperless a tag does not
  grant access to the documents carrying it, so a document filed as
  "shared" or "business" was visible to its owner only. The visibility
  now sets the household (or business) group's view permission on the
  document itself — after the consume for new uploads, at once when
  changed later — and space pinning uses the key Paperless accepts.
  Filing a chat attachment always asks who may see it (card buttons
  "nur mich" / "die Familie").
- **Bring your Google calendar.** Calendar sidebar → "Google & Co.
  übernehmen": subscribe to a secret iCal address as a read-only
  mirror that follows its source every 15 minutes (address stored
  encrypted, https only, a failed or empty fetch changes nothing), or
  import an .ics file once with a preview (re-import updates, never
  doubles). Series with an end, an interval or exceptions are written
  out with the exceptions applied. One direction only.
- **"Add to calendar" on invitation mails.** The button reads the
  invite's ICS first (exact start and end in your time zone, title,
  place, description) and only then the text. The text reader now
  knows month names in German and English ("25. Sep. 2026",
  "Oct 5, 2026"), AM/PM and the end of a range; before, a Google
  invitation answered "no date could be extracted".
- **Show Yorik a file in the chat.** Paperclip on every screen, drop
  or paste; Yorik reads PDFs, Word, text and (with the vision model)
  photos, says what it is and asks whether to file it in Paperless.
  Filing happens on your yes, by card button or in words (skills
  `read_attachment`, `file_attachment`). What is not filed belongs to
  the conversation only and goes with it or after 30 days; there is no
  second document library. Before, a drop went straight into Paperless
  unasked and the conversation never learned about it.
- **Tasks app shows your own tasks.** Tasks of people who share
  their tasks with you, and tasks you handed to someone else, are
  behind "Others (n)", off by default and kept per device.
- **Timer on the family board.** A long press on a tile starts and
  stops the task's timer, as in the Tasks app; one running timer per
  person, counted by assignee.
- **Family board for parents.** A parent ticks the children's tiles
  and adds a to-do to anyone's column with "+ Aufgabe" (children add
  to their own). The /board page offers "Mich anzeigen" to a person
  who is not on the board yet.
- **A task belongs to its assignees too.** Whoever a task is assigned
  to sees it in their own list, may tick and edit it, and finds it in
  the search, wherever it was created: a parent puts a to-do on a
  child's list from the calendar's task dialog or by chat ("task for
  Yorik: …"). A restricted account cannot delete a task someone else
  gave it. **Fixed:** assigning in the app failed since user ids
  became UUIDs (422), the task list by calendar crashed on the same
  cast, and the calendar share dialog parsed the user id as a number.
- **Family calendars.** In a family household everybody sees
  everybody's calendar, read only, as ordinary per-area sharing each
  person can untick (Settings → You → Sharing). New accounts join the
  default; the operator switches it on once for existing accounts and
  leaves out non-family accounts. Private events show as "Busy", Plan
  calendars are never shared. The calendar sidebar lost its admin
  exception (it listed other people's calendars as empty names) and now
  lists people who shared only the calendar area.
- **Calendar on the desktop.** About 13 hours in view with the hour
  height following the window, quieter event colours, drag to move and
  resize.
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

