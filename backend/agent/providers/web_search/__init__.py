"""Web search + extract + crawl plugin surface.

The active provider is chosen by config: `web.search_backend`,
`web.extract_backend`, `web.crawl_backend`, or `web.backend` (shared
fallback). One bundled backend will land in Phase 5 (ddgs, no API key).
"""
