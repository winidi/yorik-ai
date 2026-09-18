/**
 * FamilyBoard — the wall in Dæly style: the week in person colours on
 * top, today per person below, routines with a week of ticks, tap to
 * tick. Three layouts driven by `mode`: "board" (week + people),
 * "calendar" (the week, full screen), "tasks" (the people, full screen).
 *
 * Data: GET /api/ambient/board (kiosk gate or a signed-in member). Who
 * appears is decided by each person's own "show me on the wall" consent.
 * Ticking needs the active person: on the wall the avatar tap opens the
 * PIN picker (the parent handles that) and the unlock expires; on the
 * /board page the signed-in person ticks their own tiles.
 *
 * Look: near-white paper, person colour only as accent (stripe, ring,
 * chip), big legible type (bundled Nunito + Atkinson Hyperlegible), a
 * warm dark palette after 21:00 so the wall does not glow at night.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Backpack, Bed, BookOpen, Check, ChevronDown, ChevronLeft, ChevronRight, Dog, Loader2, Lock, Sparkles, Utensils } from "lucide-react";
import { api } from "@/lib/api";
import { PersonAvatar } from "@/components/PersonAvatar";
import { cn } from "@/lib/utils";
import "./board-fonts.css";

export type BoardMode = "board" | "calendar" | "tasks";

interface Person { id: string; name: string; first_name: string; color: string; avatar_url: string | null }
interface Ev { id: number; title: string; starts_at: string; ends_at: string | null; all_day: boolean; owner_id: string | null; shared: boolean; location: string | null }
interface Task { id: number; title: string; due_date: string | null; done: boolean; done_at: string | null; assignee_ids: string[]; person: string; category: string; routine: boolean; estimated_minutes: number | null }
interface LogRow { title: string; user_id: string; day: string }
interface Feed { today: string; week_start: string; days: number; people: Person[]; events: Ev[]; tasks: Task[]; routine_log: LogRow[] }
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

function countdown(fromIso: string, toIso: string): string {
  const diff = (new Date(toIso).getTime() - new Date(fromIso).getTime()) / 60000;
  if (diff < -5) return "läuft";
  if (diff < 1) return "jetzt";
  if (diff < 60) return `in ${Math.round(diff)} Min.`;
  const h = Math.floor(diff / 60), m = Math.round(diff % 60);
  return m ? `in ${h} Std. ${m} Min.` : `in ${h} Std.`;
}

export function FamilyBoard({ mode, currentUserId, onNeedSignIn, lockOthers = false }: {
  mode: BoardMode;
  currentUserId: string | null;
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
    for (const e of feed?.events || []) { const k = dayOf(e.starts_at); m.set(k, [...(m.get(k) || []), e]); }
    for (const list of m.values()) list.sort((a, b) => Number(b.all_day) - Number(a.all_day) || a.starts_at.localeCompare(b.starts_at));
    return m;
  }, [feed]);
  const people = useMemo(() => {
    const list = [...(feed?.people || [])];
    if (currentUserId) list.sort((a, b) => Number(b.id === currentUserId) - Number(a.id === currentUserId));
    return list;
  }, [feed, currentUserId]);

  // "Als Nächstes": the next timed event of today, else the first due task
  const next = useMemo(() => {
    if (!feed) return null;
    const todays = (eventsByDay.get(feed.today) || []).filter(e => !e.all_day && e.starts_at.slice(0, 16) >= now.slice(0, 16));
    if (todays.length) {
      const e = todays[0]; const p = e.owner_id ? byId.get(e.owner_id) : undefined;
      return { text: e.title, who: e.shared ? "Alle" : (p?.first_name || p?.name || ""), when: countdown(now, e.starts_at), color: e.shared ? SHARED : (p?.color || SHARED) };
    }
    const due = feed.tasks.find(t => !t.done && !!t.due_date && t.due_date <= feed.today);
    if (due) { const p = byId.get(due.assignee_ids[0]); return { text: due.title, who: p?.first_name || p?.name || "", when: "heute fällig", color: p?.color || SHARED }; }
    return null;
  }, [feed, eventsByDay, now, byId]);

  async function toggle(t: Task, owner: Person) {
    if (!feed) return;
    const mine = currentUserId && t.assignee_ids.includes(currentUserId);
    if (!mine) { onNeedSignIn(owner); return; }
    setBusy(t.id); setPop(t.id);
    try {
      await api.patch(`/api/tasks/${t.id}`, { done: t.done ? 0 : 1 });
      await load();
    } catch {} finally { setBusy(null); setTimeout(() => setPop(null), 500); }
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
        .fb-scroll { scrollbar-width: thin; scrollbar-color: ${tokens.line} transparent; }
        @media (prefers-reduced-motion: reduce) { .fb-pop { animation: none; } }
      `}</style>
      <div className={cn("grid gap-4", narrow ? "p-3 pb-32 content-start" : "h-full p-5 min-w-0")}
           style={narrow ? undefined : { gridTemplateColumns: "minmax(0, 1fr)", gridTemplateRows: showWeek && showPeople ? "auto auto minmax(0, 1fr)" : "auto minmax(0, 1fr)" }}>

        {/* header: date, next up, legend, clock */}
        <header className="flex items-start justify-between gap-4 flex-wrap min-w-0">
          <div className="min-w-0">
            <h1 className="text-[34px] leading-none font-extrabold tracking-tight" style={{ fontFamily: DISPLAY }}>
              {new Date(feed.today + "T00:00:00").toLocaleDateString("de-DE", { weekday: "long", day: "numeric", month: "long" })}
            </h1>
            {next && (
              <div className="mt-2 flex items-center gap-2 text-[15px] flex-wrap">
                <span className="w-2.5 h-2.5 rounded-full" style={{ background: next.color }} />
                <span style={{ color: tokens.muted }}>Als Nächstes</span>
                <b>{next.text}</b>
                {next.who && <span style={{ color: tokens.muted }}>· {next.who}</span>}
                <span className="rounded-full px-2 py-0.5 text-[13px] font-bold" style={{ background: tokens.soft }}>{next.when}</span>
              </div>
            )}
          </div>
          <div className="flex items-center gap-5 flex-wrap">
            <div className="flex gap-3 text-[13px] flex-wrap" style={{ color: tokens.muted }}>
              {feed.people.map(p => (
                <span key={p.id} className="flex items-center gap-1.5">
                  <PersonAvatar name={p.name} color={p.color} avatarUrl={p.avatar_url} size={26} />
                  {p.first_name || p.name}
                </span>
              ))}
              <span className="flex items-center gap-1.5"><i className="w-2.5 h-2.5 rounded-full" style={{ background: SHARED }} />Alle</span>
            </div>
            <div className="text-[40px] leading-none font-bold tabular-nums" style={{ fontFamily: DISPLAY }}>{now.slice(11, 16)}</div>
          </div>
        </header>

        {/* week strip: this week and next, arrows or a swipe */}
        {showWeek && (
          <section className={cn("relative min-w-0 flex flex-col", mode === "calendar" ? "min-h-0" : "")}
                   onPointerDown={e => { swipe.current = { x: e.clientX, y: e.clientY }; }}
                   onPointerUp={e => { const s = swipe.current; swipe.current = null; if (!s) return; const dx = e.clientX - s.x; if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(e.clientY - s.y)) setWeekOffset(o => dx < 0 ? Math.min(1, o + 1) : Math.max(0, o - 1)); }}>
            <div className="flex items-center justify-between mb-1.5 text-[12px] font-bold tracking-[.08em] uppercase" style={{ color: tokens.muted }}>
              <span>{weekOffset === 0 ? "Diese Woche" : "Nächste Woche"} · {fmtDay(days[0])} – {fmtDay(days[6])}</span>
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
                          <span key={e.id} className="flex items-center gap-1.5 text-[12.5px] leading-tight rounded-lg pl-2 pr-1.5 py-1 tabular-nums"
                                style={{ borderLeft: `4px solid ${c}`, background: `color-mix(in srgb, ${c} ${dim ? 22 : 13}%, ${tokens.card})` }}>
                            {!e.all_day && <b className="shrink-0">{hhmm(e.starts_at)}</b>}
                            <span className="truncate">{e.title}</span>
                            {p && !e.shared && <span className="ml-auto shrink-0"><PersonAvatar name={p.name} color={p.color} avatarUrl={p.avatar_url} size={16} /></span>}
                          </span>
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
              const routines = mine.filter(t => t.routine);
              const open = mine.filter(t => !t.routine && !t.done);
              const done = mine.filter(t => !t.routine && t.done);
              const total = mine.length, finished = mine.filter(t => t.done).length;
              const ratio = total ? finished / total : 0;
              const isMe = currentUserId === p.id;
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
                        {!isMe && lockOthers && <span className="rounded-full px-2 py-0.5 flex items-center gap-1" style={{ background: tokens.card, color: tokens.muted }}><Lock className="w-3 h-3" /> antippen</span>}
                      </div>
                    </div>
                  </button>

                  <div className={cn("flex flex-col gap-2 pr-0.5", narrow ? "" : "overflow-y-auto fb-scroll min-h-0")}>
                    {routines.length > 0 && <Label muted={tokens.muted}>Routine</Label>}
                    {routines.map(t => (
                      <Card key={t.id} t={t} p={p} tokens={tokens} dim={dim} busy={busy === t.id} pop={pop === t.id} locked={lockOthers && !isMe} onTap={() => toggle(t, p)}
                            today={feed.today} week={thisWeek} log={feed.routine_log} />
                    ))}
                    {open.length > 0 && routines.length > 0 && <Label muted={tokens.muted}>Aufgaben</Label>}
                    {open.map(t => <Card key={t.id} t={t} p={p} tokens={tokens} dim={dim} busy={busy === t.id} pop={pop === t.id} locked={lockOthers && !isMe} onTap={() => toggle(t, p)} today={feed.today} />)}
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
                            {done.map(t => <Card key={t.id} t={t} p={p} tokens={tokens} dim={dim} busy={busy === t.id} pop={pop === t.id} locked={lockOthers && !isMe} onTap={() => toggle(t, p)} today={feed.today} />)}
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
    </div>
  );
}

function Label({ children, muted }: { children: React.ReactNode; muted: string }) {
  return <div className="text-[11.5px] tracking-[.08em] uppercase font-bold mt-1" style={{ color: muted }}>{children}</div>;
}

function Card({ t, p, tokens, dim, busy, pop, locked, onTap, today, week, log }: {
  t: Task; p: Person; tokens: Tokens; dim: boolean; busy: boolean; pop: boolean; locked: boolean; onTap: () => void; today: string;
  week?: string[]; log?: LogRow[];
}) {
  const due = !!t.due_date && t.due_date <= today && !t.done;
  const overdue = !!t.due_date && t.due_date < today && !t.done;
  const Icon = t.routine ? routineIcon(t.title) : null;
  const dots = t.routine && week
    ? week.map(d => ({ d, on: (log || []).some(l => l.title === t.title && l.user_id === p.id && l.day === d) || (d === today && t.done), future: d > today }))
    : null;
  return (
    <button onClick={onTap} disabled={busy}
            className={cn("relative grid items-center gap-3 rounded-[16px] text-left overflow-hidden", pop && "fb-pop", t.done && "opacity-80")}
            style={{ gridTemplateColumns: "34px minmax(0, 1fr) auto", padding: "12px 12px 12px 14px", background: tokens.card,
                     border: `1px solid color-mix(in srgb, ${p.color} ${dim ? 35 : 25}%, ${tokens.line})`,
                     boxShadow: dim ? "0 1px 0 rgba(0,0,0,.3)" : "0 1px 2px rgba(31,36,48,.05), 0 8px 20px -14px rgba(31,36,48,.35)" }}>
      <span className="absolute left-0 top-0 bottom-0 w-[5px]" style={{ background: t.done ? "#2f9e64" : p.color }} />
      <span className={cn("w-[30px] h-[30px] rounded-[10px] border-[2.5px] grid place-items-center text-white transition-colors", t.done && "bg-[#2f9e64] border-[#2f9e64]")}
            style={t.done ? undefined : { borderColor: locked ? tokens.line : p.color, background: locked ? tokens.soft : "transparent" }}>
        {busy ? <Loader2 className="w-4 h-4 animate-spin" style={{ color: tokens.muted }} /> : t.done ? <Check className="w-[18px] h-[18px]" strokeWidth={3} /> : locked ? <Lock className="w-3.5 h-3.5" style={{ color: tokens.muted }} /> : null}
      </span>
      <span className="min-w-0">
        <span className={cn("block text-[17px] leading-tight font-bold", t.done && "line-through")} style={{ color: t.done ? tokens.muted : tokens.ink }}>
          {Icon && <Icon className="inline w-4 h-4 mr-1.5 -mt-0.5" style={{ color: p.color }} />}{t.title}
        </span>
        <span className="mt-1 flex flex-wrap items-center gap-1.5 text-[12px]" style={{ color: tokens.muted }}>
          {t.category && <span className="rounded-full px-1.5 py-[1px] font-semibold" style={{ background: tokens.soft }}>{t.category}</span>}
          {t.estimated_minutes ? <span>{t.estimated_minutes} Min.</span> : null}
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
    </button>
  );
}
