# Building a Yorik add-on: the dinner recorder, step by step

This is the protocol of how the Recordings feature was built in one
day (2026-09-10), written so you can build your own add-on the same
way. It is the honest version: what an add-on in Yorik is made of
today, which pieces are optional, and where the sandboxed community
app platform (`docs/BUILD_AN_APP.md`) does and does not help yet.

## What an add-on is, today

Yorik has one extension mechanism that is complete and used by
everything Yorik does itself: **a skill**. A skill is a folder with a
`skill.md` (what it does, its arguments, who may call it) and a
`skill.py` (an `execute` function). Once the folder exists, the skill
is callable

- by Yorik's own assistant in chat and by voice,
- by any outside agent over MCP (Hermes, Claude Code) with a personal
  token, with the same rights as the token's owner,
- by other skills.

Around a skill you add what the feature needs: a table (a numbered
migration), a backend module, REST routes for a UI, a page in the
React app, tests. Not every add-on needs all of that. `notify` is a
skill and nothing else; `plan_my_day` is three skills and one module;
the dinner recorder is the full set.

The five layers, in the order they were built:

| Layer | Dinner recorder | Where |
| --- | --- | --- |
| 1. Data | `recordings`, `recording_segments` | `migrations_pg/136_recordings.sql`, `137_…` |
| 2. Module | storage, pipeline, retention, routes | `backend/recordings.py`, `backend/recording_reports.py` |
| 3. Skills | `start_recording`, `finish_recording`, `recording_status`, `recording_report` | `backend/skills/<name>/` |
| 4. UI | Recordings app, recorder pill, start dialog, kiosk tile | `frontend-react/src/apps/recordings/`, `components/RecorderDock.tsx`, `components/RecordingStartDialog.tsx`, `apps/ambient/AmbientApp.tsx` |
| 5. Tests + docs | pytest on a throwaway Postgres, help page, changelog | `tests/test_recordings.py`, `tests/test_recording_reports.py`, `docs/help/17-recordings.md` |

Read `docs/SKILLS.md` once before this; it explains the skill
contract in full. This document is about how the layers fit.

## Step 1 — the data, and who may see it

A migration is a numbered SQL file in `migrations_pg/`. It runs once,
in one transaction, on the next start. Use `IF NOT EXISTS` everywhere
so a re-run is harmless.

Decide visibility on day one. Yorik's rule is: **a row belongs to
its owner, is visible inside its space, and can be shared per row**
(`backend/spaces.py`). A recording lives in the owner's personal space
and every participant gets a `row_shares` row with `level='read'`.
Two lines make the rest of Yorik enforce that:

```python
# backend/spaces.py, row_filter(): the owner column of your table
"recordings": "owner_user_id",
```

and in your module, one query that wraps every read:

```python
frag, params = spaces.row_filter(user["id"], user.get("role"), "recordings")
conn.execute(f"SELECT * FROM recordings WHERE id = ? AND {frag}", (rid, *params))
```

There is no admin exception. Admins who were not at the table get a
404 like everybody else; the test `test_record_process_and_participant_visibility`
proves it for member, restricted and a second admin.

## Step 2 — the module

One Python file per feature, no framework. Conventions that matter:

- **Timestamps** are local time strings written by Python (`_now()`),
  not `now()` from Postgres (which is UTC in the container). Mixing
  the two shows up as a two-hour offset in the UI.
- **Files** go under `data/<feature>/<id>/`. Nothing in the repo
  serves files by magic; add a route with the same visibility check.
- **Long work** runs in a thread from a `ThreadPoolExecutor(max_workers=1)`
  so it never blocks the chat, and reports progress into a column the
  UI polls (`status`, `progress`, `error`). There is no job queue in
  Yorik; the pattern is `loop.run_in_executor(_executor, fn, id)` and
  a `start_scheduler(loop)` for periodic sweeps, registered with
  `backend/workers.py` so it shows up on the home screen.
- **Failures are rows, not exceptions.** `process()` sets
  `status='failed'` with the message; `finish` can be called again.
- **Swappable steps.** The pipeline steps (`decode_audio`, `diarize`,
  `identify_clusters`, `transcribe_segment`) are module-level functions
  so tests replace them with fakes and never load a model.
- **Notifications** are one call: `notifications.create(user_id, kind,
  title, body, navigate_to=…)`. It writes the bell entry and pushes to
  the phone by itself.

Routes are a FastAPI `APIRouter` at the bottom of the same file, one
`app.include_router` line in `backend/main.py`. Anything under `/api/`
requires a session unless it authenticates on its own; the two
device routes (chunk upload, finish) do that with a per-recording
token, so a shared tablet keeps uploading after someone else signs in.

## Step 3 — the skills

Skills are the surface the language model and outside agents see, so
their text is prompt material. Rules that held up:

- `description` is one sentence. Each line of `when_to_use` is one
  sentence with one instruction.
- A skill does one thing and returns data plus an `_llm_hint` that
  tells the assistant how to phrase the answer. It does not phrase the
  answer itself.
- **Chat and MCP differ where the device matters.** From chat, ending
  a recording only marks `stop_requested`, because the device that
  holds the microphone must upload its last piece first. Over MCP the
  audio is already on the server, so the same skill finishes at once.
  `ctx.source` tells them apart.
- Skills talk to the UI through `ui_actions`
  (`backend/ui_tools._append({...})`). The frontend forwards every
  non-sticky action to a global bus; a component anywhere listens.
  That is how "Yorik, record the dinner" starts the recorder pill.
- Skills that must not be reachable by outside agents get the tag
  `no-mcp`. The recording skills are reachable; `ask_agent` is not.

## Step 4 — the UI

Three things were needed, and they are three reusable parts:

1. **A page** (`apps/recordings/RecordingsApp.tsx`): register the app
   in `backend/apps.py`, add its id to `frontend-react/src/lib/dock-order.ts`
   and a visual to `components/Dock.tsx`, add the route in `main.tsx`.
   Fetch with `api.get/post`; the same-origin session cookie does the
   rest. Two panes on wide screens, list then detail on the phone.
2. **A global component** (`components/RecorderDock.tsx`): the recorder
   must outlive route changes, including the kiosk's automatic return
   to the photo wall. It is mounted next to `NavigationBridge`, outside
   the chrome gate, keeps its state in module scope, mirrors it to
   `sessionStorage` to survive a reload, and listens to `ui_actions`.
3. **A dialog** (`components/RecordingStartDialog.tsx`) used by the
   page and the kiosk tile. The kiosk tile (in `AmbientApp.tsx`) first
   opens the existing sign-in picker so the recording belongs to a
   person, then the dialog.

`frontend-react/dist/` is committed; run `npm run build` after any
`src` change or fresh installs ship the old UI.

## Step 5 — tests, then a real run

Tests run against a throwaway Postgres (`venv/bin/python -m pytest
tests -q`). For an add-on with models, test the flow with the model
steps faked, and test the pure helpers (label numbering, turn merging,
JSON repair) directly. Then do one real run against the workstation
with `curl`, read the JSON, and only then open the browser.

The real run is where the surprises are. For the recorder they were:
the WeSpeaker embedding model clusters real voices badly and the
3D-Speaker model well; Qwen hands nested lists back as JSON strings
and quotes the transcript with German quotation marks that break JSON;
the session middleware rejected the device routes before they could
authenticate; Postgres wrote UTC start times. Each one became a test.

## Where the community app platform stands

`docs/BUILD_AN_APP.md` describes sandboxed apps: five files, their own
Postgres schema, an iframe. It is the right long-term shape for
third-party add-ons, and the dinner recorder was meant to be its first
real app. It is not, because the platform as shipped (verified
2026-09-10) cannot host it:

- The iframe is `sandbox="allow-scripts"` with `connect-src 'none'`:
  no microphone, no `fetch`, no cookies, no `network.outbound` effect.
- Connector operations receive no user and no role; `role=[…]` on
  `@operation` is not enforced; `permissions.invokes_skills` and
  `permissions.scheduled` are validated and shown on the consent
  screen but never acted on.
- The app cannot learn who is using it, so per-user rows and
  per-person visibility are not possible from inside an app.

What it would take to make the dinner recorder a community app, in
order: a user and role in every operation call; a skill-invocation
function in the SDK that honours `invokes_skills`; a host bridge that
lets an app ask the host to record (the host owns the microphone) and
to show a notification; and a `connect-src` that includes Yorik's own
API for apps that declare it. Until then, an add-on that needs a
microphone, files, background work or per-person visibility is built
the way this one was: skills plus a core module. That path is
complete, tested, and reachable from chat, voice, MCP and the app.

## Checklist for your own add-on

1. One migration, `IF NOT EXISTS`, owner column, decide who sees rows.
2. One module: storage, the work in a worker thread, status columns,
   routes with the visibility check, a retention sweep if you keep files.
3. Skills with one-sentence lines; `_llm_hint` instead of prose;
   `ui_actions` for anything the UI must do; `no-mcp` where an agent
   must not reach.
4. Page + global component + dialog only when the feature needs them;
   register the app, add the dock entry, rebuild `dist/`.
5. Tests with faked models; one real `curl` run; one browser run.
6. A help page in `docs/help/` (the assistant reads it to answer
   "how do I…"), a changelog entry, the env keys in `config.env.example`.
