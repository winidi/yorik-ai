/**
 * VcardImportModal — shared between the Contacts page (Import .vcf
 * button) and the Chat composer (drag-and-drop a .vcf into the chat).
 *
 * Two-step flow:
 *   1. file pick / drop → POST /api/contacts/import/preview → plan
 *   2. user picks Pending vs Active → POST /api/contacts/import/apply
 *
 * Plan is intentionally stateless — we send it back in full on apply,
 * no server-side cache. See backend/contacts_import.py for the schema.
 */

import { useEffect, useState, type ReactElement } from "react";
import {
  Upload, X, Check, Loader2, AlertTriangle, UserPlus, GitMerge,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { api, ApiError } from "@/lib/api";


type VcardImportPlanEntry = {
  outcome: "new" | "merge" | "name_conflict";
  card: { display_name: string; kind?: string; channels?: { kind: string; value: string }[] };
  existing_id?: number | null;
  existing_name?: string | null;
  matched_via?: string | null;
  new_channels?: { kind: string; value: string }[];
  new_addresses?: any[];
};
export type VcardImportPlan = {
  entries: VcardImportPlanEntry[];
  summary: { total: number; new: number; merge: number; name_conflict: number };
};
type VcardImportResult = {
  created: number; merged: number; skipped: number;
  errors: { display_name?: string; error: string }[];
};


export function VcardImportModal({ onClose, onApplied, initialFile }: {
  onClose: () => void;
  onApplied: () => void;
  // If the caller already has a File (e.g. from a drop event), seed
  // the modal with it so the user doesn't have to pick again.
  initialFile?: File | null;
}) {
  const [plan, setPlan] = useState<VcardImportPlan | null>(null);
  const [parsing, setParsing] = useState(false);
  const [applying, setApplying] = useState(false);
  const [result, setResult] = useState<VcardImportResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filename, setFilename] = useState<string | null>(initialFile?.name || null);
  const [dragOver, setDragOver] = useState(false);

  async function handleFile(file: File) {
    setError(null);
    setFilename(file.name);
    setParsing(true);
    setPlan(null);
    try {
      const text = await file.text();
      const p = await api.post<VcardImportPlan>("/api/contacts/import/preview", { text });
      setPlan(p);
    } catch (e: any) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setParsing(false);
    }
  }

  // Auto-parse if a file was handed in.
  useEffect(() => {
    if (initialFile) void handleFile(initialFile);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function apply(target_status: "pending" | "active") {
    if (!plan) return;
    setApplying(true);
    setError(null);
    try {
      const r = await api.post<VcardImportResult>("/api/contacts/import/apply", {
        plan, target_status,
      });
      setResult(r);
    } catch (e: any) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setApplying(false);
    }
  }

  useEffect(() => {
    function esc(e: KeyboardEvent) { if (e.key === "Escape" && !applying) onClose(); }
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [applying, onClose]);

  return (
    <div
      className="fixed inset-0 z-[1000] bg-black/60 backdrop-blur-sm flex items-center justify-center p-6"
      onClick={() => { if (!applying) onClose(); }}
    >
      <div
        className="bg-card border border-border rounded-2xl shadow-2xl w-full max-w-2xl max-h-[85vh] flex flex-col overflow-hidden"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-5 py-3 border-b border-border">
          <div className="flex items-center gap-2">
            <Upload className="w-4 h-4 text-amber-500" />
            <span className="text-sm font-semibold">Import contacts from .vcf</span>
          </div>
          <button
            onClick={onClose}
            disabled={applying}
            className="p-1 rounded hover:bg-muted text-muted-foreground hover:text-foreground transition disabled:opacity-50"
            title="Close"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-5">
          {!plan && !result && (
            <div
              onDragOver={e => { e.preventDefault(); setDragOver(true); }}
              onDragLeave={() => setDragOver(false)}
              onDrop={e => {
                e.preventDefault();
                setDragOver(false);
                const f = e.dataTransfer?.files?.[0];
                if (f) void handleFile(f);
              }}
              className={cn(
                "border-2 border-dashed rounded-xl p-10 text-center transition",
                dragOver ? "border-amber-500 bg-amber-500/5" : "border-border bg-muted/20",
              )}
            >
              <Upload className="w-8 h-8 mx-auto mb-3 text-muted-foreground" />
              <div className="text-sm font-medium mb-1">
                Drop a .vcf file here, or click to pick one
              </div>
              <div className="text-xs text-muted-foreground mb-4">
                Works with exports from iCloud, Google Contacts, Outlook, Thunderbird, etc.
              </div>
              <label className="inline-block">
                <input
                  type="file"
                  accept=".vcf,text/vcard,text/x-vcard"
                  className="hidden"
                  onChange={e => {
                    const f = e.target.files?.[0];
                    if (f) void handleFile(f);
                  }}
                />
                <span className="inline-flex items-center gap-1.5 text-xs h-8 px-3 rounded-md bg-primary text-primary-foreground hover:opacity-90 cursor-pointer">
                  {parsing ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Upload className="w-3.5 h-3.5" />}
                  {parsing ? "Parsing…" : "Choose file"}
                </span>
              </label>
              {filename && (
                <div className="text-[11px] text-muted-foreground mt-3">
                  Selected: {filename}
                </div>
              )}
            </div>
          )}

          {plan && !result && (
            <div className="space-y-4">
              <div className="grid grid-cols-3 gap-2 text-center">
                <SummaryTile icon={<UserPlus className="w-4 h-4 text-emerald-500" />} label="New" value={plan.summary.new} />
                <SummaryTile icon={<GitMerge className="w-4 h-4 text-sky-500" />} label="Merge" value={plan.summary.merge} />
                <SummaryTile icon={<AlertTriangle className="w-4 h-4 text-amber-500" />} label="Name conflict" value={plan.summary.name_conflict} />
              </div>

              <div className="max-h-[40vh] overflow-y-auto rounded-lg border border-border divide-y divide-border">
                {plan.entries.map((e, i) => <PlanEntryRow key={i} entry={e} />)}
              </div>

              {plan.summary.name_conflict > 0 && (
                <div className="flex items-start gap-2 text-[11px] text-muted-foreground bg-amber-500/[0.06] border border-amber-500/20 rounded-md p-2">
                  <AlertTriangle className="w-3.5 h-3.5 text-amber-500 shrink-0 mt-0.5" />
                  <span>
                    Name conflicts are skipped — the existing contact has a
                    different display name, so the channels won't be silently
                    merged. Edit the matching contact manually if you want to
                    add the new channels.
                  </span>
                </div>
              )}

              <div className="flex items-center justify-between gap-2 pt-2">
                <button
                  onClick={() => { setPlan(null); setFilename(null); }}
                  className="text-xs h-8 px-3 rounded-md text-muted-foreground hover:text-foreground"
                >
                  Pick a different file
                </button>
                <div className="flex items-center gap-2">
                  <button
                    onClick={() => apply("pending")}
                    disabled={applying || plan.summary.total === 0}
                    className="text-xs h-8 px-3 rounded-md bg-card border border-border hover:bg-muted disabled:opacity-50 flex items-center gap-1.5"
                    title="Park in the Pending tab for review"
                  >
                    {applying ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : null}
                    Import to Pending
                  </button>
                  <button
                    onClick={() => apply("active")}
                    disabled={applying || plan.summary.total === 0}
                    className="text-xs h-8 px-3 rounded-md bg-primary text-primary-foreground hover:opacity-90 disabled:opacity-50 flex items-center gap-1.5"
                    title="Add directly to Active contacts"
                  >
                    {applying ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : null}
                    Import to Active
                  </button>
                </div>
              </div>
            </div>
          )}

          {result && (
            <div className="space-y-4 text-center py-4">
              <div className="w-12 h-12 rounded-full bg-emerald-500/15 mx-auto flex items-center justify-center">
                <Check className="w-6 h-6 text-emerald-500" />
              </div>
              <div className="text-sm font-medium">Import complete</div>
              <div className="text-xs text-muted-foreground">
                <span className="text-emerald-500 font-medium">{result.created}</span> new
                {" · "}
                <span className="text-sky-500 font-medium">{result.merged}</span> merged
                {result.skipped > 0 && (
                  <>{" · "}<span className="text-amber-500 font-medium">{result.skipped}</span> skipped</>
                )}
                {result.errors.length > 0 && (
                  <>{" · "}<span className="text-rose-500 font-medium">{result.errors.length}</span> failed</>
                )}
              </div>
              {result.errors.length > 0 && (
                <ul className="text-[11px] text-rose-500 text-left max-h-32 overflow-y-auto space-y-0.5 px-3">
                  {result.errors.slice(0, 5).map((e, i) => (
                    <li key={i}>· {e.display_name || "(unnamed)"}: {e.error}</li>
                  ))}
                  {result.errors.length > 5 && <li>… and {result.errors.length - 5} more</li>}
                </ul>
              )}
              <button
                onClick={onApplied}
                className="text-xs h-8 px-4 rounded-md bg-primary text-primary-foreground hover:opacity-90"
              >
                Done
              </button>
            </div>
          )}

          {error && (
            <div className="mt-4 text-xs text-rose-500 bg-rose-500/[0.08] border border-rose-500/20 rounded-md p-2">
              {error}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}


function SummaryTile({ icon, label, value }: {
  icon: ReactElement; label: string; value: number;
}) {
  return (
    <div className="bg-muted/40 rounded-lg p-3 flex flex-col items-center gap-1">
      {icon}
      <div className="text-lg font-semibold tabular-nums">{value}</div>
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">{label}</div>
    </div>
  );
}


function PlanEntryRow({ entry }: { entry: VcardImportPlanEntry }) {
  const { card, outcome, existing_name, matched_via, new_channels } = entry;
  const tone =
    outcome === "new" ? "text-emerald-500" :
    outcome === "merge" ? "text-sky-500" : "text-amber-500";
  const Icon =
    outcome === "new" ? UserPlus :
    outcome === "merge" ? GitMerge : AlertTriangle;
  return (
    <div className="px-3 py-2 text-xs flex items-start gap-2">
      <Icon className={cn("w-3.5 h-3.5 shrink-0 mt-0.5", tone)} />
      <div className="flex-1 min-w-0">
        <div className="font-medium truncate">{card.display_name}</div>
        {outcome === "new" && card.channels && card.channels.length > 0 && (
          <div className="text-muted-foreground text-[11px] truncate">
            {card.channels.map(c => c.value).slice(0, 3).join(" · ")}
            {card.channels.length > 3 && ` +${card.channels.length - 3}`}
          </div>
        )}
        {outcome === "merge" && (
          <div className="text-muted-foreground text-[11px] truncate">
            Matches <span className="font-medium">{existing_name}</span>
            {matched_via && <> via <code className="text-[10px] bg-muted px-1 rounded">{matched_via}</code></>}
            {new_channels && new_channels.length > 0 && (
              <> · adds {new_channels.length} channel{new_channels.length === 1 ? "" : "s"}</>
            )}
          </div>
        )}
        {outcome === "name_conflict" && (
          <div className="text-muted-foreground text-[11px] truncate">
            Conflicts with existing <span className="font-medium">{existing_name}</span>
            {matched_via && <> on <code className="text-[10px] bg-muted px-1 rounded">{matched_via}</code></>} — skipped
          </div>
        )}
      </div>
    </div>
  );
}
