/**
 * Bottom dock — Mac-style. Fetches /api/apps and renders one tile per
 * installed app. Bundled apps get a lucide glyph in a gradient-tinted
 * squircle, matching the tile aesthetic of /r/home so the dock and the
 * landing screen read as the same surface. Community apps fall back to
 * the emoji shipped in their manifest so app authors don't need to
 * touch this file.
 *
 * Routing rules:
 *  - apps listed in REACT_ROUTES → in-shell navigate
 *  - everything else → window.location to the vanilla URL
 *  - home → /
 */

import { useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import {
  House, Calendar, ListTodo, MessageSquare, FolderOpen,
  FilePlus, Camera, MessageCircle, Inbox, Contact,
  Newspaper, Settings,
  Mic, type LucideIcon, LayoutGrid, PenLine, Landmark, Workflow, CircleHelp, Grip } from "lucide-react";

import { cn } from "@/lib/utils";
import i18n from "@/i18n";
import { api } from "@/lib/api";
import { dockOrderFor, isKid, phoneTabsFor, REACT_ROUTES } from "@/lib/dock-order";
import { useAuth } from "@/components/AuthGate";
import { openHelp } from "@/components/HelpPanel";
import { useIsPhone } from "@/lib/use-is-phone";

interface AppInfo {
  id: string;
  name: string;
  icon: string;
  bundled?: boolean;
}

const HOME_TILE: AppInfo = { id: "home", name: "Home", icon: "🏠", bundled: true };

// Vanilla apps whose URL slug differs from their app id. (Empty now —
// "docs" → /documents is handled via REACT_ROUTES above. Kept for future
// vanilla apps that need a URL alias.)
const VANILLA_PATHS: Record<string, string> = {};

// Per-app visual: lucide glyph + gradient hues + saturated glyph color +
// 1px hue ring. Hues, icons, and gradient stops mirror /r/home's APPS
// table so the two surfaces feel like one product. The ring is dock-
// specific — at 48px shoulder-to-shoulder, the hairline outline gives
// each tile the crisp "app icon" edge that home's larger grid doesn't
// need. Tailwind v4 JIT can't expand dynamic class names, so the full
// strings live here verbatim. Active tile bumps gradient opacity
// (/30 → /50) and ring opacity (/40 → /80) for a "lit" feel.
export const APP_VISUAL: Record<string, { Icon: LucideIcon; gradient: string; activeGradient: string; text: string; ring: string; activeRing: string }> = {
  home:     { Icon: House,         gradient: "from-slate-500/30 to-zinc-500/30",     activeGradient: "from-slate-500/50 to-zinc-500/50",     text: "text-slate-500",   ring: "ring-slate-400/40",   activeRing: "ring-slate-400/80" },
  calendar: { Icon: Calendar,      gradient: "from-blue-500/30 to-cyan-500/30",      activeGradient: "from-blue-500/50 to-cyan-500/50",      text: "text-blue-500",    ring: "ring-blue-400/40",    activeRing: "ring-blue-400/80" },
  tasks:    { Icon: ListTodo,      gradient: "from-emerald-500/30 to-teal-500/30",   activeGradient: "from-emerald-500/50 to-teal-500/50",   text: "text-emerald-500", ring: "ring-emerald-400/40", activeRing: "ring-emerald-400/80" },
  chat:     { Icon: MessageSquare, gradient: "from-violet-500/30 to-blue-500/30",    activeGradient: "from-violet-500/50 to-blue-500/50",    text: "text-violet-500",  ring: "ring-violet-400/40",  activeRing: "ring-violet-400/80" },
  docs:     { Icon: FolderOpen,    gradient: "from-amber-500/30 to-orange-500/30",   activeGradient: "from-amber-500/50 to-orange-500/50",   text: "text-amber-500",   ring: "ring-amber-400/40",   activeRing: "ring-amber-400/80" },
  compose:  { Icon: FilePlus,      gradient: "from-rose-500/30 to-pink-500/30",      activeGradient: "from-rose-500/50 to-pink-500/50",      text: "text-rose-500",    ring: "ring-rose-400/40",    activeRing: "ring-rose-400/80" },
  write:    { Icon: PenLine,       gradient: "from-indigo-500/30 to-violet-500/30",  activeGradient: "from-indigo-500/50 to-violet-500/50",  text: "text-indigo-500",  ring: "ring-indigo-400/40",  activeRing: "ring-indigo-400/80" },
  photos:   { Icon: Camera,        gradient: "from-emerald-500/30 to-teal-500/30",   activeGradient: "from-emerald-500/50 to-teal-500/50",   text: "text-emerald-500", ring: "ring-emerald-400/40", activeRing: "ring-emerald-400/80" },
  whatsapp: { Icon: MessageCircle, gradient: "from-emerald-500/30 to-green-500/30",  activeGradient: "from-emerald-500/50 to-green-500/50",  text: "text-emerald-500", ring: "ring-emerald-400/40", activeRing: "ring-emerald-400/80" },
  email:    { Icon: Inbox,         gradient: "from-sky-500/30 to-blue-500/30",       activeGradient: "from-sky-500/50 to-blue-500/50",       text: "text-sky-500",     ring: "ring-sky-400/40",     activeRing: "ring-sky-400/80" },
  contacts: { Icon: Contact,       gradient: "from-orange-500/30 to-amber-500/30",   activeGradient: "from-orange-500/50 to-amber-500/50",   text: "text-orange-500",  ring: "ring-orange-400/40",  activeRing: "ring-orange-400/80" },
  briefing: { Icon: Newspaper,     gradient: "from-fuchsia-500/30 to-purple-500/30", activeGradient: "from-fuchsia-500/50 to-purple-500/50", text: "text-fuchsia-500", ring: "ring-fuchsia-400/40", activeRing: "ring-fuchsia-400/80" },
  recordings: { Icon: Mic,           gradient: "from-red-500/30 to-rose-500/30",       activeGradient: "from-red-500/50 to-rose-500/50",       text: "text-red-500",     ring: "ring-red-400/40",     activeRing: "ring-red-400/80" },
  board:    { Icon: LayoutGrid,    gradient: "from-lime-500/30 to-emerald-500/30",   activeGradient: "from-lime-500/50 to-emerald-500/50",   text: "text-lime-600",    ring: "ring-lime-400/40",    activeRing: "ring-lime-400/80" },
  finance:  { Icon: Landmark,      gradient: "from-teal-500/30 to-cyan-500/30",      activeGradient: "from-teal-500/50 to-cyan-500/50",      text: "text-teal-600",    ring: "ring-teal-400/40",    activeRing: "ring-teal-400/80" },
  pipelines: { Icon: Workflow,    gradient: "from-violet-500/30 to-indigo-500/30",  activeGradient: "from-violet-500/50 to-indigo-500/50",  text: "text-violet-500",  ring: "ring-violet-400/40",  activeRing: "ring-violet-400/80" },
  settings: { Icon: Settings,      gradient: "from-slate-500/30 to-zinc-500/30",     activeGradient: "from-slate-500/50 to-zinc-500/50",     text: "text-slate-500",   ring: "ring-slate-400/40",   activeRing: "ring-slate-400/80" },
};

interface Props {
  /** The currently-active app id so the dock can highlight it. */
  activeAppId: string;
}

function appName(a: AppInfo): string {
  return i18n.t(`apps.${a.id}`, { defaultValue: a.name });
}

export function Dock({ activeAppId }: Props) {
  const [apps, setApps] = useState<AppInfo[]>([]);
  const [moreOpen, setMoreOpen] = useState(false);
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const phone = useIsPhone();
  const role = useAuth().user?.role;
  const kid = isKid(role);
  const DOCK_ORDER = dockOrderFor(role);

  useEffect(() => {
    const load = () => api.get<AppInfo[]>("/api/apps?role=admin")
      .then(setApps)
      .catch(() => setApps([]));
    load();
    // Settings → Apps & accounts turns an app on or off: show it now.
    window.addEventListener("yorik:apps-changed", load);
    return () => window.removeEventListener("yorik:apps-changed", load);
  }, []);

  // The "More" sheet closes when you leave the page or press Escape.
  useEffect(() => { setMoreOpen(false); }, [pathname]);
  useEffect(() => {
    if (!moreOpen) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setMoreOpen(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [moreOpen]);

  if (apps.length === 0) return null;

  const byId = Object.fromEntries([HOME_TILE, ...apps].map(a => [a.id, a]));
  const ordered = DOCK_ORDER.map(id => byId[id]).filter(Boolean);
  // A child's Dock is exactly KID_ORDER: no extras, no community apps.
  const extras = kid ? [] : apps.filter(a => a.bundled && !DOCK_ORDER.includes(a.id));
  const community = kid ? [] : apps.filter(a => !a.bundled && !DOCK_ORDER.includes(a.id));
  const builtins = [...ordered, ...extras];

  function handleClick(appId: string) {
    setMoreOpen(false);
    if (REACT_ROUTES[appId]) {
      navigate(REACT_ROUTES[appId]);
      return;
    }
    // Community apps (anything not in the bundled list) render inside
    // the React SPA via the generic CommunityApp host — keeps the modern
    // Dock + theme around the iframe instead of bouncing to the legacy
    // /<id> page.
    if (byId[appId] && !byId[appId].bundled) {
      navigate(`/community-app/${appId}`);
      return;
    }
    // Vanilla bundled app: hard navigate to root + the app's URL.
    // Special case: "docs" lives at /documents because FastAPI's
    // Swagger UI sits on /docs.
    window.location.href = VANILLA_PATHS[appId] || (appId === "home" ? "/" : `/${appId}`);
  }

  const tile = (a: AppInfo) => {
    const v = APP_VISUAL[a.id];
    const Icon = v?.Icon;
    const isActive = activeAppId === a.id;
    return (
      <button
        key={a.id}
        onClick={() => handleClick(a.id)}
        title={i18n.t(`apps.${a.id}`, { defaultValue: a.name })}
        aria-label={i18n.t(`apps.${a.id}`, { defaultValue: a.name })}
        className={cn(
          "relative inline-flex items-center justify-center w-12 h-12 rounded-2xl shrink-0",
          "transition-[transform,opacity,box-shadow] duration-150 ease-[cubic-bezier(.34,1.56,.64,1)]",
          "hover:-translate-y-1.5 hover:scale-110",
          "ring-1",
          v
            ? cn("bg-gradient-to-br", isActive ? v.activeGradient : v.gradient,
                 isActive ? v.activeRing : v.ring)
            : (isActive ? "bg-primary/15 ring-primary/40" : "bg-card/40 ring-border/40"),
        )}
      >
        {Icon ? (
          <Icon
            className={cn("w-[22px] h-[22px]", v?.text ?? "text-foreground/80")}
            strokeWidth={2.25}
            aria-hidden
          />
        ) : (
          <span className="text-2xl leading-none">{a.icon || "▣"}</span>
        )}
        {isActive && (
          <span className="absolute -bottom-1.5 left-1/2 -translate-x-1/2 w-1 h-1 rounded-full bg-primary shadow-[0_0_6px_var(--color-primary)]" />
        )}
      </button>
    );
  };

  if (phone) {
    // Phone: a tab bar flush with the bottom edge — four fixed tabs plus
    // "More", which lifts a sheet with every other app. Labels under the
    // glyphs, the active tab tinted; the bar pads itself for the home
    // indicator (safe-area-inset-bottom) so nothing sits under the gesture
    // zone. `--dock-clearance` in index.css matches this height on phones.
    const tabIds = phoneTabsFor(role);
    const tabs = tabIds.map(id => byId[id]).filter(Boolean) as AppInfo[];
    const rest = [...builtins, ...community].filter(a => !tabIds.includes(a.id));
    // Settings is not an "app" in /api/apps; Home shows it to grown-ups,
    // so the sheet does too.
    if (!kid && !byId.settings) rest.push({ id: "settings", name: "Settings", icon: "⚙️", bundled: true });
    const activeInMore = !tabIds.includes(activeAppId);
    const label = (a: AppInfo) => appName(a);

    const tabButton = (a: AppInfo) => {
      const v = APP_VISUAL[a.id];
      const Icon = v?.Icon;
      const isActive = activeAppId === a.id && !moreOpen;
      return (
        <button
          key={a.id}
          onClick={() => handleClick(a.id)}
          aria-label={label(a)}
          aria-current={isActive ? "page" : undefined}
          className={cn(
            "flex flex-col items-center justify-center gap-0.5 min-w-0",
            isActive ? "text-primary" : "text-muted-foreground",
          )}
        >
          <span className={cn("grid place-items-center w-11 h-7 rounded-xl transition-colors", isActive && "bg-primary/15")}>
            {Icon
              ? <Icon className="w-6 h-6" strokeWidth={isActive ? 2.25 : 1.8} aria-hidden />
              : <span className="text-xl leading-none">{a.icon || "▣"}</span>}
          </span>
          <span className="text-[10.5px] leading-none font-medium truncate max-w-full px-0.5">{label(a)}</span>
        </button>
      );
    };

    const sheetTile = (a: AppInfo) => {
      const v = APP_VISUAL[a.id];
      const Icon = v?.Icon;
      const isActive = activeAppId === a.id;
      return (
        <button
          key={a.id}
          onClick={() => handleClick(a.id)}
          className="flex flex-col items-center gap-1.5 min-w-0 text-foreground/90"
        >
          <span className={cn(
            "grid place-items-center w-12 h-12 rounded-2xl ring-1",
            v ? cn("bg-gradient-to-br", isActive ? v.activeGradient : v.gradient, isActive ? v.activeRing : v.ring)
              : "bg-card/60 ring-border/60",
          )}>
            {Icon
              ? <Icon className={cn("w-[22px] h-[22px]", v?.text ?? "text-foreground/80")} strokeWidth={2.25} aria-hidden />
              : <span className="text-2xl leading-none">{a.icon || "▣"}</span>}
          </span>
          <span className="text-[11px] leading-tight text-center truncate max-w-full">{label(a)}</span>
        </button>
      );
    };

    return (
      <>
        {moreOpen && (
          <div
            className="fixed inset-0 z-40 bg-black/40"
            onClick={() => setMoreOpen(false)}
            aria-hidden
          />
        )}
        {moreOpen && (
          <div
            role="dialog"
            aria-label={i18n.t("apps.all_apps")}
            className={cn(
              "fixed inset-x-0 z-50 bottom-[calc(3.5rem+env(safe-area-inset-bottom))]",
              "rounded-t-2xl border-t border-border bg-card shadow-[0_-12px_36px_rgba(0,0,0,0.45)]",
              "px-4 pt-2 pb-5 max-h-[70vh] overflow-y-auto",
            )}
          >
            <span className="block w-9 h-1 rounded-full bg-border mx-auto mb-3" aria-hidden />
            <p className="text-2xs font-semibold uppercase tracking-wider text-muted-foreground mb-3">{i18n.t("apps.all_apps")}</p>
            <div className="grid grid-cols-4 gap-x-2 gap-y-4">
              {rest.map(sheetTile)}
              <button
                onClick={() => { setMoreOpen(false); openHelp(); }}
                data-tour="help"
                className="flex flex-col items-center gap-1.5 min-w-0 text-foreground/90"
              >
                <span className="grid place-items-center w-12 h-12 rounded-2xl ring-1 ring-border/60 bg-card/60">
                  <CircleHelp className="w-[22px] h-[22px] text-muted-foreground" strokeWidth={2.25} aria-hidden />
                </span>
                <span className="text-[11px] leading-tight text-center">{i18n.t("apps.help")}</span>
              </button>
            </div>
          </div>
        )}
        <nav
          aria-label="App tabs"
          data-tour="dock"
          className={cn(
            "fixed inset-x-0 bottom-0 z-50 pb-[env(safe-area-inset-bottom)]",
            "border-t border-border bg-background/95 backdrop-blur-xl",
          )}
        >
          <div className="grid grid-cols-5 h-14">
            {tabs.map(tabButton)}
            <button
              onClick={() => setMoreOpen(o => !o)}
              aria-label={i18n.t("apps.more")}
              aria-expanded={moreOpen}
              className={cn(
                "flex flex-col items-center justify-center gap-0.5 min-w-0",
                moreOpen || activeInMore ? "text-primary" : "text-muted-foreground",
              )}
            >
              <span className={cn("grid place-items-center w-11 h-7 rounded-xl transition-colors", (moreOpen || activeInMore) && "bg-primary/15")}>
                <Grip className="w-6 h-6" strokeWidth={moreOpen || activeInMore ? 2.25 : 1.8} aria-hidden />
              </span>
              <span className="text-[10.5px] leading-none font-medium">{i18n.t("apps.more")}</span>
            </button>
          </div>
        </nav>
      </>
    );
  }

  return (
    <nav
      aria-label="App dock"
      data-tour="dock"
      className={cn(
        // bottom-3.5 keeps the desktop position. On iOS the home-
        // indicator gesture zone is ~34px; respect it by adding
        // safe-area-inset-bottom as extra offset when present.
        "fixed left-1/2 -translate-x-1/2 z-50",
        "bottom-[max(0.875rem,calc(env(safe-area-inset-bottom)+0.25rem))]",
        "inline-flex items-center gap-1.5 px-2 py-1.5",
        "rounded-2xl bg-card/80 border border-border backdrop-blur-xl",
        "shadow-[0_12px_36px_rgba(0,0,0,0.55),inset_0_1px_0_rgba(255,255,255,0.06)]",
        "max-w-[calc(100vw-1.5rem)] overflow-x-auto",
      )}
    >
      {builtins.map(tile)}
      {community.length > 0 && (
        <>
          <span className="w-px h-[30px] bg-border/40 mx-0.5 shrink-0" />
          {community.map(tile)}
        </>
      )}
      {/* Help for the app you're in, always the last tile. */}
      <span className="w-px h-[30px] bg-border/40 mx-0.5 shrink-0" />
      <button
        onClick={() => openHelp()}
        data-tour="help"
        title="Help"
        aria-label="Help"
        className="inline-flex items-center justify-center w-12 h-12 rounded-2xl shrink-0 ring-1 ring-border/60 bg-card/40 hover:-translate-y-1.5 hover:scale-110 transition-[transform] duration-150"
      >
        <CircleHelp className="w-[22px] h-[22px] text-muted-foreground" strokeWidth={2.25} aria-hidden />
      </button>
    </nav>
  );
}
