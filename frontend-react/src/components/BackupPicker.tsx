/**
 * BackupPicker — the "set up backups" widget.
 *
 * Shared between Settings → Backup and the onboarding wizard. Drives:
 *   - Passphrase (set once; never returned by the API)
 *   - Target path (text input OR detected-volumes dropdown)
 *   - Schedule (Off, 03:00, custom)
 *   - Heavy includes (photos / paperless / whatsapp)
 *
 * The full Settings tab adds a "Run now" + recent-runs list on top.
 * Onboarding mode (compact=true) hides the runs list and shrinks
 * helper text.
 *
 * Same-filesystem target is ALLOWED (user might rsync the dir to a
 * cloud later) but surfaces a yellow warning so it's a conscious
 * choice, not a footgun.
 */

import { useCallback, useEffect, useState } from "react";
import {
  Loader2, Shield, AlertTriangle, Check, Clock, HardDrive,
  Play, KeyRound, RefreshCw,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";

interface BackupConfig {
  target_path: string;
  schedule: string;          // "" = manual, else "HH:MM"
  include_photos: boolean;
  include_paperless: boolean;
  include_whatsapp: boolean;
  retain_count: number;
  passphrase_set: boolean;
}

interface BackupStatus {
  config: BackupConfig;
  target: { available: boolean; reason?: string; free_bytes?: number; on_same_filesystem?: boolean };
  history: Array<{
    id: number; started_at: string; finished_at?: string;
    ok: boolean; size_bytes?: number; error?: string;
  }>;
  snapshots: Array<{ name: string; size_bytes: number; mtime: string }>;
}

interface Volume {
  name: string;
  mountpoint: string;
  size?: string;
  label?: string;
  hotplug: boolean;
  suggested_target: string;
}

function fmtBytes(n?: number): string {
  if (!n) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

function fmtDateRel(iso?: string): string {
  if (!iso) return "—";
  const d = new Date(iso);
  const ms = Date.now() - d.getTime();
  if (ms < 60_000)   return "just now";
  if (ms < 3600_000) return `${Math.round(ms / 60_000)}m ago`;
  if (ms < 86_400_000) return `${Math.round(ms / 3600_000)}h ago`;
  return d.toLocaleDateString();
}

export function BackupPicker({
  compact = false,
}: {
  compact?: boolean;
}) {
  const [status, setStatus] = useState<BackupStatus | null>(null);
  const [volumes, setVolumes] = useState<Volume[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<null | "saving" | "running">(null);

  // Local form state — initialised from status on load.
  const [targetPath, setTargetPath] = useState("");
  const [pickedVolume, setPickedVolume] = useState("");
  const [useCustom, setUseCustom] = useState(true);
  const [scheduleEnabled, setScheduleEnabled] = useState(true);
  const [scheduleTime, setScheduleTime] = useState("03:00");
  const [passphrase, setPassphrase] = useState("");
  const [includePhotos, setIncludePhotos] = useState(false);
  const [includePaperless, setIncludePaperless] = useState(false);
  const [includeWhatsapp, setIncludeWhatsapp] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [s, v] = await Promise.all([
        api.get<BackupStatus>("/api/backup/status"),
        api.get<Volume[]>("/api/storage/volumes").catch(() => []),
      ]);
      setStatus(s);
      setVolumes(v);
      setTargetPath(s.config.target_path);
      setScheduleEnabled(!!s.config.schedule);
      setScheduleTime(s.config.schedule || "03:00");
      setIncludePhotos(s.config.include_photos);
      setIncludePaperless(s.config.include_paperless);
      setIncludeWhatsapp(s.config.include_whatsapp);
      // Default to custom-path when the existing target doesn't match any detected volume.
      const match = v.find(vol => vol.suggested_target === s.config.target_path);
      if (match) { setPickedVolume(match.suggested_target); setUseCustom(false); }
      else       { setUseCustom(true); }
    } catch (e: any) {
      console.error("backup status load failed:", e);
    } finally { setLoading(false); }
  }, []);
  useEffect(() => { refresh(); }, [refresh]);

  const effectiveTarget = useCustom ? targetPath.trim() : pickedVolume;
  const sameFilesystem = !!status?.target?.on_same_filesystem;

  async function save() {
    if (passphrase && passphrase.length < 8) {
      alert("Passphrase must be at least 8 characters.");
      return;
    }
    if (!effectiveTarget) {
      alert("Pick a backup target.");
      return;
    }
    setBusy("saving");
    try {
      await api.patch("/api/backup/config", {
        target_path:       effectiveTarget,
        schedule:          scheduleEnabled ? scheduleTime : "",
        include_photos:    includePhotos,
        include_paperless: includePaperless,
        include_whatsapp:  includeWhatsapp,
        ...(passphrase ? { passphrase } : {}),
      });
      setPassphrase("");
      await refresh();
    } catch (e: any) {
      alert(`Save failed: ${e?.message || e}`);
    } finally { setBusy(null); }
  }

  async function runNow() {
    if (!status?.config.passphrase_set && !passphrase) {
      alert("Set a passphrase first.");
      return;
    }
    setBusy("running");
    try {
      const r = await api.post<{ ok: boolean; size_bytes?: number; error?: string }>("/api/backup/run", {});
      if (r.ok) alert(`✓ Backup done — ${fmtBytes(r.size_bytes)}`);
      else      alert(`Backup failed: ${r.error}`);
      await refresh();
    } catch (e: any) {
      alert(`Run failed: ${e?.message || e}`);
    } finally { setBusy(null); }
  }

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground py-6 justify-center">
        <Loader2 className="w-4 h-4 animate-spin" /> Loading backup status…
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* Status banner */}
      {status && (
        <div className={cn(
          "rounded-lg border p-3 text-sm flex items-start gap-2.5",
          !status.config.passphrase_set
            ? "border-red-500/30 bg-red-500/5"
            : !status.config.schedule
              ? "border-amber-500/30 bg-amber-500/5"
              : status.target.available
                ? "border-emerald-500/30 bg-emerald-500/5"
                : "border-red-500/30 bg-red-500/5",
        )}>
          {!status.config.passphrase_set
            ? <AlertTriangle className="w-4 h-4 text-red-500 mt-0.5 shrink-0" />
            : !status.config.schedule
              ? <Clock className="w-4 h-4 text-amber-500 mt-0.5 shrink-0" />
              : status.target.available
                ? <Shield className="w-4 h-4 text-emerald-500 mt-0.5 shrink-0" />
                : <AlertTriangle className="w-4 h-4 text-red-500 mt-0.5 shrink-0" />}
          <div className="flex-1 min-w-0">
            <div className="font-medium">
              {!status.config.passphrase_set
                ? "No passphrase set — backups can't run."
                : !status.config.schedule
                  ? "Backups are manual-only (no schedule)."
                  : status.target.available
                    ? `Auto-backup at ${status.config.schedule} daily.`
                    : "Backup target unreachable."}
            </div>
            {status.history.length > 0 && (
              <div className="text-[11px] text-muted-foreground mt-0.5">
                Last run: {fmtDateRel(status.history[0].started_at)}
                {status.history[0].ok ? " ✓" : " — failed"}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Passphrase */}
      <div>
        <div className="text-xs uppercase tracking-wider text-muted-foreground font-semibold mb-1.5 flex items-center gap-1">
          <KeyRound className="w-3 h-3" />
          {status?.config.passphrase_set ? "Change passphrase (optional)" : "Set a passphrase"}
        </div>
        <input
          type="password"
          value={passphrase}
          onChange={e => setPassphrase(e.target.value)}
          placeholder={status?.config.passphrase_set ? "leave blank to keep current" : "at least 8 characters"}
          className="w-full h-9 px-3 bg-background border border-border rounded-md text-sm focus:outline-none focus:ring-1 focus:ring-ring/40"
        />
        {!compact && (
          <p className="text-[10px] text-muted-foreground mt-1">
            Snapshots are encrypted with this passphrase (age + scrypt). <strong>Lose it and the snapshots are unrecoverable</strong> —
            write it down in a password manager NOW.
          </p>
        )}
      </div>

      {/* Target */}
      <div>
        <div className="text-xs uppercase tracking-wider text-muted-foreground font-semibold mb-1.5">
          Backup target
        </div>
        {volumes.length > 0 && (
          <div className="space-y-1.5 mb-2">
            {volumes.map(v => (
              <label
                key={v.mountpoint}
                className={cn(
                  "flex items-center gap-2.5 p-2 rounded-md border cursor-pointer transition",
                  pickedVolume === v.suggested_target && !useCustom
                    ? "border-primary/40 bg-primary/5"
                    : "border-border hover:bg-muted/40",
                )}
              >
                <input
                  type="radio"
                  checked={pickedVolume === v.suggested_target && !useCustom}
                  onChange={() => { setPickedVolume(v.suggested_target); setUseCustom(false); }}
                  className="accent-primary"
                />
                <HardDrive className="w-3.5 h-3.5 text-muted-foreground" />
                <div className="flex-1 min-w-0">
                  <div className="text-sm font-medium">
                    {v.label || v.name}
                    {v.size && <span className="ml-2 text-xs text-muted-foreground">{v.size}</span>}
                    {v.hotplug && <span className="ml-2 text-[9px] px-1 rounded bg-sky-500/15 text-sky-500 uppercase tracking-wider">external</span>}
                  </div>
                  <div className="text-[10px] text-muted-foreground font-mono break-all">{v.suggested_target}</div>
                </div>
              </label>
            ))}
          </div>
        )}
        <label className={cn(
          "flex items-start gap-2.5 p-2 rounded-md border cursor-pointer transition",
          useCustom ? "border-primary/40 bg-primary/5" : "border-border hover:bg-muted/40",
        )}>
          <input
            type="radio"
            checked={useCustom}
            onChange={() => setUseCustom(true)}
            className="accent-primary mt-1"
          />
          <div className="flex-1 min-w-0">
            <div className="text-sm font-medium">Custom path</div>
            <input
              value={targetPath}
              onChange={e => setTargetPath(e.target.value)}
              onFocus={() => setUseCustom(true)}
              placeholder="/mnt/yorik-backups"
              className="mt-1 w-full h-8 px-2 bg-background border border-border rounded text-xs font-mono focus:outline-none focus:ring-1 focus:ring-ring/40"
            />
            <p className="text-[10px] text-muted-foreground mt-1">
              Local dir, NAS mount, or anywhere else readable. You can also point this at a
              folder you rsync to cloud — backups are age-encrypted so the cloud sees only ciphertext.
            </p>
          </div>
        </label>

        {/* Same-filesystem warning — allowed, just flagged */}
        {sameFilesystem && (
          <div className="mt-2 rounded-md border border-amber-500/40 bg-amber-500/5 p-2.5 text-[11px] text-amber-700 dark:text-amber-400 flex items-start gap-2">
            <AlertTriangle className="w-3.5 h-3.5 mt-0.5 shrink-0" />
            <div>
              This target is on the SAME disk as your live data. Backups will protect you from
              accidental deletion + corruption, but NOT from disk failure or theft. Consider syncing
              this folder to cloud storage too.
            </div>
          </div>
        )}
      </div>

      {/* Schedule */}
      <div>
        <div className="text-xs uppercase tracking-wider text-muted-foreground font-semibold mb-1.5 flex items-center gap-1">
          <Clock className="w-3 h-3" /> Schedule
        </div>
        <label className="flex items-center gap-2 text-sm cursor-pointer">
          <input
            type="checkbox"
            checked={scheduleEnabled}
            onChange={e => setScheduleEnabled(e.target.checked)}
            className="accent-primary"
          />
          Run daily at
          <input
            type="time"
            value={scheduleTime}
            onChange={e => setScheduleTime(e.target.value)}
            disabled={!scheduleEnabled}
            className="h-8 px-2 bg-background border border-border rounded text-sm focus:outline-none disabled:opacity-50"
          />
          <span className="text-xs text-muted-foreground">(local time)</span>
        </label>
      </div>

      {/* Includes */}
      <div>
        <div className="text-xs uppercase tracking-wider text-muted-foreground font-semibold mb-1.5">
          What to include
        </div>
        <div className="space-y-1">
          <div className="text-[11px] text-muted-foreground mb-2">
            <strong>Always:</strong> calendar, contacts, tasks, bills, chat history, settings, encryption key.
            The toggles below add HEAVY data on top — expect snapshots in the GB range when on.
          </div>
          <IncludeToggle
            label="📷 Photos (Immich library + Postgres dump)"
            desc="JPEGs + albums + faces + AI search metadata. Big — usually most of the backup."
            checked={includePhotos}
            onChange={setIncludePhotos}
          />
          <IncludeToggle
            label="📄 Paperless (PDFs + scans)"
            desc="Filed documents + OCR. Sizeable; grows with your filing rate."
            checked={includePaperless}
            onChange={setIncludePaperless}
          />
          <IncludeToggle
            label="💬 WhatsApp session (pairing secrets)"
            desc="Without this, a restored Yorik forgets which phone is paired — you'd re-scan the QR code."
            checked={includeWhatsapp}
            onChange={setIncludeWhatsapp}
          />
        </div>
      </div>

      {/* Actions */}
      <div className="flex gap-2 pt-2 border-t border-border">
        <button
          onClick={save}
          disabled={busy !== null}
          className="flex-1 h-10 rounded-md bg-primary text-primary-foreground text-sm font-medium hover:opacity-90 disabled:opacity-50 flex items-center justify-center gap-2"
        >
          {busy === "saving" ? <Loader2 className="w-4 h-4 animate-spin" /> : <Check className="w-4 h-4" />}
          Save
        </button>
        {!compact && (
          <button
            onClick={runNow}
            disabled={busy !== null || !status?.config.passphrase_set}
            className="h-10 px-4 rounded-md border border-border bg-card text-sm font-medium hover:bg-muted disabled:opacity-50 flex items-center gap-2"
          >
            {busy === "running" ? <Loader2 className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />}
            Run now
          </button>
        )}
      </div>

      {/* Recent runs — only in full mode */}
      {!compact && status && status.history.length > 0 && (
        <div>
          <div className="text-xs uppercase tracking-wider text-muted-foreground font-semibold mb-1.5 mt-2">
            Recent runs
          </div>
          <div className="space-y-1">
            {status.history.slice(0, 5).map(h => (
              <div key={h.id} className="flex items-center gap-2 text-[11px] py-1 px-2 rounded bg-muted/30">
                {h.ok ? <Check className="w-3 h-3 text-emerald-500" /> : <AlertTriangle className="w-3 h-3 text-red-500" />}
                <span>{new Date(h.started_at).toLocaleString()}</span>
                {h.size_bytes && <span className="text-muted-foreground">· {fmtBytes(h.size_bytes)}</span>}
                {h.error && <span className="text-red-500 truncate">· {h.error}</span>}
              </div>
            ))}
          </div>
        </div>
      )}

      {!compact && (
        <button
          type="button"
          onClick={refresh}
          className="text-[11px] text-muted-foreground hover:text-foreground inline-flex items-center gap-1"
        >
          <RefreshCw className="w-3 h-3" /> Refresh
        </button>
      )}
    </div>
  );
}

function IncludeToggle({
  label, desc, checked, onChange,
}: {
  label: string; desc: string; checked: boolean; onChange: (v: boolean) => void;
}) {
  return (
    <label className={cn(
      "flex items-start gap-2.5 p-2 rounded-md border cursor-pointer transition",
      checked ? "border-primary/30 bg-primary/5" : "border-border hover:bg-muted/40",
    )}>
      <input
        type="checkbox"
        checked={checked}
        onChange={e => onChange(e.target.checked)}
        className="accent-primary mt-1"
      />
      <div className="flex-1 min-w-0">
        <div className="text-sm font-medium">{label}</div>
        <div className="text-[10px] text-muted-foreground">{desc}</div>
      </div>
    </label>
  );
}
