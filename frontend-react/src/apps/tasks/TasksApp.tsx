/**
 * Tasks app — first-class to-do management with smart grouping and an
 * LLM-powered "ask anything" bar.
 *
 * Three layers of capability:
 *
 *   1. Default static view: tasks bucketed by due date (Overdue,
 *      Today, Tomorrow, This week, Later, No date) with priority
 *      badges and estimated-time chips. The everyday case.
 *
 *   2. Group toggle: swap the bucketing to category / person / flat
 *      without leaving the screen. Useful for "everything tagged
 *      #renovation" kinds of triage.
 *
 *   3. Magic search bar: free-text query → POST /api/tasks/ask →
 *      the local LLM filters and re-groups the list. Returns a
 *      one-line summary the user sees above the rendered groups.
 *      "what's overdue", "tasks for the wedding", "things under 30
 *      minutes I can knock out now" all work.
 *
 * REST `/api/tasks` is still the source of truth — the magic bar
 * just slices/relabels what's already there. Closing/editing tasks
 * doesn't go through the LLM.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  Plus, Trash2, Check, X, CalendarDays, Loader2, ChevronRight,
  CheckSquare, Square, ListTodo, Sparkles, Clock, Flag,
  ChevronDown, Repeat, Inbox as InboxIcon, Sun, CalendarRange,
  Layers, Eye, EyeOff,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { Dock } from "@/components/Dock";
import type { Task } from "../calendar/types";

const ROLE = "admin";

// Prompts behind the "Smart tools" presets (desktop chips + mobile
// sheet). Kept here so the wording stays in one place — the same query
// runs whichever surface dispatches it.
const SMART_PRESETS: Array<{ label: string; q: string }> = [
  { label: "Estimate missing durations", q: "estimate durations for tasks that don't have one yet" },
  { label: "Auto-prioritise",            q: "set priority on each task based on the title and due date" },
  { label: "Auto-categorise",            q: "suggest a category for each uncategorised task" },
];

// View tabs replace the old Open/Done/All filter — matches the
// Things 3 / Todoist mental model. Done is a per-view toggle now.
type ViewMode = "today" | "inbox" | "upcoming" | "all";
type GroupMode = "due" | "category" | "person" | "flat";

type ExtendedTask = Task & {
  priority?: number;
  estimated_minutes?: number | null;
  parent_task_id?: number | null;
  recurrence_rule?: string | null;
};

interface ProposedUpdate {
  id: number;
  changes: {
    estimated_minutes?: number;
    priority?: number;
    category?: string;
  };
}

interface AskResponse {
  query: string;
  mode: "view" | "update";
  filtered_ids: number[];
  grouping: string;
  groups: Array<{ label: string; ids: number[] }>;
  summary: string;
  updates: ProposedUpdate[];
}

export function TasksApp() {
  const tasksApi = useApi<ExtendedTask[]>(`/api/tasks?role=${ROLE}`, []);
  const tasks = tasksApi.data || [];

  const [view, setView] = useState<ViewMode>("today");
  const [showDone, setShowDone] = useState(false);
  const [grouping, setGrouping] = useState<GroupMode>("due");
  const [editingId, setEditingId] = useState<number | null>(null);

  // Deep-link: `/tasks?task=<id>` highlights that task for a few seconds
  // and scrolls it into view. Used by the home-screen briefing card so a
  // click on "Tomorrow → Buy birthday gift" lands at the task instead of
  // just on /tasks. The highlight clears itself after ~3s so it feels
  // like a flash, not permanent state.
  const [searchParams] = useSearchParams();
  const [highlightTaskId, setHighlightTaskId] = useState<number | null>(null);
  const highlightTimerRef = useRef<number | null>(null);
  useEffect(() => {
    const raw = searchParams.get("task");
    if (!raw) return;
    const id = parseInt(raw, 10);
    if (Number.isNaN(id)) return;
    setHighlightTaskId(id);
    // Scroll into view on the next paint — wait for the list to render.
    requestAnimationFrame(() => {
      document.querySelector(`[data-task-id="${id}"]`)
        ?.scrollIntoView({ behavior: "smooth", block: "center" });
    });
    if (highlightTimerRef.current) clearTimeout(highlightTimerRef.current);
    highlightTimerRef.current = window.setTimeout(() => setHighlightTaskId(null), 3000);
    return () => {
      if (highlightTimerRef.current) clearTimeout(highlightTimerRef.current);
    };
  }, [searchParams]);

  // Unified input — the one bar that does both "add a task" (natural-
  // language create → /tasks/parse-natural + /tasks) and "ask Yorik"
  // (filter/update via /tasks/ask). Two send buttons disambiguate;
  // Enter fires whichever was last used (persisted to localStorage).
  // Replaces the previous split between a top Ask bar and a sticky-
  // bottom composer.
  const [inputText, setInputText] = useState("");
  const [magicResult, setMagicResult] = useState<AskResponse | null>(null);
  const [magicLoading, setMagicLoading] = useState(false);
  const [adding, setAdding] = useState(false);

  // Which action Enter triggers. Sticky per browser so a user who
  // mostly captures gets capture-on-Enter, and a user who mostly
  // queries gets query-on-Enter. First-time default is "add" — matches
  // the previous autofocus-on-composer behavior.
  const [defaultAction, setDefaultActionState] = useState<"add" | "ask">(() => {
    if (typeof window === "undefined") return "add";
    try {
      return localStorage.getItem("yorik_tasks_default_action") === "ask" ? "ask" : "add";
    } catch { return "add"; }
  });
  const setDefaultAction = useCallback((a: "add" | "ask") => {
    setDefaultActionState(a);
    try { localStorage.setItem("yorik_tasks_default_action", a); } catch {}
  }, []);

  // Tiny breadcrumb of what the LLM just parsed, shown briefly under
  // the bar ("understood: morgen · #haushalt · alle 7 Tage"). Cleared
  // after a few seconds so the bar is calm at rest.
  const [parseHint, setParseHint] = useState<string | null>(null);
  const parseHintTimerRef = useRef<number | null>(null);

  // Desktop autofocuses the bar so power users can capture without
  // grabbing the mouse. Gate on pointer type, not viewport width — a
  // landscape tablet (1280px+) still pops the on-screen keyboard if
  // we focus on mount, covering half the list. pointer:fine = mouse,
  // pointer:coarse = touch; focus only for the mouse case.
  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (window.matchMedia("(pointer: fine)").matches) {
      inputRef.current?.focus();
    }
  }, []);

  // The pool of tasks the rest of the UI works against — view
  // mode applies BEFORE grouping/magic-search. Each view bucket:
  //   today    — due_date ≤ today (overdue rolls in here)
  //   inbox    — no due_date (categorised-but-undated tasks count too,
  //              so they don't silently fall off every tab except All)
  //   upcoming — due_date > today
  //   all      — everything
  // showDone toggles whether completed tasks appear in the chosen view.
  const filteredByMode = useMemo(() => {
    const todayISO = new Date().toISOString().slice(0, 10);
    return tasks.filter(t => {
      if (!showDone && t.done) return false;
      // Children render NESTED under their parent in TaskRow; they
      // never appear as a top-level group entry. Skip them here so
      // the grouping pass below doesn't show them twice.
      if (t.parent_task_id) return false;
      if (view === "today") {
        return !!t.due_date && t.due_date <= todayISO;
      }
      if (view === "inbox") {
        return !t.due_date;
      }
      if (view === "upcoming") {
        return !!t.due_date && t.due_date > todayISO;
      }
      return true;  // "all"
    });
  }, [tasks, view, showDone]);

  // Parent_id → children[]. Computed once per `tasks` change so each
  // TaskRow can hand its kids straight to render without rescanning.
  const childrenByParent = useMemo(() => {
    const map = new Map<number, ExtendedTask[]>();
    for (const t of tasks) {
      const pid = t.parent_task_id;
      if (pid == null) continue;
      if (!map.has(pid)) map.set(pid, []);
      map.get(pid)!.push(t);
    }
    // Sort children: done last; then by due_date ASC; then id.
    for (const arr of map.values()) {
      arr.sort((a, b) => {
        if (!!a.done !== !!b.done) return a.done ? 1 : -1;
        const ad = a.due_date || "9999";
        const bd = b.due_date || "9999";
        if (ad !== bd) return ad < bd ? -1 : 1;
        return a.id - b.id;
      });
    }
    return map;
  }, [tasks]);

  // If the magic-search bar fired, we render its `groups` directly.
  // Otherwise build groups from the chosen grouping mode.
  const renderedGroups = useMemo(() => {
    const byId = new Map<number, ExtendedTask>(filteredByMode.map(t => [t.id, t]));

    if (magicResult) {
      const grouped = magicResult.groups
        .map(g => ({ label: g.label, tasks: g.ids.map(id => byId.get(id)).filter(Boolean) as ExtendedTask[] }))
        .filter(g => g.tasks.length > 0);
      // LLM sometimes returns filtered_ids without populating groups —
      // fall back to a single "Matches" bucket so the user still sees them.
      if (grouped.length === 0 && magicResult.filtered_ids.length > 0) {
        const matches = magicResult.filtered_ids
          .map(id => byId.get(id))
          .filter(Boolean) as ExtendedTask[];
        if (matches.length > 0) return [{ label: "Matches", tasks: matches }];
      }
      return grouped;
    }

    return buildGroups(filteredByMode, grouping);
  }, [filteredByMode, grouping, magicResult]);

  const toggle = useCallback(async (t: ExtendedTask) => {
    try {
      await api.patch(`/api/tasks/${t.id}?role=${ROLE}`, { done: t.done ? 0 : 1 });
      tasksApi.refetch();
    } catch (e: any) { alert(`Failed: ${e?.message || e}`); }
  }, [tasksApi]);

  // Create a subtask under `parent`. Mirrors the structured-field shape
  // used by addTask but skips the natural-language parse — the user is
  // typing a plain title inside the parent's editor. Inherits
  // allowed_roles from "admin,member" like top-level adds; backend
  // handles assignee defaulting (creator) when assignee_user_ids omitted.
  const addSubtask = useCallback(async (parent: ExtendedTask, title: string) => {
    const trimmed = title.trim();
    if (!trimmed) return;
    try {
      await api.post(`/api/tasks?role=${ROLE}`, {
        title: trimmed,
        parent_task_id: parent.id,
        done: 0,
        priority: 1,
        allowed_roles: "admin,member",
      });
      tasksApi.refetch();
    } catch (e: any) {
      alert(`Failed to add subtask: ${e?.message || e}`);
      throw e;
    }
  }, [tasksApi]);

  const remove = useCallback(async (t: ExtendedTask) => {
    if (!confirm(`Delete task "${t.title}"?`)) return;
    try {
      await api.delete(`/api/tasks/${t.id}?role=${ROLE}`);
      tasksApi.refetch();
    } catch (e: any) { alert(`Failed: ${e?.message || e}`); }
  }, [tasksApi]);

  // Snooze — patch due_date forward by the given ISO date. Used by
  // the snooze chips that surface on row hover.
  const snoozeTask = useCallback(async (t: ExtendedTask, newDue: string) => {
    try {
      await api.patch(`/api/tasks/${t.id}?role=${ROLE}`, { due_date: newDue });
      tasksApi.refetch();
    } catch (e: any) { alert(`Snooze failed: ${e?.message || e}`); }
  }, [tasksApi]);

  const addTask = useCallback(async () => {
    const text = inputText.trim();
    if (!text) return;
    setDefaultAction("add");
    setAdding(true);
    try {
      // 1. Ask the LLM to extract structured fields from the free-form
      //    input ("Müll rausbringen morgen 18:00 #haushalt !hoch every week").
      //    The endpoint always returns at least {title: text} on failure,
      //    so this can't throw the user back to typing field-by-field.
      let parsed: {
        title: string;
        due_date?: string | null;
        priority?: number;
        category?: string | null;
        estimated_minutes?: number | null;
        recurrence_rule?: string | null;
        _warnings?: string[];
      } = { title: text };
      try {
        parsed = await api.post("/api/tasks/parse-natural", { text });
      } catch {
        // LLM offline / endpoint missing — fall through with title only.
      }

      // 2. Create the task with the parsed fields.
      await api.post(`/api/tasks?role=${ROLE}`, {
        title: (parsed.title || text).trim(),
        due_date: parsed.due_date || null,
        priority: parsed.priority ?? 1,
        category: parsed.category || null,
        estimated_minutes: parsed.estimated_minutes || null,
        recurrence_rule: parsed.recurrence_rule || null,
        done: 0,
        allowed_roles: "admin,member",
      });

      // 3. Show a brief breadcrumb of what got pulled out, so the
      //    user can see the LLM's interpretation before the row
      //    appears in the list. Cleared after ~3.5s.
      const bits: string[] = [];
      if (parsed.due_date) bits.push(`due ${parsed.due_date}`);
      if (parsed.priority === 2) bits.push("high priority");
      if (parsed.priority === 0) bits.push("low priority");
      if (parsed.category) bits.push(`#${parsed.category}`);
      if (parsed.estimated_minutes) bits.push(`${parsed.estimated_minutes} min`);
      if (parsed.recurrence_rule) bits.push(`↻ ${parsed.recurrence_rule}`);
      if (bits.length) {
        setParseHint("Yorik understood: " + bits.join(" · "));
        if (parseHintTimerRef.current) clearTimeout(parseHintTimerRef.current);
        parseHintTimerRef.current = window.setTimeout(() => setParseHint(null), 3500);
      } else {
        setParseHint(null);
      }

      setInputText("");
      tasksApi.refetch();
    } catch (e: any) { alert(`Failed: ${e?.message || e}`); }
    finally { setAdding(false); }
  }, [inputText, tasksApi, setDefaultAction]);

  const runMagic = useCallback(async () => {
    const q = inputText.trim();
    if (!q) return;
    setDefaultAction("ask");
    setMagicLoading(true);
    try {
      const r = await api.post<AskResponse>("/api/tasks/ask", { query: q });
      setMagicResult(r);
    } catch (e: any) {
      alert(`Magic search failed: ${e?.message || e}`);
    } finally { setMagicLoading(false); }
  }, [inputText, setDefaultAction]);

  // X button on the bar — abort the current magic flow. Clears both
  // the result panel and the input so the next keystroke starts fresh.
  const clearMagic = useCallback(() => {
    setInputText("");
    setMagicResult(null);
  }, []);

  const [applying, setApplying] = useState(false);
  // Tasks that just got mutated — UI highlights them briefly so the
  // user sees what changed without scanning the whole list.
  const [recentlyUpdated, setRecentlyUpdated] = useState<Set<number>>(new Set());

  // Apply a subset of the proposed updates. Called both by "Apply all"
  // (full batch with whatever the user has edited inline) and by the
  // per-row "Apply" button (single-element batch). When the applied set
  // doesn't drain the panel, we keep it open with the remaining rows so
  // the user can keep curating.
  const applyUpdates = useCallback(async (updates: ProposedUpdate[]) => {
    if (!magicResult || magicResult.mode !== "update") return;
    if (updates.length === 0) return;
    setApplying(true);
    try {
      const r = await api.post<{ applied: number; rejected: number }>(
        "/api/tasks/batch-update?role=admin",
        { updates },
      );
      const changedIds = new Set(updates.map(u => u.id));
      tasksApi.refetch();
      const remaining = magicResult.updates.filter(u => !changedIds.has(u.id));
      if (remaining.length === 0) {
        setMagicResult(null);
        setInputText("");
      } else {
        setMagicResult({ ...magicResult, updates: remaining });
      }
      setRecentlyUpdated(changedIds);
      // Clear the highlight after the flash animation finishes.
      setTimeout(() => setRecentlyUpdated(new Set()), 2200);
      if (r.rejected > 0) {
        alert(`Applied ${r.applied}, rejected ${r.rejected}.`);
      }
    } catch (e: any) {
      alert(`Apply failed: ${e?.message || e}`);
    } finally {
      setApplying(false);
    }
  }, [magicResult, tasksApi]);

  // Quick-action presets to make the update path discoverable. Tapping
  // these prefills the bar with a known-good prompt and runs it as Ask.
  // Also flips the default action to "ask" so subsequent Enter keys
  // refine the query instead of trying to create a task.
  const runPreset = useCallback(async (q: string) => {
    setInputText(q);
    setDefaultAction("ask");
    setMagicLoading(true);
    try {
      const r = await api.post<AskResponse>("/api/tasks/ask", { query: q });
      setMagicResult(r);
    } catch (e: any) {
      alert(`Magic failed: ${e?.message || e}`);
    } finally { setMagicLoading(false); }
  }, [setDefaultAction]);

  // Mobile-only toggle for the smart-tools sheet. The same three
  // presets that sit as inline chips on desktop are tucked behind a
  // single "Smart tools" button on mobile so they're discoverable
  // without eating 50px of the first viewport.
  const [mobileToolsOpen, setMobileToolsOpen] = useState(false);

  const openCount = tasks.filter(t => !t.done).length;
  const doneCount = tasks.filter(t => !!t.done).length;
  const totalEstimate = filteredByMode.reduce((acc, t) => acc + (t.estimated_minutes || 0), 0);

  // "What do I owe today?" — sum of estimated_minutes across open tasks
  // whose due_date is today or earlier (overdue rolls in here, since
  // those still need to happen today or be pushed). Counts unestimated
  // tasks separately so the chip can offer a targeted "estimate
  // missing" CTA. `doneMin` is a morale credit: tasks marked done
  // whose due_date is today (no completion timestamp in the schema, so
  // we approximate the same way doneToday does — see the comment on
  // that derivation). View-independent: this is always "today" scope,
  // not "current tab" scope, so the user has a stable anchor.
  const dailyLoad = useMemo(() => {
    const todayISO = new Date().toISOString().slice(0, 10);
    let openMin = 0;
    let doneMin = 0;
    let unestimated = 0;
    let openCountToday = 0;
    for (const t of tasks) {
      if (!t.due_date) continue;
      if (t.due_date > todayISO) continue;
      if (t.done) {
        if (t.due_date === todayISO) doneMin += t.estimated_minutes || 0;
        continue;
      }
      openCountToday += 1;
      if (t.estimated_minutes) openMin += t.estimated_minutes;
      else unestimated += 1;
    }
    return { openMin, doneMin, unestimated, openCountToday };
  }, [tasks]);

  // Per-view counts for the tab pills. Recomputed cheaply from the
  // base list — keeps the badges in sync with the actual data.
  // Subtasks are excluded so the badge matches the number of top-level
  // rows the user actually sees in the list (subtasks render nested).
  // `overdue` is the subset of Today that's strictly past — surfaced as
  // a red sub-badge so critical work isn't camouflaged inside the
  // single "Today" number.
  const viewCounts = useMemo(() => {
    const todayISO = new Date().toISOString().slice(0, 10);
    const c = { today: 0, overdue: 0, inbox: 0, upcoming: 0, all: 0 };
    for (const t of tasks) {
      if (!showDone && t.done) continue;
      if (t.parent_task_id) continue;
      c.all += 1;
      if (!t.due_date) c.inbox += 1;
      if (t.due_date && t.due_date <= todayISO) {
        c.today += 1;
        if (!t.done && t.due_date < todayISO) c.overdue += 1;
      }
      if (t.due_date && t.due_date > todayISO) c.upcoming += 1;
    }
    return c;
  }, [tasks, showDone]);

  // Done-today counter for the footer trophy chip.
  const doneToday = useMemo(() => {
    const todayISO = new Date().toISOString().slice(0, 10);
    return tasks.filter(t => t.done && (t.due_date || "").slice(0, 10) === todayISO).length;
  }, [tasks]);

  return (
    <div className="h-screen overflow-y-auto flex flex-col bg-background">
      {/* pt: tighter on mobile so the heading + bar don't eat the first
          viewport. pb: reserves room for the dock + iOS home indicator
          (env safe-area). Used to also reserve ~88px for the sticky
          bottom composer; that's gone now (unified into the bar at the
          top), so the bottom padding can be tighter. */}
      <main className="flex-1 flex flex-col max-w-6xl mx-auto w-full px-4 sm:px-8 pt-5 md:pt-10 pb-[max(7rem,calc(env(safe-area-inset-bottom)+5rem))] md:pb-24">
        <header className="mb-6">
          <div className="flex items-center gap-3 mb-2">
            <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-emerald-500/30 to-teal-500/30 flex items-center justify-center shadow-md">
              <ListTodo className="w-5 h-5 text-emerald-500" />
            </div>
            <span className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground">Yorik · tasks</span>
          </div>
          {openCount > 0 ? (
            // Mobile: compact "N open tasks" line — gets the first task
            // into the initial viewport faster on small phones. Desktop
            // keeps the full prose H1 unchanged.
            <h1 className="text-sm font-medium sm:text-3xl sm:font-semibold leading-tight">
              <span className="sm:hidden text-muted-foreground">
                <span className="text-emerald-500 font-semibold">{openCount}</span> open task{openCount !== 1 ? "s" : ""}
              </span>
              <span className="hidden sm:inline">
                You have <span className="text-emerald-500">{openCount}</span> open task{openCount !== 1 ? "s" : ""}.
              </span>
            </h1>
          ) : (
            <h1 className="text-xl sm:text-3xl font-semibold leading-tight">All clear.</h1>
          )}
          {/* Today's load chip — view-independent ("what do I owe today"
              regardless of which tab is active). High-signal so it shows
              on mobile too. The "+ N unestimated → estimate" CTA reuses
              the existing batch-estimate preset but narrowed to today's
              scope, so users can close the data gap in one tap. Color
              thresholds are hard-coded for now; a user-configurable
              daily-focus budget can land later if anyone asks. */}
          {(dailyLoad.openMin > 0 || dailyLoad.unestimated > 0 || dailyLoad.doneMin > 0) && (
            <div className="mt-2 flex items-center gap-2 flex-wrap text-xs">
              {dailyLoad.openMin > 0 && (
                <span
                  title={`${formatMinutes(dailyLoad.openMin)} of estimated work due today or overdue`}
                  className={cn(
                    "inline-flex items-center gap-1.5 px-2 py-1 rounded-md font-medium",
                    dailyLoad.openMin > 360
                      ? "bg-red-500/15 text-red-600 dark:text-red-400"
                      : dailyLoad.openMin > 180
                        ? "bg-amber-500/15 text-amber-700 dark:text-amber-400"
                        : "bg-muted text-foreground",
                  )}
                >
                  <Clock className="w-3 h-3" />
                  Today: {formatMinutes(dailyLoad.openMin)}
                </span>
              )}
              {dailyLoad.unestimated > 0 && (
                <button
                  type="button"
                  onClick={() => runPreset(
                    "estimate durations for open tasks due today or overdue that don't have one yet"
                  )}
                  disabled={magicLoading}
                  className="text-muted-foreground hover:text-violet-500 underline-offset-2 hover:underline transition disabled:opacity-50 inline-flex items-center gap-1"
                  title="Run the LLM to estimate the missing durations"
                >
                  + {dailyLoad.unestimated} unestimated
                  <Sparkles className="w-3 h-3 text-violet-500" />
                </button>
              )}
              {dailyLoad.doneMin > 0 && (
                <span
                  title={`${formatMinutes(dailyLoad.doneMin)} of estimated work already done today`}
                  className="inline-flex items-center gap-1 text-emerald-600 dark:text-emerald-400"
                >
                  <Check className="w-3 h-3" />
                  {formatMinutes(dailyLoad.doneMin)} done
                </span>
              )}
            </div>
          )}

          {/* Subtitle hidden on mobile — the open-task count above is
              enough, and reclaiming this row gets the first task into
              the initial viewport on small phones. Sums the CURRENT
              VIEW (not today specifically), so it carries different
              info than the daily-load chip above. */}
          <p className="hidden md:block text-sm text-muted-foreground mt-2">
            {totalEstimate > 0
              ? <>About <span className="text-foreground font-medium">{formatMinutes(totalEstimate)}</span> of estimated work in this view.</>
              : <>Add estimates to see how much work you're carrying.</>}
          </p>
        </header>

        {/* Unified bar — one input, two send buttons. + Add hits the
            natural-language create path; ✨ Ask hits the filter/update
            LLM. Enter fires whichever was last used (sticky per browser
            via localStorage). The active default carries a soft ring so
            users see what Enter will do without trial-and-error. Glows
            while either action is in flight. */}
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (defaultAction === "ask") runMagic();
            else addTask();
          }}
          className={cn(
            "mb-1 flex gap-1.5 sm:gap-2 bg-card border border-border rounded-xl p-2 transition",
            (magicLoading || adding) && "magic-glow",
          )}
        >
          <input
            ref={inputRef}
            value={inputText}
            onChange={e => setInputText(e.target.value)}
            placeholder="Add a task or ask Yorik anything…"
            aria-label="Add a task or ask Yorik"
            className="flex-1 min-w-0 h-11 md:h-9 px-2 bg-transparent text-sm focus:outline-none"
            disabled={magicLoading || adding}
          />
          {magicResult && (
            <button
              type="button"
              onClick={clearMagic}
              className="h-11 md:h-9 px-2 text-muted-foreground hover:text-foreground inline-flex items-center shrink-0"
              title="Clear magic filter"
              aria-label="Clear magic filter"
            >
              <X className="w-4 h-4" />
            </button>
          )}
          <button
            type="button"
            onClick={addTask}
            disabled={magicLoading || adding || !inputText.trim()}
            title={defaultAction === "add" ? "Add task (Enter)" : "Add task"}
            className={cn(
              "h-11 md:h-9 px-3 sm:px-4 rounded-md bg-primary text-primary-foreground text-sm font-semibold hover:opacity-90 disabled:opacity-50 flex items-center gap-1.5 shrink-0 transition",
              defaultAction === "add" && "ring-2 ring-primary/40",
            )}
          >
            {adding ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
            <span className="hidden sm:inline">{adding ? "Yorik liest…" : "Add"}</span>
            {defaultAction === "add" && !adding && (
              <span className="hidden md:inline text-[10px] opacity-70" aria-hidden="true">↵</span>
            )}
          </button>
          <button
            type="button"
            onClick={runMagic}
            disabled={magicLoading || adding || !inputText.trim()}
            title={defaultAction === "ask" ? "Ask Yorik (Enter)" : "Ask Yorik"}
            className={cn(
              "h-11 md:h-9 px-3 sm:px-4 rounded-md bg-violet-500 text-white text-sm font-medium hover:opacity-90 disabled:opacity-50 flex items-center gap-1.5 shrink-0 transition",
              defaultAction === "ask" && "ring-2 ring-violet-500/40",
            )}
          >
            {magicLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Sparkles className="w-4 h-4" />}
            <span className="hidden sm:inline">Ask</span>
            {defaultAction === "ask" && !magicLoading && (
              <span className="hidden md:inline text-[10px] opacity-70" aria-hidden="true">↵</span>
            )}
          </button>
        </form>

        {/* Transient feedback row under the bar. parseHint shows what
            the LLM extracted on Add ("Yorik understood: morgen · 30 min").
            doneToday is the trophy line that used to sit above the
            bottom composer. Both are auto-dismissed / data-driven. */}
        {(parseHint || doneToday >= 1) && (
          <div className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] px-1">
            {parseHint && (
              <span className="text-emerald-700 dark:text-emerald-400 inline-flex items-center gap-1">
                <Sparkles className="w-3 h-3" />
                {parseHint}
              </span>
            )}
            {doneToday >= 1 && (
              <span className="text-muted-foreground">
                {doneToday >= 5
                  ? <>Done today: <span className="text-emerald-500 font-medium">{doneToday} ✓</span> — solid run.</>
                  : <>Done today: <span className="text-emerald-500 font-medium">{doneToday} ✓</span></>}
              </span>
            )}
          </div>
        )}

        {/* Discoverable quick actions for the update path. On desktop
            they sit as inline chips. On mobile they're tucked behind a
            single "Smart tools" toggle so they're reachable without
            eating the first viewport. Both surfaces dispatch through
            `runPreset`. */}
        {!magicResult && (
          <>
            <div className="hidden md:flex mb-4 flex-wrap gap-1.5">
              {SMART_PRESETS.map(p => (
                <button
                  key={p.q}
                  type="button"
                  onClick={() => runPreset(p.q)}
                  className="text-[11px] px-2.5 py-1 rounded-full bg-card border border-border text-muted-foreground hover:text-foreground hover:border-violet-500/30 transition flex items-center gap-1"
                  disabled={magicLoading}
                >
                  <Sparkles className="w-3 h-3 text-violet-500" />
                  {p.label}
                </button>
              ))}
            </div>
            <div className="md:hidden mb-3">
              <button
                type="button"
                onClick={() => setMobileToolsOpen(o => !o)}
                aria-expanded={mobileToolsOpen}
                aria-controls="mobile-smart-tools"
                className="w-full h-10 px-3 rounded-xl bg-card border border-border text-sm text-foreground hover:border-violet-500/30 transition flex items-center justify-between gap-2"
              >
                <span className="flex items-center gap-1.5">
                  <Sparkles className="w-3.5 h-3.5 text-violet-500" />
                  Smart tools
                </span>
                <ChevronDown className={cn(
                  "w-4 h-4 text-muted-foreground transition",
                  mobileToolsOpen && "rotate-180",
                )} />
              </button>
              {mobileToolsOpen && (
                <div
                  id="mobile-smart-tools"
                  className="mt-2 flex flex-col gap-1.5 panel-enter"
                >
                  {SMART_PRESETS.map(p => (
                    <button
                      key={p.q}
                      type="button"
                      onClick={() => {
                        setMobileToolsOpen(false);
                        runPreset(p.q);
                      }}
                      disabled={magicLoading}
                      className="text-xs h-10 px-3 rounded-lg bg-card border border-border text-left text-foreground hover:border-violet-500/30 transition flex items-center gap-2 disabled:opacity-50"
                    >
                      <Sparkles className="w-3.5 h-3.5 text-violet-500 shrink-0" />
                      {p.label}
                    </button>
                  ))}
                </div>
              )}
            </div>
          </>
        )}

        {magicResult && magicResult.mode === "view" && (
          <div className="mb-4 px-3 py-2 rounded-lg bg-violet-500/[0.06] border border-violet-500/20 text-xs panel-enter">
            <span className="font-medium text-violet-500">Yorik:</span>{" "}
            <span className="text-foreground">{magicResult.summary}</span>
          </div>
        )}

        {magicResult && magicResult.mode === "update" && (
          <UpdateConfirmPanel
            result={magicResult}
            tasksById={new Map(tasks.map(t => [t.id, t]))}
            applying={applying}
            onApply={applyUpdates}
            onCancel={clearMagic}
          />
        )}

        {/* View tabs (Today / Inbox / Upcoming / All) + Show done +
            grouping selector. Replaces the old Open/Done/All filter
            with the Things-3-style view model. */}
        <div className="flex flex-wrap gap-2 mb-4 items-center">
          <div className="flex gap-1.5 mr-auto flex-wrap">
            <ViewTab icon={<Sun className="w-3 h-3" />}
                     label="Today"    count={viewCounts.today}
                     danger={viewCounts.overdue}
                     dangerTitle={`${viewCounts.overdue} overdue`}
                     active={view === "today"}    onClick={() => setView("today")} />
            <ViewTab icon={<InboxIcon className="w-3 h-3" />}
                     label="Inbox"    count={viewCounts.inbox}
                     active={view === "inbox"}    onClick={() => setView("inbox")} />
            <ViewTab icon={<CalendarRange className="w-3 h-3" />}
                     label="Upcoming" count={viewCounts.upcoming}
                     active={view === "upcoming"} onClick={() => setView("upcoming")} />
            <ViewTab icon={<Layers className="w-3 h-3" />}
                     label="All"      count={viewCounts.all}
                     active={view === "all"}      onClick={() => setView("all")} />
          </div>
          <button
            onClick={() => setShowDone(s => !s)}
            className={cn(
              "text-xs h-10 md:h-7 px-2.5 rounded-full border transition flex items-center gap-1",
              showDone
                ? "bg-emerald-500/10 border-emerald-500/30 text-emerald-700 dark:text-emerald-400"
                : "bg-card border-border text-muted-foreground hover:text-foreground",
            )}
            title={showDone ? "Hide completed tasks" : "Show completed tasks"}
            aria-label={showDone ? "Hide completed tasks" : "Show completed tasks"}
            aria-pressed={showDone}
          >
            {showDone ? <EyeOff className="w-3 h-3" /> : <Eye className="w-3 h-3" />}
            Done ({doneCount})
          </button>
          {!magicResult && (
            <div className="relative">
              <select
                value={grouping}
                onChange={e => setGrouping(e.target.value as GroupMode)}
                aria-label="Group tasks by"
                className="text-xs h-10 md:h-7 pl-3 pr-8 rounded-full bg-card border border-border text-muted-foreground hover:text-foreground focus:outline-none appearance-none cursor-pointer"
              >
                <option value="due">Group · by due date</option>
                <option value="category">Group · by category</option>
                <option value="person">Group · by person</option>
                <option value="flat">Group · none (flat)</option>
              </select>
              <ChevronDown className="w-3 h-3 absolute right-2 top-1/2 -translate-y-1/2 pointer-events-none text-muted-foreground" />
            </div>
          )}
        </div>

        {/* Grouped list — masonry-style auto-flow grid so groups
            distribute across columns instead of one tall middle column. */}
        <div className="flex-1">
          {tasksApi.loading && tasks.length === 0 && (
            <div className="flex items-center justify-center py-12 text-muted-foreground">
              <Loader2 className="w-4 h-4 animate-spin mr-2" /> Loading…
            </div>
          )}
          {!tasksApi.loading && renderedGroups.length === 0 && (
            <div className="text-center py-12 text-muted-foreground text-sm">
              <div className="italic">
                {magicResult ? "Magic search returned nothing — try different wording." :
                 view === "today"    ? "Nothing on your plate today." :
                 view === "inbox"    ? "Inbox empty — type a task below to capture." :
                 view === "upcoming" ? "No upcoming tasks scheduled." :
                                       "You haven't added any tasks yet."}
              </div>
              {view === "today" && !magicResult && (
                <button
                  onClick={() => setView("all")}
                  className="mt-3 inline-flex items-center gap-1.5 text-[11px] px-3 py-1.5 rounded-md bg-muted/40 hover:bg-muted text-muted-foreground hover:text-foreground transition"
                >
                  View all tasks
                </button>
              )}
            </div>
          )}
          <div
            className="grid gap-x-5 gap-y-6"
            style={{ gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))" }}
          >
            {renderedGroups.map((g, gi) => (
              <div
                key={g.label}
                className="task-group-enter"
                style={{ animationDelay: `${gi * 60}ms` }}
              >
                <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-1.5 px-1 flex items-center justify-between">
                  <span>{g.label}</span>
                  <span className="opacity-60">{g.tasks.length}{groupEstimate(g.tasks)}</span>
                </div>
                <div className="space-y-1">
                  {g.tasks.map((t, ti) => (
                    <div
                      key={t.id}
                      data-task-id={t.id}
                      className={cn(
                        "task-row-enter",
                        recentlyUpdated.has(t.id) && "task-row-flash",
                        highlightTaskId === t.id && "ring-2 ring-violet-500/50 rounded-lg",
                      )}
                      style={{ animationDelay: `${gi * 60 + ti * 25}ms` }}
                    >
                      <TaskRow
                        task={t}
                        expanded={editingId === t.id}
                        onToggle={() => toggle(t)}
                        onRemove={() => remove(t)}
                        onClick={() => setEditingId(editingId === t.id ? null : t.id)}
                        onSaved={() => { tasksApi.refetch(); setEditingId(null); }}
                        onSnooze={(newDue) => snoozeTask(t, newDue)}
                        children_={childrenByParent.get(t.id) || []}
                        toggleChild={(child) => toggle(child)}
                        removeChild={(child) => remove(child)}
                        onAddSubtask={(title) => addSubtask(t, title)}
                      />
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>
      </main>

      <Dock activeAppId="tasks" />

      <style>{`
        /* Stagger fade-up for groups on mount and on grouping change.
           Each group's animation-delay is set inline based on index. */
        @keyframes taskGroupEnter {
          from { opacity: 0; transform: translateY(8px); }
          to   { opacity: 1; transform: translateY(0); }
        }
        .task-group-enter {
          animation: taskGroupEnter 320ms cubic-bezier(.2,.7,.2,1) both;
        }

        /* Individual rows ease in slightly after their group. The
           combined effect feels like the list dealing itself out. */
        @keyframes taskRowEnter {
          from { opacity: 0; transform: translateY(4px) scale(0.99); }
          to   { opacity: 1; transform: translateY(0) scale(1); }
        }
        .task-row-enter {
          animation: taskRowEnter 280ms cubic-bezier(.2,.7,.2,1) both;
        }

        /* "Yorik just touched this one" flash — short violet pulse so
           the user sees which rows changed after an Apply, then fades
           into the normal styling. */
        @keyframes taskRowFlash {
          0%   { box-shadow: 0 0 0 0 hsl(263 90% 65% / 0.5); background: hsl(263 90% 65% / 0.12); }
          60%  { box-shadow: 0 0 0 6px hsl(263 90% 65% / 0); background: hsl(263 90% 65% / 0.08); }
          100% { box-shadow: 0 0 0 0 hsl(263 90% 65% / 0); background: transparent; }
        }
        .task-row-flash > div {
          animation: taskRowFlash 2000ms ease-out both;
          border-radius: 0.5rem;
        }

        /* Magic-bar confirm panel slides + scales in so it doesn't
           just pop. Tied to the panel class added inside the JSX. */
        @keyframes panelEnter {
          from { opacity: 0; transform: translateY(-4px) scale(0.99); }
          to   { opacity: 1; transform: translateY(0) scale(1); }
        }
        .panel-enter {
          animation: panelEnter 240ms cubic-bezier(.2,.7,.2,1) both;
        }

        /* Pulsing sparkle for the magic bar while the LLM is thinking,
           applied conditionally in JSX. Continuous, subtle. */
        @keyframes magicGlow {
          0%, 100% { box-shadow: 0 0 0 0 hsl(263 90% 65% / 0); }
          50%      { box-shadow: 0 0 16px 2px hsl(263 90% 65% / 0.25); }
        }
        .magic-glow {
          animation: magicGlow 1600ms ease-in-out infinite;
        }

        @media (prefers-reduced-motion: reduce) {
          .task-group-enter, .task-row-enter,
          .panel-enter, .magic-glow {
            animation: none !important;
          }
          .task-row-flash > div { animation: none !important; }
        }
      `}</style>
    </div>
  );
}

// ────────────────────────── grouping helpers ─────────────────────────

function buildGroups(tasks: ExtendedTask[], mode: GroupMode): Array<{ label: string; tasks: ExtendedTask[] }> {
  if (mode === "flat") {
    return [{ label: "All", tasks: sortForFlat(tasks) }];
  }
  if (mode === "category") {
    return groupBy(tasks, t => t.category || "Uncategorised", labelOrder("category"));
  }
  if (mode === "person") {
    return groupBy(tasks, t => {
      const names = t.assignees?.map(a => a.name).join(", ");
      return names || t.person || "Unassigned";
    });
  }
  // due (default)
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const tomorrow = new Date(today); tomorrow.setDate(today.getDate() + 1);
  const weekEnd = new Date(today); weekEnd.setDate(today.getDate() + 7);

  return groupBy(tasks, t => {
    if (!t.due_date) return "No date";
    const d = new Date(t.due_date.slice(0, 10) + "T00:00:00");
    if (d < today) return "Overdue";
    if (d.getTime() === today.getTime()) return "Today";
    if (d.getTime() === tomorrow.getTime()) return "Tomorrow";
    if (d < weekEnd) return "This week";
    return "Later";
  }, ["Overdue", "Today", "Tomorrow", "This week", "Later", "No date"]);
}

function groupBy<T>(
  items: T[],
  key: (it: T) => string,
  preferredOrder: string[] = [],
): Array<{ label: string; tasks: T[] }> {
  const buckets = new Map<string, T[]>();
  for (const it of items) {
    const k = key(it);
    if (!buckets.has(k)) buckets.set(k, []);
    buckets.get(k)!.push(it);
  }
  const labels = Array.from(buckets.keys()).sort((a, b) => {
    const ai = preferredOrder.indexOf(a);
    const bi = preferredOrder.indexOf(b);
    if (ai === -1 && bi === -1) return a.localeCompare(b);
    if (ai === -1) return 1;
    if (bi === -1) return -1;
    return ai - bi;
  });
  return labels.map(l => ({ label: l, tasks: buckets.get(l)! }));
}

function labelOrder(mode: GroupMode): string[] {
  if (mode === "category") return ["Uncategorised"];
  return [];
}

function sortForFlat(tasks: ExtendedTask[]): ExtendedTask[] {
  return [...tasks].sort((a, b) => {
    const ap = b.priority ?? 1;
    const bp = a.priority ?? 1;
    if (ap !== bp) return ap - bp;  // high first
    const ad = a.due_date || "9999";
    const bd = b.due_date || "9999";
    return ad < bd ? -1 : ad > bd ? 1 : a.id - b.id;
  });
}

function groupEstimate(tasks: ExtendedTask[]): string {
  const m = tasks.reduce((acc, t) => acc + (t.estimated_minutes || 0), 0);
  if (!m) return "";
  return ` · ${formatMinutes(m)}`;
}

function formatMinutes(m: number): string {
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  const rem = m % 60;
  if (rem === 0) return `${h}h`;
  return `${h}h ${rem}m`;
}

// ─────────────────────────── view tabs ──────────────────────────
//
// Top-of-page filter that replaced Open / Done / All. Each tab pairs
// an icon with a count chip so the user can see at a glance where
// the open work lives.
function ViewTab({ icon, label, count, active, onClick, danger, dangerTitle }: {
  icon: React.ReactNode;
  label: string;
  count: number;
  active: boolean;
  onClick: () => void;
  /** Optional secondary count rendered as a small red pill — used by
   *  Today to surface "N overdue" without burying it inside the main
   *  count. Render is suppressed when zero. */
  danger?: number;
  dangerTitle?: string;
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        // h-10 on mobile clears Apple HIG's 44pt minimum touch target;
        // desktop keeps the compact h-7.
        "text-xs h-10 md:h-7 px-3 rounded-full border transition flex items-center gap-1.5",
        active
          ? "bg-primary text-primary-foreground border-primary"
          : "bg-card border-border text-muted-foreground hover:text-foreground",
      )}
    >
      {icon}
      {label}
      <span className={cn(
        "text-[10px] tabular-nums",
        active ? "opacity-80" : "opacity-60",
      )}>
        {count}
      </span>
      {danger != null && danger > 0 && (
        <span
          title={dangerTitle}
          className={cn(
            "text-[10px] tabular-nums px-1.5 py-0.5 rounded-full font-semibold leading-none",
            active
              ? "bg-white/20 text-white"
              : "bg-red-500/15 text-red-600 dark:text-red-400",
          )}
        >
          {danger}
        </span>
      )}
    </button>
  );
}


// ─── snooze helpers ─────────────────────────────────────────────
function isoDaysFromNow(n: number): string {
  const d = new Date();
  d.setDate(d.getDate() + n);
  return d.toISOString().slice(0, 10);
}
function isoNextWeekday(target: number): string {
  // target: 0=Sun..6=Sat. Always strictly forward (≥1 day ahead).
  const d = new Date();
  const cur = d.getDay();
  let delta = (target - cur + 7) % 7;
  if (delta === 0) delta = 7;
  d.setDate(d.getDate() + delta);
  return d.toISOString().slice(0, 10);
}

function SnoozeChip({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={(e) => { e.stopPropagation(); onClick(); }}
      className="text-[11px] md:text-[10px] px-2 py-1 md:px-1.5 md:py-0.5 rounded bg-muted/60 hover:bg-violet-500/15 hover:text-violet-500 text-muted-foreground transition"
      title="Snooze: push the due date forward"
      aria-label={`Snooze to ${label}`}
    >
      {label}
    </button>
  );
}


// ─────────────────────────── row ────────────────────────────────

function TaskRow({
  task, expanded, onToggle, onRemove, onClick, onSaved,
  onSnooze, children_, toggleChild, removeChild, onAddSubtask,
}: {
  task: ExtendedTask;
  expanded: boolean;
  onToggle: () => void;
  onRemove: () => void;
  onClick: () => void;
  onSaved: () => void;
  /** Patch the task's due_date forward to the given ISO date. */
  onSnooze?: (isoDate: string) => void;
  /** Subtasks (already sorted) — rendered nested below the main row. */
  children_?: ExtendedTask[];
  toggleChild?: (child: ExtendedTask) => void;
  removeChild?: (child: ExtendedTask) => void;
  /** Create a new child under this task. Surfaced inside the inline
   *  editor as "+ Add subtask". */
  onAddSubtask?: (title: string) => Promise<void> | void;
}) {
  const dueOverdue = task.due_date && !task.done
    ? new Date(task.due_date) < new Date(new Date().toISOString().slice(0, 10))
    : false;

  const priority = task.priority ?? 1;
  const kids = children_ || [];
  const kidsOpen = kids.filter(c => !c.done).length;
  const kidsDone = kids.filter(c => !!c.done).length;
  const kidsTotal = kids.length;
  const [childrenOpen, setChildrenOpen] = useState(kidsOpen > 0);

  return (
    <div className={cn(
      "border rounded-lg bg-card transition",
      expanded ? "border-primary/40 shadow-sm" : "border-border hover:border-foreground/15",
    )}>
      <div className="flex items-center gap-3 p-3 group">
        {/* Toggle: hit area expanded to 44px on mobile via padding +
            min-w/min-h, while the visual icon stays the same size.
            role/aria-checked make the custom button announce as a
            checkbox to screen readers (the visual is Lucide icons,
            not a native <input>). */}
        <button
          onClick={(e) => { e.stopPropagation(); onToggle(); }}
          className="shrink-0 inline-flex items-center justify-center min-w-[44px] min-h-[44px] md:min-w-0 md:min-h-0 -m-2 md:m-0 p-2 md:p-0 rounded"
          title={task.done ? "Mark as not done" : "Mark as done"}
          aria-label={task.done ? "Mark as not done" : "Mark as done"}
          role="checkbox"
          aria-checked={!!task.done}
        >
          {task.done
            ? <CheckSquare className="w-5 h-5 md:w-4 md:h-4 text-emerald-500" />
            : <Square className="w-5 h-5 md:w-4 md:h-4 text-muted-foreground hover:text-foreground" />}
        </button>
        <button
          onClick={onClick}
          className="flex-1 text-left min-w-0"
        >
          <div className="flex items-center gap-2">
            {priority === 2 && (
              <Flag className="w-3 h-3 text-red-500 shrink-0" />
            )}
            {priority === 0 && (
              <Flag className="w-3 h-3 text-muted-foreground/40 shrink-0" />
            )}
            <span className={cn(
              "text-sm truncate",
              task.done && "line-through text-muted-foreground",
            )}>
              {task.title}
            </span>
          </div>
          {/* Mobile shows only the most important chips so the meta
              line stays single-row: due date (with overdue color),
              priority flag is already inline above, subtask count.
              The rest (recurrence, estimate, category, assignees)
              show on md+ and inside the expanded editor on mobile. */}
          <div className="text-[11px] text-muted-foreground flex items-center gap-2 mt-0.5 flex-wrap">
            {task.due_date && (
              <span className={cn(
                "flex items-center gap-1",
                dueOverdue && "text-red-500 font-medium",
              )}>
                <CalendarDays className="w-3 h-3" /> {formatDue(task.due_date)}
              </span>
            )}
            {task.recurrence_rule && (
              <span
                className="hidden md:flex items-center gap-1 text-violet-500"
                title={`Recurring: ${task.recurrence_rule}`}
              >
                <Repeat className="w-3 h-3" /> {task.recurrence_rule}
              </span>
            )}
            {task.estimated_minutes && (
              <span className="hidden md:flex items-center gap-1">
                <Clock className="w-3 h-3" /> {formatMinutes(task.estimated_minutes)}
              </span>
            )}
            {task.category && <span className="opacity-60">#{task.category}</span>}
            {kidsTotal > 0 && (
              <span
                className="opacity-60 flex items-center gap-1"
                title={`${kidsDone} of ${kidsTotal} subtasks done`}
              >
                <ListTodo className="w-3 h-3" /> {kidsDone}/{kidsTotal}
              </span>
            )}
            {task.assignees && task.assignees.length > 0 && (
              <span className="hidden md:inline opacity-60">{task.assignees.map(a => a.name).join(", ")}</span>
            )}
          </div>
        </button>

        {/* Snooze chips. Mobile: always visible (no hover on touch
            devices, so hover-only would be unreachable). Desktop:
            hover-only as before to keep the resting row uncluttered. */}
        {onSnooze && !task.done && (
          <div className="flex md:hidden items-center gap-1 shrink-0">
            <SnoozeChip label="+1d"  onClick={() => onSnooze(isoDaysFromNow(1))} />
            <SnoozeChip label="+1w"  onClick={() => onSnooze(isoDaysFromNow(7))} />
          </div>
        )}
        {onSnooze && !task.done && (
          <div className="hidden md:group-hover:flex items-center gap-1 shrink-0">
            <SnoozeChip label="+1d" onClick={() => onSnooze(isoDaysFromNow(1))} />
            <SnoozeChip label="Next Mon" onClick={() => onSnooze(isoNextWeekday(1))} />
            <SnoozeChip label="+1w" onClick={() => onSnooze(isoDaysFromNow(7))} />
          </div>
        )}
        {/* Trash hidden on mobile — accidental flicks on a dense list
            were deleting tasks. Delete is reachable inside the
            expanded editor (TaskInlineEditor's new "Delete" button)
            via tap-to-expand. Desktop keeps the always-visible
            trash since hover gives natural targeting affordance. */}
        <button
          onClick={(e) => { e.stopPropagation(); onRemove(); }}
          className="hidden md:inline-flex shrink-0 p-1.5 rounded-md text-muted-foreground hover:text-red-500 hover:bg-red-500/10 transition"
          title="Delete task"
          aria-label="Delete task"
        >
          <Trash2 className="w-3.5 h-3.5" />
        </button>
        <button
          onClick={(e) => { e.stopPropagation(); onClick(); }}
          className="shrink-0 inline-flex items-center justify-center min-w-[44px] min-h-[44px] md:min-w-0 md:min-h-0 -m-2 md:m-0 p-2 md:p-0 rounded text-muted-foreground hover:text-foreground"
          title={expanded ? "Collapse" : "Expand"}
          aria-label={expanded ? "Collapse task" : "Expand task"}
          aria-expanded={expanded}
        >
          <ChevronRight className={cn(
            "w-3.5 h-3.5 transition",
            expanded && "rotate-90",
          )} />
        </button>
      </div>

      {expanded && (
        <TaskInlineEditor
          task={task}
          onCancel={onClick}
          onSaved={onSaved}
          onDelete={onRemove}
          onAddSubtask={onAddSubtask}
        />
      )}

      {/* Subtasks — indented, with their own checkbox + delete. Click
          the (N/M) badge above to expand/collapse. */}
      {kidsTotal > 0 && (
        <div className="border-t border-border/60 px-3 py-1.5">
          <button
            type="button"
            onClick={() => setChildrenOpen(o => !o)}
            className="text-[10px] uppercase tracking-wider text-muted-foreground hover:text-foreground flex items-center gap-1 mb-1"
          >
            <ChevronDown className={cn(
              "w-2.5 h-2.5 transition",
              childrenOpen ? "" : "-rotate-90",
            )} />
            {/* Aggregate the OPEN subtasks' estimates only — the user
                cares about remaining work, not what's already done.
                Reuses groupEstimate which prefixes a " · " and skips
                empty totals, so a parent with no estimates stays at
                "Subtasks · 2/5". */}
            Subtasks · {kidsDone}/{kidsTotal}{groupEstimate(kids.filter(c => !c.done))}
          </button>
          {childrenOpen && (
            // Bumped from pl-2 + 1px border to pl-4 + 2px coloured
            // border so nesting reads as nesting on small screens.
            <div className="space-y-1 pl-4 border-l-2 border-emerald-500/30">
              {kids.map(c => (
                <div key={c.id} className="flex items-center gap-2 py-1 group/c">
                  <button
                    onClick={() => toggleChild?.(c)}
                    className="shrink-0 inline-flex items-center justify-center min-w-[40px] min-h-[40px] md:min-w-0 md:min-h-0 -m-2 md:m-0 p-2 md:p-0 rounded"
                    title={c.done ? "Mark as not done" : "Mark as done"}
                    aria-label={c.done ? "Mark subtask as not done" : "Mark subtask as done"}
                    role="checkbox"
                    aria-checked={!!c.done}
                  >
                    {c.done
                      ? <CheckSquare className="w-4 h-4 md:w-3.5 md:h-3.5 text-emerald-500" />
                      : <Square className="w-4 h-4 md:w-3.5 md:h-3.5 text-muted-foreground hover:text-foreground" />}
                  </button>
                  <span className={cn(
                    "flex-1 text-xs truncate",
                    c.done && "line-through text-muted-foreground",
                  )}>
                    {c.title}
                  </span>
                  {c.estimated_minutes != null && c.estimated_minutes > 0 && (
                    <span
                      className="text-[11px] md:text-[10px] text-muted-foreground shrink-0 inline-flex items-center gap-0.5"
                      title={`Estimated ${formatMinutes(c.estimated_minutes)}`}
                    >
                      <Clock className="w-3 h-3 md:w-2.5 md:h-2.5" />
                      {formatMinutes(c.estimated_minutes)}
                    </span>
                  )}
                  {c.due_date && (
                    <span className="text-[11px] md:text-[10px] text-muted-foreground shrink-0">
                      {formatDue(c.due_date)}
                    </span>
                  )}
                  {/* Subtask delete: always visible on mobile (hover-
                      only is unreachable on touch), hover-only on
                      desktop to keep the resting list calm. */}
                  <button
                    onClick={() => removeChild?.(c)}
                    className="shrink-0 p-2 md:p-1 rounded text-muted-foreground hover:text-red-500 md:opacity-0 md:group-hover/c:opacity-100 transition"
                    title="Delete subtask"
                    aria-label="Delete subtask"
                  >
                    <Trash2 className="w-3.5 h-3.5 md:w-3 md:h-3" />
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function TaskInlineEditor({
  task, onCancel, onSaved, onDelete, onAddSubtask,
}: {
  task: ExtendedTask;
  onCancel: () => void;
  onSaved: () => void;
  /** Mobile users can't see the row's trash icon (hidden to avoid
   *  accidental flick-deletes), so the editor surfaces Delete here. */
  onDelete?: () => void;
  /** Create a subtask under the current task. When provided, the
   *  editor renders an inline "+ Add subtask" row at the bottom. */
  onAddSubtask?: (title: string) => Promise<void> | void;
}) {
  const [title, setTitle] = useState(task.title);
  const [dueDate, setDueDate] = useState(task.due_date?.slice(0, 10) || "");
  const [notes, setNotes] = useState(task.notes || "");
  const [category, setCategory] = useState(task.category || "");
  const [priority, setPriority] = useState(task.priority ?? 1);
  const [estimate, setEstimate] = useState(task.estimated_minutes?.toString() || "");
  const [recurrence, setRecurrence] = useState(task.recurrence_rule || "");
  const [saving, setSaving] = useState(false);

  async function save() {
    setSaving(true);
    try {
      const est = estimate.trim() ? parseInt(estimate.trim(), 10) : null;
      await api.patch(`/api/tasks/${task.id}?role=${ROLE}`, {
        title: title.trim(),
        due_date: dueDate || null,
        notes: notes || null,
        category: category || null,
        priority,
        estimated_minutes: est && est > 0 ? est : null,
        // Empty string clears the recurrence (PATCH handler normalises).
        recurrence_rule: recurrence.trim(),
      });
      onSaved();
    } catch (e: any) {
      alert(`Failed: ${e?.message || e}`);
    } finally { setSaving(false); }
  }

  return (
    <div className="border-t border-border/60 p-3 space-y-2 bg-muted/20">
      <input
        value={title}
        onChange={e => setTitle(e.target.value)}
        className="w-full h-8 px-2 text-sm bg-background border border-border rounded-md focus:outline-none focus:ring-1 focus:ring-ring/40"
      />
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
        <input
          type="date"
          value={dueDate}
          onChange={e => setDueDate(e.target.value)}
          // Click anywhere in the field (not just the tiny calendar
          // icon) opens the native picker. showPicker() must be called
          // from a user gesture; the try/catch swallows the
          // NotAllowedError thrown on browsers that don't support it.
          onClick={e => { try { (e.currentTarget as any).showPicker?.(); } catch {} }}
          className="h-8 px-2 text-xs bg-background border border-border rounded-md focus:outline-none cursor-pointer"
          title="Due date"
        />
        <input
          type="number"
          min={5} step={5}
          value={estimate}
          onChange={e => setEstimate(e.target.value)}
          placeholder="estimate min"
          className="h-8 px-2 text-xs bg-background border border-border rounded-md focus:outline-none"
          title="Estimated minutes"
        />
        <select
          value={priority}
          onChange={e => setPriority(parseInt(e.target.value, 10))}
          className="h-8 px-2 text-xs bg-background border border-border rounded-md focus:outline-none"
          title="Priority"
        >
          <option value={0}>Low</option>
          <option value={1}>Normal</option>
          <option value={2}>High</option>
        </select>
        <input
          value={category}
          onChange={e => setCategory(e.target.value)}
          placeholder="category"
          className="h-8 px-2 text-xs bg-background border border-border rounded-md focus:outline-none"
        />
      </div>
      <input
        value={notes}
        onChange={e => setNotes(e.target.value)}
        placeholder="Notes…"
        className="w-full h-8 px-2 text-xs bg-background border border-border rounded-md focus:outline-none"
      />
      {/* Recurrence — chips for the common cases (covers ~95% of
          household tasks per the parser grammar in
          backend/tasks_recurrence.py); a Custom… toggle reveals the
          free-form shorthand input for arbitrary rules like
          "every Mon,Wed,Fri" or "every 3 days". Clicking the active
          chip clears it. Empty = one-shot. */}
      <RepeatPicker value={recurrence} onChange={setRecurrence} />
      {onAddSubtask && (
        <SubtaskComposer onAdd={onAddSubtask} />
      )}
      <div className="flex items-center gap-2 pt-1">
        {/* Delete lives on the LEFT so it's spatially separated from
            the affirmative Save/Cancel on the right — pattern matches
            macOS Mail / Things / Outlook. Especially important on
            mobile where this is the ONLY reachable delete path
            (row-level trash is hidden on mobile to prevent
            accidental flick-deletes). */}
        {onDelete && (
          <button
            type="button"
            onClick={onDelete}
            className="text-xs h-9 px-3 rounded-md border border-rose-500/30 text-rose-600 hover:bg-rose-500/10 transition flex items-center gap-1 mr-auto"
            title="Delete task"
            aria-label="Delete task"
          >
            <Trash2 className="w-3.5 h-3.5" /> Delete
          </button>
        )}
        <button
          onClick={onCancel}
          className="text-xs h-9 px-3 rounded-md border border-border hover:bg-muted flex items-center gap-1"
        >
          <X className="w-3 h-3" /> Cancel
        </button>
        <button
          onClick={save}
          disabled={saving || !title.trim()}
          className="text-xs h-9 px-3 rounded-md bg-primary text-primary-foreground hover:opacity-90 disabled:opacity-50 flex items-center gap-1"
        >
          {saving ? <Loader2 className="w-3 h-3 animate-spin" /> : <Check className="w-3 h-3" />}
          Save
        </button>
      </div>
    </div>
  );
}

// Inline subtask add inside the parent's editor. Two states: a small
// "+ Add subtask" button at rest; on click swaps to a title input that
// keeps focus across submits so the user can capture several in a row
// (Things-style). Esc closes back to the rest state. The parent owns
// the POST + refetch via the `onAdd` callback.
function SubtaskComposer({ onAdd }: {
  onAdd: (title: string) => Promise<void> | void;
}) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [saving, setSaving] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  async function submit() {
    const trimmed = text.trim();
    if (!trimmed || saving) return;
    setSaving(true);
    try {
      await onAdd(trimmed);
      setText("");
      // Refocus for rapid multi-add. Parent re-renders after refetch;
      // the input is still mounted (open === true), so focus survives.
      requestAnimationFrame(() => inputRef.current?.focus());
    } catch {
      // onAdd already alerted; keep the text so the user can retry.
    } finally {
      setSaving(false);
    }
  }

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="text-[11px] h-7 px-2.5 rounded-full border border-dashed border-border text-muted-foreground hover:text-foreground hover:border-emerald-500/40 transition inline-flex items-center gap-1"
      >
        <Plus className="w-3 h-3" />
        Add subtask
      </button>
    );
  }

  return (
    <div className="flex items-center gap-2">
      <Plus className="w-3.5 h-3.5 text-emerald-500 shrink-0" aria-hidden="true" />
      <input
        ref={inputRef}
        value={text}
        onChange={e => setText(e.target.value)}
        onKeyDown={e => {
          if (e.key === "Enter") { e.preventDefault(); submit(); }
          if (e.key === "Escape") { e.preventDefault(); setOpen(false); setText(""); }
        }}
        placeholder="Subtask title — Enter to add, Esc to close"
        aria-label="New subtask title"
        disabled={saving}
        className="flex-1 h-8 px-2 text-xs bg-background border border-border rounded-md focus:outline-none focus:ring-1 focus:ring-emerald-500/40"
      />
      <button
        type="button"
        onClick={submit}
        disabled={saving || !text.trim()}
        className="text-xs h-8 px-3 rounded-md bg-emerald-500 text-white hover:opacity-90 disabled:opacity-50 flex items-center gap-1"
      >
        {saving ? <Loader2 className="w-3 h-3 animate-spin" /> : <Check className="w-3 h-3" />}
        Add
      </button>
      <button
        type="button"
        onClick={() => { setOpen(false); setText(""); }}
        disabled={saving}
        className="text-xs h-8 px-2 rounded-md border border-border hover:bg-muted text-muted-foreground disabled:opacity-50"
        aria-label="Close subtask composer"
      >
        <X className="w-3 h-3" />
      </button>
    </div>
  );
}

// Canonical strings the recurrence parser in
// `backend/tasks_recurrence.py` definitely accepts. Keep these strings
// in sync with that parser — they're matched case-insensitively against
// `tasks.recurrence_rule` when deciding which chip to highlight.
const REPEAT_PRESETS: Array<{ label: string; value: string }> = [
  { label: "Daily",         value: "daily" },
  { label: "Weekdays",      value: "every Mon,Tue,Wed,Thu,Fri" },
  { label: "Weekly",        value: "weekly" },
  { label: "Every 2 weeks", value: "every 2 weeks" },
  { label: "Monthly",       value: "monthly" },
  { label: "Yearly",        value: "yearly" },
];

function matchedPreset(rule: string): string | null {
  const k = rule.trim().toLowerCase();
  if (!k) return null;
  const hit = REPEAT_PRESETS.find(p => p.value.toLowerCase() === k);
  return hit ? hit.value : null;
}

function RepeatPicker({
  value, onChange,
}: {
  value: string;
  onChange: (v: string) => void;
}) {
  const preset = matchedPreset(value);
  const isCustom = value.trim() !== "" && preset === null;
  // Custom field is auto-open when the existing rule isn't a preset
  // (so an already-saved "every Mon,Wed,Fri" stays editable). User can
  // also open it explicitly via the Custom… chip.
  const [showCustom, setShowCustom] = useState(isCustom);

  const pick = (v: string) => {
    // Re-clicking the active preset clears the recurrence — common
    // toggle-chip behavior, no separate "None" chip needed.
    if (preset === v) {
      onChange("");
    } else {
      onChange(v);
    }
    setShowCustom(false);
  };

  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap gap-1.5 items-center">
        <span className="text-[10px] uppercase tracking-wider text-muted-foreground mr-1 flex items-center gap-1">
          <Repeat className="w-3 h-3" /> Repeat
        </span>
        {REPEAT_PRESETS.map(p => {
          const active = preset === p.value;
          return (
            <button
              key={p.value}
              type="button"
              onClick={() => pick(p.value)}
              aria-pressed={active}
              className={cn(
                "text-xs h-9 md:h-7 px-2.5 rounded-full border transition",
                active
                  ? "bg-violet-500/15 border-violet-500/40 text-violet-600 dark:text-violet-300"
                  : "bg-card border-border text-muted-foreground hover:text-foreground hover:border-violet-500/30",
              )}
            >
              {p.label}
            </button>
          );
        })}
        <button
          type="button"
          onClick={() => {
            // Toggle: opening doesn't change the value, closing leaves it
            // alone too. To clear, the user picks a preset or empties the
            // field manually.
            setShowCustom(s => !s);
          }}
          aria-pressed={showCustom || isCustom}
          className={cn(
            "text-xs h-9 md:h-7 px-2.5 rounded-full border transition",
            (showCustom || isCustom)
              ? "bg-violet-500/15 border-violet-500/40 text-violet-600 dark:text-violet-300"
              : "bg-card border-border text-muted-foreground hover:text-foreground hover:border-violet-500/30",
          )}
        >
          Custom…
        </button>
        {value.trim() !== "" && (
          <button
            type="button"
            onClick={() => { onChange(""); setShowCustom(false); }}
            title="Clear recurrence (one-shot)"
            aria-label="Clear recurrence"
            className="text-xs h-9 md:h-7 px-2 rounded-full text-muted-foreground hover:text-foreground"
          >
            <X className="w-3 h-3" />
          </button>
        )}
      </div>
      {(showCustom || isCustom) && (
        <input
          value={value}
          onChange={e => onChange(e.target.value)}
          placeholder='e.g. "every Mon,Wed,Fri", "every 3 days", "quarterly"'
          className="w-full h-8 px-2 text-xs bg-background border border-border rounded-md focus:outline-none focus:ring-1 focus:ring-ring/40"
          title="Recurrence rule (empty = one-shot)"
        />
      )}
    </div>
  );
}

function UpdateConfirmPanel({
  result, tasksById, applying, onApply, onCancel,
}: {
  result: AskResponse;
  tasksById: Map<number, ExtendedTask>;
  applying: boolean;
  onApply: (updates: ProposedUpdate[]) => void;
  onCancel: () => void;
}) {
  const items = result.updates
    .map(u => ({ task: tasksById.get(u.id), changes: u.changes }))
    .filter(it => !!it.task) as Array<{ task: ExtendedTask; changes: ProposedUpdate["changes"] }>;

  // Per-row local edits for estimated_minutes. The LLM's suggestion is
  // the starting value; the user can tweak any of them before hitting
  // Apply (either per-row or "Apply all"). Stored as a string so the
  // input can be empty mid-edit without coercing to 0.
  const [editedMins, setEditedMins] = useState<Map<number, string>>(new Map());
  useEffect(() => {
    const m = new Map<number, string>();
    for (const u of result.updates) {
      if (u.changes.estimated_minutes != null) {
        m.set(u.id, String(u.changes.estimated_minutes));
      }
    }
    setEditedMins(m);
  }, [result]);

  // Build the update for one row, substituting the user-edited minutes
  // if there is one. Returns null if the minutes field has been blanked
  // out and there's no other change to apply.
  const buildUpdate = (id: number, changes: ProposedUpdate["changes"]): ProposedUpdate | null => {
    const next: ProposedUpdate["changes"] = { ...changes };
    if (changes.estimated_minutes != null) {
      const raw = (editedMins.get(id) ?? "").trim();
      const parsed = parseInt(raw, 10);
      if (raw === "" || Number.isNaN(parsed) || parsed <= 0) {
        delete next.estimated_minutes;
      } else {
        next.estimated_minutes = parsed;
      }
    }
    if (
      next.estimated_minutes == null &&
      next.priority == null &&
      !next.category
    ) {
      return null;
    }
    return { id, changes: next };
  };

  const applyOne = (id: number, changes: ProposedUpdate["changes"]) => {
    const u = buildUpdate(id, changes);
    if (!u) return;
    onApply([u]);
  };

  const applyAll = () => {
    const updates = items
      .map(({ task, changes }) => buildUpdate(task.id, changes))
      .filter((u): u is ProposedUpdate => u !== null);
    onApply(updates);
  };

  if (items.length === 0) {
    return (
      <div className="mb-4 px-3 py-2 rounded-lg bg-amber-500/[0.06] border border-amber-500/30 text-xs">
        <span className="font-medium text-amber-600">Yorik:</span>{" "}
        <span className="text-foreground">{result.summary}</span>
        <button onClick={onCancel} className="ml-2 underline">dismiss</button>
      </div>
    );
  }

  return (
    <div className="mb-4 rounded-xl bg-violet-500/[0.06] border border-violet-500/30 overflow-hidden panel-enter">
      <div className="px-3 py-2 border-b border-violet-500/20 text-xs flex items-center justify-between">
        <div>
          <span className="font-medium text-violet-500">Yorik proposes:</span>{" "}
          <span className="text-foreground">{result.summary}</span>
        </div>
        <span className="text-[10px] text-muted-foreground">{items.length} change{items.length !== 1 ? "s" : ""}</span>
      </div>
      <div className="max-h-80 overflow-y-auto divide-y divide-violet-500/10">
        {items.map(({ task, changes }) => {
          const rawMins = editedMins.get(task.id) ?? "";
          const parsedMins = parseInt(rawMins.trim(), 10);
          const minsValid = changes.estimated_minutes == null
            ? true
            : rawMins.trim() !== "" && !Number.isNaN(parsedMins) && parsedMins > 0;
          return (
            <div key={task.id} className="px-3 py-2 text-xs flex items-center gap-2">
              <span className="flex-1 truncate font-medium">{task.title}</span>
              <span className="flex items-center gap-1.5 text-[10px]">
                {changes.estimated_minutes != null && (
                  <span className="inline-flex items-center rounded bg-violet-500/15 text-violet-500 font-medium overflow-hidden">
                    <input
                      type="number"
                      min={1}
                      step={5}
                      inputMode="numeric"
                      value={rawMins}
                      onChange={e => {
                        const v = e.target.value;
                        setEditedMins(prev => {
                          const next = new Map(prev);
                          next.set(task.id, v);
                          return next;
                        });
                      }}
                      disabled={applying}
                      aria-label={`Estimated minutes for ${task.title}`}
                      className="w-12 px-1.5 py-0.5 bg-transparent text-right text-violet-500 font-medium focus:outline-none focus:bg-violet-500/10"
                    />
                    <span className="pr-1.5">m</span>
                  </span>
                )}
                {changes.priority != null && (
                  <span className={cn(
                    "px-1.5 py-0.5 rounded font-medium",
                    changes.priority === 2 ? "bg-red-500/15 text-red-500" :
                    changes.priority === 0 ? "bg-muted text-muted-foreground" :
                                             "bg-blue-500/15 text-blue-500",
                  )}>
                    {changes.priority === 2 ? "High" : changes.priority === 0 ? "Low" : "Normal"}
                  </span>
                )}
                {changes.category && (
                  <span className="px-1.5 py-0.5 rounded bg-emerald-500/15 text-emerald-500 font-medium">
                    #{changes.category}
                  </span>
                )}
              </span>
              <button
                onClick={() => applyOne(task.id, changes)}
                disabled={applying || !minsValid}
                title="Apply just this one"
                aria-label={`Apply update for ${task.title}`}
                className="text-[10px] px-2 py-1 rounded-md border border-violet-500/40 text-violet-500 hover:bg-violet-500/10 disabled:opacity-40 disabled:cursor-not-allowed inline-flex items-center gap-1"
              >
                <Check className="w-3 h-3" />
                Apply
              </button>
            </div>
          );
        })}
      </div>
      <div className="px-3 py-2 border-t border-violet-500/20 flex justify-end gap-2 bg-violet-500/[0.04]">
        <button
          onClick={onCancel}
          disabled={applying}
          className="text-xs px-3 py-1.5 rounded-md border border-border bg-card hover:bg-muted disabled:opacity-50"
        >
          Cancel
        </button>
        <button
          onClick={applyAll}
          disabled={applying}
          className="text-xs px-3 py-1.5 rounded-md bg-violet-500 text-white hover:opacity-90 disabled:opacity-50 flex items-center gap-1"
        >
          {applying
            ? <Loader2 className="w-3 h-3 animate-spin" />
            : <Check className="w-3 h-3" />}
          Apply all
        </button>
      </div>
    </div>
  );
}

function formatDue(due: string): string {
  // Tasks are date-granularity. Strict-mode this even if the field
  // accidentally contains an ISO timestamp from an older bad write
  // ("2026-06-03T17:00:00") — append-then-parse would otherwise yield
  // "2026-06-03T17:00:00T00:00:00" → Invalid Date in the cards.
  const datePart = (due || "").slice(0, 10);
  const d = new Date(datePart + "T00:00:00");
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const diff = Math.round((d.getTime() - today.getTime()) / (24 * 3600 * 1000));
  if (diff === 0) return "Today";
  if (diff === 1) return "Tomorrow";
  if (diff === -1) return "Yesterday";
  if (diff > 1 && diff < 7) return d.toLocaleDateString([], { weekday: "long" });
  return d.toLocaleDateString([], { day: "numeric", month: "short" });
}
