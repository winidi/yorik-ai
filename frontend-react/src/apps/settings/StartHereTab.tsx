/**
 * Settings → Start here. What a new admin does after installing Yorik,
 * in order, each line opening the place where it is done. The steps
 * come from real data (GET /api/setup/checklist, the same list Home
 * shows, plus the household's AI model and apps), so something done
 * elsewhere is ticked here too.
 */
import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Check, ChevronRight } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { InviteDialog } from "@/components/InviteDialog";
import { PhoneSetup } from "@/components/PhoneSetup";

type Tab = "profile" | "apps" | "letters" | "backup" | "system" | "ai";

interface Step { id: string; title: string; hint: string; done: boolean; action: string }
interface Checklist { steps: Step[] }
interface Row { id: string; title: string; hint: string; done?: boolean; optional?: boolean; go: () => void }

export function StartHereTab({ onOpen, available }: { onOpen: (tab: Tab) => void; available: string[] }) {
  const navigate = useNavigate();
  const [list, setList] = useState<Checklist | null>(null);
  const [aiOk, setAiOk] = useState<boolean | null>(null);
  const [appsOn, setAppsOn] = useState<number | null>(null);
  const [modal, setModal] = useState<"invite" | "phone" | null>(null);

  const load = useCallback(() => {
    api.get<Checklist>("/api/setup/checklist").then(setList).catch(() => setList({ steps: [] }));
    api.get<{ llm: { reachable: boolean } }>("/api/system/status")
      .then(s => setAiOk(!!s.llm?.reachable)).catch(() => setAiOk(null));
    api.get<{ enabled: boolean }[]>("/api/apps/opt-in")
      .then(a => setAppsOn(Array.isArray(a) ? a.filter(x => x.enabled).length : null)).catch(() => setAppsOn(null));
  }, []);
  useEffect(() => { load(); }, [load]);

  function goStep(s: Step) {
    switch (s.action) {
      case "profile":  onOpen("profile"); break;
      case "chat":     navigate("/chat"); break;
      case "phone":    setModal("phone"); break;
      case "invite":   setModal("invite"); break;
      case "mail":     navigate("/email?add=1"); break;
      case "calendar": navigate("/calendar?import=1"); break;
      case "backup":   onOpen("backup"); break;
    }
  }

  if (!list) return <div className="text-sm text-muted-foreground">Loading…</div>;

  const step = (id: string) => list.steps.find(s => s.id === id);
  const fromList = (id: string): Row[] => {
    const s = step(id);
    return s ? [{ id: s.id, title: s.title, hint: s.hint, done: s.done, go: () => goStep(s) }] : [];
  };

  const household: Row[] = [
    { id: "ai", title: "Yorik can think", hint: "The AI model is running; without it the chat can't answer",
      done: aiOk ?? undefined, go: () => onOpen("ai") },
    ...fromList("family"),
    ...fromList("backup"),
    { id: "apps", title: "Pick the extra apps you want", hint: "Write, WhatsApp, Finance, Recordings, Pipelines: off until you turn them on",
      done: appsOn === null ? undefined : appsOn > 0, optional: true, go: () => onOpen("apps") },
    { id: "away", title: "Reach Yorik when you're away from home", hint: "On your phones, through Tailscale",
      optional: true, go: () => onOpen("system") },
  ];
  const yours: Row[] = [
    ...fromList("look"),
    ...fromList("phone"),
    ...fromList("mail"),
    ...fromList("calendar"),
    ...fromList("ask"),
    { id: "letters", title: "Your letterhead", hint: "Only if you write letters or invoices with Yorik",
      optional: true, go: () => onOpen("letters") },
  ];
  // A line that would open a page this Yorik doesn't show (a hosted
  // family has no backup or AI page of its own) is left out.
  const opens: Record<string, Tab> = { ai: "ai", backup: "backup", apps: "apps", away: "system", look: "profile", letters: "letters" };
  const shown = (r: Row) => !opens[r.id] || available.includes(opens[r.id]);
  household.splice(0, household.length, ...household.filter(shown));
  yours.splice(0, yours.length, ...yours.filter(shown));
  const needed = [...household, ...yours].filter(r => !r.optional && r.done !== undefined);
  const done = needed.filter(r => r.done).length;
  const pct = needed.length ? Math.round((done / needed.length) * 100) : 0;
  const next = [...household, ...yours].find(r => !r.optional && r.done === false);

  return (
    <div>
      <header className="mb-6">
        <h1 className="text-2xl font-semibold">Start here</h1>
        <p className="text-sm text-muted-foreground mt-1">
          New to Yorik? Work down this list from the top. Each line opens the place where it is done,
          and it ticks itself off. Everything else in Settings can wait until you need it.
        </p>
      </header>

      <div className="mb-6">
        <div className="flex items-baseline justify-between text-sm mb-1.5">
          <span>{done} of {needed.length} done</span>
          {done === needed.length && needed.length > 0 && <span className="text-emerald-600">Yorik is set up.</span>}
        </div>
        <div className="h-2 rounded-full bg-muted overflow-hidden">
          <div className="h-full bg-primary rounded-full transition-all" style={{ width: `${pct}%` }} />
        </div>
      </div>

      <Section title="For the household" rows={household} next={next} />
      <Section title="For you" rows={yours} next={next} />

      {modal === "invite" && <InviteDialog onClose={() => { setModal(null); load(); }} />}
      {modal === "phone" && (
        <div className="fixed inset-0 z-[850] flex items-center justify-center p-4 bg-black/50 backdrop-blur-sm" onClick={() => setModal(null)}>
          <div className="w-full max-w-md bg-background border border-border rounded-2xl shadow-2xl p-6" onClick={e => e.stopPropagation()}>
            <h2 className="text-xl font-semibold mb-4">Yorik on your phone</h2>
            <PhoneSetup onDone={() => { setModal(null); load(); }} />
          </div>
        </div>
      )}
    </div>
  );
}

function Section({ title, rows, next }: { title: string; rows: Row[]; next?: Row }) {
  return (
    <section className="mb-6">
      <h2 className="text-sm font-semibold text-muted-foreground mb-2">{title}</h2>
      <ul className="space-y-2">
        {rows.map(r => (
          <li key={r.id}>
            <button
              onClick={r.go}
              className={cn(
                "w-full flex items-center gap-3 p-3 rounded-xl text-left transition border border-border bg-card hover:bg-muted",
                r.done && "opacity-60",
                r === next && "ring-1 ring-primary/50",
              )}
            >
              <span className={cn("w-6 h-6 rounded-md border-2 flex items-center justify-center shrink-0",
                r.done ? "bg-emerald-500 border-emerald-500 text-white" : "border-muted-foreground/40",
                r.done === undefined && "border-dashed")}>
                {r.done && <Check className="w-4 h-4" strokeWidth={3} />}
              </span>
              <span className="flex-1 min-w-0">
                <span className="block text-sm font-medium">
                  {r.title}
                  {r.optional && <span className="ml-2 text-2xs font-normal text-muted-foreground">optional</span>}
                </span>
                {!r.done && <span className="block text-xs text-muted-foreground">{r.hint}</span>}
              </span>
              <ChevronRight className="w-4 h-4 text-muted-foreground shrink-0" />
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
