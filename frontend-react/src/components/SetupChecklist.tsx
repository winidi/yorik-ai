/**
 * Home's "Set up Yorik" list. Steps come from real data
 * (GET /api/setup/checklist), so something done elsewhere shows as done
 * here too. It stays until everything is done or the person hides it.
 */
import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Check, ChevronRight, X } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { InviteDialog } from "@/components/InviteDialog";
import { PhoneSetup } from "@/components/PhoneSetup";

interface Step { id: string; title: string; hint: string; done: boolean; action: string }
interface List { steps: Step[]; done: number; total: number; hidden: boolean }

export function SetupChecklist() {
  const navigate = useNavigate();
  const [list, setList] = useState<List | null>(null);
  const [modal, setModal] = useState<"invite" | "phone" | null>(null);

  const load = useCallback(() => {
    api.get<List>("/api/setup/checklist").then(setList).catch(() => setList(null));
  }, []);
  useEffect(() => { load(); }, [load]);

  if (!list || list.hidden || list.done >= list.total) return null;

  async function hide() {
    try { await api.patch("/api/me/ui-state", { checklist_hidden: true }); } catch { /* hide locally anyway */ }
    setList(l => l && { ...l, hidden: true });
  }

  function go(step: Step) {
    switch (step.action) {
      case "profile":  navigate("/settings"); break;
      case "chat":     navigate("/chat"); break;
      case "phone":    setModal("phone"); break;
      case "invite":   setModal("invite"); break;
      case "mail":     navigate("/email?add=1"); break;
      case "calendar": navigate("/calendar?import=1"); break;
      case "backup":   navigate("/settings?tab=backup"); break;
    }
  }

  const next = list.steps.find(s => !s.done);
  const pct = Math.round((list.done / list.total) * 100);

  return (
    <section className="mb-8 bg-card border border-border rounded-2xl p-5" data-tour="checklist">
      <div className="flex items-start justify-between gap-3 mb-3">
        <div>
          <h2 className="text-lg font-semibold">Set up Yorik</h2>
          <p className="text-sm text-muted-foreground">{list.done} of {list.total} done. Take your time; this list waits.</p>
        </div>
        <button onClick={hide} className="text-muted-foreground hover:text-foreground p-1" title="Hide this list" aria-label="Hide this list">
          <X className="w-4 h-4" />
        </button>
      </div>
      <div className="h-2 rounded-full bg-muted overflow-hidden mb-4">
        <div className="h-full bg-primary rounded-full transition-all" style={{ width: `${pct}%` }} />
      </div>
      <ul className="space-y-2">
        {list.steps.map(s => (
          <li key={s.id}>
            <button
              onClick={() => !s.done && go(s)}
              disabled={s.done}
              className={cn(
                "w-full flex items-center gap-3 p-3 rounded-xl text-left transition",
                s.done ? "opacity-60" : "bg-background/60 hover:bg-muted",
                s === next && "ring-1 ring-primary/50",
              )}
            >
              <span className={cn("w-6 h-6 rounded-md border-2 flex items-center justify-center shrink-0",
                                  s.done ? "bg-emerald-500 border-emerald-500 text-white" : "border-muted-foreground/40")}>
                {s.done && <Check className="w-4 h-4" strokeWidth={3} />}
              </span>
              <span className="flex-1 min-w-0">
                <span className={cn("block text-sm font-medium", s.done && "line-through")}>{s.title}</span>
                {!s.done && <span className="block text-xs text-muted-foreground">{s.hint}</span>}
              </span>
              {!s.done && <ChevronRight className="w-4 h-4 text-muted-foreground shrink-0" />}
            </button>
          </li>
        ))}
      </ul>

      {modal === "invite" && <InviteDialog onClose={() => { setModal(null); load(); }} />}
      {modal === "phone" && (
        <div className="fixed inset-0 z-[850] flex items-center justify-center p-4 bg-black/50 backdrop-blur-sm" onClick={() => setModal(null)}>
          <div className="w-full max-w-md bg-background border border-border rounded-2xl shadow-2xl p-6" onClick={e => e.stopPropagation()}>
            <h2 className="text-xl font-semibold mb-4">Yorik on your phone</h2>
            <PhoneSetup onDone={() => { setModal(null); load(); }} />
          </div>
        </div>
      )}
    </section>
  );
}
