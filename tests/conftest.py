"""Shared pytest fixtures.

Each test gets a fresh tmp SQLite DB so the auth tests don't leak rows
into your real `data/family.db`. We point the env vars BEFORE importing
the backend module so the constants pick up the tmp path.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Iterator

import pytest


@pytest.fixture
def fresh_app(monkeypatch: pytest.MonkeyPatch) -> Iterator:
    """Reload backend.main against an empty DB. Yields the FastAPI app
    instance. Tests should use `TestClient(fresh_app)` for the request
    interface."""
    tmp = tempfile.TemporaryDirectory()
    db_path = Path(tmp.name) / "family.db"
    monkeypatch.setenv("HOMEOS_DB_PATH", str(db_path))
    monkeypatch.setenv("HOMEOS_DOCS_DIR", str(Path(tmp.name) / "docs"))
    monkeypatch.setenv("HOMEOS_DOCS_DB_PATH", str(Path(tmp.name) / "documents.db"))

    # Force re-import so module-level constants (DB_PATH etc.) bind to
    # the env vars we just set, even if a previous test already loaded
    # the module.
    import importlib
    import sys
    for mod_name in list(sys.modules):
        if mod_name.startswith("backend"):
            del sys.modules[mod_name]
    from backend import main as backend_main  # noqa: E402

    yield backend_main.app

    tmp.cleanup()
