# Yorik — architecture

This document is the contract that lets contributors and future devs
build for Yorik without breaking the safety story. It's intentionally short.

## TL;DR

```
┌────────────────────────────────────────────────────────────────────────┐
│  FRONTEND  (frontend-react/)                                           │
│  React 19 + TipTap + Tailwind + shadcn. Single SPA at /r/*.            │
│  Talks to the backend via /api/* and a few SSE streams.                │
│  Never touches credentials. CSP-locked to same-origin.                 │
└────────────────────────────────────────────────────────────────────────┘
                              ▲
                              │  /api/ask/stream  (SSE)
                              │  /api/* (REST)
                              ▼
┌────────────────────────────────────────────────────────────────────────┐
│  AGENT LOOP  (backend/agent/loop.py)                                   │
│  One async function: turn user message + system prompt → final reply.  │
│  Iterates LLM → tool dispatch → LLM until done or budget exhausted.    │
│  Emits per-iteration progress events for the SSE consumer.             │
│                                                                        │
│  Talks to ONE OpenAI-compatible endpoint (your llama-swap / vLLM /     │
│  Ollama / LM Studio) over the LlmClient. Any tool-calling model in     │
│  the 7–9B class works; configure in Settings → LLM.                    │
└────────────────────────────────────────────────────────────────────────┘
                ▲                ▲                  ▲
                │                │                  │
         dispatches to      reads/writes      reads as text
                │                │                  │
┌──────────────┴──┐    ┌─────────┴────────┐    ┌────┴─────────────┐
│  SKILLS         │    │  CONNECTORS      │    │  TEMPLATES        │
│  Capabilities   │    │  External APIs.  │    │  Declarative      │
│  the LLM names  │    │  Hold creds.     │    │  letter / invoice │
│  ("compose_     │    │  Built-in OR     │    │  forms. Jinja +   │
│   draft", "web_ │    │  n8n-backed.     │    │  JSON. Rendered   │
│   search", …).  │    │  Built-in:       │    │  to HTML → PDF.   │
│  ~40+ bundled.  │    │   paperless,     │    │  ~5 bundled,      │
│                 │    │   immich,        │    │  more in the      │
│  Auto-discover  │    │   email-imap,    │    │  community repo.  │
│  from           │    │   maps, web-*,   │    │                   │
│  backend/skills │    │   weather, …     │    │  templates/*.json │
│  /<name>/.      │    │                  │    │                   │
└─────────────────┘    └──────────────────┘    └───────────────────┘
   "Skills are the     "Connectors are       "Templates are the
    main contribution   the I/O boundary      content-author
    surface today."     to the outside        contribution
                        world."               surface."
```

## The three contributable layers

| Layer | Where | Sees credentials | Code? | Review bar |
|---|---|---|---|---|
| **Skills**     | `backend/skills/<name>/` | No — uses connectors | Python + Markdown frontmatter | Code review + security audit |
| **Connectors** | `backend/connectors/<name>.py` | Yes (Fernet-encrypted via `credential_store`) | Python | Strict — full code review + audit |
| **Templates**  | `templates/<id>.json` | No | Pure JSON + Jinja for body_html | Schema validation + render preview |

Plus a **retired fourth concept** — *Layouts* (community-contributed calendar
JS) — the legacy `frontend/` tree was removed in Aug 2026; it was never the
contribution surface the original docs imagined. The React frontend replaced
it; that future-marketplace vision shifted to letter-templates instead.

## Layer 1 — Skills

A **skill** is one capability the LLM can call by name:
*"Find Lea's birthday photo"* → `find_photo`. *"Schreibe eine Rechnung"* → `compose_draft`.

Each skill is two files in one folder:

```
backend/skills/your_skill/
├── skill.md   ← YAML frontmatter (name, description, inputs, outputs,
│                 permissions, side_effects) + Markdown body for humans
└── skill.py   ← one async def execute(ctx, **args)
```

The registry auto-discovers anything under `backend/skills/*/` at boot. No
central registration, no imports to add. Drop the folder, restart, done.

**Conventions** that emerged from shipping ~40 of them:

- `_llm_hint`: every skill returns a `_llm_hint` string telling the LLM what
  just happened in plain language. Prefix `"shown_to_user:"` means "a UI
  card surfaces this — keep your reply short". Prefix `"REJECTED:"` means
  "the user/LLM passed bad input — reply with the corrective hint".
- `needs_input` ui_action: skills emit this when they need data the user
  must provide. The chat renders an inline form; on submit, the form's
  `resume_skill` is called with the filled args. See
  `backend/skills/compose_check_recipient/`.
- `next_playbook_step`: skills that emit a `needs_input` declare what the
  LLM should call AFTER the user submits (e.g. compose_check_recipient →
  next: compose_check_template_args → next: compose_draft).
- `source_skill`: every `needs_input` carries the emitter's name so the
  LLM can tell address-form-submit from template-args-form-submit and route
  correctly.

**Tutorials**: [docs/SKILLS.md](docs/SKILLS.md) walks through writing one
end-to-end with the `scripts/new-skill.sh` scaffolder.

## Layer 2 — Connectors

A **connector** is the I/O boundary to one external service. Three flavours:

```python
# Built-in Python — backend/connectors/<name>.py
from . import ConnectorSpec, register

def my_connector(op: str, **kw) -> dict:
    # Hit the API, return a dict. Never raise; wrap errors in {ok: False, ...}
    return {"ok": True, "op": op, "data": ...}

register(ConnectorSpec(
    name="my_service",
    description="Plain-English purpose. The LLM reads this.",
    params_schema={"type": "object", "properties": {...}, "required": [...]},
    invoke=my_connector,
    requires_auth=False,            # True triggers the install-prompt flow
    credentials_schema={...},       # JSON Schema; the settings UI auto-renders
    backend="builtin",              # vs "n8n"
    version="1.0",
    tags=["category", ...],
))
```

`backend/connectors/__init__.py`'s `_autodiscover()` imports the module on
startup and the `register()` call wires it into the global registry.

**Bundled connectors today**:

| Connector | Purpose |
|---|---|
| `paperless`    | OCR'd document search via Paperless-ngx |
| `immich`       | Photo library (search / recent / of_person / stats) |
| `email-imap`   | IMAP fetch + SMTP send (built-in, no n8n) |
| `email-gmail`  | OAuth via n8n |
| `maps`         | OSM: Nominatim geocoding + OSRM/ORS routing + Overpass POI search |
| `weather`      | open-meteo (free, no key) |
| `web-fetch`*   | (Internal — backed by `agent/providers/web_search/trafilatura.py`) |
| `compose`      | The composition pipeline itself |
| `sms-twilio`   | n8n-backed |
| `banking-fints`| German banking (FinTS) |
| `n8n-echo`     | Smoke-test connector for n8n setup |

\* `web-search` + `web-extract` aren't registered as connectors per se —
they're agent tools at `backend/agent/tools_web.py` with a pluggable provider
framework at `backend/agent/providers/web_search/`. Same pattern, different
plumbing. See [docs/CONNECTORS.md](docs/CONNECTORS.md).

**Failure semantics** (load-bearing — the orchestrator relies on this):

- Every invocation returns a dict
- Success: `{"ok": True, ...}` (or just the data dict if always-success)
- Failure: `{"ok": False, "error": "<message>"}`
- Connectors **must not raise** to the caller — wrap and return
- The LLM is told to read `error` and react accordingly

## Layer 3 — Templates

JSON manifests for letters, invoices, contracts. The schema is documented
in [docs/SKILLS.md](docs/SKILLS.md) under "compose templates" and
auto-validated at load time. Key conventions:

- `body_html`: Jinja2 template — gets `args.X` references, filters
  (`replace`, `euro`, `today_de`), and `{% if %}` guards for optional
  fields. Renders to HTML, then Gotenberg renders to PDF.
- `default_args`: empty/sane defaults applied during real renders.
  **Muster\* placeholder values forbidden here** — they leak into rendered
  PDFs. Use `preview_args` instead.
- `preview_args`: example values used ONLY by the empty-state editor preview
  (`/api/compose/draft` with `preview=true`). `compose_draft` (the LLM/skill
  path that creates real drafts) never touches these.
- `ask_user_for_args`: list of `{key, label, required, hint, pattern}` —
  the `compose_check_template_args` skill turns this into the inline form
  the user fills in chat.
- `subject_template`: Jinja string used to auto-generate the email subject
  ("Kündigung Mietverhältnis – {{ args.wohnung_adresse }}").
- `delivery_default`: `"attachment"` (PDF, formal letters) vs `"inline"`
  (HTML body, informal mails). Pre-selects the SendDialog radio.

Templates are the safest layer — the worst a malicious template can do is
render an ugly or wrong PDF. No code execution.

## The agent loop

`backend/agent/loop.py:ask()` is the single entry point. One async function.

```
1. Build messages = [system_prompt (+ entity ledger), trimmed history…, user_message].
   History is cut to the model's context window (YORIK_LLM_CTX, default 65536)
   before every turn — the stored conversation is never touched.
2. Until done or iteration budget exhausted:
   a. LLM call (LlmClient.chat) with the tool schemas this ROLE may see.
   b. If reply has no tool_calls → break (final answer).
   c. For each tool call: dispatch via ToolRegistry → ToolResult.
      - invoke_skill without a prior skill_view for that skill is
        rejected with a "read the manifest first" hint.
      - Audit (mutation flag, delete counter); guardrails pre/post.
      - Stream progress events to the SSE consumer if attached.
3. Persist the conversation; return {response, ui_actions, agent_trace?, …}.
   On an LLM failure the user gets one sentence in their language
   (friendly_llm_error), never an exception string.
```

**Tools the LLM sees** — ten, the same every turn (minus `install_connector`
for non-admin roles). Skills are NOT tools: the system prompt carries a
one-line index of every skill, and the model reaches them through two
meta-tools:

| Tool | What it does |
|---|---|
| `skill_view(name)`          | Read one skill's manifest (inputs, when_to_use). Required before the first invoke. |
| `invoke_skill(name, args)`  | Run a skill. Role-gated by the registry. |
| `show_calendar(view, anchor, highlight)` | UI control |
| `list_calendar_layouts()`   | Marketplace stub |
| `list_connectors()` / `trigger_connector(name, params)` / `install_connector(name)` | Connector plumbing (`install_connector` admin-only) |
| `list_apps()`               | Discoverability; navigation itself is the `navigate_to` skill |
| `web_search(query, limit)` / `web_extract(urls)` | Web search via the active provider; page text with UNTRUSTED markers |

The system prompt (`backend/ask.py:_SYSTEM_PROMPT` + skill index + date
table) is ~24 KB, plus ~7 KB of tool schemas — roughly 8–9k tokens
before any history. Each skill use costs at least three LLM round trips
(view → invoke → reply). The prompt is the lever — when behaviour drifts,
the prompt gets tightened.

**Destructive skills confirm first.** `delete_*` skills stage the action
(`pending_actions.stage_before_apply`) and emit a `pending_confirmation`
card; the DELETE runs only when the user taps Delete (`/api/pending/{id}/confirm`
→ `pending_actions.apply`). Creates and updates apply immediately and
offer Undo for an hour.

## Streaming (SSE)

`POST /api/ask/stream` returns `text/event-stream` chunks while the agent
loop runs. Event shapes:

```
data: {"phase": "iter_start",  "iteration": N}
data: {"phase": "tool_start",  "iteration": N, "tool": "web_search", "args": {…}}
data: {"phase": "tool_done",   "iteration": N, "tool": "web_search", "duration_s": 1.2}
data: {"phase": "final",       "response": "...", "ui_actions": [...],
                                "sql_used": "...", "conversation_id": "...",
                                "agent_trace": {...}}
data: {"phase": "error",       "error": "..."}
```

The chat UI consumes these via `fetch` + `ReadableStream` (not native
`EventSource` so it can send a POST body) and updates a live status line
under the typing indicator: *🔍 Suche im Web nach …* / *📄 Lese site.de …*.

The legacy `POST /api/ask` (single-shot JSON) is still wired and used
by voice + some background paths.

## Safety architecture (defense in depth)

1. **CSP** on the dashboard locks the browser down: same-origin only for
   `connect-src` + `script-src`. A malicious app injected into the React
   shell cannot phone home.
2. **No LLM-generated SQL**: every read and write goes through a named,
   role-gated skill; the registry rejects calls outside the role's
   permissions (platform_admin / admin / member / restricted).
3. **Confirmation cards**: `delete_*` skills never run on the model's
   say-so — they stage a `pending_action` and the DELETE happens only
   when the user taps Delete. Creates/updates apply immediately and
   offer Undo (toggle in Settings → Profile).
4. **Untrusted-content wrapping**: web_extract wraps fetched page text in
   `[UNTRUSTED CONTENT FROM <url> — START] … [— END]` markers; the system
   prompt instructs the LLM to never follow instructions inside.
5. **PII redaction in web search**: `backend/skills/_web_helpers.redact_pii`
   strips the user's full name + street + multi-word contact names from
   outbound queries before they hit any search engine.
6. **No auto-writes from web data**: hard rule in the system prompt —
   information from web tools INFORMS the LLM's reply but never directly
   triggers add_contact / add_calendar_event / compose_draft. The user
   confirms.
7. **SSRF guard on web_fetch**: refuses localhost / 10/8 / 192.168/16 /
   169.254/16 / file:// / non-http(s) — protects against AWS-metadata
   exfiltration and intranet probing.
8. **Audit logs**: `web_visits` table records every search + fetch per
   user. The plan is to extend the pattern to calendar mutations,
   contact writes, and message sends.
9. **Credential encryption**: Fernet-encrypted via `credential_store`
   for Paperless tokens, Immich keys, IMAP passwords, OAuth refresh
   tokens. Plaintext-on-disk plan: never.
10. **Login throttle + API rate limit**: 5 fails/account/15min →
    account lockout, 20 fails/IP/15min → IP block; 15/min on /api/ask;
    120/min general; 50MB upload cap; 100MB request-body cap.

The combination: **if any single layer fails, the others contain the
damage.** A prompt-injected page can't drain the user's calendar because
confirm-mutations + the no-auto-writes rule both block it. A leaked
Paperless token can't be exfiltrated to an attacker server because CSP
won't let the browser make the outbound request.

See [THREAT_MODEL.md](THREAT_MODEL.md) for the explicit scope statement
(what's in vs. out of the threat model).

## Where to start as a contributor

1. **Read [docs/SKILLS.md](docs/SKILLS.md)** — skills are the lowest-friction
   contribution surface. ~20 minutes from `bash scripts/new-skill.sh foo`
   to a working LLM-callable capability.
2. **Read [docs/CONNECTORS.md](docs/CONNECTORS.md)** — if you want Yorik to
   talk to a service it doesn't know about, you write a connector.
3. **Templates** (letter/invoice JSON): the
   [yorik-community](https://github.com/winidi/yorik-community) repo has
   the catalogue + manifest schema. Templates ship without rebuilding Yorik.
4. **Issue + PR templates** in `.github/` tell you what info reviewers need.

## Roadmap pointers

The previous architecture's "Wave 2 / Wave 3" labels are retired. The
current direction:

- **Alpha → Beta**: mobile responsive pass, voice loop end-to-end verified,
  contact import (vCard/CSV), audit log UI for mutations beyond web,
  one-click VPS deploy story.
- **Skills marketplace**: extend the yorik-community pattern from
  templates to skills + connectors. Curated catalogue, signed updates,
  community-submitted ones in a separate trust tier.
- **Tailscale-by-default** for non-techies: pre-baked relay so a Pi 5 +
  Yorik is reachable from a phone without port forwarding.
- **Per-mutation audit logs**: extend `web_visits` to a unified
  `agent_actions` table — every write Yorik ever did, with timestamp +
  user + skill + args.
- **Templates layer expansion**: 30+ German bureaucracy forms
  (Elterngeld, Bauantrag, Wohnsitzanmeldung, Krankenkasse-wechsel).
  PDF-form-fill mode for legally-formatted government forms that can't
  be HTML-rendered.

What's **explicitly not** on the roadmap:
- Hosted SaaS version (undermines the privacy story — see README "Why
  Yorik?")
- Layouts marketplace (the original 2026-04 vision; superseded by the
  templates marketplace which serves the same need with less surface)
- iframe sandbox for community JS (same reason)
