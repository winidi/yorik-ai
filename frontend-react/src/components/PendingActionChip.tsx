/**
 * PendingActionChip — compact "✓ done · Undo" replacement for the
 * heavier three-button PendingActionPanel, used in the chat thread.
 *
 * Semantics:
 *   The action is ALREADY applied by the backend skill (apply-then-
 *   confirm pattern — see backend/skills/add_calendar_event/skill.py).
 *   The chip just shows the success summary + an Undo affordance.
 *
 *   "Undo" maps to /api/pending/{id}/cancel — rolls back the action.
 *   The Undo button auto-fades after `UNDO_WINDOW_MS`; the chip
 *   itself stays visible (so the user always knows what Yorik did),
 *   but the affordance becomes muted/unclickable so accidental late
 *   clicks don't surprise them.
 *
 * The legacy PendingActionPanel stays in tree for the voice popover
 * (which uses "Just testing" for QA) — we don't yank it.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  Check, Loader2, Undo2, AlertCircle,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";
import { emitUiAction } from "@/lib/uiActions";

interface PendingAction {
  pending_id: string;
  skill: string;
  preview: any;
  llm_model?: string;
}

interface Props {
  action: PendingAction;
}

// Window during which Undo is one-click. After that the chip stays
// (the user can still find the row in the calendar / contacts and
// delete it manually), but the inline button disables itself.
const UNDO_WINDOW_MS = 30_000;


export function PendingActionChip({ action }: Props) {
  const [busy, setBusy] = useState(false);
  const [undone, setUndone] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [expired, setExpired] = useState(false);
  const timerRef = useRef<number | null>(null);

  useEffect(() => {
    timerRef.current = window.setTimeout(() => setExpired(true), UNDO_WINDOW_MS);
    return () => {
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    };
  }, []);

  const undo = useCallback(async () => {
    if (busy || undone || expired) return;
    setBusy(true);
    setErr(null);
    try {
      const r = await api.post<{ ok: boolean; ui_actions?: any[] }>(
        `/api/pending/${encodeURIComponent(action.pending_id)}/cancel`,
        {},
      );
      for (const a of r.ui_actions || []) emitUiAction(a);
      setUndone(true);
    } catch (e: any) {
      setErr(e?.message || "Couldn't undo.");
    } finally {
      setBusy(false);
    }
  }, [busy, undone, expired, action.pending_id]);

  const summary = summarizeSkill(action.skill, action.preview);
  const href = undone ? null : navTargetFor(action.skill, action.preview);

  return (
    <div className={cn(
      "mt-2 inline-flex items-center gap-2 rounded-full pl-2.5 pr-1.5 py-1 text-[11px]",
      "border transition",
      undone
        ? "border-muted-foreground/20 bg-muted/40 text-muted-foreground"
        : "border-emerald-500/30 bg-emerald-500/[0.06] text-emerald-700 dark:text-emerald-400",
    )}>
      {undone ? (
        <>
          <Undo2 className="w-3 h-3" />
          <span>{summary.undoneLabel} — undone</span>
        </>
      ) : (
        <>
          <Check className="w-3 h-3" />
          {href ? (
            <a
              href={href}
              className="font-medium hover:underline"
              title="Open"
            >
              {summary.doneLabel}
            </a>
          ) : (
            <span className="font-medium">{summary.doneLabel}</span>
          )}
          <button
            type="button"
            onClick={undo}
            disabled={busy || expired}
            className={cn(
              "ml-1 inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full",
              "text-[10px] font-medium transition",
              expired
                ? "text-muted-foreground/40 cursor-not-allowed"
                : "text-emerald-700 dark:text-emerald-400 hover:bg-emerald-500/15",
              busy && "opacity-60 cursor-wait",
            )}
            title={expired
              ? "Undo window expired — delete the row manually if needed"
              : "Undo (rolls the action back)"}
          >
            {busy
              ? <Loader2 className="w-2.5 h-2.5 animate-spin" />
              : <Undo2 className="w-2.5 h-2.5" />}
            {expired ? "saved" : "Undo"}
          </button>
        </>
      )}
      {err && (
        <span className="text-rose-500 inline-flex items-center gap-1">
          <AlertCircle className="w-3 h-3" /> {err}
        </span>
      )}
    </div>
  );
}


/* ─── summary text per skill ────────────────────────────────────── */
//
// Keep the chip ONE line. The full structured preview already lives
// in the calendar/contacts/tasks app the user can jump to; the chip
// just needs to remind them what just happened in plain words.

interface Summary { doneLabel: string; undoneLabel: string }

function summarizeSkill(skill: string, preview: any): Summary {
  const p = preview || {};
  switch (skill) {
    case "add_calendar_event":
      return mk(
        `Event added: ${truncate(p.title || "(no title)", 36)}`
          + (p.starts_at ? ` · ${fmtWhen(p.starts_at)}` : ""),
        "Event",
      );
    case "update_calendar_event":
      return mk(`Event updated: ${truncate(p.title || p.event?.title || "Event", 36)}`, "Update");
    case "delete_calendar_event":
      return mk(`Event deleted: ${truncate(p.event?.title || "Event", 36)}`, "Deletion");
    case "add_task":
      return mk(`Task added: ${truncate(p.title || "Task", 36)}`, "Task");
    case "update_task":
      return mk(`Task updated: ${truncate(p.title || "Task", 36)}`, "Update");
    case "delete_task":
      return mk(`Task deleted: ${truncate(p.title || "Task", 36)}`, "Deletion");
    case "add_contact":
      return mk(`Contact added: ${truncate(p.display_name || "Contact", 36)}`, "Contact");
    case "update_contact":
      return mk(`Contact updated: ${truncate(p.display_name || "Contact", 36)}`, "Update");
    case "delete_contact":
      return mk(`Contact deleted: ${truncate(p.display_name || "Contact", 36)}`, "Deletion");
    case "block_travel_time": {
      const isReturn = p.direction === "return";
      const noun = isReturn ? "Return trip" : "Travel";
      return mk(`${noun} blocked (${p.minutes || "?"} min)`, `${noun} block`);
    }
    case "add_bill":
      return mk(`Bill added: ${truncate(p.title || "Bill", 36)}`, "Bill");
    default:
      return mk(`${skill} done`, skill);
  }
}

function mk(doneLabel: string, undoneNoun: string): Summary {
  return { doneLabel, undoneLabel: undoneNoun };
}

function truncate(s: string, n: number): string {
  if (!s) return "";
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

/* ─── click-through target per skill ────────────────────────────── */
//
// Returns a deep-link path the chip should navigate to when clicked,
// or null if the action has no useful target (e.g. delete — the row's
// gone, no reason to open it). Calendar links anchor to the event's
// week so the user sees the event in context, not the dialog alone.

function navTargetFor(skill: string, preview: any): string | null {
  const p = preview || {};
  switch (skill) {
    case "add_calendar_event":
    case "update_calendar_event": {
      const starts = p.starts_at || p.after?.starts_at;
      const id = p.event_id;
      const date = isoDate(starts);
      if (!date) return null;
      const qs = new URLSearchParams({ date, view: "week" });
      if (id) qs.set("event", String(id));
      return `/r/calendar?${qs.toString()}`;
    }
    case "delete_calendar_event": {
      // Event is gone — but landing the user in the week it lived in
      // is still useful ("did I delete the right one?" → glance check).
      const starts = p.event?.starts_at;
      const date = isoDate(starts);
      if (!date) return null;
      return `/r/calendar?date=${date}&view=week`;
    }
    case "add_task":
    case "update_task": {
      const id = p.task_id || p.id;
      return id ? `/r/tasks?task=${encodeURIComponent(String(id))}` : `/r/tasks`;
    }
    case "delete_task":
      return `/r/tasks`;
    case "add_contact":
    case "update_contact": {
      const id = p.contact_id || p.id;
      return id ? `/r/contacts?contact=${encodeURIComponent(String(id))}` : `/r/contacts`;
    }
    case "delete_contact":
      return `/r/contacts`;
    case "add_bill":
      return `/r/home`;
    case "block_travel_time": {
      const starts = p.starts_at;
      const date = isoDate(starts);
      return date ? `/r/calendar?date=${date}&view=week` : `/r/calendar`;
    }
    default:
      return null;
  }
}

function isoDate(iso?: string | null): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  // Use local-date components so the user lands on the day they see
  // in chat (not the UTC date, which can be off by one near midnight).
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${yyyy}-${mm}-${dd}`;
}

function fmtWhen(iso: string): string {
  // Parse ISO; fall back to raw if it doesn't look like one.
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const tomorrow = new Date(now); tomorrow.setDate(now.getDate() + 1);
  const sameTomorrow = d.toDateString() === tomorrow.toDateString();
  const hhmm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  if (sameDay)      return `heute ${hhmm}`;
  if (sameTomorrow) return `morgen ${hhmm}`;
  return `${String(d.getDate()).padStart(2, "0")}.${String(d.getMonth() + 1).padStart(2, "0")}. ${hhmm}`;
}
