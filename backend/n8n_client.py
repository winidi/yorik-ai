"""n8n REST API wrapper.

Used by n8n-backed connectors to import workflow templates, trigger them
via webhook, and check on executions. Auth via X-N8N-API-KEY header
(generated in the n8n UI under Settings → API on first run).

This module fails GRACEFULLY: every public function returns a {ok, ...}
dict and never raises on transport/auth errors — the rest of Yorik should
keep working even when n8n is unreachable or the API key isn't configured.

Wave 4a: just the wrapper. Connector-template install + webhook trigger
get used by Wave 4b's gmail / twilio connectors.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

import requests

log = logging.getLogger("homeos.n8n")

def _base_url() -> str:
    return os.getenv("HOMEOS_N8N_BASE_URL", "http://127.0.0.1:5678").rstrip("/")


def _api_key() -> str:
    return os.getenv("HOMEOS_N8N_API_KEY", "").strip()


# Backwards-compat module-level reads (some callers reference these directly).
N8N_BASE_URL = _base_url()
N8N_API_KEY = _api_key()
TIMEOUT_S = 10
WEBHOOK_TIMEOUT_S = 30  # workflows can be slow


def is_configured() -> bool:
    """True if we have the credentials to talk to n8n's API."""
    return bool(_api_key())


def _headers() -> Dict[str, str]:
    return {
        "X-N8N-API-KEY": _api_key(),
        "Accept": "application/json",
    }


def _wrap_err(msg: str, **extra) -> Dict[str, Any]:
    return {"ok": False, "error": msg, **extra}


def is_reachable() -> Dict[str, Any]:
    """GET /api/v1/workflows just to confirm both the server and key work."""
    if not is_configured():
        return _wrap_err("HOMEOS_N8N_API_KEY not set; n8n-backed connectors unavailable")
    try:
        r = requests.get(f"{_base_url()}/api/v1/workflows", headers=_headers(), timeout=TIMEOUT_S, params={"limit": 1})
    except requests.RequestException as exc:
        return _wrap_err(f"n8n unreachable at {_base_url()}: {exc}")
    if r.status_code == 401:
        return _wrap_err("n8n rejected the API key (401)")
    if not r.ok:
        return _wrap_err(f"n8n returned HTTP {r.status_code}")
    return {"ok": True, "base_url": _base_url(), "version_hint": r.headers.get("X-N8N-Version", "?")}


def list_workflows() -> Dict[str, Any]:
    if not is_configured():
        return _wrap_err("n8n not configured")
    try:
        r = requests.get(f"{_base_url()}/api/v1/workflows", headers=_headers(), timeout=TIMEOUT_S)
        r.raise_for_status()
        return {"ok": True, "workflows": r.json().get("data", [])}
    except requests.RequestException as exc:
        return _wrap_err(f"list_workflows failed: {exc}")


def get_workflow(workflow_id: str) -> Dict[str, Any]:
    if not is_configured():
        return _wrap_err("n8n not configured")
    try:
        r = requests.get(f"{_base_url()}/api/v1/workflows/{workflow_id}", headers=_headers(), timeout=TIMEOUT_S)
        if r.status_code == 404:
            return _wrap_err("workflow not found", workflow_id=workflow_id)
        r.raise_for_status()
        return {"ok": True, "workflow": r.json()}
    except requests.RequestException as exc:
        return _wrap_err(f"get_workflow failed: {exc}")


def import_workflow(workflow_json: Dict[str, Any]) -> Dict[str, Any]:
    """POST a workflow JSON into n8n. The JSON is the same shape as n8n exports."""
    if not is_configured():
        return _wrap_err("n8n not configured")
    try:
        r = requests.post(
            f"{_base_url()}/api/v1/workflows",
            headers={**_headers(), "Content-Type": "application/json"},
            data=json.dumps(workflow_json),
            timeout=TIMEOUT_S,
        )
        if not r.ok:
            return _wrap_err(f"import failed HTTP {r.status_code}: {r.text[:300]}")
        return {"ok": True, "workflow_id": r.json().get("id"), "name": r.json().get("name")}
    except requests.RequestException as exc:
        return _wrap_err(f"import_workflow failed: {exc}")


def activate_workflow(workflow_id: str) -> Dict[str, Any]:
    if not is_configured():
        return _wrap_err("n8n not configured")
    try:
        r = requests.post(f"{_base_url()}/api/v1/workflows/{workflow_id}/activate", headers=_headers(), timeout=TIMEOUT_S)
        if not r.ok:
            return _wrap_err(f"activate failed HTTP {r.status_code}: {r.text[:300]}")
        return {"ok": True, "workflow_id": workflow_id, "active": True}
    except requests.RequestException as exc:
        return _wrap_err(f"activate_workflow failed: {exc}")


def trigger_webhook(path: str, body: Optional[Dict[str, Any]] = None, method: str = "POST") -> Dict[str, Any]:
    """Trigger a workflow via its webhook node. `path` is what comes after /webhook/.

    NOTE: this uses the webhook URL, not the API URL, and does NOT require
    an API key — webhooks have their own auth (header/basic/none) configured
    on the workflow itself.
    """
    url = f"{_base_url()}/webhook/{path.lstrip('/')}"
    try:
        if method.upper() == "GET":
            r = requests.get(url, params=body or {}, timeout=WEBHOOK_TIMEOUT_S)
        else:
            r = requests.request(method.upper(), url, json=body or {}, timeout=WEBHOOK_TIMEOUT_S)
    except requests.RequestException as exc:
        return _wrap_err(f"webhook call failed: {exc}", path=path)
    if r.status_code == 404:
        return _wrap_err("webhook not registered — is the workflow active?", path=path)
    if not r.ok:
        return _wrap_err(f"webhook HTTP {r.status_code}: {r.text[:200]}", path=path)
    try:
        return {"ok": True, "result": r.json()}
    except ValueError:
        return {"ok": True, "result": r.text}
