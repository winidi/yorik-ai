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
import { Trans, useTranslation } from "react-i18next";
import i18n from "@/i18n";
import { formatDate } from "@/i18n/format";
import {
  Plus, Trash2, Check, X, CalendarDays, Loader2, ChevronRight, Play, Pause,
  CheckSquare, Square, ListTodo, Sparkles, Clock, Flag,
  ChevronDown, Repeat, Inbox as InboxIcon, Sun, CalendarRange,
  Layers, Eye, EyeOff,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { Dock } from "@/components/Dock";
import { useAuth } from "@/components/AuthGate";
import type { Task } from "../calendar/types";
import { toast } from "@/components/Toast";
import { MobileTopBar } from "@/components/MobileShell";
import { announceFocusChange } from "@/components/FocusCard";
import { ListSkeleton } from "@/components/Skeleton";
import { TickMark } from "@/components/TickMark";
import { MemberStack } from "@/components/PersonAvatar";

const ROLE = "admin";

// Prompts behind the "Smart tools" presets (desktop chips + mobile
// sheet). Kept here so the wording stays in one place — the same query
// runs whichever surface dispatches it. Labels are catalogue keys
// (tasks.presets.*), resolved at render.
const SMART_PRESETS: Array<{ labelKey: string; q: string }> = [
  { labelKey: "tasks.presets.estimate",   q: "estimate durations for tasks that don't have one yet" },
  { labelKey: "tasks.presets.prioritise", q: "set priority on each task based on the title and due date" },
  { labelKey: "tasks.presets.categorise", q: "suggest a category for each uncategorised task" },
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
  /** ISO datetime when the active-timer started; null/absent = not running. */
  started_at?: string | null;
  /** Minutes of recorded work folded in on each stop/done-flip. */
  actual_minutes?: number | null;
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
  const { t } = useTranslation();
  const tasksApi = useApi<ExtendedTask[]>(`/api/tasks?role=${ROLE}`, []);
  // The focus card (on every screen) can finish or pause a task.
  const refetchTasks = useRef(tasksApi.refetch);
  refetchTasks.current = tasksApi.refetch;
  useEffect(() => {
    const on = () => { void refetchTasks.current(); };
    window.addEventListener("yorik:tasks-changed", on);
    return () => window.removeEventListener("yorik:tasks-changed", on);
  }, []);
  // The list is yours: tasks assigned to you, and unassigned ones you
  // created. What others shared with you, and what you handed to
  // someone else, shows with "Others" (off by default, kept per device).
  const auth = useAuth();
  const meId = String((auth.user as any)?.id || "");
  const [showOthers, setShowOthers] = useState<boolean>(() => {
    try { return localStorage.getItem("yorik_tasks_show_others") === "1"; } catch { return false; }
  });
  const allTasks = tasksApi.data || [];
  const isMine = useCallback((t: ExtendedTask) => {
    const who = (t.assignees || []).map(a => String(a.user_id));
    if (who.length) return who.includes(meId);
    return String((t as any).created_by_user_id || "") === meId || !(t as any).created_by_user_id;
  }, [meId]);
  const othersCount = useMemo(() => meId ? allTasks.filter(t => !isMine(t) && !t.done).length : 0, [allTasks, isMine, meId]);
  const tasks = useMemo(() => (showOthers || !meId) ? allTasks : allTasks.filter(isMine), [allTasks, showOthers, isMine, meId]);

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
        if (matches.length > 0) return [{ label: t("tasks.matches"), tasks: matches }];
      }
      return grouped;
    }

    return buildGroups(filteredByMode, grouping);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filteredByMode, grouping, magicResult, i18n.language]);

  const toggle = useCallback(async (t: ExtendedTask) => {
    try {
      await api.patch(`/api/tasks/${t.id}?role=${ROLE}`, { done: t.done ? 0 : 1 });
      if (t.started_at) announceFocusChange();
      tasksApi.refetch();
    } catch (e: any) { toast(i18n.t("tasks.errors.failed", { error: e?.message || e })); }
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
      toast(t("tasks.errors.addSubtask", { error: e?.message || e }));
      throw e;
    }
  }, [tasksApi]);

  const remove = useCallback(async (t: ExtendedTask) => {
    if (!confirm(i18n.t("tasks.confirmDelete", { title: t.title }))) return;
    try {
      await api.delete(`/api/tasks/${t.id}?role=${ROLE}`);
      tasksApi.refetch();
    } catch (e: any) { toast(i18n.t("tasks.errors.failed", { error: e?.message || e })); }
  }, [tasksApi]);

  // Snooze — patch due_date forward by the given ISO date. Used by
  // the snooze chips that surface on row hover.
  const snoozeTask = useCallback(async (t: ExtendedTask, newDue: string) => {
    try {
      await api.patch(`/api/tasks/${t.id}?role=${ROLE}`, { due_date: newDue });
      tasksApi.refetch();
    } catch (e: any) { toast(i18n.t("tasks.errors.snooze", { error: e?.message || e })); }
  }, [tasksApi]);

  // Long-press start / stop on the active-timer. Backend enforces the
  // single-active invariant (starting one task auto-stops any other
  // running task this user owns), so we don't need to manage that here.
  const toggleTimer = useCallback(async (t: ExtendedTask) => {
    try {
      const endpoint = t.started_at ? "stop" : "start";
      await api.post(`/api/tasks/${t.id}/${endpoint}?role=${ROLE}`);
      tasksApi.refetch();
      announceFocusChange();
    } catch (e: any) { toast(i18n.t("tasks.errors.timer", { error: e?.message || e })); }
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
      if (parsed.due_date) bits.push(t("tasks.understood.due", { date: parsed.due_date }));
      if (parsed.priority === 2) bits.push(t("tasks.understood.high"));
      if (parsed.priority === 0) bits.push(t("tasks.understood.low"));
      if (parsed.category) bits.push(`#${parsed.category}`);
      if (parsed.estimated_minutes) bits.push(`${parsed.estimated_minutes} min`);
      if (parsed.recurrence_rule) bits.push(`↻ ${parsed.recurrence_rule}`);
      if (bits.length) {
        setParseHint(t("tasks.understood.prefix") + bits.join(" · "));
        if (parseHintTimerRef.current) clearTimeout(parseHintTimerRef.current);
        parseHintTimerRef.current = window.setTimeout(() => setParseHint(null), 3500);
      } else {
        setParseHint(null);
      }

      setInputText("");
      tasksApi.refetch();
    } catch (e: any) { toast(t("tasks.errors.failed", { error: e?.message || e })); }
    finally { setAdding(false); }
  // eslint-disable-next-line react-hooks/exhaustive-deps
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
      toast(t("tasks.errors.magic", { error: e?.message || e }));
    } finally { setMagicLoading(false); }
  // eslint-disable-next-line react-hooks/exhaustive-deps
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
        toast(t("tasks.applied", { applied: r.applied, rejected: r.rejected }));
      }
    } catch (e: any) {
      toast(t("tasks.errors.apply", { error: e?.message || e }));
    } finally {
      setApplying(false);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
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
      toast(t("tasks.errors.magicShort", { error: e?.message || e }));
    } finally { setMagicLoading(false); }
  // eslint-disable-next-line react-hooks/exhaustive-deps
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
      {/* Phone: the shared app bar; the big icon and prose heading are
          desktop-only, the count line stays small. */}
      <MobileTopBar title={t("tasks.title")} />
      {/* pt: tighter on mobile so the heading + bar don't eat the first
          viewport. pb: reserves room for the dock + iOS home indicator
          (env safe-area). Used to also reserve ~88px for the sticky
          bottom composer; that's gone now (unified into the bar at the
          top), so the bottom padding can be tighter. */}
      <main className="flex-1 flex flex-col max-w-6xl mx-auto w-full px-4 sm:px-8 pt-3 md:pt-10 pb-[max(7rem,calc(env(safe-area-inset-bottom)+5rem))] md:pb-24">
        <header className="mb-4 md:mb-6">
          <div className="hidden md:flex items-center gap-3 mb-2">
            <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-emerald-500/30 to-teal-500/30 flex items-center justify-center shadow-md">
              <ListTodo className="w-5 h-5 text-emerald-500" />
            </div>
          </div>
          {openCount > 0 ? (
            // Mobile: compact "N open tasks" line — gets the first task
            // into the initial viewport faster on small phones. Desktop
            // keeps the full prose H1 unchanged.
            <h1 className="text-sm font-medium sm:text-3xl sm:font-semibold leading-tight">
              <span className="sm:hidden text-muted-foreground">
                <Trans i18nKey="tasks.header.openShort" count={openCount} components={{ n: <span className="text-emerald-500 font-semibold" /> }} />
              </span>
              <span className="hidden sm:inline">
                <Trans i18nKey="tasks.header.open" count={openCount} components={{ n: <span className="text-emerald-500" /> }} />
              </span>
            </h1>
          ) : (
            <h1 className="text-xl sm:text-3xl font-semibold leading-tight">{t("tasks.header.allClear")}</h1>
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
                  title={t("tasks.header.loadTitle", { time: formatMinutes(dailyLoad.openMin) })}
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
                  {t("tasks.header.today", { time: formatMinutes(dailyLoad.openMin) })}
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
                  title={t("tasks.header.estimateTitle")}
                >
                  {t("tasks.header.unestimated", { count: dailyLoad.unestimated })}
                  <Sparkles className="w-3 h-3 text-violet-500" />
                </button>
              )}
              {dailyLoad.doneMin > 0 && (
                <span
                  title={t("tasks.header.doneTitle", { time: formatMinutes(dailyLoad.doneMin) })}
                  className="inline-flex items-center gap-1 text-emerald-600 dark:text-emerald-400"
                >
                  <Check className="w-3 h-3" />
                  {t("tasks.header.done", { time: formatMinutes(dailyLoad.doneMin) })}
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
              ? <Trans i18nKey="tasks.header.about" values={{ time: formatMinutes(totalEstimate) }} components={{ n: <span className="text-foreground font-medium" /> }} />
              : <>{t("tasks.header.addEstimates")}</>}
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
            placeholder={t("tasks.bar.placeholder")}
            aria-label={t("tasks.bar.aria")}
            className="flex-1 min-w-0 h-11 md:h-9 px-2 bg-transparent text-sm focus:outline-none"
            disabled={magicLoading || adding}
          />
          {magicResult && (
            <button
              type="button"
              onClick={clearMagic}
              className="h-11 md:h-9 px-2 text-muted-foreground hover:text-foreground inline-flex items-center shrink-0"
              title={t("tasks.bar.clearMagic")}
              aria-label={t("tasks.bar.clearMagic")}
            >
              <X className="w-4 h-4" />
            </button>
          )}
          <button
            type="button"
            onClick={addTask}
            disabled={magicLoading || adding || !inputText.trim()}
            title={defaultAction === "add" ? t("tasks.bar.addEnter") : t("tasks.bar.add")}
            className={cn(
              "h-11 md:h-9 px-3 sm:px-4 rounded-md bg-primary text-primary-foreground text-sm font-semibold hover:opacity-90 disabled:opacity-50 flex items-center gap-1.5 shrink-0 transition",
              defaultAction === "add" && "ring-2 ring-primary/40",
            )}
          >
            {adding ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
            <span className="hidden sm:inline">{adding ? t("tasks.reading") : t("tasks.add")}</span>
            {defaultAction === "add" && !adding && (
              <span className="hidden md:inline text-2xs opacity-70" aria-hidden="true">↵</span>
            )}
          </button>
          <button
            type="button"
            onClick={runMagic}
            disabled={magicLoading || adding || !inputText.trim()}
            title={defaultAction === "ask" ? t("tasks.bar.askEnter") : t("tasks.bar.askTitle")}
            className={cn(
              "h-11 md:h-9 px-3 sm:px-4 rounded-md bg-violet-500 text-white text-sm font-medium hover:opacity-90 disabled:opacity-50 flex items-center gap-1.5 shrink-0 transition",
              defaultAction === "ask" && "ring-2 ring-violet-500/40",
            )}
          >
            {magicLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Sparkles className="w-4 h-4" />}
            <span className="hidden sm:inline">{t("tasks.bar.ask")}</span>
            {defaultAction === "ask" && !magicLoading && (
              <span className="hidden md:inline text-2xs opacity-70" aria-hidden="true">↵</span>
            )}
          </button>
        </form>

        {/* Transient feedback row under the bar. parseHint shows what
            the LLM extracted on Add ("Yorik understood: morgen · 30 min").
            doneToday is the trophy line that used to sit above the
            bottom composer. Both are auto-dismissed / data-driven. */}
        {(parseHint || doneToday >= 1) && (
          <div className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs px-1">
            {parseHint && (
              <span className="text-emerald-700 dark:text-emerald-400 inline-flex items-center gap-1">
                <Sparkles className="w-3 h-3" />
                {parseHint}
              </span>
            )}
            {doneToday >= 1 && (
              <span className="text-muted-foreground">
                {doneToday >= 5
                  ? <Trans i18nKey="tasks.bar.doneTodayRun" values={{ count: doneToday }} components={{ n: <span className="text-emerald-500 font-medium" /> }} />
                  : <Trans i18nKey="tasks.bar.doneToday" values={{ count: doneToday }} components={{ n: <span className="text-emerald-500 font-medium" /> }} />}
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
                  className="text-xs px-2.5 py-1 rounded-full bg-card border border-border text-muted-foreground hover:text-foreground hover:border-violet-500/30 transition flex items-center gap-1"
                  disabled={magicLoading}
                >
                  <Sparkles className="w-3 h-3 text-violet-500" />
                  {t(p.labelKey)}
                </button>
              ))}
            </div>
            <div className="md:hidden mb-2">
              <button
                type="button"
                onClick={() => setMobileToolsOpen(o => !o)}
                aria-expanded={mobileToolsOpen}
                aria-controls="mobile-smart-tools"
                className="h-8 px-3 rounded-full bg-card border border-border text-xs text-foreground hover:border-violet-500/30 transition inline-flex items-center gap-1.5"
              >
                <Sparkles className="w-3.5 h-3.5 text-violet-500" />
                {t("tasks.bar.smartTools")}
                <ChevronDown className={cn(
                  "w-3.5 h-3.5 text-muted-foreground transition",
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
                      {t(p.labelKey)}
                    </button>
                  ))}
                </div>
              )}
            </div>
          </>
        )}

        {magicResult && magicResult.mode === "view" && (
          <div className="mb-4 px-3 py-2 rounded-lg bg-violet-500/[0.06] border border-violet-500/20 text-xs panel-enter">
            <span className="font-medium text-violet-500">{t("tasks.bar.yorik")}</span>{" "}
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
        {/* On a phone everything above the first task took ~60 % of the
            screen (2026-09-29): the filters are one row that scrolls
            sideways there, and wrap freely on a desktop. */}
        <div className="flex flex-nowrap md:flex-wrap gap-2 mb-3 md:mb-4 items-center overflow-x-auto md:overflow-visible -mx-4 px-4 md:mx-0 md:px-0 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
          <div className="flex gap-1.5 mr-auto flex-nowrap md:flex-wrap shrink-0">
            <ViewTab icon={<Sun className="w-3 h-3" />}
                     label={t("tasks.tabs.today")}    count={viewCounts.today}
                     danger={viewCounts.overdue}
                     dangerTitle={t("tasks.tabs.overdue", { count: viewCounts.overdue })}
                     active={view === "today"}    onClick={() => setView("today")} />
            <ViewTab icon={<InboxIcon className="w-3 h-3" />}
                     label={t("tasks.tabs.inbox")}    count={viewCounts.inbox}
                     active={view === "inbox"}    onClick={() => setView("inbox")} />
            <ViewTab icon={<CalendarRange className="w-3 h-3" />}
                     label={t("tasks.tabs.upcoming")} count={viewCounts.upcoming}
                     active={view === "upcoming"} onClick={() => setView("upcoming")} />
            <ViewTab icon={<Layers className="w-3 h-3" />}
                     label={t("tasks.tabs.all")}      count={viewCounts.all}
                     active={view === "all"}      onClick={() => setView("all")} />
          </div>
          <button
            onClick={() => setShowDone(s => !s)}
            className={cn(
              "text-xs h-9 md:h-7 px-2.5 rounded-full border transition flex items-center gap-1 shrink-0 whitespace-nowrap",
              showDone
                ? "bg-emerald-500/10 border-emerald-500/30 text-emerald-700 dark:text-emerald-400"
                : "bg-card border-border text-muted-foreground hover:text-foreground",
            )}
            title={showDone ? t("tasks.tabs.hideDone") : t("tasks.tabs.showDone")}
            aria-label={showDone ? t("tasks.tabs.hideDone") : t("tasks.tabs.showDone")}
            aria-pressed={showDone}
          >
            {showDone ? <EyeOff className="w-3 h-3" /> : <Eye className="w-3 h-3" />}
            {t("tasks.tabs.done", { count: doneCount })}
          </button>
          {(othersCount > 0 || showOthers) && (
            <button
              onClick={() => setShowOthers(v => { try { localStorage.setItem("yorik_tasks_show_others", v ? "0" : "1"); } catch {} return !v; })}
              className={cn(
                "text-xs h-9 md:h-7 px-2.5 rounded-full border transition flex items-center gap-1 shrink-0 whitespace-nowrap",
                showOthers
                  ? "bg-violet-500/10 border-violet-500/30 text-violet-700 dark:text-violet-300"
                  : "bg-card border-border text-muted-foreground hover:text-foreground",
              )}
              title={showOthers ? t("tasks.tabs.onlyMine") : t("tasks.tabs.alsoOthers")}
              aria-pressed={showOthers}
            >
              {showOthers ? <EyeOff className="w-3 h-3" /> : <Eye className="w-3 h-3" />}
              {t("tasks.tabs.others", { count: othersCount })}
            </button>
          )}
          {!magicResult && (
            <div className="relative shrink-0">
              <select
                value={grouping}
                onChange={e => setGrouping(e.target.value as GroupMode)}
                aria-label={t("tasks.tabs.groupBy")}
                className="text-xs h-9 md:h-7 pl-3 pr-8 rounded-full bg-card border border-border text-muted-foreground hover:text-foreground focus:outline-none appearance-none cursor-pointer"
              >
                <option value="due">{t("tasks.tabs.group.due")}</option>
                <option value="category">{t("tasks.tabs.group.category")}</option>
                <option value="person">{t("tasks.tabs.group.person")}</option>
                <option value="flat">{t("tasks.tabs.group.flat")}</option>
              </select>
              <ChevronDown className="w-3 h-3 absolute right-2 top-1/2 -translate-y-1/2 pointer-events-none text-muted-foreground" />
            </div>
          )}
        </div>

        {/* Grouped list — masonry-style auto-flow grid so groups
            distribute across columns instead of one tall middle column. */}
        <div className="flex-1">
          {tasksApi.loading && tasks.length === 0 && (
            <ListSkeleton rows={4} />
          )}
          {!tasksApi.loading && renderedGroups.length === 0 && (
            <div className="text-center py-12 text-muted-foreground text-sm">
              <div className="italic">
                {magicResult ? t("tasks.empty.magic") :
                 view === "today"    ? t("tasks.empty.today") :
                 view === "inbox"    ? t("tasks.empty.inbox") :
                 view === "upcoming" ? t("tasks.empty.upcoming") :
                                       t("tasks.empty.all")}
              </div>
              {view === "today" && !magicResult && (
                <button
                  onClick={() => setView("all")}
                  className="mt-3 inline-flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-md bg-muted/40 hover:bg-muted text-muted-foreground hover:text-foreground transition"
                >
                  {t("tasks.empty.viewAll")}
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
                <div className="text-2xs text-muted-foreground font-semibold mb-1.5 px-1 flex items-center justify-between">
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
                        onToggleTimer={() => toggleTimer(t)}
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
  // Group labels are resolved here, at call time, so they follow the
  // person's language.
  const g = (k: string) => i18n.t(`tasks.groups.${k}`);
  if (mode === "flat") {
    return [{ label: g("all"), tasks: sortForFlat(tasks) }];
  }
  if (mode === "category") {
    return groupBy(tasks, t => t.category || g("uncategorised"), labelOrder("category"));
  }
  if (mode === "person") {
    return groupBy(tasks, t => {
      const names = t.assignees?.map(a => a.name).join(", ");
      return names || t.person || g("unassigned");
    });
  }
  // due (default)
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const tomorrow = new Date(today); tomorrow.setDate(today.getDate() + 1);
  const weekEnd = new Date(today); weekEnd.setDate(today.getDate() + 7);

  return groupBy(tasks, t => {
    if (!t.due_date) return g("noDate");
    const d = new Date(t.due_date.slice(0, 10) + "T00:00:00");
    if (d < today) return g("overdue");
    if (d.getTime() === today.getTime()) return g("today");
    if (d.getTime() === tomorrow.getTime()) return g("tomorrow");
    if (d < weekEnd) return g("thisWeek");
    return g("later");
  }, [g("overdue"), g("today"), g("tomorrow"), g("thisWeek"), g("later"), g("noDate")]);
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
  if (mode === "category") return [i18n.t("tasks.groups.uncategorised")];
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
        // h-9 on mobile is close to Apple HIG's 44pt minimum touch target;
        // desktop keeps the compact h-7.
        "text-xs h-9 md:h-7 px-3 rounded-full border transition flex items-center gap-1.5 shrink-0 whitespace-nowrap",
        active
          ? "bg-primary text-primary-foreground border-primary"
          : "bg-card border-border text-muted-foreground hover:text-foreground",
      )}
    >
      {icon}
      {label}
      <span className={cn(
        "text-2xs tabular-nums",
        active ? "opacity-80" : "opacity-60",
      )}>
        {count}
      </span>
      {danger != null && danger > 0 && (
        <span
          title={dangerTitle}
          className={cn(
            "text-2xs tabular-nums px-1.5 py-0.5 rounded-full font-semibold leading-none",
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
  const { t } = useTranslation();
  return (
    <button
      type="button"
      onClick={(e) => { e.stopPropagation(); onClick(); }}
      className="text-xs md:text-2xs px-2 py-1 md:px-1.5 md:py-0.5 rounded bg-muted/60 hover:bg-violet-500/15 hover:text-violet-500 text-muted-foreground transition"
      title={t("tasks.snooze.title")}
      aria-label={t("tasks.snooze.aria", { label })}
    >
      {label}
    </button>
  );
}


// ─────────────────────────── row ────────────────────────────────

function TaskRow({
  task, expanded, onToggle, onRemove, onClick, onSaved,
  onSnooze, onToggleTimer, children_, toggleChild, removeChild, onAddSubtask,
}: {
  task: ExtendedTask;
  expanded: boolean;
  onToggle: () => void;
  onRemove: () => void;
  onClick: () => void;
  onSaved: () => void;
  /** Patch the task's due_date forward to the given ISO date. */
  onSnooze?: (isoDate: string) => void;
  /** Long-press handler: start the timer if idle, stop if running. */
  onToggleTimer?: () => void;
  /** Subtasks (already sorted) — rendered nested below the main row. */
  children_?: ExtendedTask[];
  toggleChild?: (child: ExtendedTask) => void;
  removeChild?: (child: ExtendedTask) => void;
  /** Create a new child under this task. Surfaced inside the inline
   *  editor as "+ Add subtask". */
  onAddSubtask?: (title: string) => Promise<void> | void;
}) {
  const { t } = useTranslation();
  const dueOverdue = task.due_date && !task.done
    ? new Date(task.due_date) < new Date(new Date().toISOString().slice(0, 10))
    : false;

  const priority = task.priority ?? 1;
  const kids = children_ || [];
  const kidsOpen = kids.filter(c => !c.done).length;
  const kidsDone = kids.filter(c => !!c.done).length;
  const kidsTotal = kids.length;
  const [childrenOpen, setChildrenOpen] = useState(kidsOpen > 0);

  // Active-timer tick. Only ticks while running, so an idle list of
  // 200 rows costs zero intervals.
  const running = !!task.started_at;
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    if (!running) return;
    const id = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => clearInterval(id);
  }, [running]);
  // SQLite/Postgres both store started_at as 'YYYY-MM-DD HH:MM:SS' (naive
  // UTC). Browser's Date() reads that as LOCAL time, which would show a
  // huge bogus offset; append 'Z' so it's parsed as UTC.
  const startedMs = task.started_at
    ? new Date(/[zZ]|[+-]\d{2}:?\d{2}$/.test(task.started_at) ? task.started_at : task.started_at.replace(" ", "T") + "Z").getTime()
    : 0;
  const liveMin = running ? Math.max(0, (nowMs - startedMs) / 60000) : 0;
  // Accumulated prior work (folded in on each /stop and on done-flip)
  // resumes the bar from where it left off instead of restarting at 0.
  const priorMin = task.actual_minutes ?? 0;
  const displayMin = priorMin + liveMin;
  const estMin = task.estimated_minutes ?? 0;
  const progress = estMin > 0 ? displayMin / estMin : 0;         // 0..∞
  // Visual floor: 10% so the bar is visible the instant the user
  // long-presses, even when 0 minutes have elapsed. Without this
  // floor, activation looks like nothing happened until ~3min of a
  // 30min task ticks past.
  const fillPct = Math.max(10, Math.min(progress, 1) * 100);
  // Color shifts as work runs into / past the estimate. Stays calm —
  // /15 fill + /40 border keeps it editorial, not alarming.
  const phase: "under" | "over" | "way-over" =
    progress < 1 ? "under" : progress < 1.5 ? "over" : "way-over";
  const fillClass = !running ? ""
    : phase === "under"    ? "bg-emerald-500/15"
    : phase === "over"     ? "bg-amber-500/15"
    :                        "bg-rose-500/15";
  const borderRunning = !running ? ""
    : phase === "under"    ? "border-emerald-500/40"
    : phase === "over"     ? "border-amber-500/50"
    :                        "border-rose-500/50";
  // Done tasks render the same bar at lower opacity so the list
  // becomes a glance-able post-mortem: which estimates ran long. No
  // floor here — a done task that actually took 0 minutes shouldn't
  // pretend to have substance.
  const showDoneBar = !!task.done && priorMin > 0 && estMin > 0 && !running;
  const doneFillPct = showDoneBar ? Math.min(progress, 1) * 100 : 0;
  const doneFillClass = !showDoneBar ? ""
    : phase === "under"    ? "bg-emerald-500/10"
    : phase === "over"     ? "bg-amber-500/10"
    :                        "bg-rose-500/10";

  // Long-press → toggle timer. Suppress the click that fires on
  // pointerup so we don't ALSO expand the editor. Cancels if the
  // pointer moves > 10px (treats it as a scroll-start, not a press).
  const lpRef = useRef<{ timer: number | null; fired: boolean; x: number; y: number }>(
    { timer: null, fired: false, x: 0, y: 0 }
  );
  const lpCancel = () => {
    if (lpRef.current.timer) { clearTimeout(lpRef.current.timer); lpRef.current.timer = null; }
  };
  const lpDown = (e: React.PointerEvent) => {
    if (!onToggleTimer) return;
    lpRef.current.fired = false;
    lpRef.current.x = e.clientX;
    lpRef.current.y = e.clientY;
    lpRef.current.timer = window.setTimeout(() => {
      lpRef.current.fired = true;
      lpRef.current.timer = null;
      try { (navigator as any).vibrate?.(15); } catch { /* not supported */ }
      onToggleTimer();
    }, 500);
  };
  const lpMove = (e: React.PointerEvent) => {
    if (!lpRef.current.timer) return;
    if (Math.abs(e.clientX - lpRef.current.x) > 10 || Math.abs(e.clientY - lpRef.current.y) > 10) {
      lpCancel();
    }
  };
  const clickGuarded = (e: React.MouseEvent) => {
    if (lpRef.current.fired) {
      lpRef.current.fired = false;
      e.preventDefault();
      return;
    }
    onClick();
  };

  return (
    <div className={cn(
      "relative overflow-hidden border rounded-lg bg-card transition",
      expanded ? "border-primary/40 shadow-sm" : (running ? borderRunning : "border-border hover:border-foreground/15"),
    )}>
      {/* Progress fill. Tasks with an estimate get a filling bar; tasks
          without one get a thin emerald hairline on the left edge so
          "running but no estimate" still reads at a glance. */}
      {running && estMin > 0 && (
        <div
          className={cn("absolute inset-y-0 left-0 pointer-events-none transition-all duration-1000 ease-linear", fillClass)}
          style={{ width: `${fillPct}%` }}
          aria-hidden
        />
      )}
      {running && estMin === 0 && (
        <div
          className="absolute inset-y-0 left-0 w-[3px] pointer-events-none bg-emerald-500/50"
          aria-hidden
        />
      )}
      {/* Done-task post-mortem bar: dimmer than the running bar so the
          line-through stays the dominant "done" cue, but still readable
          as "this took N of M minutes". */}
      {showDoneBar && (
        <div
          className={cn("absolute inset-y-0 left-0 pointer-events-none", doneFillClass)}
          style={{ width: `${doneFillPct}%` }}
          aria-hidden
        />
      )}
      <div className="relative flex items-center gap-3 p-3 group">
        {/* Toggle: hit area expanded to 44px on mobile via padding +
            min-w/min-h, while the visual icon stays the same size.
            role/aria-checked make the custom button announce as a
            checkbox to screen readers (the visual is Lucide icons,
            not a native <input>). */}
        <button
          onClick={(e) => { e.stopPropagation(); onToggle(); }}
          className="shrink-0 inline-flex items-center justify-center min-w-[44px] min-h-[44px] md:min-w-0 md:min-h-0 -m-2 md:m-0 p-2 md:p-0 rounded"
          title={task.done ? t("tasks.row.markNotDone") : t("tasks.row.markDone")}
          aria-label={task.done ? t("tasks.row.markNotDone") : t("tasks.row.markDone")}
          role="checkbox"
          aria-checked={!!task.done}
        >
          <TickMark done={!!task.done} />
        </button>
        <button
          onClick={clickGuarded}
          onPointerDown={lpDown}
          onPointerMove={lpMove}
          onPointerUp={lpCancel}
          onPointerCancel={lpCancel}
          onContextMenu={(e) => e.preventDefault()}
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
          <div className="text-xs text-muted-foreground flex items-center gap-2 mt-0.5 flex-wrap">
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
                title={t("tasks.row.recurring", { rule: task.recurrence_rule })}
              >
                <Repeat className="w-3 h-3" /> {task.recurrence_rule}
              </span>
            )}
            {/* When the timer is running, the estimate chip flips into a
                live "Nm / Mm" badge (or "running · Nm" without estimate).
                Promoted to visible on mobile too — it's the readout for
                the green fill bar. */}
            {running ? (
              <span
                className={cn(
                  "flex items-center gap-1 font-medium",
                  phase === "under"    && "text-emerald-600 dark:text-emerald-400",
                  phase === "over"     && "text-amber-700 dark:text-amber-400",
                  phase === "way-over" && "text-rose-600 dark:text-rose-400",
                )}
                title={estMin > 0
                  ? t("tasks.row.runningTitle", { elapsed: Math.floor(displayMin), estimate: estMin, prior: priorMin })
                  : t("tasks.row.runningTitleNoEstimate", { elapsed: Math.floor(displayMin), prior: priorMin })}
              >
                <Clock className="w-3 h-3" />
                {estMin > 0
                  ? `${formatMinutes(Math.floor(displayMin))} / ${formatMinutes(estMin)}`
                  : t("tasks.row.running", { time: formatMinutes(Math.floor(displayMin)) })}
              </span>
            ) : task.done && priorMin > 0 ? (
              // Done-task post-mortem chip. With estimate: "took 14m / 30m"
              // tinted by phase so over/under is glance-readable. Without
              // estimate: a quiet "took 14m" so the recorded time still
              // surfaces somewhere.
              <span
                className={cn(
                  "flex items-center gap-1",
                  estMin > 0 && phase === "under"    && "text-emerald-600/80 dark:text-emerald-400/80",
                  estMin > 0 && phase === "over"     && "text-amber-700/80 dark:text-amber-400/80",
                  estMin > 0 && phase === "way-over" && "text-rose-600/80 dark:text-rose-400/80",
                )}
                title={estMin > 0
                  ? t("tasks.row.tookTitle", { prior: priorMin, estimate: estMin })
                  : t("tasks.row.tookTitleNoEstimate", { prior: priorMin })}
              >
                <Clock className="w-3 h-3" />
                {estMin > 0
                  ? t("tasks.row.tookOf", { time: formatMinutes(priorMin), estimate: formatMinutes(estMin) })
                  : t("tasks.row.took", { time: formatMinutes(priorMin) })}
              </span>
            ) : task.estimated_minutes ? (
              <span className="hidden md:flex items-center gap-1">
                <Clock className="w-3 h-3" /> {formatMinutes(task.estimated_minutes)}
              </span>
            ) : null}
            {task.category && <span className="opacity-60">#{task.category}</span>}
            {kidsTotal > 0 && (
              <span
                className="opacity-60 flex items-center gap-1"
                title={t("tasks.row.subtasksDone", { done: kidsDone, total: kidsTotal })}
              >
                <ListTodo className="w-3 h-3" /> {kidsDone}/{kidsTotal}
              </span>
            )}
            {task.assignees && task.assignees.length > 0 && (
              <MemberStack people={task.assignees.map(a => ({ id: String(a.user_id), name: a.name }))} size={18} />
            )}
          </div>
        </button>

        {/* Snooze chips. Mobile: always visible (no hover on touch
            devices, so hover-only would be unreachable), and only one —
            two pushed the title down to "E-Mail beantworten i…"
            (2026-09-29); a week's snooze is a tap away in the editor's
            date field. Desktop: hover-only as before to keep the
            resting row uncluttered. */}
        {onSnooze && !task.done && (
          <div className="flex md:hidden items-center gap-1 shrink-0">
            <SnoozeChip label={t("tasks.snooze.day")}  onClick={() => onSnooze(isoDaysFromNow(1))} />
          </div>
        )}
        {onSnooze && !task.done && (
          <div className="hidden md:group-hover:flex items-center gap-1 shrink-0">
            <SnoozeChip label={t("tasks.snooze.day")} onClick={() => onSnooze(isoDaysFromNow(1))} />
            <SnoozeChip label={t("tasks.snooze.nextMon")} onClick={() => onSnooze(isoNextWeekday(1))} />
            <SnoozeChip label={t("tasks.snooze.week")} onClick={() => onSnooze(isoDaysFromNow(7))} />
          </div>
        )}
        {/* Trash hidden on mobile — accidental flicks on a dense list
            were deleting tasks. Delete is reachable inside the
            expanded editor (TaskInlineEditor's new "Delete" button)
            via tap-to-expand. Desktop keeps the always-visible
            trash since hover gives natural targeting affordance. */}
        {onToggleTimer && !task.done && (
          // Focus: start this task's timer; the FocusCard then shows it
          // on every screen. Visible while running, on hover otherwise.
          <button
            onClick={(e) => { e.stopPropagation(); onToggleTimer(); }}
            className={cn(
              "shrink-0 p-1.5 rounded-md transition",
              task.started_at
                ? "inline-flex text-emerald-500 hover:bg-emerald-500/10"
                : "hidden md:group-hover:inline-flex text-muted-foreground hover:text-emerald-500 hover:bg-emerald-500/10",
            )}
            title={task.started_at ? t("tasks.row.pauseFocus") : t("tasks.row.focusThis")}
            aria-label={task.started_at ? t("tasks.row.pauseFocus") : t("tasks.row.focusThis")}
          >
            {task.started_at ? <Pause className="w-3.5 h-3.5" /> : <Play className="w-3.5 h-3.5" />}
          </button>
        )}
        <button
          onClick={(e) => { e.stopPropagation(); onRemove(); }}
          className="hidden md:group-hover:inline-flex shrink-0 p-1.5 rounded-md text-muted-foreground hover:text-red-500 hover:bg-red-500/10 transition"
          title={t("tasks.row.delete")}
          aria-label={t("tasks.row.delete")}
        >
          <Trash2 className="w-3.5 h-3.5" />
        </button>
        <button
          onClick={(e) => { e.stopPropagation(); onClick(); }}
          className="shrink-0 inline-flex items-center justify-center min-w-[44px] min-h-[44px] md:min-w-0 md:min-h-0 -m-2 md:m-0 p-2 md:p-0 rounded text-muted-foreground hover:text-foreground"
          title={expanded ? t("tasks.row.collapse") : t("tasks.row.expand")}
          aria-label={expanded ? t("tasks.row.collapseTask") : t("tasks.row.expandTask")}
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
          onToggleTimer={task.done ? undefined : onToggleTimer}
        />
      )}

      {/* Subtasks — indented, with their own checkbox + delete. Click
          the (N/M) badge above to expand/collapse. */}
      {kidsTotal > 0 && (
        <div className="border-t border-border/60 px-3 py-1.5">
          <button
            type="button"
            onClick={() => setChildrenOpen(o => !o)}
            className="text-2xs text-muted-foreground hover:text-foreground flex items-center gap-1 mb-1"
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
            {t("tasks.row.subtasks")}{kidsDone}/{kidsTotal}{groupEstimate(kids.filter(c => !c.done))}
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
                    title={c.done ? t("tasks.row.markNotDone") : t("tasks.row.markDone")}
                    aria-label={c.done ? t("tasks.row.markSubtaskNotDone") : t("tasks.row.markSubtaskDone")}
                    role="checkbox"
                    aria-checked={!!c.done}
                  >
                    <TickMark done={!!c.done} size={15} />
                  </button>
                  <span className={cn(
                    "flex-1 text-xs truncate",
                    c.done && "line-through text-muted-foreground",
                  )}>
                    {c.title}
                  </span>
                  {c.estimated_minutes != null && c.estimated_minutes > 0 && (
                    <span
                      className="text-xs md:text-2xs text-muted-foreground shrink-0 inline-flex items-center gap-0.5"
                      title={t("tasks.row.estimated", { time: formatMinutes(c.estimated_minutes) })}
                    >
                      <Clock className="w-3 h-3 md:w-2.5 md:h-2.5" />
                      {formatMinutes(c.estimated_minutes)}
                    </span>
                  )}
                  {c.due_date && (
                    <span className="text-xs md:text-2xs text-muted-foreground shrink-0">
                      {formatDue(c.due_date)}
                    </span>
                  )}
                  {/* Subtask delete: always visible on mobile (hover-
                      only is unreachable on touch), hover-only on
                      desktop to keep the resting list calm. */}
                  <button
                    onClick={() => removeChild?.(c)}
                    className="shrink-0 p-2 md:p-1 rounded text-muted-foreground hover:text-red-500 md:opacity-0 md:group-hover/c:opacity-100 transition"
                    title={t("tasks.row.deleteSubtask")}
                    aria-label={t("tasks.row.deleteSubtask")}
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
  task, onCancel, onSaved, onDelete, onAddSubtask, onToggleTimer,
}: {
  /** Start / pause focus on this task (its timer). */
  onToggleTimer?: () => void;
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
  const { t } = useTranslation();
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
      toast(t("tasks.errors.failed", { error: e?.message || e }));
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
          title={t("tasks.editor.dueDate")}
        />
        <input
          type="number"
          min={5} step={5}
          value={estimate}
          onChange={e => setEstimate(e.target.value)}
          placeholder={t("tasks.editor.estimatePlaceholder")}
          className="h-8 px-2 text-xs bg-background border border-border rounded-md focus:outline-none"
          title={t("tasks.editor.estimateTitle")}
        />
        <select
          value={priority}
          onChange={e => setPriority(parseInt(e.target.value, 10))}
          className="h-8 px-2 text-xs bg-background border border-border rounded-md focus:outline-none"
          title={t("tasks.editor.priority")}
        >
          <option value={0}>{t("tasks.priority.low")}</option>
          <option value={1}>{t("tasks.priority.normal")}</option>
          <option value={2}>{t("tasks.priority.high")}</option>
        </select>
        <input
          value={category}
          onChange={e => setCategory(e.target.value)}
          placeholder={t("tasks.editor.categoryPlaceholder")}
          className="h-8 px-2 text-xs bg-background border border-border rounded-md focus:outline-none"
        />
      </div>
      <input
        value={notes}
        onChange={e => setNotes(e.target.value)}
        placeholder={t("tasks.editor.notesPlaceholder")}
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
      <div className="flex flex-wrap items-center gap-2 pt-1">
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
            title={t("tasks.row.delete")}
            aria-label={t("tasks.row.delete")}
          >
            <Trash2 className="w-3.5 h-3.5" /> {t("tasks.editor.delete")}
          </button>
        )}
        {onToggleTimer && (
          <button
            type="button"
            onClick={onToggleTimer}
            className={cn(
              "text-xs h-9 px-3 rounded-md flex items-center gap-1 transition",
              task.started_at
                ? "border border-emerald-500/40 text-emerald-600 hover:bg-emerald-500/10"
                : "bg-emerald-600 hover:bg-emerald-700 text-white",
              !onDelete && "mr-auto",
            )}
          >
            {task.started_at
              ? <><Pause className="w-3.5 h-3.5" /> {t("tasks.editor.pauseFocus")}</>
              : <><Play className="w-3.5 h-3.5" /> {t("tasks.editor.focus")}</>}
          </button>
        )}
        <button
          onClick={onCancel}
          className="text-xs h-9 px-3 rounded-md border border-border hover:bg-muted flex items-center gap-1"
        >
          <X className="w-3 h-3" /> {t("tasks.editor.cancel")}
        </button>
        <button
          onClick={save}
          disabled={saving || !title.trim()}
          className="text-xs h-9 px-3 rounded-md bg-primary text-primary-foreground hover:opacity-90 disabled:opacity-50 flex items-center gap-1"
        >
          {saving ? <Loader2 className="w-3 h-3 animate-spin" /> : <Check className="w-3 h-3" />}
          {t("tasks.editor.save")}
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
  const { t } = useTranslation();
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
        className="text-xs h-7 px-2.5 rounded-full border border-dashed border-border text-muted-foreground hover:text-foreground hover:border-emerald-500/40 transition inline-flex items-center gap-1"
      >
        <Plus className="w-3 h-3" />
        {t("tasks.subtask.add")}
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
        placeholder={t("tasks.subtask.placeholder")}
        aria-label={t("tasks.subtask.aria")}
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
        {t("tasks.subtask.addButton")}
      </button>
      <button
        type="button"
        onClick={() => { setOpen(false); setText(""); }}
        disabled={saving}
        className="text-xs h-8 px-2 rounded-md border border-border hover:bg-muted text-muted-foreground disabled:opacity-50"
        aria-label={t("tasks.subtask.close")}
      >
        <X className="w-3 h-3" />
      </button>
    </div>
  );
}

// Canonical strings the recurrence parser in
// `backend/tasks_recurrence.py` definitely accepts. Keep these strings
// in sync with that parser — they're matched case-insensitively against
// `tasks.recurrence_rule` when deciding which chip to highlight. Labels
// are catalogue keys (tasks.repeat.*), resolved at render.
const REPEAT_PRESETS: Array<{ labelKey: string; value: string }> = [
  { labelKey: "tasks.repeat.daily",    value: "daily" },
  { labelKey: "tasks.repeat.weekdays", value: "every Mon,Tue,Wed,Thu,Fri" },
  { labelKey: "tasks.repeat.weekly",   value: "weekly" },
  { labelKey: "tasks.repeat.biweekly", value: "every 2 weeks" },
  { labelKey: "tasks.repeat.monthly",  value: "monthly" },
  { labelKey: "tasks.repeat.yearly",   value: "yearly" },
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
  const { t } = useTranslation();
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
        <span className="text-2xs text-muted-foreground mr-1 flex items-center gap-1">
          <Repeat className="w-3 h-3" /> {t("tasks.repeat.label")}
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
              {t(p.labelKey)}
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
          {t("tasks.repeat.custom")}
        </button>
        {value.trim() !== "" && (
          <button
            type="button"
            onClick={() => { onChange(""); setShowCustom(false); }}
            title={t("tasks.repeat.clearTitle")}
            aria-label={t("tasks.repeat.clear")}
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
          placeholder={t("tasks.repeat.customPlaceholder")}
          className="w-full h-8 px-2 text-xs bg-background border border-border rounded-md focus:outline-none focus:ring-1 focus:ring-ring/40"
          title={t("tasks.repeat.customTitle")}
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
  const { t } = useTranslation();
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
        <span className="font-medium text-amber-600">{t("tasks.panel.yorik")}</span>{" "}
        <span className="text-foreground">{result.summary}</span>
        <button onClick={onCancel} className="ml-2 underline">{t("tasks.panel.dismiss")}</button>
      </div>
    );
  }

  return (
    <div className="mb-4 rounded-xl bg-violet-500/[0.06] border border-violet-500/30 overflow-hidden panel-enter">
      <div className="px-3 py-2 border-b border-violet-500/20 text-xs flex items-center justify-between">
        <div>
          <span className="font-medium text-violet-500">{t("tasks.panel.proposes")}</span>{" "}
          <span className="text-foreground">{result.summary}</span>
        </div>
        <span className="text-2xs text-muted-foreground">{t("tasks.panel.changes", { count: items.length })}</span>
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
              <span className="flex items-center gap-1.5 text-2xs">
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
                      aria-label={t("tasks.panel.minutesFor", { title: task.title })}
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
                    {changes.priority === 2 ? t("tasks.priority.high") : changes.priority === 0 ? t("tasks.priority.low") : t("tasks.priority.normal")}
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
                title={t("tasks.panel.applyOneTitle")}
                aria-label={t("tasks.panel.applyOneAria", { title: task.title })}
                className="text-2xs px-2 py-1 rounded-md border border-violet-500/40 text-violet-500 hover:bg-violet-500/10 disabled:opacity-40 disabled:cursor-not-allowed inline-flex items-center gap-1"
              >
                <Check className="w-3 h-3" />
                {t("tasks.panel.apply")}
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
          {t("tasks.panel.cancel")}
        </button>
        <button
          onClick={applyAll}
          disabled={applying}
          className="text-xs px-3 py-1.5 rounded-md bg-violet-500 text-white hover:opacity-90 disabled:opacity-50 flex items-center gap-1"
        >
          {applying
            ? <Loader2 className="w-3 h-3 animate-spin" />
            : <Check className="w-3 h-3" />}
          {t("tasks.panel.applyAll")}
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
  if (diff === 0) return i18n.t("common.today");
  if (diff === 1) return i18n.t("common.tomorrow");
  if (diff === -1) return i18n.t("common.yesterday");
  if (diff > 1 && diff < 7) return formatDate(d, { weekday: "long" });
  return formatDate(d, { day: "numeric", month: "short" });
}
