/**
 * StoragePicker — the "where do photos + documents live?" widget.
 * Used by both Settings → Storage AND the onboarding wizard.
 *
 * Shows current state, detected external volumes, and the
 * move / restore actions. Heavy ops show a spinner + disable
 * navigation; the parent handles a "this may take a while" warning
 * before triggering.
 */

import { useCallback, useEffect, useState } from "react";
import {
  HardDrive, ArrowRightLeft, Loader2, AlertTriangle, Check, RefreshCw,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";

interface SubtreeStatus {
  subtree: string;
  expected_path: string;
  is_symlink: boolean;
  target: string | null;
  healthy: boolean;
  bytes_used: number | null;
}

export interface StorageStatus {
  storage_root: string | null;
  all_healthy: boolean;
  subtrees: SubtreeStatus[];
}

interface Volume {
  name: string;
  mountpoint: string;
  size?: string;
  label?: string;
  hotplug: boolean;
  suggested_target: string;
}

function fmtBytes(n: number | null): string {
  if (!n) return "—";
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let i = -1, v = n;
  do { v /= 1024; i += 1; } while (v >= 1024 && i < units.length - 1);
  return `${v.toFixed(1)} ${units[i]}`;
}

export function StoragePicker({
  onChange,
  compact = false,
}: {
  onChange?: (status: StorageStatus) => void;
  /** Compact mode for the onboarding wizard — no header/footer chrome. */
  compact?: boolean;
}) {
  const [status, setStatus] = useState<StorageStatus | null>(null);
  const [volumes, setVolumes] = useState<Volume[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<null | "moving" | "restoring">(null);
  const [pickedVolume, setPickedVolume] = useState<string>("");
  const [customPath, setCustomPath] = useState<string>("");
  const [useCustom, setUseCustom] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [s, v] = await Promise.all([
        api.get<StorageStatus>("/api/storage"),
        api.get<Volume[]>("/api/storage/volumes").catch(() => []),
      ]);
      setStatus(s);
      setVolumes(v);
      onChange?.(s);
    } catch (e: any) {
      console.error("storage refresh failed:", e);
    } finally { setLoading(false); }
  }, [onChange]);

  useEffect(() => { refresh(); }, [refresh]);

  const targetPath = useCustom ? customPath.trim() : pickedVolume;
  const isOnExternal = !!status?.storage_root;

  async function moveToExternal() {
    if (!targetPath) return;
    if (!confirm(
      `Move documents + photos to ${targetPath}?\n\n` +
      `This may take a while for large libraries (a 50 GB photo library typically takes 5–15 min on USB-C). ` +
      `Yorik will stay responsive but uploads/photo imports are paused during the move.`
    )) return;
    setBusy("moving");
    try {
      await api.post("/api/storage/move", { target_root: targetPath });
      await refresh();
      alert(`✓ Moved. Documents + photos now live on ${targetPath}.`);
    } catch (e: any) {
      alert(`Move failed: ${e?.message || e}`);
    } finally { setBusy(null); }
  }

  async function restoreToInternal() {
    if (!confirm(
      "Move documents + photos BACK to the internal disk?\n\n" +
      "The copy on the external SSD stays put — you can delete it manually after verifying everything works internally."
    )) return;
    setBusy("restoring");
    try {
      await api.post("/api/storage/restore");
      await refresh();
      alert("✓ Restored to internal storage.");
    } catch (e: any) {
      alert(`Restore failed: ${e?.message || e}`);
    } finally { setBusy(null); }
  }

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground py-6 justify-center">
        <Loader2 className="w-4 h-4 animate-spin" /> Loading storage status…
      </div>
    );
  }

  return (
    <div className={cn("space-y-4", compact ? "" : "")}>
      {/* Current state */}
      {status && (
        <div className={cn(
          "rounded-lg border p-3 text-sm",
          status.all_healthy
            ? isOnExternal
              ? "border-emerald-500/30 bg-emerald-500/5"
              : "border-border bg-muted/40"
            : "border-red-500/30 bg-red-500/5",
        )}>
          <div className="flex items-start gap-2.5">
            {!status.all_healthy
              ? <AlertTriangle className="w-4 h-4 text-red-500 mt-0.5 shrink-0" />
              : <HardDrive className={cn("w-4 h-4 mt-0.5 shrink-0",
                  isOnExternal ? "text-emerald-500" : "text-muted-foreground")} />}
            <div className="flex-1 min-w-0">
              <div className="font-medium">
                {!status.all_healthy
                  ? "Storage problem detected"
                  : isOnExternal
                    ? "External storage"
                    : "Internal storage"}
              </div>
              <div className="text-xs text-muted-foreground mt-0.5 break-all">
                {!status.all_healthy
                  ? "One or more relocated subtrees can't be reached. Reconnect the SSD or restore to internal."
                  : isOnExternal
                    ? <>Documents + photos live at <code className="font-mono">{status.storage_root}</code></>
                    : "Everything lives under the project's data/ directory. Fine for small households; an external SSD is recommended once your photo library passes ~10 GB."}
              </div>
            </div>
          </div>

          {/* Per-subtree breakdown — only shown when external */}
          {(isOnExternal || !status.all_healthy) && (
            <div className="mt-3 pt-3 border-t border-current/15 space-y-1">
              {status.subtrees.map(s => (
                <div key={s.subtree} className="flex items-center gap-2 text-[11px]">
                  {s.healthy
                    ? <Check className="w-3 h-3 text-emerald-500" />
                    : <AlertTriangle className="w-3 h-3 text-red-500" />}
                  <code className="font-mono">{s.subtree}</code>
                  <span className="text-muted-foreground ml-auto">{fmtBytes(s.bytes_used)}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Move-to-external picker (when currently internal) */}
      {!isOnExternal && (
        <div className="space-y-3">
          <div className="text-xs uppercase tracking-wider text-muted-foreground font-semibold">
            Move to external storage
          </div>
          {volumes.length > 0 ? (
            <div className="space-y-1.5">
              {volumes.map(v => (
                <label
                  key={v.mountpoint}
                  className={cn(
                    "flex items-center gap-2.5 p-2.5 rounded-md border cursor-pointer transition",
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
          ) : (
            <div className="text-xs text-muted-foreground italic">
              No external volumes detected. Plug in an SSD or use a custom path below.
            </div>
          )}

          {/* Custom path escape hatch */}
          <label className={cn(
            "flex items-start gap-2.5 p-2.5 rounded-md border cursor-pointer transition",
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
                value={customPath}
                onChange={e => setCustomPath(e.target.value)}
                onFocus={() => setUseCustom(true)}
                placeholder="/mnt/yorik-storage"
                className="mt-1 w-full h-8 px-2 bg-background border border-border rounded text-xs font-mono focus:outline-none focus:ring-1 focus:ring-ring/40"
              />
              <div className="text-[10px] text-muted-foreground mt-1">
                Must already exist and be on a different filesystem than the project. Yorik creates a <code className="font-mono">yorik/</code> subdir under it.
              </div>
            </div>
          </label>

          <button
            type="button"
            onClick={moveToExternal}
            disabled={!targetPath || busy !== null}
            className={cn(
              "w-full h-10 rounded-md text-sm font-medium flex items-center justify-center gap-2 transition",
              targetPath
                ? "bg-primary text-primary-foreground hover:opacity-90"
                : "bg-muted text-muted-foreground cursor-not-allowed",
              busy && "opacity-60",
            )}
          >
            {busy === "moving"
              ? <Loader2 className="w-4 h-4 animate-spin" />
              : <ArrowRightLeft className="w-4 h-4" />}
            Move to external storage
          </button>
        </div>
      )}

      {/* Restore-to-internal when external */}
      {isOnExternal && (
        <button
          type="button"
          onClick={restoreToInternal}
          disabled={busy !== null}
          className={cn(
            "w-full h-10 rounded-md text-sm font-medium border border-border bg-card",
            "hover:bg-muted/50 flex items-center justify-center gap-2 transition",
            busy && "opacity-60",
          )}
        >
          {busy === "restoring"
            ? <Loader2 className="w-4 h-4 animate-spin" />
            : <ArrowRightLeft className="w-4 h-4" />}
          Move back to internal storage
        </button>
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
