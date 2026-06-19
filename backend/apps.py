"""Yorik's app registry — first-class destinations on the home screen.

An "app" is the top-level concept above layouts/connectors/templates. Each app
has an icon on the home screen, a name, a voice trigger, and a view kind:

    - "native"  — the parent dashboard renders it directly (chat, docs)
    - "iframe"  — sandboxed JS in a srcdoc iframe, same as our layout pattern
    - "calendar" — special-case: delegates to the existing layout machinery
                   so swapping google-classic ↔ apple-minimal still works

Bundled apps register at import time, same pattern as connectors. Community
apps (Wave 6b) drop a folder under data/apps/<id>/ with a manifest.json and
get loaded by the same registry — no code changes here.

The registry is per-process state; it's not persisted to the DB. Apps that
NEED durable state (CRM, inventory) put their tables in their own
data/apps/<id>/data.db file. That's Wave 6b.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

log = logging.getLogger("homeos.apps")


@dataclass
class App:
    id: str                           # unique slug; matches state.app on the frontend
    name: str                         # display label
    icon: str                         # emoji or short text — rendered in the icon grid
    description: str = ""             # one-line "what this app does"
    view_kind: str = "native"         # "native" | "iframe" | "calendar"
    entry: Optional[str] = None       # iframe: path to JS; native: ignored; calendar: layout id
    requires_role: List[str] = field(default_factory=lambda: ["admin", "member", "child", "employee", "viewer"])
    bundled: bool = True              # community-installed apps set this to False
    version: str = "1.0"
    tags: List[str] = field(default_factory=list)
    # Voice-trigger phrases beyond the literal name. The LLM resolves via
    # navigate_to but having explicit aliases helps when the model isn't sure.
    aliases: List[str] = field(default_factory=list)
    # "embedded" (default — Yorik's top header remains visible) or "fullscreen"
    # (header hidden; only the bottom dock stays as Yorik chrome, so the app's
    # iframe owns the whole viewport). Used by community apps that want their
    # own brand to dominate (CRM, dashboards, etc).
    chrome: str = "embedded"
    # Opt-in apps are registered unconditionally but hidden from /api/apps
    # until the user enables them in Settings → Apps (or via env var). Use
    # this for apps that depend on external infrastructure the user has to
    # set up themselves (e.g. WhatsApp needs the Baileys bridge container).
    # Shipping them disabled-by-default keeps a fresh install from showing
    # apps that crash on first click.
    opt_in: bool = False


_REGISTRY: Dict[str, App] = {}


def _is_opt_in_enabled(app_id: str) -> bool:
    """Check whether an opt-in app has been enabled by the user.

    Order of precedence:
      1. `YORIK_ENABLE_<ID>=1` env var (set in config.env). Wins because
         a user who explicitly set this expects the app to be on even if
         the DB row is missing or stale.
      2. `app_settings.app_enabled_<id> = '1'` (set by the Settings UI).
      3. Default off.

    Kept tolerant of a missing app_settings table / unreachable DB so an
    early-boot /api/apps call doesn't 500.
    """
    env_key = f"YORIK_ENABLE_{app_id.upper()}"
    if os.getenv(env_key, "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    try:
        from . import database  # local import to avoid circular at module load
        with database.get_conn() as conn:
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key = ?",
                (f"app_enabled_{app_id}",),
            ).fetchone()
        return bool(row and str(row[0]).strip() == "1")
    except Exception:  # noqa: BLE001 — see docstring
        return False


def set_opt_in_enabled(app_id: str, enabled: bool) -> None:
    """Persist the opt-in flag. Called by the Settings → Apps toggle.

    Writes to app_settings; the next /api/apps call picks it up (no
    process restart needed because the gate is evaluated per request).
    """
    from . import database
    from datetime import datetime, timezone
    with database.get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value, updated_at) "
            "VALUES (?, ?, ?)",
            (f"app_enabled_{app_id}", "1" if enabled else "0",
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def register(app: App) -> None:
    if app.id in _REGISTRY:
        log.warning("app '%s' already registered — overwriting", app.id)
    _REGISTRY[app.id] = app
    log.info("app registered: %s (%s)", app.id, app.view_kind)


def get(app_id: str) -> Optional[App]:
    return _REGISTRY.get(app_id)


def list_all(role: Optional[str] = None, include_disabled_opt_ins: bool = False) -> List[App]:
    apps = list(_REGISTRY.values())
    if role and role not in ("platform_admin", "admin"):
        apps = [a for a in apps if role in a.requires_role]
    if not include_disabled_opt_ins:
        # Hide opt-in apps the user hasn't enabled. Keeps WhatsApp etc.
        # out of the dock / home grid until the user flips the toggle in
        # Settings → Apps.
        apps = [a for a in apps if not a.opt_in or _is_opt_in_enabled(a.id)]
    # Stable display order: bundled first, then alphabetical by name
    apps.sort(key=lambda a: (not a.bundled, a.name.lower()))
    return apps


def list_opt_in_apps() -> List[Dict[str, Any]]:
    """For Settings → Apps. Returns every opt-in app + its current state,
    regardless of whether it's currently enabled, so the UI can render the
    toggle row."""
    out: List[Dict[str, Any]] = []
    for a in _REGISTRY.values():
        if not a.opt_in:
            continue
        d = to_dict(a)
        d["enabled"] = _is_opt_in_enabled(a.id)
        out.append(d)
    out.sort(key=lambda d: d["name"].lower())
    return out


def to_dict(app: App) -> Dict[str, Any]:
    return {
        "id": app.id,
        "name": app.name,
        "icon": app.icon,
        "description": app.description,
        "view_kind": app.view_kind,
        "entry": app.entry,
        "requires_role": list(app.requires_role),
        "bundled": app.bundled,
        "version": app.version,
        "tags": list(app.tags),
        "aliases": list(app.aliases),
        "chrome": app.chrome,
        "opt_in": app.opt_in,
    }


def resolve_voice_target(query: str) -> Optional[App]:
    """Loose match for voice-driven app opening. The LLM does the heavy lifting
    via navigate_to; this is a safety net for when the LLM passes in something
    that's close but not exact."""
    if not query:
        return None
    q = query.strip().lower()
    # Direct id match
    if q in _REGISTRY:
        return _REGISTRY[q]
    # Name / alias match
    for a in _REGISTRY.values():
        if a.name.lower() == q or q in [x.lower() for x in a.aliases]:
            return a
    # Substring fallback
    for a in _REGISTRY.values():
        if q in a.name.lower():
            return a
    return None


# ─── bundled apps ──────────────────────────────────────────────────────────
# Order is intentional but the registry sort overrides display order.

register(App(
    id="calendar",
    name="Calendar",
    icon="🗓",
    description="Family + business calendar. Switch between Google and Apple layouts; drag to create events; voice to add or move them.",
    view_kind="calendar",
    entry="yorik-calendar",  # default layout id; the Calendar app's own switcher overrides
    tags=["bundled", "core"],
    aliases=["agenda", "schedule", "kalender", "termine"],
))

register(App(
    id="chat",
    name="Chat",
    icon="🤖",
    description="Talk to Yorik. Long-running conversations, voice or text, with full memory across sessions.",
    view_kind="native",
    tags=["bundled", "core"],
    aliases=["assistant", "conversation", "ask"],
))

register(App(
    id="docs",
    name="Documents",
    icon="📄",
    description="Your filing cabinet — PDFs, contracts, invoices, lab results. OCR'd, tagged, searchable by voice (Powered by Paperless-ngx).",
    view_kind="external_iframe",
    entry="paperless",
    # Keep the Yorik header visible so dock / role / theme toggle / settings
    # stay one click away — consistent with Calendar / Chat / Compose.
    chrome="embedded",
    tags=["bundled", "core"],
    aliases=["documents", "files", "papers", "dokumente", "paperless"],
))

# Compose — AI-first document composer (TipTap editor + templates +
# Gotenberg PDF render). Voice creates a draft from a template,
# user refines, and the result is auto-saved to Paperless so it's
# immediately voice-searchable next time.
register(App(
    id="compose",
    name="Compose",
    icon="✍️",
    description="Draft invoices, quotes, letters by voice. Yorik fills templates from your other apps' data; you refine with highlight-and-ask; one click saves to Paperless or emails it out.",
    view_kind="native",
    tags=["bundled", "core"],
    aliases=["compose", "write", "draft", "invoice", "rechnung", "quote", "angebot", "letter", "brief"],
))

# Photos is an iframe over a separate Immich instance running on this host.
# entry_kind="external_iframe" tells the frontend to mount an iframe pointing
# at the Immich URL (autodetected per-origin: same hostname, port 2283 over
# http when on localhost, port 8443 over https when behind Tailscale).
# Chrome is fullscreen — Immich is a full app and competing chrome confuses
# the user.
register(App(
    id="photos",
    name="Photos",
    icon="📷",
    description="Your photos and videos, private and on-device. Auto-uploads from phone via the Immich app. Ask Yorik to find any photo by description, face, or place.",
    view_kind="external_iframe",
    entry="immich",  # frontend looks this up to pick the right URL
    tags=["bundled", "core"],
    aliases=["photos", "pictures", "gallery", "fotos", "bilder", "immich"],
    # Photos uses Immich which has its own complete navigation chrome — go
    # immersive: hide Yorik header AND the dock so the iframe is the whole
    # viewport. A small floating "exit" button in the corner returns home.
    chrome="fullscreen",
))


# WhatsApp — Baileys bridge sits in front of a WhatsApp Web "linked
# device" session. Native UI inside Yorik (no iframe — WhatsApp Web
# blocks framing via X-Frame-Options). The bridge handles the protocol;
# Yorik handles ingestion, draft generation, and the chat surface.
#
# Registered unconditionally so it appears in Settings → Apps where the
# user can toggle it on. opt_in=True keeps it out of /api/apps (dock,
# home grid, voice resolver) until the user enables it — otherwise a
# fresh install would show an app that crashes the moment the Baileys
# bridge isn't running. Enable via Settings → Apps OR YORIK_ENABLE_WHATSAPP=1.
register(App(
    id="whatsapp",
    name="WhatsApp",
    icon="💬",
    description="Your WhatsApp inbox — Yorik reads incoming messages and drafts personalised replies you approve before sending. PDFs auto-file into Documents, images into Photos.",
    view_kind="native",
    chrome="embedded",
    tags=["bundled", "core"],
    aliases=["whatsapp", "wa", "messages", "chats", "wapp"],
    opt_in=True,
))

# Email — multi-account inbox with AI drafts, briefing, semantic search.
# Lives in the React shell at /r/email; this entry exists so it appears
# in the dock alongside the vanilla apps, and so /api/apps reflects the
# full installed-apps catalogue.
# Briefing — daily inbox digest powered by JSON templates (briefings/).
# Lives in the React shell at /r/briefing alongside Email.
register(App(
    id="briefing",
    name="Briefing",
    icon="📰",
    description="Daily summary across email, WhatsApp, calendar, and any custom template you (or the community) install.",
    view_kind="native",
    chrome="embedded",
    tags=["bundled", "core"],
    aliases=["briefing", "digest", "summary", "morning"],
))

register(App(
    id="email",
    name="Email",
    icon="📧",
    description="Your email inbox across all accounts (Gmail, Outlook, iCloud, GMX, etc.). Yorik auto-drafts replies, summarises your inbox, and cross-references with WhatsApp + filed documents.",
    view_kind="native",
    chrome="embedded",
    tags=["bundled", "core"],
    aliases=["email", "mail", "inbox", "imap", "post"],
))

# Tasks — first-class to-do management. Calendar still has the per-day
# task panel for time-of-day planning, but this is the "manage the
# whole list" surface.
register(App(
    id="tasks",
    name="Tasks",
    icon="✅",
    description="Add, complete, and triage to-dos. Tasks with a due date show up on the calendar too. Auto-syncs with the chat agent and the morning briefing card.",
    view_kind="native",
    chrome="embedded",
    tags=["bundled", "core"],
    aliases=["tasks", "todo", "todos", "to-do", "aufgaben"],
))

# Contacts — identity hub. People and businesses Yorik knows about, with
# channels (email/phone/whatsapp) and addresses. Compose, Email and the
# agent all look up here instead of guessing.
register(App(
    id="contacts",
    name="Contacts",
    icon="👥",
    description="Your address book. Auto-captures contacts from email and WhatsApp into a Pending tab so you confirm or spam them. Used by Compose to fill in recipient addresses.",
    view_kind="native",
    chrome="embedded",
    tags=["bundled", "core"],
    aliases=["contacts", "people", "address book", "kontakte", "adressbuch"],
))


def warm_up() -> None:
    """Call on startup to ensure the registry is populated. Importing this
    module already triggers registration of bundled apps; the loader scans
    apps/ for community-installed ones."""
    log.info("apps: %d registered (%s)", len(_REGISTRY), ", ".join(a.id for a in list_all()))


def load_community_apps() -> None:
    """Scan apps/ source dir and load every non-builtin app. Called at
    FastAPI startup AFTER bundled apps register."""
    from . import app_loader
    loaded = app_loader.scan_and_load_all()
    if loaded:
        log.info("community apps loaded: %s", ", ".join(la.app_id for la in loaded))
