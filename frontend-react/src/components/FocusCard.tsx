/**
 * FocusCard — the task you are working on, on every screen.
 *
 * Dirk 2026-09-29: "with ADHD I see the other tasks and switch back and
 * forth, which is exhausting. Yorik should keep working — I still write
 * mails and make appointments — but one card, whatever I am doing, says
 * which task I am on, so I stay with it."
 *
 * It shows while a task's timer runs (started with "Focus" in Tasks):
 * the title, the time spent, and three ways on — Done, Pause, open it.
 * Nothing else is hidden or blocked. The card listens for
 * `yorik:focus-changed` (Tasks fires it on start/stop/done) and checks
 * again every minute, so a start on the phone shows up here too.
 */
import { useCallback, useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { Check, Pause, Target } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

interface RunningTask {
  id: number;
  title: string;
  started_at: string;
  elapsed_minutes: number;
  estimated_minutes?: number | null;
}

export const FOCUS_CHANGED = "yorik:focus-changed";

/** Tell every FocusCard to look again (after start / stop / done). */
export function announceFocusChange(): void {
  window.dispatchEvent(new Event(FOCUS_CHANGED));
}

function fmtMinutes(m: number): string {
  const h = Math.floor(m / 60);
  const min = m % 60;
  return h ? `${h}h ${String(min).padStart(2, "0")}m` : `${min}m`;
}

export function FocusCard() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const loc = useLocation();
  const [task, setTask] = useState<RunningTask | null>(null);
  const [loadedAt, setLoadedAt] = useState(() => Date.now());
  const [, tick] = useState(0);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const r = await api.get<{ task: RunningTask | null }>("/api/tasks/running");
      setTask(r.task);
      setLoadedAt(Date.now());
    } catch { /* signed out or offline: show nothing */ }
  }, []);

  useEffect(() => {
    void load();
    const poll = window.setInterval(load, 60_000);
    const onChange = () => { void load(); };
    window.addEventListener(FOCUS_CHANGED, onChange);
    window.addEventListener("focus", onChange);
    return () => {
      window.clearInterval(poll);
      window.removeEventListener(FOCUS_CHANGED, onChange);
      window.removeEventListener("focus", onChange);
    };
  }, [load]);

  // Minute counter moves without asking the server every minute.
  useEffect(() => {
    if (!task) return;
    const id = window.setInterval(() => tick(n => n + 1), 30_000);
    return () => window.clearInterval(id);
  }, [task]);

  if (!task || loc.pathname.startsWith("/ambient")) return null;

  const minutes = task.elapsed_minutes + Math.floor((Date.now() - loadedAt) / 60_000);
  const over = !!task.estimated_minutes && minutes > task.estimated_minutes;

  async function act(kind: "done" | "pause") {
    if (!task || busy) return;
    setBusy(true);
    try {
      if (kind === "done") await api.patch(`/api/tasks/${task.id}`, { done: 1 });
      else await api.post(`/api/tasks/${task.id}/stop`);
      setTask(null);
      announceFocusChange();
      window.dispatchEvent(new CustomEvent("yorik:tasks-changed"));
    } catch { /* stays; the next poll shows the truth */ }
    finally { setBusy(false); }
  }

  return (
    <div
      role="status"
      className={cn(
        "fixed z-[45] left-1/2 -translate-x-1/2",
        // Below the mobile top bar; on desktop in the empty middle of
        // the app headers.
        "top-[calc(env(safe-area-inset-top)+3rem)] md:top-3",
        "max-w-[calc(100vw-2rem)] md:max-w-md",
        "flex items-center gap-2 rounded-full border border-emerald-500/40 bg-card/95 backdrop-blur",
        "pl-3 pr-1.5 py-1.5 shadow-lg shadow-emerald-500/10",
      )}
    >
      <Target className="w-4 h-4 text-emerald-500 shrink-0" aria-hidden />
      <button
        onClick={() => navigate(`/tasks?task=${task.id}`)}
        className="min-w-0 text-left"
        title={t("tasks.focus.open")}
      >
        <span className="block text-2xs text-emerald-500 font-medium leading-none">{t("tasks.focus.now")}</span>
        <span className="block text-sm font-medium truncate max-w-[52vw] md:max-w-[18rem]">{task.title}</span>
      </button>
      <span className={cn("text-xs tabular-nums shrink-0", over ? "text-amber-500" : "text-muted-foreground")}>
        {fmtMinutes(minutes)}
      </span>
      <button
        onClick={() => act("pause")}
        disabled={busy}
        className="grid place-items-center w-8 h-8 rounded-full hover:bg-muted text-muted-foreground shrink-0"
        title={t("tasks.focus.pause")}
        aria-label={t("tasks.focus.pause")}
      >
        <Pause className="w-4 h-4" />
      </button>
      <button
        onClick={() => act("done")}
        disabled={busy}
        className="grid place-items-center w-8 h-8 rounded-full bg-emerald-600 hover:bg-emerald-700 text-white shrink-0"
        title={t("tasks.focus.done")}
        aria-label={t("tasks.focus.done")}
      >
        <Check className="w-4 h-4" />
      </button>
    </div>
  );
}
