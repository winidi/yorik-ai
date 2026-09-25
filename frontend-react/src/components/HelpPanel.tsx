/**
 * The in-app help: a side sheet with the page for the app you are in,
 * and the list of all topics. Same docs/help/*.md the chat's help skill
 * reads (GET /api/help). Open it from anywhere with openHelp(topic?).
 */
import { useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { ArrowLeft, LifeBuoy, MessageSquare, X } from "lucide-react";
import { api } from "@/lib/api";
import { appIdFromPath } from "@/lib/dock-order";
import { AssistantMarkdown } from "@/components/AssistantMarkdown";
import { cn } from "@/lib/utils";
import { startTour } from "@/components/FirstRunTour";

interface Topic { topic: string; title: string; summary: string; app: string }
interface Page extends Topic { body: string }

// Which help page belongs to which app (app id as in the Dock).
const TOPIC_FOR_APP: Record<string, string> = {
  home: "next-steps", calendar: "calendar", tasks: "tasks", chat: "next-steps",
  docs: "paperless", compose: "compose", write: "schreiben", photos: "immich",
  whatsapp: "whatsapp", email: "email", contacts: "contacts", briefing: "briefing",
  board: "family-board", recordings: "recordings", finance: "finance",
  pipelines: "pipelines", settings: "first-run",
};

export function openHelp(topic?: string) {
  window.dispatchEvent(new CustomEvent("yorik:help", { detail: { topic } }));
}

export function HelpPanel() {
  const loc = useLocation();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [topics, setTopics] = useState<Topic[] | null>(null);
  const [page, setPage] = useState<Page | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function show(topic: string | null) {
    setError(null);
    if (!topics) {
      api.get<{ topics: Topic[] }>("/api/help").then(r => setTopics(r.topics)).catch(() => {});
    }
    if (!topic) { setPage(null); return; }
    try { setPage(await api.get<Page>(`/api/help/${topic}`)); }
    catch (e: any) { setPage(null); setError(e?.message || "This help page couldn't be loaded."); }
  }

  useEffect(() => {
    function onHelp(e: Event) {
      const want = (e as CustomEvent).detail?.topic as string | undefined;
      const app = appIdFromPath(loc.pathname) || "home";
      setOpen(true);
      void show(want || TOPIC_FOR_APP[app] || null);
    }
    window.addEventListener("yorik:help", onHelp);
    return () => window.removeEventListener("yorik:help", onHelp);
  }, [loc.pathname, topics]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  if (!open) return null;
  return (
    <div className="fixed inset-0 z-[900] flex justify-end" role="dialog" aria-label="Help">
      <div className="absolute inset-0 bg-black/40 backdrop-blur-sm" onClick={() => setOpen(false)} />
      <aside className="relative w-full max-w-lg h-full bg-background border-l border-border shadow-2xl flex flex-col">
        <header className="h-14 px-4 flex items-center gap-2 border-b border-border shrink-0">
          {page
            ? <button onClick={() => void show(null)} className="w-9 h-9 rounded-md hover:bg-muted flex items-center justify-center" aria-label="All help topics"><ArrowLeft className="w-4 h-4" /></button>
            : <LifeBuoy className="w-5 h-5 text-primary ml-1.5" />}
          <h2 className="flex-1 font-semibold truncate">{page ? page.title : "Help"}</h2>
          <button onClick={() => setOpen(false)} className="w-9 h-9 rounded-md hover:bg-muted flex items-center justify-center" aria-label="Close help"><X className="w-4 h-4" /></button>
        </header>
        <div className="flex-1 overflow-y-auto px-5 py-5">
          {error && <p className="text-sm text-destructive mb-4">{error}</p>}
          {page ? (
            <AssistantMarkdown className="text-sm leading-relaxed">{page.body}</AssistantMarkdown>
          ) : (
            <div className="space-y-2">
              <p className="text-sm text-muted-foreground mb-3">Pick a topic, or ask Yorik in your own words.</p>
              {(topics || []).map(t => (
                <button key={t.topic} onClick={() => void show(t.topic)}
                        className="w-full text-left p-3 rounded-xl border border-border bg-card hover:border-foreground/20 transition">
                  <div className="text-sm font-medium">{t.title}</div>
                  {t.summary && <div className="text-xs text-muted-foreground mt-0.5 line-clamp-2">{t.summary}</div>}
                </button>
              ))}
              {!topics && <p className="text-sm text-muted-foreground">Loading…</p>}
              <button onClick={() => { setOpen(false); navigate("/home"); setTimeout(startTour, 600); }}
                      className="w-full text-left p-3 rounded-xl border border-dashed border-border hover:border-foreground/20 transition text-sm">
                Show the tour again
              </button>
            </div>
          )}
        </div>
        <footer className="p-4 border-t border-border shrink-0">
          <button
            onClick={() => { setOpen(false); navigate(`/chat?say=${encodeURIComponent(page ? `How does ${page.title} work?` : "How do I get started?")}`); }}
            className={cn("w-full flex items-center justify-center gap-2 px-4 py-2.5 rounded-xl bg-primary text-primary-foreground text-sm font-medium hover:opacity-90 transition")}
          >
            <MessageSquare className="w-4 h-4" /> Ask Yorik instead
          </button>
        </footer>
      </aside>
    </div>
  );
}
