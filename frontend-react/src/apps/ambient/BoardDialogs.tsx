/**
 * The family board's two small windows: a to-do's details (title, day,
 * repetition; opened by a double tap on a tile) and an appointment's
 * details (read only; opened by a tap on a calendar entry). Both take
 * the board's tokens so they follow the day and the night palette.
 */
import { useEffect, useState } from "react";
import { CalendarDays, Clock, MapPin, Repeat, StickyNote, X } from "lucide-react";
import { PersonAvatar } from "@/components/PersonAvatar";

export interface DialogTokens { bg: string; card: string; ink: string; muted: string; line: string; soft: string }
interface PersonLook { name: string; first_name: string; color: string; avatar_url: string | null }

const DISPLAY = '"Nunito", "Segoe UI", system-ui, sans-serif';

/** What the backend's small recurrence grammar understands (tasks_recurrence.py). */
export const REPEATS: Array<{ rule: string; label: string }> = [
  { rule: "", label: "Einmalig" },
  { rule: "daily", label: "Täglich" },
  { rule: "every Mon,Tue,Wed,Thu,Fri", label: "Werktags" },
  { rule: "weekly", label: "Wöchentlich" },
  { rule: "every 2 weeks", label: "Alle 2 Wochen" },
  { rule: "monthly", label: "Monatlich" },
  { rule: "yearly", label: "Jährlich" },
];
export function repeatLabel(rule: string | null | undefined): string {
  if (!rule) return "";
  const known = REPEATS.find(r => r.rule.toLowerCase() === rule.trim().toLowerCase());
  return known ? known.label : rule;
}

function Sheet({ tokens, accent, onClose, children, label }: { tokens: DialogTokens; accent: string; onClose: () => void; children: React.ReactNode; label: string }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="fixed inset-0 z-40 grid place-items-center p-4" style={{ background: "rgba(20,22,28,.45)" }} onClick={onClose}>
      <div role="dialog" aria-modal="true" aria-label={label} onClick={e => e.stopPropagation()}
           className="relative w-full max-w-[460px] max-h-full overflow-y-auto rounded-[24px] p-5 select-text"
           style={{ background: tokens.card, color: tokens.ink, boxShadow: `inset 0 0 0 1px ${tokens.line}, 0 30px 60px -20px rgba(0,0,0,.5)`, borderTop: `6px solid ${accent}` }}>
        <button onClick={onClose} aria-label="Schließen" className="absolute right-3 top-3 rounded-full p-2" style={{ background: tokens.soft, color: tokens.muted }}><X className="w-4 h-4" /></button>
        {children}
      </div>
    </div>
  );
}

export function TaskDialog({ task, person, today, tokens, onSave, onClose }: {
  task: { id: number; title: string; due_date: string | null; recurrence_rule?: string };
  person: PersonLook; today: string; tokens: DialogTokens;
  /** only what was changed is handed over */
  onSave: (changes: { title?: string; due_date?: string; recurrence_rule?: string }) => void;
  onClose: () => void;
}) {
  const [title, setTitle] = useState(task.title);
  const [due, setDue] = useState((task.due_date || "").slice(0, 10));
  const [rule, setRule] = useState(task.recurrence_rule || "");
  const custom = !!rule && !REPEATS.some(r => r.rule.toLowerCase() === rule.toLowerCase());
  function submit(e: React.FormEvent) {
    e.preventDefault();
    const changes: { title?: string; due_date?: string; recurrence_rule?: string } = {};
    if (title.trim() && title.trim() !== task.title) changes.title = title.trim();
    if (due !== (task.due_date || "").slice(0, 10)) changes.due_date = due;
    if (rule !== (task.recurrence_rule || "")) changes.recurrence_rule = rule;
    // a repetition counts on from the task's day: give it one
    if (changes.recurrence_rule && !due) changes.due_date = today;
    onSave(changes);
  }
  const field = { background: tokens.soft, color: tokens.ink, boxShadow: `inset 0 0 0 1px ${tokens.line}` };
  return (
    <Sheet tokens={tokens} accent={person.color} onClose={onClose} label="Aufgabe bearbeiten">
      <form onSubmit={submit} className="grid gap-4">
        <div className="flex items-center gap-2 text-[13px] font-bold" style={{ color: tokens.muted }}>
          <PersonAvatar name={person.name} color={person.color} avatarUrl={person.avatar_url} size={24} /> Aufgabe von {person.first_name || person.name}
        </div>
        <input autoFocus value={title} onChange={e => setTitle(e.target.value)} aria-label="Titel"
               className="rounded-2xl px-3 py-3 text-[18px] font-bold outline-none" style={{ ...field, boxShadow: `inset 0 0 0 2px ${person.color}` }} />
        <label className="grid gap-1.5 text-[13px] font-bold" style={{ color: tokens.muted }}>
          <span className="flex items-center gap-1.5"><CalendarDays className="w-4 h-4" /> Wann</span>
          <span className="flex gap-2 flex-wrap items-center">
            <input type="date" value={due} onChange={e => setDue(e.target.value)} className="rounded-xl px-3 py-2 text-[15px] outline-none" style={field} />
            {due && <button type="button" onClick={() => setDue("")} className="rounded-xl px-3 py-2 text-[13px]" style={field}>ohne Datum</button>}
          </span>
        </label>
        <div className="grid gap-1.5 text-[13px] font-bold" style={{ color: tokens.muted }}>
          <span className="flex items-center gap-1.5"><Repeat className="w-4 h-4" /> Wiederholen</span>
          <div className="flex gap-1.5 flex-wrap" role="group" aria-label="Wiederholen">
            {REPEATS.map(r => {
              const on = r.rule.toLowerCase() === rule.toLowerCase();
              return (
                <button key={r.rule} type="button" aria-pressed={on} onClick={() => setRule(r.rule)} className="rounded-full px-3 py-1.5 text-[14px]"
                        style={on ? { background: person.color, color: "#fff", fontWeight: 800 } : { ...field, fontWeight: 600 }}>{r.label}</button>
              );
            })}
            {custom && <span className="rounded-full px-3 py-1.5 text-[14px] text-white" style={{ background: person.color }}>{rule}</span>}
          </div>
          {rule && <span className="font-normal text-[12.5px]">Nach dem Abhaken erscheint die nächste von selbst.</span>}
        </div>
        <div className="flex justify-end gap-2 pt-1">
          <button type="button" onClick={onClose} className="rounded-2xl px-4 py-2.5 text-[15px] font-bold" style={field}>Abbrechen</button>
          <button type="submit" className="rounded-2xl px-5 py-2.5 text-[15px] font-bold text-white" style={{ background: person.color }}>Speichern</button>
        </div>
      </form>
    </Sheet>
  );
}

export function EventDetails({ ev, person, tokens, shared, onClose }: {
  ev: { title: string; starts_at: string; ends_at: string | null; all_day: boolean; location: string | null; notes?: string; calendar?: string };
  person?: PersonLook; shared: string; tokens: DialogTokens; onClose: () => void;
}) {
  const accent = person?.color || shared;
  const day = (iso: string) => new Date(iso.slice(0, 10) + "T00:00:00").toLocaleDateString("de-DE", { weekday: "long", day: "numeric", month: "long" });
  const sameDay = !ev.ends_at || ev.ends_at.slice(0, 10) === ev.starts_at.slice(0, 10);
  const when = ev.all_day
    ? (sameDay ? "ganztägig" : `bis ${day(ev.ends_at!)}`)
    : `${ev.starts_at.slice(11, 16)}${ev.ends_at ? ` – ${sameDay ? "" : day(ev.ends_at) + ", "}${ev.ends_at.slice(11, 16)}` : ""} Uhr`;
  const Row = ({ Icon, children }: { Icon: typeof Clock; children: React.ReactNode }) => (
    <div className="flex items-start gap-2.5 text-[15px]"><Icon className="w-4 h-4 mt-[3px] shrink-0" style={{ color: tokens.muted }} /><span className="min-w-0 whitespace-pre-wrap" style={{ overflowWrap: "anywhere" }}>{children}</span></div>
  );
  return (
    <Sheet tokens={tokens} accent={accent} onClose={onClose} label="Termin">
      <div className="grid gap-3">
        <div className="flex items-center gap-2 text-[13px] font-bold pr-10" style={{ color: tokens.muted }}>
          {person ? <><PersonAvatar name={person.name} color={person.color} avatarUrl={person.avatar_url} size={24} /> {person.first_name || person.name}</>
                  : <><i className="w-3 h-3 rounded-full" style={{ background: shared }} /> Alle</>}
          {ev.calendar && <span className="font-normal">· {ev.calendar}</span>}
        </div>
        <h2 className="text-[24px] leading-tight font-extrabold" style={{ fontFamily: DISPLAY, overflowWrap: "anywhere" }}>{ev.title}</h2>
        <Row Icon={CalendarDays}>{day(ev.starts_at)}</Row>
        <Row Icon={Clock}>{when}</Row>
        {ev.location && <Row Icon={MapPin}>{ev.location}</Row>}
        {ev.notes && <Row Icon={StickyNote}>{ev.notes}</Row>}
      </div>
    </Sheet>
  );
}
