/**
 * Timetable — the children's school week on the family board: a grid
 * per child, Monday to Friday by period, the same subject always in the
 * same tint, today's column and the running period marked.
 *
 * Data: GET /api/ambient/timetable (kiosk gate or a signed-in member);
 * every child on the wall has one, empty to begin with. A parent or the
 * child itself fills it in right here ("Bearbeiten"): subject and room
 * per cell, the times per period, periods added or taken away at the end.
 *
 * An empty timetable has no card (a child not yet at school), only a
 * line for whoever may start it. With several timetables, the names in
 * the board's legend pick whose to show (`picked`, kept by the board).
 */
import { useCallback, useEffect, useState } from "react";
import { Check, Loader2, Minus, Pencil, Plus } from "lucide-react";
import { api } from "@/lib/api";
import { PersonAvatar } from "@/components/PersonAvatar";
import { cn } from "@/lib/utils";
import type { DialogTokens } from "./BoardDialogs";

interface Period { start: string; end: string }
interface Cell { subject: string; room: string }
interface Plan { periods: Period[]; cells: Record<string, Cell> }
interface Child { id: string; name: string; first_name: string; color: string; avatar_url: string | null; timetable: Plan; filled: boolean }

const DAYS = ["Mo", "Di", "Mi", "Do", "Fr"];
const DISPLAY = '"Nunito", "Segoe UI", system-ui, sans-serif';
const TINTS = ["#e0486b", "#e8833a", "#d4a514", "#2f9e64", "#1c9aa0", "#3b82c4", "#6b5fd3", "#b455c0", "#8a6d4f", "#5d7285"];
const MAX_PERIODS = 12;

function tint(subject: string): string {
  let h = 0;
  for (const ch of subject.trim().toLowerCase()) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return TINTS[h % TINTS.length];
}

export function Timetable({ tokens, dim, narrow, today, now, currentUserId, isParent, picked, onFilled }: {
  tokens: DialogTokens; dim: boolean; narrow: boolean;
  /** YYYY-MM-DD and YYYY-MM-DDTHH:MM of the board's clock */
  today: string; now: string;
  currentUserId: string | null; isParent: boolean;
  /** whose timetable to show; empty = all that are filled in */
  picked: Set<string>;
  /** tells the board who has a timetable, for its legend */
  onFilled: (ids: string[]) => void;
}) {
  const [children, setChildren] = useState<Child[] | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState<Plan | null>(null);
  const [saving, setSaving] = useState(false);
  const [failed, setFailed] = useState(false);

  const load = useCallback(async () => {
    try { setChildren((await api.get<{ people: Child[] }>("/api/ambient/timetable")).people); } catch { setChildren(c => c || []); }
  }, []);
  useEffect(() => { load(); }, [load]);
  const filledKey = (children || []).filter(c => c.filled).map(c => c.id).join(",");
  useEffect(() => { onFilled(filledKey ? filledKey.split(",") : []); }, [filledKey]);   // eslint-disable-line react-hooks/exhaustive-deps
  // keep the wall fresh, but never under someone's hands
  useEffect(() => { if (editing) return; const t = setInterval(load, 5 * 60_000); return () => clearInterval(t); }, [load, editing]);

  const weekday = (new Date(today + "T00:00:00").getDay() + 6) % 7;      // Mon = 0
  const clock = now.slice(11, 16);

  function startEdit(c: Child) {
    setEditing(c.id); setFailed(false);
    setDraft({ periods: c.timetable.periods.map(p => ({ ...p })), cells: Object.fromEntries(Object.entries(c.timetable.cells).map(([k, v]) => [k, { ...v }])) });
  }
  function setCell(key: string, part: Partial<Cell>) {
    setDraft(d => d && { ...d, cells: { ...d.cells, [key]: { ...(d.cells[key] || { subject: "", room: "" }), ...part } } });
  }
  function setPeriod(i: number, part: Partial<Period>) {
    setDraft(d => d && { ...d, periods: d.periods.map((p, j) => j === i ? { ...p, ...part } : p) });
  }
  async function save(c: Child) {
    if (!draft) return;
    setSaving(true); setFailed(false);
    try {
      await api.put(`/api/ambient/timetable/${c.id}`, draft);
      await load();
      setEditing(null); setDraft(null);
    } catch { setFailed(true); } finally { setSaving(false); }
  }

  if (!children) return <div className="grid place-items-center min-h-[12rem]"><Loader2 className="w-7 h-7 animate-spin" style={{ color: tokens.muted }} /></div>;
  if (children.length === 0) return (
    <div className="rounded-[24px] border border-dashed p-6 text-[14px] self-start" style={{ borderColor: tokens.line, color: tokens.muted }}>
      Noch kein Stundenplan: er ist für die Kinder, die auf der Familientafel stehen (Settings → Profile → „Show me on the household wall“).
    </div>
  );

  const field = { background: tokens.card, color: tokens.ink, boxShadow: `inset 0 0 0 1px ${tokens.line}` };
  const mayEditOf = (c: Child) => !!currentUserId && (currentUserId === c.id || isParent);
  // a picked child without a timetable (any more) does not hide everything
  const chosen = children.filter(c => c.filled && picked.has(c.id));
  const shown = children.filter(c => editing === c.id || (c.filled && (chosen.length === 0 || chosen.includes(c))));
  const toStart = children.filter(c => !c.filled && editing !== c.id && mayEditOf(c));
  return (
    <div className={cn("grid gap-3 min-w-0", narrow ? "" : "min-h-0")} style={{ gridTemplateColumns: "minmax(0, 1fr)", gridTemplateRows: narrow ? undefined : "minmax(0, 1fr) auto" }}>
    {shown.length === 0 && (
      <div className="rounded-[24px] border border-dashed p-6 text-[14px] self-start" style={{ borderColor: tokens.line, color: tokens.muted }}>
        Noch kein Stundenplan eingetragen.{toStart.length ? " Unten legst du einen an." : " Eltern oder das Schulkind selbst legen ihn nach dem Anmelden an."}
      </div>
    )}
    {shown.length > 0 && (
    <section className={cn("grid gap-4 min-w-0", narrow ? "" : "min-h-0")} style={{ gridTemplateColumns: narrow ? "minmax(0, 1fr)" : `repeat(${shown.length}, minmax(0, 1fr))` }}>
      {shown.map(c => {
        const edit = editing === c.id && !!draft;
        const plan = edit ? draft! : c.timetable;
        const mayEdit = mayEditOf(c);
        const subjects = [...new Set(Object.values(plan.cells).map(x => x.subject).filter(Boolean))].sort();
        return (
          <div key={c.id} className={cn("rounded-[24px] flex flex-col gap-3 min-w-0", narrow ? "" : "min-h-0")}
               style={{ background: `color-mix(in srgb, ${c.color} ${dim ? 12 : 8}%, ${tokens.bg})`, padding: "16px 14px 14px", boxShadow: `inset 0 0 0 1px color-mix(in srgb, ${c.color} 22%, transparent)` }}>
            <div className="flex items-center gap-3 flex-wrap">
              <PersonAvatar name={c.name} color={c.color} avatarUrl={c.avatar_url} size={44} />
              <div className="text-[24px] leading-none font-extrabold truncate mr-auto" style={{ fontFamily: DISPLAY }}>{c.first_name || c.name}</div>
              {edit ? (
                <>
                  {failed && <span className="text-[13px] font-bold" style={{ color: "#e0486b" }}>Nicht gespeichert</span>}
                  <button onClick={() => { setEditing(null); setDraft(null); }} className="rounded-full px-4 py-2 text-[14px] font-bold" style={field}>Abbrechen</button>
                  <button onClick={() => save(c)} disabled={saving} className="rounded-full px-4 py-2 text-[14px] font-bold text-white flex items-center gap-1.5" style={{ background: c.color }}>
                    {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Check className="w-4 h-4" strokeWidth={3} />} Speichern
                  </button>
                </>
              ) : mayEdit && !editing && (
                <button onClick={() => startEdit(c)} className="rounded-full px-4 py-2 text-[14px] font-bold flex items-center gap-1.5" style={{ ...field, color: tokens.muted }}>
                  <Pencil className="w-3.5 h-3.5" /> {c.filled ? "Bearbeiten" : "Ausfüllen"}
                </button>
              )}
            </div>

            <div className={cn("min-w-0 pr-1 fb-scroll", narrow ? "overflow-x-auto" : "flex-1 min-h-0 overflow-auto overscroll-contain")}>
              <div className="grid gap-1.5" style={{ gridTemplateColumns: `${edit ? "5.6rem" : "3.4rem"} repeat(5, minmax(${edit ? "5.5rem" : "0px"}, 1fr))` }}>
                <span />
                {DAYS.map((d, i) => (
                  <div key={d} className="text-center text-[14px] font-extrabold rounded-full py-0.5" style={{ fontFamily: DISPLAY, ...(i === weekday ? { background: tokens.ink, color: tokens.bg } : { color: tokens.muted }) }}>{d}</div>
                ))}
                {plan.periods.map((p, row) => {
                  const running = !edit && weekday < 5 && !!p.start && !!p.end && clock >= p.start && clock < p.end;
                  return [
                    <div key={`t${row}`} className="flex flex-col justify-center text-[11.5px] leading-tight tabular-nums" style={{ color: tokens.muted }}>
                      <b className="text-[15px]" style={{ color: running ? c.color : tokens.ink, fontFamily: DISPLAY }}>{row + 1}.</b>
                      {edit ? (
                        <>
                          <input type="time" value={p.start} onChange={e => setPeriod(row, { start: e.target.value })} aria-label={`${row + 1}. Stunde Beginn`} className="rounded-md px-1 py-0.5 text-[12px] outline-none select-text" style={field} />
                          <input type="time" value={p.end} onChange={e => setPeriod(row, { end: e.target.value })} aria-label={`${row + 1}. Stunde Ende`} className="mt-0.5 rounded-md px-1 py-0.5 text-[12px] outline-none select-text" style={field} />
                        </>
                      ) : <>{p.start && <span>{p.start}</span>}{p.end && <span>{p.end}</span>}</>}
                    </div>,
                    ...DAYS.map((_, day) => {
                      const key = `${day}-${row}`;
                      const cell = plan.cells[key];
                      if (edit) return (
                        <div key={key} className="grid gap-0.5">
                          <input value={cell?.subject || ""} onChange={e => setCell(key, { subject: e.target.value })} list={`fb-subjects-${c.id}`} maxLength={40}
                                 placeholder="Fach" aria-label={`${DAYS[day]}, ${row + 1}. Stunde, Fach`} className="min-w-0 rounded-lg px-2 py-1.5 text-[14px] font-bold outline-none select-text"
                                 style={{ ...field, boxShadow: `inset 0 0 0 1px ${cell?.subject ? tint(cell.subject) : tokens.line}` }} />
                          <input value={cell?.room || ""} onChange={e => setCell(key, { room: e.target.value })} maxLength={20}
                                 placeholder="Raum" aria-label={`${DAYS[day]}, ${row + 1}. Stunde, Raum`} className="min-w-0 rounded-lg px-2 py-1 text-[12px] outline-none select-text" style={field} />
                        </div>
                      );
                      const col = cell?.subject ? tint(cell.subject) : tokens.line;
                      return (
                        <div key={key} className="rounded-xl px-2 py-1.5 min-h-[3rem] min-w-0 flex flex-col justify-center"
                             style={cell ? { background: `color-mix(in srgb, ${col} ${dim ? 26 : 15}%, ${tokens.card})`, borderLeft: `4px solid ${col}`,
                                             boxShadow: running && day === weekday ? `0 0 0 2px ${c.color}` : undefined, opacity: day === weekday || weekday > 4 ? 1 : 0.82 }
                                         : { background: day === weekday ? tokens.soft : "transparent", boxShadow: `inset 0 0 0 1px ${tokens.line}` }}>
                          {cell && <span lang="de" className="text-[14px] font-bold leading-tight" style={{ overflowWrap: "anywhere", hyphens: "auto" }}>{cell.subject}</span>}
                          {cell?.room && <span className="text-[11.5px]" style={{ color: tokens.muted }}>{cell.room}</span>}
                        </div>
                      );
                    }),
                  ];
                })}
              </div>
              {edit && (
                <div className="mt-3 flex gap-2 flex-wrap text-[13px]">
                  <button onClick={() => setDraft(d => d && d.periods.length < MAX_PERIODS ? { ...d, periods: [...d.periods, { start: "", end: "" }] } : d)} disabled={plan.periods.length >= MAX_PERIODS}
                          className="rounded-full px-3 py-1.5 font-bold flex items-center gap-1 disabled:opacity-40" style={{ ...field, color: tokens.muted }}><Plus className="w-3.5 h-3.5" /> Stunde</button>
                  <button onClick={() => setDraft(d => { if (!d || d.periods.length <= 1) return d; const last = d.periods.length - 1; return { periods: d.periods.slice(0, last), cells: Object.fromEntries(Object.entries(d.cells).filter(([k]) => Number(k.split("-")[1]) < last)) }; })}
                          disabled={plan.periods.length <= 1} className="rounded-full px-3 py-1.5 font-bold flex items-center gap-1 disabled:opacity-40" style={{ ...field, color: tokens.muted }}><Minus className="w-3.5 h-3.5" /> letzte Stunde</button>
                  <datalist id={`fb-subjects-${c.id}`}>{subjects.map(s => <option key={s} value={s} />)}</datalist>
                </div>
              )}
            </div>
          </div>
        );
      })}
    </section>
    )}
    {toStart.length > 0 && !editing && (
      <div className="flex gap-2 flex-wrap">
        {toStart.map(c => (
          <button key={c.id} onClick={() => startEdit(c)} className="rounded-full pl-1 pr-4 py-1 text-[14px] flex items-center gap-2" style={{ color: tokens.muted, boxShadow: `inset 0 0 0 1px ${tokens.line}` }}>
            <PersonAvatar name={c.name} color={c.color} avatarUrl={c.avatar_url} size={26} /><Plus className="w-4 h-4" /> Stundenplan für {c.first_name || c.name} anlegen
          </button>
        ))}
      </div>
    )}
    </div>
  );
}
