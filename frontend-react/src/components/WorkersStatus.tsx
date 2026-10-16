/**
 * Liveness chips for long-running background workers.
 *
 * Polls /api/dashboard/workers every 15s. Each worker gets a colored
 * pill — green ok, amber warn (degraded but alive), red error (crashed
 * or never started). Hovering shows the detail line ("connected to
 * Baileys", "reconnecting in 4s", etc.) and the last-heartbeat age.
 *
 * Hidden entirely when there are no workers registered yet (fresh
 * boot, first 10s) so the home screen doesn't flash an empty section.
 */

import { useCallback, useEffect, useState } from "react";
import {
  Activity, CheckCircle2, AlertTriangle, XCircle, Loader2,
  Mail, MessageSquare, Database, Cog,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";
import i18n from "@/i18n";

interface Worker {
  name: string;
  status: "ok" | "warn" | "error" | "starting";
  detail: string;
  kind: string;
  last_heartbeat_age_s: number | null;
  uptime_s: number;
  error_count: number;
}

interface WorkersResponse { workers: Worker[]; }

// Worker name → icon. Catch-all for unknown workers so a new worker
// landing in the registry without a frontend change still renders.
const WORKER_META: Array<{ match: RegExp; icon: any; label: (n: string) => string }> = [
  { match: /^email_supervisor$/,  icon: Mail,          label: () => i18n.t("settings.workers.emailSupervisor") },
  { match: /^email_account_(\d+)$/, icon: Mail,         label: (n) => i18n.t("settings.workers.emailAccount", { n: n.split("_").pop() }) },
  { match: /^whatsapp_/,          icon: MessageSquare, label: () => i18n.t("settings.workers.whatsappBridge") },
  { match: /^backup_/,            icon: Database,      label: () => i18n.t("settings.workers.backupScheduler") },
];

function metaFor(name: string) {
  const found = WORKER_META.find(m => m.match.test(name));
  return found
    ? { icon: found.icon, label: found.label(name) }
    : { icon: Cog, label: name };
}

function statusIcon(s: Worker["status"]) {
  switch (s) {
    case "ok":       return { Icon: CheckCircle2,   cls: "text-emerald-500" };
    case "warn":     return { Icon: AlertTriangle,  cls: "text-amber-500"   };
    case "error":    return { Icon: XCircle,        cls: "text-red-500"     };
    case "starting": return { Icon: Loader2,        cls: "text-muted-foreground animate-spin" };
  }
}

function formatAge(s: number | null): string {
  if (s == null) return "—";
  if (s < 60)    return i18n.t("settings.workers.secAgo", { n: s });
  if (s < 3600)  return i18n.t("settings.workers.minAgo", { n: Math.floor(s / 60) });
  return i18n.t("settings.workers.hoursAgo", { n: Math.floor(s / 3600) });
}

export function WorkersStatus() {
  const { t } = useTranslation();
  const [workers, setWorkers] = useState<Worker[] | null>(null);

  const refresh = useCallback(async () => {
    try {
      const r = await api.get<WorkersResponse>("/api/dashboard/workers");
      setWorkers(r.workers);
    } catch { /* silent */ }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 15_000);
    return () => clearInterval(t);
  }, [refresh]);

  // Hide while we haven't fetched yet OR there are genuinely no workers.
  if (!workers || workers.length === 0) return null;

  // If everything is ok, render a single compact "Workers · all healthy"
  // line — no need for a full chip grid every render.
  const allOk = workers.every(w => w.status === "ok");

  return (
    <section className="mb-8">
      <h2 className="text-xs text-muted-foreground font-semibold mb-3 flex items-center gap-2">
        <Activity className="w-3 h-3" /> {t("settings.workers.title")}
        {allOk && (
          <span className="text-2xs normal-case tracking-normal text-emerald-500 font-normal">
            · {t("settings.workers.allHealthy", { count: workers.length })}
          </span>
        )}
      </h2>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2">
        {workers.map(w => {
          const { icon: Icon, label } = metaFor(w.name);
          const { Icon: SIcon, cls } = statusIcon(w.status);
          return (
            <div
              key={w.name}
              className={cn(
                "flex items-center gap-2.5 px-3 py-2 rounded-lg border bg-card text-xs",
                w.status === "error" ? "border-red-500/40 bg-red-500/[0.04]"
                : w.status === "warn" ? "border-amber-500/40 bg-amber-500/[0.04]"
                : "border-border",
              )}
              title={`${w.detail || "—"} · ${t("settings.workers.uptime", { age: formatAge(w.uptime_s) })}`}
            >
              <Icon className="w-3.5 h-3.5 text-muted-foreground shrink-0" />
              <div className="flex-1 min-w-0">
                <div className="font-medium truncate">{label}</div>
                <div className="text-2xs text-muted-foreground truncate">
                  {w.detail || (w.status === "starting" ? t("settings.workers.starting") : "—")}
                </div>
              </div>
              <div className="flex items-center gap-1 shrink-0">
                <SIcon className={cn("w-3.5 h-3.5", cls)} />
                {w.last_heartbeat_age_s != null && (
                  <span className="text-2xs text-muted-foreground tabular-nums">
                    {formatAge(w.last_heartbeat_age_s)}
                  </span>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}
