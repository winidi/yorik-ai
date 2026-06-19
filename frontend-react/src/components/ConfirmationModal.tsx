/**
 * Confirmation modal — beta safety net for LLM-initiated mutations.
 *
 * Listens for `yorik-ui-action` events of type `pending_confirmation`
 * (emitted by the skills themselves via the backend's ui_actions list).
 * Renders a 3-button modal:
 *
 *   [ Just testing ]      [ Cancel ]      [ Looks good ]
 *      (left)              (middle)        (bottom right)
 *
 * The button layout is deliberate per the user's spec: "Just testing"
 * is a frequent action during beta (test the LLM without polluting the
 * success rate), so it gets a prominent left position; "Looks good" is
 * the happy path on the right; "Cancel" is the less-likely escape hatch
 * in the middle.
 *
 * Each button resolves the pending action via:
 *   POST /api/pending/{id}/confirm   (Looks good)
 *   POST /api/pending/{id}/cancel    (Cancel)
 *   POST /api/pending/{id}/test      (Just testing — runs but excluded from stats)
 *
 * The decision is logged to skill_decisions per (skill, llm_model) so
 * the Settings → Quality tab can show "Qwen 7B: 94% success on calendar
 * adds, Llama 3.2: 71%". This is the killer beta-telemetry feature.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { CheckCircle2, X, FlaskConical, AlertCircle, Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";

interface PendingAction {
  pending_id: string;
  skill: string;
  preview: any;
  llm_model?: string;
}

interface ConfirmResponse {
  ok: boolean;
  result?: any;
  ui_actions?: any[];
  test?: boolean;
  cancelled?: boolean;
}

export function ConfirmationModal() {
  const [pending, setPending] = useState<PendingAction | null>(null);
  const [busy, setBusy] = useState<"confirm" | "cancel" | "test" | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const voiceListenRef = useRef<{ stop: () => void } | null>(null);

  // Listen for the staged-action UI event from skills.
  useEffect(() => {
    function handler(e: Event) {
      const detail = (e as CustomEvent).detail;
      if (!detail || detail.type !== "pending_confirmation") return;
      setPending({
        pending_id: detail.pending_id,
        skill:      detail.skill,
        preview:    detail.preview,
        llm_model:  detail.llm_model,
      });
      setErr(null);
    }
    window.addEventListener("yorik-ui-action", handler);
    return () => window.removeEventListener("yorik-ui-action", handler);
  }, []);

  // On mount, fetch any open pending actions (e.g. user reloaded mid-modal).
  useEffect(() => {
    if (pending) return;
    let cancelled = false;
    (async () => {
      try {
        const r = await api.get<{ pending: PendingAction[] }>("/api/pending");
        if (!cancelled && r.pending?.length > 0) {
          setPending(r.pending[0]);
        }
      } catch {
        // Not logged in or no pending — silent.
      }
    })();
    return () => { cancelled = true; };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const resolve = useCallback(async (kind: "confirm" | "cancel" | "test") => {
    if (!pending) return;
    setBusy(kind);
    setErr(null);
    try {
      const r = await api.post<ConfirmResponse>(
        `/api/pending/${encodeURIComponent(pending.pending_id)}/${kind}`,
        {},
      );
      // Apply downstream UI actions (show_calendar with highlight, etc.)
      for (const action of r.ui_actions || []) {
        window.dispatchEvent(new CustomEvent("yorik-ui-action", { detail: action }));
      }
      setPending(null);
      voiceListenRef.current?.stop();
    } catch (e: any) {
      setErr(e?.message || "Failed to resolve. Try again or cancel.");
    } finally {
      setBusy(null);
    }
  }, [pending]);

  // Voice listen window: when a modal opens, briefly listen for ja/nein/passt
  // so a voice user doesn't have to reach for the screen.
  useEffect(() => {
    if (!pending) return;
    voiceListenRef.current = startVoiceListen({
      onConfirm: () => resolve("confirm"),
      onCancel:  () => resolve("cancel"),
    });
    return () => voiceListenRef.current?.stop();
  }, [pending, resolve]);

  if (!pending) return null;

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/40 backdrop-blur-sm p-4">
      <div className="w-full max-w-md bg-card border border-border rounded-2xl shadow-2xl overflow-hidden">
        <header className="px-5 py-3 border-b border-border flex items-center justify-between">
          <div className="flex items-center gap-2">
            <div className="w-7 h-7 rounded-full bg-violet-500/15 flex items-center justify-center">
              <AlertCircle className="w-4 h-4 text-violet-500" />
            </div>
            <div>
              <div className="text-sm font-semibold">Does this look right?</div>
              <div className="text-[10px] text-muted-foreground font-mono">
                {pending.skill}
                {pending.llm_model && ` · ${pending.llm_model}`}
              </div>
            </div>
          </div>
          <button
            onClick={() => resolve("cancel")}
            aria-label="Close"
            className="w-7 h-7 rounded-md hover:bg-muted text-muted-foreground flex items-center justify-center"
            disabled={busy !== null}
          >
            <X className="w-4 h-4" />
          </button>
        </header>

        <div className="p-5">
          <SkillPreview skill={pending.skill} preview={pending.preview} />
          {err && (
            <div className="mt-3 text-xs text-red-600 flex items-start gap-1.5">
              <AlertCircle className="w-3.5 h-3.5 mt-0.5 shrink-0" />
              <span>{err}</span>
            </div>
          )}
          <div className="mt-3 text-[10px] text-muted-foreground italic">
            Listening for "ja" / "nein" / "passt" / "abbrechen"…
          </div>
        </div>

        <footer className="px-5 py-3 border-t border-border bg-muted/20 grid grid-cols-3 gap-2">
          <button
            onClick={() => resolve("test")}
            disabled={busy !== null}
            className={cn(
              "text-xs px-3 py-2 rounded-md border border-border bg-card",
              "hover:bg-muted transition inline-flex items-center justify-center gap-1.5",
              busy === "test" && "opacity-60 cursor-wait",
            )}
            title="Run it, but don't count toward the LLM's success rate"
          >
            {busy === "test"
              ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
              : <FlaskConical className="w-3.5 h-3.5 text-amber-500" />}
            Just testing
          </button>
          <button
            onClick={() => resolve("cancel")}
            disabled={busy !== null}
            className={cn(
              "text-xs px-3 py-2 rounded-md border border-border bg-card text-muted-foreground",
              "hover:bg-muted hover:text-foreground transition",
              busy === "cancel" && "opacity-60 cursor-wait",
            )}
          >
            {busy === "cancel" ? "Cancelling…" : "Cancel"}
          </button>
          <button
            onClick={() => resolve("confirm")}
            disabled={busy !== null}
            className={cn(
              "text-xs px-3 py-2 rounded-md bg-violet-500 hover:bg-violet-600 text-white font-medium",
              "transition inline-flex items-center justify-center gap-1.5 shadow-sm",
              busy === "confirm" && "opacity-80 cursor-wait",
            )}
          >
            {busy === "confirm"
              ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
              : <CheckCircle2 className="w-3.5 h-3.5" />}
            Looks good
          </button>
        </footer>
      </div>
    </div>
  );
}

// ── Per-skill preview renderers ─────────────────────────────────────

function SkillPreview({ skill, preview }: { skill: string; preview: any }) {
  if (!preview) return <div className="text-sm text-muted-foreground italic">(no preview available)</div>;

  if (skill === "add_calendar_event") return <CalendarCreatePreview p={preview} />;
  if (skill === "update_calendar_event") return <CalendarUpdatePreview p={preview} />;
  if (skill === "delete_calendar_event") return <CalendarDeletePreview p={preview} />;

  // Generic fallback for skills we haven't tailored yet.
  return (
    <pre className="text-[11px] bg-muted/40 border border-border rounded p-2 font-mono whitespace-pre-wrap break-words">
      {JSON.stringify(preview, null, 2)}
    </pre>
  );
}

function fmtDateTime(iso?: string): string {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    return d.toLocaleString([], {
      weekday: "short",
      day: "numeric",
      month: "short",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

function CalendarCreatePreview({ p }: { p: any }) {
  return (
    <div className="space-y-2 text-sm">
      <Row label="Create event" value={p.title} bold />
      <Row label="When" value={`${fmtDateTime(p.starts_at)} → ${fmtDateTime(p.ends_at)}`} />
      {p.person && <Row label="Who" value={p.person} />}
      {p.notes && <Row label="Notes" value={p.notes} />}
      {p.all_day && <div className="text-xs text-amber-600">All day</div>}
    </div>
  );
}

function CalendarUpdatePreview({ p }: { p: any }) {
  const fields = ["title", "starts_at", "ends_at", "person", "notes", "all_day"] as const;
  const changed = fields.filter(k => p.before?.[k] !== p.after?.[k]);
  return (
    <div className="space-y-2 text-sm">
      <Row label="Update event" value={`#${p.event_id}`} bold />
      {changed.length === 0 && <div className="text-xs text-muted-foreground italic">No changes detected.</div>}
      {changed.map(k => {
        const before = k.includes("_at") ? fmtDateTime(p.before[k]) : String(p.before[k] ?? "—");
        const after  = k.includes("_at") ? fmtDateTime(p.after[k])  : String(p.after[k] ?? "—");
        return (
          <div key={k} className="text-xs">
            <span className="text-muted-foreground capitalize">{k.replace("_", " ")}:</span>{" "}
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
    <div className="space-y-2 text-sm">
      <Row label="Delete event" value={`#${p.event_id}`} bold />
      <div className="p-2 bg-red-500/5 border border-red-500/20 rounded text-xs">
        <div className="font-medium">{p.event?.title}</div>
        <div className="text-muted-foreground">
          {fmtDateTime(p.event?.starts_at)}
          {p.event?.person && ` · ${p.event.person}`}
        </div>
        {p.event?.notes && <div className="mt-1 text-muted-foreground">{p.event.notes}</div>}
      </div>
      <div className="text-[10px] text-amber-600">This will be deleted permanently.</div>
    </div>
  );
}

function Row({ label, value, bold }: { label: string; value: string; bold?: boolean }) {
  return (
    <div className="text-xs">
      <span className="text-muted-foreground">{label}:</span>{" "}
      <span className={cn(bold && "font-semibold text-sm")}>{value}</span>
    </div>
  );
}

// ── Voice listen for ja/nein during modal ──────────────────────────

function startVoiceListen({ onConfirm, onCancel }: {
  onConfirm: () => void;
  onCancel: () => void;
}): { stop: () => void } {
  // SpeechRecognition (Chrome/Edge) — non-blocking, ~10s window.
  // Fallback: silent — user clicks the button instead.
  const SR =
    (typeof window !== "undefined" && (window as any).SpeechRecognition) ||
    (typeof window !== "undefined" && (window as any).webkitSpeechRecognition);
  if (!SR) return { stop: () => {} };

  const rec = new SR();
  rec.continuous = false;
  rec.interimResults = false;
  // Default to browser language; the user's profile language could refine this.
  try { rec.lang = navigator.language || "en-US"; } catch {}

  let stopped = false;
  const timeout = setTimeout(() => { try { rec.stop(); } catch {} }, 10000);

  rec.onresult = (e: any) => {
    if (stopped) return;
    const text: string = (e.results[0]?.[0]?.transcript || "").toLowerCase().trim();
    if (!text) return;
    // Confirm tokens (multi-language)
    const confirmTokens = ["ja", "yes", "passt", "klar", "ok", "okay", "yep", "sí", "si", "oui", "tak"];
    const cancelTokens  = ["nein", "no", "abbrechen", "cancel", "stopp", "stop", "non", "nie"];
    if (confirmTokens.some(t => text.startsWith(t) || text === t)) {
      stopped = true;
      onConfirm();
    } else if (cancelTokens.some(t => text.startsWith(t) || text === t)) {
      stopped = true;
      onCancel();
    }
  };
  rec.onerror = () => {};  // silent — user can click

  try { rec.start(); } catch {}

  return {
    stop: () => {
      stopped = true;
      clearTimeout(timeout);
      try { rec.stop(); } catch {}
    },
  };
}
