"""Paperless-ngx connector — Yorik's filing-cabinet integration.

Phase 1 (this file): thin REST wrapper around Paperless's HTTP API. The
voice agent can do full-text search ("show me the Vodafone contract"),
list by correspondent/tag, fetch single documents.

Phase 2 (separate file, follow-up): semantic search via sqlite-vec —
Yorik mirrors Paperless's OCR'd text into a vector index, gives the LLM
a 'search_documents' tool that returns the most-similar passages
regardless of whether they contain the exact keyword.

Credentials are auto-populated by start.sh's first-run flow (creates the
Paperless admin user, grabs the API token via Django shell, writes it to
app_settings.paperless_api_token). User can override either field in
Settings → Connectors → Paperless.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import requests

from . import ConnectorSpec, register
from .. import credential_store
from ..database import conn_ctx, DEFAULT_DB_PATH

log = logging.getLogger("homeos.connectors.paperless")

DEFAULT_BASE_URL = "http://localhost:8010"
TIMEOUT_S = 6
MAX_RETURN = 20


def _settings() -> Dict[str, Any]:
    """Read base_url + api_key from the connector's credential blob (admin
    UI), falling back to the auto-populated app_settings entries from
    start.sh's first-run flow."""
    creds = credential_store.get("paperless") or {}
    base_url = creds.get("base_url")
    api_key = creds.get("api_key")
    if not (base_url and api_key):
        try:
            with conn_ctx(DEFAULT_DB_PATH) as c:
                for key in ("paperless_base_url", "paperless_api_token"):
                    row = c.execute(
                        "SELECT value FROM app_settings WHERE key = ?", (key,)
                    ).fetchone()
                    if row and row["value"]:
                        if key == "paperless_base_url" and not base_url:
                            base_url = row["value"]
                        elif key == "paperless_api_token" and not api_key:
                            api_key = row["value"]
        except Exception:  # noqa: BLE001
            pass
    return {"base_url": (base_url or DEFAULT_BASE_URL).rstrip("/"),
            "api_key": api_key or ""}


def _headers(s: Dict[str, Any]) -> Dict[str, str]:
    return {
        "Authorization": f"Token {s['api_key']}",
        "Accept": "application/json",
    }


def _doc_dict(d: Dict[str, Any], base_url: str) -> Dict[str, Any]:
    """Slim shape the LLM + frontend get back. Hides Paperless's verbose
    internal fields; exposes a direct preview URL the user can click."""
    did = d.get("id")
    return {
        "id": did,
        "title": d.get("title") or "(untitled)",
        "correspondent": d.get("correspondent__name") or d.get("correspondent"),
        "doc_type": d.get("document_type__name") or d.get("document_type"),
        "tags": d.get("tag_names") or d.get("tags") or [],
        "created": d.get("created_date") or d.get("created"),
        "modified": d.get("modified"),
        "preview_url": f"{base_url}/api/documents/{did}/preview/",
        "thumbnail_url": f"{base_url}/api/documents/{did}/thumb/",
        "view_url": f"{base_url}/documents/{did}/",
    }


def paperless(op: str, query: str = "", limit: int = 10, doc_id: Optional[int] = None,
              correspondent: str = "", tag: str = "", **_kw) -> Dict[str, Any]:
    op = (op or "").lower().strip()
    s = _settings()
    if not s["api_key"]:
        return {"ok": False,
                "error": "Paperless not configured — open Settings → Connectors → Paperless and paste an API token. "
                         "(start.sh normally does this for you on first run.)"}
    try:
        if op == "search":
            if not query.strip():
                return {"ok": False, "error": "search requires 'query'"}
            r = requests.get(
                f"{s['base_url']}/api/documents/",
                headers=_headers(s),
                params={"query": query, "page_size": min(limit, MAX_RETURN), "truncate_content": "true"},
                timeout=TIMEOUT_S,
            )
            r.raise_for_status()
            items = (r.json() or {}).get("results", []) or []
            return {"op": "search", "query": query,
                    "documents": [_doc_dict(d, s["base_url"]) for d in items[:limit]]}

        if op == "search_semantic":
            # Vector search via Yorik's sqlite-vec mirror — returns the
            # most-meaning-similar PASSAGES, not just keyword matches.
            # Use this for "what does it say about X" / "when is X due" /
            # "how were my Y results" questions. The LLM can synthesise an
            # answer from the passages and cite back via doc_url.
            if not query.strip():
                return {"ok": False, "error": "search_semantic requires 'query'"}
            from .. import paperless_ingest as pi
            results = pi.search(query, k=min(limit, MAX_RETURN))
            return {"op": "search_semantic", "query": query, "passages": results}

        if op == "recent":
            r = requests.get(
                f"{s['base_url']}/api/documents/",
                headers=_headers(s),
                params={"ordering": "-created", "page_size": min(limit, MAX_RETURN)},
                timeout=TIMEOUT_S,
            )
            r.raise_for_status()
            items = (r.json() or {}).get("results", []) or []
            return {"op": "recent", "documents": [_doc_dict(d, s["base_url"]) for d in items[:limit]]}

        if op == "by_correspondent":
            if not correspondent.strip():
                return {"ok": False, "error": "by_correspondent requires 'correspondent'"}
            r = requests.get(
                f"{s['base_url']}/api/correspondents/",
                headers=_headers(s),
                params={"name__icontains": correspondent},
                timeout=TIMEOUT_S,
            )
            r.raise_for_status()
            matches = (r.json() or {}).get("results", []) or []
            if not matches:
                return {"op": "by_correspondent", "correspondent": correspondent, "documents": []}
            cid = matches[0]["id"]
            r = requests.get(
                f"{s['base_url']}/api/documents/",
                headers=_headers(s),
                params={"correspondent__id": cid, "page_size": min(limit, MAX_RETURN),
                        "ordering": "-created"},
                timeout=TIMEOUT_S,
            )
            r.raise_for_status()
            items = (r.json() or {}).get("results", []) or []
            return {"op": "by_correspondent", "correspondent": matches[0]["name"],
                    "documents": [_doc_dict(d, s["base_url"]) for d in items[:limit]]}

        if op == "get":
            if not doc_id:
                return {"ok": False, "error": "get requires 'doc_id'"}
            r = requests.get(
                f"{s['base_url']}/api/documents/{int(doc_id)}/",
                headers=_headers(s), timeout=TIMEOUT_S,
            )
            r.raise_for_status()
            return {"op": "get", "document": _doc_dict(r.json() or {}, s["base_url"])}

        if op == "test_connection":
            # /api/ 302-redirects to /api/schema/; hit a real endpoint instead
            # so we actually exercise the token.
            r = requests.get(
                f"{s['base_url']}/api/documents/",
                headers=_headers(s), params={"page_size": 1},
                timeout=TIMEOUT_S,
            )
            doc_count = (r.json() or {}).get("count") if r.ok else None
            return {"ok": r.ok, "status": r.status_code, "base_url": s["base_url"],
                    "document_count": doc_count}

        return {"ok": False,
                "error": f"unknown op '{op}'. Use search/recent/by_correspondent/get/test_connection."}
    except requests.RequestException as exc:
        return {"ok": False, "error": f"paperless request failed: {exc}"}


register(ConnectorSpec(
    name="paperless",
    description=(
        "Search the user's filed documents (invoices, contracts, lab results, letters). "
        "PREFER {op: 'search_semantic'} for any natural-language question like 'what did I "
        "charge Müller last time' / 'when is my Vodafone bill due' / 'how were my last blood "
        "tests' — it returns relevant PASSAGES with citations. "
        "Use {op: 'search'} for an exact keyword match. "
        "Other ops: recent / by_correspondent / get / test_connection."
    ),
    params_schema={
        "type": "object",
        "properties": {
            "op": {"type": "string", "enum": ["search", "search_semantic", "recent",
                                              "by_correspondent", "get", "test_connection"]},
            "query": {"type": "string"},
            "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 20},
            "doc_id": {"type": "integer"},
            "correspondent": {"type": "string"},
            "tag": {"type": "string"},
        },
        "required": ["op"],
    },
    invoke=paperless,
    requires_auth=True,
    install_hint=(
        "Normally start.sh auto-creates the admin user and grabs the API token on first run. "
        "If you're configuring manually: open http://localhost:8010, sign in, then Settings → "
        "API Tokens → New, copy it here."
    ),
    credentials_schema={
        "type": "object",
        "required": ["api_key"],
        "properties": {
            "base_url": {
                "type": "string",
                "title": "Paperless URL",
                "default": "http://localhost:8010",
                "description": "Where Paperless is reachable from this Yorik box.",
            },
            "api_key": {
                "type": "string",
                "title": "API Token",
                "format": "password",
                "description": "Paperless → Settings → API Tokens → New.",
            },
        },
    },
    backend="builtin",
    version="1.0",
    tags=["documents", "paperless", "filing", "ocr", "local"],
))
