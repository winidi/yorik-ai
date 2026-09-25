/**
 * One pipeline: what needs the person now (attention card), the
 * sequence as a timeline with per-mail approval, what the answer is
 * recognised by, the send window, and the history of every check.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  ArrowLeft, Check, ChevronDown, ChevronRight, Loader2, Mail, Send, UserRound, Plus, Trash2,
  RefreshCw, Pause, Play, CircleCheck, Ban, Search, AlertTriangle, Clock, X,
} from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { toast } from "@/components/Toast";
import type { Candidate, Config, Features, Pipeline, PipelineEvent, Step } from "./types";
import { attentionLabel, dateShort, relDay, stateLabel, timeShort } from "./format";

type DraftStep = Pick<Step, "action" | "after_days" | "payload"> & Partial<Step> & { key: string };

function errText(e: any): string {
  const d = e?.body?.detail;
  if (d && typeof d === "object" && d.reason) return d.reason;
  return e?.message || "Ging nicht";
}

let keySeq = 0;
const newKey = () => `n${++keySeq}`;

function toDraft(steps: Step[]): DraftStep[] {
  return steps.filter(s => s.status === "offen").map(s => ({ ...s, key: `s${s.id}` }));
}

export function PipelineDetail({ id }: { id: number }) {
  const navigate = useNavigate();
  const [p, setP] = useState<Pipeline | null>(null);
  const [missing, setMissing] = useState(false);
  const [draft, setDraft] = useState<DraftStep[]>([]);
  const [dirty, setDirty] = useState(false);
  const [opened, setOpened] = useState<Set<string>>(new Set());
  const [expanded, setExpanded] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const apply = useCallback((next: Pipeline) => {
    setP(next);
    setDraft(toDraft(next.steps));
    setDirty(false);
  }, []);

  const load = useCallback(async () => {
    try { apply(await api.get<Pipeline>(`/api/pipelines/${id}`)); }
    catch (e: any) { if (e?.status === 404) setMissing(true); else toast(errText(e), "error"); }
  }, [id, apply]);

  useEffect(() => { void load(); }, [load]);

  // While the model writes the reminders, look again every few seconds.
  const drafting = !!p?.config?.drafting;
  useEffect(() => {
    if (!drafting) return;
    const t = setInterval(() => { void load(); }, 3000);
    return () => clearInterval(t);
  }, [drafting, load]);

  async function run(label: string, fn: () => Promise<Pipeline>, ok?: string) {
    setBusy(label);
    try {
      apply(await fn());
      if (ok) toast(ok, "success");
    } catch (e: any) {
      toast(errText(e), "error");
      await load();
    } finally { setBusy(null); }
  }

  const post = (path: string, body?: any) => () => api.post<Pipeline>(`/api/pipelines/${id}${path}`, body ?? {});

  if (missing) {
    return (
      <div>
        <BackLink />
        <p className="text-sm text-muted-foreground mt-6">Diese Pipeline gibt es nicht (mehr).</p>
      </div>
    );
  }
  if (!p) return <div className="text-sm text-muted-foreground flex items-center gap-2"><Loader2 className="w-4 h-4 animate-spin" /> Lädt…</div>;

  const editable = (p.state === "entwurf" || p.state === "laeuft" || p.state === "pausiert") && !p.config?.drafting;
  const doneSteps = p.steps.filter(s => s.status === "erledigt");
  const openMail = draft.filter(s => s.action === "mail_senden");
  const allApproved = openMail.every(s => s.approved);
  const allOpened = openMail.every(s => opened.has(s.key));

  function toggle(key: string) {
    setExpanded(k => (k === key ? null : key));
    setOpened(o => new Set(o).add(key));
  }

  function change(key: string, patch: Partial<DraftStep>) {
    setDraft(d => d.map(s => {
      if (s.key !== key) return s;
      const textChanged = patch.payload && (patch.payload.body !== s.payload.body
        || patch.payload.subject !== s.payload.subject || (patch.payload.to || []).join() !== (s.payload.to || []).join());
      const payload = patch.payload ? { ...patch.payload, ...(textChanged ? { source: "bearbeitet" as const } : {}) } : s.payload;
      return { ...s, ...patch, payload, approved: false };
    }));
    setDirty(true);
  }

  function addReminder() {
    const last = [...p!.steps, ...draft].filter(s => s.action === "mail_senden").pop();
    const step: DraftStep = {
      key: newKey(), action: "mail_senden", after_days: 7, approved: false,
      payload: { to: last?.payload.to || p!.origin.to || [], subject: last?.payload.subject || `Re: ${p!.origin.subject || ""}`, body: "" },
    };
    setDraft(d => {
      const i = d.findIndex(s => s.action === "uebergabe");
      return i < 0 ? [...d, step] : [...d.slice(0, i), step, ...d.slice(i)];
    });
    setDirty(true);
    setExpanded(step.key);
    setOpened(o => new Set(o).add(step.key));
  }

  function removeStep(key: string) {
    setDraft(d => d.filter(s => s.key !== key));
    setDirty(true);
  }

  async function saveSteps() {
    await run("save", () => api.put<Pipeline>(`/api/pipelines/${id}/steps`, {
      steps: draft.map(s => ({ action: s.action, after_days: s.after_days, payload: s.payload })),
    }), "Gespeichert");
  }

  async function approve(step: DraftStep, approved: boolean) {
    if (!step.id) return;
    await run(`approve${step.id}`, post(`/steps/${step.id}/approve`, { approved }));
  }

  async function approveAll() {
    setBusy("approveAll");
    try {
      let last: Pipeline | null = null;
      for (const s of openMail) {
        if (!s.approved && s.id) last = await api.post<Pipeline>(`/api/pipelines/${id}/steps/${s.id}/approve`, { approved: true });
      }
      if (last) apply(last);
    } catch (e: any) { toast(errText(e), "error"); await load(); }
    finally { setBusy(null); }
  }

  return (
    <div>
      <BackLink />
      <div className="mt-4 mb-1 flex items-start justify-between gap-3">
        <h1 className="text-lg font-semibold leading-snug">{p.title}</h1>
        <StateChip p={p} />
      </div>
      {p.goal && <p className="text-sm text-muted-foreground mb-1">Ziel: {p.goal}</p>}
      {p.last_check && (
        <p className="text-xs text-muted-foreground mb-5 flex items-center gap-1.5">
          <Search className="w-3.5 h-3.5" /> Zuletzt geprüft {timeShort(p.last_check.at)}: {p.last_check.summary}
        </p>
      )}

      {p.state === "laeuft" && p.attention && (
        <AttentionCard p={p} busy={busy} run={run} post={post} onAddReminder={addReminder} />
      )}

      {/* ── the sequence ─────────────────────────────────────── */}
      {p.config?.drafting && (
        <div className="mt-6 rounded-xl border border-primary/30 bg-primary/5 p-4 flex items-start gap-3">
          <Loader2 className="w-4 h-4 animate-spin text-primary mt-0.5 shrink-0" />
          <div>
            <div className="text-sm font-medium">Yorik schreibt die Erinnerungen…</div>
            <p className="text-xs text-muted-foreground">
              Er liest deine Mail, überlegt, welche Antwort du erwartest, und schlägt Abstände und Texte vor. Das dauert meist unter einer Minute.
            </p>
          </div>
        </div>
      )}
      <div className="flex items-center justify-between mt-7 mb-3 gap-2">
        <div className="text-[13px] font-medium text-muted-foreground">Ablauf</div>
        <div className="flex items-center gap-2">
        {editable && openMail.length > 0 && (
          <button
            onClick={() => {
              if (openMail.some(s => s.approved || s.payload.source === "bearbeitet")
                  && !window.confirm("Yorik schreibt die offenen Erinnerungen neu; deine Änderungen und Freigaben daran gehen verloren. Weiter?")) return;
              void run("redraft", post("/redraft"));
            }}
            disabled={busy !== null || dirty}
            className="text-xs rounded-md border border-border px-2.5 py-1 hover:bg-muted disabled:opacity-40"
          >
            Neu schreiben lassen
          </button>
        )}
        {editable && openMail.length > 0 && !allApproved && !dirty && (
          <button
            onClick={approveAll}
            disabled={!allOpened || busy !== null}
            title={allOpened ? "" : "Öffne erst jede Mail einmal"}
            className="text-xs rounded-md border border-border px-2.5 py-1 hover:bg-muted disabled:opacity-40"
          >
            {busy === "approveAll" ? "…" : allOpened ? "Alle freigeben" : "Alle freigeben (erst jede öffnen)"}
          </button>
        )}
        </div>
      </div>

      <ol className="relative ml-3 border-l border-border space-y-4 pb-1">
        <TimelineItem icon={<Mail className="w-3.5 h-3.5" />} done>
          <OriginCard p={p} />
        </TimelineItem>
        {doneSteps.map(s => (
          <TimelineItem key={`d${s.id}`} icon={s.action === "uebergabe" ? <UserRound className="w-3.5 h-3.5" /> : <Check className="w-3.5 h-3.5" />} done>
            <div className="text-sm">
              {s.action === "uebergabe" ? "Übergabe an dich" : `Erinnerung gesendet`}
              <span className="text-muted-foreground"> · {dateShort(s.done_at)}</span>
            </div>
            {s.action === "mail_senden" && (
              <div className="text-xs text-muted-foreground truncate">{s.payload.subject} → {(s.payload.to || []).join(", ")}</div>
            )}
          </TimelineItem>
        ))}
        {draft.map((s, i) => (
          <TimelineItem
            key={s.key}
            icon={s.action === "uebergabe" ? <UserRound className="w-3.5 h-3.5" /> : <Send className="w-3.5 h-3.5" />}
            highlight={p.next_step?.id === s.id && p.attention === "schritt_faellig"}
          >
            <StepCard
              step={s}
              index={i}
              editable={editable}
              dirty={dirty}
              expanded={expanded === s.key}
              quote={p.quote || ""}
              busy={busy}
              onToggle={() => toggle(s.key)}
              onChange={patch => change(s.key, patch)}
              onRemove={() => removeStep(s.key)}
              onApprove={a => approve(s, a)}
            />
          </TimelineItem>
        ))}
      </ol>

      {editable && (
        <div className="flex items-center gap-2 mt-4 ml-3 flex-wrap">
          <button onClick={addReminder} className="text-[13px] flex items-center gap-1 rounded-md border border-border px-2.5 py-1.5 hover:bg-muted">
            <Plus className="w-3.5 h-3.5" /> Erinnerung hinzufügen
          </button>
          {dirty && (
            <>
              <button onClick={saveSteps} disabled={busy !== null} className="text-[13px] rounded-md bg-primary text-primary-foreground px-3 py-1.5 font-medium">
                {busy === "save" ? "Speichert…" : "Änderungen speichern"}
              </button>
              <button onClick={() => { setDraft(toDraft(p.steps)); setDirty(false); }} className="text-[13px] text-muted-foreground hover:text-foreground px-2">
                Verwerfen
              </button>
            </>
          )}
        </div>
      )}

      {p.state === "entwurf" && (
        <div className="mt-6 rounded-xl border border-border bg-card p-4">
          <div className="text-sm font-medium mb-1">Starten</div>
          <p className="text-[13px] text-muted-foreground mb-3">
            Yorik schaut ab jetzt nach der Antwort. Wird eine Erinnerung fällig, fragt er dich vorher; nichts geht ohne dein Okay raus.
          </p>
          <button
            onClick={() => run("start", post("/start"), "Gestartet")}
            disabled={!allApproved || dirty || busy !== null}
            className="rounded-md bg-primary text-primary-foreground px-4 py-2 text-sm font-medium disabled:opacity-40"
          >
            {busy === "start" ? "Startet…" : "Starten"}
          </button>
          {(!allApproved || dirty) && (
            <span className="text-xs text-muted-foreground ml-3">
              {dirty ? "Erst speichern." : "Erst jede Erinnerung freigeben."}
            </span>
          )}
        </div>
      )}

      <FeaturesPanel p={p} editable={editable} onSaved={apply} />
      <WindowPanel p={p} editable={editable} onSaved={apply} />

      <Controls p={p} busy={busy} run={run} post={post} onDeleted={() => navigate("/pipelines")} />

      <History events={p.events || []} />
    </div>
  );
}

function BackLink() {
  const navigate = useNavigate();
  return (
    <button onClick={() => navigate("/pipelines")} className="flex items-center gap-1 text-[13px] text-muted-foreground hover:text-foreground">
      <ArrowLeft className="w-4 h-4" /> Pipelines
    </button>
  );
}

function StateChip({ p }: { p: Pipeline }) {
  const label = p.state === "laeuft" ? "Läuft" : stateLabel(p.state).split(" —")[0];
  return (
    <span className={cn(
      "shrink-0 text-xs rounded-full px-2 py-0.5 border",
      p.state === "laeuft" ? "border-primary/40 text-primary" :
      p.state === "erledigt" ? "border-emerald-500/40 text-emerald-600" : "border-border text-muted-foreground",
    )}>{label}</span>
  );
}

function TimelineItem({ icon, done, highlight, children }: { icon: React.ReactNode; done?: boolean; highlight?: boolean; children: React.ReactNode }) {
  return (
    <li className="ml-5 relative">
      <span className={cn(
        "absolute -left-[31px] top-0.5 w-6 h-6 rounded-full border flex items-center justify-center bg-background",
        done ? "border-border text-muted-foreground" : highlight ? "border-primary text-primary" : "border-border text-foreground",
      )}>{icon}</span>
      {children}
    </li>
  );
}

function OriginCard({ p }: { p: Pipeline }) {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <button onClick={() => setOpen(v => !v)} className="text-left w-full">
        <div className="text-sm">Deine Mail <span className="text-muted-foreground">· {dateShort(p.origin.sent_at || p.since_at)}</span></div>
        <div className="text-xs text-muted-foreground truncate">{p.origin.subject} → {(p.origin.to || []).join(", ")}</div>
      </button>
      {open && p.origin.body_excerpt && (
        <pre className="mt-2 text-xs whitespace-pre-wrap font-sans text-muted-foreground bg-muted/40 rounded-md p-3 max-h-64 overflow-y-auto">{p.origin.body_excerpt}</pre>
      )}
    </div>
  );
}

function StepCard({ step, index, editable, dirty, expanded, quote, busy, onToggle, onChange, onRemove, onApprove }: {
  step: DraftStep; index: number; editable: boolean; dirty: boolean; expanded: boolean; quote: string; busy: string | null;
  onToggle: () => void; onChange: (patch: Partial<DraftStep>) => void; onRemove: () => void; onApprove: (a: boolean) => void;
}) {
  const isMail = step.action === "mail_senden";
  const [showQuote, setShowQuote] = useState(false);
  const days = (
    <span className="inline-flex items-center gap-1">
      nach
      {editable ? (
        <input
          type="number" min={0} max={365} value={step.after_days}
          onChange={e => onChange({ after_days: Math.max(0, Math.min(365, Number(e.target.value) || 0)) })}
          className="w-12 bg-transparent border border-border rounded px-1 py-0 text-center tabular-nums"
          aria-label="Tage"
        />
      ) : <b className="tabular-nums">{step.after_days}</b>}
      {step.after_days === 1 ? "Tag" : "Tagen"}
    </span>
  );

  if (!isMail) {
    return (
      <div>
        <div className="text-sm flex items-center gap-1.5 flex-wrap">Übergabe an dich <span className="text-muted-foreground text-xs">· {days}{step.due_at && !dirty ? `, ${relDay(step.due_at)}` : ""}</span></div>
        <div className="text-xs text-muted-foreground">Kommt bis dahin keine Antwort, meldet sich Yorik bei dir.</div>
      </div>
    );
  }

  return (
    <div className={cn("rounded-xl border bg-card", expanded ? "border-primary/30" : "border-border")}>
      <button onClick={onToggle} className="w-full text-left px-3.5 py-2.5 flex items-center gap-2">
        <div className="min-w-0 flex-1">
          <div className="text-sm flex items-center gap-1.5 flex-wrap">
            {index === 0 ? "Erinnerung" : `${index + 1}. Erinnerung`}
            <span className="text-muted-foreground text-xs">· {step.after_days} {step.after_days === 1 ? "Tag" : "Tage"} danach{step.due_at && !dirty ? `, ${relDay(step.due_at)}` : ""}</span>
          </div>
          <div className="text-xs text-muted-foreground truncate">{step.payload.subject}</div>
        </div>
        {step.approved
          ? <span className="text-xs text-emerald-600 flex items-center gap-1 shrink-0"><Check className="w-3.5 h-3.5" /> freigegeben</span>
          : <span className="text-xs text-amber-600 shrink-0">nicht freigegeben</span>}
        {expanded ? <ChevronDown className="w-4 h-4 text-muted-foreground" /> : <ChevronRight className="w-4 h-4 text-muted-foreground" />}
      </button>
      {expanded && (
        <div className="px-3.5 pb-3.5 space-y-2.5 border-t border-border pt-3">
          <div className="text-xs text-muted-foreground">
            {days} der vorigen Nachricht{step.payload.why ? <> · <span className="italic">{step.payload.why}</span></> : null}
          </div>
          <SourceNote source={step.payload.source} />
          <Field label="An">
            <input
              value={(step.payload.to || []).join(", ")}
              disabled={!editable}
              onChange={e => onChange({ payload: { ...step.payload, to: e.target.value.split(/[,;\s]+/).filter(Boolean) } })}
              className="w-full bg-transparent border border-border rounded-md px-2.5 py-1.5 text-sm"
            />
          </Field>
          <Field label="Betreff">
            <input
              value={step.payload.subject || ""}
              disabled={!editable}
              onChange={e => onChange({ payload: { ...step.payload, subject: e.target.value } })}
              className="w-full bg-transparent border border-border rounded-md px-2.5 py-1.5 text-sm"
            />
          </Field>
          <Field label="Text">
            <textarea
              value={step.payload.body || ""}
              disabled={!editable}
              rows={Math.min(14, Math.max(5, (step.payload.body || "").split("\n").length + 1))}
              onChange={e => onChange({ payload: { ...step.payload, body: e.target.value } })}
              className="w-full bg-transparent border border-border rounded-md px-2.5 py-2 text-sm leading-relaxed"
            />
          </Field>
          {quote && (
            <div>
              <button onClick={() => setShowQuote(v => !v)} className="text-xs text-muted-foreground hover:text-foreground">
                {showQuote ? "Zitat ausblenden" : "Darunter steht deine erste Mail als Zitat"}
              </button>
              {showQuote && <pre className="mt-1.5 text-xs whitespace-pre-wrap font-sans text-muted-foreground bg-muted/40 rounded-md p-2.5 max-h-48 overflow-y-auto">{quote.trim()}</pre>}
            </div>
          )}
          {editable && (
            <div className="flex items-center justify-between gap-2 pt-1">
              <button onClick={onRemove} className="text-xs text-muted-foreground hover:text-rose-500 flex items-center gap-1">
                <Trash2 className="w-3.5 h-3.5" /> Entfernen
              </button>
              {step.id && !dirty ? (
                step.approved ? (
                  <button onClick={() => onApprove(false)} disabled={busy !== null} className="text-xs rounded-md border border-border px-2.5 py-1.5 hover:bg-muted">
                    Freigabe zurücknehmen
                  </button>
                ) : (
                  <button onClick={() => onApprove(true)} disabled={busy !== null} className="text-[13px] rounded-md bg-primary text-primary-foreground px-3 py-1.5 font-medium flex items-center gap-1">
                    <Check className="w-4 h-4" /> Freigeben
                  </button>
                )
              ) : (
                <span className="text-xs text-muted-foreground">Erst speichern, dann freigeben</span>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function SourceNote({ source }: { source?: string }) {
  const text = {
    llm: "Von Yorik geschrieben. Wenn sie fällig wird, schreibt er sie mit dem aktuellen Stand noch einmal neu.",
    llm_frisch: "Heute von Yorik neu geschrieben, mit allem, was seitdem passiert ist.",
    vorlage: "Nur eine Vorlage: das Sprachmodell war nicht erreichbar. „Neu schreiben lassen“ versucht es noch einmal.",
    bearbeitet: "Von dir bearbeitet.",
  }[source || ""];
  if (!text) return null;
  return <div className={cn("text-xs", source === "vorlage" ? "text-amber-600" : "text-muted-foreground")}>{text}</div>;
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="text-xs text-muted-foreground">{label}</span>
      {children}
    </label>
  );
}

// ─── what needs the person ────────────────────────────────────────

function AttentionCard({ p, busy, run, post, onAddReminder }: {
  p: Pipeline; busy: string | null;
  run: (label: string, fn: () => Promise<Pipeline>, ok?: string) => Promise<void>;
  post: (path: string, body?: any) => () => Promise<Pipeline>;
  onAddReminder: () => void;
}) {
  const [insist, setInsist] = useState(false);
  const d = p.attention_detail || {};
  const step = p.steps.find(s => s.id === (d.step_id ?? p.next_step?.id));
  const due = p.next_step?.due_at ? new Date(p.next_step.due_at).getTime() <= Date.now() : false;

  const btn = "rounded-md px-3 py-1.5 text-[13px] font-medium disabled:opacity-40";
  const primary = cn(btn, "bg-primary text-primary-foreground");
  const secondary = cn(btn, "border border-border hover:bg-muted");

  return (
    <div className="rounded-xl border border-primary/40 bg-primary/5 p-4 mb-2">
      <div className="text-sm font-medium mb-2 flex items-center gap-1.5">
        {p.attention === "kann_nicht_pruefen" || p.attention === "versand_unklar"
          ? <AlertTriangle className="w-4 h-4 text-amber-500" />
          : <Clock className="w-4 h-4 text-primary" />}
        {attentionLabel(p.attention)}
      </div>

      {p.attention === "vielleicht" && (
        <div className="space-y-2.5">
          <p className="text-[13px] text-muted-foreground">
            Diese Mail{(d.candidates || []).length > 1 ? "s könnten" : " könnte"} die Antwort sein. Solange du nicht entschieden hast, geht keine Erinnerung raus.
          </p>
          {(d.candidates || []).map((c: Candidate) => (
            <div key={c.id} className="rounded-lg border border-border bg-card p-3">
              <div className="flex items-baseline justify-between gap-2">
                <div className="text-sm font-medium truncate">{c.subject || "(ohne Betreff)"}</div>
                <div className="text-xs text-muted-foreground shrink-0">{relDay(c.date)}</div>
              </div>
              <div className="text-xs text-muted-foreground truncate">
                von {c.from_name ? `${c.from_name} <${c.from}>` : c.from}{c.folder ? ` · ${c.folder}` : ""}
              </div>
              {c.snippet && <div className="text-xs mt-1 line-clamp-2">{c.snippet}</div>}
              <div className="flex flex-wrap gap-1 mt-1.5">
                {c.why.map(w => <span key={w} className="text-2xs rounded-full bg-muted px-2 py-0.5 text-muted-foreground">{w}</span>)}
              </div>
              <div className="flex flex-wrap gap-2 mt-2.5">
                <button className={primary} disabled={busy !== null}
                  onClick={() => run("answer", post("/answer", { mail_id: c.id, is_answer: true }), "Erledigt")}>
                  Ja, das ist die Antwort
                </button>
                <button className={secondary} disabled={busy !== null}
                  onClick={() => run("answer", post("/answer", { mail_id: c.id, is_answer: false }))}>
                  Nein, weiter warten
                </button>
                <a href={`/r/email?msg=${c.id}`} target="_blank" rel="noreferrer" className="text-xs text-muted-foreground hover:text-foreground self-center">
                  Mail öffnen
                </a>
              </div>
            </div>
          ))}
        </div>
      )}

      {p.attention === "schritt_faellig" && step && (
        <div>
          <p className="text-[13px] text-muted-foreground mb-2">
            {d.summary}. Keine Antwort gefunden.
            {step.payload.source === "llm_frisch" && " Yorik hat die Erinnerung für heute neu geschrieben."}
          </p>
          <div className="rounded-lg border border-border bg-card p-3 mb-3">
            <div className="text-xs text-muted-foreground">an {(step.payload.to || []).join(", ")}</div>
            <div className="text-sm font-medium">{step.payload.subject}</div>
            <pre className="text-xs whitespace-pre-wrap font-sans mt-1.5 max-h-40 overflow-y-auto">{step.payload.body}</pre>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <button className={cn(primary, "flex items-center gap-1.5")} disabled={busy !== null}
              onClick={() => run("send", post(`/steps/${step.id}/send`,
                step.approved ? {} : { approve: true, seen_body: step.payload.body || "" }), "Erinnerung gesendet")}>
              {busy === "send" ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
              {step.approved ? "Jetzt senden" : "Freigeben und senden"}
            </button>
            <span className="text-xs text-muted-foreground">Ändern kannst du den Text unten im Ablauf.</span>
            <span className="text-xs text-muted-foreground">Yorik prüft direkt vor dem Senden noch einmal.</span>
          </div>
        </div>
      )}

      {p.attention === "kann_nicht_pruefen" && (
        <div>
          <ul className="text-[13px] text-muted-foreground list-disc ml-5 mb-3">
            {(d.problems || []).map((x: string) => <li key={x}>{x}</li>)}
          </ul>
          <p className="text-xs text-muted-foreground mb-3">
            Solange Yorik deine Post nicht sicher überblickt, schickt er keine Erinnerung. Er versucht es alle 15 Minuten wieder.
          </p>
          <div className="flex flex-wrap gap-2">
            <button className={secondary} disabled={busy !== null} onClick={() => run("check", post("/check"))}>
              <RefreshCw className="w-3.5 h-3.5 inline mr-1" /> Nochmal prüfen
            </button>
            {due && p.next_step?.action === "mail_senden" && (
              insist ? (
                <button className={cn(btn, "bg-amber-500 text-white")} disabled={busy !== null}
                  onClick={() => run("send", post(`/steps/${p.next_step!.id}/send`, { despite_stale: true }), "Erinnerung gesendet")}>
                  Ja, trotzdem senden
                </button>
              ) : (
                <button className={secondary} onClick={() => setInsist(true)}>Trotzdem senden…</button>
              )
            )}
          </div>
        </div>
      )}

      {p.attention === "uebergabe" && (
        <div>
          <p className="text-[13px] text-muted-foreground mb-3">Alle Erinnerungen sind raus, eine Antwort ist nicht gekommen. Wie weiter?</p>
          <div className="flex flex-wrap gap-2">
            <button className={secondary} onClick={onAddReminder}><Plus className="w-3.5 h-3.5 inline mr-1" />Noch eine Erinnerung</button>
            <button className={secondary} disabled={busy !== null} onClick={() => run("finish", post("/finish"), "Erledigt")}>Erledigt</button>
            <button className={secondary} disabled={busy !== null} onClick={() => run("cancel", post("/cancel"))}>Abbrechen</button>
          </div>
        </div>
      )}

      {p.attention === "selbst_geantwortet" && (
        <div>
          <p className="text-[13px] text-muted-foreground mb-2">Du hast der Gegenseite selbst geschrieben:</p>
          <ul className="text-[13px] mb-3 space-y-0.5">
            {(d.mails || []).map((m: any) => <li key={m.id}>„{m.subject}“ <span className="text-muted-foreground">· {relDay(m.date)}</span></li>)}
          </ul>
          <div className="flex flex-wrap gap-2">
            <button className={primary} disabled={busy !== null} onClick={() => run("continue", post("/continue"))}>Weiter verfolgen</button>
            <button className={secondary} disabled={busy !== null} onClick={() => run("finish", post("/finish"), "Erledigt")}>Erledigt</button>
          </div>
        </div>
      )}

      {p.attention === "versand_unklar" && (
        <div>
          <p className="text-[13px] text-muted-foreground mb-3">
            {d.error ? `Der Versand meldete: ${d.error}. ` : "Der Versand wurde unterbrochen. "}
            Schau in deinem Postfach unter „Gesendet“ nach. Yorik schickt nichts noch einmal, bevor du das sagst.
          </p>
          <div className="flex flex-wrap gap-2">
            <button className={secondary} disabled={busy !== null} onClick={() => run("unclear", post("/unclear", { was_sent: true }))}>Ist rausgegangen</button>
            <button className={secondary} disabled={busy !== null} onClick={() => run("unclear", post("/unclear", { was_sent: false }))}>Ist nicht rausgegangen</button>
          </div>
        </div>
      )}

      {p.attention === "person_aus" && (
        <p className="text-[13px] text-muted-foreground">Pipelines sind für dich ausgeschaltet; diese hier wartet, bis sie wieder an sind.</p>
      )}
    </div>
  );
}

// ─── what the answer is recognised by ─────────────────────────────

const FEATURE_GROUPS: { key: keyof Features; label: string; hint: string }[] = [
  { key: "addresses", label: "Adressen", hint: "angeschriebene Adressen" },
  { key: "domains", label: "Domains", hint: "jede Adresse dieser Domain zählt" },
  { key: "names", label: "Namen in der Absender-Domain", hint: "z. B. „stadtwerke“ passt auch zu service@stadtwerke-energie.com" },
  { key: "numbers", label: "Nummern", hint: "Kunden-, Vertrags-, Rechnungsnummer" },
  { key: "words", label: "Stichworte", hint: "Wörter, die in der Antwort stehen müssten" },
];

function FeaturesPanel({ p, editable, onSaved }: { p: Pipeline; editable: boolean; onSaved: (p: Pipeline) => void }) {
  const [open, setOpen] = useState(false);
  const [f, setF] = useState<Features>(p.features);
  const [adding, setAdding] = useState<Record<string, string>>({});
  useEffect(() => { setF(p.features); }, [p.features]);
  const dirty = useMemo(() => JSON.stringify(f) !== JSON.stringify(p.features), [f, p.features]);

  async function save() {
    try {
      onSaved(await api.patch<Pipeline>(`/api/pipelines/${p.id}`, {
        features: { addresses: f.addresses, domains: f.domains, names: f.names, numbers: f.numbers, words: f.words || [] },
      }));
      toast("Gespeichert", "success");
    } catch (e: any) { toast(errText(e), "error"); }
  }

  return (
    <Panel title="Woran Yorik die Antwort erkennt" open={open} onToggle={() => setOpen(v => !v)}>
      <p className="text-xs text-muted-foreground mb-3">
        Gesucht wird in allen deinen Postfächern samt Spam, seit {dateShort(p.since_at)}. Jede Mail, die hierzu passt, zeigt Yorik dir, bevor er erinnert.
      </p>
      <div className="space-y-3">
        {FEATURE_GROUPS.map(g => {
          const values = (f[g.key] as string[] | undefined) || [];
          return (
            <div key={g.key}>
              <div className="text-xs font-medium">{g.label} <span className="text-muted-foreground font-normal">· {g.hint}</span></div>
              <div className="flex flex-wrap gap-1.5 mt-1">
                {values.map(v => (
                  <span key={v} className="text-xs rounded-full border border-border px-2 py-0.5 flex items-center gap-1">
                    {v}
                    {editable && (
                      <button aria-label={`${v} entfernen`} onClick={() => setF({ ...f, [g.key]: values.filter(x => x !== v) })}>
                        <X className="w-3 h-3 text-muted-foreground" />
                      </button>
                    )}
                  </span>
                ))}
                {editable && (
                  <input
                    value={adding[g.key] || ""}
                    placeholder="+ hinzufügen"
                    onChange={e => setAdding({ ...adding, [g.key]: e.target.value })}
                    onKeyDown={e => {
                      const v = (adding[g.key] || "").trim();
                      if (e.key === "Enter" && v) {
                        setF({ ...f, [g.key]: [...values, v] });
                        setAdding({ ...adding, [g.key]: "" });
                      }
                    }}
                    className="text-xs bg-transparent border border-dashed border-border rounded-full px-2 py-0.5 w-32"
                  />
                )}
              </div>
            </div>
          );
        })}
      </div>
      {dirty && (
        <button onClick={save} className="mt-3 text-[13px] rounded-md bg-primary text-primary-foreground px-3 py-1.5 font-medium">Speichern</button>
      )}
    </Panel>
  );
}

function WindowPanel({ p, editable, onSaved }: { p: Pipeline; editable: boolean; onSaved: (p: Pipeline) => void }) {
  const [open, setOpen] = useState(false);
  const c: Config = { ...{ send_days: "alle", send_from_hour: 8, send_to_hour: 20 } as Config, ...p.config };
  async function set(patch: Partial<Config>) {
    try { onSaved(await api.patch<Pipeline>(`/api/pipelines/${p.id}`, { config: { ...c, ...patch } })); }
    catch (e: any) { toast(errText(e), "error"); }
  }
  const hours = Array.from({ length: 25 }, (_, i) => i);
  return (
    <Panel title={`Sendezeiten · ${c.send_days === "werktags" ? "werktags" : "jeden Tag"}, ${c.send_from_hour}–${c.send_to_hour} Uhr`}
      open={open} onToggle={() => setOpen(v => !v)}>
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <select disabled={!editable} value={c.send_days} onChange={e => set({ send_days: e.target.value as Config["send_days"] })}
          className="bg-transparent border border-border rounded-md px-2 py-1">
          <option value="alle">jeden Tag</option>
          <option value="werktags">nur werktags</option>
        </select>
        <span className="flex items-center gap-1.5">
          von
          <select disabled={!editable} value={c.send_from_hour} onChange={e => set({ send_from_hour: Number(e.target.value) })}
            className="bg-transparent border border-border rounded-md px-2 py-1">
            {hours.slice(0, 24).map(h => <option key={h} value={h}>{h}</option>)}
          </select>
          bis
          <select disabled={!editable} value={c.send_to_hour} onChange={e => set({ send_to_hour: Number(e.target.value) })}
            className="bg-transparent border border-border rounded-md px-2 py-1">
            {hours.slice(1).map(h => <option key={h} value={h}>{h}</option>)}
          </select>
          Uhr
        </span>
      </div>
      <p className="text-xs text-muted-foreground mt-2">Außerhalb dieser Zeiten fragt Yorik nicht nach dem Senden, sondern wartet.</p>
    </Panel>
  );
}

function Panel({ title, open, onToggle, children }: { title: string; open: boolean; onToggle: () => void; children: React.ReactNode }) {
  return (
    <div className="mt-6 rounded-xl border border-border bg-card">
      <button onClick={onToggle} className="w-full flex items-center justify-between px-4 py-3 text-left">
        <span className="text-sm font-medium">{title}</span>
        {open ? <ChevronDown className="w-4 h-4 text-muted-foreground" /> : <ChevronRight className="w-4 h-4 text-muted-foreground" />}
      </button>
      {open && <div className="px-4 pb-4">{children}</div>}
    </div>
  );
}

// ─── controls and history ─────────────────────────────────────────

function Controls({ p, busy, run, post, onDeleted }: {
  p: Pipeline; busy: string | null;
  run: (label: string, fn: () => Promise<Pipeline>, ok?: string) => Promise<void>;
  post: (path: string, body?: any) => () => Promise<Pipeline>;
  onDeleted: () => void;
}) {
  const [confirmDelete, setConfirmDelete] = useState(false);
  const b = "text-[13px] rounded-md border border-border px-3 py-1.5 hover:bg-muted flex items-center gap-1.5 disabled:opacity-40";
  const ended = p.state === "erledigt" || p.state === "abgebrochen";
  async function del() {
    try { await api.delete(`/api/pipelines/${p.id}`); onDeleted(); }
    catch (e: any) { toast(errText(e), "error"); }
  }
  return (
    <div className="flex flex-wrap gap-2 mt-6">
      {p.state === "laeuft" && (
        <>
          <button className={b} disabled={busy !== null} onClick={() => run("check", post("/check"), "Geprüft")}>
            <RefreshCw className={cn("w-3.5 h-3.5", busy === "check" && "animate-spin")} /> Jetzt prüfen
          </button>
          <button className={b} disabled={busy !== null} onClick={() => run("pause", post("/pause"))}><Pause className="w-3.5 h-3.5" /> Pausieren</button>
        </>
      )}
      {p.state === "pausiert" && (
        <button className={b} disabled={busy !== null} onClick={() => run("resume", post("/resume"))}><Play className="w-3.5 h-3.5" /> Fortsetzen</button>
      )}
      {(p.state === "laeuft" || p.state === "pausiert") && (
        <>
          <button className={b} disabled={busy !== null} onClick={() => run("finish", post("/finish"), "Erledigt")}><CircleCheck className="w-3.5 h-3.5" /> Erledigt</button>
          <button className={b} disabled={busy !== null} onClick={() => run("cancel", post("/cancel"))}><Ban className="w-3.5 h-3.5" /> Abbrechen</button>
        </>
      )}
      {(p.state === "entwurf" || ended) && (
        confirmDelete
          ? <button className={cn(b, "border-rose-500/50 text-rose-500")} onClick={del}><Trash2 className="w-3.5 h-3.5" /> Wirklich löschen</button>
          : <button className={b} onClick={() => setConfirmDelete(true)}><Trash2 className="w-3.5 h-3.5" /> Löschen</button>
      )}
    </div>
  );
}

function History({ events }: { events: PipelineEvent[] }) {
  const [open, setOpen] = useState<number | null>(null);
  if (!events.length) return null;
  return (
    <div className="mt-8">
      <div className="text-[13px] font-medium text-muted-foreground mb-2">Verlauf</div>
      <ul className="space-y-1">
        {events.map(e => (
          <li key={e.id} className="text-xs">
            <button onClick={() => setOpen(o => (o === e.id ? null : e.id))} className="w-full text-left flex gap-2 hover:bg-muted/40 rounded px-1 py-0.5">
              <span className="text-muted-foreground tabular-nums shrink-0 w-28">{timeShort(e.at)}</span>
              <span className={cn(e.kind === "aktion" && "font-medium", e.kind === "mensch" && "text-primary")}>{e.text}</span>
            </button>
            {open === e.id && e.data && e.kind === "pruefung" && (
              <div className="ml-[7.5rem] mt-1 mb-2 text-muted-foreground space-y-0.5">
                {(e.data.problems || []).map((x: string) => <div key={x}>⚠ {x}</div>)}
                {(e.data.candidates || []).map((c: Candidate) => (
                  <div key={c.id}>✉ {c.subject} — {c.from} ({c.why.join(", ")})</div>
                ))}
              </div>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
