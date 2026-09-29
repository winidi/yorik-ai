/**
 * One pipeline: what needs the person now (attention card), the
 * sequence as a timeline with per-mail approval, what the answer is
 * recognised by, the send window, and the history of every check.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import i18n from "@/i18n";
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
  return e?.message || i18n.t("pipelines.failed");
}

let keySeq = 0;
const newKey = () => `n${++keySeq}`;

function toDraft(steps: Step[]): DraftStep[] {
  return steps.filter(s => s.status === "offen").map(s => ({ ...s, key: `s${s.id}` }));
}

export function PipelineDetail({ id }: { id: number }) {
  const { t } = useTranslation();
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
        <p className="text-sm text-muted-foreground mt-6">{t("pipelines.detail.missing")}</p>
      </div>
    );
  }
  if (!p) return <div className="text-sm text-muted-foreground flex items-center gap-2"><Loader2 className="w-4 h-4 animate-spin" /> {t("common.loading")}</div>;

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
    }), t("pipelines.saved"));
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
      {p.goal && <p className="text-sm text-muted-foreground mb-1">{t("pipelines.detail.goal", { goal: p.goal })}</p>}
      {p.last_check && (
        <p className="text-xs text-muted-foreground mb-5 flex items-center gap-1.5">
          <Search className="w-3.5 h-3.5" /> {t("pipelines.detail.lastChecked", { when: timeShort(p.last_check.at), summary: p.last_check.summary })}
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
            <div className="text-sm font-medium">{t("pipelines.detail.draftingTitle")}</div>
            <p className="text-xs text-muted-foreground">
              {t("pipelines.detail.draftingText")}
            </p>
          </div>
        </div>
      )}
      <div className="flex items-center justify-between mt-7 mb-3 gap-2">
        <div className="text-[13px] font-medium text-muted-foreground">{t("pipelines.detail.sequence")}</div>
        <div className="flex items-center gap-2">
        {editable && openMail.length > 0 && (
          <button
            onClick={() => {
              if (openMail.some(s => s.approved || s.payload.source === "bearbeitet")
                  && !window.confirm(t("pipelines.detail.redraftConfirm"))) return;
              void run("redraft", post("/redraft"));
            }}
            disabled={busy !== null || dirty}
            className="text-xs rounded-md border border-border px-2.5 py-1 hover:bg-muted disabled:opacity-40"
          >
            {t("pipelines.detail.redraft")}
          </button>
        )}
        {editable && openMail.length > 0 && !allApproved && !dirty && (
          <button
            onClick={approveAll}
            disabled={!allOpened || busy !== null}
            title={allOpened ? "" : t("pipelines.detail.openEachFirst")}
            className="text-xs rounded-md border border-border px-2.5 py-1 hover:bg-muted disabled:opacity-40"
          >
            {busy === "approveAll" ? "…" : allOpened ? t("pipelines.detail.approveAll") : t("pipelines.detail.approveAllOpenFirst")}
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
              {s.action === "uebergabe" ? t("pipelines.handover") : t("pipelines.reminderSent")}
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
            <Plus className="w-3.5 h-3.5" /> {t("pipelines.detail.addReminder")}
          </button>
          {dirty && (
            <>
              <button onClick={saveSteps} disabled={busy !== null} className="text-[13px] rounded-md bg-primary text-primary-foreground px-3 py-1.5 font-medium">
                {busy === "save" ? t("pipelines.detail.saving") : t("pipelines.detail.saveChanges")}
              </button>
              <button onClick={() => { setDraft(toDraft(p.steps)); setDirty(false); }} className="text-[13px] text-muted-foreground hover:text-foreground px-2">
                {t("pipelines.detail.discard")}
              </button>
            </>
          )}
        </div>
      )}

      {p.state === "entwurf" && (
        <div className="mt-6 rounded-xl border border-border bg-card p-4">
          <div className="text-sm font-medium mb-1">{t("pipelines.detail.start")}</div>
          <p className="text-[13px] text-muted-foreground mb-3">
            {t("pipelines.detail.startText")}
          </p>
          <button
            onClick={() => run("start", post("/start"), t("pipelines.detail.started"))}
            disabled={!allApproved || dirty || busy !== null}
            className="rounded-md bg-primary text-primary-foreground px-4 py-2 text-sm font-medium disabled:opacity-40"
          >
            {busy === "start" ? t("pipelines.detail.starting") : t("pipelines.detail.start")}
          </button>
          {(!allApproved || dirty) && (
            <span className="text-xs text-muted-foreground ml-3">
              {dirty ? t("pipelines.detail.saveFirst") : t("pipelines.detail.approveEachFirst")}
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
  const { t } = useTranslation();
  const label = p.state === "laeuft" ? t("pipelines.state.laeuft") : stateLabel(p.state).split(" —")[0];
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
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  return (
    <div>
      <button onClick={() => setOpen(v => !v)} className="text-left w-full">
        <div className="text-sm">{t("pipelines.detail.yourMail")} <span className="text-muted-foreground">· {dateShort(p.origin.sent_at || p.since_at)}</span></div>
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
  const { t } = useTranslation();
  const isMail = step.action === "mail_senden";
  const [showQuote, setShowQuote] = useState(false);
  const days = (
    <span className="inline-flex items-center gap-1">
      {t("pipelines.step.after")}
      {editable ? (
        <input
          type="number" min={0} max={365} value={step.after_days}
          onChange={e => onChange({ after_days: Math.max(0, Math.min(365, Number(e.target.value) || 0)) })}
          className="w-12 bg-transparent border border-border rounded px-1 py-0 text-center tabular-nums"
          aria-label={t("pipelines.step.daysAria")}
        />
      ) : <b className="tabular-nums">{step.after_days}</b>}
      {t("pipelines.step.daysUnit", { count: step.after_days })}
    </span>
  );

  if (!isMail) {
    return (
      <div>
        <div className="text-sm flex items-center gap-1.5 flex-wrap">{t("pipelines.handover")} <span className="text-muted-foreground text-xs">· {days}{step.due_at && !dirty ? `, ${relDay(step.due_at)}` : ""}</span></div>
        <div className="text-xs text-muted-foreground">{t("pipelines.step.handoverNote")}</div>
      </div>
    );
  }

  return (
    <div className={cn("rounded-xl border bg-card", expanded ? "border-primary/30" : "border-border")}>
      <button onClick={onToggle} className="w-full text-left px-3.5 py-2.5 flex items-center gap-2">
        <div className="min-w-0 flex-1">
          <div className="text-sm flex items-center gap-1.5 flex-wrap">
            {index === 0 ? t("pipelines.step.reminder") : t("pipelines.step.reminderN", { n: index + 1 })}
            <span className="text-muted-foreground text-xs">· {t("pipelines.step.daysLater", { count: step.after_days })}{step.due_at && !dirty ? `, ${relDay(step.due_at)}` : ""}</span>
          </div>
          <div className="text-xs text-muted-foreground truncate">{step.payload.subject}</div>
        </div>
        {step.approved
          ? <span className="text-xs text-emerald-600 flex items-center gap-1 shrink-0"><Check className="w-3.5 h-3.5" /> {t("pipelines.step.approved")}</span>
          : <span className="text-xs text-amber-600 shrink-0">{t("pipelines.step.notApproved")}</span>}
        {expanded ? <ChevronDown className="w-4 h-4 text-muted-foreground" /> : <ChevronRight className="w-4 h-4 text-muted-foreground" />}
      </button>
      {expanded && (
        <div className="px-3.5 pb-3.5 space-y-2.5 border-t border-border pt-3">
          <div className="text-xs text-muted-foreground">
            {days}{t("pipelines.step.afterPrevious")}{step.payload.why ? <> · <span className="italic">{step.payload.why}</span></> : null}
          </div>
          <SourceNote source={step.payload.source} />
          <Field label={t("pipelines.step.to")}>
            <input
              value={(step.payload.to || []).join(", ")}
              disabled={!editable}
              onChange={e => onChange({ payload: { ...step.payload, to: e.target.value.split(/[,;\s]+/).filter(Boolean) } })}
              className="w-full bg-transparent border border-border rounded-md px-2.5 py-1.5 text-sm"
            />
          </Field>
          <Field label={t("pipelines.step.subject")}>
            <input
              value={step.payload.subject || ""}
              disabled={!editable}
              onChange={e => onChange({ payload: { ...step.payload, subject: e.target.value } })}
              className="w-full bg-transparent border border-border rounded-md px-2.5 py-1.5 text-sm"
            />
          </Field>
          <Field label={t("pipelines.step.body")}>
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
                {showQuote ? t("pipelines.step.hideQuote") : t("pipelines.step.showQuote")}
              </button>
              {showQuote && <pre className="mt-1.5 text-xs whitespace-pre-wrap font-sans text-muted-foreground bg-muted/40 rounded-md p-2.5 max-h-48 overflow-y-auto">{quote.trim()}</pre>}
            </div>
          )}
          {editable && (
            <div className="flex items-center justify-between gap-2 pt-1">
              <button onClick={onRemove} className="text-xs text-muted-foreground hover:text-rose-500 flex items-center gap-1">
                <Trash2 className="w-3.5 h-3.5" /> {t("pipelines.step.remove")}
              </button>
              {step.id && !dirty ? (
                step.approved ? (
                  <button onClick={() => onApprove(false)} disabled={busy !== null} className="text-xs rounded-md border border-border px-2.5 py-1.5 hover:bg-muted">
                    {t("pipelines.step.revokeApproval")}
                  </button>
                ) : (
                  <button onClick={() => onApprove(true)} disabled={busy !== null} className="text-[13px] rounded-md bg-primary text-primary-foreground px-3 py-1.5 font-medium flex items-center gap-1">
                    <Check className="w-4 h-4" /> {t("pipelines.step.approve")}
                  </button>
                )
              ) : (
                <span className="text-xs text-muted-foreground">{t("pipelines.step.saveThenApprove")}</span>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function SourceNote({ source }: { source?: string }) {
  const { t } = useTranslation();
  const known = ["llm", "llm_frisch", "vorlage", "bearbeitet"];
  const text = source && known.includes(source) ? t(`pipelines.source.${source}`) : null;
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
  const { t } = useTranslation();
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
            {t("pipelines.attn.maybeText", { count: (d.candidates || []).length > 1 ? 2 : 1 })}
          </p>
          {(d.candidates || []).map((c: Candidate) => (
            <div key={c.id} className="rounded-lg border border-border bg-card p-3">
              <div className="flex items-baseline justify-between gap-2">
                <div className="text-sm font-medium truncate">{c.subject || t("pipelines.noSubject")}</div>
                <div className="text-xs text-muted-foreground shrink-0">{relDay(c.date)}</div>
              </div>
              <div className="text-xs text-muted-foreground truncate">
                {t("pipelines.attn.from", { from: c.from_name ? `${c.from_name} <${c.from}>` : c.from })}{c.folder ? ` · ${c.folder}` : ""}
              </div>
              {c.snippet && <div className="text-xs mt-1 line-clamp-2">{c.snippet}</div>}
              <div className="flex flex-wrap gap-1 mt-1.5">
                {c.why.map(w => <span key={w} className="text-2xs rounded-full bg-muted px-2 py-0.5 text-muted-foreground">{w}</span>)}
              </div>
              <div className="flex flex-wrap gap-2 mt-2.5">
                <button className={primary} disabled={busy !== null}
                  onClick={() => run("answer", post("/answer", { mail_id: c.id, is_answer: true }), t("pipelines.done"))}>
                  {t("pipelines.attn.isAnswer")}
                </button>
                <button className={secondary} disabled={busy !== null}
                  onClick={() => run("answer", post("/answer", { mail_id: c.id, is_answer: false }))}>
                  {t("pipelines.attn.notAnswer")}
                </button>
                <a href={`/r/email?msg=${c.id}`} target="_blank" rel="noreferrer" className="text-xs text-muted-foreground hover:text-foreground self-center">
                  {t("pipelines.attn.openMail")}
                </a>
              </div>
            </div>
          ))}
        </div>
      )}

      {p.attention === "schritt_faellig" && step && (
        <div>
          <p className="text-[13px] text-muted-foreground mb-2">
            {d.summary}. {t("pipelines.attn.noReplyFound")}
            {step.payload.source === "llm_frisch" && ` ${t("pipelines.attn.rewrittenToday")}`}
          </p>
          <div className="rounded-lg border border-border bg-card p-3 mb-3">
            <div className="text-xs text-muted-foreground">{t("pipelines.attn.to", { to: (step.payload.to || []).join(", ") })}</div>
            <div className="text-sm font-medium">{step.payload.subject}</div>
            <pre className="text-xs whitespace-pre-wrap font-sans mt-1.5 max-h-40 overflow-y-auto">{step.payload.body}</pre>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <button className={cn(primary, "flex items-center gap-1.5")} disabled={busy !== null}
              onClick={() => run("send", post(`/steps/${step.id}/send`,
                step.approved ? {} : { approve: true, seen_body: step.payload.body || "" }), t("pipelines.reminderSent"))}>
              {busy === "send" ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
              {step.approved ? t("pipelines.attn.sendNow") : t("pipelines.attn.approveAndSend")}
            </button>
            <span className="text-xs text-muted-foreground">{t("pipelines.attn.editBelow")}</span>
            <span className="text-xs text-muted-foreground">{t("pipelines.attn.checksBeforeSending")}</span>
          </div>
        </div>
      )}

      {p.attention === "kann_nicht_pruefen" && (
        <div>
          <ul className="text-[13px] text-muted-foreground list-disc ml-5 mb-3">
            {(d.problems || []).map((x: string) => <li key={x}>{x}</li>)}
          </ul>
          <p className="text-xs text-muted-foreground mb-3">
            {t("pipelines.attn.cantCheckText")}
          </p>
          <div className="flex flex-wrap gap-2">
            <button className={secondary} disabled={busy !== null} onClick={() => run("check", post("/check"))}>
              <RefreshCw className="w-3.5 h-3.5 inline mr-1" /> {t("pipelines.attn.checkAgain")}
            </button>
            {due && p.next_step?.action === "mail_senden" && (
              insist ? (
                <button className={cn(btn, "bg-amber-500 text-white")} disabled={busy !== null}
                  onClick={() => run("send", post(`/steps/${p.next_step!.id}/send`, { despite_stale: true }), t("pipelines.reminderSent"))}>
                  {t("pipelines.attn.sendAnywayConfirm")}
                </button>
              ) : (
                <button className={secondary} onClick={() => setInsist(true)}>{t("pipelines.attn.sendAnyway")}</button>
              )
            )}
          </div>
        </div>
      )}

      {p.attention === "uebergabe" && (
        <div>
          <p className="text-[13px] text-muted-foreground mb-3">{t("pipelines.attn.handoverText")}</p>
          <div className="flex flex-wrap gap-2">
            <button className={secondary} onClick={onAddReminder}><Plus className="w-3.5 h-3.5 inline mr-1" />{t("pipelines.attn.oneMoreReminder")}</button>
            <button className={secondary} disabled={busy !== null} onClick={() => run("finish", post("/finish"), t("pipelines.done"))}>{t("pipelines.done")}</button>
            <button className={secondary} disabled={busy !== null} onClick={() => run("cancel", post("/cancel"))}>{t("pipelines.cancel")}</button>
          </div>
        </div>
      )}

      {p.attention === "selbst_geantwortet" && (
        <div>
          <p className="text-[13px] text-muted-foreground mb-2">{t("pipelines.attn.selfRepliedText")}</p>
          <ul className="text-[13px] mb-3 space-y-0.5">
            {(d.mails || []).map((m: any) => <li key={m.id}>{t("pipelines.attn.quotedSubject", { subject: m.subject })} <span className="text-muted-foreground">· {relDay(m.date)}</span></li>)}
          </ul>
          <div className="flex flex-wrap gap-2">
            <button className={primary} disabled={busy !== null} onClick={() => run("continue", post("/continue"))}>{t("pipelines.attn.keepFollowing")}</button>
            <button className={secondary} disabled={busy !== null} onClick={() => run("finish", post("/finish"), t("pipelines.done"))}>{t("pipelines.done")}</button>
          </div>
        </div>
      )}

      {p.attention === "versand_unklar" && (
        <div>
          <p className="text-[13px] text-muted-foreground mb-3">
            {d.error ? t("pipelines.attn.sendReported", { error: d.error }) : t("pipelines.attn.sendInterrupted")}
            {" "}{t("pipelines.attn.checkSentFolder")}
          </p>
          <div className="flex flex-wrap gap-2">
            <button className={secondary} disabled={busy !== null} onClick={() => run("unclear", post("/unclear", { was_sent: true }))}>{t("pipelines.attn.wasSent")}</button>
            <button className={secondary} disabled={busy !== null} onClick={() => run("unclear", post("/unclear", { was_sent: false }))}>{t("pipelines.attn.wasNotSent")}</button>
          </div>
        </div>
      )}

      {p.attention === "person_aus" && (
        <p className="text-[13px] text-muted-foreground">{t("pipelines.attn.personOffText")}</p>
      )}
    </div>
  );
}

// ─── what the answer is recognised by ─────────────────────────────

// Labels and hints: pipelines.features.<key>.label / .hint
const FEATURE_GROUPS: { key: keyof Features }[] = [
  { key: "addresses" },
  { key: "domains" },
  { key: "names" },
  { key: "numbers" },
  { key: "words" },
];

function FeaturesPanel({ p, editable, onSaved }: { p: Pipeline; editable: boolean; onSaved: (p: Pipeline) => void }) {
  const { t } = useTranslation();
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
      toast(t("pipelines.saved"), "success");
    } catch (e: any) { toast(errText(e), "error"); }
  }

  return (
    <Panel title={t("pipelines.features.title")} open={open} onToggle={() => setOpen(v => !v)}>
      <p className="text-xs text-muted-foreground mb-3">
        {t("pipelines.features.scope", { date: dateShort(p.since_at) })}
      </p>
      <div className="space-y-3">
        {FEATURE_GROUPS.map(g => {
          const values = (f[g.key] as string[] | undefined) || [];
          return (
            <div key={g.key}>
              <div className="text-xs font-medium">{t(`pipelines.features.${g.key}.label`)} <span className="text-muted-foreground font-normal">· {t(`pipelines.features.${g.key}.hint`)}</span></div>
              <div className="flex flex-wrap gap-1.5 mt-1">
                {values.map(v => (
                  <span key={v} className="text-xs rounded-full border border-border px-2 py-0.5 flex items-center gap-1">
                    {v}
                    {editable && (
                      <button aria-label={t("pipelines.features.removeValue", { value: v })} onClick={() => setF({ ...f, [g.key]: values.filter(x => x !== v) })}>
                        <X className="w-3 h-3 text-muted-foreground" />
                      </button>
                    )}
                  </span>
                ))}
                {editable && (
                  <input
                    value={adding[g.key] || ""}
                    placeholder={t("pipelines.features.addPlaceholder")}
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
        <button onClick={save} className="mt-3 text-[13px] rounded-md bg-primary text-primary-foreground px-3 py-1.5 font-medium">{t("common.save")}</button>
      )}
    </Panel>
  );
}

function WindowPanel({ p, editable, onSaved }: { p: Pipeline; editable: boolean; onSaved: (p: Pipeline) => void }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const c: Config = { ...{ send_days: "alle", send_from_hour: 8, send_to_hour: 20 } as Config, ...p.config };
  async function set(patch: Partial<Config>) {
    try { onSaved(await api.patch<Pipeline>(`/api/pipelines/${p.id}`, { config: { ...c, ...patch } })); }
    catch (e: any) { toast(errText(e), "error"); }
  }
  const hours = Array.from({ length: 25 }, (_, i) => i);
  return (
    <Panel title={t("pipelines.window.title", {
      days: c.send_days === "werktags" ? t("pipelines.window.weekdays") : t("pipelines.window.everyDay"),
      from: c.send_from_hour, to: c.send_to_hour,
    })}
      open={open} onToggle={() => setOpen(v => !v)}>
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <select disabled={!editable} value={c.send_days} onChange={e => set({ send_days: e.target.value as Config["send_days"] })}
          className="bg-transparent border border-border rounded-md px-2 py-1">
          <option value="alle">{t("pipelines.window.everyDay")}</option>
          <option value="werktags">{t("pipelines.window.onlyWeekdays")}</option>
        </select>
        <span className="flex items-center gap-1.5">
          {t("pipelines.window.from")}
          <select disabled={!editable} value={c.send_from_hour} onChange={e => set({ send_from_hour: Number(e.target.value) })}
            className="bg-transparent border border-border rounded-md px-2 py-1">
            {hours.slice(0, 24).map(h => <option key={h} value={h}>{h}</option>)}
          </select>
          {t("pipelines.window.to")}
          <select disabled={!editable} value={c.send_to_hour} onChange={e => set({ send_to_hour: Number(e.target.value) })}
            className="bg-transparent border border-border rounded-md px-2 py-1">
            {hours.slice(1).map(h => <option key={h} value={h}>{h}</option>)}
          </select>
          {t("pipelines.window.hourUnit")}
        </span>
      </div>
      <p className="text-xs text-muted-foreground mt-2">{t("pipelines.window.note")}</p>
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
  const { t } = useTranslation();
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
          <button className={b} disabled={busy !== null} onClick={() => run("check", post("/check"), t("pipelines.controls.checked"))}>
            <RefreshCw className={cn("w-3.5 h-3.5", busy === "check" && "animate-spin")} /> {t("pipelines.controls.checkNow")}
          </button>
          <button className={b} disabled={busy !== null} onClick={() => run("pause", post("/pause"))}><Pause className="w-3.5 h-3.5" /> {t("pipelines.controls.pause")}</button>
        </>
      )}
      {p.state === "pausiert" && (
        <button className={b} disabled={busy !== null} onClick={() => run("resume", post("/resume"))}><Play className="w-3.5 h-3.5" /> {t("pipelines.controls.resume")}</button>
      )}
      {(p.state === "laeuft" || p.state === "pausiert") && (
        <>
          <button className={b} disabled={busy !== null} onClick={() => run("finish", post("/finish"), t("pipelines.done"))}><CircleCheck className="w-3.5 h-3.5" /> {t("pipelines.done")}</button>
          <button className={b} disabled={busy !== null} onClick={() => run("cancel", post("/cancel"))}><Ban className="w-3.5 h-3.5" /> {t("pipelines.cancel")}</button>
        </>
      )}
      {(p.state === "entwurf" || ended) && (
        confirmDelete
          ? <button className={cn(b, "border-rose-500/50 text-rose-500")} onClick={del}><Trash2 className="w-3.5 h-3.5" /> {t("pipelines.controls.reallyDelete")}</button>
          : <button className={b} onClick={() => setConfirmDelete(true)}><Trash2 className="w-3.5 h-3.5" /> {t("common.delete")}</button>
      )}
    </div>
  );
}

function History({ events }: { events: PipelineEvent[] }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState<number | null>(null);
  if (!events.length) return null;
  return (
    <div className="mt-8">
      <div className="text-[13px] font-medium text-muted-foreground mb-2">{t("pipelines.history")}</div>
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
