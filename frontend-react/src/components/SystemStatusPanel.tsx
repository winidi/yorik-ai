/**
 * System status for the admin: service chips (LLM, email, Paperless,
 * backup, numbering) and the background workers. Lives in
 * Settings → System; Home only shows the one-line summary from
 * `useHouseHealth`, so the family never sees model names or worker ids.
 */

import { useCallback, useEffect, useState } from "react";
import { Server, Mail, FileText, Database, Hash, RefreshCw } from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";
import { WorkersStatus } from "@/components/WorkersStatus";

export interface SystemStatus {
  llm: { model: string; base_url: string; reachable: boolean };
  email: { configured: boolean; kinds: string[] };
  paperless: { admin_token_set: boolean; url: string | null };
  backup: { last: any; configured: boolean };
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
  if (status && !status.llm.reachable) {
    out.push({ tone: "error", text: "Yorik can't think right now" });
  }
  for (const w of workers) {
    if (w.name.startsWith("backup_")) continue;
    if (w.status === "error") out.push({ tone: "error", text: `${workerLabel(w.name)} stopped` });
  }
  if (status && !status.backup.configured) {
    out.push({ tone: "warn", text: "Backup isn't set up yet" });
  }
  for (const w of workers) {
    if (w.name.startsWith("backup_")) continue;
    if (w.status === "warn") out.push({ tone: "warn", text: `${workerLabel(w.name)} needs a look` });
  }
  return out;
}

function workerLabel(name: string): string {
  if (/^email_account_/.test(name) || name === "email_supervisor") return "Email";
  if (name.startsWith("whatsapp_")) return "WhatsApp";
  if (name.startsWith("paperless")) return "Documents";
  if (name.startsWith("calendar")) return "Calendar sync";
  if (name === "bank-sync") return "Bank sync";
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

  return { status, workers, loaded, refresh, issues: healthIssues(status, isAdmin ? workers : []) };
}

export function SystemStatusPanel() {
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

  return (
    <div className="space-y-8">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">System</h1>
          <p className="text-sm text-muted-foreground mt-1">
            What runs behind Yorik. Only admins see this page; everyone else gets one line on Home when something needs attention.
          </p>
        </div>
        <button
          onClick={refresh}
          className="text-muted-foreground hover:text-foreground transition shrink-0 mt-1"
          title="Refresh status"
        >
          <RefreshCw className={cn("w-4 h-4", loading && "animate-spin")} />
        </button>
      </div>

      <section>
        <h2 className="text-xs text-muted-foreground font-semibold mb-3">
          Services
        </h2>
        <div className="grid grid-cols-2 sm:grid-cols-3 gap-2.5">
          <StatusChip
            icon={Server}
            label="LLM"
            detail={status?.llm.model || "—"}
            tone={!status ? "loading" : status.llm.reachable ? "ok" : "error"}
            hint={status?.llm.reachable ? "Reachable" : "Unreachable"}
          />
          <StatusChip
            icon={Mail}
            label="Email"
            detail={status?.email.kinds?.[0] || "Not configured"}
            tone={!status ? "loading" : status.email.configured ? "ok" : "off"}
            hint={status?.email.configured ? "Ready" : "Optional"}
          />
          <StatusChip
            icon={FileText}
            label="Paperless"
            detail={status?.paperless.admin_token_set ? "Linked" : "Not linked"}
            tone={!status ? "loading" : status?.paperless.admin_token_set ? "ok" : "off"}
            hint={status?.paperless.admin_token_set ? "Token set" : "Optional"}
          />
          <StatusChip
            icon={Database}
            label="Backup"
            detail={backupSummary(status)}
            tone={!status ? "loading" : status.backup.configured ? "ok" : "off"}
            hint={status?.backup.last?.finished_at || ""}
          />
          <StatusChip
            icon={Hash}
            label="Numbering"
            detail={status ? `${status.counts.numbering_series || 0} series` : "—"}
            tone={!status ? "loading" : (status.counts.numbering_series || 0) > 0 ? "ok" : "off"}
            hint={(status?.counts.numbering_series || 0) > 0 ? "Configured" : "Optional"}
          />
        </div>
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
  if (!s) return "—";
  if (!s.backup.configured) return "Not set up";
  if (!s.backup.last) return "No runs yet";
  const ts = s.backup.last?.finished_at || s.backup.last?.started_at;
  if (!ts) return "Configured";
  const d = new Date(ts.replace(" ", "T") + (ts.includes("T") ? "" : "Z"));
  if (isNaN(d.getTime())) return "Configured";
  const diffMin = (Date.now() - d.getTime()) / 60_000;
  if (diffMin < 60)        return `${Math.round(diffMin)}m ago`;
  if (diffMin < 60 * 24)   return `${Math.round(diffMin / 60)}h ago`;
  return `${Math.round(diffMin / (60 * 24))}d ago`;
}
