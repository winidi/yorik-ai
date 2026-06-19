/**
 * PendingActionPanel — inline three-button confirm widget.
 *
 * Renders below an assistant message (chat) or inside the voice popover.
 * Replaces the floating ConfirmationModal — the beta UX is "show me the
 * choice right next to what Yorik said about it", not a popup.
 *
 * Three buttons, layout per user spec:
 *   [ Just testing ]  [ Cancel ]  [ Looks good ]
 *      (left, amber)    (middle)    (right, violet)
 *
 * Semantics:
 *   - Looks good → /api/pending/{id}/confirm  → keep the row, log success
 *   - Cancel     → /api/pending/{id}/cancel   → rollback the row, log failure
 *   - Just testing → /api/pending/{id}/test    → rollback, log as test (excluded from success rate)
 *
 * After resolution: the panel collapses to a one-line confirmation
 * stamp ("✓ Confirmed" / "✗ Cancelled" / "🧪 Tested — reverted") and any
 * returned ui_actions (refresh calendar etc.) are dispatched to window.
 */

import { useCallback, useState } from "react";
import { CheckCircle2, FlaskConical, X, Loader2, AlertCircle } from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";
import { emitUiAction } from "@/lib/uiActions";

interface PendingAction {
  pending_id: string;
  skill: string;
  preview: any;
  llm_model?: string;
}

type Resolution = "confirmed" | "cancelled" | "test";

interface Props {
  action: PendingAction;
  /** Optional callback once the user resolves it (parent can update its own state). */
  onResolved?: (kind: Resolution) => void;
  /** Compact mode for tight popovers (voice). */
  compact?: boolean;
}

export function PendingActionPanel({ action, onResolved, compact }: Props) {
  const [busy, setBusy] = useState<Resolution | null>(null);
  const [resolved, setResolved] = useState<Resolution | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const resolve = useCallback(async (kind: Resolution) => {
    setBusy(kind);
    setErr(null);
    try {
      // Map UI-level kind to backend route. Backend routes are
      // /confirm, /cancel, /test — NOT the past-tense names we use
      // internally for the resolved-stamp.
      const route = kind === "confirmed" ? "confirm"
                  : kind === "cancelled" ? "cancel"
                  : "test";
      const r = await api.post<{
        ok: boolean;
        ui_actions?: any[];
      }>(`/api/pending/${encodeURIComponent(action.pending_id)}/${route}`, {});
      // Replay downstream UI actions (calendar refresh after rollback, etc.)
      for (const a of r.ui_actions || []) {
        emitUiAction(a);
      }
      setResolved(kind);
      onResolved?.(kind);
    } catch (e: any) {
      setErr(e?.message || "Couldn't resolve. Try again.");
    } finally {
      setBusy(null);
    }
  }, [action.pending_id, onResolved]);

  // Resolution stamp (collapsed state after a click).
  if (resolved) {
    return <ResolvedStamp kind={resolved} compact={compact} />;
  }

  return (
    <div className={cn(
      "mt-2 border border-border rounded-xl bg-card/80 overflow-hidden",
      compact ? "" : "max-w-md",
    )}>
      <div className={cn("px-3 pt-2.5 pb-2", compact ? "pb-1.5" : "")}>
        <div className="flex items-center gap-1.5 mb-1.5">
          <AlertCircle className="w-3.5 h-3.5 text-violet-500" />
          <span className="text-xs font-semibold">Does this look right?</span>
          <span className="text-[9px] text-muted-foreground font-mono ml-auto">
            {action.skill}{action.llm_model && ` · ${action.llm_model}`}
          </span>
        </div>
        <PendingPreview skill={action.skill} preview={action.preview} />
        {err && (
          <div className="mt-2 text-[11px] text-red-600 flex items-start gap-1.5">
            <AlertCircle className="w-3 h-3 mt-0.5 shrink-0" />
            <span>{err}</span>
          </div>
        )}
      </div>
      <div className="px-3 pb-2.5 pt-1.5 border-t border-border bg-muted/20 grid grid-cols-3 gap-1.5">
        <button
          onClick={() => resolve("test")}
          disabled={busy !== null}
          className={cn(
            "text-[11px] px-2 py-1.5 rounded-md border border-border bg-card",
            "hover:bg-muted transition inline-flex items-center justify-center gap-1",
            busy === "test" && "opacity-60 cursor-wait",
          )}
          title="Run it, then revert. Doesn't count toward the LLM's success rate."
        >
          {busy === "test"
            ? <Loader2 className="w-3 h-3 animate-spin" />
            : <FlaskConical className="w-3 h-3 text-amber-500" />}
          Just testing
        </button>
        <button
          onClick={() => resolve("cancelled")}
          disabled={busy !== null}
          className={cn(
            "text-[11px] px-2 py-1.5 rounded-md border border-border bg-card text-muted-foreground",
            "hover:bg-muted hover:text-foreground transition",
            busy === "cancelled" && "opacity-60 cursor-wait",
          )}
        >
          {busy === "cancelled" ? "…" : "Cancel"}
        </button>
        <button
          onClick={() => resolve("confirmed")}
          disabled={busy !== null}
          className={cn(
            "text-[11px] px-2 py-1.5 rounded-md bg-violet-500 hover:bg-violet-600 text-white font-medium",
            "transition inline-flex items-center justify-center gap-1 shadow-sm",
            busy === "confirmed" && "opacity-80 cursor-wait",
          )}
        >
          {busy === "confirmed"
            ? <Loader2 className="w-3 h-3 animate-spin" />
            : <CheckCircle2 className="w-3 h-3" />}
          Looks good
        </button>
      </div>
    </div>
  );
}

function ResolvedStamp({ kind, compact }: { kind: Resolution; compact?: boolean }) {
  const data = {
    confirmed: { icon: CheckCircle2, color: "text-emerald-600", label: "Confirmed" },
    cancelled: { icon: X,            color: "text-muted-foreground", label: "Cancelled — reverted" },
    test:      { icon: FlaskConical, color: "text-amber-600", label: "Tested — reverted" },
  }[kind];
  const Icon = data.icon;
  return (
    <div className={cn(
      "mt-2 inline-flex items-center gap-1.5 px-2 py-1 rounded-md text-[10px] border border-border bg-muted/30",
      data.color, compact ? "" : "",
    )}>
      <Icon className="w-3 h-3" />
      {data.label}
    </div>
  );
}

// ── Per-skill preview renderers ─────────────────────────────────────

function PendingPreview({ skill, preview }: { skill: string; preview: any }) {
  if (!preview) return <div className="text-xs text-muted-foreground italic">(no preview)</div>;
  if (skill === "add_calendar_event")    return <CalendarCreatePreview p={preview} />;
  if (skill === "update_calendar_event") return <CalendarUpdatePreview p={preview} />;
  if (skill === "delete_calendar_event") return <CalendarDeletePreview p={preview} />;
  if (skill === "add_task")              return <TaskCreatePreview p={preview} />;
  if (skill === "update_task")           return <TaskUpdatePreview p={preview} />;
  if (skill === "delete_task")           return <TaskDeletePreview p={preview} />;
  if (skill === "add_bill")              return <BillCreatePreview p={preview} />;
  if (skill === "update_bill")           return <BillUpdatePreview p={preview} />;
  if (skill === "delete_bill")           return <BillDeletePreview p={preview} />;
  return (
    <pre className="text-[10px] bg-muted/30 border border-border rounded p-1.5 font-mono whitespace-pre-wrap break-words">
      {JSON.stringify(preview, null, 2)}
    </pre>
  );
}

function fmtDateTime(iso?: string): string {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    return d.toLocaleString([], {
      weekday: "short", day: "numeric", month: "short",
      hour: "2-digit", minute: "2-digit",
    });
  } catch { return iso; }
}

function CalendarCreatePreview({ p }: { p: any }) {
  return (
    <div className="space-y-0.5 text-xs">
      <div><span className="text-muted-foreground">Create:</span> <span className="font-semibold">{p.title}</span></div>
      <div><span className="text-muted-foreground">When:</span> {fmtDateTime(p.starts_at)} → {fmtDateTime(p.ends_at)}</div>
      {p.person && <div><span className="text-muted-foreground">Who:</span> {p.person}</div>}
      {p.notes  && <div><span className="text-muted-foreground">Notes:</span> {p.notes}</div>}
    </div>
  );
}

function CalendarUpdatePreview({ p }: { p: any }) {
  const fields = ["title", "starts_at", "ends_at", "person", "notes", "all_day"] as const;
  const changed = fields.filter(k => p.before?.[k] !== p.after?.[k]);
  return (
    <div className="space-y-0.5 text-xs">
      <div><span className="text-muted-foreground">Update event:</span> <span className="font-mono">#{p.event_id}</span></div>
      {changed.length === 0 && <div className="text-muted-foreground italic">No changes.</div>}
      {changed.map(k => {
        const before = k.includes("_at") ? fmtDateTime(p.before[k]) : String(p.before[k] ?? "—");
        const after  = k.includes("_at") ? fmtDateTime(p.after[k])  : String(p.after[k] ?? "—");
        return (
          <div key={k} className="text-[11px]">
            <span className="text-muted-foreground">{k.replace("_", " ")}:</span>{" "}
            <span className="line-through text-red-500/80">{before}</span>{" → "}
            <span className="font-medium text-emerald-600">{after}</span>
          </div>
        );
      })}
    </div>
  );
}

function CalendarDeletePreview({ p }: { p: any }) {
  return (
    <div className="space-y-0.5 text-xs">
      <div><span className="text-muted-foreground">Delete event:</span> <span className="font-mono">#{p.event_id}</span></div>
      <div className="p-1.5 bg-red-500/5 border border-red-500/20 rounded text-[11px]">
        <div className="font-medium">{p.event?.title}</div>
        <div className="text-muted-foreground">{fmtDateTime(p.event?.starts_at)}{p.event?.person && ` · ${p.event.person}`}</div>
      </div>
    </div>
  );
}

// ── Task previews ────────────────────────────────────────────────────

function fmtDate(iso?: string): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" });
  } catch { return iso; }
}

function TaskCreatePreview({ p }: { p: any }) {
  return (
    <div className="space-y-0.5 text-xs">
      <div><span className="text-muted-foreground">Create task:</span> <span className="font-semibold">{p.title}</span></div>
      {p.due_date && <div><span className="text-muted-foreground">Due:</span> {fmtDate(p.due_date)}</div>}
      {p.person   && <div><span className="text-muted-foreground">Who:</span> {p.person}</div>}
      {p.category && <div><span className="text-muted-foreground">Category:</span> {p.category}</div>}
      {p.notes    && <div><span className="text-muted-foreground">Notes:</span> {p.notes}</div>}
    </div>
  );
}

function TaskUpdatePreview({ p }: { p: any }) {
  const fields = ["title", "due_date", "done", "person", "category", "notes"] as const;
  const changed = fields.filter(k => p.before?.[k] !== p.after?.[k]);
  return (
    <div className="space-y-0.5 text-xs">
      <div><span className="text-muted-foreground">Update task:</span> <span className="font-mono">#{p.task_id}</span></div>
      {changed.length === 0 && <div className="text-muted-foreground italic">No changes.</div>}
      {changed.map(k => {
        const before = k === "due_date" ? fmtDate(p.before[k]) : String(p.before[k] ?? "—");
        const after  = k === "due_date" ? fmtDate(p.after[k])  : String(p.after[k] ?? "—");
        return (
          <div key={k} className="text-[11px]">
            <span className="text-muted-foreground">{k.replace("_", " ")}:</span>{" "}
            <span className="line-through text-red-500/80">{before}</span>{" → "}
            <span className="font-medium text-emerald-600">{after}</span>
          </div>
        );
      })}
    </div>
  );
}

function TaskDeletePreview({ p }: { p: any }) {
  return (
    <div className="space-y-0.5 text-xs">
      <div><span className="text-muted-foreground">Delete task:</span> <span className="font-mono">#{p.task_id}</span></div>
      <div className="p-1.5 bg-red-500/5 border border-red-500/20 rounded text-[11px]">
        <div className="font-medium">{p.task?.title}</div>
        <div className="text-muted-foreground">
          {p.task?.due_date && `Due ${fmtDate(p.task.due_date)}`}
          {p.task?.person && ` · ${p.task.person}`}
          {p.task?.done && " · ✓ done"}
        </div>
      </div>
    </div>
  );
}

// ── Bill previews ────────────────────────────────────────────────────

function fmtMoney(amount?: number, currency?: string): string {
  if (amount == null) return "—";
  const c = currency || "EUR";
  try {
    return new Intl.NumberFormat([], { style: "currency", currency: c }).format(amount);
  } catch {
    return `${amount} ${c}`;
  }
}

function BillCreatePreview({ p }: { p: any }) {
  return (
    <div className="space-y-0.5 text-xs">
      <div><span className="text-muted-foreground">Create bill:</span> <span className="font-semibold">{p.name}</span></div>
      <div><span className="text-muted-foreground">Amount:</span> <span className="font-mono">{fmtMoney(p.amount, p.currency)}</span></div>
      {p.due_date  && <div><span className="text-muted-foreground">Due:</span> {fmtDate(p.due_date)}</div>}
      {p.recurring && <div><span className="text-muted-foreground">Recurring:</span> {p.recurring}</div>}
      {p.notes     && <div><span className="text-muted-foreground">Notes:</span> {p.notes}</div>}
    </div>
  );
}

function BillUpdatePreview({ p }: { p: any }) {
  const fields = ["name", "amount", "currency", "due_date", "recurring", "paid", "notes"] as const;
  const changed = fields.filter(k => p.before?.[k] !== p.after?.[k]);
  return (
    <div className="space-y-0.5 text-xs">
      <div><span className="text-muted-foreground">Update bill:</span> <span className="font-mono">#{p.bill_id}</span></div>
      {changed.length === 0 && <div className="text-muted-foreground italic">No changes.</div>}
      {changed.map(k => {
        const fmt = (v: any) =>
          k === "amount"   ? fmtMoney(v, p.after?.currency || p.before?.currency)
        : k === "due_date" ? fmtDate(v)
        : String(v ?? "—");
        return (
          <div key={k} className="text-[11px]">
            <span className="text-muted-foreground">{k.replace("_", " ")}:</span>{" "}
            <span className="line-through text-red-500/80">{fmt(p.before[k])}</span>{" → "}
            <span className="font-medium text-emerald-600">{fmt(p.after[k])}</span>
          </div>
        );
      })}
    </div>
  );
}

function BillDeletePreview({ p }: { p: any }) {
  return (
    <div className="space-y-0.5 text-xs">
      <div><span className="text-muted-foreground">Delete bill:</span> <span className="font-mono">#{p.bill_id}</span></div>
      <div className="p-1.5 bg-red-500/5 border border-red-500/20 rounded text-[11px]">
        <div className="font-medium">{p.bill?.name}</div>
        <div className="text-muted-foreground">
          {fmtMoney(p.bill?.amount, p.bill?.currency)}
          {p.bill?.due_date && ` · due ${fmtDate(p.bill.due_date)}`}
          {p.bill?.paid && " · ✓ paid"}
        </div>
      </div>
    </div>
  );
}
