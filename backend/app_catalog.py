"""App catalog — backs the in-Yorik Marketplace.

Reads marketplace/catalog.json at the repo root. Each entry describes
an installable community app: id, display fields, and a `source_type`
that tells the installer where to fetch the source from.

v1 supports `source_type: "bundled"` only — apps shipped inside the
yorik-ai repo under examples/<app>/. Future source types:
  - "git"  : clone a public URL, install from the cloned tree
  - "zip"  : download a zip URL, extract, install

The marketplace endpoints in main.py use load_catalog() + the existing
app_loader.install_app_from_dir_copy() to do the install.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


REPO_ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = REPO_ROOT / "marketplace" / "catalog.json"


@dataclass
class CatalogEntry:
    id: str
    name: str
    description: str
    icon: str
    version: str
    author: str
    source_type: str
    source_path: str
    license: str = ""
    homepage: str = ""
    tags: List[str] = field(default_factory=list)


class CatalogError(Exception):
    """Raised when the catalog file is missing or malformed."""


def load_catalog() -> List[CatalogEntry]:
    """Read the catalog from disk. Returns [] if the file is missing
    (fresh installs without a catalog are valid — the marketplace just
    shows empty)."""
    if not CATALOG_PATH.exists():
        return []
    try:
        raw = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CatalogError(f"catalog.json is malformed: {exc}") from exc
    entries: List[CatalogEntry] = []
    for item in raw.get("apps", []):
        try:
            entries.append(CatalogEntry(
                id=item["id"],
                name=item["name"],
                description=item.get("description", ""),
                icon=item.get("icon", ""),
                version=item.get("version", "0.0.0"),
                author=item.get("author", ""),
                source_type=item["source_type"],
                source_path=item["source_path"],
                license=item.get("license", ""),
                homepage=item.get("homepage", ""),
                tags=list(item.get("tags") or []),
            ))
        except KeyError as exc:
            raise CatalogError(f"catalog entry missing required key: {exc}") from exc
    return entries


def find_entry(app_id: str) -> Optional[CatalogEntry]:
    for e in load_catalog():
        if e.id == app_id:
            return e
    return None


def resolve_source_dir(entry: CatalogEntry) -> Path:
    """Return the absolute on-disk path of the app source.

    For `bundled`, that's REPO_ROOT/<source_path>. Other source types
    aren't supported yet (raise NotImplementedError so the caller can
    surface a clear error to the user).
    """
    if entry.source_type == "bundled":
        return REPO_ROOT / entry.source_path
    raise NotImplementedError(
        f"catalog source_type {entry.source_type!r} not supported in this Yorik version"
    )


def _peek_manifest_grants(entry: CatalogEntry) -> Dict[str, Any]:
    """Read requires_tables_external / requires_connectors from the app's
    bundled manifest.json so the install-confirmation modal can show the
    user what they're consenting to BEFORE the install runs. Best-effort —
    returns empty lists if the source can't be peeked (e.g., future git
    sources before download)."""
    try:
        src = resolve_source_dir(entry)
        mf = src / "manifest.json"
        if not mf.exists():
            return {"requires_tables_external": [], "requires_connectors": []}
        data = json.loads(mf.read_text(encoding="utf-8"))
        return {
            "requires_tables_external": list(data.get("requires_tables_external") or []),
            "requires_connectors": list(data.get("requires_connectors") or []),
        }
    except (NotImplementedError, OSError, json.JSONDecodeError):
        return {"requires_tables_external": [], "requires_connectors": []}


def entry_to_dict(entry: CatalogEntry, *, installed: bool) -> Dict[str, Any]:
    """Shape the entry for /api/apps/available — adds `installed` flag and
    peeked permissions, hides the source mechanics from the frontend."""
    out = {
        "id": entry.id,
        "name": entry.name,
        "description": entry.description,
        "icon": entry.icon,
        "version": entry.version,
        "author": entry.author,
        "license": entry.license,
        "homepage": entry.homepage,
        "tags": entry.tags,
        "installed": installed,
    }
    out.update(_peek_manifest_grants(entry))
    return out
