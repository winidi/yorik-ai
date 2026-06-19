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
  Calendar, X, Loader2, ArrowRight,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";

interface SearchHit {
  source: "email" | "whatsapp" | "paperless" | "immich" | "calendar";
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

const SOURCE_META: Record<string, { label: string; icon: any; tint: string }> = {
  email:     { label: "Email",     icon: Mail,         tint: "text-blue-500" },
  whatsapp:  { label: "WhatsApp",  icon: MessageSquare, tint: "text-emerald-500" },
  paperless: { label: "Documents", icon: FileText,     tint: "text-amber-500" },
  immich:    { label: "Photos",    icon: ImageIcon,    tint: "text-pink-500" },
  calendar:  { label: "Calendar",  icon: Calendar,     tint: "text-violet-500" },
};

const SOURCE_ORDER: Array<keyof typeof SOURCE_META> = [
  "email", "whatsapp", "paperless", "immich", "calendar",
];

export function CommandPalette() {
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
    for (const k of SOURCE_ORDER) (data.results[k] || []).forEach(h => out.push(h));
    return out;
  }, [data]);

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
            placeholder="Search across email, WhatsApp, documents, photos, calendar…"
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
              {loading ? "Searching…" : "No results yet — try a longer query."}
            </div>
          ) : data.total === 0 ? (
            <div className="p-8 text-center text-sm text-muted-foreground">
              No matches for "{query}".
            </div>
          ) : (
            <>
              {SOURCE_ORDER.map(source => {
                const hits = data.results[source] || [];
                if (hits.length === 0) return null;
                const meta = SOURCE_META[source];
                return (
                  <div key={source}>
                    <div className="px-4 py-1.5 text-[10px] uppercase tracking-wider text-muted-foreground font-semibold flex items-center gap-2 bg-muted/30">
                      <meta.icon className={cn("w-3 h-3", meta.tint)} />
                      {meta.label} · {hits.length}
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
                            <div className="text-sm font-medium truncate">{hit.title || "(untitled)"}</div>
                            {hit.subtitle && (
                              <div className="text-xs text-muted-foreground truncate">{hit.subtitle}</div>
                            )}
                            {hit.snippet && (
                              <div className="text-xs text-muted-foreground truncate mt-0.5">{hit.snippet}</div>
                            )}
                          </div>
                          <span className="text-[10px] text-muted-foreground tabular-nums shrink-0 mt-1">
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

        <div className="px-4 py-2 border-t border-border text-[10px] text-muted-foreground flex items-center gap-3">
          <span>↑↓ navigate</span>
          <span>↵ open</span>
          <span>ESC close</span>
          {data && <span className="ml-auto">{data.total} result{data.total !== 1 ? "s" : ""}</span>}
        </div>
      </div>
    </div>,
    document.body
  );
}

function Hint() {
  return (
    <div className="p-8 text-center text-sm text-muted-foreground">
      <Search className="w-8 h-8 mx-auto mb-3 opacity-40" />
      <div>Search everything — email, WhatsApp, filed documents, photos, calendar.</div>
      <div className="mt-2 text-xs opacity-70">Try a name, topic, or invoice number.</div>
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
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  if (d.getFullYear() === now.getFullYear()) {
    return d.toLocaleDateString([], { month: "short", day: "numeric" });
  }
  return d.toLocaleDateString([], { year: "2-digit", month: "short" });
}
