/**
 * Settings → System → Updates: is a new version ready, and one button
 * to install it. Backend: /api/system/update (update_routes.py), which
 * starts yorik-update.service (classic install) or asks the updater
 * container (Docker install, deploy/updater.sh); Yorik restarts itself
 * afterwards.
 */
import { useEffect, useState } from "react";
import { Loader2, RefreshCw } from "lucide-react";
import { api } from "@/lib/api";

interface Status {
  available: boolean; reason?: string; current?: string; behind?: number; changes?: string[];
  local_changes?: boolean; can_update?: boolean; running?: boolean;
  runtime?: string; last_failed?: string | null;
}

export function UpdateCard() {
  const [s, setS] = useState<Status | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const load = (check = false) => api.get<Status>(`/api/system/update${check ? "?check=1" : ""}`).then(setS).catch(() => setS(null));
  useEffect(() => { void load(); }, []);

  async function update() {
    setBusy(true); setNote(null);
    try {
      const r = await api.post<{ message: string }>("/api/system/update");
      setNote(r.message);
      // Wait for the restart: poll until Yorik answers again (Docker: until
      // the updater says it's done or failed).
      const t0 = Date.now();
      const tick = setInterval(async () => {
        try {
          if (s?.runtime === "docker") {
            const st = await api.get<Status>("/api/system/update");
            if (st.last_failed) { clearInterval(tick); setS(st); setBusy(false); setNote(null); return; }
            if (!st.running && Date.now() - t0 > 20000) { clearInterval(tick); window.location.reload(); }
          } else {
            const r2 = await fetch("/api/health", { cache: "no-store" });
            if (r2.ok && Date.now() - t0 > 20000) { clearInterval(tick); window.location.reload(); }
          }
        } catch { /* restarting */ }
        if (Date.now() - t0 > 15 * 60_000) clearInterval(tick);
      }, 5000);
    } catch (e: any) { setNote(e?.message || "The update couldn't start."); setBusy(false); }
  }

  if (!s || !s.available) return null;
  return (
    <section className="space-y-2">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-muted-foreground">Updates</h2>
        <button onClick={() => void load(true)} className="text-muted-foreground hover:text-foreground" title="Check now"><RefreshCw className="w-3.5 h-3.5" /></button>
      </div>
      <div className="bg-card border border-border rounded-xl p-4 space-y-3">
        {s.behind ? (
          <>
            <div className="text-sm font-medium">A new version is ready ({s.behind} change{s.behind === 1 ? "" : "s"})</div>
            {s.changes && s.changes.length > 0 && (
              <ul className="text-xs text-muted-foreground list-disc pl-4 space-y-0.5">{s.changes.slice(0, 6).map((c, i) => <li key={i}>{c}</li>)}</ul>
            )}
            {s.local_changes ? (
              <p className="text-xs text-muted-foreground">This copy has local changes, so it's updated with <code>./scripts/yorik upgrade</code>, not from here.</p>
            ) : s.can_update ? (
              <button onClick={update} disabled={busy || s.running}
                      className="px-4 py-2 rounded-lg bg-primary text-primary-foreground text-sm font-medium disabled:opacity-60 inline-flex items-center gap-2">
                {(busy || s.running) && <Loader2 className="w-4 h-4 animate-spin" />} {s.running || busy ? "Updating…" : "Update now"}
              </button>
            ) : (
              <p className="text-xs text-muted-foreground">Updating from here needs a one-time setup: run <code>bash install.sh</code> again. Or use <code>./scripts/yorik upgrade</code>.</p>
            )}
            {s.last_failed && !busy && (
              <p className="text-sm text-destructive">The last update didn't finish: {s.last_failed}. Your data is safe. Try again later.</p>
            )}
            <p className="text-xs text-muted-foreground">
              {s.runtime === "docker"
                ? "Yorik downloads the new version and restarts in a few minutes. Your data stays."
                : "Yorik makes sure the last backup opens, updates, and restarts in a minute or two."}
            </p>
          </>
        ) : (
          <div className="text-sm">Yorik is up to date <span className="text-muted-foreground">({s.current})</span></div>
        )}
        {note && <p className="text-sm text-muted-foreground">{note}</p>}
      </div>
    </section>
  );
}
