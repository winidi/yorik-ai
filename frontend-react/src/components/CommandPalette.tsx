/**
 * Universal search palette — ⌘K / Ctrl+K from anywhere in the React shell.
 *
 * Fetches /api/search as you type (debounced 200ms), renders grouped
 * results, keyboard-navigable, click/Enter to navigate. Mounted via
 * a portal at document body so position is independent of where it's
 * rendered in the component tree.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  Search, Mail, MessageSquare, FileText, Image as ImageIcon,
  Calendar, X, Loader2, ArrowRight, CheckSquare, Users, Mic, PenLine, Landmark,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";
import { locale } from "@/i18n";

interface SearchHit {
  source: "email" | "whatsapp" | "paperless" | "immich" | "calendar"
        | "tasks" | "contacts" | "recordings" | "drafts";
  id: number | string;
  title: string;
  subtitle?: string;
  snippet?: string;
  timestamp?: string | number;
  navigate_to: string;
  thumbnail_url?: string | null;
}

interface SearchResponse {
  query: string;
  total: number;
  results: Record<string, SearchHit[]>;
}

// `labelKey` is resolved at render (sourceLabel) so the group heading
// follows the person's language.
const SOURCE_META: Record<string, { labelKey: string; icon: any; tint: string }> = {
  email:     { labelKey: "apps.email",     icon: Mail,         tint: "text-blue-500" },
  whatsapp:  { labelKey: "apps.whatsapp",  icon: MessageSquare, tint: "text-emerald-500" },
  paperless: { labelKey: "apps.docs",      icon: FileText,     tint: "text-amber-500" },
  immich:    { labelKey: "apps.photos",    icon: ImageIcon,    tint: "text-pink-500" },
  calendar:  { labelKey: "apps.calendar",  icon: Calendar,     tint: "text-violet-500" },
  tasks:     { labelKey: "apps.tasks",     icon: CheckSquare,  tint: "text-teal-500" },
  contacts:  { labelKey: "apps.contacts",  icon: Users,        tint: "text-orange-500" },
  recordings: { labelKey: "apps.recordings", icon: Mic,        tint: "text-rose-500" },
  drafts:    { labelKey: "apps.search.source.drafts", icon: PenLine, tint: "text-sky-500" },
  letters:   { labelKey: "apps.write",     icon: PenLine,      tint: "text-sky-500" },
  bank:      { labelKey: "apps.search.source.bank", icon: Landmark, tint: "text-lime-600" },
  pipelines: { labelKey: "apps.search.source.pipelines", icon: ArrowRight, tint: "text-indigo-500" },
};

const SOURCE_ORDER: Array<keyof typeof SOURCE_META> = [
  "calendar", "tasks", "email", "whatsapp", "paperless", "contacts",
  "recordings", "drafts", "letters", "bank", "pipelines", "immich",
];

/** How well a hit matches the words typed: a word as a whole word in
 *  the title counts most, inside the title less, in the snippet least. */
function matchScore(hit: SearchHit, words: string[]): number {
  const title = (hit.title || "").toLowerCase();
  const titleWords = new Set(title.split(/[^\p{L}\p{N}]+/u));
  const rest = `${hit.subtitle || ""} ${hit.snippet || ""}`.toLowerCase();
  let score = 0;
  for (const w of words) {
    if (titleWords.has(w)) score += 3;
    else if (title.includes(w)) score += 2;
    if (rest.includes(w)) score += 1;
  }
  return score;
}

/** Sources with the best-matching hit first — "kobra vertrag" put the
 *  Kobra document under five Vodafone mails (2026-09-29). The fixed
 *  order only breaks ties. */
function orderedSources(data: SearchResponse | null, query: string): Array<keyof typeof SOURCE_META> {
  if (!data) return SOURCE_ORDER;
  const words = query.toLowerCase().split(/\s+/).filter(w => w.length > 1);
  const best = (k: keyof typeof SOURCE_META) =>
    Math.max(0, ...(data.results[k] || []).map(h => matchScore(h, words)));
  return [...SOURCE_ORDER].sort((a, b) => best(b) - best(a));
}

export function CommandPalette() {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [data, setData] = useState<SearchResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [activeIdx, setActiveIdx] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  // Global keyboard listener for ⌘K / Ctrl+K. Always mounted.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen(o => !o);
      } else if (e.key === "Escape" && open) {
        e.preventDefault();
        setOpen(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  // Focus the input when the palette opens.
  useEffect(() => {
    if (open) {
      setActiveIdx(0);
      setTimeout(() => inputRef.current?.focus(), 30);
    } else {
      setQuery("");
      setData(null);
    }
  }, [open]);

  // Debounced fetch on query change.
  useEffect(() => {
    if (!open) return;
    const q = query.trim();
    if (q.length < 2) { setData(null); return; }
    let cancelled = false;
    setLoading(true);
    const t = setTimeout(async () => {
      try {
        const res = await api.get<SearchResponse>(`/api/search?q=${encodeURIComponent(q)}`);
        if (!cancelled) { setData(res); setActiveIdx(0); }
      } catch {
        if (!cancelled) setData(null);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }, 200);
    return () => { cancelled = true; clearTimeout(t); };
  }, [query, open]);

  // Flat ordered list for keyboard nav.
  const flat = useMemo(() => {
    if (!data) return [];
    const out: SearchHit[] = [];
    for (const k of orderedSources(data, query)) (data.results[k] || []).forEach(h => out.push(h));
    return out;
  }, [data, query]);
  const sources = useMemo(() => orderedSources(data, query), [data, query]);

  const navigate = useCallback((hit: SearchHit) => {
    setOpen(false);
    // Same-shell React routes use react-router's history (relative);
    // anything else (vanilla routes / external Paperless / Immich) gets
    // a hard navigate.
    if (hit.navigate_to.startsWith("/r/")) {
      window.location.assign(hit.navigate_to);
    } else if (hit.navigate_to.startsWith("http")) {
      window.open(hit.navigate_to, "_blank", "noreferrer");
    } else {
      window.location.assign(hit.navigate_to);
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "ArrowDown") { e.preventDefault(); setActiveIdx(i => Math.min(i + 1, flat.length - 1)); }
      else if (e.key === "ArrowUp") { e.preventDefault(); setActiveIdx(i => Math.max(i - 1, 0)); }
      else if (e.key === "Enter" && flat[activeIdx]) {
        e.preventDefault();
        navigate(flat[activeIdx]);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, flat, activeIdx, navigate]);

  if (!open) return null;

  // Render via portal so we're always above everything.
  return createPortal(
    <div
      className="fixed inset-0 z-[1000] flex items-start justify-center pt-[10vh] px-4 bg-black/50 backdrop-blur-sm"
      onClick={() => setOpen(false)}
    >
      <div
        className="w-full max-w-2xl bg-card border border-border rounded-xl shadow-2xl overflow-hidden flex flex-col max-h-[70vh]"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center gap-3 px-4 h-14 border-b border-border">
          <Search className="w-5 h-5 text-muted-foreground shrink-0" />
          <input
            ref={inputRef}
            value={query}
            onChange={e => setQuery(e.target.value)}
            placeholder={t("apps.search.placeholder")}
            className="flex-1 h-9 bg-transparent text-base placeholder:text-muted-foreground focus:outline-none"
          />
          {loading && <Loader2 className="w-4 h-4 text-muted-foreground animate-spin" />}
          <button onClick={() => setOpen(false)} className="text-muted-foreground hover:text-foreground text-xs flex items-center gap-1">
            ESC <X className="w-3 h-3" />
          </button>
        </div>

        <div className="overflow-y-auto flex-1">
          {query.trim().length < 2 ? (
            <Hint />
          ) : !data ? (
            <div className="p-8 text-center text-sm text-muted-foreground">
              {loading ? t("apps.search.searching") : t("apps.search.noResultsYet")}
            </div>
          ) : data.total === 0 ? (
            <div className="p-8 text-center text-sm text-muted-foreground">
              {t("apps.search.noMatches", { query })}
            </div>
          ) : (
            <>
              {sources.map(source => {
                const hits = data.results[source] || [];
                if (hits.length === 0) return null;
                const meta = SOURCE_META[source];
                return (
                  <div key={source}>
                    <div className="px-4 py-1.5 text-2xs text-muted-foreground font-semibold flex items-center gap-2 bg-muted/30">
                      <meta.icon className={cn("w-3 h-3", meta.tint)} />
                      {t(meta.labelKey)} · {hits.length}
                    </div>
                    {hits.map(hit => {
                      const globalIdx = flat.indexOf(hit);
                      const active = globalIdx === activeIdx;
                      return (
                        <button
                          key={`${source}-${hit.id}`}
                          onClick={() => navigate(hit)}
                          onMouseEnter={() => setActiveIdx(globalIdx)}
                          className={cn(
                            "w-full text-left flex items-start gap-3 px-4 py-2.5 transition border-l-2",
                            active
                              ? "bg-accent border-l-primary"
                              : "border-l-transparent hover:bg-muted/50"
                          )}
                        >
                          {hit.thumbnail_url ? (
                            <img src={hit.thumbnail_url} alt=""
                              className="w-10 h-10 rounded object-cover shrink-0" />
                          ) : (
                            <meta.icon className={cn("w-4 h-4 mt-1 shrink-0", meta.tint)} />
                          )}
                          <div className="flex-1 min-w-0">
                            <div className="text-sm font-medium truncate">{hit.title || t("apps.search.untitled")}</div>
                            {hit.subtitle && (
                              <div className="text-xs text-muted-foreground truncate">{hit.subtitle}</div>
                            )}
                            {hit.snippet && (
                              <div className="text-xs text-muted-foreground truncate mt-0.5">{hit.snippet}</div>
                            )}
                          </div>
                          <span className="text-2xs text-muted-foreground tabular-nums shrink-0 mt-1">
                            {formatTs(hit.timestamp)}
                          </span>
                          {active && (
                            <ArrowRight className="w-3.5 h-3.5 text-muted-foreground mt-1.5 shrink-0" />
                          )}
                        </button>
                      );
                    })}
                  </div>
                );
              })}
            </>
          )}
        </div>

        <div className="px-4 py-2 border-t border-border text-2xs text-muted-foreground flex items-center gap-3">
          <span>{t("apps.search.footerNavigate")}</span>
          <span>{t("apps.search.footerOpen")}</span>
          <span>{t("apps.search.footerClose")}</span>
          {data && <span className="ml-auto">{t("apps.search.results", { count: data.total })}</span>}
        </div>
      </div>
    </div>,
    document.body
  );
}

function Hint() {
  const { t } = useTranslation();
  return (
    <div className="p-8 text-center text-sm text-muted-foreground">
      <Search className="w-8 h-8 mx-auto mb-3 opacity-40" />
      <div>{t("apps.search.hintTitle")}</div>
      <div className="mt-2 text-xs opacity-70">{t("apps.search.hintSub")}</div>
    </div>
  );
}

function formatTs(ts?: string | number): string {
  if (!ts) return "";
  let d: Date;
  if (typeof ts === "number") {
    // Treat large numbers as ms, small as seconds.
    d = new Date(ts > 1e12 ? ts : ts * 1000);
  } else {
    d = new Date(ts);
  }
  if (isNaN(d.getTime())) return "";
  const now = new Date();
  if (d.toDateString() === now.toDateString()) {
    return d.toLocaleTimeString(locale(), { hour: "2-digit", minute: "2-digit" });
  }
  if (d.getFullYear() === now.getFullYear()) {
    return d.toLocaleDateString(locale(), { month: "short", day: "numeric" });
  }
  return d.toLocaleDateString(locale(), { year: "2-digit", month: "short" });
}
