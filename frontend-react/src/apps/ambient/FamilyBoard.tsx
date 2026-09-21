/**
 * FamilyBoard — the family planner on the wall: the week in person colours on
 * top, today per person below, routines with a week of ticks, tap to
 * tick. Three layouts driven by `mode`: "board" (week + people),
 * "calendar" (the week, full screen), "tasks" (the people, full screen).
 *
 * Data: GET /api/ambient/board (kiosk gate or a signed-in member). Who
 * appears is decided by each person's own "show me on the wall" consent.
 * Ticking needs the active person: on the wall the avatar tap opens the
 * PIN picker (the parent handles that) and the unlock expires; on the
 * /board page the signed-in person ticks their own tiles. Parents (any
 * account that is not restricted) also tick the children's tiles and
 * add a to-do to anyone's column; children add to their own. Whoever
 * may tick a column may also sort it (drag the grip on a tile) and
 * open a to-do (double tap: title, day, repetition). A tap on a
 * calendar entry shows the appointment.
 *
 * Look: near-white paper, person colour only as accent (stripe, ring,
 * chip), big legible type (bundled Nunito + Atkinson Hyperlegible), a
 * warm dark palette after 21:00 so the wall does not glow at night.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Backpack, Bed, BookOpen, Check, Timer, ChevronDown, ChevronLeft, ChevronRight, Dog, GripVertical, Loader2, Repeat, Lock, Plus, Sparkles, Utensils } from "lucide-react";
import { api } from "@/lib/api";
import { PersonAvatar } from "@/components/PersonAvatar";
import { cn } from "@/lib/utils";
import { EventDetails, TaskDialog, repeatLabel } from "./BoardDialogs";
import "./board-fonts.css";

export type BoardMode = "board" | "calendar" | "tasks";

interface Person { id: string; name: string; first_name: string; color: string; avatar_url: string | null; role?: string }
interface Ev { id: number; title: string; starts_at: string; ends_at: string | null; all_day: boolean; owner_id: string | null; shared: boolean; location: string | null; notes?: string; calendar?: string }
interface Task { id: number; title: string; due_date: string | null; done: boolean; done_at: string | null; assignee_ids: string[]; person: string; category: string; routine: boolean; estimated_minutes: number | null; started_at?: string | null; actual_minutes?: number | null; positions?: Record<string, number>; recurrence_rule?: string }
interface LogRow { title: string; user_id: string; day: string }
interface Feed { today: string; week_start: string; days: number; people: Person[]; events: Ev[]; tasks: Task[]; routine_log: LogRow[] }
type Group = "routine" | "open";
interface Grip { onPointerDown: (e: React.PointerEvent) => void }
interface Tokens { bg: string; card: string; ink: string; muted: string; line: string; soft: string }

const SHARED = "#6b7a8f";
const WD = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];
const WORK_START = 8 * 60, WORK_END = 20 * 60;
const DISPLAY = '"Nunito", "Segoe UI", system-ui, sans-serif';
const BODY = '"Atkinson Hyperlegible", "Segoe UI", system-ui, sans-serif';

function addDays(iso: string, n: number): string {
  // local calendar arithmetic; toISOString() would shift the date in UTC
  const d = new Date(iso + "T00:00:00"); d.setDate(d.getDate() + n);
  const p = (x: number) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}
const pad = (n: number) => String(n).padStart(2, "0");
const hhmm = (iso: string) => iso.slice(11, 16);
const dayOf = (iso: string) => iso.slice(0, 10);
const mins = (iso: string) => parseInt(iso.slice(11, 13), 10) * 60 + parseInt(iso.slice(14, 16), 10);
const fmtDay = (iso: string) => `${iso.slice(8, 10)}.${iso.slice(5, 7)}.`;
const nowIso = () => { const d = new Date(); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`; };

function routineIcon(title: string) {
  const t = title.toLowerCase();
  if (/hausaufg|lesen|lernen|schule/.test(t)) return BookOpen;
  if (/ranzen|rucksack|tasche|packen/.test(t)) return Backpack;
  if (/bett|zimmer|aufräum|schlaf/.test(t)) return Bed;
  if (/tisch|küche|essen|spül|koch/.test(t)) return Utensils;
  if (/hund|katze|gassi|futter/.test(t)) return Dog;
  return Sparkles;
}

export function FamilyBoard({ mode, currentUserId, currentUserRole = null, onNeedSignIn, lockOthers = false, offerJoin = false }: {
  mode: BoardMode;
  currentUserId: string | null;
  /** Role of the signed-in / unlocked person; anything but "restricted" is a parent. */
  currentUserRole?: string | null;
  /** /board page: tell the signed-in person when they are not on the board. */
  offerJoin?: boolean;
  onNeedSignIn: (person: Person) => void;
  /** show a lock on tiles that are not the active person's (the wall) */
  lockOthers?: boolean;
}) {
  const [feed, setFeed] = useState<Feed | null>(null);
  const [busy, setBusy] = useState<number | null>(null);
  const [pop, setPop] = useState<number | null>(null);
  const [weekOffset, setWeekOffset] = useState(0);
  const [now, setNow] = useState(nowIso());
  const [narrow, setNarrow] = useState(() => typeof window !== "undefined" && window.innerWidth < 760);
  const [openDone, setOpenDone] = useState<Record<string, boolean>>({});
  // Whose calendar the week shows: tap names in the legend to pick one
  // or several, "Alle" for everybody. Empty = everybody. Kept per device.
  const [picked, setPicked] = useState<Set<string>>(() => {
    try { return new Set(JSON.parse(localStorage.getItem("yorik:board:calendars") || "[]") as string[]); } catch { return new Set(); }
  });
  function pickCalendar(id: string | null) {
    setPicked(prev => {
      const n = new Set(id === null ? [] : prev);
      if (id !== null) { if (n.has(id)) n.delete(id); else n.add(id); }
      try { localStorage.setItem("yorik:board:calendars", JSON.stringify([...n])); } catch {}
      return n;
    });
  }
  const [adding, setAdding] = useState<string | null>(null);      // person id whose "+" is open
  const [draft, setDraft] = useState("");
  const [draftDay, setDraftDay] = useState("");                   // "" = today
  const [shownEvent, setShownEvent] = useState<Ev | null>(null);
  const [editing, setEditing] = useState<{ pid: string; id: number } | null>(null);   // the to-do whose details are open
  // Sorting a column: the tile under the finger moves through the list
  // while it is dragged by its grip; the order is saved on release.
  const [drag, setDrag] = useState<{ pid: string; group: Group; id: number; ids: number[] } | null>(null);
  const isParent = !!currentUserId && !!currentUserRole && currentUserRole.toLowerCase() !== "restricted";
  const swipe = useRef<{ x: number; y: number } | null>(null);

  const load = useCallback(async () => {
    try { setFeed(await api.get<Feed>("/api/ambient/board?days=14")); } catch {}
  }, []);
  useEffect(() => { load(); const t = setInterval(load, 60_000); return () => clearInterval(t); }, [load]);
  useEffect(() => { const t = setInterval(() => setNow(nowIso()), 15_000); return () => clearInterval(t); }, []);
  useEffect(() => {
    const onResize = () => setNarrow(window.innerWidth < 760);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  const hour = parseInt(now.slice(11, 13), 10);
  const dim = hour >= 21 || hour < 6;
  const tokens: Tokens = dim
    ? { bg: "#1b1a17", card: "#26241f", ink: "#f1ede4", muted: "#a9a297", line: "#3a3730", soft: "#2e2b25" }
    : { bg: "#fbfaf7", card: "#ffffff", ink: "#1f2430", muted: "#6b7280", line: "#e9e6df", soft: "#f4f2ec" };

  const byId = useMemo(() => new Map((feed?.people || []).map(p => [p.id, p])), [feed]);
  const days = useMemo(() => feed ? Array.from({ length: 7 }, (_, i) => addDays(feed.week_start, weekOffset * 7 + i)) : [], [feed, weekOffset]);
  const eventsByDay = useMemo(() => {
    const m = new Map<string, Ev[]>();
    // picked people who left the board do not hide everything
    const active = new Set([...picked].filter(id => (feed?.people || []).some(p => p.id === id)));
    for (const e of feed?.events || []) {
      // household events ("Alle") concern whoever is picked
      if (active.size && !e.shared && !(e.owner_id && active.has(e.owner_id))) continue;
      const k = dayOf(e.starts_at); m.set(k, [...(m.get(k) || []), e]);
    }
    for (const list of m.values()) list.sort((a, b) => Number(b.all_day) - Number(a.all_day) || a.starts_at.localeCompare(b.starts_at));
    return m;
  }, [feed, picked]);
  const people = useMemo(() => {
    // The same order on every big screen: the head of the household on
    // the left, the other adults next, the children to the right (the
    // feed is by account age within a rank). Only the phone, where the
    // board is one long page, starts with the signed-in person.
    const rank = (p: Person) => ({ platform_admin: 0, admin: 1, member: 2 } as Record<string, number>)[(p.role || "").toLowerCase()] ?? 3;
    const list = (feed?.people || []).map((p, i) => ({ p, i }))
      .sort((a, b) => rank(a.p) - rank(b.p) || a.i - b.i).map(x => x.p);
    if (narrow && currentUserId) list.sort((a, b) => Number(b.id === currentUserId) - Number(a.id === currentUserId));
    return list;
  }, [feed, currentUserId, narrow]);

  async function toggle(t: Task, owner: Person) {
    if (!feed) return;
    const mine = currentUserId && t.assignee_ids.includes(currentUserId);
    const asParent = isParent && (owner.role || "").toLowerCase() === "restricted";
    if (!mine && !asParent) { onNeedSignIn(owner); return; }
    setBusy(t.id); setPop(t.id);
    try {
      await api.patch(`/api/tasks/${t.id}`, { done: t.done ? 0 : 1 });
      await load();
    } catch {} finally { setBusy(null); setTimeout(() => setPop(null), 500); }
  }

  // Long press: start or stop the task's timer, as in the Tasks app.
  async function toggleTimer(t: Task, owner: Person) {
    const mine = currentUserId && t.assignee_ids.includes(currentUserId);
    const asParent = isParent && (owner.role || "").toLowerCase() === "restricted";
    if (!mine && !asParent) { onNeedSignIn(owner); return; }
    if (t.done) return;
    try {
      await api.post(`/api/tasks/${t.id}/${t.started_at ? "stop" : "start"}`, {});
      await load();
    } catch {}
  }

  async function addTask(p: Person) {
    const title = draft.trim();
    if (!title || !feed) return;
    setAdding(null); setDraft("");
    try {
      await api.post("/api/tasks", { title, due_date: draftDay || feed.today, assignee_user_ids: [p.id] });
      await load();
    } catch {}
  }

  async function saveTask(t: Task, changes: { title?: string; due_date?: string; recurrence_rule?: string }) {
    setEditing(null);
    if (!Object.keys(changes).length) return;
    setFeed(f => f && { ...f, tasks: f.tasks.map(x => x.id === t.id ? { ...x, ...changes, due_date: changes.due_date === undefined ? x.due_date : (changes.due_date || null) } : x) });
    try { await api.patch(`/api/tasks/${t.id}`, changes); } catch {}
    await load();
  }

  // A column in its hand-made order; tiles nobody placed yet follow in
  // the feed's order (by due date).
  function ordered(list: Task[], pid: string, group?: Group): Task[] {
    if (group && drag && drag.pid === pid && drag.group === group) {
      const at = new Map(drag.ids.map((id, i) => [id, i]));
      return [...list].sort((a, b) => (at.get(a.id) ?? 1e9) - (at.get(b.id) ?? 1e9));
    }
    return list.map((t, i) => ({ t, i }))
      .sort((a, b) => (a.t.positions?.[pid] ?? 1e9) - (b.t.positions?.[pid] ?? 1e9) || a.i - b.i).map(x => x.t);
  }

  function grip(pid: string, group: Group, t: Task, ids: number[]): Grip {
    return { onPointerDown: e => { e.stopPropagation(); /* not a tap, not a long press */ setDrag({ pid, group, id: t.id, ids }); } };
  }

  // The drag is followed on the window, not on the grip: React moves the
  // dragged tile in the DOM while it travels, and a moved node loses its
  // pointer capture.
  const dragRef = useRef(drag); dragRef.current = drag;
  const dropRef = useRef<() => void>(() => {});
  const dragOn = !!drag;
  useEffect(() => {
    if (!dragOn) return;
    const move = (e: PointerEvent) => {
      const d = dragRef.current;
      const list = d && document.querySelector<HTMLElement>(`[data-fb-col="${d.pid}"]`);
      if (!d || !list) return;
      // the tile's new place: after every other tile whose middle is above the finger
      const others = [...list.querySelectorAll<HTMLElement>(`[data-fb-tile="${d.group}"]`)].filter(el => el.dataset.fbId !== String(d.id));
      const to = others.filter(el => { const r = el.getBoundingClientRect(); return r.top + r.height / 2 < e.clientY; }).length;
      const rest = d.ids.filter(id => id !== d.id);
      const next = [...rest.slice(0, to), d.id, ...rest.slice(to)];
      if (next.some((id, i) => id !== d.ids[i])) setDrag({ ...d, ids: next });
      const box = list.getBoundingClientRect();                   // a long column scrolls along
      if (e.clientY < box.top + 48) list.scrollBy({ top: -14 });
      else if (e.clientY > box.bottom - 48) list.scrollBy({ top: 14 });
    };
    const up = () => dropRef.current();
    const cancel = () => setDrag(null);
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", cancel);
    return () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); window.removeEventListener("pointercancel", cancel); };
  }, [dragOn]);

  async function dropTile() {
    const d = drag;
    setDrag(null);
    if (!d || !feed) return;
    const col = feed.tasks.filter(t => t.assignee_ids.includes(d.pid));
    const part = (g: Group | "done") => ordered(col.filter(t => g === "routine" ? t.routine : g === "done" ? !t.routine && t.done : !t.routine && !t.done), d.pid).map(t => t.id);
    const before = [...part("routine"), ...part("open"), ...part("done")];
    const ids = [...(d.group === "routine" ? d.ids : part("routine")), ...(d.group === "open" ? d.ids : part("open")), ...part("done")];
    if (ids.every((id, i) => id === before[i])) return;
    const at = new Map(ids.map((id, i) => [id, i]));
    setFeed(f => f && { ...f, tasks: f.tasks.map(t => at.has(t.id) ? { ...t, positions: { ...t.positions, [d.pid]: at.get(t.id)! } } : t) });
    try { await api.put("/api/ambient/board/order", { user_id: d.pid, task_ids: ids }); } catch {}
    await load();
  }

  dropRef.current = () => { void dropTile(); };

  async function joinBoard() {
    try { await api.patch("/api/users/me/kiosk-agenda-consent", { consent: true }); await load(); } catch {}
  }

  if (!feed) return <div className="absolute inset-0 grid place-items-center" style={{ background: tokens.bg }}><Loader2 className="w-8 h-8 animate-spin" style={{ color: tokens.muted }} /></div>;

  const showWeek = mode !== "tasks";
  const showPeople = mode !== "calendar";
  const todayEvents = eventsByDay.get(feed.today) || [];
  const thisWeek = Array.from({ length: 7 }, (_, i) => addDays(feed.week_start, i));

  return (
    <div className={cn("select-none", narrow ? "min-h-full" : "absolute inset-0")} style={{ background: tokens.bg, color: tokens.ink, fontFamily: BODY, transition: "background .8s ease, color .8s ease" }}>
      <style>{`
        .fb-pop { animation: fb-pop .45s cubic-bezier(.2,.9,.3,1.4); }
        @keyframes fb-pop { 0% { transform: scale(1); } 40% { transform: scale(1.04); } 100% { transform: scale(1); } }
        .fb-scroll { scrollbar-width: thin; scrollbar-color: color-mix(in srgb, ${tokens.muted} 55%, transparent) transparent; }
        .fb-running { animation: fb-run 2.4s ease-in-out infinite; }
        @keyframes fb-run { 0%, 100% { box-shadow: 0 0 0 0 transparent; } 50% { box-shadow: 0 0 0 4px color-mix(in srgb, currentColor 18%, transparent); } }
        @media (prefers-reduced-motion: reduce) { .fb-running { animation: none; } }
        .fb-grip { touch-action: none; cursor: grab; }
        .fb-dragging { z-index: 2; transform: scale(1.02); cursor: grabbing; }
        .fb-title { overflow-wrap: anywhere; hyphens: auto; text-wrap: pretty; }
        @media (prefers-reduced-motion: reduce) { .fb-pop { animation: none; } }
      `}</style>
      <div className={cn("grid gap-4", narrow ? "p-3 pb-32 content-start" : "h-full p-5 min-w-0")}
           style={narrow ? undefined : { gridTemplateColumns: "minmax(0, 1fr)", gridTemplateRows: showWeek && showPeople ? "auto auto minmax(0, 1fr)" : "auto minmax(0, 1fr)" }}>

        {/* header: date, next up, legend, clock — and, in every mode, the
            offer to a signed-in person who is not on the board yet */}
        <div className="grid gap-3 min-w-0" style={{ gridTemplateColumns: "minmax(0, 1fr)" }}>
        {offerJoin && currentUserId && !feed.people.some(p => p.id === currentUserId) && (
          <div className="rounded-2xl px-4 py-3 flex items-center justify-between gap-3 flex-wrap text-[14px]" style={{ background: tokens.card, boxShadow: `inset 0 0 0 1px ${tokens.line}` }}>
            <span className="text-[15px]"><b>Du stehst noch nicht auf der Familientafel.</b> Wer hier steht, zeigt dem Haushalt seine Termine und Aufgaben des Tages.</span>
            <button onClick={joinBoard} className="rounded-full px-5 py-2 text-[15px] font-bold text-white" style={{ background: "#2f9e64" }}>Mich anzeigen</button>
          </div>
        )}
        <header className="flex items-start justify-between gap-4 flex-wrap min-w-0">
          <div className="min-w-0">
            <h1 className="text-[34px] leading-none font-extrabold tracking-tight" style={{ fontFamily: DISPLAY }}>
              {new Date(feed.today + "T00:00:00").toLocaleDateString("de-DE", { weekday: "long", day: "numeric", month: "long" })}
            </h1>
          </div>
          <div className="flex items-center gap-5 flex-wrap">
            {/* legend = calendar picker: tap a name for that person's week,
                several for a joint view, "Alle" for everybody */}
            <div className="flex gap-1.5 text-[13px] flex-wrap items-center" role="group" aria-label="Kalender auswählen">
              {(() => {
                const active = people.filter(p => picked.has(p.id));
                const all = active.length === 0;
                return (
                  <>
                    {people.map(p => {
                      const on = picked.has(p.id);
                      return (
                        <button key={p.id} onClick={() => pickCalendar(p.id)} aria-pressed={on}
                                title={on ? `${p.first_name || p.name} ausblenden` : `Kalender von ${p.first_name || p.name} zeigen`}
                                className="flex items-center gap-1.5 rounded-full pl-1 pr-3 py-1 transition"
                                style={on
                                  ? { background: `color-mix(in srgb, ${p.color} ${dim ? 30 : 18}%, ${tokens.card})`, boxShadow: `inset 0 0 0 2px ${p.color}`, color: tokens.ink, fontWeight: 800 }
                                  : { background: "transparent", boxShadow: `inset 0 0 0 1px ${tokens.line}`, color: tokens.muted, opacity: all ? 1 : 0.5 }}>
                          <PersonAvatar name={p.name} color={p.color} avatarUrl={p.avatar_url} size={26} />
                          {p.first_name || p.name}
                          {on && <Check className="w-3.5 h-3.5" strokeWidth={3} style={{ color: p.color }} />}
                        </button>
                      );
                    })}
                    <button onClick={() => pickCalendar(null)} aria-pressed={all} title="Alle Kalender zeigen"
                            className="flex items-center gap-1.5 rounded-full px-3 py-1 transition" style={{ minHeight: 34,
                              ...(all ? { background: tokens.ink, color: tokens.bg, fontWeight: 800 }
                                      : { boxShadow: `inset 0 0 0 1px ${tokens.line}`, color: tokens.muted }) }}>
                      <i className="w-2.5 h-2.5 rounded-full" style={{ background: all ? tokens.bg : SHARED }} />Alle
                    </button>
                  </>
                );
              })()}
            </div>
            <div className="text-[40px] leading-none font-bold tabular-nums" style={{ fontFamily: DISPLAY }}>{now.slice(11, 16)}</div>
          </div>
        </header>
        </div>

        {/* week strip: this week and next, arrows or a swipe */}
        {showWeek && (
          <section className={cn("relative min-w-0 flex flex-col", mode === "calendar" ? "min-h-0" : "")}
                   onPointerDown={e => { swipe.current = { x: e.clientX, y: e.clientY }; }}
                   onPointerUp={e => { const s = swipe.current; swipe.current = null; if (!s) return; const dx = e.clientX - s.x; if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(e.clientY - s.y)) setWeekOffset(o => dx < 0 ? Math.min(1, o + 1) : Math.max(0, o - 1)); }}>
            <div className="flex items-center justify-between mb-1.5 text-[12px] font-bold tracking-[.08em] uppercase" style={{ color: tokens.muted }}>
              <span>{weekOffset === 0 ? "Diese Woche" : "Nächste Woche"} · {fmtDay(days[0])} – {fmtDay(days[6])}
                {people.some(p => picked.has(p.id)) && <span className="normal-case tracking-normal"> · nur {people.filter(p => picked.has(p.id)).map(p => p.first_name || p.name).join(" + ")}</span>}</span>
              <span className="flex gap-1">
                <button onClick={() => setWeekOffset(0)} disabled={weekOffset === 0} className="p-1 rounded-full disabled:opacity-30" style={{ background: tokens.soft }} aria-label="Diese Woche"><ChevronLeft className="w-4 h-4" /></button>
                <button onClick={() => setWeekOffset(1)} disabled={weekOffset === 1} className="p-1 rounded-full disabled:opacity-30" style={{ background: tokens.soft }} aria-label="Nächste Woche"><ChevronRight className="w-4 h-4" /></button>
              </span>
            </div>
            <div className={cn("grid gap-2.5 min-w-0", mode === "calendar" ? "flex-1 min-h-0" : "")} style={{ gridTemplateColumns: narrow ? "repeat(2, minmax(0, 1fr))" : "repeat(7, minmax(0, 1fr))" }}>
              {days.map((d, i) => {
                const today = d === feed.today, past = d < feed.today;
                const evs = eventsByDay.get(d) || [];
                return (
                  <div key={d}
                       className={cn("rounded-2xl border flex flex-col gap-1.5 overflow-hidden", mode === "calendar" ? "min-h-0" : "min-h-[8.5rem]")}
                       style={{ background: today ? tokens.card : dim ? tokens.soft : "rgba(255,255,255,.65)", borderColor: today ? tokens.ink : tokens.line,
                                boxShadow: today ? `inset 0 0 0 1px ${tokens.ink}, 0 10px 24px -18px rgba(31,36,48,.5)` : undefined, opacity: past ? 0.55 : 1, padding: "10px 10px 12px" }}>
                    <div className="flex justify-between items-baseline">
                      <span className="font-extrabold text-[16px]" style={{ fontFamily: DISPLAY }}>{WD[i]}</span>
                      <span className={cn("text-[13px] font-bold rounded-full px-1.5", today && "text-white")} style={today ? { background: tokens.ink } : { color: tokens.muted }}>{d.slice(8, 10)}.</span>
                    </div>
                    <div className="flex flex-col gap-1.5 overflow-y-auto fb-scroll">
                      {evs.length === 0 && <span className="text-[12px]" style={{ color: tokens.muted }}>frei</span>}
                      {evs.map(e => {
                        const p = e.owner_id ? byId.get(e.owner_id) : undefined;
                        const c = e.shared ? SHARED : (p?.color || SHARED);
                        return (
                          <button key={e.id} onClick={() => setShownEvent(e)} title="Antippen: Termin ansehen"
                                className="flex items-center gap-1.5 text-left text-[12.5px] leading-tight rounded-lg pl-2 pr-1.5 py-1 tabular-nums shrink-0"
                                style={{ borderLeft: `4px solid ${c}`, background: `color-mix(in srgb, ${c} ${dim ? 22 : 13}%, ${tokens.card})` }}>
                            {!e.all_day && <b className="shrink-0">{hhmm(e.starts_at)}</b>}
                            <span className="truncate">{e.title}</span>
                            {p && !e.shared && <span className="ml-auto shrink-0"><PersonAvatar name={p.name} color={p.color} avatarUrl={p.avatar_url} size={16} /></span>}
                          </button>
                        );
                      })}
                    </div>
                  </div>
                );
              })}
            </div>
          </section>
        )}

        {/* people */}
        {showPeople && (
          <section className={cn("grid gap-4 min-w-0", narrow ? "" : "min-h-0")} style={{ gridTemplateColumns: narrow ? "1fr" : `repeat(${Math.max(1, people.length)}, minmax(0, 1fr))` }}>
            {people.map(p => {
              const mine = feed.tasks.filter(t => t.assignee_ids.includes(p.id));
              // a ticked routine already has its next instance: that one waits for its day
              const tickedToday = new Set(mine.filter(t => t.routine && t.done).map(t => t.title));
              const routines = ordered(mine.filter(t => t.routine && !(!t.done && tickedToday.has(t.title) && !!t.due_date && t.due_date > feed.today)), p.id, "routine");
              const open = ordered(mine.filter(t => !t.routine && !t.done), p.id, "open");
              const done = ordered(mine.filter(t => !t.routine && t.done), p.id);
              const total = mine.length, finished = mine.filter(t => t.done).length;
              const ratio = total ? finished / total : 0;
              const isMe = currentUserId === p.id;
              const isChild = (p.role || "").toLowerCase() === "restricted";
              const mayTick = isMe || (isParent && isChild);
              const mayAdd = !!currentUserId && (isMe || isParent);
              const tile = (t: Task, group?: Group, ids?: number[]) => ({
                t, p, tokens, dim, busy: busy === t.id, pop: pop === t.id, locked: lockOthers && !mayTick, today: feed.today,
                onTap: () => toggle(t, p), onHold: () => toggleTimer(t, p),
                canEdit: mayTick, onEdit: () => setEditing({ pid: p.id, id: t.id }),
                group, dragging: drag?.id === t.id && drag.pid === p.id,
                grip: mayTick && group && ids && ids.length > 1 ? grip(p.id, group, t, ids) : undefined,
              });
              const myEvents = todayEvents.filter(e => e.owner_id === p.id && !e.all_day);
              const nextEv = myEvents.find(e => e.starts_at.slice(0, 16) >= now.slice(0, 16));
              const busyMin = myEvents.reduce((acc, e) => acc + Math.max(0, Math.min(WORK_END, e.ends_at ? mins(e.ends_at) : mins(e.starts_at) + 60) - Math.max(WORK_START, mins(e.starts_at))), 0);
              const freeMin = Math.max(0, WORK_END - WORK_START - busyMin);
              const r = 31, circ = 2 * Math.PI * r;
              return (
                <div key={p.id} className={cn("rounded-[24px] flex flex-col gap-3", narrow ? "" : "min-h-0")}
                     style={{ background: `color-mix(in srgb, ${p.color} ${dim ? 12 : 8}%, ${tokens.bg})`, padding: "16px 14px 14px", boxShadow: `inset 0 0 0 1px color-mix(in srgb, ${p.color} 22%, transparent)` }}>
                  <button className="flex items-center gap-3 text-left" onClick={() => onNeedSignIn(p)} title={isMe ? "Angemeldet" : "Antippen zum Anmelden"}>
                    <span className="relative inline-block shrink-0" style={{ width: 70, height: 70 }}>
                      <svg width="70" height="70" className="absolute inset-0 -rotate-90">
                        <circle cx="35" cy="35" r={r} fill="none" stroke={tokens.line} strokeWidth="4" />
                        <circle cx="35" cy="35" r={r} fill="none" stroke={p.color} strokeWidth="4" strokeLinecap="round"
                                strokeDasharray={`${circ * ratio} ${circ}`} style={{ transition: "stroke-dasharray .6s ease" }} />
                      </svg>
                      <span className="absolute inset-[7px]"><PersonAvatar name={p.name} color={p.color} avatarUrl={p.avatar_url} size={56} /></span>
                    </span>
                    <div className="min-w-0">
                      <div className="text-[26px] leading-none font-extrabold truncate" style={{ fontFamily: DISPLAY }}>{p.first_name || p.name}</div>
                      <div className="mt-1.5 flex flex-wrap gap-1.5 text-[12px]">
                        <span className="rounded-full px-2 py-0.5 font-bold" style={{ background: tokens.card, color: tokens.ink }}>{finished}/{total} erledigt</span>
                        {nextEv
                          ? <span className="rounded-full px-2 py-0.5" style={{ background: tokens.card, color: tokens.muted }}>{hhmm(nextEv.starts_at)} {nextEv.title}</span>
                          : <span className="rounded-full px-2 py-0.5" style={{ background: tokens.card, color: tokens.muted }}>{freeMin >= WORK_END - WORK_START ? "heute keine Termine" : `${Math.floor(freeMin / 60)} Std. frei`}</span>}
                        {isMe && <span className="rounded-full px-2 py-0.5 font-bold text-white" style={{ background: p.color }}>angemeldet</span>}
                        {!mayTick && lockOthers && <span className="rounded-full px-2 py-0.5 flex items-center gap-1" style={{ background: tokens.card, color: tokens.muted }}><Lock className="w-3 h-3" /> antippen</span>}
                      </div>
                    </div>
                  </button>

                  <div data-fb-col={p.id} className={cn("flex flex-col gap-2 pr-1", narrow ? "" : "flex-1 min-h-0 overflow-y-auto overscroll-contain fb-scroll")} style={{ touchAction: "pan-y" }}>
                    {routines.length > 0 && <Label muted={tokens.muted}>Routine</Label>}
                    {routines.map(t => (
                      <Card key={t.id} {...tile(t, "routine", routines.map(x => x.id))} week={thisWeek} log={feed.routine_log} />
                    ))}
                    {open.length > 0 && routines.length > 0 && <Label muted={tokens.muted}>Aufgaben</Label>}
                    {open.map(t => <Card key={t.id} {...tile(t, "open", open.map(x => x.id))} />)}
                    {mayAdd && (adding === p.id ? (
                      <form onSubmit={e => { e.preventDefault(); void addTask(p); }} className="grid gap-2 shrink-0"
                            onBlur={e => { if (!draft.trim() && !e.currentTarget.contains(e.relatedTarget as Node | null)) setAdding(null); }}>
                        <div className="flex gap-2">
                          <input autoFocus value={draft} onChange={e => setDraft(e.target.value)}
                                 placeholder={isMe ? "Neue Aufgabe" : `Neue Aufgabe für ${p.first_name || p.name}`}
                                 className="flex-1 min-w-0 rounded-2xl px-3 py-2.5 text-[15px] outline-none select-text"
                                 style={{ background: tokens.card, color: tokens.ink, boxShadow: `inset 0 0 0 2px ${p.color}` }} />
                          <button type="submit" className="rounded-2xl px-3 font-bold text-white" style={{ background: p.color }}>OK</button>
                        </div>
                        {/* for when: today, tomorrow or any day */}
                        <div className="flex gap-1.5 flex-wrap items-center text-[13px]" role="group" aria-label="Für wann">
                          {[{ d: "", l: "Heute" }, { d: addDays(feed.today, 1), l: "Morgen" }].map(o => {
                            const on = (draftDay || "") === o.d || (o.d === "" && draftDay === feed.today);
                            return <button key={o.l} type="button" aria-pressed={on} onClick={() => setDraftDay(o.d)} className="rounded-full px-3 py-1.5 font-bold"
                                           style={on ? { background: p.color, color: "#fff" } : { background: tokens.card, color: tokens.muted, boxShadow: `inset 0 0 0 1px ${tokens.line}` }}>{o.l}</button>;
                          })}
                          <input type="date" value={draftDay || feed.today} min={feed.today} onChange={e => setDraftDay(e.target.value)} aria-label="Datum"
                                 className="rounded-full px-3 py-1.5 outline-none" style={{ background: tokens.card, color: tokens.ink, boxShadow: `inset 0 0 0 1px ${draftDay && draftDay !== feed.today && draftDay !== addDays(feed.today, 1) ? p.color : tokens.line}` }} />
                        </div>
                      </form>
                    ) : (
                      <button onClick={() => { setAdding(p.id); setDraft(""); setDraftDay(""); }}
                              className="rounded-2xl px-3 py-2 text-[14px] flex items-center gap-1.5 self-start"
                              style={{ color: tokens.muted, boxShadow: `inset 0 0 0 1px ${tokens.line}` }}>
                        <Plus className="w-4 h-4" /> Aufgabe
                      </button>
                    ))}
                    {mine.length === 0 && (
                      <div className="rounded-2xl p-4 text-[14px]" style={{ background: tokens.card, color: tokens.muted }}>
                        Nichts offen für heute.{nextEv ? ` Um ${hhmm(nextEv.starts_at)}: ${nextEv.title}.` : " Ein freier Tag."}
                      </div>
                    )}
                    {done.length > 0 && (
                      <div className="mt-1">
                        <button onClick={() => setOpenDone(s => ({ ...s, [p.id]: !s[p.id] }))}
                                className="flex items-center gap-1.5 text-[12px] font-bold tracking-[.06em] uppercase" style={{ color: tokens.muted }}>
                          <ChevronDown className={cn("w-4 h-4 transition-transform", !openDone[p.id] && "-rotate-90")} /> Erledigt ({done.length})
                        </button>
                        {openDone[p.id] && (
                          <div className="mt-2 flex flex-col gap-2">
                            {done.map(t => <Card key={t.id} {...tile(t)} />)}
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                </div>
              );
            })}
            {people.length === 0 && (
              <div className="rounded-[24px] border border-dashed p-6 text-[14px]" style={{ borderColor: tokens.line, color: tokens.muted }}>
                Noch niemand auf der Wand. Jede Person schaltet sich unter Settings → You → „Show me on the household wall“ frei.
              </div>
            )}
          </section>
        )}
      </div>
      {shownEvent && <EventDetails ev={shownEvent} person={shownEvent.shared || !shownEvent.owner_id ? undefined : byId.get(shownEvent.owner_id)} shared={SHARED} tokens={tokens} onClose={() => setShownEvent(null)} />}
      {(() => {
        const t = editing && feed.tasks.find(x => x.id === editing.id);
        const p = editing && byId.get(editing.pid);
        return t && p ? <TaskDialog task={t} person={p} today={feed.today} tokens={tokens} onSave={c => saveTask(t, c)} onClose={() => setEditing(null)} /> : null;
      })()}
    </div>
  );
}

function Label({ children, muted }: { children: React.ReactNode; muted: string }) {
  return <div className="text-[11.5px] tracking-[.08em] uppercase font-bold mt-1" style={{ color: muted }}>{children}</div>;
}

function Card({ t, p, tokens, dim, busy, pop, locked, onTap, onHold, today, week, log, canEdit, onEdit, group, dragging, grip }: {
  t: Task; p: Person; tokens: Tokens; dim: boolean; busy: boolean; pop: boolean; locked: boolean; onTap: () => void; onHold: () => void; today: string;
  week?: string[]; log?: LogRow[];
  /** double tap opens the to-do; a single tap then waits a moment for the second one */
  canEdit: boolean; onEdit: () => void;
  /** the grip sorts the tile within its group */
  group?: Group; dragging: boolean; grip?: Grip;
}) {
  // Timer: started_at is naive UTC (as in the Tasks app); minutes so far
  // = what earlier runs folded into actual_minutes + the live run.
  const running = !!t.started_at && !t.done;
  const [tick, setTick] = useState(0);
  useEffect(() => { if (!running) return; const i = setInterval(() => setTick(x => x + 1), 1000); return () => clearInterval(i); }, [running]);
  void tick;
  const startedMs = t.started_at ? new Date(/[zZ]|[+-]\d{2}:?\d{2}$/.test(t.started_at) ? t.started_at : t.started_at.replace(" ", "T") + "Z").getTime() : 0;
  const liveSec = running ? Math.max(0, Math.floor((Date.now() - startedMs) / 1000)) : 0;
  const totalMin = (t.actual_minutes || 0) + liveSec / 60;
  const clock = `${Math.floor(totalMin)}:${String(Math.floor((totalMin % 1) * 60)).padStart(2, "0")}`;
  // Long press (500 ms) toggles the timer; a moved finger is a scroll.
  const lp = useRef<{ timer: number | null; fired: boolean; x: number; y: number }>({ timer: null, fired: false, x: 0, y: 0 });
  const lpCancel = () => { if (lp.current.timer) { clearTimeout(lp.current.timer); lp.current.timer = null; } };
  const lpDown = (e: React.PointerEvent) => {
    lp.current = { timer: window.setTimeout(() => { lp.current.fired = true; lp.current.timer = null; try { (navigator as any).vibrate?.(15); } catch {} onHold(); }, 500), fired: false, x: e.clientX, y: e.clientY };
  };
  const lpMove = (e: React.PointerEvent) => { if (lp.current.timer && (Math.abs(e.clientX - lp.current.x) > 10 || Math.abs(e.clientY - lp.current.y) > 10)) lpCancel(); };
  const single = useRef<number | null>(null);
  useEffect(() => () => { if (single.current) clearTimeout(single.current); }, []);
  // letting go of the grip over the tile is a click on the tile: not a tick
  const justDragged = useRef(false);
  useEffect(() => {
    if (dragging) { justDragged.current = true; return; }
    const i = setTimeout(() => { justDragged.current = false; }, 350);
    return () => clearTimeout(i);
  }, [dragging]);
  const tap = () => {
    if (lp.current.fired) { lp.current.fired = false; return; }
    if (justDragged.current) return;
    if (!canEdit) { onTap(); return; }
    if (single.current) { clearTimeout(single.current); single.current = null; onEdit(); return; }
    single.current = window.setTimeout(() => { single.current = null; onTap(); }, 280);
  };
  const due = !!t.due_date && t.due_date <= today && !t.done;
  const overdue = !!t.due_date && t.due_date < today && !t.done;
  const Icon = t.routine ? routineIcon(t.title) : null;
  const dots = t.routine && week
    ? week.map(d => ({ d, on: (log || []).some(l => l.title === t.title && l.user_id === p.id && l.day === d) || (d === today && t.done), future: d > today }))
    : null;
  return (
    <button onClick={tap} disabled={busy} data-fb-tile={group} data-fb-id={t.id}
            onPointerDown={lpDown} onPointerMove={lpMove} onPointerUp={lpCancel} onPointerLeave={lpCancel} onPointerCancel={lpCancel}
            onContextMenu={e => e.preventDefault()}
            title={[canEdit ? "Doppeltipp: bearbeiten" : "", t.done ? "" : running ? "Lange drücken: Zeit stoppen" : "Lange drücken: Zeit starten"].filter(Boolean).join(" · ") || undefined}
            className={cn("relative grid items-center gap-3 rounded-[16px] text-left overflow-hidden shrink-0", pop && "fb-pop", t.done && "opacity-80", running && "fb-running", dragging && "fb-dragging")}
            style={{ gridTemplateColumns: grip ? "34px minmax(0, 1fr) auto 28px" : "34px minmax(0, 1fr) auto", padding: grip ? "12px 4px 12px 14px" : "12px 12px 12px 14px", background: tokens.card,
                     border: running || dragging ? `2px solid ${p.color}` : `1px solid color-mix(in srgb, ${p.color} ${dim ? 35 : 25}%, ${tokens.line})`,
                     boxShadow: dragging ? "0 14px 30px -12px rgba(31,36,48,.55)" : dim ? "0 1px 0 rgba(0,0,0,.3)" : "0 1px 2px rgba(31,36,48,.05), 0 8px 20px -14px rgba(31,36,48,.35)" }}>
      <span className="absolute left-0 top-0 bottom-0 w-[5px]" style={{ background: t.done ? "#2f9e64" : p.color }} />
      <span className={cn("w-[30px] h-[30px] rounded-[10px] border-[2.5px] grid place-items-center text-white transition-colors", t.done && "bg-[#2f9e64] border-[#2f9e64]")}
            style={t.done ? undefined : { borderColor: locked ? tokens.line : p.color, background: locked ? tokens.soft : "transparent" }}>
        {busy ? <Loader2 className="w-4 h-4 animate-spin" style={{ color: tokens.muted }} /> : t.done ? <Check className="w-[18px] h-[18px]" strokeWidth={3} /> : locked ? <Lock className="w-3.5 h-3.5" style={{ color: tokens.muted }} /> : null}
      </span>
      <span className="min-w-0">
        <span lang="de" className={cn("fb-title block font-bold", t.title.length > 48 ? "text-[15px] leading-snug" : "text-[17px] leading-tight", t.done && "line-through")} style={{ color: t.done ? tokens.muted : tokens.ink }}>
          {Icon && <Icon className="inline w-4 h-4 mr-1.5 -mt-0.5" style={{ color: p.color }} />}{t.title}
        </span>
        <span className="mt-1 flex flex-wrap items-center gap-1.5 text-[12px]" style={{ color: tokens.muted }}>
          {t.category && <span className="rounded-full px-1.5 py-[1px] font-semibold" style={{ background: tokens.soft }}>{t.category}</span>}
          {t.recurrence_rule && <span className="flex items-center gap-1"><Repeat className="w-3 h-3" />{repeatLabel(t.recurrence_rule)}</span>}
          {running
            ? <span className="flex items-center gap-1 rounded-full px-2 py-[1px] font-bold tabular-nums text-white" style={{ background: p.color }}><Timer className="w-3 h-3" />{clock}{t.estimated_minutes ? ` / ${t.estimated_minutes} Min.` : ""}</span>
            : <>
                {t.estimated_minutes ? <span>{t.estimated_minutes} Min.</span> : null}
                {(t.actual_minutes || 0) > 0 && <span className="flex items-center gap-1 tabular-nums"><Timer className="w-3 h-3" />{Math.round(t.actual_minutes || 0)} Min. gebraucht</span>}
              </>}
          {overdue && <span className="flex items-center gap-1 font-bold" style={{ color: "#e0486b" }}><i className="w-1.5 h-1.5 rounded-full" style={{ background: "#e0486b" }} />überfällig seit {fmtDay(t.due_date!)}</span>}
          {dots && (
            <span className="flex items-center gap-1 ml-auto" title="Diese Woche">
              {dots.map(w => <i key={w.d} className="w-2.5 h-2.5 rounded-full" style={{ background: w.on ? p.color : w.future ? "transparent" : tokens.line, border: w.future ? `1px solid ${tokens.line}` : "none" }} />)}
            </span>
          )}
        </span>
      </span>
      <span className="text-[18px] font-bold tabular-nums whitespace-nowrap" style={{ fontFamily: DISPLAY, color: t.done ? "#2f9e64" : due ? "#e0486b" : tokens.muted }}>
        {t.done ? (t.done_at ? hhmm(t.done_at) : "✓") : due ? "heute" : t.due_date ? fmtDay(t.due_date) : ""}
      </span>
      {grip && (
        <span {...grip} onClick={e => e.stopPropagation()} role="img" aria-label="Ziehen, um die Reihenfolge zu ändern" title="Ziehen: Reihenfolge ändern"
              className="fb-grip self-stretch grid place-items-center -my-3" style={{ color: tokens.muted }}>
          <GripVertical className="w-5 h-5" />
        </span>
      )}
    </button>
  );
}
