/**
 * The family board's two small windows: a to-do's details (title, day,
 * repetition; opened by a double tap on a tile) and an appointment's
 * details (read only; opened by a tap on a calendar entry). Both take
 * the board's tokens so they follow the day and the night palette.
 */
import { useEffect, useState } from "react";
import { CalendarDays, Clock, MapPin, Repeat, StickyNote, Trash2, X } from "lucide-react";
import { useTranslation } from "react-i18next";
import { PersonAvatar } from "@/components/PersonAvatar";
import i18n from "@/i18n";
import { formatDate } from "@/i18n/format";

export interface DialogTokens { bg: string; card: string; ink: string; muted: string; line: string; soft: string }
interface PersonLook { name: string; first_name: string; color: string; avatar_url: string | null }

const DISPLAY = '"Nunito", "Segoe UI", system-ui, sans-serif';

/** What the backend's small recurrence grammar understands (tasks_recurrence.py). */
/** `label` is a translation key. */
export const REPEATS: Array<{ rule: string; label: string }> = [
  { rule: "", label: "board.repeat.once" },
  { rule: "daily", label: "board.repeat.daily" },
  { rule: "every Mon,Tue,Wed,Thu,Fri", label: "board.repeat.weekdays" },
  { rule: "weekly", label: "board.repeat.weekly" },
  { rule: "every 2 weeks", label: "board.repeat.every2Weeks" },
  { rule: "monthly", label: "board.repeat.monthly" },
  { rule: "yearly", label: "board.repeat.yearly" },
];
export function repeatLabel(rule: string | null | undefined): string {
  if (!rule) return "";
  const known = REPEATS.find(r => r.rule.toLowerCase() === rule.trim().toLowerCase());
  return known ? i18n.t(known.label) : rule;
}

function Sheet({ tokens, accent, onClose, children, label }: { tokens: DialogTokens; accent: string; onClose: () => void; children: React.ReactNode; label: string }) {
  const { t } = useTranslation();
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="fixed inset-0 z-[800] grid place-items-center p-4" style={{ background: "rgba(20,22,28,.45)" }} onClick={onClose}>
      <div role="dialog" aria-modal="true" aria-label={label} onClick={e => e.stopPropagation()}
           className="relative w-full max-w-[460px] max-h-full overflow-y-auto rounded-[24px] p-5 select-text"
           style={{ background: tokens.card, color: tokens.ink, boxShadow: `inset 0 0 0 1px ${tokens.line}, 0 30px 60px -20px rgba(0,0,0,.5)`, borderTop: `6px solid ${accent}` }}>
        <button onClick={onClose} aria-label={t("common.close")} className="absolute right-3 top-3 rounded-full p-2" style={{ background: tokens.soft, color: tokens.muted }}><X className="w-4 h-4" /></button>
        {children}
      </div>
    </div>
  );
}

export function TaskDialog({ task, person, today, tokens, onSave, onDelete, onClose }: {
  task: { id: number; title: string; due_date: string | null; recurrence_rule?: string };
  person: PersonLook; today: string; tokens: DialogTokens;
  /** only what was changed is handed over */
  onSave: (changes: { title?: string; due_date?: string; recurrence_rule?: string }) => void;
  /** resolves to a sentence for the person when the to-do could not be deleted */
  onDelete: () => Promise<string | null>;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  // Deleting takes a second tap: on the wall a to-do must not vanish by accident.
  const [sure, setSure] = useState(false);
  const [refused, setRefused] = useState<string | null>(null);
  async function remove() {
    if (!sure) { setSure(true); return; }
    setRefused(await onDelete()); setSure(false);
  }
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
    <Sheet tokens={tokens} accent={person.color} onClose={onClose} label={t("board.editTask")}>
      <form onSubmit={submit} className="grid gap-4">
        <div className="flex items-center gap-2 text-[13px] font-bold" style={{ color: tokens.muted }}>
          <PersonAvatar name={person.name} color={person.color} avatarUrl={person.avatar_url} size={24} /> {t("board.taskOf", { name: person.first_name || person.name })}
        </div>
        <input autoFocus value={title} onChange={e => setTitle(e.target.value)} aria-label={t("board.title")}
               className="rounded-2xl px-3 py-3 text-[18px] font-bold outline-none" style={{ ...field, boxShadow: `inset 0 0 0 2px ${person.color}` }} />
        <label className="grid gap-1.5 text-[13px] font-bold" style={{ color: tokens.muted }}>
          <span className="flex items-center gap-1.5"><CalendarDays className="w-4 h-4" /> {t("board.when")}</span>
          <span className="flex gap-2 flex-wrap items-center">
            <input type="date" value={due} onChange={e => setDue(e.target.value)} className="rounded-xl px-3 py-2 text-[15px] outline-none" style={field} />
            {due && <button type="button" onClick={() => setDue("")} className="rounded-xl px-3 py-2 text-[13px]" style={field}>{t("board.noDate")}</button>}
          </span>
        </label>
        <div className="grid gap-1.5 text-[13px] font-bold" style={{ color: tokens.muted }}>
          <span className="flex items-center gap-1.5"><Repeat className="w-4 h-4" /> {t("board.repeatLabel")}</span>
          <div className="flex gap-1.5 flex-wrap" role="group" aria-label={t("board.repeatLabel")}>
            {REPEATS.map(r => {
              const on = r.rule.toLowerCase() === rule.toLowerCase();
              return (
                <button key={r.rule} type="button" aria-pressed={on} onClick={() => setRule(r.rule)} className="rounded-full px-3 py-1.5 text-[14px]"
                        style={on ? { background: person.color, color: "#fff", fontWeight: 800 } : { ...field, fontWeight: 600 }}>{t(r.label)}</button>
              );
            })}
            {custom && <span className="rounded-full px-3 py-1.5 text-[14px] text-white" style={{ background: person.color }}>{rule}</span>}
          </div>
          {rule && <span className="font-normal text-[12.5px]">{t("board.repeatHint")}</span>}
        </div>
        {refused && <div className="text-[13.5px] font-bold" style={{ color: "#e0486b" }}>{refused}</div>}
        <div className="flex justify-end gap-2 pt-1 flex-wrap">
          <button type="button" onClick={remove} onBlur={() => setSure(false)} className="mr-auto rounded-2xl px-4 py-2.5 text-[15px] font-bold flex items-center gap-1.5"
                  style={sure ? { background: "#e0486b", color: "#fff" } : { ...field, color: "#e0486b" }}>
            <Trash2 className="w-4 h-4" /> {sure ? (task.recurrence_rule ? t("board.confirmDeleteRepeat") : t("board.confirmDelete")) : t("common.delete")}
          </button>
          <button type="button" onClick={onClose} className="rounded-2xl px-4 py-2.5 text-[15px] font-bold" style={field}>{t("common.cancel")}</button>
          <button type="submit" className="rounded-2xl px-5 py-2.5 text-[15px] font-bold text-white" style={{ background: person.color }}>{t("common.save")}</button>
        </div>
      </form>
    </Sheet>
  );
}

export function EventDetails({ ev, person, tokens, shared, onClose }: {
  ev: { title: string; starts_at: string; ends_at: string | null; all_day: boolean; location: string | null; notes?: string; calendar?: string };
  person?: PersonLook; shared: string; tokens: DialogTokens; onClose: () => void;
}) {
  const { t } = useTranslation();
  const accent = person?.color || shared;
  const day = (iso: string) => formatDate(iso.slice(0, 10) + "T00:00:00", { weekday: "long", day: "numeric", month: "long" });
  const sameDay = !ev.ends_at || ev.ends_at.slice(0, 10) === ev.starts_at.slice(0, 10);
  const when = ev.all_day
    ? (sameDay ? t("board.allDay") : t("board.untilDay", { day: day(ev.ends_at!) }))
    : t("board.timeRange", { range: `${ev.starts_at.slice(11, 16)}${ev.ends_at ? ` – ${sameDay ? "" : day(ev.ends_at) + ", "}${ev.ends_at.slice(11, 16)}` : ""}` });
  const Row = ({ Icon, children }: { Icon: typeof Clock; children: React.ReactNode }) => (
    <div className="flex items-start gap-2.5 text-[15px]"><Icon className="w-4 h-4 mt-[3px] shrink-0" style={{ color: tokens.muted }} /><span className="min-w-0 whitespace-pre-wrap" style={{ overflowWrap: "anywhere" }}>{children}</span></div>
  );
  return (
    <Sheet tokens={tokens} accent={accent} onClose={onClose} label={t("board.appointment")}>
      <div className="grid gap-3">
        <div className="flex items-center gap-2 text-[13px] font-bold pr-10" style={{ color: tokens.muted }}>
          {person ? <><PersonAvatar name={person.name} color={person.color} avatarUrl={person.avatar_url} size={24} /> {person.first_name || person.name}</>
                  : <><i className="w-3 h-3 rounded-full" style={{ background: shared }} /> {t("board.everyone")}</>}
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
