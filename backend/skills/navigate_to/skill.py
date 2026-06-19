"""navigate_to — emit a UI navigation action so the user lands on the
right Yorik app after a "show me X" voice/chat request.

Pure navigation — no DB writes, no LLM calls. Caps complexity to the
mapping table below so adding a new app means one line here, not a
new skill.
"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlencode


# Friendly name → React route under /r/. Keep in sync with main.tsx
# Routes. We expose the "dock-facing" name so the LLM can match what
# the user actually says (they say "open whatsapp" not "open /r/whatsapp").
_APP_ROUTES = {
    # English / canonical
    "home":      "/r/home",
    "calendar":  "/r/calendar",
    "chat":      "/r/chat",
    "contacts":  "/r/contacts",
    "documents": "/r/documents",
    "docs":      "/r/documents",
    "compose":   "/r/compose",
    "email":     "/r/email",
    "mail":      "/r/email",
    "inbox":     "/r/email",
    "photos":    "/r/photos",
    "tasks":     "/r/tasks",
    "todo":      "/r/tasks",
    "todos":     "/r/tasks",
    "whatsapp":  "/r/whatsapp",
    "wa":        "/r/whatsapp",
    "briefing":  "/r/briefing",
    "settings":  "/r/settings",
    # German aliases — the LLM passes whatever the user said. Better
    # to accept their phrasing than to round-trip through translation.
    "startseite":     "/r/home",
    "kalender":       "/r/calendar",
    "kontakte":       "/r/contacts",
    "kontakt":        "/r/contacts",
    "dokumente":      "/r/documents",
    "dokument":       "/r/documents",
    "briefe":         "/r/compose",
    "brief":          "/r/compose",
    "schreiben":      "/r/compose",
    "post":           "/r/email",
    "emails":         "/r/email",
    "fotos":          "/r/photos",
    "bilder":         "/r/photos",
    "aufgaben":       "/r/tasks",
    "einstellungen":  "/r/settings",
    "tagesübersicht": "/r/briefing",
    "tagesplan":      "/r/briefing",
}


async def execute(
    ctx,  # noqa: ARG001
    app: str,
    query_params: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    key = (app or "").strip().lower()
    if not key:
        raise ValueError("app is required")
    if key not in _APP_ROUTES:
        # Be lenient — strip trailing 's', try the German plural drop,
        # etc. The LLM occasionally pluralizes ("contact" not "contacts").
        for variant in (key.rstrip("s"), key + "s"):
            if variant in _APP_ROUTES:
                key = variant
                break
        else:
            raise ValueError(
                f"unknown app {app!r}. Pick one of: "
                + ", ".join(sorted(set(_APP_ROUTES.values())))
            )
    path = _APP_ROUTES[key]
    if query_params:
        # Drop None values; coerce everything else to string for safety.
        clean = {k: str(v) for k, v in query_params.items() if v is not None}
        if clean:
            path = f"{path}?{urlencode(clean)}"

    from backend.ui_tools import _append
    _append({
        "type": "navigate",
        "path": path,
        "app":  key,
        "reason": "user asked to see this screen",
    })
    # IMPORTANT: do NOT include literal example reply strings here.
    # Qwen3 copies any concrete example verbatim — that's how the user
    # ended up with "Hier sind deine Kontakte." when they asked to
    # open the calendar (skill.py and skill.md both used to list
    # Kontakte/contacts as the example). Tell the LLM what kind of
    # sentence to write, naming THIS turn's actual destination, but
    # don't hand it a ready-made phrase it can clone.
    return {
        "_llm_hint": (
            f"shown_to_user: navigating the user to the '{key}' screen. "
            f"Write ONE short acknowledgement (≤6 words) in the user's "
            f"language that mentions '{key}' (or its language equivalent — "
            f"e.g. 'Kalender' for calendar, 'Kontakte' for contacts). "
            f"Do NOT copy a stock phrase; do NOT restate the obvious. "
            f"NEVER mention a different screen than '{key}'."
        ),
        "navigated_to": path,
    }
