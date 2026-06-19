"""Connector layer — the second of HomeOS's three-layer architecture.

Layers:
  1. Layouts     — pure-presentation UI. Sandboxed. No network. (frontend/layouts)
  2. Connectors  — backend services that talk to external APIs. (this package)
  3. Templates   — data-only declarative forms (Elterngeld, taxes; future).

A `Connector` is "a function we can call by name". Two impl strategies share
the same interface:
  - **Built-in Python connectors** (this directory) — fastest, no auth setup,
    perfect for simple HTTP APIs (weather, geocoding, currency). Pre-installed
    in every box.
  - **n8n-backed connectors** (Wave 3) — for OAuth-heavy integrations
    (Gmail, Slack, banking). Credentials live in n8n's encrypted store.

The LLM never knows which backend a connector uses. It just calls
`trigger_connector(name, params)` and gets a dict back.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import pkgutil
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

log = logging.getLogger("homeos.connectors")


@dataclass
class ConnectorSpec:
    """Metadata + entry-point for one connector. Each connector module exports a SPEC."""
    name: str                         # "weather", "geocode"; the LLM uses this
    description: str                  # what it does, shown in the LLM's tool list
    params_schema: Dict[str, Any]     # JSON schema for params
    # Python connectors set this; n8n-backed connectors set it to None and
    # connectors._invoke_n8n() dispatches via webhook instead.
    invoke: Optional[Callable[..., Awaitable[Dict[str, Any]]]]
    requires_auth: bool = False       # if True, the install_connector flow runs an OAuth/key prompt
    install_hint: Optional[str] = None  # human-readable instructions if auth needed
    backend: str = "builtin"          # "builtin" or "n8n"
    version: str = "1.0"
    tags: List[str] = field(default_factory=list)
    # For requires_auth=True + backend=builtin: JSON schema describing the
    # credentials the user must enter. The frontend renders this as a form;
    # values are stored encrypted via credential_store. n8n-backed connectors
    # use n8n's own credential UI and leave this empty.
    credentials_schema: Optional[Dict[str, Any]] = None
    # n8n-only: the workflow JSON template to import on install, and the
    # webhook path to trigger.
    n8n_workflow_template: Optional[Dict[str, Any]] = None
    n8n_webhook_path: Optional[str] = None


_REGISTRY: Dict[str, ConnectorSpec] = {}


def register(spec: ConnectorSpec) -> None:
    if spec.name in _REGISTRY:
        log.warning("connector '%s' already registered — overwriting", spec.name)
    _REGISTRY[spec.name] = spec
    log.info("connector registered: %s (%s)", spec.name, spec.backend)


def get(name: str) -> Optional[ConnectorSpec]:
    return _REGISTRY.get(name)


def list_all() -> List[ConnectorSpec]:
    return list(_REGISTRY.values())


def to_catalogue_entry(spec: ConnectorSpec) -> Dict[str, Any]:
    """Public JSON shape for /api/connectors and the LLM's list tool."""
    # Importing here avoids a cycle at package init time.
    from .. import credential_store
    return {
        "name": spec.name,
        "description": spec.description,
        "params_schema": spec.params_schema,
        "requires_auth": spec.requires_auth,
        "install_hint": spec.install_hint,
        "backend": spec.backend,
        "version": spec.version,
        "tags": list(spec.tags),
        "credentials_schema": spec.credentials_schema,
        "configured": credential_store.get(spec.name) is not None if spec.requires_auth else True,
    }


async def _invoke_n8n(spec: "ConnectorSpec", params: Dict[str, Any]) -> Dict[str, Any]:
    """Dispatch an n8n-backed connector via webhook.

    The webhook URL/path is stored at install time. The connector spec's
    n8n_webhook_path is the default; the credential_store row may override
    it (e.g. n8n assigned a different path on import).
    """
    from .. import credential_store, n8n_client

    install = credential_store.get(spec.name) or {}
    path = install.get("webhook_path") or spec.n8n_webhook_path
    if not path:
        return {
            "ok": False,
            "needs_install": True,
            "error": f"connector '{spec.name}' is n8n-backed but not yet installed. Run install_connector first.",
        }
    return await asyncio.to_thread(lambda: n8n_client.trigger_webhook(path, params))


async def invoke(name: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Call a connector by name. Always returns a dict — errors land in `error` key."""
    spec = get(name)
    if not spec:
        return {"ok": False, "error": f"unknown connector '{name}'", "available": [s.name for s in list_all()]}

    # n8n-backed connectors don't have a Python invoke — they route through webhook.
    if spec.backend == "n8n":
        try:
            result = await _invoke_n8n(spec, params or {})
        except Exception as exc:  # noqa: BLE001
            log.warning("n8n connector %s failed: %s", name, exc, exc_info=True)
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if not isinstance(result, dict):
            result = {"value": result}
        result.setdefault("ok", True)
        return result

    # Python builtin connectors.
    try:
        if inspect.iscoroutinefunction(spec.invoke):
            result = await spec.invoke(**params)
        else:
            # Run sync invokes in a thread so they don't block the event loop.
            result = await asyncio.to_thread(lambda: spec.invoke(**params))
        if not isinstance(result, dict):
            result = {"value": result}
        result.setdefault("ok", True)
        return result
    except TypeError as exc:
        return {"ok": False, "error": f"bad parameters: {exc}"}
    except Exception as exc:  # noqa: BLE001
        log.warning("connector %s failed: %s", name, exc, exc_info=True)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _autodiscover() -> None:
    """Import every sibling module so its top-level register() calls run.

    Skip modules that are already being loaded — that happens when some
    other module does `from .connectors.paperless import _settings` *before*
    `backend.connectors` itself has been imported. Python then loads
    paperless.py first; paperless's `from . import register` triggers our
    `__init__.py` to load; we run `_autodiscover()` here and would dead-
    lock trying to import paperless again. The original load completes
    on its own and calls register() at module end, so re-entering would
    have been a no-op anyway.
    """
    import sys
    pkg = __name__
    for mod_info in pkgutil.iter_modules(__path__):  # type: ignore[name-defined]
        if mod_info.name.startswith("_"):
            continue
        full = f"{pkg}.{mod_info.name}"
        if full in sys.modules:
            continue  # already loaded or in-flight — skip re-entry
        try:
            importlib.import_module(full)
        except Exception as exc:  # noqa: BLE001
            log.exception("connector module %s failed to load: %s", mod_info.name, exc)


_autodiscover()
