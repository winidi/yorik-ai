/**
 * FamilyBoard — the wall in Dæly style: the week in person colours on
 * top, today per person below, routines for the kids, tap to tick.
 * Three layouts driven by `mode`: "board" (week + people), "calendar"
 * (the week, full screen), "tasks" (the people, full screen).
 *
 * Data: GET /api/ambient/board (kiosk gate, no session needed). Who
 * appears is decided by each person's own "show me on the wall"
 * consent. Ticking a task needs a signed-in person: the avatar tap
 * opens the PIN picker (the parent handles that); once the session is
 * that person's, their tiles become tappable.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Check, Loader2, Lock } from "lucide-react";
import { api } from "@/lib/api";
import { PersonAvatar } from "@/components/PersonAvatar";
import { cn } from "@/lib/utils";

export type BoardMode = "board" | "calendar" | "tasks";

interface Person { id: string; name: string; first_name: string; color: string; avatar_url: string | null }
interface Ev { id: number; title: string; starts_at: string; ends_at: string | null; all_day: boolean; owner_id: string | null; shared: boolean; location: string | null }
interface Task { id: number; title: string; due_date: string | null; done: boolean; done_at: string | null; assignee_ids: string[]; person: string; routine: boolean; estimated_minutes: number | null }
interface Feed { today: string; week_start: string; days: number; people: Person[]; events: Ev[]; tasks: Task[] }

const SHARED = "#6b7a8f";
const WD = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];

function addDays(iso: string, n: number): string {
  // local calendar arithmetic; toISOString() would shift the date in UTC
  const d = new Date(iso + "T00:00:00"); d.setDate(d.getDate() + n);
  const p = (x: number) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}
function hhmm(iso: string): string { return iso.slice(11, 16); }
function dayOf(iso: string): string { return iso.slice(0, 10); }

export function FamilyBoard({ mode, currentUserId, onNeedSignIn, lockOthers = false }: {
  mode: BoardMode;
  currentUserId: string | null;
  onNeedSignIn: (person: Person) => void;
  /** show a lock on tiles that are not the active person's (the wall) */
  lockOthers?: boolean;
}) {
  const [feed, setFeed] = useState<Feed | null>(null);
  const [busy, setBusy] = useState<number | null>(null);

  const load = useCallback(async () => {
    try { setFeed(await api.get<Feed>("/api/ambient/board")); } catch {}
  }, []);
  useEffect(() => { load(); const t = setInterval(load, 60_000); return () => clearInterval(t); }, [load]);

  const byId = useMemo(() => new Map((feed?.people || []).map(p => [p.id, p])), [feed]);
  const days = useMemo(() => feed ? Array.from({ length: feed.days }, (_, i) => addDays(feed.week_start, i)) : [], [feed]);
  const eventsByDay = useMemo(() => {
    const m = new Map<string, Ev[]>();
    for (const e of feed?.events || []) { const k = dayOf(e.starts_at); m.set(k, [...(m.get(k) || []), e]); }
    return m;
  }, [feed]);

  async function toggle(t: Task, owner: Person) {
    if (!feed) return;
    const mine = currentUserId && t.assignee_ids.includes(currentUserId);
    if (!mine) { onNeedSignIn(owner); return; }
    setBusy(t.id);
    try {
      await api.patch(`/api/tasks/${t.id}`, { done: t.done ? 0 : 1 });
      await load();
    } catch {} finally { setBusy(null); }
  }

  if (!feed) return <div className="absolute inset-0 grid place-items-center text-white/70"><Loader2 className="w-8 h-8 animate-spin" /></div>;

  const showWeek = mode !== "tasks";
  const showPeople = mode !== "calendar";

  return (
    <div className="absolute inset-0 overflow-hidden bg-[#fbfaf7] text-[#1f2430] select-none"
         style={{ fontFamily: '"Atkinson Hyperlegible", "Nunito", "Segoe UI", system-ui, sans-serif' }}>
      <div className="h-full grid gap-4 p-5 min-w-0" style={{ gridTemplateColumns: "minmax(0, 1fr)", gridTemplateRows: showWeek && showPeople ? "auto auto 1fr" : "auto 1fr" }}>
        <header className="flex items-baseline justify-between gap-4 flex-wrap min-w-0">
          <h1 className="text-3xl font-extrabold tracking-tight">{new Date(feed.today + "T00:00:00").toLocaleDateString("de-DE", { weekday: "long", day: "numeric", month: "long" })}</h1>
          <div className="flex gap-4 text-sm text-[#6b7280] flex-wrap min-w-0">
            {feed.people.map(p => <span key={p.id} className="flex items-center gap-1.5"><i className="w-2.5 h-2.5 rounded-full" style={{ background: p.color }} />{p.first_name || p.name}</span>)}
            <span className="flex items-center gap-1.5"><i className="w-2.5 h-2.5 rounded-full" style={{ background: SHARED }} />Alle</span>
          </div>
        </header>

        {showWeek && (
          <section className={cn("grid gap-2.5 min-w-0", mode === "calendar" ? "h-full min-h-0" : "")} style={{ gridTemplateColumns: `repeat(${days.length}, minmax(0, 1fr))` }}>
            {days.map((d, i) => {
              const today = d === feed.today;
              const evs = eventsByDay.get(d) || [];
              return (
                <div key={d} className={cn("rounded-2xl border p-2.5 flex flex-col gap-2 min-h-[7.5rem] overflow-hidden",
                                           today ? "border-[#1f2430] shadow-[inset_0_0_0_1px_#1f2430] bg-white" : "border-[#e9e6df] bg-white/70")}>
                  <div className="flex justify-between font-bold text-[15px]"><span>{WD[i % 7]}</span><span className="text-[#6b7280] font-semibold">{d.slice(8, 10)}.</span></div>
                  <div className="flex flex-col gap-1.5 overflow-y-auto">
                    {evs.length === 0 && <span className="text-xs text-[#9aa0ab]">frei</span>}
                    {evs.map(e => {
                      const c = e.shared ? SHARED : (byId.get(e.owner_id || "")?.color || SHARED);
                      return (
                        <span key={e.id} className="block text-[12.5px] leading-tight rounded-lg px-2 py-1 border-l-4 tabular-nums"
                              style={{ borderLeftColor: c, background: `color-mix(in srgb, ${c} 14%, white)` }}>
                          {!e.all_day && <b className="mr-1">{hhmm(e.starts_at)}</b>}{e.title}
                        </span>
                      );
                    })}
                  </div>
                </div>
              );
            })}
          </section>
        )}

        {showPeople && (
          <section className="grid gap-4 min-h-0 min-w-0" style={{ gridTemplateColumns: `repeat(${Math.max(1, feed.people.length)}, minmax(0, 1fr))` }}>
            {feed.people.map(p => {
              const mine = feed.tasks.filter(t => t.assignee_ids.includes(p.id));
              const routines = mine.filter(t => t.routine);
              const tasks = mine.filter(t => !t.routine);
              const open = mine.filter(t => !t.done).length, done = mine.filter(t => t.done).length;
              const isMe = currentUserId === p.id;
              return (
                <div key={p.id} className="rounded-[22px] p-4 flex flex-col gap-3 min-h-0" style={{ background: `color-mix(in srgb, ${p.color} 9%, white)` }}>
                  <button className="flex items-center gap-3 text-left" onClick={() => onNeedSignIn(p)} title={isMe ? "Angemeldet" : "Antippen zum Anmelden"}>
                    <PersonAvatar name={p.name} color={p.color} avatarUrl={p.avatar_url} size={54} className={cn("ring-4", isMe ? "ring-[#1f2430]" : "ring-white")} />
                    <div>
                      <div className="text-2xl font-extrabold leading-tight">{p.first_name || p.name}</div>
                      <div className="text-[13px] text-[#6b7280]">{open} offen · {done} erledigt{isMe ? " · angemeldet" : lockOthers ? " · antippen zum Abhaken" : ""}</div>
                    </div>
                  </button>
                  <div className="flex flex-col gap-2 overflow-y-auto min-h-0">
                    {routines.length > 0 && <div className="text-[11.5px] tracking-[.08em] uppercase text-[#6b7280] font-bold mt-1">Routine heute</div>}
                    {routines.map(t => <Tile key={t.id} t={t} color={p.color} today={feed.today} busy={busy === t.id} locked={lockOthers && !isMe} onTap={() => toggle(t, p)} />)}
                    {tasks.length > 0 && routines.length > 0 && <div className="text-[11.5px] tracking-[.08em] uppercase text-[#6b7280] font-bold mt-1">Aufgaben</div>}
                    {tasks.map(t => <Tile key={t.id} t={t} color={p.color} today={feed.today} busy={busy === t.id} locked={lockOthers && !isMe} onTap={() => toggle(t, p)} />)}
                    {mine.length === 0 && <div className="text-sm text-[#9aa0ab]">Nichts offen. Schöner Tag.</div>}
                  </div>
                </div>
              );
            })}
            {feed.people.length === 0 && (
              <div className="rounded-[22px] border border-dashed border-[#e9e6df] p-6 text-[#6b7280] text-sm">
                Noch niemand auf der Wand. Jede Person schaltet sich unter Settings → You → „Show me on the wall“ frei.
              </div>
            )}
          </section>
        )}
      </div>
    </div>
  );
}

function Tile({ t, color, today, busy, locked = false, onTap }: { t: Task; color: string; today: string; busy: boolean; locked?: boolean; onTap: () => void }) {
  const due = t.due_date && t.due_date <= today && !t.done;
  return (
    <button onClick={onTap} disabled={busy}
            className={cn("grid grid-cols-[30px_1fr_auto] items-center gap-2.5 bg-white rounded-[14px] px-3 py-2.5 text-left border",
                          t.done && "opacity-70")}
            style={{ borderColor: `color-mix(in srgb, ${color} 25%, white)` }}>
      <span className={cn("w-[26px] h-[26px] rounded-[9px] border-[2.5px] grid place-items-center text-white", t.done && "bg-[#2f9e64] border-[#2f9e64]")}
            style={t.done ? undefined : { borderColor: color }}>
        {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin text-[#6b7280]" /> : t.done ? <Check className="w-4 h-4" /> : locked ? <Lock className="w-3 h-3 text-[#9aa0ab]" /> : null}
      </span>
      <span className={cn("text-[16px] leading-tight", t.done && "line-through text-[#6b7280]")}>{t.title}
        {t.estimated_minutes ? <small className="block text-xs text-[#6b7280]">{t.estimated_minutes} Min.</small> : null}
      </span>
      <span className={cn("text-xs tabular-nums whitespace-nowrap", due ? "text-[#e0486b] font-bold" : "text-[#6b7280]")}>
        {t.done ? (t.done_at ? hhmm(t.done_at) : "✓") : due ? (t.due_date === today ? "heute" : "überfällig") : (t.due_date ? t.due_date.slice(8, 10) + "." + t.due_date.slice(5, 7) + "." : "")}
      </span>
    </button>
  );
}
