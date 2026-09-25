/**
 * Pipelines — Yorik follows a matter until it is done. Stage 1: a sent
 * mail is followed until the answer comes; every reminder asks before
 * it goes out. See docs/plans/2026-09-25-pipelines.md.
 *
 * /pipelines        overview: what needs you, what runs, what is done
 * /pipelines/:id    one pipeline: attention card, timeline, history
 */
import { useCallback, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Workflow, Plus, ChevronRight, ChevronDown, Loader2, X, Mail, Users } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { Dock } from "@/components/Dock";
import { toast } from "@/components/Toast";
import { PipelineDetail } from "./PipelineDetail";
import type { Pipeline, PersonSwitch, SentMail } from "./types";
import { attentionLabel, relDay, stateLabel } from "./format";

export function PipelinesApp() {
  const { id } = useParams();
  return (
    <div className="h-screen overflow-y-auto bg-background">
      <div className="px-4 sm:px-6 pt-6 pb-28 max-w-2xl lg:max-w-3xl mx-auto w-full">
        {id ? <PipelineDetail id={Number(id)} /> : <Overview />}
      </div>
      <Dock activeAppId="pipelines" />
    </div>
  );
}

function Overview() {
  const navigate = useNavigate();
  const [items, setItems] = useState<Pipeline[] | null>(null);
  const [me, setMe] = useState<{ enabled: boolean; can_manage_people: boolean } | null>(null);
  const [picking, setPicking] = useState(false);
  const [showDone, setShowDone] = useState(false);

  const load = useCallback(async () => {
    try {
      const [list, who] = await Promise.all([
        api.get<Pipeline[]>("/api/pipelines"),
        api.get<{ enabled: boolean; can_manage_people: boolean }>("/api/pipelines/me"),
      ]);
      setItems(list);
      setMe(who);
    } catch (e: any) {
      setItems([]);
      toast(e?.message || "Pipelines konnten nicht geladen werden", "error");
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const needs = (items || []).filter(p => p.state === "laeuft" && p.attention);
  const running = (items || []).filter(p => (p.state === "laeuft" && !p.attention) || p.state === "pausiert");
  const drafts = (items || []).filter(p => p.state === "entwurf");
  const done = (items || []).filter(p => p.state === "erledigt" || p.state === "abgebrochen");

  return (
    <>
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2.5">
          <Workflow className="w-5 h-5 text-muted-foreground" />
          <h1 className="text-lg font-semibold">Pipelines</h1>
        </div>
        {me?.enabled !== false && (
          <button
            onClick={() => setPicking(true)}
            className="flex items-center gap-1.5 rounded-md bg-primary text-primary-foreground px-3 py-1.5 text-sm font-medium"
          >
            <Plus className="w-4 h-4" /> Antwort verfolgen
          </button>
        )}
      </div>
      <p className="text-sm text-muted-foreground mb-7">
        Yorik bleibt dran, bis die Antwort da ist, und fragt dich vor jeder Erinnerung.
      </p>

      {me?.enabled === false && (
        <div className="rounded-xl border border-amber-500/30 bg-amber-500/5 p-4 text-sm mb-6">
          Pipelines sind für dich ausgeschaltet. Deine Eltern oder ein Admin können sie wieder einschalten.
        </div>
      )}

      {picking && (
        <SentMailPicker
          onClose={() => setPicking(false)}
          onPicked={async (mailId) => {
            try {
              const p = await api.post<Pipeline>("/api/pipelines", { mail_id: mailId });
              navigate(`/pipelines/${p.id}`);
            } catch (e: any) { toast(e?.message || "Anlegen fehlgeschlagen", "error"); }
          }}
        />
      )}

      {items === null && <div className="text-sm text-muted-foreground flex items-center gap-2"><Loader2 className="w-4 h-4 animate-spin" /> Lädt…</div>}

      {items && items.length === 0 && (
        <div className="rounded-xl border border-border bg-card p-5 text-sm text-muted-foreground leading-relaxed">
          Noch keine Pipeline. Nimm eine Mail, die du geschickt hast, zum Beispiel eine Kündigung oder eine
          Anfrage: Yorik schaut, ob eine Antwort kommt, auch von einer anderen Adresse, und schlägt dir
          sonst eine Erinnerung vor.
        </div>
      )}

      {needs.length > 0 && (
        <Section title="Braucht dich" accent>
          {needs.map(p => <Row key={p.id} p={p} />)}
        </Section>
      )}
      {running.length > 0 && (
        <Section title="Laufend">
          {running.map(p => <Row key={p.id} p={p} />)}
        </Section>
      )}
      {drafts.length > 0 && (
        <Section title="Entwürfe">
          {drafts.map(p => <Row key={p.id} p={p} />)}
        </Section>
      )}
      {done.length > 0 && (
        <div className="mt-8">
          <button
            onClick={() => setShowDone(v => !v)}
            className="flex items-center gap-1.5 text-[13px] text-muted-foreground hover:text-foreground mb-2"
          >
            {showDone ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
            Beendet ({done.length})
          </button>
          {showDone && <div className="space-y-2">{done.map(p => <Row key={p.id} p={p} />)}</div>}
        </div>
      )}

      {me?.can_manage_people && <PeopleSwitches />}
    </>
  );
}

function Section({ title, accent, children }: { title: string; accent?: boolean; children: React.ReactNode }) {
  return (
    <div className="mb-7">
      <div className={cn("text-[13px] font-medium mb-2", accent ? "text-primary" : "text-muted-foreground")}>{title}</div>
      <div className="space-y-2">{children}</div>
    </div>
  );
}

function Row({ p }: { p: Pipeline }) {
  const navigate = useNavigate();
  const sub = p.state === "laeuft" && p.attention
    ? attentionLabel(p.attention)
    : p.state === "laeuft" && p.next_step
      ? (p.next_step.action === "uebergabe" ? "Übergabe an dich " : "Nächste Erinnerung ") + relDay(p.next_step.due_at)
      : stateLabel(p.state);
  const sentCount = p.steps.filter(s => s.action === "mail_senden" && s.status === "erledigt").length;
  const mailSteps = p.steps.filter(s => s.action === "mail_senden").length;
  return (
    <button
      onClick={() => navigate(`/pipelines/${p.id}`)}
      className={cn(
        "w-full text-left rounded-xl border bg-card px-4 py-3 flex items-center gap-3 hover:bg-muted/40 transition",
        p.state === "laeuft" && p.attention ? "border-primary/40" : "border-border",
      )}
    >
      <Mail className="w-4 h-4 text-muted-foreground shrink-0" />
      <div className="min-w-0 flex-1">
        <div className="text-sm font-medium truncate">{p.title}</div>
        <div className={cn("text-[12px] truncate", p.state === "laeuft" && p.attention ? "text-primary" : "text-muted-foreground")}>
          {sub}
        </div>
      </div>
      {mailSteps > 0 && p.state !== "entwurf" && (
        <div className="text-[11px] text-muted-foreground tabular-nums shrink-0">{sentCount}/{mailSteps}</div>
      )}
      <ChevronRight className="w-4 h-4 text-muted-foreground shrink-0" />
    </button>
  );
}

function SentMailPicker({ onClose, onPicked }: { onClose: () => void; onPicked: (id: number) => void }) {
  const [mails, setMails] = useState<SentMail[] | null>(null);
  const [busy, setBusy] = useState<number | null>(null);
  useEffect(() => {
    api.get<SentMail[]>("/api/pipelines/sent-mails").then(setMails).catch(() => setMails([]));
  }, []);
  return (
    <div className="fixed inset-0 z-50 bg-black/50 flex items-end sm:items-center justify-center p-0 sm:p-4" onClick={onClose}>
      <div className="bg-card border border-border rounded-t-2xl sm:rounded-2xl w-full max-w-lg max-h-[80vh] flex flex-col" onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between px-5 pt-4 pb-3 border-b border-border">
          <div>
            <div className="font-medium">Welche Mail verfolgen?</div>
            <div className="text-[12px] text-muted-foreground">Deine zuletzt gesendeten Mails</div>
          </div>
          <button onClick={onClose} className="p-1.5 rounded-md hover:bg-muted" aria-label="Schließen"><X className="w-4 h-4" /></button>
        </div>
        <div className="overflow-y-auto p-2">
          {mails === null && <div className="p-4 text-sm text-muted-foreground">Lädt…</div>}
          {mails && mails.length === 0 && <div className="p-4 text-sm text-muted-foreground">Keine gesendeten Mails gefunden.</div>}
          {mails?.map(m => (
            <button
              key={m.id}
              disabled={busy !== null}
              onClick={() => { setBusy(m.id); onPicked(m.id); }}
              className="w-full text-left rounded-lg px-3 py-2.5 hover:bg-muted/60 flex gap-3 items-start"
            >
              <div className="min-w-0 flex-1">
                <div className="text-sm font-medium truncate">{m.subject || "(ohne Betreff)"}</div>
                <div className="text-[12px] text-muted-foreground truncate">an {m.to.join(", ")} · {relDay(m.date)}</div>
              </div>
              {busy === m.id && <Loader2 className="w-4 h-4 animate-spin mt-1" />}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

function PeopleSwitches() {
  const [open, setOpen] = useState(false);
  const [people, setPeople] = useState<PersonSwitch[] | null>(null);
  const load = useCallback(() => {
    api.get<PersonSwitch[]>("/api/pipelines/people").then(setPeople).catch(() => setPeople([]));
  }, []);
  useEffect(() => { if (open && people === null) load(); }, [open, people, load]);

  async function flip(p: PersonSwitch) {
    try {
      await api.put(`/api/pipelines/people/${p.id}`, { enabled: !p.enabled });
      toast(`Pipelines für ${p.name} ${p.enabled ? "aus" : "an"}`, "success");
      load();
    } catch (e: any) { toast(e?.message || "Ging nicht", "error"); }
  }

  return (
    <div className="mt-10 border-t border-border pt-5">
      <button onClick={() => setOpen(v => !v)} className="flex items-center gap-1.5 text-[13px] text-muted-foreground hover:text-foreground">
        <Users className="w-4 h-4" /> Wer darf Pipelines nutzen
        {open ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
      </button>
      {open && (
        <div className="mt-3 space-y-1.5">
          <p className="text-[12px] text-muted-foreground mb-2">
            Für alle an. Ausschalten hält laufende Pipelines der Person an; ihre Inhalte siehst du dadurch nicht.
          </p>
          {people === null && <div className="text-sm text-muted-foreground">Lädt…</div>}
          {people?.map(p => (
            <div key={p.id} className="flex items-center justify-between rounded-lg border border-border bg-card px-3 py-2">
              <div className="min-w-0">
                <div className="text-sm">{p.name}</div>
                {!p.can_change ? (
                  <div className="text-[11px] text-muted-foreground">nur ein Admin kann das ändern</div>
                ) : p.changed_by && (
                  <div className="text-[11px] text-muted-foreground">zuletzt geändert von {p.changed_by}</div>
                )}
              </div>
              {p.can_change && <Toggle on={p.enabled} onClick={() => flip(p)} label={`Pipelines für ${p.name}`} />}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function Toggle({ on, onClick, label, disabled }: { on: boolean; onClick: () => void; label: string; disabled?: boolean }) {
  return (
    <button
      role="switch"
      aria-checked={on}
      aria-label={label}
      disabled={disabled}
      onClick={onClick}
      className={cn(
        "relative w-10 h-6 rounded-full transition shrink-0 disabled:opacity-40",
        on ? "bg-primary" : "bg-muted-foreground/30",
      )}
    >
      <span className={cn("absolute top-0.5 w-5 h-5 rounded-full bg-background shadow transition-all", on ? "left-[18px]" : "left-0.5")} />
    </button>
  );
}
