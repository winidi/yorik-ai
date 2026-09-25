/**
 * Single source of truth for the dock's app ordering + route mapping.
 * Lives in /lib so both Dock.tsx (renderer) and SwipeNav.tsx
 * (global gesture handler) can import it without pulling in each
 * other's React deps. Keep this list in sync with backend's
 * /api/apps response — the dock filters DOCK_ORDER against the
 * installed-apps payload, so adding an id here that isn't returned
 * by the backend just silently skips it.
 */

// Visual left-to-right order in the bottom dock. Settings is
// intentionally NOT here — it's reachable from /r/home and is a
// destination, not a daily driver. Matches the vanilla dock so
// muscle memory + swipe order line up.
export const DOCK_ORDER = ["home", "calendar", "tasks", "chat", "docs", "compose", "write", "photos", "whatsapp", "email", "contacts", "briefing", "board", "recordings", "finance", "pipelines"];

// A child's account (role 'restricted') sees fewer apps: the family board first, then their
// to-dos, the calendar, photos and the chat. Mail, documents, compose,
// finance and the rest are grown-up tools. The same list drives the
// Dock, the swipe order and Home's tiles.
export const KID_ORDER = ["home", "board", "tasks", "calendar", "photos", "chat"];

// Children's accounts carry role 'restricted' (migration 036 folded the
// old 'child' into it; 'child' can still arrive on an old session).
export function isKid(role?: string | null): boolean { return role === "restricted" || role === "child"; }

export function dockOrderFor(role?: string | null): string[] {
  return isKid(role) ? KID_ORDER : DOCK_ORDER;
}

// app id → React route inside this SPA. Apps not in this map
// either don't exist in the React shell (vanilla) or are
// community apps mounted via /community-app/:appId.
export const REACT_ROUTES: Record<string, string> = {
  home:     "/home",
  email:    "/email",
  whatsapp: "/whatsapp",
  calendar: "/calendar",
  chat:     "/chat",
  docs:     "/documents",
  compose:  "/compose",
  write:    "/write",
  photos:   "/photos",
  tasks:    "/tasks",
  contacts: "/contacts",
  briefing: "/briefing",
  recordings: "/recordings",
  board:    "/board",
  finance:  "/finance",
  pipelines: "/pipelines",
  settings: "/settings",
};

/** Reverse lookup: which app id owns this pathname (or null). */
export function appIdFromPath(pathname: string): string | null {
  for (const [id, route] of Object.entries(REACT_ROUTES)) {
    if (pathname === route || pathname.startsWith(route + "/")) return id;
  }
  return null;
}
