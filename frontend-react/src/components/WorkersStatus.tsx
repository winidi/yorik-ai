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
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";

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
  { match: /^email_supervisor$/,  icon: Mail,          label: () => "Email supervisor" },
  { match: /^email_account_(\d+)$/, icon: Mail,         label: (n) => `Email · account ${n.split("_").pop()}` },
  { match: /^whatsapp_/,          icon: MessageSquare, label: () => "WhatsApp bridge" },
  { match: /^backup_/,            icon: Database,      label: () => "Backup scheduler" },
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
  if (s < 60)    return `${s}s ago`;
  if (s < 3600)  return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
}

export function WorkersStatus() {
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
      <h2 className="text-[11px] uppercase tracking-wider text-muted-foreground font-semibold mb-3 flex items-center gap-2">
        <Activity className="w-3 h-3" /> Background workers
        {allOk && (
          <span className="text-[10px] normal-case tracking-normal text-emerald-500 font-normal">
            · all {workers.length} healthy
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
              title={`${w.detail || "—"} · uptime ${formatAge(w.uptime_s)}`}
            >
              <Icon className="w-3.5 h-3.5 text-muted-foreground shrink-0" />
              <div className="flex-1 min-w-0">
                <div className="font-medium truncate">{label}</div>
                <div className="text-[10px] text-muted-foreground truncate">
                  {w.detail || (w.status === "starting" ? "starting…" : "—")}
                </div>
              </div>
              <div className="flex items-center gap-1 shrink-0">
                <SIcon className={cn("w-3.5 h-3.5", cls)} />
                {w.last_heartbeat_age_s != null && (
                  <span className="text-[10px] text-muted-foreground tabular-nums">
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
