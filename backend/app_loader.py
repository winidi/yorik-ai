"""Community-app install / load / uninstall lifecycle.

A community app is a directory:

    apps/<id>/
    ├── manifest.json
    ├── schema.sql
    ├── connector.py
    ├── app.js
    ├── importers/         (optional, CSV mapping presets)
    └── README.md

On startup, scan_and_load_all() walks `apps/<id>/` (in the repo) for any apps
NOT in our built-in set (calendar/chat/docs), validates each manifest +
schema, applies the schema to data/apps/<id>/data.db, imports connector.py,
registers each @operation function as a connector with backend="app:<id>",
and registers the App on the home screen.

At runtime, install_app(source) installs an app from either a local directory
or an uploaded zip. uninstall_app(id) reverses everything.

Per-app DB lives at data/apps/<id>/data.db — apart from the package source
so the repo can ship pre-bundled reference apps (carpenter-crm) without
shipping their runtime data.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import logging
import re
import shutil
import sys
import zipfile
from contextvars import Token
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from . import app_sdk, apps as apps_mod, connectors as connectors_mod
from .app_schema_validator import validate_schema
from .database import DEFAULT_DB_PATH

log = logging.getLogger("homeos.app_loader")


# Expose `yorik.app_sdk` so community apps can write the documented
# `from yorik.app_sdk import operation, db, llm` regardless of where
# Yorik's internals actually live. Apps stay decoupled from the
# `backend/` package layout.
import types as _types
if "yorik" not in sys.modules:
    _yorik_pkg = _types.ModuleType("yorik")
    _yorik_pkg.__path__ = []  # mark as a (namespace) package
    sys.modules["yorik"] = _yorik_pkg
sys.modules.setdefault("yorik.app_sdk", app_sdk)

REPO_ROOT = Path(__file__).resolve().parent.parent
APPS_SRC_DIR = REPO_ROOT / "apps"        # package source (committed, read-only at runtime)
APPS_DATA_DIR = REPO_ROOT / "data" / "apps"  # per-app SQLite data (v1) — runtime state
# Phase E §6 — destination for marketplace + zip installs. Lives
# under data/ so it's writable under the systemd ProtectSystem=full
# sandbox; apps/ stays read-only so the bundled (trusted) calendar /
# chat / docs / etc. can't be overwritten by an exploit in the
# install path or by a malicious community app.
APPS_INSTALLED_DIR = REPO_ROOT / "data" / "apps-src"

# IDs of bundled apps registered statically in backend/apps.py. Filesystem
# apps with these IDs are skipped to avoid double-registration.
_BUILTIN_APP_IDS: Set[str] = {"calendar", "chat", "docs"}

# Track loaded community apps so we can unregister cleanly on uninstall.
_LOADED: Dict[str, "LoadedApp"] = {}


@dataclass
class LoadedApp:
    app_id: str
    source_dir: Path
    data_dir: Path
    manifest: Dict[str, Any]
    operation_connector_names: List[str] = field(default_factory=list)


# ─── manifest validation ───────────────────────────────────────────────────

REQUIRED_MANIFEST_FIELDS = {"id", "name", "icon", "version", "schema", "connector", "entry_ui"}
OPTIONAL_MANIFEST_FIELDS = {
    "author", "license", "min_yorik_version", "description",
    "requires_tables_external", "requires_connectors", "home_icon", "tags", "aliases",
    # chrome: "embedded" (default — Yorik's top header stays visible) or
    # "fullscreen" (header hidden; app's iframe takes the full viewport;
    # the bottom dock remains as the only Yorik chrome).
    "chrome",
    # Author bookkeeping — useful even at scale-zero. `author_id` is
    # the slug used in namespaced ids (`<author_id>.<app-slug>`); a
    # stable handle prevents collisions if a second author ever ships
    # an app with the same short name. `homepage` is where the user
    # can read docs.
    "author_id",
    "homepage",
}
ALLOWED_CHROME = {"embedded", "fullscreen"}

# Phase E — manifest v2 adds the platform fields. A manifest with
# `manifest_version: 2` is allowed to use these in addition to the v1
# fields above; a v1 manifest using them is rejected so apps can't
# silently start asking for permissions without bumping the version.
V2_MANIFEST_FIELDS = {
    "manifest_version",
    "owned_schema",
    "owned_tables",
    "permissions",
    "network",
    "ui",
}
ALLOWED_PERMISSION_KEYS = {
    "reads", "writes", "invokes_skills",
    "uses_connectors", "realtime_subscriptions",
    "scheduled", "webhooks",
}
ALLOWED_UI_TYPES = {"spa", "iframe", "none"}
# Per-app schema name must be a safe Postgres identifier. Apps default
# to `app_<id>` if `owned_schema` is omitted.
_SCHEMA_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

# Namespaced app id: `<author-slug>.<app-slug>`. Recommended for any
# app a user installs from outside their own machine — two devs can
# both ship a "cleaning-crm" without colliding. Bare ids (no dot) are
# still accepted because bundled + locally-installed apps don't need
# namespacing; a warning logs to nudge external apps toward it.
_NAMESPACED_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*\.[a-z0-9][a-z0-9_-]*$")
_BARE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _validate_manifest(manifest: Dict[str, Any]) -> List[str]:
    errs: List[str] = []
    mv = manifest.get("manifest_version", 1)
    if mv not in (1, 2):
        errs.append(f"manifest_version must be 1 or 2, got {mv!r}")
        mv = 1
    missing = REQUIRED_MANIFEST_FIELDS - set(manifest)
    if missing:
        errs.append(f"missing required fields: {sorted(missing)}")
    allowed = REQUIRED_MANIFEST_FIELDS | OPTIONAL_MANIFEST_FIELDS
    if mv == 2:
        allowed = allowed | V2_MANIFEST_FIELDS
    unknown = set(manifest) - allowed
    # `manifest_version` itself is always allowed regardless of value, so
    # an invalid version doesn't also become an "unknown field".
    unknown.discard("manifest_version")
    if unknown:
        # Special-case v2-only fields used in a v1 manifest so the
        # author gets a useful error instead of a generic "unknown".
        v2_only = unknown & V2_MANIFEST_FIELDS
        if v2_only and mv == 1:
            errs.append(
                f"v2-only fields {sorted(v2_only)} require manifest_version: 2"
            )
            unknown = unknown - V2_MANIFEST_FIELDS
        if unknown:
            errs.append(f"unknown fields: {sorted(unknown)}")
    # v2 manifests must NOT carry a `backend:` block — apps don't run
    # server processes in v1 of the platform. See §11 of the masterplan
    # for the four patterns apps use when they need more than CRUD.
    if mv == 2 and "backend" in manifest:
        errs.append("manifest v2: `backend` is not allowed; apps don't run server processes")

    aid = manifest.get("id", "")
    if not isinstance(aid, str) or not aid:
        errs.append("id must be a non-empty string")
    elif aid in _BUILTIN_APP_IDS:
        errs.append(f"id {aid!r} collides with a builtin app")
    elif not (_NAMESPACED_ID_RE.match(aid) or _BARE_ID_RE.match(aid)):
        errs.append(
            f"id {aid!r} must be lowercase alphanumeric + hyphens/underscores, "
            f"optionally namespaced as <author>.<app> (e.g. 'acme.cleaning-crm')"
        )
    elif "." not in aid:
        # Warning, not error — bare ids are fine for personal/bundled
        # use. Apps shared with other people should namespace to avoid
        # collisions. Log it so reviewers see.
        log.warning(
            "app %r uses a bare id (no '<author>.' prefix). Bare ids are "
            "fine for local installs but may collide with other authors "
            "if shared — consider namespacing.",
            aid,
        )

    # min_yorik_version — string like "0.1.0" or "0.2". Useful even
    # at v1 so apps can refuse to load against incompatible cores.
    myv = manifest.get("min_yorik_version")
    if myv is not None and not (isinstance(myv, str) and re.match(r"^\d+(\.\d+){0,2}$", myv)):
        errs.append(f"min_yorik_version must look like '0.2' or '0.2.1', got {myv!r}")

    # author_id (optional but recommended for namespaced ids).
    auth = manifest.get("author_id")
    if auth is not None and not (isinstance(auth, str) and auth):
        errs.append("author_id must be a non-empty string when present")

    # homepage URL (optional).
    hp = manifest.get("homepage")
    if hp is not None and not (isinstance(hp, str) and hp.startswith(("http://", "https://"))):
        errs.append("homepage must be an http(s) URL")

    reqs = manifest.get("requires_tables_external") or []
    if not isinstance(reqs, list):
        errs.append("requires_tables_external must be a list")
    else:
        for r in reqs:
            if not isinstance(r, dict) or {"db", "table", "access"} - set(r):
                errs.append(f"requires_tables_external entry malformed: {r}")
            elif r.get("db") not in ("family", "documents"):
                errs.append(f"requires_tables_external db must be 'family' or 'documents', got {r.get('db')!r}")
            elif r.get("access") not in ("read", "write", "read+write"):
                errs.append(f"requires_tables_external access must be read/write/read+write, got {r.get('access')!r}")

    reqc = manifest.get("requires_connectors") or []
    if not isinstance(reqc, list) or not all(isinstance(x, str) for x in reqc):
        errs.append("requires_connectors must be a list of strings")

    chrome = manifest.get("chrome")
    if chrome is not None and chrome not in ALLOWED_CHROME:
        errs.append(f"chrome must be one of {sorted(ALLOWED_CHROME)}, got {chrome!r}")

    if mv == 2:
        _validate_v2_fields(manifest, errs)

    return errs


def _validate_v2_fields(manifest: Dict[str, Any], errs: List[str]) -> None:
    """Validate the platform-era manifest fields (`manifest_version: 2`).

    Mutates `errs`. Anything missing is fine — every v2 field is optional;
    the loader picks safe defaults (empty permissions, schema = `app_<id>`).
    """
    aid = manifest.get("id", "") or "unknown"

    # owned_schema — Postgres identifier. Default = `app_<id>` with
    # hyphens converted to underscores. We don't enforce the default
    # here (the installer does), only validate when present.
    sch = manifest.get("owned_schema")
    if sch is not None:
        if not (isinstance(sch, str) and _SCHEMA_NAME_RE.match(sch)):
            errs.append(
                f"owned_schema {sch!r} must be a Postgres identifier "
                f"(lowercase, starts with a letter, ≤63 chars)"
            )

    # owned_tables — list of table names declared by schema.sql.
    ot = manifest.get("owned_tables")
    if ot is not None:
        if not isinstance(ot, list) or not all(
            isinstance(t, str) and _SCHEMA_NAME_RE.match(t) for t in ot
        ):
            errs.append("owned_tables must be a list of Postgres-safe identifiers")

    # permissions — block with the seven sub-keys.
    perms = manifest.get("permissions")
    if perms is not None:
        if not isinstance(perms, dict):
            errs.append("permissions must be an object")
        else:
            bad = set(perms) - ALLOWED_PERMISSION_KEYS
            if bad:
                errs.append(
                    f"permissions: unknown keys {sorted(bad)}, "
                    f"allowed: {sorted(ALLOWED_PERMISSION_KEYS)}"
                )

            for entry in perms.get("reads") or []:
                if not isinstance(entry, dict) or not entry.get("table") or not entry.get("purpose"):
                    errs.append(f"permissions.reads entry malformed: {entry!r}")
                    continue
                cols = entry.get("columns")
                if cols is not None and not (
                    isinstance(cols, list) and all(isinstance(c, str) for c in cols)
                ):
                    errs.append(f"permissions.reads[{entry.get('table')!r}].columns must be a list of strings")

            # v1 of the platform forbids writes into Yorik core tables —
            # apps already have full CRUD on their owned schema. Cross-write
            # goes through skills (see masterplan §11). If anyone declares
            # a writes entry we accept the shape but log a clear error so
            # the operator sees it on consent review.
            wr = perms.get("writes")
            if wr:
                if not isinstance(wr, list):
                    errs.append("permissions.writes must be a list")
                else:
                    log.warning(
                        "app %s declares permissions.writes — not supported in platform v1; "
                        "use invokes_skills instead", aid,
                    )

            sk = perms.get("invokes_skills")
            if sk is not None and not (isinstance(sk, list) and all(isinstance(s, str) for s in sk)):
                errs.append("permissions.invokes_skills must be a list of skill ids")

            uc = perms.get("uses_connectors")
            if uc is not None and not (isinstance(uc, list) and all(isinstance(s, str) for s in uc)):
                errs.append("permissions.uses_connectors must be a list of connector names")

            rs = perms.get("realtime_subscriptions")
            if rs is not None and not (isinstance(rs, list) and all(isinstance(s, str) for s in rs)):
                errs.append("permissions.realtime_subscriptions must be a list of table names")

            sched = perms.get("scheduled")
            if sched is not None:
                if not isinstance(sched, list):
                    errs.append("permissions.scheduled must be a list")
                else:
                    for s in sched:
                        if not (isinstance(s, dict) and s.get("cron") and s.get("invokes") and s.get("purpose")):
                            errs.append(f"permissions.scheduled entry needs cron + invokes + purpose: {s!r}")

            wh = perms.get("webhooks")
            if wh is not None:
                if not isinstance(wh, list):
                    errs.append("permissions.webhooks must be a list")
                else:
                    for w in wh:
                        if not (isinstance(w, dict) and w.get("path") and w.get("purpose")):
                            errs.append(f"permissions.webhooks entry needs path + purpose: {w!r}")

    # network.outbound — declared external origins (shown on consent screen,
    # enforced via iframe CSP). Each entry needs a purpose string so the
    # user knows why this app is allowed to call the outside world.
    net = manifest.get("network")
    if net is not None:
        if not isinstance(net, dict):
            errs.append("network must be an object")
        else:
            out = net.get("outbound") or []
            if not isinstance(out, list):
                errs.append("network.outbound must be a list")
            else:
                for o in out:
                    if isinstance(o, str):
                        # Bare URL string — accepted but discouraged
                        # (no purpose recorded). Warn so reviewers nudge
                        # the author toward the {url, purpose} form.
                        if not o.startswith(("http://", "https://")):
                            errs.append(f"network.outbound entry must be an http(s) URL, got {o!r}")
                    elif isinstance(o, dict):
                        if not o.get("url") or not o.get("purpose"):
                            errs.append(f"network.outbound entry needs url + purpose: {o!r}")
                        elif not str(o["url"]).startswith(("http://", "https://")):
                            errs.append(f"network.outbound url must be http(s): {o['url']!r}")
                    else:
                        errs.append(f"network.outbound entry malformed: {o!r}")

    # ui — v2 supersedes entry_ui with a structured block.
    ui = manifest.get("ui")
    if ui is not None:
        if not isinstance(ui, dict):
            errs.append("ui must be an object")
        else:
            t = ui.get("type")
            if t is not None and t not in ALLOWED_UI_TYPES:
                errs.append(f"ui.type must be one of {sorted(ALLOWED_UI_TYPES)}, got {t!r}")
            mp = ui.get("mount_path")
            if mp is not None and not (isinstance(mp, str) and mp.startswith("/")):
                errs.append(f"ui.mount_path must start with '/', got {mp!r}")
            entry = ui.get("entry")
            if entry is not None and not isinstance(entry, str):
                errs.append("ui.entry must be a string path")


# ─── operation registration ───────────────────────────────────────────────

def _wrap_operation_as_connector(app_id: str, fn: Callable, op_name: str, role: List[str], doc: str):
    """Wrap a user @operation function so the active_app contextvar is set
    while it runs, then register it as a connector."""
    qualified = f"{app_id}.{op_name}"
    sig = inspect.signature(fn)

    # Build a JSON schema from the function signature.
    properties: Dict[str, Any] = {}
    required: List[str] = []
    for pname, param in sig.parameters.items():
        if pname in ("self", "cls"):
            continue
        type_hint = param.annotation if param.annotation is not inspect.Parameter.empty else str
        json_type = {
            str: "string", int: "integer", float: "number",
            bool: "boolean", list: "array", dict: "object",
        }.get(type_hint, "string")
        prop = {"type": json_type}
        if param.default is inspect.Parameter.empty:
            required.append(pname)
        else:
            prop["default"] = param.default if not callable(param.default) else None
        properties[pname] = prop
    params_schema = {"type": "object", "properties": properties}
    if required:
        params_schema["required"] = required

    async def wrapper(**kwargs):
        token: Token = app_sdk.set_active_app(app_id)
        try:
            if inspect.iscoroutinefunction(fn):
                return await fn(**kwargs)
            else:
                # Run sync user code in a thread to keep the event loop free.
                import asyncio
                return await asyncio.to_thread(lambda: fn(**kwargs))
        finally:
            app_sdk.reset_active_app(token)

    spec = connectors_mod.ConnectorSpec(
        name=qualified,
        description=f"[{app_id}] {doc}",
        params_schema=params_schema,
        invoke=wrapper,
        requires_auth=False,  # auth is the app-grant system, not creds
        backend=f"app:{app_id}",
        version="1.0",
        tags=["app", app_id],
    )
    connectors_mod.register(spec)
    return qualified


def _discover_operations(module) -> List[Callable]:
    """Walk module attributes, return functions tagged with @operation."""
    out = []
    for name in dir(module):
        obj = getattr(module, name, None)
        if callable(obj) and getattr(obj, "_yorik_operation", False):
            out.append(obj)
    return out


# ─── load / install / uninstall ────────────────────────────────────────────

def _load_module_from_path(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    if not spec or not spec.loader:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def load_app(
    source_dir: Path,
    *,
    validate_only: bool = False,
    install_db: bool = True,
) -> LoadedApp:
    """Validate + load an app from the given source directory.

    Returns a LoadedApp record. Raises ValueError on validation failure
    (with a multi-line message so the caller can surface it cleanly).

    `install_db=False` skips the per-app DB setup (sqlite file create
    for v1, or Postgres CREATE SCHEMA + RLS for v2) and only does the
    Python-side registration. Used by the boot reloader, which finds
    apps in the installed_apps ledger whose Postgres schema is already
    in place — re-running CREATE SCHEMA would drop the user's data.
    """
    source_dir = source_dir.resolve()
    manifest_path = source_dir / "manifest.json"
    if not manifest_path.exists():
        raise ValueError(f"{source_dir}: manifest.json missing")
    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"{source_dir}: manifest.json invalid JSON: {exc}") from exc

    errs = _validate_manifest(manifest)
    if errs:
        raise ValueError(f"{source_dir}/manifest.json invalid:\n  - " + "\n  - ".join(errs))

    app_id = manifest["id"]

    # Validate schema
    schema_path = source_dir / manifest["schema"]
    if not schema_path.exists():
        raise ValueError(f"{source_dir}: schema file {manifest['schema']!r} missing")
    schema_sql = schema_path.read_text()
    # The sqlite-targeted validator (v1) doesn't know Postgres types
    # like UUID / BIGSERIAL / TIMESTAMPTZ. v2 schemas run inside an
    # isolated Postgres schema under `SET LOCAL search_path = app_<id>`
    # — Postgres itself rejects bad DDL and the search_path scope
    # contains the blast radius. Skip the v1 validator there.
    if manifest.get("manifest_version") != 2:
        sv = validate_schema(schema_sql)
        if not sv.ok:
            msg_lines = "\n  - ".join(str(e) for e in sv.errors)
            raise ValueError(f"{source_dir}/{manifest['schema']} failed validation:\n  - {msg_lines}")

    if validate_only:
        # Synthesize a LoadedApp without side effects.
        return LoadedApp(
            app_id=app_id,
            source_dir=source_dir,
            data_dir=APPS_DATA_DIR / app_id,
            manifest=manifest,
        )

    # Apply schema to the per-app DB.
    data_dir = APPS_DATA_DIR / app_id
    data_dir.mkdir(parents=True, exist_ok=True)

    if install_db:
        if manifest.get("manifest_version") == 2:
            # v2 path: per-app Postgres schema. policies.sql is required
            # when the manifest declares owned_tables (the lifecycle
            # module enforces).
            from . import app_schema_lifecycle as _lifecycle
            policies_path = source_dir / "policies.sql"
            policies_sql = policies_path.read_text() if policies_path.exists() else None
            _lifecycle.install_app_schema(
                manifest=manifest,
                schema_sql=schema_sql,
                policies_sql=policies_sql,
                source_dir=str(source_dir),
            )
        else:
            # v1 path: per-app SQLite file at data/apps/<id>/data.db.
            import sqlite3
            db_path = data_dir / "data.db"
            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute("PRAGMA foreign_keys = ON;")
                conn.executescript(schema_sql)
                conn.commit()
            finally:
                conn.close()

    # Import connector module and register operations.
    connector_path = source_dir / manifest["connector"]
    if not connector_path.exists():
        raise ValueError(f"{source_dir}: connector file {manifest['connector']!r} missing")
    module_name = f"yorik_app_{app_id.replace('-', '_')}"
    try:
        module = _load_module_from_path(module_name, connector_path)
    except Exception as exc:
        raise ValueError(f"{source_dir}/{manifest['connector']} failed to import: {exc}") from exc

    op_funcs = _discover_operations(module)
    registered_names: List[str] = []
    for fn in op_funcs:
        name = _wrap_operation_as_connector(
            app_id=app_id,
            fn=fn,
            op_name=fn._yorik_op_name,
            role=fn._yorik_op_role,
            doc=fn._yorik_op_doc,
        )
        registered_names.append(name)

    # Register the App in the home-screen registry.
    apps_mod.register(apps_mod.App(
        id=app_id,
        name=manifest["name"],
        icon=manifest["icon"],
        description=manifest.get("description") or "",
        view_kind="iframe",
        entry=manifest["entry_ui"],  # filename inside source_dir
        bundled=False,
        version=manifest["version"],
        tags=manifest.get("tags") or [],
        aliases=manifest.get("aliases") or [],
        chrome=manifest.get("chrome") or "embedded",
    ))

    loaded = LoadedApp(
        app_id=app_id,
        source_dir=source_dir,
        data_dir=data_dir,
        manifest=manifest,
        operation_connector_names=registered_names,
    )
    _LOADED[app_id] = loaded
    if manifest.get("manifest_version") == 2:
        log.info("app loaded (v2): %s (%d operations, schema=%s)",
                 app_id, len(registered_names),
                 manifest.get("owned_schema") or f"app_{app_id}")
    else:
        log.info("app loaded: %s (%d operations, data_dir=%s)",
                 app_id, len(registered_names), data_dir)
    return loaded


def unload_app(app_id: str) -> bool:
    """Reverse load_app: unregister connectors + the app from the home screen.
    Does NOT delete the per-app data directory — that's a separate uninstall_app() call."""
    loaded = _LOADED.pop(app_id, None)
    if not loaded:
        return False
    for cname in loaded.operation_connector_names:
        connectors_mod._REGISTRY.pop(cname, None)
    apps_mod._REGISTRY.pop(app_id, None)
    # Remove imported module so a re-install runs fresh code.
    mod_name = f"yorik_app_{app_id.replace('-', '_')}"
    sys.modules.pop(mod_name, None)
    log.info("app unloaded: %s", app_id)
    return True


def uninstall_app(app_id: str, *, wipe_data: bool = True) -> bool:
    """Full uninstall: unload + optionally rm -rf the per-app data dir
    (v1) or DROP SCHEMA app_<id> CASCADE (v2).

    Manifest version is read from the installed_apps ledger so a v2
    uninstall works even if the app wasn't in _LOADED on restart
    (in-place installs don't persist to apps/<id>/).
    """
    from . import app_schema_lifecycle as _lifecycle
    installed = _lifecycle.get_installed_app(app_id)
    manifest_version: Optional[int]
    if installed and installed.get("manifest"):
        manifest_version = installed["manifest"].get("manifest_version")
    else:
        loaded = _LOADED.get(app_id)
        manifest_version = (loaded.manifest.get("manifest_version") if loaded else None)

    unload_app(app_id)

    if manifest_version == 2:
        _lifecycle.uninstall_app_schema(app_id=app_id, keep_data=not wipe_data)
    data_dir = APPS_DATA_DIR / app_id
    if wipe_data and data_dir.exists():
        shutil.rmtree(data_dir)
        log.info("app data wiped: %s", data_dir)
    return True


def install_app_from_dir(source: Path) -> LoadedApp:
    """Install an app from a local source directory. (Doesn't move it; just
    registers it. The source stays where it is.)"""
    return load_app(source)


def install_app_from_dir_copy(source: Path) -> LoadedApp:
    """Like install_app_from_dir, but COPIES the source into
    data/apps-src/<id>/ so the install persists across uvicorn
    restarts. Used by the marketplace install path and the §7
    consent confirm endpoint.

    The destination is intentionally under data/ (writable in the
    systemd sandbox), not apps/ (the read-only bundled-apps tree).
    Keeping community installs out of apps/ means an exploit in the
    install path can only touch data/ — the bundled calendar / chat /
    docs connectors stay immutable.
    """
    manifest = json.loads((source / "manifest.json").read_text())
    app_id = manifest.get("id")
    if not app_id:
        raise ValueError(f"{source}/manifest.json missing 'id'")
    APPS_INSTALLED_DIR.mkdir(parents=True, exist_ok=True)
    target = APPS_INSTALLED_DIR / app_id
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)
    return load_app(target)


def install_app_from_zip(zip_path: Path, dest: Optional[Path] = None) -> LoadedApp:
    """Install from a zip archive. Extracts to data/apps-src/<id>/
    then loads. Same trust separation as install_app_from_dir_copy:
    untrusted zip contents land under data/, never inside apps/.
    """
    with zipfile.ZipFile(zip_path) as zf:
        # Find the manifest inside the zip to figure out the app id
        manifest_member = next((n for n in zf.namelist() if n.endswith("manifest.json")), None)
        if not manifest_member:
            raise ValueError("zip missing manifest.json")
        manifest = json.loads(zf.read(manifest_member))
        app_id = manifest.get("id")
        if not app_id:
            raise ValueError("zip's manifest.json missing 'id'")
        target = (dest or APPS_INSTALLED_DIR) / app_id
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
        # Determine the prefix in the zip (e.g. "carpenter-crm/" or "")
        prefix = manifest_member[:-len("manifest.json")]
        for member in zf.namelist():
            if not member.startswith(prefix) or member == prefix:
                continue
            rel = member[len(prefix):]
            if not rel:
                continue
            out_path = target / rel
            if member.endswith("/"):
                out_path.mkdir(parents=True, exist_ok=True)
            else:
                out_path.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, open(out_path, "wb") as dst:
                    dst.write(src.read())
    return load_app(target)


def scan_and_load_all(apps_src_dir: Optional[Path] = None) -> List[LoadedApp]:
    """Walk the apps/ source directory and load every app that isn't builtin.
    Also re-register v2 in-place installs from the installed_apps ledger
    so apps survive a uvicorn restart without being copied into apps/.

    Called once at FastAPI startup. Errors on individual apps are logged
    (so one bad app doesn't break the box) but the others still load.
    """
    src = apps_src_dir or APPS_SRC_DIR
    loaded: List[LoadedApp] = []
    # Two source roots:
    #   - apps/ (bundled, committed, read-only) — install_db=True
    #     so a fresh checkout creates the per-app sqlite file.
    #   - data/apps-src/ (marketplace + zip installs, writable) —
    #     install_db=False because the lifecycle already created
    #     the Postgres schema when this install was confirmed.
    #     Running CREATE SCHEMA again would drop the user's data.
    # Iterate apps/ first so a bundled app wins on collision —
    # community installs must never silently override the trusted
    # bundle.
    for root, install_db in ((src, True), (APPS_INSTALLED_DIR, False)):
        if not root.exists():
            continue
        for entry in sorted(root.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            if entry.name in _BUILTIN_APP_IDS:
                continue
            if entry.name in _LOADED:
                continue  # already loaded from a higher-trust root
            try:
                loaded.append(load_app(entry, install_db=install_db))
            except Exception as exc:
                log.exception("failed to load app %s: %s", entry.name, exc)

    # Phase E §13 — re-register v2 installs whose source isn't under
    # apps/. The Postgres schema is already in place, so pass
    # install_db=False to skip the DROP+CREATE step (which would wipe
    # the user's data). source_dir comes from the ledger; we log and
    # skip if the directory has vanished between restarts.
    try:
        from . import app_schema_lifecycle as _lifecycle
        from .database_pg import conn_ctx_pg as _conn_ctx_pg
        # Phase E #36 — make sure PostgREST's exposed schemas list
        # matches the ledger after a restart. Idempotent.
        try:
            with _conn_ctx_pg("main") as _c:
                _lifecycle._resync_postgrest_schemas(_c)
                _c.commit()
        except Exception:  # noqa: BLE001
            log.exception("boot: PGRST schema resync failed (non-fatal)")
        for row in _lifecycle.list_active_v2_installs():
            aid = row["app_id"]
            if aid in _LOADED:
                continue  # already loaded from apps/
            src_path = row.get("source_dir")
            if not src_path:
                log.warning("v2 app %s: no source_dir in ledger — skip", aid)
                continue
            sp = Path(src_path)
            if not sp.exists():
                log.warning("v2 app %s: source_dir %s gone — skip", aid, sp)
                continue
            try:
                loaded.append(load_app(sp, install_db=False))
            except Exception as exc:
                log.exception("failed to re-register v2 app %s: %s", aid, exc)
    except Exception:
        # Ledger query failing (e.g. table doesn't exist yet on a
        # fresh DB) shouldn't block apps/ loading.
        log.exception("scan_and_load_all: v2 ledger reload failed")

    return loaded


def get_loaded(app_id: str) -> Optional[LoadedApp]:
    return _LOADED.get(app_id)


def list_loaded() -> List[LoadedApp]:
    return list(_LOADED.values())
