# Building a Yorik connector

A **connector** is a function the LLM can call by name. *"What's the weather in Berlin?"* triggers `connector.weather(city="Berlin")`. *"Find the invoice from Müller"* triggers `connector.paperless(op="search", query="Müller")`.

If you want Yorik to talk to a service it doesn't talk to yet — Spotify, Strava, your bank, your power meter, anything — this is the doc.

## Heads up: n8n is BYO (bring-your-own)

OAuth-heavy connectors (Gmail, Twilio, anything where managing rotating tokens in Python is painful) can install themselves as n8n workflows. **Yorik does NOT bundle n8n.** n8n ships under the [Sustainable Use License](https://docs.n8n.io/sustainable-use-license/) — source-available, not OSI-approved open source. Bundling it would drag SUL distribution terms into the AGPL-3 codebase and create license-compatibility friction we don't want.

To use the n8n-backed connector path: install n8n yourself (Docker, npm, n8n cloud — your choice, your license to accept), then set `HOMEOS_N8N_BASE_URL` in `config.env` to point at it. If your n8n runs on `localhost:5678`, `start.sh` auto-detects it. The path-prefix reverse proxy at `/n8n/`, the `connector_templates/`, and `backend/n8n_proxy.py` are in the codebase and ready to integrate with your instance. The `connector_templates/echo.workflow.json` file is the reference for what an n8n-backed connector looks like.

If you only build Python connectors (no OAuth), you can ignore n8n entirely.

## The contract

Every connector is one Python file in `backend/connectors/`. The file does three things:

1. Defines a function that does the work
2. Builds a `ConnectorSpec` dataclass describing it
3. Calls `register(spec)` at module level

The autodiscover (`backend/connectors/__init__.py:_autodiscover`) imports every sibling module at startup, so just dropping your file in the directory is enough — no registry edits needed.

## The simplest possible example: weather

`backend/connectors/weather.py` is the reference for connectors that need no credentials. ~80 lines, talks to a free public API, no setup required.

```python
"""Weather connector — uses open-meteo.com (free, no API key)."""

from __future__ import annotations
import requests
from typing import Any, Dict
from . import ConnectorSpec, register

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT_S = 6


def weather(city: str, units: str = "celsius") -> Dict[str, Any]:
    """Current weather in `city`."""
    # ... do the actual work, return a dict ...
    return {
        "city": city,
        "temp": 18.5,
        "condition": "partly cloudy",
        "source": "open-meteo",
    }


register(ConnectorSpec(
    name="weather",
    description=(
        "Get current weather for a city. Returns temperature, condition, "
        "humidity, wind, and an emoji icon. Always available — no API key needed."
    ),
    params_schema={
        "type": "object",
        "properties": {
            "city":  {"type": "string", "description": "e.g. 'Berlin'"},
            "units": {"type": "string", "enum": ["celsius", "fahrenheit"], "default": "celsius"},
        },
        "required": ["city"],
    },
    invoke=weather,
    requires_auth=False,
    backend="builtin",
    version="1.0",
    tags=["weather", "free", "no-auth"],
))
```

That's the whole connector. The LLM now has a `weather` tool with a typed schema. Yorik's frontend renders it in Settings → Connectors with a Test button.

## The `ConnectorSpec` fields

| Field | Required | What it does |
|---|---|---|
| `name` | yes | Unique identifier the LLM uses (`weather`, `paperless`, `spotify`). Lowercase, no spaces. |
| `description` | yes | One-paragraph what-it-does. The LLM reads this to decide when to call your connector — write it well. |
| `params_schema` | yes | JSON schema for the function's arguments. The LLM uses this to know what to pass. |
| `invoke` | yes (for builtin) | The callable. Can be sync or async. Must return a dict. |
| `requires_auth` | no | `True` if the connector needs credentials (API key, OAuth token, etc.). |
| `install_hint` | no | Human-readable instructions if `requires_auth=True`. Shown in the install dialog. |
| `backend` | no | `"builtin"` (default) or `"n8n"` for webhook-driven connectors. |
| `version` | no | Semver. Bump when you change the params_schema or output shape. |
| `tags` | no | Searchable labels: `["weather", "free"]`, `["finance", "auth-required"]`. |
| `credentials_schema` | no | JSON schema for credentials (when `requires_auth=True`). Frontend renders this as a form. |

## When your connector needs credentials

The reference: `backend/connectors/paperless.py`. Pattern:

```python
from .. import credential_store

def _settings() -> Dict[str, Any]:
    """Read base_url + api_key from the encrypted credential store."""
    creds = credential_store.get("paperless") or {}
    return {
        "base_url": (creds.get("base_url") or "http://localhost:8010").rstrip("/"),
        "api_key":  creds.get("api_key"),
    }


def paperless(op: str, **kwargs) -> Dict[str, Any]:
    s = _settings()
    if not s["api_key"]:
        return {"ok": False, "error": "Paperless API key not configured"}
    # ... use s["base_url"] and s["api_key"] for HTTP calls ...


register(ConnectorSpec(
    name="paperless",
    description="Full-text search + retrieval of documents in your Paperless-ngx library.",
    params_schema={
        "type": "object",
        "properties": {
            "op":    {"type": "string", "enum": ["search", "recent", "by_correspondent"]},
            "query": {"type": "string"},
            # ...
        },
        "required": ["op"],
    },
    invoke=paperless,
    requires_auth=True,
    install_hint="Paste your Paperless URL + API token. Get the token from Paperless → Account → API Keys → New.",
    backend="builtin",
    version="1.0",
    credentials_schema={
        "type": "object",
        "properties": {
            "base_url": {"type": "string", "format": "uri", "default": "http://localhost:8010"},
            "api_key":  {"type": "string", "format": "password"},
        },
        "required": ["base_url", "api_key"],
    },
    tags=["documents", "paperless", "auth-required"],
))
```

Yorik handles the rest:
- Settings → Connectors renders the credentials form from `credentials_schema`
- User input is encrypted with the per-install Fernet key
- `credential_store.get("paperless")` returns the decrypted blob inside your `invoke()`
- The credentials never end up in logs (the credential store has masking built in)

## When the connector is OAuth-heavy (Gmail, Slack, banking)

Use the n8n backend instead. n8n already has 400+ pre-built OAuth integrations and a hardened credentials store. Yorik's job is to wire them up.

Note: this path requires the user to have BYO'd n8n (see "Heads up: n8n is BYO" above). Without an n8n instance, OAuth-heavy connectors marked `backend="n8n"` will register but won't have a send path — Yorik will surface a "no n8n configured" error when the LLM tries to invoke them.

Reference: `backend/connectors/email_gmail.py`. Pattern: ship an n8n workflow template, set `backend="n8n"`, point Yorik at the n8n webhook path.

```python
register(ConnectorSpec(
    name="email_gmail",
    description="Send + search Gmail via n8n (OAuth handled there).",
    params_schema={...},
    invoke=None,                              # n8n handles the call
    requires_auth=True,
    install_hint="On install, Yorik imports a workflow into your n8n and you authenticate Gmail there.",
    backend="n8n",
    version="1.0",
    n8n_workflow_template={                   # the workflow JSON
        "name": "Yorik: Gmail",
        "nodes": [...],
        "connections": {...},
    },
    n8n_webhook_path="yorik/gmail",
    tags=["email", "gmail", "oauth"],
))
```

Yorik imports the workflow on install, the user clicks one button in n8n to grant OAuth, and from then on Yorik POSTs to `http://127.0.0.1:5678/webhook/yorik/gmail` to invoke it.

You don't need to handle OAuth refresh, scopes, or any of that — n8n does it.

## Return values

Always return a dict. Conventions:

- `{"ok": False, "error": "..."}` for failures the LLM should see and handle
- For success: any dict the LLM can serialize. Keep keys short and self-describing.
- Don't return raw HTTP bodies — normalize the shape so changes to the upstream API don't propagate to the LLM's understanding.
- Include a `"source"` field naming the upstream (`"open-meteo"`, `"paperless@localhost:8010"`) so debugging is easy.

## Sync vs async

Both work. If your connector does any HTTP, async is better:

```python
import httpx
from typing import Any, Dict

async def weather(city: str) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=6) as client:
        r = await client.get(FORECAST_URL, params={...})
        return r.json()
```

The dispatcher handles both — if `invoke` is a coroutine it's `await`ed, otherwise it's called in a thread pool. Don't mix sync `requests` with `asyncio.sleep()` in the same function — pick a lane.

## Testing your connector

Inline smoke test (paste into a Python REPL with the venv active):

```python
from backend import connectors as c

# Should show your connector
[s.name for s in c.list_all()]
# → ['weather', 'paperless', 'your_connector', ...]

# Invoke it directly
import asyncio
asyncio.run(c.invoke("your_connector", {"city": "Berlin"}))
# → {'city': 'Berlin', 'temp': 18.5, ...}
```

For a real test (recommended), add a file to `tests/connectors/test_your_connector.py`:

```python
import asyncio
from backend import connectors as c

def test_your_connector_basic():
    r = asyncio.run(c.invoke("your_connector", {"city": "Berlin"}))
    assert r.get("city") == "Berlin"
    assert isinstance(r.get("temp"), (int, float))

def test_your_connector_handles_bad_input():
    r = asyncio.run(c.invoke("your_connector", {"city": "NotARealCity"}))
    assert r.get("ok") is False
    assert "error" in r
```

`pytest tests/connectors/ -q` runs them.

## Where to look for examples

| File | Pattern |
|---|---|
| `backend/connectors/weather.py` | Simplest — no auth, public API (open-meteo) |
| `backend/connectors/maps.py` | OpenStreetMap. Three ops behind one connector: `geocode` (Nominatim), `directions` (OSRM + optional OpenRouteService when an API key is configured), `search_pois` (Overpass with German/English keyword → OSM tag mapping). 24h in-process geocode cache; ORS fallback to OSRM on failure. |
| `backend/connectors/paperless.py` | Auth via credential store, complex op enum |
| `backend/connectors/immich.py` | Auth via credential store, multiple ops |
| `backend/connectors/email_imap.py` | Auth via credential store + multipart-alternative for HTML body |
| `backend/connectors/email_gmail.py` | n8n-backed, OAuth |
| `backend/connectors/sms_twilio.py` | n8n-backed, simple webhook |
| `backend/connectors/banking_fints.py` | German banking, complex auth (FinTS) |
| `backend/connectors/compose.py` | Internal — wraps the compose pipeline |

## The special case: web search + extract

`web_search` and `web_extract` are agent tools, NOT regular connectors —
they live at `backend/agent/tools_web.py` with a pluggable provider
framework at `backend/agent/providers/web_search/`. Same shape as
ConnectorSpec, different plumbing because they predate the connector
framework. The contribution surface is the **provider** interface:

```
backend/agent/providers/web_search/
├── base.py             # WebSearchProvider ABC
├── registry.py         # backend selection (env-driven)
├── ddgs.py             # bundled: DuckDuckGo HTML scrape — search-only, no key
└── trafilatura.py      # bundled: pure-Python main-text extraction — extract-only
```

A provider declares which capabilities it offers (`supports_search`,
`supports_extract`, `supports_crawl`) and implements the matching methods.
Multiple providers can be registered; the active one for each capability
is chosen via env vars in this precedence order:

1. `YORIK_WEB_SEARCH_BACKEND` / `_EXTRACT_BACKEND` / `_CRAWL_BACKEND`
   (per-capability override)
2. `YORIK_WEB_BACKEND` (shared fallback)
3. The single registered provider that supports the capability
4. The legacy preference order (firecrawl → tavily → exa → searxng →
   brave-free → ddgs)
5. None → the tool surfaces a "set up a web backend" message

To add a provider (e.g. Brave Search API, Tavily, Firecrawl): drop a
file in `backend/agent/providers/web_search/<name>.py`, subclass
`WebSearchProvider`, register it via `register_provider()` in your
module-level code or in `tools_web.register_web_tools`. The frontend
Settings → Connectors picker reads `get_setup_schema()` to render the
key-input form.

### Safety rules for web tools

The tool wrappers (`WebSearchTool` / `WebExtractTool`) enforce three
load-bearing rules on top of whatever the provider does:

- **PII redaction**: every `web_search` query goes through
  `backend/skills/_web_helpers.redact_pii()` before the network call.
  Multi-word phrases (the user's full name, street, contact display-
  names of 2+ words) are stripped. Pure-PII queries are refused outright.
- **UNTRUSTED-content markers**: `web_extract` wraps each page's text in
  `[UNTRUSTED CONTENT FROM <url> — START] … [— END]` markers; the system
  prompt instructs the LLM to never follow instructions inside.
- **Audit log**: every search + fetch lands in the `web_visits` table
  with the user_id, action, query/url, provider, status, error. Surfaces
  via `/api/web/visits` for the per-user privacy panel.

When you write a new provider, you DON'T re-implement these — they live
in the tool wrapper layer. Just make your provider implement the
contract from `base.py` cleanly.

### SSRF on web_extract — applies to every extract provider

The bundled `trafilatura` provider has an SSRF guard (refuses localhost
/ 10/8 / 192.168/16 / 169.254 / file:// / non-http(s)). If you ship a
provider that fetches arbitrary URLs (Tavily, Firecrawl), it MUST do
the same — Yorik refuses to ship an extract backend without it. The
DNS-rebinding-resistant check pattern is in
`backend/agent/providers/web_search/trafilatura.py:_is_private_or_local()`.

You should not need a third provider just to take advantage of Brave /
SearXNG / Tavily / Firecrawl etc. — write each as a `WebSearchProvider`
subclass alongside `ddgs.py` and `trafilatura.py`. The first one to
land an API-keyed search backend will likely use `Brave Search API`
(free 2000 req/mo tier, paid tiers above).

## Submitting your connector

1. Read [CONTRIBUTING.md](../CONTRIBUTING.md)
2. Fork, branch, write your connector
3. Add a test in `tests/connectors/`
4. `pytest tests/ -q` should pass
5. Open a PR — the bug template asks for "what tool/service does this connector talk to" so reviewers have context

For builtin connectors we accept that talk to:
- Free public APIs (no auth)
- Self-hostable services (Paperless-style)
- User-owned credentials (the user's own Spotify account, etc.)

We don't accept builtin connectors for:
- Services that bill per-call without the user understanding the cost
- Services that require Yorik-the-project to hold a single shared API key
- Closed-source vendor lock-in services

For OAuth-heavy services that don't fit those rules, use n8n-backed connectors instead.

## Questions

[GitHub Discussions → tag `connectors`](https://github.com/winidi/yorik-ai/discussions). Show what you're building before you spend a weekend on it.
