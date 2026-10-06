/**
 * Settings → Developer → Diagnostics: what Yorik has sent (or drafted,
 * queued, declined), each report's exact payload, and the field list.
 * Admins see every report, members their own (backend/diagnostics/routes).
 */
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Loader2, RefreshCw } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

interface Report {
  id: string; created_at: string; kind: string; trigger: string | null; status: string; attempts: number;
  last_error: string | null; sent_at: string | null; bytes: number; payload: any; scrub_summary: any;
}

const STATUS_CLS: Record<string, string> = {
  draft: "text-muted-foreground", queued: "text-amber-600", sent: "text-emerald-600",
  declined: "text-muted-foreground", failed: "text-red-500",
};

export function DiagnosticsSection({ toast }: { toast: (text: string, kind?: "info" | "success" | "error") => void }) {
  const { t } = useTranslation();
  const [reports, setReports] = useState<Report[] | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setReports((await api.get<{ reports: Report[] }>("/api/diagnostics/reports?limit=100")).reports);
    } catch (e: any) {
      toast(e?.message || "Could not load", "error");
    } finally { setLoading(false); }
  }, [toast]);
  useEffect(() => { refresh(); }, [refresh]);

  const current = reports?.find(r => r.id === open) || null;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <div className="text-sm font-medium">{t("settings.diagnostics.reportsTitle", "What Yorik has sent")}</div>
          <p className="text-xs text-muted-foreground">
            {t("settings.diagnostics.reportsIntro", "Every report Yorik drafted, queued or sent, with the exact bytes. The same text is written to the log.")}
          </p>
        </div>
        <button onClick={refresh} disabled={loading} className="text-xs inline-flex items-center gap-1 px-2.5 h-8 rounded-md border border-border hover:bg-muted">
          <RefreshCw className={cn("w-3.5 h-3.5", loading && "animate-spin")} /> {t("common.refresh", "Refresh")}
        </button>
      </div>
      {reports === null && <Loader2 className="w-4 h-4 animate-spin text-muted-foreground" />}
      {reports && reports.length === 0 && (
        <div className="rounded-xl border border-border p-4 text-sm text-muted-foreground">
          {t("settings.diagnostics.none", "Nothing yet. Reports appear here when one is drafted from a thumbs-down or \"Report a problem\", or when daily counts are switched on.")}
        </div>
      )}
      {reports && reports.length > 0 && (
        <div className="rounded-xl border border-border overflow-hidden text-xs">
          <table className="w-full">
            <thead className="bg-muted/40 text-muted-foreground">
              <tr>
                <th className="text-left px-3 py-2 font-medium">{t("common.date", "Date")}</th>
                <th className="text-left px-3 py-2 font-medium">{t("settings.diagnostics.kind", "Kind")}</th>
                <th className="text-left px-3 py-2 font-medium">{t("settings.diagnostics.status", "Status")}</th>
                <th className="text-right px-3 py-2 font-medium">{t("settings.diagnostics.size", "Size")}</th>
              </tr>
            </thead>
            <tbody>
              {reports.map(r => (
                <tr key={r.id} onClick={() => setOpen(open === r.id ? null : r.id)}
                    className={cn("border-t border-border/60 cursor-pointer hover:bg-muted/30", open === r.id && "bg-muted/30")}>
                  <td className="px-3 py-2 whitespace-nowrap">{r.created_at.slice(0, 16).replace("T", " ")}</td>
                  <td className="px-3 py-2">{r.kind === "usage_daily" ? t("settings.diagnostics.daily", "daily counts") : `${t("settings.diagnostics.error", "error")} · ${r.trigger || ""}`}</td>
                  <td className={cn("px-3 py-2", STATUS_CLS[r.status] || "")}>{r.status}{r.attempts > 0 && r.status !== "sent" ? ` (${r.attempts})` : ""}</td>
                  <td className="px-3 py-2 text-right tabular-nums">{r.bytes} B</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {current && (
        <div className="rounded-xl border border-border p-4 space-y-2">
          <div className="text-xs text-muted-foreground">
            {current.scrub_summary && (
              <span>{t("settings.diagnostics.replaced", "Replaced")}: {Object.entries(current.scrub_summary.replaced || {}).map(([k, v]) => `${v} ${k}`).join(", ") || "—"}
                {current.scrub_summary.dropped_fields ? ` · ${current.scrub_summary.dropped_fields} ${t("settings.diagnostics.dropped", "fields dropped")}` : ""}
                {current.scrub_summary.free_text_dropped ? ` · ${t("settings.diagnostics.freeTextDropped", "free text left out")}` : ""}
              </span>
            )}
            {current.last_error && <span className="block text-red-500">{current.last_error}</span>}
          </div>
          <textarea readOnly value={JSON.stringify(current.payload, null, 2)}
                    className="w-full h-72 font-mono text-2xs p-3 rounded-md bg-muted/30 border border-border" />
        </div>
      )}
    </div>
  );
}
