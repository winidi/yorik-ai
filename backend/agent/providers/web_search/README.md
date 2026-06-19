# Web search providers

The pluggable backend surface for `web_search` / `web_extract` / `web_crawl`
tools. One concrete provider ships in the box (`ddgs.py` — DuckDuckGo, free,
no API key, search only).

## How the dispatcher picks a backend

Resolution order (per capability — search / extract / crawl independently):

1. **`YORIK_WEB_{CAPABILITY}_BACKEND`** env var (per-capability override).
2. **`YORIK_WEB_BACKEND`** env var (shared fallback).
3. **Single-provider shortcut**: if only one registered provider supports
   the capability AND is available, use it.
4. **Legacy preference walk**: `firecrawl → parallel → tavily → exa →
   searxng → brave-free → ddgs`, filtered by availability.

So out of the box (`ddgs` is the only registered backend and it's
available) `web_search` works, `web_extract` errors with a "configure a
backend" hint.

## Adding a new backend

1. Drop a sibling file next to `ddgs.py`, e.g. `tavily.py`:

   ```python
   from .base import WebSearchProvider

   class TavilyProvider(WebSearchProvider):
       @property
       def name(self): return "tavily"
       def is_available(self): return bool(os.getenv("TAVILY_API_KEY"))
       def supports_extract(self): return True
       def search(self, query, limit=5): ...
       def extract(self, urls, **kw): ...
   ```

2. Register it in `backend/agent/tools_web.py:register_web_tools()` or
   wherever you'd like it auto-loaded:

   ```python
   from .providers.web_search.tavily import TavilyProvider
   registry.register_provider(TavilyProvider())
   ```

3. Pin the new package in `backend/requirements.txt`.

That's it. The resolver picks it up automatically; setting
`YORIK_WEB_EXTRACT_BACKEND=tavily` switches `web_extract` over without
restart.

## Response shape (the contract every provider must match)

Search:

```json
{"success": true,
 "data": {"web": [{"title": "...", "url": "...", "description": "...", "position": 1}, ...]}}
```

Extract:

```json
{"success": true,
 "data": [{"url": "...", "title": "...", "content": "...", "raw_content": "...", "metadata": {}}, ...]}
```

Failure (either capability):

```json
{"success": false, "error": "human-readable reason"}
```

The agent tools (`tools_web.py`) re-shape into a JSON tool-result the LLM
sees — keep your provider's success/data shape consistent and the tool
wrapper does the rest.

## Yorik vs. Hermes alignment

This ABC + registry is a port of NousResearch/hermes-agent's
`agent/web_search_provider.py` + `agent/web_search_registry.py` (MIT).
The response-shape contract is preserved bit-for-bit, so providers
written for Hermes can be ported by changing only the import path of
the ABC.
