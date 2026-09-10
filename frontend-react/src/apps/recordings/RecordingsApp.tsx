/**
 * Recordings — dinners and meetings Yorik listened to, with the report
 * (decisions, tasks, nice moments, friction) and the transcript.
 * Two panes on wide screens, list → detail on the phone. Tasks in a
 * report are proposals: "Adopt" creates the task for the named person.
 */
import { useCallback, useEffect, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { ArrowLeft, Check, ChevronDown, ChevronRight, Loader2, Mic, RefreshCw, Trash2 } from "lucide-react";
import { api } from "@/lib/api";
import { useAuth } from "@/components/AuthGate";
import { Dock } from "@/components/Dock";
import { RecordingStartDialog } from "@/components/RecordingStartDialog";
import { subscribeRecorder } from "@/components/RecorderDock";
import { cn } from "@/lib/utils";

interface Recording {
  id: number; title: string; kind: string; status: string; owner_user_id: string; owner_name: string;
  participants: { user_id: string; name: string }[]; started_at: string; duration_s: number | null;
  progress: string | null; error: string | null; has_report: boolean; audio_available: boolean;
}
interface Segment { seq: number; start_s: number; end_s: number; speaker: string; user_id: string | null; text: string }
interface Task { title: string; person: string; people?: string[]; due_date: string; why: string; task_id?: number; adopted_by?: string; adopted_at?: string; with?: string[] }
interface Report {
  summary: string; decisions: { text: string; who: string; when: string }[]; tasks: Task[];
  highlights: string[]; friction: string[]; open_questions: string[]; dates: { text: string; date: string; time: string }[];
  template: string; generated_at: string;
}

const KIND_LABEL: Record<string, string> = { dinner: "Dinner", meeting: "Meeting", conversation: "Conversation" };

function fmtDuration(s: number | null): string {
  if (!s) return "";
  const m = Math.round(s / 60);
  return m < 1 ? `${Math.round(s)} s` : `${m} min`;
}

function fmtDate(iso: string): string {
  const d = new Date(iso.replace(" ", "T"));
  return isNaN(d.getTime()) ? iso : d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" }) + " " + d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

export function RecordingsApp() {
  const { id } = useParams();
  const navigate = useNavigate();
  const [list, setList] = useState<Recording[] | null>(null);
  const [startOpen, setStartOpen] = useState(false);
  const selected = id ? Number(id) : null;

  const load = useCallback(async () => {
    try {
      const r = await api.get<{ recordings: Recording[] }>("/api/recordings");
      setList(r.recordings);
    } catch { setList([]); }
  }, []);
  useEffect(() => { load(); }, [load]);
  useEffect(() => subscribeRecorder(s => { if (s.phase === "done" || s.phase === "recording") load(); }), [load]);
  // a recording in flight: refresh the list every 10 s so the status moves
  useEffect(() => {
    if (!list?.some(r => ["recording", "uploaded", "processing"].includes(r.status))) return;
    const t = setInterval(load, 10_000);
    return () => clearInterval(t);
  }, [list, load]);

  return (
    <div className="h-screen overflow-y-auto bg-background text-foreground">
      <div className="mx-auto max-w-6xl px-4 pt-6 pb-[max(7rem,env(safe-area-inset-bottom)+6rem)]">
        <div className="flex items-center justify-between gap-3 mb-4">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">Recordings</h1>
            <p className="text-xs text-muted-foreground">Dinners and meetings, with what came out of them.</p>
          </div>
          <button onClick={() => setStartOpen(true)}
                  className="flex items-center gap-2 rounded-full bg-red-500 text-white px-4 py-2 text-sm font-semibold hover:bg-red-400">
            <Mic className="w-4 h-4" /> Record
          </button>
        </div>

        <div className="grid md:grid-cols-[18rem_1fr] gap-5">
          <div className={cn("space-y-1", selected && "hidden md:block")}>
            {list === null && <Loader2 className="w-4 h-4 animate-spin text-muted-foreground" />}
            {list?.length === 0 && (
              <div className="rounded-xl border border-dashed border-border p-5 text-sm text-muted-foreground">
                Nothing recorded yet. Tap Record at the table, or say "Yorik, record the dinner".
              </div>
            )}
            {list?.map(r => (
              <button key={r.id} onClick={() => navigate(`/recordings/${r.id}`)}
                      className={cn("w-full text-left rounded-xl border px-3 py-2.5 hover:bg-muted/60",
                                    selected === r.id ? "border-primary bg-muted/60" : "border-border")}>
                <div className="flex items-center justify-between gap-2">
                  <div className="font-medium truncate">{r.title || KIND_LABEL[r.kind] || "Recording"}</div>
                  <StatusPill r={r} />
                </div>
                <div className="text-[11px] text-muted-foreground mt-0.5">
                  {fmtDate(r.started_at)}{r.duration_s ? ` · ${fmtDuration(r.duration_s)}` : ""} · {[r.owner_name, ...r.participants.map(p => p.name)].filter(Boolean).join(", ")}
                </div>
              </button>
            ))}
          </div>
          <div className={cn(!selected && "hidden md:block")}>
            {selected
              ? <RecordingDetail id={selected} onBack={() => navigate("/recordings")} onDeleted={() => { navigate("/recordings"); load(); }} />
              : <div className="hidden md:flex h-full min-h-[16rem] items-center justify-center rounded-xl border border-dashed border-border text-sm text-muted-foreground">Pick a recording</div>}
          </div>
        </div>
      </div>
      {startOpen && <RecordingStartDialog onClose={() => setStartOpen(false)} onStarted={() => load()} />}
      <Dock activeAppId="recordings" />
    </div>
  );
}

function StatusPill({ r }: { r: Recording }) {
  const map: Record<string, string> = {
    recording: "bg-red-500/15 text-red-500", uploaded: "bg-amber-500/15 text-amber-600", processing: "bg-amber-500/15 text-amber-600",
    done: "bg-emerald-500/15 text-emerald-600", failed: "bg-red-500/15 text-red-500",
  };
  const label = r.status === "processing" ? (r.progress || "processing") : r.status === "done" ? (r.has_report ? "report" : "transcript") : r.status;
  return <span className={cn("shrink-0 rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide", map[r.status] || "bg-muted")}>{label}</span>;
}

function RecordingDetail({ id, onBack, onDeleted }: { id: number; onBack: () => void; onDeleted: () => void }) {
  const auth = useAuth();
  const [rec, setRec] = useState<Recording | null>(null);
  const [report, setReport] = useState<Report | null>(null);
  const [segments, setSegments] = useState<Segment[] | null>(null);
  const [showTranscript, setShowTranscript] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [adoptOpen, setAdoptOpen] = useState<number | null>(null);
  const [together, setTogether] = useState<Set<string>>(new Set());

  const load = useCallback(async () => {
    try {
      const r = await api.get<Recording>(`/api/recordings/${id}`);
      setRec(r);
      if (r.has_report) setReport(await api.get<Report>(`/api/recordings/${id}/report`));
      else setReport(null);
      if (r.status === "done") setSegments((await api.get<{ segments: Segment[] }>(`/api/recordings/${id}/transcript`)).segments);
    } catch (e: any) { setErr(e?.message || "could not load"); }
  }, [id]);
  useEffect(() => { setRec(null); setReport(null); setSegments(null); setErr(null); load(); }, [load]);
  useEffect(() => {
    if (!rec || !["recording", "uploaded", "processing"].includes(rec.status)) return;
    const t = setInterval(load, 5_000);
    return () => clearInterval(t);
  }, [rec, load]);

  function tableOthers(): { user_id: string; name: string }[] {
    if (!rec) return [];
    const meId = (auth.user as any)?.id;
    return [{ user_id: rec.owner_user_id, name: rec.owner_name }, ...rec.participants].filter(p => p.user_id !== meId && p.name);
  }

  function openAdopt(i: number, t: Task) {
    // preselect the people the report says are in it together, except me
    const names = new Set((t.people || []).map(n => n.toLowerCase()));
    const pre = new Set<string>();
    for (const p of tableOthers()) if (names.has(p.name.toLowerCase())) pre.add(p.user_id);
    setTogether(pre);
    setAdoptOpen(i);
  }

  async function adopt(i: number) {
    setBusy(`adopt-${i}`);
    try {
      setReport(await api.post<Report>(`/api/recordings/${id}/tasks/${i}/adopt`, { with_user_ids: [...together] }));
      setAdoptOpen(null);
    } catch (e: any) { setErr(e?.message || "could not add the task"); }
    finally { setBusy(null); }
  }
  async function buildReport(refresh = false) {
    setBusy("report");
    try {
      setReport(await api.post<Report>(`/api/recordings/${id}/report`, { refresh }));
      setRec(r => r ? { ...r, has_report: true } : r);
    } catch (e: any) { setErr(e?.message || "could not write the report"); }
    finally { setBusy(null); }
  }
  async function remove() {
    if (!confirm("Delete this recording, its transcript and report?")) return;
    try { await api.delete(`/api/recordings/${id}`); onDeleted(); }
    catch (e: any) { setErr(e?.message || "could not delete"); }
  }

  if (err && !rec) return <div className="text-sm text-red-500">{err}</div>;
  if (!rec) return <Loader2 className="w-4 h-4 animate-spin text-muted-foreground" />;
  const mine = rec.owner_user_id === (auth.user as any)?.id;
  const others = tableOthers();

  return (
    <div className="space-y-5">
      <div className="flex items-start gap-3">
        <button onClick={onBack} className="md:hidden p-1 -ml-1 rounded-full hover:bg-muted" aria-label="Back"><ArrowLeft className="w-5 h-5" /></button>
        <div className="flex-1 min-w-0">
          <h2 className="text-xl font-semibold truncate">{rec.title || KIND_LABEL[rec.kind]}</h2>
          <div className="text-xs text-muted-foreground">
            {fmtDate(rec.started_at)}{rec.duration_s ? ` · ${fmtDuration(rec.duration_s)}` : ""} · {[rec.owner_name, ...rec.participants.map(p => p.name)].filter(Boolean).join(", ")}
          </div>
        </div>
        {mine && <button onClick={remove} className="p-2 rounded-full hover:bg-muted text-muted-foreground" title="Delete"><Trash2 className="w-4 h-4" /></button>}
      </div>
      {err && <div className="text-sm text-red-500">{err}</div>}

      {rec.status !== "done" && rec.status !== "failed" && (
        <div className="rounded-xl border border-border p-4 text-sm flex items-center gap-3">
          <Loader2 className="w-4 h-4 animate-spin" />
          {rec.status === "recording" ? "Recording is running." : `Writing the transcript… ${rec.progress || ""}`}
        </div>
      )}
      {rec.status === "failed" && <div className="rounded-xl border border-red-500/40 p-4 text-sm text-red-500">Failed: {rec.error}</div>}

      {rec.status === "done" && !report && (
        <div className="rounded-xl border border-border p-4 text-sm flex items-center justify-between gap-3">
          <span>The transcript is there. No report yet.</span>
          <button onClick={() => buildReport(false)} disabled={busy === "report"}
                  className="flex items-center gap-2 rounded-full bg-primary text-primary-foreground px-3 py-1.5 text-xs font-semibold disabled:opacity-50">
            {busy === "report" ? <Loader2 className="w-3 h-3 animate-spin" /> : <RefreshCw className="w-3 h-3" />} Write the report
          </button>
        </div>
      )}

      {report && (
        <div className="space-y-5">
          <p className="text-[15px] leading-relaxed">{report.summary}</p>

          {report.tasks.length > 0 && (
            <Section title="Tasks" hint="Proposals until someone adopts them">
              {report.tasks.map((t, i) => (
                <div key={i} className="py-2 border-t border-border first:border-t-0">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="font-medium">{t.title}</div>
                      <div className="text-xs text-muted-foreground">
                        {[(t.people && t.people.length > 1) ? t.people.join(" + ") : t.person, t.due_date].filter(Boolean).join(" · ")}{t.why ? ` — ${t.why}` : ""}
                      </div>
                    </div>
                    {t.task_id
                      ? <span className="shrink-0 flex items-center gap-1 text-xs text-emerald-600"><Check className="w-3.5 h-3.5" /> {t.adopted_by || "added"}{t.with && t.with.length ? ` + ${t.with.join(", ")}` : ""}</span>
                      : <button onClick={() => adoptOpen === i ? setAdoptOpen(null) : openAdopt(i, t)} disabled={busy === `adopt-${i}`}
                                className="shrink-0 rounded-full border border-border px-3 py-1 text-xs font-semibold hover:bg-muted disabled:opacity-50">
                          Adopt
                        </button>}
                  </div>
                  {adoptOpen === i && !t.task_id && (
                    <div className="mt-2 rounded-lg bg-muted/50 p-3 text-xs flex flex-wrap items-center gap-3">
                      <span className="text-muted-foreground">Mine{others.length ? ", together with:" : "."}</span>
                      {others.map(p => {
                        const on = together.has(p.user_id);
                        return (
                          <label key={p.user_id} className="flex items-center gap-1.5 cursor-pointer">
                            <input type="checkbox" checked={on} className="w-3.5 h-3.5 accent-primary"
                                   onChange={() => setTogether(prev => { const n = new Set(prev); on ? n.delete(p.user_id) : n.add(p.user_id); return n; })} />
                            {p.name}
                          </label>
                        );
                      })}
                      <button onClick={() => adopt(i)} disabled={busy === `adopt-${i}`}
                              className="ml-auto rounded-full bg-primary text-primary-foreground px-3 py-1 font-semibold disabled:opacity-50">
                        {busy === `adopt-${i}` ? <Loader2 className="w-3 h-3 animate-spin" /> : "Add task"}
                      </button>
                    </div>
                  )}
                </div>
              ))}
            </Section>
          )}
          {report.decisions.length > 0 && (
            <Section title="Decisions">
              {report.decisions.map((d, i) => <Line key={i} text={d.text} meta={[d.who, d.when].filter(Boolean).join(" · ")} />)}
            </Section>
          )}
          {report.highlights.length > 0 && <Section title="Nice moments" tone="text-emerald-600">{report.highlights.map((h, i) => <Line key={i} text={h} />)}</Section>}
          {report.friction.length > 0 && <Section title="What did not go well" tone="text-amber-600">{report.friction.map((h, i) => <Line key={i} text={h} />)}</Section>}
          {report.open_questions.length > 0 && <Section title="Open questions">{report.open_questions.map((h, i) => <Line key={i} text={h} />)}</Section>}
          {report.dates.length > 0 && (
            <Section title="Dates mentioned">
              {report.dates.map((d, i) => <Line key={i} text={d.text} meta={[d.date, d.time].filter(Boolean).join(" ")} />)}
            </Section>
          )}
          <div className="text-[11px] text-muted-foreground flex items-center gap-3">
            <span>Report written {fmtDate(report.generated_at)}</span>
            <button onClick={() => buildReport(true)} disabled={busy === "report"} className="flex items-center gap-1 hover:text-foreground">
              {busy === "report" ? <Loader2 className="w-3 h-3 animate-spin" /> : <RefreshCw className="w-3 h-3" />} write again
            </button>
          </div>
        </div>
      )}

      {segments && (
        <div>
          <button onClick={() => setShowTranscript(v => !v)} className="flex items-center gap-1 text-sm font-medium">
            {showTranscript ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />} Transcript ({segments.length} turns)
          </button>
          {showTranscript && (
            <div className="mt-2 space-y-2 text-sm">
              {segments.map(s => (
                <div key={s.seq} className="grid grid-cols-[3.2rem_7rem_1fr] gap-2">
                  <span className="text-[11px] text-muted-foreground tabular-nums pt-0.5">{Math.floor(s.start_s / 60)}:{String(Math.floor(s.start_s % 60)).padStart(2, "0")}</span>
                  <span className={cn("font-medium truncate", s.user_id ? "" : "text-muted-foreground")}>{s.speaker}</span>
                  <span>{s.text}</span>
                </div>
              ))}
              {rec.audio_available && <audio controls preload="none" src={`/api/recordings/${id}/audio`} className="mt-3 w-full" />}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function Section({ title, hint, tone, children }: { title: string; hint?: string; tone?: string; children: React.ReactNode }) {
  return (
    <div className="rounded-xl border border-border p-4">
      <div className="flex items-baseline justify-between gap-2 mb-1">
        <h3 className={cn("text-xs uppercase tracking-wider font-semibold", tone || "text-muted-foreground")}>{title}</h3>
        {hint && <span className="text-[11px] text-muted-foreground">{hint}</span>}
      </div>
      <div className="text-sm">{children}</div>
    </div>
  );
}

function Line({ text, meta }: { text: string; meta?: string }) {
  return (
    <div className="py-1.5 border-t border-border first:border-t-0">
      <div>{text}</div>
      {meta && <div className="text-xs text-muted-foreground">{meta}</div>}
    </div>
  );
}
