# Building a Yorik skill

A **skill** is a capability the LLM can invoke by name. *"Find the photo of Lea from her birthday"* triggers `find_photo`. *"Schreibe eine Rechnung über 1200 €"* triggers `compose_draft`.

If you want Yorik to do something it can't do yet — talk to a new service, run a calculation, automate a household routine — this is where you start.

## The contract

A skill is **two files in one folder** under `backend/skills/<name>/`:

```
backend/skills/your_skill/
├── skill.md   ← the spec the LLM reads (when to use, inputs, outputs)
└── skill.py   ← one async function called execute(ctx, **args)
```

The registry auto-discovers anything dropped in there at boot. No central registration, no imports to add anywhere else. Just write the two files and restart.

## The simplest possible skill

Try it right now: scaffold a skill in 10 seconds:

```bash
bash scripts/new-skill.sh weather_outlook
```

You'll get a working stub at `backend/skills/weather_outlook/` with placeholder doc + a function that returns dummy data. The LLM picks it up on the next restart.

Or by hand — minimal example:

`backend/skills/dice_roll/skill.md`:

```yaml
---
name: dice_roll
description: Roll N dice with M sides each. Cheap, deterministic, harmless.
when_to_use: |
  - User says "roll the dice" / "würfel mal"
  - Game-related queries: "1d20 for initiative"
inputs:
  n:
    type: integer
    required: false
    default: 1
    description: How many dice to roll
  sides:
    type: integer
    required: false
    default: 6
    description: How many sides each die has
outputs:
  rolls:
    type: array
    description: The individual rolls as integers
  total:
    type: integer
    description: Sum of all rolls
permissions: ["*"]
side_effects: none
tags: [game, random, harmless]
---
# dice_roll
Use Python's `secrets` (cryptographically random) not `random` — even for games,
because deterministic outputs from the same prompt feel broken.
```

`backend/skills/dice_roll/skill.py`:

```python
"""dice_roll skill — N dice with M sides."""
import secrets

async def execute(ctx, n: int = 1, sides: int = 6) -> dict:
    n = max(1, min(int(n), 100))
    sides = max(2, min(int(sides), 1000))
    rolls = [secrets.randbelow(sides) + 1 for _ in range(n)]
    return {"rolls": rolls, "total": sum(rolls)}
```

That's a working skill. After backend restart:

- The LLM sees it via `list_skills`
- The user says *"würfel mal mit 2d20"* → LLM calls `use_skill(name="dice_roll", args={n: 2, sides: 20})`
- The result lands back in chat as part of the LLM's answer

## skill.md frontmatter — every field

| Field | Type | Required | What |
|---|---|---|---|
| `name` | string | yes | Unique skill ID. Lowercase, underscores. The LLM uses this. |
| `description` | string | yes | One-sentence "what does this do". The LLM reads this to choose between skills. |
| `when_to_use` | multi-line string | yes | Bullet list of trigger phrases / scenarios. **THIS IS THE MOST IMPORTANT FIELD** — it's what makes the LLM pick your skill over `run_sql`. Be specific and concrete. |
| `inputs` | object | yes | Each input has `type`, `required`, `description`, optional `default`. Map keys become the kwargs of `execute()`. |
| `outputs` | object | no | Descriptive only — the LLM uses it to interpret the response. |
| `permissions` | array | no | List of roles allowed to invoke (`["admin"]`, `["admin", "member"]`, `["*"]` = anyone). Default: `["admin", "member"]`. |
| `side_effects` | string | no | Human-readable summary: `"none"`, `"writes events table"`, `"sends an email"`. |
| `cost` | string | no | `"free"`, `"1 LLM call"`, `"1 Immich API call"`. Helps choose between heavy + light skills. |
| `tags` | array | no | Searchable labels. |

## skill.py — the entrypoint

Always async, always takes `ctx` first, returns a dict:

```python
async def execute(ctx, *named_inputs) -> dict[str, Any]:
    ...
    return {"key": "value"}
```

### `ctx` — what's in it

```python
ctx.user_id        # int — the calling user's id (defaults to 1 if anonymous)
ctx.role           # str — "admin" / "member" / "child" / etc.
ctx.language       # str — the user's profile language ("en", "de", ...)
ctx.call_skill(name, **args)   # invoke another skill, for composition
```

### Return shape

Whatever the LLM should see. Conventions:
- Failures the LLM should handle: `{"ok": False, "error": "..."}` — DON'T raise.
- Failures the LLM should propagate to the user as an error: `raise ValueError("...")`. The registry catches it and surfaces it.
- Anything else: a flat-ish dict. Keep keys short. The LLM will summarize.

## Three patterns

### Pattern 1 — read-only (the simplest)

No DB writes, no external state. Look up something, return it.

Examples: `check_calendar`, `find_photo`, `find_document`, `dice_roll`.

Just write the function. No additional plumbing.

### Pattern 2 — apply-then-confirm (for mutations)

When your skill writes to the DB (calendar events, tasks, bills), follow the **apply-then-rollback** pattern for the confirmation modal.

```python
async def execute(ctx, title: str, ...) -> dict:
    # 1. Apply immediately
    with get_conn() as conn:
        cur = conn.execute("INSERT INTO ... VALUES (...)", (...))
        row_id = cur.lastrowid

    # 2. Emit a UI refresh action
    from backend.ui_tools import _append
    _append({"type": "refresh_data", "table": "your_table", "highlight_id": row_id})

    # 3. Stage rollback if confirmation is enabled
    from backend import pending_actions as pa
    if pa.should_confirm(ctx):
        pa.stage_with_rollback(
            skill="your_skill",
            rollback_kind="delete_your_row",  # define a handler in pending_actions.rollback()
            rollback_args={"id": row_id},
            preview={"action": "create", "title": title, "id": row_id},
            ctx=ctx,
        )

    return {"id": row_id, "...": ...}
```

The chat then shows a **Just testing / Cancel / Looks good** panel under Yorik's reply. Cancel/test runs your rollback handler.

Look at `backend/skills/add_task/skill.py` for the canonical pattern.

### Pattern 3 — external service

Your skill calls an HTTP API (Spotify, Strava, your bank). Two sub-patterns:

- **Public API**: use `httpx`, return the result. No credentials. (See `backend/connectors/weather.py` for the connector flavour of this — also valid for a skill.)
- **User-authenticated**: store the user's credentials encrypted via `backend.credential_store`, read them in your skill. Best for services where each household member has their own login.

For OAuth-heavy services (Gmail, banking), use the **n8n-backed connector** path instead of writing the OAuth flow yourself — note this requires the user to BYO n8n (not bundled with Yorik). See `docs/CONNECTORS.md`.

## Testing your skill

### Inline smoke test

```python
# Paste this in a REPL with the venv active
import asyncio
from types import SimpleNamespace
from backend.skills.dice_roll.skill import execute

ctx = SimpleNamespace(user_id=1, role="admin", language="en")
print(asyncio.run(execute(ctx, n=3, sides=6)))
# → {'rolls': [4, 2, 5], 'total': 11}
```

### Pytest

Add a file `tests/skills/test_dice_roll.py`:

```python
import asyncio
from types import SimpleNamespace
from backend.skills.dice_roll.skill import execute

def test_dice_roll_basic():
    ctx = SimpleNamespace(user_id=1, role="admin", language="en")
    r = asyncio.run(execute(ctx, n=3, sides=6))
    assert len(r["rolls"]) == 3
    assert sum(r["rolls"]) == r["total"]
    assert all(1 <= x <= 6 for x in r["rolls"])

def test_dice_roll_clamps_extremes():
    ctx = SimpleNamespace(user_id=1, role="admin", language="en")
    r = asyncio.run(execute(ctx, n=1000, sides=10000))
    assert len(r["rolls"]) == 100   # clamped to max
    assert all(1 <= x <= 1000 for x in r["rolls"])  # sides clamped too
```

`pytest tests/skills/ -q` runs them.

### Live test via the LLM

After restarting the backend, open chat and say a trigger phrase from your `when_to_use` list. Watch DevTools console:

```
[chat] sending: "würfel mal mit 2 würfeln"
...
[chat] response: "Du hast eine 4 und eine 5 gewürfelt — Summe 9."
```

If the LLM didn't pick your skill: your `when_to_use` isn't specific enough. Look at how `add_calendar_event` or `find_photo` phrase theirs — concrete user phrases work better than abstract descriptions.

## What good skills do well

- **One job per skill.** `add_calendar_event` doesn't also search. Compose skills instead.
- **Defensive validation.** Cap loops (`n = min(n, 100)`). Validate ISO dates. Reject empty strings. The LLM is not your friend.
- **Honest error returns.** `{"ok": False, "error": "..."}` is fine. Hallucinating success is not.
- **Brief result strings.** The LLM will summarize what you return. 100 rows back is fine; a 50KB blob is not.
- **No surprises.** Skills that delete should require explicit IDs. No `delete_all_X` skills. Ever.

## Submitting your skill

1. Read [`CONTRIBUTING.md`](../CONTRIBUTING.md) — DCO signoff is mandatory
2. Fork → branch → add `backend/skills/<name>/`
3. Add a test in `tests/skills/`
4. `pytest tests/ -q` passes
5. Open a PR with the skill name in the title

For built-in skills (shipped with Yorik), we look for:

- ✅ Public APIs or self-hostable services
- ✅ Skills that fit the family / small-business OS theme
- ✅ Good `when_to_use` so the LLM actually picks them
- ❌ Wrapper around a single proprietary cloud API with no offline alternative
- ❌ Skills that bypass the confirmation modal for mutations
- ❌ Skills that import network code at module load time (defer to function body)

## Where to look in the codebase

| File | Pattern |
|---|---|
| `backend/skills/find_photo/skill.py` | Read-only, calls a connector |
| `backend/skills/check_calendar/skill.py` | Read-only, SQLite query |
| `backend/skills/add_calendar_event/skill.py` | Apply-then-rollback (canonical) |
| `backend/skills/compose_draft/skill.py` | Mutates + emits UI action |
| `backend/skills/email_briefing/skill.py` | Calls the LLM as part of the work |
| `backend/skills/universal_search/skill.py` | Composite: invokes multiple search paths |

## Conventions that emerged from shipping ~40 skills

Five patterns surface again and again. None are required by the framework — but
adopting them keeps your skill consistent with the bundled set, which makes the
LLM's job easier and the UX more uniform.

### 1. `_llm_hint` — the in-band channel to the next iteration

Every skill returns an `_llm_hint` string. It's the natural-language note the
LLM reads BEFORE it composes the user-facing reply. Three prefixes carry
meaning:

- `"shown_to_user:..."` — *"a UI card already surfaces this — your reply
  should be ONE short sentence acknowledging it."* Use when your skill emitted
  a `ui_action` (a draft card, photo grid, price summary, etc.).
- `"REJECTED: ..."` — *"the LLM/user passed bad input — reply with the
  corrective hint below, do NOT retry the same call."* Use for hard-rejection
  paths: required-arg missing, placeholder content, SSRF refusal, ambiguity.
- *(no prefix)* — informational. The LLM weaves it into the reply.

Make the hint **prescriptive**, not descriptive — the LLM follows direct
instructions ("call X next, with arg Y") much more reliably than abstract
guidance ("the data is incomplete").

### 2. `needs_input` — inline forms instead of prose Q&A

When your skill needs data the user must provide, emit a `needs_input`
ui_action via `from backend.ui_tools import _append`:

```python
_append({
  "type":           "needs_input",
  "source_skill":   "compose_check_recipient",     # which skill emitted this
  "title":          "Postanschrift von Hans Becker",
  "context":        "Damit der Brief richtig adressiert wird, brauche ich:",
  "fields": [
    {"key": "line1",    "label": "Straße + Hausnr.", "required": True},
    {"key": "postcode", "label": "PLZ", "pattern": r"^\d{5}$"},
    {"key": "city",     "label": "Ort", "required": True},
  ],
  "suggestions": [...],  # optional, one-click "use this" chips
  "save_to_contact": {...},  # optional: persist values on a contact too
  "next_playbook_step": "compose_check_template_args",  # what to call next
  "resume_skill":    "compose_check_template_args",
  "resume_args":     {"contact_id": cid, "template_id": template_id},
})
```

The chat UI renders this as an inline form card. When the user submits, a
synthetic `[form_submit from=<source_skill>] ...` message goes back to the
LLM, which calls `next_playbook_step` with the filled fields. Reference
implementations: `compose_check_recipient`, `compose_check_template_args`,
`find_recipient_address_from_documents`. Frontend renderer:
`frontend-react/src/apps/compose/NeedsInputCard.tsx`.

### 3. Deterministic playbooks — composing skills into a single user-visible flow

Yorik's complex flows (postal letter, photo-attach, group-pricing) are
chained skill calls in a deterministic order. The pattern:

1. **find_contact / find_X** — resolve the subject.
2. **compose_check_recipient** / similar gate — verify the data the
   downstream skill needs is present.
3. **(if gate failed) find_recipient_address_from_documents / paperless
   mining** — try to get the missing data from in-house sources first.
4. **(still failed) emit `needs_input`** — ask the user via a form.
5. **The next playbook step**: another check OR the final action.
6. **Final action** (compose_draft, add_calendar_event, etc.).

The system prompt teaches this sequence. Skills enforce it by:
- Returning the next-step hint in `_llm_hint`.
- Setting `next_playbook_step` on emitted `needs_input` actions so the form
  submit routes correctly.
- The terminal skill (compose_draft, add_calendar_event) **hard-gates** —
  refuses to run when required args missing, auto-emits the same form. This
  makes the playbook robust to LLM shortcutting.

### 4. UI actions are typed cards

Every visible "card" in chat is a `ui_action` with a stable shape. The chat
frontend has a renderer per type. Current ones:

| ui_action type | Emitted by | Rendered as |
|---|---|---|
| `compose_draft_created` | compose_draft | "Bearbeiten →" card → opens Compose |
| `template_picker` | compose_draft (vague body) | Grid of template suggestions |
| `needs_input` | compose_check_*, find_recipient_address_* | Inline form |
| `photo_picker` | propose_inline_photo | 3-up thumbnail grid |
| `web_results` | web_search tool | Title + URL + snippet cards |
| `price_summary` | compute_group_price | Receipt-style line items + total |
| `venue_saved` | save_venue | "✓ saved + Open in Contacts" |
| `pending_confirmation` | any mutation skill (via pending_actions) | Confirm / Cancel / Test panel |
| `pois_found` | find_provider_nearby | POI cards with map link |
| `photos_found` | find_photo | Photo thumbnails (inline / lightbox) |
| `documents_found` | find_document | Document hits with snippets |
| `show_calendar` | add_calendar_event etc. | Auto-navigates the calendar app |
| `navigate` | navigate_to | Routes to a Yorik app |
| `open_app` | various | Same |

Adding a new ui_action type is a 30-line frontend change in `ChatApp.tsx`.
Match an existing card's style for visual coherence.

### 5. Compose templates as a data-only contribution surface

Templates (`templates/*.json`) are a separate contribution path that doesn't
need code — pure JSON with Jinja2 in `body_html`. Schema:

| Field | Required | Purpose |
|---|---|---|
| `id`, `name`, `version`, `body_html` | yes | The basics. |
| `description`, `tags`, `vertical` | no | Search / categorisation. |
| `default_args` | no | Sane defaults applied at every render. **Never put `Muster*` placeholder values here** — they leak into the PDF. Use `preview_args` instead. |
| `preview_args` | no | Example values shown ONLY by the empty-state editor preview (`/api/compose/draft?preview=true`). `compose_draft` (real-draft path) never touches these. |
| `ask_user_for_args` | no | List of `{key, label, required, hint, pattern}` driving the form-based input (`compose_check_template_args` reads it). The LLM-driven flow uses this; un-asked-for fields fall back to auto-detection. |
| `subject_template` | no | Jinja string for auto-generating the email subject when the LLM didn't pass one. |
| `delivery_default` | no | `"attachment"` (PDF — formal letters) or `"inline"` (HTML body — informal). Pre-selects the SendDialog radio. |
| `editor_notes` | no | HTML hints / legal disclaimers rendered as a card BELOW the editor; never enters the PDF. |
| `skills_supported` | no | Self-describing metadata for templates that integrate with skills (auto_fill_from, send_channels, voice_triggers, language). |

Body-html idioms that prevent broken-grammar rendering when fields are empty:

```jinja
{# Guard every optional sentence — empty {{ args.X }} produces visible gaps #}
{% if args.mietvertrag_vom %}vom <strong>{{ args.mietvertrag_vom }}</strong>{% endif %}

{# Conditional address rendering with line breaks #}
{% if args.empfaenger_adresse %}<br>{{ args.empfaenger_adresse | replace('\n', '<br>') | safe }}{% endif %}

{# Country line suppressed for DE→DE letters (compose_draft handles this) #}
```

Reference templates: `templates/kuendigung-mietvertrag-de.json` (full polish,
13 versions of iteration), `templates/mietminderung-de.json` (the same bar
applied to a different formal letter), `templates/generic-letter.json`
(universal DIN 5008 — recipient block, date, salutation, body, signature;
the safe default for any non-specific letter).

Templates live in `templates/` in-tree. A separate community catalogue lives
at [yorik-community](https://github.com/winidi/yorik-community) — templates
there are installed via the "Browse community templates" button in the
Compose sidebar.

## The bundled skill catalogue — a guided tour

What ships with Yorik today, grouped by the things people actually ask it
to do. This is a curated highlight reel — there are ~60 skills total;
browse `backend/skills/` for the full list. Each has a `skill.md` at
`backend/skills/<name>/skill.md` you can read for the full contract.

### Calendar + tasks + bills

| Skill | What it does |
|---|---|
| `add_calendar_event` | Create an event. Accepts `location` → geocodes + computes travel time inline. |
| `update_calendar_event` | Modify an event (time, title, location — re-geocodes on location change). |
| `delete_calendar_event` | Delete ONE event by id. ≤1 per request. |
| `check_calendar` | Read events in a window (default = next 7 days). |
| `add_task`, `update_task`, `delete_task`, `check_tasks` | The task counterparts. |
| `add_bill`, `update_bill`, `delete_bill`, `check_bills` | Recurring expense tracking. |

### Compose flow

| Skill | What it does |
|---|---|
| `compose_draft` | The terminal step. Hard-gates required template args. Updates in-place via `existing_draft_id`. |
| `compose_check_recipient` | Verifies the chosen contact has a usable postal address. |
| `compose_check_template_args` | Auto-detects unfilled template slots, emits a form with `ask_user_for_args` polish. |
| `find_recipient_address_from_documents` | Mines Paperless for past correspondence with a contact + extracts addresses. |
| `propose_inline_photo` | Visual photo picker for "schreib X mit einem Foto vom Y". |
| `email_draft` | Per-thread email reply drafts (multiple variants). |
| `whatsapp_draft` | Same for WhatsApp messages. |
| `propose_meeting_times` | Find N free slots + draft an email reply proposing them. |

### Contacts

| Skill | What it does |
|---|---|
| `find_contact` | Name / alias / channel lookup. Surfaces postal-address presence in the hint so the LLM can route correctly. |
| `list_contacts_for_picking` | Compact full address book (~30 chars per contact). Fallback when `find_contact` returns 0. |
| `find_known_provider` | "Do I already have a dentist?" — checks contacts + past Paperless + past events with locations. |
| `add_contact`, `update_contact`, `delete_contact` | CRUD. All apply-then-confirm. |
| `add_contact_address`, `add_contact_channel` | Attach address / email / phone / WhatsApp / etc. |
| `mark_contact_spam`, `promote_pending_contact` | Status transitions. |

### Web + maps + media

| Skill | What it does |
|---|---|
| `find_photo` | Immich CLIP search / recent / by-person / stats. |
| `find_document` | Paperless semantic search (sqlite-vec mirror). |
| `find_provider_nearby` | Overpass POI search (dentists / pharmacies / restaurants / …). |
| `calculate_travel_time` | Driving/cycling/walking time from A to B (defaults A to home). |
| `compute_group_price` | Deterministic arithmetic + receipt-style chat card. |
| `extract_price_table` | Parse a fetched web page into `{venue, prices: [...]}` structure. |
| `save_venue` | Persist a business with cached price_table for compound-knowledge follow-ups. |

### Search / overview

| Skill | What it does |
|---|---|
| `universal_search` | Search across email + WhatsApp + Paperless + Immich + calendar in one call. |
| `email_briefing` | "What's happening in my inbox?" natural-language summary. |
| `whatsapp_briefing` | Same for WhatsApp — action items + per-chat summary. |

### Meta

| Skill | What it does |
|---|---|
| `navigate_to` | Switch the user's view to a Yorik app (calendar, contacts, settings, …). |
| `undo_last_action` | Roll back the most recent mutation. |

## Questions

GitHub Discussions → tag `skills`: <https://github.com/winidi/yorik-ai/discussions>. Show what you're building before you spend a weekend on it.
