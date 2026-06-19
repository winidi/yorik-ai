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
 *   3. System status row — five chips: LLM · Email · Paperless · Backup
 *      · Numbering. Green = ready, amber = configured but degraded,
 *      red = broken, grey = not set up. Click → Settings or the right
 *      connector setup.
 *   4. Quick actions — "Write a letter" · "Create event" · "Find a
 *      document" — keyboard-discoverable jumps into the relevant app.
 *
 * Visual style matches the rest: same gradient backdrop, same card
 * surfaces, same tinted icon tiles.
 */

import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Loader2, Sparkles, Calendar, MessageSquare, FolderOpen, FilePlus,
  MessageCircle, Inbox, Newspaper, Hash, Settings as Cog,
  RefreshCw, Wifi, WifiOff, AlertCircle, CheckCircle2, Server,
  Mail, FileText, Database, ArrowRight, Plus, Search, Camera,
  ListTodo,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";
import { useAuth } from "@/components/AuthGate";
import { Dock } from "@/components/Dock";
import { WorkersStatus } from "@/components/WorkersStatus";
import { DemoDataPanel } from "@/components/DemoDataPanel";

interface SystemStatus {
  llm: { model: string; base_url: string; reachable: boolean };
  email: { configured: boolean; kinds: string[] };
  paperless: { admin_token_set: boolean; url: string | null };
  backup: { last: any; configured: boolean };
  counts: Record<string, number>;
  user: { name: string; role: string; language: string };
  configured_connectors: string[];
}

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
  { id: "documents", label: "Documents",  route: "/documents", icon: FolderOpen,    color: "from-amber-500/30 to-orange-500/30 text-amber-500",     blurb: "Your filing cabinet — search by meaning." },
  { id: "compose",   label: "Compose",    route: "/compose",   icon: FilePlus,      color: "from-rose-500/30 to-pink-500/30 text-rose-500",         blurb: "Write invoices, quotes, letters with AI." },
  { id: "whatsapp",  label: "WhatsApp",   route: "/whatsapp",  icon: MessageCircle, color: "from-emerald-500/30 to-green-500/30 text-emerald-500", blurb: "Chat replies drafted while you sleep.", optional: true },
  { id: "email",     label: "Email",      route: "/email",     icon: Inbox,         color: "from-sky-500/30 to-blue-500/30 text-sky-500",           blurb: "Inbox with AI triage and summaries." },
  { id: "photos",    label: "Photos",     route: "/photos",    icon: Camera,        color: "from-emerald-500/30 to-teal-500/30 text-emerald-500",   blurb: "Your photos and videos via Immich." },
  { id: "briefing",  label: "Briefing",   route: "/briefing",  icon: Newspaper,     color: "from-fuchsia-500/30 to-purple-500/30 text-fuchsia-500", blurb: "Your morning digest in one screen." },
  { id: "settings",  label: "Settings",   route: "/settings",  icon: Cog,           color: "from-slate-500/30 to-zinc-500/30 text-slate-500",       blurb: "Profile, connectors, quality, numbering." },
];


export function HomeApp() {
  const auth = useAuth();
  const navigate = useNavigate();
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [installedIds, setInstalledIds] = useState<Set<string> | null>(null);

  const refresh = useCallback(async () => {
    try {
      const s = await api.get<SystemStatus>("/api/system/status?role=admin");
      setStatus(s);
    } catch {
      // home stays usable even if status fails
    }
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

  useEffect(() => { refresh(); }, [refresh]);

  // Optional tiles (marked `optional: true` in APPS) are filtered by what
  // /api/apps reports as installed — keeps a fresh user from seeing a
  // WhatsApp tile they haven't enabled yet. Until /api/apps responds we
  // hide them, which is the better default than briefly flashing them.
  const visibleApps = APPS.filter(app =>
    !app.optional || (installedIds && installedIds.has(app.id)),
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
              <span className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground">Yorik · home</span>
            </div>
            <h1 className="text-2xl sm:text-3xl font-semibold leading-tight">
              {greeting}, <span className="bg-gradient-to-r from-violet-500 to-blue-500 bg-clip-text text-transparent">{firstName}</span>.
            </h1>
            <p className="text-sm text-muted-foreground mt-2 max-w-md leading-relaxed">
              Everything Yorik runs lives on this machine. Click an app, or just ask.
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
              <kbd className="text-[10px] px-1.5 py-0.5 rounded bg-background border border-border">
                {navigator.platform.includes("Mac") ? "⌘K" : "Ctrl K"}
              </kbd>
            </button>
            <button
              onClick={refresh}
              className="text-muted-foreground hover:text-foreground transition"
              title="Refresh status"
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

        <DemoDataPanel />

        {/* App grid */}
        <section className="mb-10">
          <h2 className="text-[11px] uppercase tracking-wider text-muted-foreground font-semibold mb-3">
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
                      "w-10 h-10 rounded-xl flex items-center justify-center bg-gradient-to-br",
                      app.color,
                    )}>
                      <app.icon className="w-5 h-5" />
                    </div>
                    {typeof count === "number" && (
                      <span className="text-[10px] tabular-nums px-2 py-0.5 rounded-full bg-muted/60 text-muted-foreground">
                        {count}
                      </span>
                    )}
                    {app.external && (
                      <span className="text-[10px] inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded-full bg-muted/60 text-muted-foreground">
                        ↗
                      </span>
                    )}
                  </div>
                  <div className="font-medium text-sm">{app.label}</div>
                  <div className="text-[11px] text-muted-foreground mt-0.5 line-clamp-2 leading-snug">
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

        {/* System status */}
        <section className="mb-10">
          <h2 className="text-[11px] uppercase tracking-wider text-muted-foreground font-semibold mb-3">
            System status
          </h2>
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-2.5">
            <StatusChip
              icon={Server}
              label="LLM"
              detail={status?.llm.model || "—"}
              tone={!status ? "loading" : status.llm.reachable ? "ok" : "error"}
              hint={status?.llm.reachable ? "Reachable" : "Unreachable"}
            />
            <StatusChip
              icon={Mail}
              label="Email"
              detail={status?.email.kinds?.[0] || "Not configured"}
              tone={!status ? "loading" : status.email.configured ? "ok" : "off"}
              onClick={() => navigate("/settings")}
              hint={status?.email.configured ? "Ready" : "Click to set up"}
            />
            <StatusChip
              icon={FileText}
              label="Paperless"
              detail={status?.paperless.admin_token_set ? "Linked" : "Not linked"}
              tone={!status ? "loading" : status?.paperless.admin_token_set ? "ok" : "off"}
              hint={status?.paperless.admin_token_set ? "Token set" : "Optional"}
            />
            <StatusChip
              icon={Database}
              label="Backup"
              detail={backupSummary(status)}
              tone={!status ? "loading" : status.backup.configured ? "ok" : "off"}
              hint={status?.backup.last?.finished_at || ""}
            />
            <StatusChip
              icon={Hash}
              label="Numbering"
              detail={status ? `${status.counts.numbering_series || 0} series` : "—"}
              tone={!status ? "loading" : (status.counts.numbering_series || 0) > 0 ? "ok" : "off"}
              onClick={() => navigate("/settings")}
              hint={(status?.counts.numbering_series || 0) > 0 ? "Configured" : "Optional"}
            />
          </div>
        </section>

        <WorkersStatus />

        {/* Quick actions */}
        <section>
          <h2 className="text-[11px] uppercase tracking-wider text-muted-foreground font-semibold mb-3">
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

type Tone = "ok" | "off" | "error" | "loading";

function StatusChip({ icon: Icon, label, detail, tone, hint, onClick }: {
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  detail: string;
  tone: Tone;
  hint?: string;
  onClick?: () => void;
}) {
  const dotClass =
    tone === "ok"      ? "bg-emerald-500" :
    tone === "error"   ? "bg-red-500"     :
    tone === "off"     ? "bg-muted-foreground/40" :
                         "bg-muted-foreground/30 animate-pulse";

  const Wrap = onClick ? "button" : "div" as any;
  return (
    <Wrap
      onClick={onClick}
      className={cn(
        "bg-card border border-border rounded-xl p-3 text-left transition",
        onClick && "hover:border-foreground/20 hover:shadow-md cursor-pointer w-full",
      )}
      title={hint}
    >
      <div className="flex items-center gap-2 mb-1.5">
        <Icon className="w-3.5 h-3.5 text-muted-foreground" />
        <span className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold flex-1">
          {label}
        </span>
        <span className={cn("w-2 h-2 rounded-full", dotClass)} />
      </div>
      <div className="text-sm font-medium truncate" title={detail}>{detail}</div>
    </Wrap>
  );
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

function backupSummary(s: SystemStatus | null): string {
  if (!s) return "—";
  if (!s.backup.configured) return "Not set up";
  if (!s.backup.last) return "No runs yet";
  const ts = s.backup.last?.finished_at || s.backup.last?.started_at;
  if (!ts) return "Configured";
  const d = new Date(ts.replace(" ", "T") + (ts.includes("T") ? "" : "Z"));
  if (isNaN(d.getTime())) return "Configured";
  const diffMin = (Date.now() - d.getTime()) / 60_000;
  if (diffMin < 60)        return `${Math.round(diffMin)}m ago`;
  if (diffMin < 60 * 24)   return `${Math.round(diffMin / 60)}h ago`;
  return `${Math.round(diffMin / (60 * 24))}d ago`;
}
