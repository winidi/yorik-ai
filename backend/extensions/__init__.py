"""Yorik extensions — optional, regionally-scoped, or domain-specific
add-ons that ship outside the lean core.

An extension lives at extensions/<id>/ with:
  extension.json     — metadata + Python deps + which hooks it exports
  requirements.txt   — pip-installable extra Python packages
  *.py               — the module(s) that get imported when the extension
                       is loaded; top-level register_hook() calls wire
                       it into the platform

Loader flow:
  1. Scan extensions/ → read each extension.json (Tier-0 schema-validate)
  2. For each: check Python deps via importlib.metadata
       installed → dynamically import the module(s), top-level
                   register_hook() calls run, extension is "active"
       missing   → skipped; surfaced in /api/extensions so the user can
                   one-click install
  3. Yorik core code calls extensions.invoke_hooks("hook.name", **kw) at
     well-defined points. Each registered hook gets called in registration
     order; return values are chained when the hook is a transform.

Why this exists:
  - ZUGFeRD (German e-invoice format) shouldn't ship to American users.
    Other regional features (Swiss QR-Bill, UK MTD, Italian FatturaPA)
    will follow the same pattern.
  - Heavy optional deps (LLM model packs, additional CV/NLP libs) stay
    out of base install.
  - Community can contribute extensions without touching core.

Hooks currently defined:
  compose.pdf_post_process(pdf_bytes, template, args) → pdf_bytes
    Called after Gotenberg renders the Compose PDF, BEFORE returning
    it to the user. Extensions can transform the PDF (e.g. embed XML
    for ZUGFeRD). Chain semantics: each hook receives the output of
    the previous one. Returning None falls through to the prior bytes.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("homeos.extensions")

EXTENSIONS_DIR = Path(os.getenv("HOMEOS_EXTENSIONS_DIR", "extensions"))

REQUIRED_FIELDS = {"id", "name", "version"}
OPTIONAL_FIELDS = {
    "description", "author", "license", "country", "tags",
    "python_requirements", "modules", "hooks", "docs_url",
}

# ─── Hook registry ────────────────────────────────────────────────────────

_hooks: Dict[str, List[Callable]] = {}


def register_hook(name: str, fn: Callable) -> None:
    """Extensions call this at module-load time to wire themselves in."""
    _hooks.setdefault(name, []).append(fn)
    log.info("hook registered: %s → %s.%s", name, fn.__module__, fn.__name__)


def invoke_hooks(name: str, *args, **kwargs) -> Any:
    """Run every hook registered under `name`. For transform-style hooks
    that take + return a value as the first positional arg, chain the
    output of one hook into the next (None falls through to prior bytes)."""
    callbacks = _hooks.get(name) or []
    if not callbacks:
        return args[0] if args else None
    current = args[0] if args else None
    rest = args[1:]
    for cb in callbacks:
        try:
            result = cb(current, *rest, **kwargs)
            if result is not None:
                current = result
        except Exception as exc:  # noqa: BLE001
            log.exception("extension hook %s.%s failed: %s", name, cb.__name__, exc)
    return current


def list_hooks() -> Dict[str, List[str]]:
    return {name: [f"{cb.__module__}.{cb.__name__}" for cb in cbs]
            for name, cbs in _hooks.items()}


# ─── Extension manifest validation ────────────────────────────────────────

class ExtensionError(ValueError):
    pass


def _validate(manifest: Dict[str, Any]) -> List[str]:
    errs: List[str] = []
    missing = REQUIRED_FIELDS - set(manifest)
    if missing:
        errs.append(f"missing fields: {sorted(missing)}")
    unknown = set(manifest) - REQUIRED_FIELDS - OPTIONAL_FIELDS
    if unknown:
        errs.append(f"unknown fields: {sorted(unknown)}")
    aid = manifest.get("id", "")
    if not isinstance(aid, str) or not aid or not aid.replace("-", "_").replace("_", "").isalnum():
        errs.append("id must be alphanumeric + hyphens/underscores")
    py = manifest.get("python_requirements") or []
    if not isinstance(py, list) or not all(isinstance(x, str) for x in py):
        errs.append("python_requirements must be a list of pip-style strings")
    return errs


# ─── Dependency check ────────────────────────────────────────────────────

def _dep_installed(req: str) -> bool:
    """Check if a single 'pkg>=ver' requirement is satisfied. Uses
    importlib.metadata which respects extras + version specifiers via
    a string match (good enough for the common cases; not a full PEP 508
    resolver — extensions should keep their requirements simple)."""
    import re
    m = re.match(r"^([A-Za-z0-9_\-.]+)", req)
    if not m:
        return False
    pkg = m.group(1)
    try:
        importlib.metadata.distribution(pkg)
        return True
    except importlib.metadata.PackageNotFoundError:
        return False


def deps_status(manifest: Dict[str, Any]) -> Dict[str, Any]:
    """Returns {"all_met": bool, "missing": [...], "present": [...]}."""
    reqs = manifest.get("python_requirements") or []
    missing = [r for r in reqs if not _dep_installed(r)]
    present = [r for r in reqs if r not in missing]
    return {"all_met": not missing, "missing": missing, "present": present}


# ─── Discovery + loading ─────────────────────────────────────────────────

_loaded_ids: set[str] = set()


def _load_one(ext_dir: Path) -> Dict[str, Any]:
    """Read one extension manifest, validate, attempt to load its code
    modules if deps are satisfied. Returns the manifest enriched with
    {_source_dir, _deps, _loaded, _errors}."""
    manifest_path = ext_dir / "extension.json"
    if not manifest_path.exists():
        return {"_source_dir": str(ext_dir), "_loaded": False,
                "_errors": ["extension.json missing"]}
    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as exc:
        return {"_source_dir": str(ext_dir), "_loaded": False,
                "_errors": [f"invalid JSON: {exc}"]}

    errs = _validate(manifest)
    manifest["_source_dir"] = str(ext_dir)
    manifest["_errors"] = errs
    deps = deps_status(manifest)
    manifest["_deps"] = deps

    if errs:
        manifest["_loaded"] = False
        return manifest
    if not deps["all_met"]:
        manifest["_loaded"] = False
        return manifest

    # Load the extension's Python modules. Default: every .py file in the
    # extension dir (except __init__ / extension.* / dotfiles). The manifest
    # can override via a "modules" list.
    module_names = manifest.get("modules") or sorted(
        p.stem for p in ext_dir.glob("*.py") if not p.stem.startswith("_") and p.stem != "extension"
    )
    if manifest["id"] in _loaded_ids:
        manifest["_loaded"] = True
        return manifest
    _loaded_ids.add(manifest["id"])
    for mod_name in module_names:
        mod_path = ext_dir / f"{mod_name}.py"
        if not mod_path.exists():
            continue
        # Import the module under a synthetic name so multiple extensions
        # can have a `zugferd.py` without clashing.
        spec_name = f"yorik_ext_{manifest['id']}_{mod_name}"
        spec = importlib.util.spec_from_file_location(spec_name, mod_path)
        if not spec or not spec.loader:
            continue
        try:
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec_name] = module
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001
            log.exception("extension %s module %s failed to load: %s",
                          manifest["id"], mod_name, exc)
            manifest["_errors"].append(f"module {mod_name}: {exc}")
    manifest["_loaded"] = not manifest["_errors"]
    return manifest


def load_all() -> List[Dict[str, Any]]:
    """Scan EXTENSIONS_DIR and load every extension. Already-loaded
    extensions are skipped (cached via _loaded_ids) so this is safe to
    call repeatedly."""
    if not EXTENSIONS_DIR.exists():
        return []
    out: List[Dict[str, Any]] = []
    for sub in sorted(EXTENSIONS_DIR.iterdir()):
        if not sub.is_dir():
            continue
        out.append(_load_one(sub))
    return out


def public_dict(m: Dict[str, Any]) -> Dict[str, Any]:
    """Slim shape for /api/extensions."""
    return {
        "id": m.get("id"),
        "name": m.get("name"),
        "description": m.get("description") or "",
        "version": m.get("version"),
        "author": m.get("author") or "yorik-core",
        "country": m.get("country"),
        "tags": list(m.get("tags") or []),
        "python_requirements": list(m.get("python_requirements") or []),
        "deps": m.get("_deps", {"all_met": False, "missing": [], "present": []}),
        "loaded": bool(m.get("_loaded")),
        "errors": list(m.get("_errors") or []),
        "docs_url": m.get("docs_url"),
    }


# ─── pip install on demand ───────────────────────────────────────────────

def install_dependencies(extension_id: str) -> Dict[str, Any]:
    """Run `pip install -r extensions/<id>/requirements.txt` in the same
    Python interpreter Yorik is running under. Admin-gated at the
    endpoint layer. Returns stdout/stderr for the UI to surface."""
    ext_dir = EXTENSIONS_DIR / extension_id
    if not ext_dir.exists():
        return {"ok": False, "error": f"extension {extension_id} not found"}
    reqs = ext_dir / "requirements.txt"
    if not reqs.exists():
        return {"ok": True, "message": "no requirements.txt — nothing to install"}
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", str(reqs),
             "--disable-pip-version-check", "--no-input"],
            capture_output=True, text=True, timeout=300,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "pip install timed out after 5 minutes"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "")[-2000:],
        "stderr": (proc.stderr or "")[-2000:],
    }
