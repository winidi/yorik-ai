/**
 * Briefing app — day-tab UI built on the same template engine as before.
 *
 * Top bar: four time-period tabs (Yesterday / Today / Tomorrow / Today
 * recap). Below: a date navigator letting the user walk further back
 * (only into dates we have a saved snapshot for; can't browse into
 * the deep future for past tabs).
 *
 * The four tabs each map to a fixed briefing template (day-yesterday,
 * day-today, day-tomorrow, day-recap). When the user picks a past
 * date, the backend returns a snapshot if it has one — which is the
 * point of nightly /api/briefings/snapshots: yesterday's email may
 * have been categorised differently today, but the snapshot preserves
 * what the day actually felt like.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  RefreshCw, Newspaper, Sparkles, AlertCircle,
  ChevronLeft, ChevronRight, Sun,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";
import { Dock } from "@/components/Dock";

type Period = "yesterday" | "today" | "tomorrow" | "recap";

interface SectionResult {
  id: string;
  title: string;
  icon?: string;
  render: "markdown" | "list" | "events_list" | "card" | "metric" | "raw_json";
  priority?: "high" | "normal";
  ok: boolean;
  hidden?: boolean;
  error?: string;
  result?: any;
  lines?: string[];
}

interface BriefingResult {
  template: { id: string; name: string; description: string };
  sections: SectionResult[];
  synthesis?: string;
  generated_at: string;
  summary_line?: string;
  period?: Period;
  target_date?: string;
  _snapshot?: { generated_at: string };
}

const TAB_LABELS: Record<Period, string> = {
  yesterday: "Yesterday",
  today:     "Today",
  tomorrow:  "Tomorrow",
  recap:     "Recap",
};

function pickDefaultPeriod(): Period {
  const stored = localStorage.getItem("yorik_briefing_period") as Period | null;
  if (stored && stored in TAB_LABELS) return stored;
  const h = new Date().getHours();
  if (h >= 21 || h < 5) return "tomorrow";   // late night → look ahead
  if (h >= 17)          return "recap";       // evening → today recap
  return "today";                              // morning + midday default
}

function isoToday(): string {
  return new Date().toISOString().slice(0, 10);
}

function shiftDate(iso: string, deltaDays: number): string {
  const d = new Date(iso + "T12:00:00");
  d.setDate(d.getDate() + deltaDays);
  return d.toISOString().slice(0, 10);
}

function formatDateLabel(iso: string): string {
  const d = new Date(iso + "T12:00:00");
  return d.toLocaleDateString(undefined, {
    weekday: "short", year: "numeric", month: "short", day: "numeric",
  });
}

export function BriefingApp() {
  const [period, setPeriod] = useState<Period>(pickDefaultPeriod);
  // `dateOffset` lets the user navigate further back than the default
  // for yesterday/recap (e.g. day before yesterday). For today/tomorrow
  // it's pinned to 0 — those tabs are about right-now.
  const [dateOffset, setDateOffset] = useState(0);
  const [snapshotDates, setSnapshotDates] = useState<Set<string>>(new Set());
  const [data, setData] = useState<BriefingResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Resolve the actual target date from the selected period + offset.
  const targetDate = useMemo(() => {
    const today = isoToday();
    if (period === "today" || period === "recap") return shiftDate(today, dateOffset);
    if (period === "yesterday") return shiftDate(today, -1 + dateOffset);
    if (period === "tomorrow")  return shiftDate(today, 1);
    return today;
  }, [period, dateOffset]);

  // Snapshot date list — pulled once to know which past days are
  // browsable. Frontend doesn't have to wait for it to render the
  // current tab.
  useEffect(() => {
    api.get<{ dates: string[] }>("/api/briefings/snapshots/dates")
      .then(r => setSnapshotDates(new Set(r.dates)))
      .catch(() => { /* non-fatal — disables the "older" arrow */ });
  }, []);

  const refresh = useCallback(async (force = false) => {
    setLoading(true);
    setError(null);
    try {
      const url = `/api/briefings/day?period=${period}&date=${targetDate}`
                + (force ? "&force=1" : "");
      const r = await api.get<BriefingResult>(url);
      setData(r);
    } catch (e: any) {
      setError(e?.message || "briefing failed");
      setData(null);
    } finally {
      setLoading(false);
    }
  }, [period, targetDate]);

  // Auto-refresh on tab/date change uses the cache. The RefreshCw
  // button always bypasses it (see onClick below) so the user has an
  // explicit "I want fresh data right now" path.
  useEffect(() => { refresh(); }, [refresh]);
  useEffect(() => {
    localStorage.setItem("yorik_briefing_period", period);
  }, [period]);

  // Reset offset when switching to a tab where it doesn't apply.
  function switchPeriod(next: Period) {
    setPeriod(next);
    if (next !== "yesterday" && next !== "recap") setDateOffset(0);
  }

  // Date-nav arrows. Only enabled on yesterday/recap.
  const canGoBack = (period === "yesterday" || period === "recap")
                 && snapshotDates.has(shiftDate(targetDate, -1));
  const canGoForward = (period === "yesterday" || period === "recap") && dateOffset < 0;

  const showDateNav = period === "yesterday" || period === "recap";
  const isSnapshot = !!data?._snapshot;

  return (
    <div className="h-screen overflow-y-auto bg-background text-foreground pb-[max(5rem,env(safe-area-inset-bottom)+4rem)]">
      {/* Header */}
      <header className="border-b border-border bg-card/40 backdrop-blur-sm sticky top-0 z-10">
        <div className="max-w-4xl mx-auto px-4 sm:px-6 h-14 flex items-center gap-3">
          <Newspaper className="w-5 h-5 text-primary shrink-0" />
          <div className="font-semibold">Briefing</div>
          <div className="flex-1" />
          <button
            onClick={() => refresh(true)}
            disabled={loading}
            className="p-2.5 md:p-2 rounded-md hover:bg-muted text-muted-foreground"
            title="Refresh (bypass cache)"
          >
            <RefreshCw className={cn("w-5 h-5 md:w-4 md:h-4", loading && "animate-spin")} />
          </button>
        </div>
        {/* Tab bar */}
        <div className="max-w-4xl mx-auto px-2 sm:px-4 pb-2 flex gap-1 overflow-x-auto snap-x snap-mandatory">
          {(Object.keys(TAB_LABELS) as Period[]).map(p => (
            <button
              key={p}
              onClick={() => switchPeriod(p)}
              className={cn(
                "snap-start px-3 py-2 md:py-1.5 rounded-md text-sm font-medium transition whitespace-nowrap",
                period === p
                  ? "bg-primary text-primary-foreground"
                  : "text-muted-foreground hover:bg-muted hover:text-foreground",
              )}
            >
              {TAB_LABELS[p]}
            </button>
          ))}
        </div>
        {/* Date navigator — lives inside the sticky header so the
         * date follows the user as they scroll long briefings. Only
         * shown on yesterday/recap tabs (the others are about now). */}
        {showDateNav && (
          <div className="max-w-4xl mx-auto px-4 sm:px-6 pb-2 pt-2 border-t border-border/40 flex items-center gap-3">
            <button
              onClick={() => setDateOffset(o => o - 1)}
              disabled={!canGoBack}
              className="p-2.5 md:p-1.5 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition disabled:opacity-30 disabled:cursor-not-allowed"
              title="Older"
            >
              <ChevronLeft className="w-5 h-5 md:w-4 md:h-4" />
            </button>
            <div className="text-sm font-medium tabular-nums">{formatDateLabel(targetDate)}</div>
            {isSnapshot && (
              <span className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded-full bg-amber-500/10 text-amber-600 dark:text-amber-400">
                snapshot
              </span>
            )}
            <button
              onClick={() => setDateOffset(o => o + 1)}
              disabled={!canGoForward}
              className="p-2.5 md:p-1.5 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition disabled:opacity-30 disabled:cursor-not-allowed"
              title="Newer"
            >
              <ChevronRight className="w-5 h-5 md:w-4 md:h-4" />
            </button>
          </div>
        )}
      </header>

      {/* Body */}
      <main className="max-w-4xl mx-auto p-6 pb-24">
        {error && (
          <div className="flex gap-2 p-4 bg-destructive/10 border border-destructive/30 rounded-md text-sm mb-4">
            <AlertCircle className="w-4 h-4 text-destructive shrink-0 mt-0.5" />
            <span className="text-destructive">{error}</span>
          </div>
        )}

        {loading && !data && (
          <div className="space-y-4">
            {[1,2,3].map(i => (
              <div key={i} className="h-32 bg-muted/40 rounded-lg animate-pulse" />
            ))}
          </div>
        )}

        {data?.summary_line && (
          <div className="mb-5 text-sm sm:text-base leading-snug text-muted-foreground">
            <span className="text-foreground font-medium">{data.summary_line}</span>
          </div>
        )}

        {data?.synthesis && (
          <div className="mb-4 md:mb-6 p-5 rounded-xl bg-gradient-to-br from-primary/10 to-primary/5 border border-primary/20">
            <div className="flex items-center gap-2 mb-2 text-xs uppercase tracking-wider text-primary font-semibold">
              <Sparkles className="w-3.5 h-3.5" />
              Briefing
            </div>
            <div className="text-sm leading-relaxed whitespace-pre-line">{data.synthesis}</div>
          </div>
        )}

        {data?.sections?.filter(s => !s.hidden).map(s => (
          <SectionCard key={s.id} section={s} />
        ))}

        {/* All-empty fallback: every section either returned nothing or
            had its condition evaluate false, AND there's no synthesis
            and no summary_line. Without this the user sees just the
            "Generated …" footer and thinks the briefing is broken.
            Most-common trigger: fresh install with no events / tasks /
            bills / emails — quiet, not broken.
            Defensive `?? []` for installs where the backend ships
            a degraded payload without a sections key. */}
        {data
          && (data.sections ?? []).filter(s => !s.hidden).length === 0
          && !data.synthesis
          && !data.summary_line
          && !error
          && (
            <div className="text-center py-16">
              <div className="w-12 h-12 mx-auto mb-3 rounded-full bg-amber-500/10 flex items-center justify-center">
                <Sun className="w-5 h-5 text-amber-500" />
              </div>
              <div className="text-base font-medium">Quiet day</div>
              <div className="text-sm text-muted-foreground mt-1 max-w-md mx-auto">
                Nothing on your calendar, no overdue tasks, no bills due, no new email.
                Yorik will surface anything new automatically.
              </div>
            </div>
          )}

        {data && (
          <div className="mt-6 text-xs text-center text-muted-foreground">
            Generated {new Date(data.generated_at).toLocaleString()}
          </div>
        )}
      </main>

      <Dock activeAppId="briefing" />
    </div>
  );
}

// ───────────────────────── section renderers ────────────────────────

function SectionCard({ section }: { section: SectionResult }) {
  if (!section.ok) {
    return (
      <div className="p-4 rounded-lg border border-destructive/30 bg-destructive/5 mb-4">
        <div className="text-xs uppercase tracking-wider text-destructive font-semibold mb-1">
          {section.icon} {section.title}
        </div>
        <div className="text-xs text-muted-foreground">Error: {section.error}</div>
      </div>
    );
  }
  return (
    <div className={cn(
      "p-5 rounded-lg border border-border bg-card mb-4",
      section.priority === "high" && "border-primary/40 shadow-sm",
    )}>
      <div className="flex items-center gap-2 mb-3">
        <span className="text-lg">{section.icon}</span>
        <h3 className="font-semibold">{section.title}</h3>
      </div>
      <RenderSection section={section} />
    </div>
  );
}

function RenderSection({ section }: { section: SectionResult }) {
  // Shape-based renderers — fire regardless of the declared render
  // type because "raw_json" is the catch-all template authors pick
  // when no special renderer fits. Order matters: more-specific
  // detectors first.

  // find_photo → thumbnail grid
  const photos = section.result?.photos;
  if (Array.isArray(photos) && photos.length > 0 && photos[0]?.thumbnail_url) {
    return <PhotoGrid photos={photos} />;
  }
  if (Array.isArray(photos) && photos.length === 0) {
    return <p className="text-sm text-muted-foreground italic">No photos.</p>;
  }

  // check_tasks → checkbox list with inline complete (Layer 2 will
  // wire the PATCH; for now the checkbox is read-only display).
  const tasks = section.result?.tasks;
  if (Array.isArray(tasks)) {
    if (tasks.length === 0) {
      return <p className="text-sm text-muted-foreground italic">No tasks.</p>;
    }
    return <TaskList tasks={tasks} />;
  }

  // bills_due payload (we shape it server-side; see template).
  const bills = section.result?.bills;
  if (Array.isArray(bills)) {
    if (bills.length === 0) {
      return <p className="text-sm text-muted-foreground italic">No bills due.</p>;
    }
    return <BillsList bills={bills} />;
  }
  if (section.render === "markdown") {
    const text = section.result?.summary || (typeof section.result === "string" ? section.result : "");
    // Email + WhatsApp briefings attach a structured array of items
    // that need a reply. Pass them through to MarkdownBlock so the
    // **bold sender names** in the LLM prose become clickable links
    // straight to the source thread/chat — no separate "Jump to ..."
    // list needed, the prose IS the action list.
    const emailThreads = Array.isArray(section.result?.threads_needing_reply)
      ? section.result.threads_needing_reply : [];
    const waChats = Array.isArray(section.result?.chats_needing_reply)
      ? section.result.chats_needing_reply : [];
    return <MarkdownBlock md={text} emailThreads={emailThreads} waChats={waChats} />;
  }
  if (section.render === "list") {
    const lines = section.lines || [];
    if (lines.length === 0) return <p className="text-sm text-muted-foreground italic">Nothing to show.</p>;
    return (
      <ul className="space-y-1.5 text-sm">
        {lines.map((l, i) => (
          <li key={i} className="flex items-baseline gap-2">
            <span className="text-muted-foreground">•</span>
            <span>{l}</span>
          </li>
        ))}
      </ul>
    );
  }
  if (section.render === "events_list") {
    const events = section.result?.events || [];
    const freeSlots = section.result?.free_slots || [];
    if (events.length === 0 && freeSlots.length === 0) {
      return <p className="text-sm text-muted-foreground italic">Nothing on the calendar.</p>;
    }
    return (
      <>
        {events.length > 0 && (
          <ul className="space-y-1 text-sm">
            {events.map((e: any) => (
              <li key={e.id}>
                <a
                  href={`/r/calendar?date=${e.date || (e.starts_at || "").slice(0, 10)}`}
                  className="flex items-baseline gap-3 -mx-2 px-2 py-2.5 md:py-1 rounded hover:bg-muted/40 group"
                >
                  <span className="text-muted-foreground tabular-nums text-xs shrink-0 w-12">{e.time}</span>
                  <span className="flex-1 group-hover:text-foreground">{e.title}</span>
                  {e.who && <span className="text-xs text-muted-foreground">{e.who}</span>}
                </a>
              </li>
            ))}
          </ul>
        )}
        {freeSlots.length > 0 && (
          <div className="mt-3 pt-3 border-t border-border/40 text-xs text-muted-foreground">
            <div className="font-semibold mb-1.5 uppercase tracking-wider">Free slots</div>
            <div className="flex flex-wrap gap-1">
              {freeSlots.slice(0, 6).map((s: any, i: number) => (
                <a
                  key={i}
                  href={`/r/calendar?date=${s.date}`}
                  className="px-2.5 py-1 md:px-1.5 md:py-0.5 bg-muted rounded text-xs md:text-[11px] hover:bg-primary/15 hover:text-foreground transition"
                >
                  {s.date.slice(5)} · {s.start}–{s.end}
                </a>
              ))}
            </div>
          </div>
        )}
      </>
    );
  }
  if (section.render === "card") {
    return <pre className="text-xs whitespace-pre-wrap">{JSON.stringify(section.result, null, 2)}</pre>;
  }
  if (section.render === "metric") {
    return <div className="text-3xl font-bold">{String(section.result)}</div>;
  }
  return <pre className="text-xs whitespace-pre-wrap text-muted-foreground">{JSON.stringify(section.result, null, 2)}</pre>;
}

// Tasks list with date label + checkbox. Click → deep-link to /r/tasks
// at that task (Tasks app already scrolls + flashes on ?task=ID).
function TaskList({ tasks }: { tasks: any[] }) {
  return (
    <ul className="space-y-1.5 text-sm">
      {tasks.map(t => (
        <li key={t.id}>
          <a
            href={`/r/tasks?task=${t.id}`}
            className="flex items-center gap-2.5 group hover:bg-muted/40 -mx-2 px-2 py-2.5 md:py-1 rounded"
          >
            <span
              className={cn(
                "w-5 h-5 md:w-4 md:h-4 rounded border-2 shrink-0 flex items-center justify-center",
                t.done ? "bg-primary border-primary" : "border-muted-foreground/40 group-hover:border-primary/60",
              )}
              aria-hidden="true"
            >
              {t.done && <span className="text-[10px] text-primary-foreground">✓</span>}
            </span>
            <span className={cn("flex-1", t.done && "line-through text-muted-foreground")}>
              {t.title}
            </span>
            {t.due_date && (
              <span className="text-xs text-muted-foreground tabular-nums shrink-0">
                {formatDueDateShort(t.due_date)}
              </span>
            )}
            {t.priority >= 2 && !t.done && (
              <span className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded bg-rose-500/10 text-rose-600 dark:text-rose-400 shrink-0">
                !
              </span>
            )}
          </a>
        </li>
      ))}
    </ul>
  );
}

function formatDueDateShort(iso: string): string {
  const d = new Date(iso + "T12:00:00");
  if (isNaN(d.getTime())) return iso;
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const diff = Math.round((d.getTime() - today.getTime()) / (24 * 3600 * 1000));
  if (diff === 0) return "today";
  if (diff === 1) return "tomorrow";
  if (diff === -1) return "yesterday";
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

// Bills list — name + amount + due date. Click → if the bill was
// auto-imported from email, jump to the source thread. Otherwise
// land on /r/email which is the closest "bill management" surface
// we have today (the home-screen bill detail modal lives on home,
// not here; lifting it into a shared component is Layer-2 work).
function BillsList({ bills }: { bills: any[] }) {
  return (
    <ul className="space-y-1 text-sm">
      {bills.map(b => {
        const href = b.email_message_id
          ? `/r/email?msg=${encodeURIComponent(b.email_message_id)}`
          : "/r/email";
        return (
          <li key={b.id}>
            <a
              href={href}
              className="flex items-center gap-3 -mx-2 px-2 py-2.5 md:py-1 rounded hover:bg-muted/40 group"
            >
              <span className="flex-1 truncate group-hover:text-foreground">{b.name}</span>
              <span className="tabular-nums text-sm font-medium shrink-0">
                {Number(b.amount).toFixed(2)} {b.currency}
              </span>
              <span className="text-xs text-muted-foreground tabular-nums shrink-0">
                {formatDueDateShort(b.due_date)}
              </span>
            </a>
          </li>
        );
      })}
    </ul>
  );
}

// Compact "jump to..." chip-list used under the email/whatsapp markdown.
// The LLM-rendered prose tells the user WHAT happened; this list lets
// them click straight into the source.
function ActionList({ title, items }: {
  title: string;
  items: { key: string; primary: string; secondary?: string; href: string }[];
}) {
  if (items.length === 0) return null;
  return (
    <div className="mt-3 pt-3 border-t border-border/40">
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-1.5">
        {title}
      </div>
      <ul className="space-y-0.5">
        {items.slice(0, 8).map(it => (
          <li key={it.key}>
            <a
              href={it.href}
              className="flex items-baseline gap-2 -mx-2 px-2 py-2.5 md:py-1 rounded hover:bg-muted/40 group text-sm"
            >
              <span className="flex-1 truncate group-hover:text-foreground">{it.primary}</span>
              {it.secondary && (
                <span className="text-xs text-muted-foreground shrink-0">{it.secondary}</span>
              )}
            </a>
          </li>
        ))}
      </ul>
    </div>
  );
}

// Thumbnail grid for find_photo results. Clicks deep-link into the
// Photos app at the specific asset (matches the chat lightbox pattern).
function PhotoGrid({ photos }: { photos: any[] }) {
  return (
    <div className="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-6 gap-2 md:gap-1.5">
      {photos.map(p => (
        <a
          key={p.id}
          href={`/r/photos?asset=${encodeURIComponent(p.id)}`}
          title={p.original_name || ""}
          className="aspect-square overflow-hidden rounded-md border border-border bg-muted hover:border-primary/40 hover:shadow-md transition"
        >
          <img
            src={p.thumbnail_url}
            alt=""
            loading="lazy"
            className="w-full h-full object-cover"
          />
        </a>
      ))}
    </div>
  );
}

// Minimal markdown: **bold**, ## headers, - lists, single newlines as breaks.
// Optional `emailThreads` / `waChats` arrays turn matching **bold** sender
// names INTO links straight to the source thread/chat. Matching is
// case-insensitive prefix-aware so "Bob" in markdown matches "Bob Smith"
// in the threads array. The bold formatting stays — we just wrap it
// with an anchor. The LLM is prompted to put sender names in bold, so
// this lights up the natural call-to-action in the prose without
// needing a separate jump-list.
function MarkdownBlock({ md, emailThreads = [], waChats = [] }: {
  md: string;
  emailThreads?: any[];
  waChats?: any[];
}) {
  if (!md) return <p className="text-sm text-muted-foreground italic">(empty)</p>;

  // Build lookup tables: lowercased name → href. We try multiple name
  // shapes per thread/chat so we catch nicknames + first-name-only.
  const linkMap: Array<[string, string]> = [];
  function add(name: string | undefined | null, href: string) {
    const n = (name || "").trim();
    if (!n) return;
    linkMap.push([n.toLowerCase(), href]);
    // Also index by first word — LLM often shortens "Bob Smith" → "Bob".
    const first = n.split(/\s+/)[0];
    if (first && first.length >= 2 && first.toLowerCase() !== n.toLowerCase()) {
      linkMap.push([first.toLowerCase(), href]);
    }
  }
  for (const t of emailThreads) {
    if (!t?.id) continue;
    const href = `/r/email?msg=${encodeURIComponent(String(t.id))}`;
    add(t.from, href);
    add(t.from_name, href);
    add((t.from_email || "").split("@")[0], href);
  }
  for (const c of waChats) {
    if (!c?.jid) continue;
    const href = `/r/whatsapp?chat=${encodeURIComponent(c.jid)}`;
    add(c.name, href);
    add(c.jid.split("@")[0], href);
  }
  // Sort longest-name-first so "Bob Smith" wins over "Bob" when both apply.
  linkMap.sort((a, b) => b[0].length - a[0].length);

  function maybeLink(boldText: string): string {
    const k = boldText.toLowerCase().trim();
    for (const [needle, href] of linkMap) {
      if (k === needle || k.startsWith(needle + " ") || k.endsWith(" " + needle)) {
        return `<a href="${href}" class="text-primary underline underline-offset-2 md:no-underline md:hover:underline"><strong>${boldText}</strong></a>`;
      }
    }
    return `<strong>${boldText}</strong>`;
  }

  const html = escape(md)
    .replace(/^##\s+(.+)$/gm, "<h4 class='font-semibold text-primary mt-3 mb-1'>$1</h4>")
    .replace(/\*\*([^*]+)\*\*/g, (_m, inner) => maybeLink(inner))
    .split("\n").map(line =>
      /^\s*-\s+/.test(line)
        ? `<li class='ml-4 list-disc text-sm'>${line.replace(/^\s*-\s+/, "")}</li>`
        : line.trim() ? `<p class='text-sm leading-relaxed my-1'>${line}</p>` : ""
    ).join("");
  return <div className="space-y-1" dangerouslySetInnerHTML={{ __html: html }} />;
}

function escape(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
