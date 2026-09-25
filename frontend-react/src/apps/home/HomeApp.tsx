/**
 * Yorik Home — the landing screen.
 *
 * Layout: full-width single-pane dashboard (no three-pane shell — this
 * IS the navigation hub, so wrapping it in another sidebar would be
 * redundant). Sections, top to bottom:
 *
 *   1. Greeting hero — time-of-day-aware, with the user's first name,
 *      single primary CTA "Ask Yorik anything" that jumps to chat.
 *   2. App grid — large tinted tiles, one per React app, with count
 *      chips ("12 events", "3 unpaid bills", "4 templates") so the
 *      home screen is informative at a glance.
 *   3. Health line — one sentence: "Everything is running", or what
 *      needs attention. The chips and workers behind it live in
 *      Settings → System (admin only), not in front of the family.
 *   4. Quick actions — "Write a letter" · "Create event" · "Find a
 *      document" — keyboard-discoverable jumps into the relevant app.
 *
 * Visual style matches the rest: same gradient backdrop, same card
 * surfaces, same tinted icon tiles.
 */

import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Sparkles, Calendar, MessageSquare, FolderOpen, FilePlus,
  MessageCircle, Inbox, Newspaper, Settings as Cog,
  RefreshCw, ArrowRight, Plus, Search, Camera, ListTodo, CircleHelp,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";
import { useAuth } from "@/components/AuthGate";
import { Dock, APP_VISUAL } from "@/components/Dock";
import { isKid, KID_ORDER } from "@/lib/dock-order";
import { DemoDataPanel } from "@/components/DemoDataPanel";
import { FamilyRow } from "@/components/FamilyRow";
import { openHelp } from "@/components/HelpPanel";
import { useHouseHealth, type SystemStatus, type HealthIssue } from "@/components/SystemStatusPanel";

// One bit of data we want to surface on each app tile, pulled from the
// status counts. Empty entry = no chip.
const TILE_COUNT_KEY: Record<string, keyof SystemStatus["counts"] | undefined> = {
  calendar:  "events",
  tasks:     "tasks",
  chat:      "conversations",
  documents: "documents",
  compose:   "templates",
};

interface AppTile {
  id: string;
  label: string;
  route: string;
  icon: React.ComponentType<{ className?: string }>;
  color: string;
  blurb: string;
  /** Render as an external link instead of an in-app navigation. */
  external?: boolean;
  /** Only show if /api/apps lists this id. Use for integrations that
   *  require a connected service (WhatsApp needs the Baileys bridge,
   *  etc.) so a fresh user doesn't see a broken-looking tile. */
  optional?: boolean;
}

const APPS: AppTile[] = [
  { id: "chat",      label: "Chat",       route: "/chat",      icon: MessageSquare, color: "from-violet-500/30 to-blue-500/30 text-violet-500",     blurb: "Ask Yorik anything in plain language." },
  { id: "calendar",  label: "Calendar",   route: "/calendar",  icon: Calendar,      color: "from-blue-500/30 to-cyan-500/30 text-blue-500",         blurb: "Events, tasks, drag-to-create blocks." },
  { id: "tasks",     label: "Tasks",      route: "/tasks",     icon: ListTodo,      color: "from-emerald-500/30 to-teal-500/30 text-emerald-500",   blurb: "Add, complete, and triage your to-dos." },
  { id: "documents", label: "Documents",  route: "/documents", icon: FolderOpen,    color: "from-amber-500/30 to-orange-500/30 text-amber-500",     blurb: "Letters, bills and papers, easy to find." },
  { id: "compose",   label: "Compose",    route: "/compose",   icon: FilePlus,      color: "from-rose-500/30 to-pink-500/30 text-rose-500",         blurb: "Write invoices, quotes, letters with AI." },
  { id: "whatsapp",  label: "WhatsApp",   route: "/whatsapp",  icon: MessageCircle, color: "from-emerald-500/30 to-green-500/30 text-emerald-500", blurb: "Chat replies drafted while you sleep.", optional: true },
  { id: "email",     label: "Email",      route: "/email",     icon: Inbox,         color: "from-sky-500/30 to-blue-500/30 text-sky-500",           blurb: "Your inbox, sorted and summarized." },
  { id: "photos",    label: "Photos",     route: "/photos",    icon: Camera,        color: "from-emerald-500/30 to-teal-500/30 text-emerald-500",   blurb: "The family's photos and videos." },
  { id: "briefing",  label: "Briefing",   route: "/briefing",  icon: Newspaper,     color: "from-fuchsia-500/30 to-purple-500/30 text-fuchsia-500", blurb: "Your morning digest in one screen." },
  { id: "settings",  label: "Settings",   route: "/settings",  icon: Cog,           color: "from-slate-500/30 to-zinc-500/30 text-slate-500",       blurb: "Your profile, family and connections." },
];


// Same gradient, glyph colour and ring as the app's Dock tile, so a
// person learns one picture per app. `color` is the fallback for an
// app the Dock doesn't know.
const DOCK_ID: Record<string, string> = { documents: "docs" };
function tileVisual(app: AppTile): string {
  const v = APP_VISUAL[DOCK_ID[app.id] || app.id];
  return v ? `${v.gradient} ${v.text} ${v.ring}` : app.color;
}

export function HomeApp() {
  const auth = useAuth();
  const navigate = useNavigate();
  const isAdmin = auth.user.role === "admin" || auth.user.role === "platform_admin";
  const health = useHouseHealth(isAdmin);
  const status: SystemStatus | null = health.status;
  const [loading, setLoading] = useState(true);
  const [installedIds, setInstalledIds] = useState<Set<string> | null>(null);

  const loadApps = useCallback(async () => {
    setLoading(true);
    try {
      const apps = await api.get<Array<{ id: string }>>("/api/apps?role=admin");
      setInstalledIds(new Set(apps.map(a => a.id)));
    } catch {
      // If /api/apps fails, fall back to showing the always-on tiles only.
      setInstalledIds(new Set());
    } finally {
      setLoading(false);
    }
  }, []);

  // The health hook loads itself on mount; the button reloads both.
  useEffect(() => { loadApps(); }, [loadApps]);
  const refresh = () => { health.refresh(); loadApps(); };

  // Optional tiles (marked `optional: true` in APPS) are filtered by what
  // /api/apps reports as installed — keeps a fresh user from seeing a
  // WhatsApp tile they haven't enabled yet. Until /api/apps responds we
  // hide them, which is the better default than briefly flashing them.
  const kid = isKid(auth.user.role);
  const visibleApps = APPS.filter(app =>
    (!app.optional || (installedIds && installedIds.has(app.id)))
    && (!kid || KID_ORDER.includes(DOCK_ID[app.id] || app.id)),
  );

  const firstName = (auth.user.name || "").split(" ")[0] || "there";
  const greeting = pickGreeting();

  return (
    <div className="h-screen overflow-y-auto bg-background text-foreground pb-24 home-bg">
      <div className="max-w-5xl mx-auto px-4 sm:px-8 pt-8 sm:pt-12 pb-8">
        {/* Hero */}
        <header className="flex items-start justify-between gap-6 mb-6 sm:mb-8">
          <div className="min-w-0">
            <div className="flex items-center gap-3 mb-2">
              <img
                src="/r/butler-mark.png"
                alt="Yorik"
                className="w-10 h-10 object-contain dark:invert"
              />
            </div>
            <h1 className="text-2xl sm:text-3xl font-semibold leading-tight">
              {greeting}, <span className="bg-gradient-to-r from-violet-500 to-blue-500 bg-clip-text text-transparent">{firstName}</span>.
            </h1>
            <p className="text-sm text-muted-foreground mt-2 max-w-md leading-relaxed">
              Everything stays at home. Pick an app, or just ask.
            </p>
          </div>

          <div className="flex items-center gap-3 shrink-0">
            <button
              onClick={() => {
                window.dispatchEvent(new KeyboardEvent("keydown", {
                  key: "k", ctrlKey: true, metaKey: navigator.platform.includes("Mac"),
                }));
              }}
              className="hidden sm:flex items-center gap-2 px-3 py-1.5 rounded-lg bg-muted/40 hover:bg-muted text-xs text-muted-foreground hover:text-foreground transition"
              title="Search everything — email, photos, documents, calendar"
            >
              <Search className="w-3.5 h-3.5" />
              <span>Search</span>
              <kbd className="text-2xs px-1.5 py-0.5 rounded bg-background border border-border">
                {navigator.platform.includes("Mac") ? "⌘K" : "Ctrl K"}
              </kbd>
            </button>
            <button
              onClick={() => openHelp()}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-muted/40 hover:bg-muted text-xs text-muted-foreground hover:text-foreground transition"
              title="Help: how Yorik works, step by step"
            >
              <CircleHelp className="w-3.5 h-3.5" />
              <span>Help</span>
            </button>
            <button
              onClick={refresh}
              className="text-muted-foreground hover:text-foreground transition"
              title="Refresh"
            >
              <RefreshCw className={cn("w-4 h-4", loading && "animate-spin")} />
            </button>
          </div>
        </header>

        {/* Big Ask CTA */}
        <button
          onClick={() => navigate("/chat")}
          className={cn(
            "w-full text-left bg-card border border-border rounded-2xl p-5 mb-8 transition group",
            "hover:border-violet-500/40 hover:shadow-lg",
          )}
        >
          <div className="flex items-center gap-4">
            <div className="w-12 h-12 rounded-xl bg-gradient-to-br from-violet-500 to-blue-500 text-white flex items-center justify-center shadow-md">
              <Sparkles className="w-5 h-5" />
            </div>
            <div className="flex-1 min-w-0">
              <div className="font-semibold mb-0.5">Ask Yorik anything</div>
              <div className="text-sm text-muted-foreground">
                "Schedule the dentist Friday at 2pm", "find my insurance policy", "draft a Mietminderung"…
              </div>
            </div>
            <ArrowRight className="w-4 h-4 text-muted-foreground group-hover:text-foreground transition shrink-0" />
          </div>
        </button>

        <FamilyRow />

        <DemoDataPanel />

        {/* App grid */}
        <section className="mb-10">
          <h2 className="text-sm font-semibold text-muted-foreground mb-3">
            Apps
          </h2>
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3">
            {visibleApps.map(app => {
              const countKey = TILE_COUNT_KEY[app.id];
              const count = countKey && status ? status.counts[countKey as string] : undefined;
              const tileBody = (
                <>
                  <div className="flex items-center justify-between mb-3">
                    <div className={cn(
                      "w-10 h-10 rounded-xl flex items-center justify-center bg-gradient-to-br ring-1",
                      tileVisual(app),
                    )}>
                      <app.icon className="w-5 h-5" />
                    </div>
                    {typeof count === "number" && (
                      <span className="text-2xs tabular-nums px-2 py-0.5 rounded-full bg-muted/60 text-muted-foreground">
                        {count}
                      </span>
                    )}
                    {app.external && (
                      <span className="text-2xs inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded-full bg-muted/60 text-muted-foreground">
                        ↗
                      </span>
                    )}
                  </div>
                  <div className="font-medium text-sm">{app.label}</div>
                  <div className="text-xs text-muted-foreground mt-0.5 line-clamp-2 leading-snug">
                    {app.blurb}
                  </div>
                </>
              );
              const tileClass = cn(
                "text-left p-4 rounded-xl bg-card border border-border transition group block",
                "hover:border-foreground/20 hover:shadow-md hover:-translate-y-0.5",
              );
              return app.external ? (
                <a
                  key={app.id}
                  href={app.route}
                  target="_blank"
                  rel="noopener noreferrer"
                  className={tileClass}
                >
                  {tileBody}
                </a>
              ) : (
                <button
                  key={app.id}
                  onClick={() => navigate(app.route)}
                  className={tileClass}
                >
                  {tileBody}
                </button>
              );
            })}
          </div>
        </section>

        {health.loaded && (
          <HealthLine
            issues={health.issues}
            isAdmin={isAdmin}
            onOpen={() => navigate("/settings?tab=system")}
          />
        )}

        {/* Quick actions */}
        <section>
          <h2 className="text-sm font-semibold text-muted-foreground mb-3">
            Quick actions
          </h2>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
            <QuickAction
              icon={FilePlus}
              label="Write a letter or invoice"
              onClick={() => navigate("/compose")}
            />
            <QuickAction
              icon={Plus}
              label="Add a calendar event"
              onClick={() => navigate("/calendar")}
            />
            <QuickAction
              icon={Search}
              label="Find a document"
              onClick={() => navigate("/documents")}
            />
          </div>
        </section>
      </div>

      <Dock activeAppId="home" />

      <style>{`
        .home-bg {
          background-image:
            radial-gradient(circle at 20% 10%, hsl(263 70% 60% / 0.07), transparent 50%),
            radial-gradient(circle at 80% 90%, hsl(200 70% 60% / 0.05), transparent 50%);
        }
      `}</style>
    </div>
  );
}

// ─── chips + helpers ──────────────────────────────────────────────────

/** One sentence instead of a dashboard. Admins always see it (green
 *  when all is well, so they know it was checked) and can click
 *  through to Settings → System; members only see it when Yorik
 *  itself can't answer, and without the link. */
function HealthLine({ issues, isAdmin, onOpen }: {
  issues: HealthIssue[];
  isAdmin: boolean;
  onOpen: () => void;
}) {
  const shown = isAdmin ? issues : issues.filter(i => i.tone === "error" && i.text.startsWith("Yorik"));
  if (!isAdmin && shown.length === 0) return null;
  const worst = shown.some(i => i.tone === "error") ? "error" : shown.length ? "warn" : "ok";
  const text =
    shown.length === 0 ? "Everything is running" :
    shown.length === 1 ? shown[0].text :
    `${shown.length} things need you: ${shown.map(i => i.text.charAt(0).toLowerCase() + i.text.slice(1)).join(", ")}`;
  const body = (
    <>
      <span className={cn(
        "w-2.5 h-2.5 rounded-full shrink-0",
        worst === "ok" ? "bg-emerald-500" : worst === "warn" ? "bg-amber-500" : "bg-red-500",
      )} />
      <span className="text-sm flex-1 min-w-0">{text}</span>
      {isAdmin && <ArrowRight className="w-3.5 h-3.5 text-muted-foreground group-hover:text-foreground transition shrink-0" />}
    </>
  );
  const cls = cn(
    "w-full mb-10 flex items-center gap-3 px-4 py-3 rounded-xl text-left transition",
    worst === "ok"   ? "bg-card border border-border" :
    worst === "warn" ? "bg-amber-500/10 border border-amber-500/20" :
                       "bg-red-500/10 border border-red-500/25",
  );
  return isAdmin
    ? <button onClick={onOpen} className={cn(cls, "group hover:border-foreground/20")} title="Open Settings → System">{body}</button>
    : <div className={cls}>{body}</div>;
}

function QuickAction({ icon: Icon, label, onClick }: {
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "bg-card border border-border rounded-xl px-4 py-3 text-left transition flex items-center gap-3",
        "hover:border-foreground/20 hover:shadow-md group",
      )}
    >
      <Icon className="w-4 h-4 text-muted-foreground group-hover:text-foreground transition" />
      <span className="text-sm font-medium flex-1">{label}</span>
      <ArrowRight className="w-3.5 h-3.5 text-muted-foreground group-hover:text-foreground transition" />
    </button>
  );
}

function pickGreeting(): string {
  const h = new Date().getHours();
  if (h < 5)  return "Up late";
  if (h < 12) return "Good morning";
  if (h < 17) return "Good afternoon";
  if (h < 22) return "Good evening";
  return "Good night";
}
