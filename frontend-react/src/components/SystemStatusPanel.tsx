/**
 * System status for the admin: service chips (AI model, email,
 * documents, backup) and the background workers. Lives in
 * Settings → System; Home only shows the one-line summary from
 * `useHouseHealth`, so the family never sees model names or worker ids.
 */

import { useCallback, useEffect, useState } from "react";
import { Server, Mail, FileText, Database, RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";
import i18n from "@/i18n";
import { WorkersStatus } from "@/components/WorkersStatus";
import { PhonesAccessCard } from "@/components/PhonesAccessCard";
import { UpdateCard } from "@/components/UpdateCard";

export interface SystemStatus {
  llm: {
    model: string; base_url: string; reachable: boolean;
    /** First start of the Docker install: the model is still downloading. */
    download?: { model: string; status: "downloading" | "failed"; percent: number | null };
  };
  /** null for members: the server only gives them what Home needs. */
  email: { configured: boolean; kinds: string[] } | null;
  paperless: { admin_token_set: boolean; url: string | null } | null;
  backup: { last: { status?: "ok" | "failed" | "running"; error?: string | null; finished_at?: string; started_at?: string } | null; configured: boolean } | null;
  counts: Record<string, number>;
  user: { name: string; role: string; language: string };
  configured_connectors: string[];
}

interface WorkerLite { name: string; status: "ok" | "warn" | "error" | "starting"; detail: string }

export interface HealthIssue { tone: "warn" | "error"; text: string }

/** The things someone has to act on, worst first. Optional services
 *  (email, Paperless, numbering) that are simply not set up are not
 *  issues. The backup scheduler's own "no schedule" warning is the
 *  same fact as "backup not set up", so it is folded into that. */
export function healthIssues(status: SystemStatus | null, workers: WorkerLite[]): HealthIssue[] {
  const out: HealthIssue[] = [];
  const dl = status?.llm.download;
  if (dl?.status === "downloading") {
    out.push({ tone: "warn", text: i18n.t("settings.health.downloading", { pct: dl.percent != null ? ` (${dl.percent}%)` : "" }) });
  } else if (dl?.status === "failed") {
    out.push({ tone: "error", text: i18n.t("settings.health.downloadStopped") });
  } else if (status && !status.llm.reachable) {
    out.push({ tone: "error", text: i18n.t("settings.health.cantThink") });
  }
  for (const w of workers) {
    if (w.name.startsWith("backup_")) continue;
    if (w.status === "error") out.push({ tone: "error", text: i18n.t("settings.health.workerStopped", { name: workerLabel(w.name) }) });
  }
  if (status?.backup && !status.backup.configured) {
    out.push({ tone: "warn", text: i18n.t("settings.health.backupNotSetUp") });
  } else if (status?.backup?.last?.status === "failed") {
    out.push({ tone: "error", text: i18n.t("settings.health.lastBackupFailed") });
  }
  for (const w of workers) {
    if (w.name.startsWith("backup_")) continue;
    if (w.status === "warn") out.push({ tone: "warn", text: i18n.t("settings.health.workerNeedsLook", { name: workerLabel(w.name) }) });
  }
  return out;
}

function workerLabel(name: string): string {
  if (/^email_account_/.test(name) || name === "email_supervisor") return i18n.t("settings.health.workers.email");
  if (name.startsWith("whatsapp_")) return i18n.t("settings.health.workers.whatsapp");
  if (name.startsWith("paperless")) return i18n.t("settings.health.workers.documents");
  if (name.startsWith("calendar")) return i18n.t("settings.health.workers.calendar");
  if (name === "bank-sync") return i18n.t("settings.health.workers.bank");
  const s = name.replace(/[_-]+/g, " ").trim();
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/** Status + workers for the Home summary line. Admin only: members get
 *  `null` status from the admin endpoints anyway, and we skip the
 *  worker poll for them. */
export function useHouseHealth(isAdmin: boolean) {
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [workers, setWorkers] = useState<WorkerLite[]>([]);
  const [loaded, setLoaded] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setStatus(await api.get<SystemStatus>("/api/system/status?role=admin"));
    } catch { /* Home stays usable without it */ }
    if (isAdmin) {
      try {
        const r = await api.get<{ workers: WorkerLite[] }>("/api/dashboard/workers");
        setWorkers(r.workers || []);
      } catch { /* same */ }
    }
    setLoaded(true);
  }, [isAdmin]);

  useEffect(() => { refresh(); }, [refresh]);
  // While the model downloads, look again every 15 s so the percentage moves.
  const downloading = status?.llm.download?.status === "downloading";
  useEffect(() => {
    if (!downloading) return;
    const t = window.setInterval(refresh, 15000);
    return () => window.clearInterval(t);
  }, [downloading, refresh]);

  return { status, workers, loaded, refresh, issues: healthIssues(status, isAdmin ? workers : []) };
}

export function SystemStatusPanel() {
  const { t } = useTranslation();
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setStatus(await api.get<SystemStatus>("/api/system/status?role=admin"));
    } catch { /* chips stay in their loading tone */ }
    finally { setLoading(false); }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const [docsBusy, setDocsBusy] = useState(false);
  const [docsNote, setDocsNote] = useState<string | null>(null);
  async function reconnectDocs() {
    setDocsBusy(true); setDocsNote(null);
    try {
      const r = await api.post<{ ok?: boolean; error?: string; checked?: number }>("/api/documents/sync-paperless");
      setDocsNote(r.error ? r.error : t("settings.connections.docsConnected", { count: r.checked ?? 0 }));
      refresh();
    } catch (e: any) {
      setDocsNote(e?.message || t("settings.connections.failed"));
    } finally { setDocsBusy(false); }
  }

  return (
    <div className="space-y-8">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">{t("settings.health.title")}</h1>
          <p className="text-sm text-muted-foreground mt-1">
            {t("settings.health.intro")}
          </p>
        </div>
        <button
          onClick={refresh}
          className="text-muted-foreground hover:text-foreground transition shrink-0 mt-1"
          title={t("settings.health.refresh")}
        >
          <RefreshCw className={cn("w-4 h-4", loading && "animate-spin")} />
        </button>
      </div>

      <section>
        <h2 className="text-xs text-muted-foreground font-semibold mb-3">
          {t("settings.health.services")}
        </h2>
        <div className="grid grid-cols-2 sm:grid-cols-3 gap-2.5">
          <StatusChip
            icon={Server}
            label={t("settings.health.aiModel")}
            detail={status?.llm.model || "—"}
            tone={!status ? "loading" : status.llm.reachable ? "ok" : "error"}
            hint={status?.llm.reachable ? t("settings.health.reachable") : t("settings.health.unreachable")}
          />
          <StatusChip
            icon={Mail}
            label={t("settings.health.email")}
            detail={status?.email?.kinds?.[0] || t("settings.health.notConfigured")}
            tone={!status ? "loading" : status.email?.configured ? "ok" : "off"}
            hint={status?.email?.configured ? t("settings.health.ready") : t("settings.health.optional")}
          />
          <StatusChip
            icon={FileText}
            label={t("settings.health.documents")}
            detail={status?.paperless?.admin_token_set ? t("settings.health.linked") : t("settings.health.notLinked")}
            tone={!status ? "loading" : status?.paperless?.admin_token_set ? "ok" : "off"}
            hint={status?.paperless?.admin_token_set ? t("settings.health.docsConnected") : t("settings.health.optional")}
          />
          <StatusChip
            icon={Database}
            label={t("settings.health.backup")}
            detail={backupSummary(status)}
            tone={!status ? "loading" : status.backup?.last?.status === "failed" ? "error" : status.backup?.configured ? "ok" : "off"}
            hint={status?.backup?.last?.status === "failed" ? (status.backup.last.error || t("settings.health.lastRunFailed")) : (status?.backup?.last?.finished_at || "")}
          />
        </div>
      </section>

      <UpdateCard />

      <PhonesAccessCard />

      <section className="space-y-2">
        <h2 className="text-sm font-semibold text-muted-foreground">{t("settings.health.repair")}</h2>
        <div className="flex flex-wrap items-center gap-3">
          <button
            onClick={reconnectDocs}
            disabled={docsBusy}
            className="px-3 py-2 rounded-lg border border-border bg-card hover:border-foreground/20 text-sm transition disabled:opacity-60"
          >
            {docsBusy ? t("settings.health.reconnecting") : t("settings.health.reconnectDocs")}
          </button>
          {docsNote && <span className="text-sm text-muted-foreground">{docsNote}</span>}
        </div>
        <p className="text-xs text-muted-foreground">
          {t("settings.health.reconnectHint")}
        </p>
      </section>

      <WorkersStatus />

    </div>
  );
}

type Tone = "ok" | "off" | "error" | "loading";

function StatusChip({ icon: Icon, label, detail, tone, hint }: {
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  detail: string;
  tone: Tone;
  hint?: string;
}) {
  const dotClass =
    tone === "ok"      ? "bg-emerald-500" :
    tone === "error"   ? "bg-red-500"     :
    tone === "off"     ? "bg-muted-foreground/40" :
                         "bg-muted-foreground/30 animate-pulse";
  return (
    <div className="bg-card border border-border rounded-xl p-3 text-left" title={hint}>
      <div className="flex items-center gap-2 mb-1.5">
        <Icon className="w-3.5 h-3.5 text-muted-foreground" />
        <span className="text-2xs text-muted-foreground font-semibold flex-1">
          {label}
        </span>
        <span className={cn("w-2 h-2 rounded-full", dotClass)} />
      </div>
      <div className="text-sm font-medium truncate" title={detail}>{detail}</div>
    </div>
  );
}

function backupSummary(s: SystemStatus | null): string {
  if (!s || !s.backup) return "—";
  if (!s.backup.configured) return i18n.t("settings.health.notSetUp");
  if (!s.backup.last) return i18n.t("settings.health.noRuns");
  if (s.backup.last.status === "failed") return i18n.t("settings.health.lastRunFailed");
  if (s.backup.last.status === "running") return i18n.t("settings.health.runningNow");
  const ts = s.backup.last?.finished_at || s.backup.last?.started_at;
  if (!ts) return i18n.t("settings.health.configured");
  const d = new Date(ts.replace(" ", "T") + (ts.includes("T") ? "" : "Z"));
  if (isNaN(d.getTime())) return i18n.t("settings.health.configured");
  const diffMin = (Date.now() - d.getTime()) / 60_000;
  if (diffMin < 60)        return i18n.t("settings.health.minAgo", { n: Math.round(diffMin) });
  if (diffMin < 60 * 24)   return i18n.t("settings.health.hoursAgo", { n: Math.round(diffMin / 60) });
  return i18n.t("settings.health.daysAgo", { n: Math.round(diffMin / (60 * 24)) });
}
