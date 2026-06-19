/**
 * Yorik Calendar — shadcn-polished three-pane layout.
 *
 *   [mini-cal + filters]   [month / week / day grid]   [selected day]
 *      ~260px                       flex-1                    ~340px
 *
 * Three views: month grid, week (7-col time grid), day (1-col time grid).
 * Add-event dialog handles new + edit. Click an empty time slot in
 * week/day → new event with that time pre-filled.
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  ChevronLeft, ChevronRight, Plus, Calendar as CalendarIcon,
  Loader2, Check, Trash2, X, Clock, MapPin, Car,
  Eye, EyeOff, Share2, ShieldAlert, UsersRound,
  Sparkles, Search, AlertTriangle, Video, Repeat,
} from "lucide-react";

// ── Travel-time helpers ────────────────────────────────────────────
/** "1h 25m" / "23 min" / "12 min" — same shape as the maps connector. */
function formatTravelTime(seconds: number): string {
  const min = Math.round(seconds / 60);
  if (min < 60) return `${min} min`;
  return `${Math.floor(min / 60)}h ${min % 60}m`;
}
/** "13:35" from "leave by" perspective — backs out travel time from
 *  the event's start time. */
function leaveAt(startsAt: string, travelSeconds: number): string {
  const start = new Date(startsAt);
  if (isNaN(start.getTime())) return "";
  const leave = new Date(start.getTime() - travelSeconds * 1000);
  const hh = String(leave.getHours()).padStart(2, "0");
  const mm = String(leave.getMinutes()).padStart(2, "0");
  return `${hh}:${mm}`;
}
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { Dock } from "@/components/Dock";
import { useAuth } from "@/components/AuthGate";
import {
  CATEGORY_PALETTE, CATEGORY_ORDER, swatchFor,
  type EventCategory,
} from "./categoryPalette";
import {
  useTriPane, MobileTopBar, MobileBackdrop,
  mobileAsideLeft, mobileAsideRight,
} from "@/components/MobileShell";
import type {
  CalendarEvent, Task, AssignableUser,
  Calendar, CalendarShare, EventAttendee, FreebusyBlocks,
} from "./types";

const ROLE = "admin";
const WEEK_STARTS_MONDAY = true;
const DAYS_SHORT = WEEK_STARTS_MONDAY
  ? ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
  : ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

type ViewMode = "month" | "week" | "day";

export function CalendarApp() {
  const today = useMemo(() => new Date(), []);
  const [view, setView] = useState<ViewMode>(() => {
    const saved = localStorage.getItem("yorik_cal_view") as ViewMode | null;
    const isMobile = typeof window !== "undefined"
      && window.matchMedia("(max-width: 639px)").matches;
    // Month view is intentionally not exposed on mobile (the grid is too
    // dense to read at phone widths). If a desktop session saved "month"
    // and the same browser later opens on mobile, treat it as "week" so
    // the user isn't stuck on a view their switcher can't deselect.
    if (isMobile) {
      if (saved === "month") return "week";
      if (saved === "day" || saved === "week") return saved;
      return "day";
    }
    if (saved) return saved;
    return "month";
  });
  // `anchor` is "the date the view is centered on". For month it's
  // any date in the displayed month; for week, any date in the week;
  // for day, the day itself.
  const [anchor, setAnchor] = useState<Date>(today);
  const [selected, setSelected] = useState<Date>(today);
  const [editing, setEditing] = useState<
    CalendarEvent
    | "new"
    | {
        defaultDate: Date;
        defaultStartMin?: number;
        defaultEndMin?: number;
        defaultTitle?: string;
        // Optional pre-fills coming from the ⌘K quick-add overlay
        // (LLM-parsed). The dialog reads these on mount, then
        // behaves normally — user can override any of them.
        defaultAllDay?: boolean;
        defaultLocation?: string;
        defaultAttendeeNames?: string[];
        defaultCalendarKind?: "personal" | "shared";
      }
    | null
  >(null);
  const [editingTask, setEditingTask] = useState<Task | "new" | null>(null);
  // ⌘K quick-add overlay state — controls the LLM-parsed event capture
  // (new endpoint /api/events/parse-natural). Open via ⌘K shortcut or
  // the "+ Quick add" header button.
  const [quickAddOpen, setQuickAddOpen] = useState(false);
  // Search popover state — calls /api/events/search?q=…
  const [searchOpen, setSearchOpen] = useState(false);
  // Event IDs to briefly highlight (e.g. after voice creates an event).
  // Cleared automatically after ~4s so the highlight feels like a flash,
  // not a permanent state.
  const [highlightedIds, setHighlightedIds] = useState<Set<number>>(new Set());
  // Which calendars are visible in the grid right now (sidebar eye
  // toggles). Stored in localStorage so reload keeps the user's view.
  // First-run default — handled in the seeding effect just below — is
  // "only my own calendars visible"; the user opts INTO Shared and
  // others' calendars by clicking their eye toggle. Mirrors the
  // mental model of "I live in my calendar, I peek at others when
  // needed" rather than the noisier always-overlay-everything default.
  const [hiddenCalendarIds, setHiddenCalendarIds] = useState<Set<number>>(() => {
    try {
      const raw = localStorage.getItem("yorik_cal_hidden");
      if (!raw) return new Set();
      return new Set<number>(JSON.parse(raw));
    } catch { return new Set(); }
  });
  useEffect(() => {
    localStorage.setItem("yorik_cal_hidden", JSON.stringify([...hiddenCalendarIds]));
  }, [hiddenCalendarIds]);
  // Which calendar to share — drives the ShareCalendarModal.
  const [sharingCalendar, setSharingCalendar] = useState<Calendar | null>(null);

  useEffect(() => { localStorage.setItem("yorik_cal_view", view); }, [view]);

  // ⌘K opens quick-add anywhere on the calendar route. Skip when the
  // user is typing in an input/textarea/contenteditable so we don't
  // hijack the global Find shortcut by accident.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const t = e.target as HTMLElement | null;
      const inEditable = !!t && (
        t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable
      );
      if (e.key === "k" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        setQuickAddOpen(true);
        setSearchOpen(false);
      } else if (e.key === "/" && !inEditable) {
        e.preventDefault();
        setSearchOpen(true);
        setQuickAddOpen(false);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // Deep-link: `/calendar?date=YYYY-MM-DD` jumps the view to that date.
  // Used by the home-screen briefing card so clicking "Tomorrow → meeting
  // at 14:00" lands on the right day instead of dumping the user on today.
  // Runs once on mount + whenever the query param changes.
  const [searchParams] = useSearchParams();
  useEffect(() => {
    const d = searchParams.get("date");
    if (d && /^\d{4}-\d{2}-\d{2}$/.test(d)) {
      const target = new Date(d + "T12:00:00");  // noon avoids DST edge cases
      if (!isNaN(target.getTime())) {
        setAnchor(target);
        setSelected(target);
      }
    }
    const v = searchParams.get("view");
    if (v && (v === "month" || v === "week" || v === "day")) {
      setView(v as ViewMode);
    }
  }, [searchParams]);


  // Listen for global UI actions — primarily `show_calendar` emitted by
  // the calendar mutation skills (add / update / delete). Without this
  // the user voices "termin 12:30" and the calendar shows nothing.
  //
  // We BOTH listen for live events AND drain the pending queue on mount.
  // The queue catches the case where a voice flow navigates the user from
  // /chat to /calendar — the dispatchEvent fires before this component
  // has mounted, so the listener would miss it.
  useEffect(() => {
    function applyShowCalendar(detail: any) {
      if (!detail || detail.type !== "show_calendar") return;
      // eslint-disable-next-line no-console
      console.log("[calendar] show_calendar action:", detail);
      if (detail.anchor_date) {
        try {
          const d = new Date(detail.anchor_date);
          if (!isNaN(d.getTime())) setAnchor(d);
        } catch {}
      }
      if (detail.view && ["month", "week", "day"].includes(detail.view)) {
        setView(detail.view as ViewMode);
      }
      const ids: number[] = Array.isArray(detail.highlight_event_ids) ? detail.highlight_event_ids : [];
      if (ids.length > 0) {
        setHighlightedIds(new Set(ids));
        setTimeout(() => setHighlightedIds(new Set()), 4000);
      }
      // useApi auto-refetches when startISO/endISO deps change (which
      // they do when we setAnchor/setView). Explicit refetch is a
      // safety net for the case where the new anchor maps to the same
      // window (no deps change → no auto-refetch).
      eventsApi.refetch();
    }
    function onUiAction(e: Event) {
      const detail = (e as CustomEvent).detail;
      if (detail?.type === "show_calendar") {
        applyShowCalendar(detail);
      } else if (detail?.type === "refresh_data") {
        // Skill emitted by add/update/delete_task or _bill — refetch the
        // table that changed so the user sees the new row immediately.
        if (detail.table === "tasks") tasksApi.refetch();
        else if (detail.table === "events") eventsApi.refetch();
        // Bills aren't shown in CalendarApp — HomeApp/dock count chips
        // will pick up the change on next refresh tick.
      }
    }
    // Drain any actions queued before this component mounted (e.g.
    // VoiceFab navigated us here from /chat and the event fired during
    // route transition). Apply EACH in arrival order — typical voice
    // flow emits TWO: one from the mutation skill (carries the new
    // highlight_event_ids), one from the LLM's explicit show_calendar
    // tool call (sets view=week, no highlights). If we only kept the
    // last, the highlights vanish. Applying both in order keeps both
    // pieces of state — last setView wins, but setHighlightedIds from
    // the first survives because the second doesn't touch it.
    import("@/lib/uiActions").then(({ drainUiActions }) => {
      const drained = drainUiActions(["show_calendar"]);
      if (drained.length > 0) {
        // eslint-disable-next-line no-console
        console.log("[calendar] drained queued actions:", drained.length, drained);
        for (const action of drained) {
          applyShowCalendar(action);
        }
      }
    });
    window.addEventListener("yorik-ui-action", onUiAction);
    return () => window.removeEventListener("yorik-ui-action", onUiAction);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Compute the visible date window per view.
  const { windowStart, windowEnd } = useMemo(() => {
    if (view === "month") {
      const s = startOfWeek(startOfMonth(anchor), WEEK_STARTS_MONDAY);
      const e = new Date(s); e.setDate(e.getDate() + 42);
      return { windowStart: s, windowEnd: e };
    }
    if (view === "week") {
      const s = startOfWeek(anchor, WEEK_STARTS_MONDAY);
      const e = new Date(s); e.setDate(e.getDate() + 7);
      return { windowStart: s, windowEnd: e };
    }
    // day
    const s = startOfDay(anchor);
    const e = new Date(s); e.setDate(e.getDate() + 1);
    return { windowStart: s, windowEnd: e };
  }, [view, anchor]);
  // Aliases kept so existing MonthGrid usage below isn't a sea of renames.
  const gridStart = windowStart;
  const gridEnd = windowEnd;

  // Fetch events for the visible window + a buffer so navigation feels
  // instant when the user pages back/forward by a week.
  const startISO = isoDate(gridStart);
  const endISO   = isoDate(gridEnd);
  // Calendars (sidebar source of truth). Load once; the user toggling
  // visibility doesn't refetch — visibility lives client-side.
  const calsApi = useApi<Calendar[]>("/api/calendars", []);
  const calendars = useMemo(() => calsApi.data || [], [calsApi.data]);

  // First-run default: hide every calendar the user doesn't own, so
  // they land on a clean "just my events" view. Subsequent toggles
  // are persisted (see hiddenCalendarIds effect above) and we never
  // touch the set again — `yorik_cal_hidden_seeded` is the guard.
  useEffect(() => {
    if (calendars.length === 0) return;
    if (localStorage.getItem("yorik_cal_hidden_seeded") === "1") return;
    const initiallyHidden = new Set(
      calendars.filter(c => !c.you_own).map(c => c.id),
    );
    setHiddenCalendarIds(initiallyHidden);
    localStorage.setItem("yorik_cal_hidden_seeded", "1");
  }, [calendars]);
  // Fast lookup color/name for chip rendering.
  const calendarsById = useMemo(
    () => new Map(calendars.map(c => [c.id, c])),
    [calendars],
  );
  // Only fetch events for calendars the user hasn't hidden. When
  // every calendar is hidden (eye-toggled off), pass an impossible id
  // so the server returns []; saves a refetch on toggle changes.
  const visibleCalIds = useMemo(
    () => calendars.filter(c => !hiddenCalendarIds.has(c.id)).map(c => c.id),
    [calendars, hiddenCalendarIds],
  );
  const calIdsParam = visibleCalIds.length === 0 && calendars.length > 0
    ? "&calendar_ids=-1"   // sentinel: deliberately match nothing
    : visibleCalIds.length > 0 && visibleCalIds.length < calendars.length
      ? `&calendar_ids=${visibleCalIds.join(",")}`
      : "";
  const eventsApi = useApi<CalendarEvent[]>(
    `/api/events?role=${ROLE}&start_date=${startISO}&end_date=${endISO}${calIdsParam}`,
    [startISO, endISO, calIdsParam],
  );
  // Tasks honor the same calendar visibility filter so the sidebar's
  // toggles also slice the tasks pane. Server resolves visible calendar
  // ids → owner user ids and returns only tasks where any assignee
  // matches (plus unassigned tasks when the Household calendar is on).
  const tasksApi = useApi<Task[]>(
    `/api/tasks?role=${ROLE}${calIdsParam.replace("&", "&")}`,
    [calIdsParam],
  );
  const events = eventsApi.data || [];
  const tasks = tasksApi.data || [];

  // Deep-link: `/calendar?event=N` opens the dialog for that event.
  // Fired by the NotificationBell when the user clicks an "Event
  // invitation" notification.
  useEffect(() => {
    const raw = searchParams.get("event");
    if (!raw) return;
    const id = parseInt(raw, 10);
    if (!Number.isFinite(id) || id <= 0) return;
    (async () => {
      try {
        const found = events.find(e => e.id === id);
        if (found) { setEditing(found); return; }
        const r = await api.get<CalendarEvent>(`/api/events/${id}?role=${ROLE}`);
        if (r) {
          setEditing(r);
          const d = new Date(r.starts_at);
          setAnchor(d); setSelected(d);
        }
      } catch { /* ignore — event may have been deleted */ }
    })();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  // Group events by ISO date for fast lookup in the grid.
  const eventsByDay = useMemo(() => {
    const m = new Map<string, CalendarEvent[]>();
    for (const e of events) {
      const k = (e.starts_at || "").slice(0, 10);
      if (!k) continue;
      (m.get(k) || m.set(k, []).get(k)!).push(e);
    }
    // Sort each day chronologically.
    for (const arr of m.values()) arr.sort((a, b) => a.starts_at.localeCompare(b.starts_at));
    return m;
  }, [events]);

  // Tasks grouped by due date (for the right panel).
  const tasksByDay = useMemo(() => {
    const m = new Map<string, Task[]>();
    for (const t of tasks) {
      const k = (t.due_date || "").slice(0, 10);
      if (!k) continue;
      (m.get(k) || m.set(k, []).get(k)!).push(t);
    }
    return m;
  }, [tasks]);

  const goToToday = useCallback(() => {
    setAnchor(new Date());
    setSelected(new Date());
  }, []);
  // Prev/next behaves per view: month → month, week → week, day → day.
  const navigate = useCallback((dir: 1 | -1) => {
    setAnchor(d => {
      const n = new Date(d);
      if (view === "month") n.setMonth(n.getMonth() + dir);
      else if (view === "week") n.setDate(n.getDate() + 7 * dir);
      else n.setDate(n.getDate() + dir);
      return n;
    });
  }, [view]);
  const prevMonth = useCallback(() => navigate(-1), [navigate]);
  const nextMonth = useCallback(() => navigate(1), [navigate]);

  const onEventSaved = () => { eventsApi.refetch(); setEditing(null); };

  const tri = useTriPane();

  // Provide the calendars map to every event-rendering descendant so
  // they can call useEventVisualFn() — used to tint shared events with
  // the calendar owner's color (see eventVisual + CalendarVisualContext).
  const visualCtxValue = useMemo(() => ({ calendarsById }), [calendarsById]);

  return (
    <CalendarVisualContext.Provider value={visualCtxValue}>
    <div className="flex h-screen bg-background text-foreground relative">
      <MobileBackdrop show={tri.leftOpen || tri.rightOpen} onClick={tri.closeAll} />
      {/* ── Sidebar: mini-cal + filters ──────────────────────── */}
      <aside className={cn(
        "w-[260px] border-r border-border flex flex-col bg-sidebar shrink-0",
        mobileAsideLeft(tri.leftOpen),
      )}>
        <header className="h-16 px-5 flex items-center justify-between border-b border-border">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-full bg-violet-500/15 flex items-center justify-center">
              <CalendarIcon className="w-4 h-4 text-violet-500" />
            </div>
            <div>
              <div className="font-semibold leading-none">Calendar</div>
              <div className="text-[10px] text-muted-foreground uppercase tracking-wider mt-0.5">
                {events.length} event{events.length === 1 ? "" : "s"}
              </div>
            </div>
          </div>
        </header>

        <div className="p-4">
          <button
            onClick={() => { setSelected(new Date()); setEditing("new"); }}
            className="w-full flex items-center justify-center gap-2 h-10 rounded-md bg-primary text-primary-foreground text-sm font-medium hover:opacity-90 transition shadow-sm"
          >
            <Plus className="w-4 h-4" /> New event
          </button>
        </div>

        {/* Calendars list — eye toggles + colors. Sits above MiniCalendar
            so the user's first decision (which calendars do I want to see
            right now?) is the most prominent thing in the sidebar. */}
        <CalendarsSidebar
          calendars={calendars}
          hidden={hiddenCalendarIds}
          onToggle={(id) => setHiddenCalendarIds(prev => {
            const next = new Set(prev);
            if (next.has(id)) next.delete(id); else next.add(id);
            return next;
          })}
          onSetHidden={setHiddenCalendarIds}
          onShare={(c) => setSharingCalendar(c)}
          onRefresh={() => calsApi.refetch()}
        />

        <MiniCalendar
          anchor={anchor}
          selected={selected}
          today={today}
          eventsByDay={eventsByDay}
          onSelect={(d) => { setSelected(d); setAnchor(startOfMonth(d)); }}
          onAnchorChange={setAnchor}
        />

        <footer className="mt-auto border-t border-border p-3 text-[11px] text-muted-foreground">
          {eventsApi.loading ? "Loading…" : "Auto-syncs · click any day"}
        </footer>
      </aside>

      {/* ── Main: view-aware grid ────────────────────────────── */}
      {/* pb on mobile reserves room for the fixed Dock (~70px tall) +
          the iPhone home-indicator safe area. Shrinks the inner
          viewport for child scrollers (TimeGridView, MobileDayEventList)
          so 22:00-24:00 events scroll to a visible endpoint above the
          dock instead of sliding under it. Desktop unchanged — the
          desktop layout has enough natural whitespace at the bottom. */}
      <main className="flex-1 flex flex-col min-w-0 bg-background pb-[calc(env(safe-area-inset-bottom)+5rem)] md:pb-0">
        <MobileTopBar
          title={viewTitle(view, anchor, gridStart)}
          onMenuClick={() => tri.setLeftOpen(true)}
          onContextClick={() => tri.setRightOpen(true)}
          contextLabel="Tasks"
        />
        <header className="h-16 px-3 md:px-6 flex items-center justify-between border-b border-border">
          <div className="flex items-center gap-4">
            {/* Title is shown by MobileTopBar above; hide the duplicate
                on mobile to give the nav buttons + view switcher
                breathing room. Desktop keeps the h2 as before. */}
            <h2 className="hidden md:block text-xl font-semibold tracking-tight">
              {viewTitle(view, anchor, gridStart)}
            </h2>
            <div className="flex items-center gap-1">
              <NavBtn icon={ChevronLeft} onClick={prevMonth} title="Previous" />
              <button
                onClick={goToToday}
                className="px-3 h-8 text-xs rounded-md hover:bg-muted text-foreground font-medium"
              >
                Today
              </button>
              <NavBtn icon={ChevronRight} onClick={nextMonth} title="Next" />
            </div>
          </div>
          <div className="flex items-center gap-2 text-xs">
            <button
              onClick={() => setSearchOpen(true)}
              className="inline-flex items-center gap-1 px-2.5 h-10 md:h-8 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition"
              title="Search events (/)"
              aria-label="Search events"
            >
              <Search className="w-5 h-5 md:w-3.5 md:h-3.5" />
              <span className="hidden sm:inline">Search</span>
            </button>
            <button
              onClick={() => setQuickAddOpen(true)}
              className="inline-flex items-center gap-1.5 px-2.5 h-10 md:h-8 rounded-md bg-violet-500/10 hover:bg-violet-500/20 text-violet-600 dark:text-violet-400 transition"
              title="Quick-add — natural language (⌘K)"
              aria-label="Quick add event"
            >
              <Sparkles className="w-5 h-5 md:w-3.5 md:h-3.5" />
              <span className="hidden sm:inline">Quick add</span>
              <kbd className="hidden sm:inline text-[9px] bg-card border border-border rounded px-1 ml-0.5">⌘K</kbd>
            </button>
            {/* Desktop switcher — three options. */}
            <div className="hidden sm:block">
              <ViewSwitcher value={view} onChange={setView} />
            </div>
            {/* Mobile switcher — Day / Week only. Month view is too dense
                to read at 375px, so we don't expose it on phones. Lives
                inline in the toolbar (next to Search + Quick-add) per
                user request. */}
            <div className="sm:hidden">
              <ViewSwitcher value={view} onChange={setView} options={["day", "week"]} />
            </div>
          </div>
        </header>

        <TravelTimeAnnouncement events={events} />

        {view === "month" && (
          <div className="flex-1 flex flex-col p-3 md:p-6 min-h-0">
            <div className="grid grid-cols-7 gap-1 mb-1.5 text-[11px] md:text-[10px] uppercase tracking-wider text-muted-foreground font-medium">
              {DAYS_SHORT.map(d => (
                <div key={d} className="text-center py-1">{d}</div>
              ))}
            </div>
            <MonthGrid
              gridStart={gridStart}
              anchor={anchor}
              today={today}
              selected={selected}
              eventsByDay={eventsByDay}
              tasksByDay={tasksByDay}
              highlightedIds={highlightedIds}
              onSelectDay={setSelected}
              onEventClick={setEditing}
              onTaskDropped={(day, task) => {
                setSelected(day);
                setEditing({
                  defaultDate: day,
                  defaultTitle: task.title,
                  defaultCalendarKind: "personal",
                });
              }}
            />
            {/* Mobile-only: selected day's events as a vertical list
                under the grid. Replaces what desktop shows inside the
                right-aside DayPane (which is a closed drawer by default
                on mobile, so without this list mobile users would have
                to open the drawer just to read event titles). */}
            <div className="md:hidden mt-4">
              <div className="text-[11px] uppercase tracking-wider text-muted-foreground font-semibold mb-2">
                {selected.toLocaleDateString([], { weekday: "long", day: "numeric", month: "long" })}
              </div>
              <MobileDayEventList
                events={eventsByDay.get(isoDate(selected)) || []}
                onEventClick={setEditing}
                onAdd={() => setEditing({ defaultDate: selected })}
              />
            </div>
          </div>
        )}

        {view === "week" && (
          <TimeGridView
            days={Array.from({ length: 7 }, (_, i) => addDays(gridStart, i))}
            today={today}
            selected={selected}
            eventsByDay={eventsByDay}
            highlightedIds={highlightedIds}
            onSelectDay={setSelected}
            onEventClick={setEditing}
            onRangeSelected={(day, startMin, endMin) => {
              setSelected(day);
              setEditing({ defaultDate: day, defaultStartMin: startMin, defaultEndMin: endMin });
            }}
            onTaskDropped={(day, mins, task) => {
              setSelected(day);
              setEditing({
                defaultDate: day,
                defaultStartMin: mins,
                defaultEndMin: mins + 60,
                defaultTitle: task.title,
                defaultCalendarKind: "personal",
              });
            }}
          />
        )}

        {view === "day" && (
          <TimeGridView
            days={[anchor]}
            today={today}
            highlightedIds={highlightedIds}
            selected={selected}
            eventsByDay={eventsByDay}
            onSelectDay={setSelected}
            onEventClick={setEditing}
            onRangeSelected={(day, startMin, endMin) => {
              setSelected(day);
              setEditing({ defaultDate: day, defaultStartMin: startMin, defaultEndMin: endMin });
            }}
            onTaskDropped={(day, mins, task) => {
              setSelected(day);
              setEditing({
                defaultDate: day,
                defaultStartMin: mins,
                defaultEndMin: mins + 60,
                defaultTitle: task.title,
                defaultCalendarKind: "personal",
              });
            }}
            singleDay
          />
        )}
      </main>

      {/* ── Right pane: selected day ─────────────────────────── */}
      <aside className={cn(
        "w-[340px] border-l border-border flex flex-col bg-card shrink-0",
        mobileAsideRight(tri.rightOpen),
      )}>
        <DayPane
          date={selected}
          events={eventsByDay.get(isoDate(selected)) || []}
          tasks={tasksByDay.get(isoDate(selected)) || []}
          allTasks={tasks}
          onAdd={() => setEditing("new")}
          onEditEvent={setEditing}
          onEditTask={setEditingTask}
          onTaskToggle={async (t) => {
            await api.patch(`/api/tasks/${t.id}?role=${ROLE}`, { done: !t.done });
            tasksApi.refetch();
          }}
        />
      </aside>

      {/* Mobile FAB — primary "+ event" affordance. Sits at the
          BOTTOM-LEFT, not bottom-right, so it doesn't collide with
          the globally-fixed VoiceFab at right-4/bottom-20. Left = create
          / Right = voice — same convention Telegram uses for compose
          vs attach. Above the Dock via safe-area math. */}
      <button
        type="button"
        onClick={() => setEditing({ defaultDate: selected })}
        className="md:hidden fixed left-4 bottom-[max(5.5rem,calc(env(safe-area-inset-bottom)+4.5rem))] z-40 w-14 h-14 rounded-full bg-primary text-primary-foreground shadow-lg flex items-center justify-center hover:opacity-90 active:scale-95 transition"
        aria-label="Add new event"
        title="Add event"
      >
        <Plus className="w-6 h-6" strokeWidth={2.5} />
      </button>

      {editing !== null && (
        <EventDialog
          event={editing === "new" || (typeof editing === "object" && editing && "defaultDate" in (editing as any)) ? null : (editing as CalendarEvent)}
          defaultDate={
            editing && typeof editing === "object" && "defaultDate" in (editing as any)
              ? (editing as any).defaultDate
              : selected
          }
          defaultStartMin={
            editing && typeof editing === "object" && "defaultStartMin" in (editing as any)
              ? (editing as any).defaultStartMin
              : undefined
          }
          defaultEndMin={
            editing && typeof editing === "object" && "defaultEndMin" in (editing as any)
              ? (editing as any).defaultEndMin
              : undefined
          }
          defaultTitle={
            editing && typeof editing === "object" && "defaultTitle" in (editing as any)
              ? (editing as any).defaultTitle
              : undefined
          }
          defaultAllDay={
            editing && typeof editing === "object" && "defaultAllDay" in (editing as any)
              ? (editing as any).defaultAllDay
              : undefined
          }
          defaultLocation={
            editing && typeof editing === "object" && "defaultLocation" in (editing as any)
              ? (editing as any).defaultLocation
              : undefined
          }
          defaultAttendeeNames={
            editing && typeof editing === "object" && "defaultAttendeeNames" in (editing as any)
              ? (editing as any).defaultAttendeeNames
              : undefined
          }
          defaultCalendarKind={
            editing && typeof editing === "object" && "defaultCalendarKind" in (editing as any)
              ? (editing as any).defaultCalendarKind
              : undefined
          }
          dayEvents={events.filter(ev => {
            const dRef = editing && typeof editing === "object" && "defaultDate" in (editing as any)
              ? (editing as any).defaultDate as Date
              : selected;
            return (ev.starts_at || "").slice(0, 10) === isoDate(dRef);
          })}
          calendars={calendars}
          onClose={() => setEditing(null)}
          onSaved={onEventSaved}
        />
      )}

      {quickAddOpen && (
        <EventQuickAddModal
          onClose={() => setQuickAddOpen(false)}
          onParsed={(p) => {
            // Build the editing prefill from the parsed result. The
            // dialog opens immediately so the user reviews / edits
            // before the actual POST.
            setQuickAddOpen(false);
            const target = p.date ? new Date(p.date + "T12:00:00") : selected;
            const startMin = p.start_time
              ? parseInt(p.start_time.slice(0, 2), 10) * 60 + parseInt(p.start_time.slice(3, 5), 10)
              : undefined;
            const endMin = p.end_time
              ? parseInt(p.end_time.slice(0, 2), 10) * 60 + parseInt(p.end_time.slice(3, 5), 10)
              : (startMin != null && p.duration_minutes
                  ? startMin + p.duration_minutes
                  : undefined);
            setAnchor(target);
            setSelected(target);
            setEditing({
              defaultDate:           target,
              defaultStartMin:       startMin,
              defaultEndMin:         endMin,
              defaultTitle:          p.title,
              defaultAllDay:         p.all_day,
              defaultLocation:       p.location,
              defaultAttendeeNames:  p.attendee_names,
            });
          }}
        />
      )}

      {searchOpen && (
        <EventSearchModal
          onClose={() => setSearchOpen(false)}
          onPick={(ev) => {
            setSearchOpen(false);
            // Jump to that day + flash the row.
            const d = new Date((ev.starts_at || "").slice(0, 10) + "T12:00:00");
            if (!isNaN(d.getTime())) {
              setAnchor(d);
              setSelected(d);
            }
            setHighlightedIds(new Set([ev.id]));
            setTimeout(() => setHighlightedIds(new Set()), 4000);
            setView("day");
          }}
        />
      )}

      {sharingCalendar && (
        <ShareCalendarModal
          calendar={sharingCalendar}
          onClose={() => setSharingCalendar(null)}
          onChanged={() => calsApi.refetch()}
        />
      )}

      {editingTask !== null && (
        <TaskDialog
          task={editingTask === "new" ? null : editingTask}
          defaultDate={selected}
          onClose={() => setEditingTask(null)}
          onSaved={() => { tasksApi.refetch(); setEditingTask(null); }}
        />
      )}

      <Dock activeAppId="calendar" />
    </div>
    </CalendarVisualContext.Provider>
  );
}

// ───────────────────────── mini-calendar (sidebar nav) ──────────────

function MiniCalendar({
  anchor, selected, today, eventsByDay, onSelect, onAnchorChange,
}: {
  anchor: Date;
  selected: Date;
  today: Date;
  eventsByDay: Map<string, CalendarEvent[]>;
  onSelect: (d: Date) => void;
  onAnchorChange: (d: Date) => void;
}) {
  const gridStart = startOfWeek(startOfMonth(anchor), WEEK_STARTS_MONDAY);
  const days = Array.from({ length: 42 }, (_, i) => {
    const d = new Date(gridStart);
    d.setDate(d.getDate() + i);
    return d;
  });
  return (
    <div className="px-4 py-2">
      <div className="flex items-center justify-between mb-2">
        <div className="text-sm font-medium">
          {anchor.toLocaleDateString([], { month: "short", year: "numeric" })}
        </div>
        <div className="flex items-center gap-0.5">
          <button
            onClick={() => onAnchorChange(addMonths(anchor, -1))}
            className="w-9 h-9 md:w-6 md:h-6 hover:bg-muted rounded text-muted-foreground flex items-center justify-center"
            aria-label="Previous month"
          >
            <ChevronLeft className="w-4 h-4 md:w-3 md:h-3" />
          </button>
          <button
            onClick={() => onAnchorChange(addMonths(anchor, 1))}
            className="w-9 h-9 md:w-6 md:h-6 hover:bg-muted rounded text-muted-foreground flex items-center justify-center"
            aria-label="Next month"
          >
            <ChevronRight className="w-4 h-4 md:w-3 md:h-3" />
          </button>
        </div>
      </div>
      <div className="grid grid-cols-7 gap-0.5 text-[10px] text-muted-foreground mb-1 text-center">
        {DAYS_SHORT.map(d => (
          <div key={d} className="py-0.5 font-medium">{d[0]}</div>
        ))}
      </div>
      <div className="grid grid-cols-7 gap-0.5">
        {days.map((d, i) => {
          const inMonth = d.getMonth() === anchor.getMonth();
          const isToday = sameDay(d, today);
          const isSel   = sameDay(d, selected);
          const hasEvent = eventsByDay.has(isoDate(d));
          return (
            <button
              key={i}
              onClick={() => onSelect(d)}
              className={cn(
                "h-7 text-xs rounded relative transition",
                isSel
                  ? "bg-primary text-primary-foreground font-semibold"
                  : isToday
                    ? "bg-primary/15 text-primary font-semibold"
                    : inMonth ? "hover:bg-muted text-foreground" : "text-muted-foreground/40 hover:bg-muted",
              )}
            >
              {d.getDate()}
              {hasEvent && !isSel && (
                <span className="absolute bottom-0.5 left-1/2 -translate-x-1/2 w-1 h-1 rounded-full bg-primary" />
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}

// ───────────────────────── main month grid ──────────────────────────

function MonthGrid({
  gridStart, anchor, today, selected, eventsByDay, tasksByDay, highlightedIds,
  onSelectDay, onEventClick, onTaskDropped,
}: {
  gridStart: Date;
  anchor: Date;
  today: Date;
  selected: Date;
  eventsByDay: Map<string, CalendarEvent[]>;
  tasksByDay: Map<string, Task[]>;
  highlightedIds: Set<number>;
  onSelectDay: (d: Date) => void;
  onEventClick: (e: CalendarEvent) => void;
  onTaskDropped: (d: Date, task: Task) => void;
}) {
  const days = Array.from({ length: 42 }, (_, i) => {
    const d = new Date(gridStart);
    d.setDate(d.getDate() + i);
    return d;
  });
  return (
    <div className="flex-1 grid grid-cols-7 grid-rows-6 gap-1 min-h-0">
      {days.map((d, i) => (
        <DayCell
          key={i}
          day={d}
          inMonth={d.getMonth() === anchor.getMonth()}
          isToday={sameDay(d, today)}
          isSelected={sameDay(d, selected)}
          events={eventsByDay.get(isoDate(d)) || []}
          taskCount={(tasksByDay.get(isoDate(d)) || []).filter(t => !t.done).length}
          highlightedIds={highlightedIds}
          onSelect={() => onSelectDay(d)}
          onEventClick={onEventClick}
          onTaskDropped={onTaskDropped}
          isWeekend={d.getDay() === 0 || d.getDay() === 6}
        />
      ))}
    </div>
  );
}

function DayCell({
  day, inMonth, isToday, isSelected, events, taskCount, highlightedIds,
  onSelect, onEventClick, onTaskDropped, isWeekend,
}: {
  day: Date;
  inMonth: boolean;
  isToday: boolean;
  isSelected: boolean;
  events: CalendarEvent[];
  taskCount: number;
  highlightedIds: Set<number>;
  onSelect: () => void;
  onEventClick: (e: CalendarEvent) => void;
  onTaskDropped: (d: Date, task: Task) => void;
  isWeekend: boolean;
}) {
  // Show up to 3 event chips, "+N more" if there are more.
  const MAX_CHIPS = 3;
  const visible = events.slice(0, MAX_CHIPS);
  const extra = events.length - visible.length;
  const [dropping, setDropping] = useState(false);
  const visualOf = useEventVisualFn();

  return (
    <div
      onClick={onSelect}
      onDragOver={(e) => {
        if (e.dataTransfer.types.includes("application/yorik-task")) {
          e.preventDefault();
          e.dataTransfer.dropEffect = "copy";
          setDropping(true);
        }
      }}
      onDragLeave={() => setDropping(false)}
      onDrop={(e) => {
        setDropping(false);
        const raw = e.dataTransfer.getData("application/yorik-task");
        if (!raw) return;
        try {
          const task: Task = JSON.parse(raw);
          e.preventDefault();
          onTaskDropped(day, task);
        } catch {}
      }}
      className={cn(
        "rounded-lg p-1.5 flex flex-col gap-1 cursor-pointer transition min-h-0 border",
        dropping
          ? "border-primary border-dashed bg-primary/10"
          : isSelected
            ? "border-primary/50 bg-primary/[0.04]"
            : isToday
              ? "border-primary/30 bg-primary/[0.025] hover:bg-muted/30"
              : "border-transparent hover:bg-muted/30",
        !inMonth && "opacity-40",
        isWeekend && inMonth && !isToday && !isSelected && !dropping && "bg-muted/15",
      )}
    >
      <div className="flex items-center justify-between gap-1">
        <span className={cn(
          "text-xs tabular-nums w-6 h-6 flex items-center justify-center rounded-full font-medium",
          isToday && "bg-primary text-primary-foreground",
          !isToday && isSelected && "text-primary font-semibold",
        )}>
          {day.getDate()}
        </span>
        {taskCount > 0 && (
          <span className="text-[9px] text-muted-foreground tabular-nums">{taskCount}T</span>
        )}
      </div>
      {/* Mobile: a single row of up to 3 coloured dots per category
          present that day. Event chips below text-[11px] in a 37px
          cell are unreadable on a phone; dots give density at a
          glance without trying to render the title. The mobile
          "day events" list under the grid shows the full info for
          the selected day. Desktop keeps the chips. */}
      <div className="md:hidden flex items-center justify-center gap-1 mt-auto pt-1">
        {events.slice(0, 4).map(e => {
          const v = visualOf(e, false);
          return (
            <span
              key={`${e.id}_${e.occurrence_date || ""}`}
              className="w-1.5 h-1.5 rounded-full"
              style={{ background: v.accent }}
              aria-hidden="true"
            />
          );
        })}
      </div>
      <div className="hidden md:flex flex-col gap-0.5 overflow-hidden">
        {visible.map(e => (
          <EventChip key={`${e.id}_${e.occurrence_date || ""}`} ev={e}
            highlighted={highlightedIds.has(e.id)}
            onClick={(ev) => { ev.stopPropagation(); onEventClick(e); }} />
        ))}
        {extra > 0 && (
          <div className="text-[10px] text-muted-foreground pl-1">+{extra} more</div>
        )}
      </div>
    </div>
  );
}

function EventChip({ ev, highlighted, onClick }:
  { ev: CalendarEvent; highlighted?: boolean; onClick: (e: React.MouseEvent) => void }) {
  const visualOf = useEventVisualFn();
  const vis = visualOf(ev, !!highlighted);
  const allDay = !!ev.all_day;
  const time = allDay ? "" : (ev.starts_at?.slice(11, 16) || "");
  // Hover title — surface the travel-time badge that doesn't fit on
  // the chip itself so a user scanning the grid still gets the full
  // info on mouseover. Falls back to the event title when there's no
  // travel data.
  const tip = (ev.travel_time_s != null && ev.travel_time_s > 0)
    ? `${ev.title} · ${formatTravelTime(ev.travel_time_s)}`
      + (ev.travel_distance_m ? ` · ${(ev.travel_distance_m/1000).toFixed(1)} km` : "")
      + (ev.travel_provider ? ` · via ${ev.travel_provider.toUpperCase()}` : "")
    : ev.title;
  return (
    <button
      onClick={onClick}
      title={tip}
      className={cn(
        "text-left text-[11px] px-1.5 py-0.5 rounded truncate flex items-center gap-1 hover:brightness-110 transition",
        highlighted && "ring-2 ring-violet-400 ring-offset-1 animate-pulse-flash"
      )}
      style={{
        background: vis.fill,
        color: vis.accent,
        borderLeft: `2px solid ${vis.border}`,
      }}
    >
      {time && <span className="font-medium tabular-nums shrink-0 text-[10px] opacity-80">{time}</span>}
      {ev.recurring && (
        <Repeat
          className="w-2.5 h-2.5 shrink-0 opacity-60"
          aria-label="Recurring"
        />
      )}
      {ev.travel_time_s != null && ev.travel_time_s > 0 && (
        // Tiny driving indicator — the full "18 min · 9.2 km" badge
        // only fits the day-list view, but the grid needs to hint
        // that a trip exists so the user can plan their day. Title
        // attribute carries the full info on hover.
        <Car
          className="w-2.5 h-2.5 shrink-0 opacity-70"
          aria-label="Has travel time"
        />
      )}
      <span className="truncate font-medium">{ev.title}</span>
      {highlighted && (
        <style>{`
          @keyframes pulse-flash {
            0%, 100% { transform: scale(1); }
            50% { transform: scale(1.04); }
          }
          .animate-pulse-flash { animation: pulse-flash 1s ease-in-out 3; }
        `}</style>
      )}
    </button>
  );
}

// ────────────────────── mobile-only day event list ──────────────────
//
// Rendered under the MonthGrid on mobile so phone users can read the
// selected day's events without opening the right-aside drawer. Each
// row is a fat tappable button (≥44pt) with time + colour stripe +
// title; tap → open the EventDialog. Empty state shows a "+ Add"
// button so creating an event in the selected day is one tap.

function MobileDayEventList({
  events,
  onEventClick,
  onAdd,
}: {
  events: CalendarEvent[];
  onEventClick: (e: CalendarEvent) => void;
  onAdd: () => void;
}) {
  if (events.length === 0) {
    return (
      <button
        onClick={onAdd}
        className="w-full text-left px-3 py-3 rounded-lg border border-dashed border-border text-sm text-muted-foreground hover:bg-muted/30 hover:text-foreground transition flex items-center gap-2"
      >
        <Plus className="w-4 h-4" /> No events — tap to add one
      </button>
    );
  }
  // Sort by start time. All-day events first.
  const sorted = [...events].sort((a, b) => {
    if (!!a.all_day !== !!b.all_day) return a.all_day ? -1 : 1;
    return (a.starts_at || "").localeCompare(b.starts_at || "");
  });
  const visualOf = useEventVisualFn();
  return (
    <div className="space-y-1.5">
      {sorted.map(e => {
        const v = visualOf(e, false);
        const time = e.all_day ? "all day" : (e.starts_at?.slice(11, 16) || "");
        return (
          <button
            key={`${e.id}_${e.occurrence_date || ""}`}
            onClick={() => onEventClick(e)}
            className="w-full text-left flex items-stretch gap-3 rounded-lg bg-card border border-border hover:border-foreground/20 transition overflow-hidden"
          >
            <span className="w-1 shrink-0" style={{ background: v.accent }} aria-hidden="true" />
            <div className="flex-1 min-w-0 py-2.5 pr-3">
              <div className="text-sm font-medium truncate">{e.title}</div>
              <div className="text-[12px] text-muted-foreground mt-0.5 flex items-center gap-2 flex-wrap">
                <span className="tabular-nums">{time}</span>
                {e.location && <span className="truncate opacity-80">· {e.location}</span>}
              </div>
            </div>
          </button>
        );
      })}
    </div>
  );
}


// ───────────────────────── right pane: selected day ─────────────────

function DayPane({
  date, events, tasks, allTasks, onAdd, onEditEvent, onEditTask, onTaskToggle,
}: {
  date: Date;
  events: CalendarEvent[];
  tasks: Task[];
  allTasks: Task[];
  onAdd: () => void;
  onEditEvent: (e: CalendarEvent) => void;
  onEditTask: (t: Task) => void;
  onTaskToggle: (t: Task) => void;
}) {
  const dayLabel = date.toLocaleDateString([], { weekday: "long" });
  const fullLabel = date.toLocaleDateString([], { day: "numeric", month: "long", year: "numeric" });
  const visualOf = useEventVisualFn();

  // Selected-day vs today comparison drives two extras only shown on
  // today's pane: an "Overdue" group (tasks with due_date < today and
  // still open) and an "Anytime" group (open tasks without any due_date,
  // which would otherwise be invisible from the calendar route).
  const todayIso = new Date().toISOString().slice(0, 10);
  const selectedIso = isoDate(date);
  const isToday = selectedIso === todayIso;
  const overdueTasks = isToday
    ? allTasks.filter(t => !t.done && t.due_date && t.due_date.slice(0, 10) < todayIso)
    : [];
  const undatedOpenTasks = isToday
    ? allTasks.filter(t => !t.done && !t.due_date)
    : [];
  // Future: open tasks due AFTER the selected day. Sorted by due_date
  // so the soonest is first. Shows on every day's pane (not just today)
  // so the user always knows what's coming.
  const futureTasks = allTasks
    .filter(t => !t.done && t.due_date && t.due_date.slice(0, 10) > selectedIso)
    .sort((a, b) => (a.due_date || "").localeCompare(b.due_date || ""));
  return (
    <>
      <header className="h-16 px-5 flex items-center justify-between border-b border-border">
        <div>
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold">
            {dayLabel}
          </div>
          <div className="font-semibold text-base leading-tight mt-0.5">
            {fullLabel}
          </div>
        </div>
        <button
          onClick={onAdd}
          className="w-8 h-8 rounded-md bg-primary/10 hover:bg-primary/20 text-primary flex items-center justify-center transition"
          title="Add event on this day"
        >
          <Plus className="w-4 h-4" />
        </button>
      </header>

      <div className="flex-1 overflow-y-auto px-5 py-4 space-y-5">
        <section>
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-2">
            Events · {events.length}
          </div>
          {events.length === 0 ? (
            <button
              onClick={onAdd}
              className="w-full text-left px-3 py-3 rounded-lg border border-dashed border-border text-sm text-muted-foreground hover:bg-muted/30 hover:text-foreground transition flex items-center gap-2"
            >
              <Plus className="w-4 h-4" /> Nothing scheduled — add an event
            </button>
          ) : (
            <div className="space-y-1.5">
              {events.map(e => (
                <button
                  key={`${e.id}_${e.occurrence_date || ""}`}
                  onClick={() => onEditEvent(e)}
                  className="w-full text-left p-3 rounded-lg border border-border hover:border-primary/40 hover:bg-muted/30 transition flex items-start gap-3"
                >
                  <div className="w-1.5 self-stretch rounded-full shrink-0"
                       style={{ background: visualOf(e, false).accent }} />
                  <div className="flex-1 min-w-0">
                    <div className="text-sm font-medium truncate">{e.title}</div>
                    <div className="flex items-center gap-3 text-xs text-muted-foreground mt-0.5 flex-wrap">
                      {e.all_day ? (
                        <span>All day</span>
                      ) : (
                        <span className="flex items-center gap-1 tabular-nums">
                          <Clock className="w-3 h-3" />
                          {e.starts_at?.slice(11, 16)}
                          {e.ends_at && <>– {e.ends_at.slice(11, 16)}</>}
                        </span>
                      )}
                      {e.person && e.person !== "all" && (
                        <span className="flex items-center gap-1 capitalize">{e.person}</span>
                      )}
                      {e.travel_time_s && e.travel_time_s > 0 && (
                        <span
                          className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-amber-500/10 text-amber-700 dark:text-amber-300 border border-amber-500/30 tabular-nums"
                          title={
                            `Driving from your home address` +
                            (e.travel_distance_m ? ` · ${(e.travel_distance_m/1000).toFixed(1)} km` : "") +
                            (e.travel_provider ? ` · via ${e.travel_provider.toUpperCase()}` : "")
                          }
                        >
                          <Car className="w-3 h-3" />
                          {formatTravelTime(e.travel_time_s)}
                          {!e.all_day && e.starts_at && (
                            <span className="opacity-70">
                              · leave {leaveAt(e.starts_at, e.travel_time_s)}
                            </span>
                          )}
                        </span>
                      )}
                    </div>
                    {e.location && (
                      <div className="flex items-center gap-1 text-xs text-muted-foreground mt-1 truncate">
                        <MapPin className="w-3 h-3 shrink-0" />
                        <span className="truncate">{e.location}</span>
                      </div>
                    )}
                    {e.notes && (
                      <div className="text-xs text-muted-foreground mt-1 line-clamp-2">{e.notes}</div>
                    )}
                  </div>
                </button>
              ))}
            </div>
          )}
        </section>

        {overdueTasks.length > 0 && (
          <section>
            <div className="text-[10px] uppercase tracking-wider text-rose-600 dark:text-rose-400 font-semibold mb-2">
              Overdue · {overdueTasks.length}
            </div>
            <TaskRows
              items={overdueTasks}
              showDueDate
              onEditTask={onEditTask}
              onTaskToggle={onTaskToggle}
            />
          </section>
        )}

        <section>
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-2">
            {isToday ? `Due today · ${tasks.length}` : `Due ${dayLabel} · ${tasks.length}`}
          </div>
          {tasks.length === 0 ? (
            <div className="text-sm text-muted-foreground italic">No tasks due on this day.</div>
          ) : (
            <TaskRows
              items={tasks}
              onEditTask={onEditTask}
              onTaskToggle={onTaskToggle}
            />
          )}
        </section>

        {undatedOpenTasks.length > 0 && (
          <section>
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-2">
              Anytime · {undatedOpenTasks.length}
            </div>
            <TaskRows
              items={undatedOpenTasks}
              onEditTask={onEditTask}
              onTaskToggle={onTaskToggle}
            />
          </section>
        )}

        {futureTasks.length > 0 && (
          <section>
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-2">
              Future · {futureTasks.length}
            </div>
            <TaskRows
              items={futureTasks}
              showDueDate
              onEditTask={onEditTask}
              onTaskToggle={onTaskToggle}
            />
          </section>
        )}
      </div>
    </>
  );
}

// Shared task-row list used by all task groups in DayPane.
// Drag handle stays so the user can drop items onto a day slot.
// When >4 rows, the list scrolls internally so one long group doesn't
// crowd out the others (the parent pane already scrolls vertically;
// the internal cap just keeps each group glanceable).
function TaskRows({
  items, showDueDate, onEditTask, onTaskToggle,
}: {
  items: Task[];
  showDueDate?: boolean;
  onEditTask: (t: Task) => void;
  onTaskToggle: (t: Task) => void;
}) {
  const scroll = items.length > 4;
  return (
    <div className={cn(
      "space-y-1",
      scroll && "max-h-56 overflow-y-auto pr-1",
    )}>
      {items.map(t => (
        <div
          key={t.id}
          draggable
          onDragStart={(e) => {
            e.dataTransfer.setData("application/yorik-task", JSON.stringify(t));
            e.dataTransfer.effectAllowed = "copy";
          }}
          className="w-full p-2 rounded-md hover:bg-muted/40 flex items-start gap-2.5 transition group cursor-pointer"
          onClick={() => onEditTask(t)}
          title="Click to edit · drag onto a day/time to schedule"
        >
          <button
            onClick={(e) => { e.stopPropagation(); onTaskToggle(t); }}
            aria-label={t.done ? "Mark as not done" : "Mark as done"}
            className={cn(
              "w-4 h-4 rounded border-2 shrink-0 mt-0.5 flex items-center justify-center transition",
              t.done
                ? "bg-emerald-500 border-emerald-500"
                : "border-muted-foreground/40 group-hover:border-primary",
            )}>
            {t.done ? <Check className="w-2.5 h-2.5 text-white" /> : null}
          </button>
          <div className="flex-1 min-w-0">
            <div className={cn("text-sm", t.done && "line-through text-muted-foreground")}>
              {t.title}
            </div>
            <div className="flex items-center gap-2 text-[10px] text-muted-foreground mt-0.5">
              {showDueDate && t.due_date && (
                <span className="tabular-nums">{shortDay(t.due_date)}</span>
              )}
              {t.person && t.person !== "all" && (
                <span className="capitalize">{t.person}</span>
              )}
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

// ───────────────────────── add/edit event dialog ────────────────────

function EventDialog({
  event, defaultDate, defaultStartMin, defaultEndMin, defaultTitle,
  defaultAllDay, defaultLocation, defaultAttendeeNames, defaultCalendarKind,
  dayEvents, calendars, onClose, onSaved,
}: {
  event: CalendarEvent | null;
  defaultDate: Date;
  defaultStartMin?: number;
  defaultEndMin?: number;
  defaultTitle?: string;
  /** Quick-add prefills coming from the LLM parser. */
  defaultAllDay?: boolean;
  defaultLocation?: string;
  defaultAttendeeNames?: string[];
  /**
   * Which calendar bucket the new event should land in by default.
   * "personal" — the logged-in user's own calendar (used for the
   * task-drop flow, where the task is implicitly the user's own).
   * Omitted/"shared" — the household calendar (the original default
   * for events created via the "+ Add" button).
   */
  defaultCalendarKind?: "personal" | "shared";
  /** Other events on the same day, used to surface conflicts inline. */
  dayEvents?: CalendarEvent[];
  calendars: Calendar[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const isNew = !event;
  const { user: meUser } = useAuth();
  const myUserId = meUser.id;
  // ── Calendar overlay state ──
  // Default the new-event calendar to either the user's own personal
  // calendar (drag-from-task flow) or the household Shared calendar
  // (everything else). Fall back to the first writable calendar if
  // neither match. The dropdown still lets the user override, and
  // auto-route swaps to Shared server-side when attendees are added.
  const writableCals = useMemo(
    () => calendars.filter(c => c.access_level === "write"),
    [calendars],
  );
  const calendarDefault = useMemo(() => {
    if (defaultCalendarKind === "personal") {
      const mine = writableCals.find(
        c => c.kind === "personal" && c.owner_user_id === myUserId,
      );
      if (mine) return mine;
    }
    return writableCals.find(c => c.kind === "shared") || writableCals[0];
  }, [writableCals, defaultCalendarKind, myUserId]);
  const [calendarId, setCalendarId] = useState<number | undefined>(
    event?.calendar_id ?? calendarDefault?.id
  );
  useEffect(() => {
    // Only initialise once: when the dialog opens for a new event AND
    // the user hasn't picked yet, snap to the resolved default.
    if (isNew && calendarId === undefined && calendarDefault) {
      setCalendarId(calendarDefault.id);
    }
  }, [isNew, calendarId, calendarDefault]);

  // Attendees state — both account holders (user_ids) and free-text
  // names for kids without logins.
  const [attendeeUserIds, setAttendeeUserIds] = useState<Set<number>>(new Set());
  const [attendeeNamesStr, setAttendeeNamesStr] = useState<string>(
    () => (defaultAttendeeNames || []).join(", "),
  );
  const [visibility, setVisibility] = useState<"default" | "private">(event?.visibility || "default");
  // Loaded once when editing an existing event so the picker reflects
  // who's already on the invite. Also pulls THIS user's attendee row
  // (if any) so we can render the RSVP banner. (meUser / myUserId
  // come from the useAuth call at the top of this component.)
  const [myAttendee, setMyAttendee] = useState<EventAttendee | null>(null);
  const [allAttendees, setAllAttendees] = useState<EventAttendee[]>([]);
  const eventId = event?.id;
  const refreshAttendees = useCallback(async () => {
    if (!eventId) return;
    try {
      const list = await api.get<EventAttendee[]>(`/api/events/${eventId}/attendees`);
      setAllAttendees(list);
      setAttendeeUserIds(new Set(list.filter(a => a.user_id).map(a => a.user_id!)));
      setAttendeeNamesStr(list.filter(a => !a.user_id).map(a => a.person_name).join(", "));
      setMyAttendee(list.find(a => a.user_id === myUserId) || null);
    } catch { /* silent */ }
  }, [eventId, myUserId]);
  useEffect(() => {
    refreshAttendees();
  }, [refreshAttendees]);

  async function rsvp(status: "accepted" | "declined" | "tentative",
                       proposedTime?: string) {
    if (!eventId) return;
    try {
      await api.post(`/api/events/${eventId}/rsvp`, {
        status, proposed_time_iso: proposedTime || null,
      });
      await refreshAttendees();
    } catch (e: any) {
      alert(`RSVP failed: ${e.message || e}`);
    }
  }
  const defaultTimeStr = defaultStartMin != null
    ? minsToTime(defaultStartMin)
    : "09:00";
  const defaultEndStr = defaultEndMin != null
    ? minsToTime(defaultEndMin)
    : defaultStartMin != null
      ? minsToTime(defaultStartMin + 60)
      : "10:00";
  const [title, setTitle] = useState(event?.title || defaultTitle || "");
  const [date, setDate] = useState(() => event ? event.starts_at.slice(0, 10) : isoDate(defaultDate));
  const [allDay, setAllDay] = useState(!!event?.all_day || !!defaultAllDay);
  const [startTime, setStartTime] = useState(() => event?.starts_at?.slice(11, 16) || defaultTimeStr);
  const [endTime, setEndTime] = useState(() => event?.ends_at?.slice(11, 16) || defaultEndStr);
  const [color, setColor] = useState(event?.color || "#818cf8");
  const [category, setCategory] = useState<EventCategory | "">(
    (event?.category as EventCategory) || ""
  );
  const [person, setPerson] = useState(event?.person || "");
  const [notes, setNotes] = useState(event?.notes || "");
  const [location, setLocation] = useState(event?.location || defaultLocation || "");
  const [allowedRoles, setAllowedRoles] = useState(event?.allowed_roles || "admin,member");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // ── Recurrence ──
  // The DB column is a plain string code; we map it to a UI mode + an
  // optional weekday set. Modes:
  //   "" (none) | daily | weekly | weekdays | monthly | yearly
  // For `weekdays` the user can narrow the default Mo-Fr set to any
  // subset via the chip row. ISO weekdays: 1=Mon ... 7=Sun.
  const parseRecurring = (raw?: string | null): { mode: string; days: Set<number> } => {
    const s = (raw || "").trim().toLowerCase();
    if (!s) return { mode: "", days: new Set([1, 2, 3, 4, 5]) };
    if (s === "weekdays") return { mode: "weekdays", days: new Set([1, 2, 3, 4, 5]) };
    if (s.startsWith("weekdays:")) {
      const ds = new Set<number>();
      s.slice("weekdays:".length).split(",").forEach(p => {
        const n = parseInt(p.trim(), 10);
        if (n >= 1 && n <= 7) ds.add(n);
      });
      return { mode: "weekdays", days: ds.size ? ds : new Set([1, 2, 3, 4, 5]) };
    }
    if (["daily", "weekly", "monthly", "yearly"].includes(s)) {
      return { mode: s, days: new Set([1, 2, 3, 4, 5]) };
    }
    return { mode: "", days: new Set([1, 2, 3, 4, 5]) };
  };
  const initial = parseRecurring(event?.recurring);
  const [recurMode, setRecurMode] = useState<string>(initial.mode);
  const [recurDays, setRecurDays] = useState<Set<number>>(initial.days);

  const buildRecurring = (): string | null => {
    if (!recurMode) return null;
    if (recurMode === "weekdays") {
      const sorted = [...recurDays].sort();
      if (sorted.length === 0) return null;
      const isMonFri = sorted.length === 5
        && sorted[0] === 1 && sorted[4] === 5;
      return isMonFri ? "weekdays" : `weekdays:${sorted.join(",")}`;
    }
    return recurMode;
  };

  async function save() {
    setSaving(true);
    setError(null);
    try {
      const startsAt = allDay ? `${date}T00:00:00` : `${date}T${startTime}:00`;
      const endsAt = allDay ? `${date}T23:59:59` : `${date}T${endTime}:00`;
      const names = attendeeNamesStr
        .split(",").map(s => s.trim()).filter(Boolean);
      const payload: any = {
        title: title.trim(),
        starts_at: startsAt,
        ends_at: endsAt,
        all_day: allDay,
        color,
        category: category || null,
        person: person || null,
        notes: notes || null,
        location: location.trim() || null,
        allowed_roles: allowedRoles,
        calendar_id: calendarId,
        visibility,
        recurring: buildRecurring(),
        attendee_user_ids: [...attendeeUserIds],
        attendee_names: names,
      };
      if (isNew) {
        await api.post(`/api/events?role=${ROLE}`, payload);
      } else {
        await api.patch(`/api/events/${event!.id}?role=${ROLE}`, payload);
      }
      onSaved();
    } catch (e: any) {
      setError(e.message || "save failed");
    } finally { setSaving(false); }
  }

  async function del() {
    if (!event) return;
    if (!confirm(`Delete "${event.title}"?`)) return;
    setSaving(true);
    try {
      await api.delete(`/api/events/${event.id}?role=${ROLE}`);
      onSaved();
    } catch (e: any) {
      setError(e.message || "delete failed");
      setSaving(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-[800] flex items-center justify-center bg-black/60 backdrop-blur-sm p-4"
      onClick={onClose}
    >
      <div
        className="bg-card border border-border rounded-2xl shadow-2xl w-full max-w-xl max-h-[90vh] overflow-y-auto"
        onClick={e => e.stopPropagation()}
      >
        <header className="px-6 py-5 border-b border-border flex items-center justify-between">
          <div>
            <h2 className="font-semibold text-base">{isNew ? "New event" : "Edit event"}</h2>
            <p className="text-xs text-muted-foreground mt-0.5">
              {date && new Date(date + "T12:00:00").toLocaleDateString([], { weekday: "long", day: "numeric", month: "long" })}
            </p>
          </div>
          <button
            onClick={onClose}
            className="p-2.5 md:p-1.5 hover:bg-muted rounded-md text-muted-foreground min-w-[44px] min-h-[44px] md:min-w-0 md:min-h-0 flex items-center justify-center"
            aria-label="Close"
          >
            <X className="w-5 h-5 md:w-4 md:h-4" />
          </button>
        </header>

        {/* RSVP banner — only when the current user is an attendee whose
            response is still needed. Sits above the form so it's the
            first thing seen after clicking the invitation notification. */}
        {myAttendee && (myAttendee.response_status === "needs_action"
                        || myAttendee.response_status === "tentative") && (
          <div className="px-6 py-3 bg-amber-500/10 border-b border-amber-500/30 flex items-center gap-2 text-xs">
            <UsersRound className="w-3.5 h-3.5 text-amber-600 shrink-0" />
            <span className="flex-1">You're invited — does this time work?</span>
            <button
              onClick={() => rsvp("accepted")}
              className="px-2.5 py-1 rounded-md bg-emerald-500 text-white text-[11px] font-medium hover:opacity-90 flex items-center gap-1"
            >
              <Check className="w-3 h-3" /> Accept
            </button>
            <button
              onClick={() => {
                const t = window.prompt("Propose a new start time (HH:MM)", startTime);
                if (!t) return;
                const iso = `${date}T${t}:00`;
                rsvp("tentative", iso);
              }}
              className="px-2.5 py-1 rounded-md border border-border bg-card text-[11px] hover:bg-muted"
            >
              Propose…
            </button>
            <button
              onClick={() => rsvp("declined")}
              className="px-2.5 py-1 rounded-md border border-border bg-card text-[11px] hover:bg-red-500/10 hover:text-red-500 hover:border-red-500/30 flex items-center gap-1"
            >
              <X className="w-3 h-3" /> Decline
            </button>
          </div>
        )}

        {/* Inviter view: roster of who's RSVP'd and how. */}
        {!isNew && allAttendees.length > 0 && (
          <div className="px-6 py-2 bg-muted/30 border-b border-border flex flex-wrap items-center gap-1.5">
            <span className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mr-1">
              Attendees
            </span>
            {allAttendees.map(a => {
              const label = a.user_name || a.person_name || "?";
              const pillCls = {
                accepted:     "bg-emerald-500/15 text-emerald-600 border-emerald-500/30",
                declined:     "bg-red-500/15 text-red-500 border-red-500/30 line-through",
                tentative:    "bg-amber-500/15 text-amber-600 border-amber-500/30",
                needs_action: "bg-muted text-muted-foreground border-border",
              }[a.response_status];
              return (
                <span
                  key={a.id}
                  className={cn("text-[10px] px-1.5 py-0.5 rounded-full border", pillCls)}
                  title={a.proposed_time_iso
                    ? `Proposed: ${a.proposed_time_iso.slice(11, 16)}`
                    : a.response_status.replace("_", " ")}
                >
                  {label}
                  {a.proposed_time_iso && <> · ↩ {a.proposed_time_iso.slice(11, 16)}</>}
                </span>
              );
            })}
          </div>
        )}

        <div className="p-6 space-y-4 text-sm">
          {/* Conferencing detection — surfaces a Join button when the
              location or notes contain a Zoom/Meet/Teams/Jitsi URL.
              Lives ABOVE the form because joining a meeting is the
              primary action for an open conferencing event, not the
              fields. */}
          {(() => {
            const link = detectConferenceUrl(`${location}\n${notes}`);
            if (!link) return null;
            return (
              <a
                href={link.url}
                target="_blank"
                rel="noopener"
                className="-mt-2 mb-1 inline-flex items-center gap-2 px-3 py-2 rounded-lg bg-sky-500/10 hover:bg-sky-500/20 border border-sky-500/30 text-sky-600 dark:text-sky-400 transition w-full"
                title={link.url}
              >
                <Video className="w-4 h-4 shrink-0" />
                <span className="font-medium">Join meeting</span>
                <span className="text-[11px] opacity-70 truncate">· {link.kind}</span>
              </a>
            );
          })()}

          {/* Conflict warning — surfaces when the proposed window
              overlaps an existing event on the same day. Soft warning
              (doesn't block save), since conflicts are sometimes
              intentional (back-to-back, overlap with travel block). */}
          {!allDay && (() => {
            const conflicts = detectConflicts({
              eventId:    event?.id,
              dateISO:    date,
              startTime,
              endTime,
              dayEvents:  dayEvents || [],
            });
            if (conflicts.length === 0) return null;
            return (
              <div className="-mt-2 mb-1 flex items-start gap-2 p-2.5 rounded-lg bg-amber-500/10 border border-amber-500/30 text-amber-700 dark:text-amber-400">
                <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
                <div className="flex-1 min-w-0 text-[11px] leading-relaxed">
                  <div className="font-semibold mb-0.5">
                    Conflicts with {conflicts.length} other event{conflicts.length === 1 ? "" : "s"}:
                  </div>
                  {conflicts.slice(0, 3).map(c => (
                    <div key={c.id} className="truncate">
                      · {c.title} ({(c.starts_at || "").slice(11, 16)}–{(c.ends_at || "").slice(11, 16)})
                    </div>
                  ))}
                  {conflicts.length > 3 && (
                    <div className="opacity-70">… and {conflicts.length - 3} more</div>
                  )}
                </div>
              </div>
            );
          })()}

          <Field label="Title">
            <input
              value={title}
              onChange={e => setTitle(e.target.value)}
              placeholder="What's happening?"
              className="w-full h-10 px-3 rounded-md bg-muted text-sm focus:outline-none focus:ring-2 focus:ring-ring/40"
              autoFocus
            />
          </Field>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <Field label="Date">
              <input
                type="date"
                value={date}
                onChange={e => setDate(e.target.value)}
                className="w-full h-10 sm:h-9 px-2 rounded-md bg-muted text-sm focus:outline-none focus:ring-2 focus:ring-ring/40"
              />
            </Field>
            <Field label="Category">
              <div className="flex flex-wrap gap-1.5 mt-1.5">
                {CATEGORY_ORDER.map(cat => {
                  const sw = CATEGORY_PALETTE[cat];
                  const sel = category === cat;
                  return (
                    <button
                      key={cat}
                      type="button"
                      onClick={() => setCategory(sel ? "" : cat)}
                      title={sw.label}
                      className={cn(
                        "text-[10px] px-2 py-1 rounded-md border transition flex items-center gap-1.5",
                        sel
                          ? "border-foreground font-medium"
                          : "border-border hover:border-foreground/40",
                      )}
                      style={sel ? {
                        background: sw.fill,
                        color: sw.accent,
                        borderColor: sw.accent,
                      } : undefined}
                    >
                      <span
                        className="w-2 h-2 rounded-full"
                        style={{ background: sw.accent }}
                      />
                      {sw.label}
                    </button>
                  );
                })}
              </div>
              {!category && (
                <div className="flex gap-1.5 mt-2 items-center">
                  <span className="text-[10px] text-muted-foreground mr-1">Color:</span>
                  {EVENT_COLORS.map(c => (
                    <button
                      key={c}
                      onClick={() => setColor(c)}
                      aria-label={`color ${c}`}
                      className={cn(
                        "w-5 h-5 rounded-full transition",
                        color === c ? "ring-2 ring-foreground ring-offset-1 ring-offset-card" : "hover:scale-110",
                      )}
                      style={{ background: c }}
                    />
                  ))}
                </div>
              )}
            </Field>
          </div>

          <label className="flex items-center gap-2 text-xs">
            <input
              type="checkbox"
              checked={allDay}
              onChange={e => setAllDay(e.target.checked)}
              className="w-4 h-4"
            />
            All day
          </label>

          {!allDay && (
            <div className="grid grid-cols-2 gap-3">
              <Field label="Starts">
                <input
                  type="time"
                  value={startTime}
                  onChange={e => setStartTime(e.target.value)}
                  className="w-full h-10 sm:h-9 px-2 rounded-md bg-muted text-sm focus:outline-none focus:ring-2 focus:ring-ring/40"
                />
              </Field>
              <Field label="Ends">
                <input
                  type="time"
                  value={endTime}
                  onChange={e => setEndTime(e.target.value)}
                  className="w-full h-10 sm:h-9 px-2 rounded-md bg-muted text-sm focus:outline-none focus:ring-2 focus:ring-ring/40"
                />
              </Field>
            </div>
          )}

          <Field label="Person">
            <select
              value={person}
              onChange={e => setPerson(e.target.value)}
              className="w-full h-9 px-2 rounded-md bg-muted text-sm focus:outline-none focus:ring-2 focus:ring-ring/40"
            >
              <option value="">(not specified)</option>
              <option value="all">Everyone</option>
              <option value="admin">Admin</option>
              <option value="member">Member</option>
              <option value="child">Child</option>
              <option value="employee">Employee</option>
            </select>
          </Field>

          <Field label="Location (optional)">
            <div className="relative">
              <MapPin className="w-3.5 h-3.5 text-muted-foreground absolute left-2.5 top-1/2 -translate-y-1/2 pointer-events-none" />
              <input
                value={location}
                onChange={e => setLocation(e.target.value)}
                placeholder={'e.g. "Dr. Miller Dental, 7 Main St, Boston"'}
                className="w-full h-9 pl-8 pr-3 rounded-md bg-muted text-sm focus:outline-none focus:ring-2 focus:ring-ring/40"
              />
            </div>
            {event?.travel_time_s && event.travel_time_s > 0 ? (
              <div className="mt-1.5 text-[11px] text-muted-foreground flex items-center gap-1.5">
                <Car className="w-3 h-3 text-amber-600" />
                <span>
                  {formatTravelTime(event.travel_time_s)}
                  {event.travel_distance_m
                    ? ` · ${(event.travel_distance_m/1000).toFixed(1)} km`
                    : ""}
                  {" "}driving from your home address
                  {event.travel_provider ? ` (via ${event.travel_provider.toUpperCase()})` : ""}
                </span>
              </div>
            ) : location && !event ? (
              <div className="mt-1.5 text-[11px] text-muted-foreground">
                Travel time will be computed after saving.
              </div>
            ) : null}
          </Field>

          <Field label="Notes">
            <textarea
              value={notes}
              onChange={e => setNotes(e.target.value)}
              rows={3}
              className="w-full px-3 py-2 rounded-md bg-muted text-sm focus:outline-none focus:ring-2 focus:ring-ring/40 resize-none"
            />
          </Field>

          <Field label="Repeat">
            <select
              value={recurMode}
              onChange={e => setRecurMode(e.target.value)}
              className="w-full h-9 px-2 rounded-md bg-muted text-sm focus:outline-none focus:ring-2 focus:ring-ring/40"
            >
              <option value="">One-time (don't repeat)</option>
              <option value="daily">Daily</option>
              <option value="weekly">Weekly (same weekday)</option>
              <option value="weekdays">On specific weekdays…</option>
              <option value="monthly">Monthly</option>
              <option value="yearly">Yearly</option>
            </select>
            {recurMode === "weekdays" && (
              <div className="mt-2 flex flex-wrap gap-1.5">
                {[
                  { d: 1, label: "Mon" }, { d: 2, label: "Tue" }, { d: 3, label: "Wed" },
                  { d: 4, label: "Thu" }, { d: 5, label: "Fri" }, { d: 6, label: "Sat" },
                  { d: 7, label: "Sun" },
                ].map(({ d, label }) => {
                  const on = recurDays.has(d);
                  return (
                    <button
                      key={d}
                      type="button"
                      onClick={() => {
                        const next = new Set(recurDays);
                        if (on) next.delete(d); else next.add(d);
                        setRecurDays(next);
                      }}
                      className={cn(
                        "w-9 h-9 rounded-md border text-xs font-medium transition",
                        on
                          ? "bg-primary text-primary-foreground border-primary"
                          : "border-border text-muted-foreground hover:text-foreground hover:border-foreground/40",
                      )}
                    >
                      {label}
                    </button>
                  );
                })}
                <div className="basis-full flex gap-2 mt-1">
                  <button
                    type="button"
                    onClick={() => setRecurDays(new Set([1, 2, 3, 4, 5]))}
                    className="text-[10px] text-muted-foreground hover:text-foreground underline-offset-2 hover:underline"
                  >
                    Weekdays
                  </button>
                  <span className="text-[10px] text-muted-foreground">·</span>
                  <button
                    type="button"
                    onClick={() => setRecurDays(new Set([6, 7]))}
                    className="text-[10px] text-muted-foreground hover:text-foreground underline-offset-2 hover:underline"
                  >
                    Weekend
                  </button>
                  <span className="text-[10px] text-muted-foreground">·</span>
                  <button
                    type="button"
                    onClick={() => setRecurDays(new Set([1, 2, 3, 4, 5, 6, 7]))}
                    className="text-[10px] text-muted-foreground hover:text-foreground underline-offset-2 hover:underline"
                  >
                    All
                  </button>
                </div>
              </div>
            )}
            {!isNew && (event?.recurring || recurMode) && (
              <div className="mt-1.5 text-[11px] text-muted-foreground">
                Changes apply to the entire series.
              </div>
            )}
          </Field>

          {/* Calendar overlay extensions: which calendar, who's invited,
              visibility. The free/busy preview lives just below so the
              user sees conflicts as they type. */}
          <Field label="Calendar">
            <select
              value={calendarId ?? ""}
              onChange={e => setCalendarId(parseInt(e.target.value, 10))}
              className="w-full h-9 px-2 rounded-md bg-muted text-sm focus:outline-none focus:ring-2 focus:ring-ring/40"
            >
              {writableCals.map(c => (
                <option key={c.id} value={c.id}>
                  {c.kind === "shared" ? "👥 " : ""}{c.name}
                </option>
              ))}
            </select>
          </Field>

          <Field label="Attendees (other accounts)">
            <AttendeeUserPicker
              selected={attendeeUserIds}
              onChange={setAttendeeUserIds}
            />
          </Field>

          <Field label="Other attendees (kids, guests — comma-separated)">
            <input
              value={attendeeNamesStr}
              onChange={e => setAttendeeNamesStr(e.target.value)}
              placeholder="e.g. Mia, Tom"
              className="w-full h-9 px-3 rounded-md bg-muted text-sm focus:outline-none focus:ring-2 focus:ring-ring/40"
            />
          </Field>

          {attendeeUserIds.size > 0 && !allDay && (
            <FreebusyPreview
              userIds={[...attendeeUserIds]}
              date={date}
              startTime={startTime}
              endTime={endTime}
            />
          )}

          <Field label="Privacy">
            <div className="flex gap-2">
              <button
                type="button"
                onClick={() => setVisibility("default")}
                className={cn(
                  "flex-1 h-9 rounded-md text-xs font-medium border transition",
                  visibility === "default"
                    ? "bg-primary text-primary-foreground border-primary"
                    : "border-border text-muted-foreground hover:text-foreground"
                )}
              >
                Default
              </button>
              <button
                type="button"
                onClick={() => setVisibility("private")}
                className={cn(
                  "flex-1 h-9 rounded-md text-xs font-medium border transition flex items-center justify-center gap-1.5",
                  visibility === "private"
                    ? "bg-amber-500 text-white border-amber-500"
                    : "border-border text-muted-foreground hover:text-foreground"
                )}
              >
                🔒 Private
              </button>
            </div>
          </Field>

          <Field label="Visible to (comma-separated roles)">
            <input
              value={allowedRoles}
              onChange={e => setAllowedRoles(e.target.value)}
              className="w-full h-9 px-3 rounded-md bg-muted text-sm focus:outline-none focus:ring-2 focus:ring-ring/40"
            />
          </Field>

          {error && (
            <div className="text-sm text-destructive bg-destructive/10 p-2 rounded border-l-2 border-destructive">
              {error}
            </div>
          )}
        </div>

        <footer className="px-6 py-4 border-t border-border flex items-center justify-between gap-2">
          {!isNew ? (
            <button
              onClick={del}
              disabled={saving}
              className="px-3 h-9 text-xs text-destructive hover:bg-destructive/10 rounded-md flex items-center gap-1.5"
            >
              <Trash2 className="w-3.5 h-3.5" /> Delete
            </button>
          ) : <span />}
          <div className="flex gap-2">
            <button onClick={onClose}
              className="px-4 h-9 text-sm rounded-md hover:bg-muted">Cancel</button>
            <button
              onClick={save}
              disabled={!title.trim() || saving}
              className="px-4 h-9 text-sm rounded-md bg-primary text-primary-foreground hover:opacity-90 disabled:opacity-50 flex items-center gap-2"
            >
              {saving && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
              {isNew ? "Create event" : "Save changes"}
            </button>
          </div>
        </footer>
      </div>
    </div>
  );
}

// ───────────────────────── assignee picker ──────────────────────────

function AssigneePicker({
  users, selected, everyone, currentUserId, onChange,
}: {
  users: AssignableUser[];
  selected: Set<number>;
  everyone: boolean;
  currentUserId?: number;
  onChange: (selected: Set<number>, everyone: boolean) => void;
}) {
  const [popoverOpen, setPopoverOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);

  // Close on outside click.
  useEffect(() => {
    if (!popoverOpen) return;
    function onDoc(e: MouseEvent) {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) {
        setPopoverOpen(false);
      }
    }
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [popoverOpen]);

  function toggle(id: number) {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    onChange(next, false); // any individual toggle disables "everyone"
  }

  const display = everyone
    ? [{ id: -1, name: "Everyone" }]
    : Array.from(selected)
        .map(id => users.find(u => u.id === id))
        .filter((u): u is AssignableUser => !!u);

  return (
    <div ref={wrapRef} className="relative">
      <div
        onClick={() => setPopoverOpen(o => !o)}
        className="min-h-[2.25rem] w-full px-2 py-1 rounded-md bg-muted flex flex-wrap items-center gap-1 cursor-pointer hover:ring-2 hover:ring-ring/30 transition"
      >
        {display.length === 0 && (
          <span className="text-muted-foreground text-xs italic px-1">Pick people…</span>
        )}
        {display.map(u => (
          <AssigneeChip
            key={u.id}
            name={u.name}
            isMe={u.id === currentUserId}
            isEveryone={u.id === -1}
            onRemove={(e) => {
              e.stopPropagation();
              if (u.id === -1) {
                onChange(new Set(), false);
              } else {
                toggle(u.id);
              }
            }}
          />
        ))}
      </div>

      {popoverOpen && (
        <div className="absolute z-30 top-full left-0 right-0 mt-1 bg-card border border-border rounded-md shadow-lg max-h-64 overflow-y-auto">
          <button
            onClick={() => {
              onChange(new Set(), true);
              setPopoverOpen(false);
            }}
            className="w-full flex items-center gap-2 px-3 py-2 text-sm hover:bg-muted/60 border-b border-border"
          >
            <span className={cn(
              "w-4 h-4 rounded border-2 flex items-center justify-center",
              everyone ? "bg-primary border-primary" : "border-muted-foreground/40",
            )}>
              {everyone && <Check className="w-3 h-3 text-primary-foreground" />}
            </span>
            <span className="font-medium">Everyone</span>
            <span className="text-xs text-muted-foreground ml-auto">all {users.length} users</span>
          </button>
          {users.map(u => {
            const isSel = !everyone && selected.has(u.id);
            return (
              <button
                key={u.id}
                onClick={() => { toggle(u.id); }}
                className="w-full flex items-center gap-2 px-3 py-2 text-sm hover:bg-muted/60"
              >
                <span className={cn(
                  "w-4 h-4 rounded border-2 flex items-center justify-center shrink-0",
                  isSel ? "bg-primary border-primary" : "border-muted-foreground/40",
                )}>
                  {isSel && <Check className="w-3 h-3 text-primary-foreground" />}
                </span>
                <UserAvatar name={u.name} size="sm" />
                <span className="flex-1 truncate text-left">{u.name}</span>
                {u.id === currentUserId && (
                  <span className="text-[10px] text-muted-foreground">you</span>
                )}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

function AssigneeChip({ name, isMe, isEveryone, onRemove }:
  { name: string; isMe?: boolean; isEveryone?: boolean; onRemove: (e: React.MouseEvent) => void }) {
  return (
    <span className={cn(
      "inline-flex items-center gap-1 pl-1 pr-1.5 py-0.5 rounded-full text-xs font-medium",
      isEveryone
        ? "bg-primary/20 text-primary"
        : isMe ? "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400" : "bg-card border border-border",
    )}>
      {!isEveryone && <UserAvatar name={name} size="xs" />}
      {isEveryone && <span className="text-[12px]">👥</span>}
      <span>{name}</span>
      <button
        onClick={onRemove}
        className="opacity-60 hover:opacity-100 hover:bg-foreground/10 rounded-full w-3.5 h-3.5 flex items-center justify-center"
        aria-label={`Remove ${name}`}
      >
        <X className="w-2.5 h-2.5" />
      </button>
    </span>
  );
}

function UserAvatar({ name, size = "sm" }:
  { name: string; size?: "xs" | "sm" }) {
  const initials = (name || "?")
    .split(/\s+/).filter(Boolean).slice(0, 2)
    .map(s => s[0]).join("").toUpperCase();
  let h = 0;
  for (let i = 0; i < name.length; i++) h = ((h << 5) - h) + name.charCodeAt(i);
  const hue = Math.abs(h) % 360;
  const dim = size === "xs" ? "w-4 h-4 text-[9px]" : "w-5 h-5 text-[10px]";
  return (
    <span className={cn("rounded-full flex items-center justify-center font-semibold shrink-0", dim)}
      style={{ background: `hsl(${hue} 50% 50% / 0.25)`, color: `hsl(${hue} 60% 50%)` }}>
      {initials}
    </span>
  );
}

// ───────────────────────── task dialog ─────────────────────────────

function TaskDialog({
  task, defaultDate, onClose, onSaved,
}: {
  task: Task | null;
  defaultDate: Date;
  onClose: () => void;
  onSaved: () => void;
}) {
  const isNew = !task;
  const [title, setTitle] = useState(task?.title || "");
  const [dueDate, setDueDate] = useState(task?.due_date?.slice(0, 10) || isoDate(defaultDate));
  const [hasDueDate, setHasDueDate] = useState(!!task?.due_date);
  const [done, setDone] = useState(!!task?.done);
  const [category, setCategory] = useState(task?.category || "");
  const [notes, setNotes] = useState(task?.notes || "");
  const [allowedRoles, setAllowedRoles] = useState(task?.allowed_roles || "admin,member");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Assignees as a Set of user ids. Initialised from the task's
  // existing assignees on edit; on create, default to the current
  // user once we've fetched them.
  const [assigneeIds, setAssigneeIds] = useState<Set<number>>(
    () => new Set(task?.assignees?.map(a => a.user_id) || [])
  );
  const [assignEveryone, setAssignEveryone] = useState(false);
  const { data: me } = useApi<{ user?: { id: number; name: string } }>("/api/auth/me", []);
  const { data: assignableUsers } = useApi<AssignableUser[]>("/api/users/assignable", []);

  // On create, default the assignee set to the current user once both
  // /auth/me and /users/assignable have loaded (so we know who's logged in).
  useEffect(() => {
    if (isNew && me?.user?.id && assigneeIds.size === 0 && !assignEveryone) {
      setAssigneeIds(new Set([me.user.id]));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [me?.user?.id, isNew]);

  async function save() {
    setSaving(true);
    setError(null);
    try {
      const payload: any = {
        title: title.trim(),
        due_date: hasDueDate ? dueDate : null,
        done: done ? 1 : 0,
        category: category || null,
        notes: notes || null,
        allowed_roles: allowedRoles,
        assign_everyone: assignEveryone,
        assignee_user_ids: assignEveryone ? [] : Array.from(assigneeIds),
      };
      if (isNew) {
        await api.post(`/api/tasks?role=${ROLE}`, payload);
      } else {
        await api.patch(`/api/tasks/${task!.id}?role=${ROLE}`, payload);
      }
      onSaved();
    } catch (e: any) {
      setError(e.message || "save failed");
    } finally { setSaving(false); }
  }

  async function del() {
    if (!task) return;
    if (!confirm(`Delete task "${task.title}"?`)) return;
    setSaving(true);
    try {
      await api.delete(`/api/tasks/${task.id}?role=${ROLE}`);
      onSaved();
    } catch (e: any) {
      setError(e.message || "delete failed");
      setSaving(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-[800] flex items-center justify-center bg-black/60 backdrop-blur-sm p-4"
      onClick={onClose}
    >
      <div
        className="bg-card border border-border rounded-2xl shadow-2xl w-full max-w-md max-h-[90vh] overflow-y-auto"
        onClick={e => e.stopPropagation()}
      >
        <header className="px-6 py-5 border-b border-border flex items-center justify-between">
          <div>
            <h2 className="font-semibold text-base">{isNew ? "New task" : "Edit task"}</h2>
            {hasDueDate && (
              <p className="text-xs text-muted-foreground mt-0.5">
                Due {new Date(dueDate.slice(0, 10) + "T12:00:00").toLocaleDateString([], { weekday: "long", day: "numeric", month: "long" })}
              </p>
            )}
          </div>
          <button onClick={onClose} className="p-1.5 hover:bg-muted rounded-md text-muted-foreground">
            <X className="w-4 h-4" />
          </button>
        </header>

        <div className="p-6 space-y-4 text-sm">
          <Field label="Title">
            <input
              value={title}
              onChange={e => setTitle(e.target.value)}
              placeholder="What needs to be done?"
              className="w-full h-10 px-3 rounded-md bg-muted text-sm focus:outline-none focus:ring-2 focus:ring-ring/40"
              autoFocus
            />
          </Field>

          <label className="flex items-center gap-2 text-xs">
            <input
              type="checkbox"
              checked={done}
              onChange={e => setDone(e.target.checked)}
              className="w-4 h-4"
            />
            Done
          </label>

          <label className="flex items-center gap-2 text-xs">
            <input
              type="checkbox"
              checked={hasDueDate}
              onChange={e => setHasDueDate(e.target.checked)}
              className="w-4 h-4"
            />
            Has due date
          </label>

          {hasDueDate && (
            <Field label="Due date">
              <input
                type="date"
                value={dueDate}
                onChange={e => setDueDate(e.target.value)}
                className="w-full h-9 px-2 rounded-md bg-muted text-sm focus:outline-none focus:ring-2 focus:ring-ring/40"
              />
            </Field>
          )}

          <Field label="Assign to">
            <AssigneePicker
              users={assignableUsers || []}
              selected={assigneeIds}
              everyone={assignEveryone}
              currentUserId={me?.user?.id}
              onChange={(ids, every) => {
                setAssigneeIds(ids);
                setAssignEveryone(every);
              }}
            />
            <small className="muted block mt-1">
              {assignEveryone
                ? "Everyone (all users will be notified)"
                : assigneeIds.size === 0
                  ? "Nobody — task won't show in anyone's list"
                  : assigneeIds.size === 1 && me?.user && assigneeIds.has(me.user.id)
                    ? "Just you"
                    : `${assigneeIds.size} ${assigneeIds.size === 1 ? "person" : "people"} — they'll get a notification`}
            </small>
          </Field>

          <Field label="Category">
            <input
              value={category}
              onChange={e => setCategory(e.target.value)}
              placeholder="e.g. shopping"
              className="w-full h-9 px-2 rounded-md bg-muted text-sm focus:outline-none focus:ring-2 focus:ring-ring/40"
            />
          </Field>

          <Field label="Notes">
            <textarea
              value={notes}
              onChange={e => setNotes(e.target.value)}
              rows={3}
              className="w-full px-3 py-2 rounded-md bg-muted text-sm focus:outline-none focus:ring-2 focus:ring-ring/40 resize-none"
            />
          </Field>

          <Field label="Visible to (comma-separated roles)">
            <input
              value={allowedRoles}
              onChange={e => setAllowedRoles(e.target.value)}
              className="w-full h-9 px-3 rounded-md bg-muted text-sm focus:outline-none focus:ring-2 focus:ring-ring/40"
            />
          </Field>

          {error && (
            <div className="text-sm text-destructive bg-destructive/10 p-2 rounded border-l-2 border-destructive">
              {error}
            </div>
          )}
        </div>

        <footer className="px-6 py-4 border-t border-border flex items-center justify-between gap-2">
          {!isNew ? (
            <button
              onClick={del}
              disabled={saving}
              className="px-3 h-9 text-xs text-destructive hover:bg-destructive/10 rounded-md flex items-center gap-1.5"
            >
              <Trash2 className="w-3.5 h-3.5" /> Delete
            </button>
          ) : <span />}
          <div className="flex gap-2">
            <button onClick={onClose}
              className="px-4 h-9 text-sm rounded-md hover:bg-muted">Cancel</button>
            <button
              onClick={save}
              disabled={!title.trim() || saving}
              className="px-4 h-9 text-sm rounded-md bg-primary text-primary-foreground hover:opacity-90 disabled:opacity-50 flex items-center gap-2"
            >
              {saving && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
              {isNew ? "Create task" : "Save changes"}
            </button>
          </div>
        </footer>
      </div>
    </div>
  );
}

const EVENT_COLORS = [
  "#818cf8", // indigo (default)
  "#22c55e", // green
  "#f59e0b", // amber
  "#ef4444", // red
  "#3b82f6", // blue
  "#ec4899", // pink
  "#a855f7", // purple
  "#14b8a6", // teal
];

// ───────────────────────── widget atoms ──────────────────────────────

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="text-[10px] font-semibold text-muted-foreground uppercase tracking-wider block mb-1">
        {label}
      </span>
      {children}
    </label>
  );
}

/** One-time, dismissible "new feature" tip for travel times. Auto-hides
 *  as soon as the user has at least one event with a computed
 *  travel_time_s (they discovered the feature on their own) OR after
 *  they explicitly dismiss via the X button. */
const TRAVEL_TIP_DISMISS_KEY = "yorik.calendar.travelTimeTipDismissed";

function TravelTimeAnnouncement({ events }: { events: CalendarEvent[] }) {
  const [dismissed, setDismissed] = useState<boolean>(() => {
    try { return localStorage.getItem(TRAVEL_TIP_DISMISS_KEY) === "1"; }
    catch { return false; }
  });
  if (dismissed) return null;
  // If the user already has any event with a travel time, they've
  // discovered the feature — silently hide forever.
  const hasAnyTravelEvent = events.some(e => (e.travel_time_s ?? 0) > 0);
  if (hasAnyTravelEvent) {
    try { localStorage.setItem(TRAVEL_TIP_DISMISS_KEY, "1"); } catch {}
    return null;
  }
  function dismiss() {
    setDismissed(true);
    try { localStorage.setItem(TRAVEL_TIP_DISMISS_KEY, "1"); } catch {}
  }
  return (
    <div className="px-6 py-2.5 border-b border-border bg-amber-500/[0.06]">
      <div className="flex items-center gap-3">
        <div className="w-8 h-8 rounded-full bg-amber-500/15 flex items-center justify-center shrink-0">
          <Car className="w-4 h-4 text-amber-600" />
        </div>
        <div className="flex-1 min-w-0 text-xs leading-relaxed">
          <span className="font-semibold text-foreground">New:</span>{" "}
          <span className="text-foreground/85">
            Yorik automatically calculates travel time and departure time for
            events with a location. Add a <strong>Location</strong> to your
            next event — e.g. <em>"Dentist in Boston"</em> — and see what happens.
          </span>
          <span className="text-muted-foreground">
            {" "}Optional: even more accurate with an{" "}
            <a
              href="https://openrouteservice.org/dev/#/signup"
              target="_blank" rel="noopener"
              className="underline hover:text-foreground"
            >OpenRouteService API key</a>{" "}
            under Settings → Connectors → Maps.
          </span>
        </div>
        <button
          onClick={dismiss}
          className="text-muted-foreground hover:text-foreground transition p-1 rounded shrink-0"
          title="Don't show again"
        >
          <X className="w-3.5 h-3.5" />
        </button>
      </div>
    </div>
  );
}

function NavBtn({ icon: Icon, onClick, title }:
  { icon: any; onClick: () => void; title: string }) {
  return (
    <button
      onClick={onClick}
      title={title}
      aria-label={title}
      className="w-10 h-10 md:w-8 md:h-8 rounded-md hover:bg-muted text-muted-foreground flex items-center justify-center"
    >
      <Icon className="w-5 h-5 md:w-4 md:h-4" />
    </button>
  );
}


// ─── conference URL detection (used by EventDialog header) ──────
//
// Returns the first match found in the given text — checks the
// big four meeting providers. Generous patterns: most users paste
// the raw URL from their calendar invite, no normalisation needed.
function detectConferenceUrl(text: string): { url: string; kind: string } | null {
  if (!text) return null;
  const patterns: Array<{ kind: string; re: RegExp }> = [
    { kind: "Zoom",  re: /https?:\/\/[\w.-]*zoom\.us\/(?:j|my|meeting)\/\S+/i },
    { kind: "Meet",  re: /https?:\/\/meet\.google\.com\/[a-z]+-[a-z]+-[a-z]+(?:\?\S*)?/i },
    { kind: "Teams", re: /https?:\/\/teams\.microsoft\.com\/l\/meetup-join\/\S+/i },
    { kind: "Jitsi", re: /https?:\/\/(?:meet\.jit\.si|[\w.-]*jitsi[\w.-]*)\/\S+/i },
    { kind: "Whereby", re: /https?:\/\/[\w.-]*whereby\.com\/\S+/i },
    { kind: "BigBlueButton", re: /https?:\/\/[\w.-]*\/bbb\/\S+/i },
  ];
  for (const { kind, re } of patterns) {
    const m = text.match(re);
    if (m) return { url: m[0], kind };
  }
  return null;
}


// ─── conflict detection (used by EventDialog warning chip) ──────
//
// Naive interval-overlap on the day's events. Skip the event being
// edited (so saving without changes doesn't warn about itself).
// All-day events on the same day are flagged as conflicting too
// since they consume the whole window.
function detectConflicts({
  eventId, dateISO, startTime, endTime, dayEvents,
}: {
  eventId?: number;
  dateISO: string;
  startTime: string;
  endTime: string;
  dayEvents: CalendarEvent[];
}): CalendarEvent[] {
  const startISO = `${dateISO}T${startTime}:00`;
  const endISO   = `${dateISO}T${endTime}:00`;
  if (endISO <= startISO) return [];   // user is mid-edit; ignore
  return dayEvents.filter(ev => {
    if (eventId && ev.id === eventId) return false;
    if (ev.all_day) return true;
    const a = ev.starts_at || "";
    const b = ev.ends_at   || "";
    if (!a || !b) return false;
    // Standard overlap test: A starts before B ends AND A ends after B starts.
    return a < endISO && b > startISO;
  });
}


// ─── Quick-add overlay (⌘K) ────────────────────────────────────
//
// One textarea, calls /api/events/parse-natural, hands the parsed
// fields back to the parent which opens the EventDialog pre-filled.
// Stateless on its own — the parent owns the parsed result.

function EventQuickAddModal({
  onClose, onParsed,
}: {
  onClose: () => void;
  onParsed: (p: {
    title: string;
    date?: string;
    start_time?: string;
    end_time?: string;
    duration_minutes?: number;
    all_day?: boolean;
    location?: string;
    attendee_names?: string[];
  }) => void;
}) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    function esc(e: KeyboardEvent) { if (e.key === "Escape") onClose(); }
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [onClose]);

  async function submit() {
    if (!text.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      const r = await api.post<{
        title: string;
        date?: string;
        start_time?: string;
        end_time?: string;
        duration_minutes?: number;
        all_day?: boolean;
        location?: string;
        attendee_names?: string[];
      }>("/api/events/parse-natural", { text });
      onParsed(r);
    } catch (e: any) {
      setError(e?.message || String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-[1000] bg-black/60 backdrop-blur-sm flex items-start justify-center pt-12 md:pt-24 px-4 md:px-6"
         onClick={onClose}>
      <div className="bg-card border border-border rounded-2xl shadow-2xl w-full max-w-xl overflow-hidden"
           onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between px-4 py-2 border-b border-border">
          <div className="flex items-center gap-2">
            <Sparkles className="w-4 h-4 text-violet-500" />
            <span className="text-sm font-semibold">Quick-add event</span>
          </div>
          <button onClick={onClose} className="p-1 rounded text-muted-foreground hover:text-foreground hover:bg-muted">
            <X className="w-4 h-4" />
          </button>
        </div>
        <div className="p-4 space-y-2">
          <textarea
            autoFocus
            value={text}
            onChange={e => setText(e.target.value)}
            onKeyDown={e => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                void submit();
              } else if (e.key === "Enter" && !e.shiftKey && !e.metaKey && !e.ctrlKey) {
                e.preventDefault();
                void submit();
              }
            }}
            disabled={busy}
            placeholder='Lunch with Hans at Tartine tomorrow 12:30 for 90 min'
            rows={3}
            className="w-full bg-muted/50 border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring/40 resize-none"
          />
          <div className="flex items-center justify-between text-[11px] text-muted-foreground">
            <span>Yorik resolves date, time, location, attendees — you confirm before save.</span>
            <span>Enter to parse · Esc cancel</span>
          </div>
          {error && (
            <div className="text-[11px] text-rose-500">{error}</div>
          )}
          <div className="flex justify-end">
            <button
              type="button"
              onClick={() => void submit()}
              disabled={!text.trim() || busy}
              className="h-8 px-3 rounded-md bg-violet-500 text-white text-xs font-medium hover:opacity-90 disabled:opacity-50 flex items-center gap-1.5"
            >
              {busy
                ? <><Loader2 className="w-3 h-3 animate-spin" /> Yorik is reading…</>
                : <><Sparkles className="w-3 h-3" /> Parse + review</>}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}


// ─── Search modal (/) ───────────────────────────────────────────
//
// Debounced /api/events/search hit list. On pick, parent jumps to
// that day, flips to day view, flashes the highlight.

function EventSearchModal({
  onClose, onPick,
}: {
  onClose: () => void;
  onPick: (ev: CalendarEvent) => void;
}) {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<CalendarEvent[]>([]);
  const [loading, setLoading] = useState(false);
  const [highlight, setHighlight] = useState(0);

  useEffect(() => {
    function esc(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setHighlight(h => Math.min(h + 1, Math.max(0, hits.length - 1)));
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setHighlight(h => Math.max(0, h - 1));
      }
      if (e.key === "Enter" && hits[highlight]) {
        e.preventDefault();
        onPick(hits[highlight]);
      }
    }
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [onClose, onPick, hits, highlight]);

  // Debounced fetch. 150ms keeps the popover snappy for short queries.
  useEffect(() => {
    if (!q.trim()) { setHits([]); return; }
    const handle = window.setTimeout(async () => {
      setLoading(true);
      try {
        const r = await api.get<CalendarEvent[]>(
          `/api/events/search?q=${encodeURIComponent(q)}&limit=20`,
        );
        setHits(r);
        setHighlight(0);
      } catch {
        setHits([]);
      } finally {
        setLoading(false);
      }
    }, 150);
    return () => window.clearTimeout(handle);
  }, [q]);

  return (
    <div className="fixed inset-0 z-[1000] bg-black/60 backdrop-blur-sm flex items-start justify-center pt-12 md:pt-24 px-4 md:px-6"
         onClick={onClose}>
      <div className="bg-card border border-border rounded-2xl shadow-2xl w-full max-w-xl overflow-hidden"
           onClick={e => e.stopPropagation()}>
        <div className="flex items-center gap-2 px-3 py-2 border-b border-border">
          <Search className="w-4 h-4 text-muted-foreground" />
          <input
            autoFocus
            value={q}
            onChange={e => setQ(e.target.value)}
            placeholder='Search events — title, location, notes, person'
            className="flex-1 h-8 bg-transparent text-sm focus:outline-none"
          />
          {loading && <Loader2 className="w-3.5 h-3.5 animate-spin text-muted-foreground" />}
          <button onClick={onClose} className="p-1 rounded text-muted-foreground hover:text-foreground hover:bg-muted">
            <X className="w-4 h-4" />
          </button>
        </div>
        <div className="max-h-[60vh] overflow-y-auto">
          {!q.trim() && (
            <div className="px-3 py-6 text-[11px] text-muted-foreground text-center italic">
              Type to search across all events you can see.
            </div>
          )}
          {q.trim() && !loading && hits.length === 0 && (
            <div className="px-3 py-6 text-[11px] text-muted-foreground text-center italic">
              No matches for "{q}".
            </div>
          )}
          {hits.map((ev, i) => (
            <button
              key={ev.id}
              type="button"
              onMouseEnter={() => setHighlight(i)}
              onClick={() => onPick(ev)}
              className={cn(
                "w-full text-left px-3 py-2 text-xs flex items-start gap-2 transition border-b border-border last:border-b-0",
                i === highlight ? "bg-violet-500/10" : "hover:bg-muted/50",
              )}
            >
              <CalendarIcon className="w-3 h-3 text-violet-500 shrink-0 mt-0.5" />
              <div className="flex-1 min-w-0">
                <div className="font-medium truncate">{ev.title}</div>
                <div className="text-[10px] text-muted-foreground truncate">
                  {(ev.starts_at || "").slice(0, 16).replace("T", " · ")}
                  {ev.location && <> · {ev.location}</>}
                </div>
              </div>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

function ViewSwitcher({ value, onChange, options }:
  { value: ViewMode; onChange: (v: ViewMode) => void; options?: ViewMode[] }) {
  const opts: ViewMode[] = options || ["day", "week", "month"];
  return (
    <div className="inline-flex rounded-md border border-border bg-muted/30 p-0.5">
      {opts.map(o => (
        <button
          key={o}
          onClick={() => onChange(o)}
          aria-label={`${o} view`}
          aria-pressed={value === o}
          className={cn(
            "px-2 md:px-3 h-9 md:h-7 text-xs rounded capitalize transition font-medium",
            value === o
              ? "bg-card text-foreground shadow-sm"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          {o}
        </button>
      ))}
    </div>
  );
}

// ───────────────────────── time-grid view (week + day) ─────────────

// Full 24 hours visible internally. Scrollbar is hidden but scroll
// works (mouse-wheel, touchpad). Initial scroll auto-positions so
// the user lands at "now" — they can scroll up to see early hours
// or down to see late evening / next day's wrap-around.
const GRID_START_HOUR = 0;     // 00:00
const GRID_END_HOUR   = 24;    // 24:00
const HOUR_PX         = 64;
const VISIBLE_HOURS = GRID_END_HOUR - GRID_START_HOUR; // 24

function TimeGridView({
  days, today, selected, eventsByDay, highlightedIds,
  onSelectDay, onEventClick, onRangeSelected, onTaskDropped, singleDay,
}: {
  days: Date[];
  today: Date;
  selected: Date;
  eventsByDay: Map<string, CalendarEvent[]>;
  highlightedIds?: Set<number>;
  onSelectDay: (d: Date) => void;
  onEventClick: (e: CalendarEvent) => void;
  onRangeSelected: (d: Date, startMin: number, endMin: number) => void;
  onTaskDropped: (d: Date, mins: number, task: Task) => void;
  singleDay?: boolean;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);

  // Auto-scroll to current hour on mount. With the full 24h grid we
  // need this — otherwise the user lands at midnight every time.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const now = new Date();
    const minsNow = now.getHours() * 60 + now.getMinutes();
    const targetPx = Math.max(0, (minsNow - GRID_START_HOUR * 60) * (HOUR_PX / 60) - 180);
    el.scrollTop = targetPx;
  }, []);

  // Live "now" tick — re-render every 60s so the indicator stays accurate.
  const [, setNowTick] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setNowTick(x => x + 1), 60_000);
    return () => clearInterval(t);
  }, []);

  return (
    <div className="flex-1 flex flex-col min-h-0 bg-background">
      {/* Sticky day-header */}
      <div className="flex border-b border-border bg-card/40 backdrop-blur-sm shrink-0">
        <div className="w-14 shrink-0" /> {/* gutter aligning with the time-rail */}
        {days.map(d => {
          const isToday = sameDay(d, today);
          const isSel   = sameDay(d, selected);
          return (
            <button
              key={d.toISOString()}
              onClick={() => onSelectDay(d)}
              className={cn(
                "flex-1 py-2 flex flex-col items-center border-r border-border last:border-r-0 transition group",
                isSel ? "bg-primary/[0.04]" : "hover:bg-muted/30",
              )}
            >
              <span className={cn(
                "text-[10px] uppercase tracking-wider font-medium",
                isToday ? "text-primary" : "text-muted-foreground",
              )}>
                {d.toLocaleDateString([], { weekday: "short" })}
              </span>
              <span className={cn(
                "text-lg tabular-nums font-medium mt-0.5 w-9 h-9 flex items-center justify-center rounded-full",
                isToday && "bg-primary text-primary-foreground",
              )}>
                {d.getDate()}
              </span>
            </button>
          );
        })}
      </div>

      {/* All-day strip */}
      <AllDayStrip days={days} eventsByDay={eventsByDay} onEventClick={onEventClick} />

      {/* Scrollable time grid — scrollbar hidden, wheel/trackpad still scroll. */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto overflow-x-hidden no-scrollbar">
        <div className="flex relative w-full min-w-0" style={{ minHeight: VISIBLE_HOURS * HOUR_PX }}>
          {/* Time rail */}
          <div className="w-14 shrink-0 relative">
            {Array.from({ length: VISIBLE_HOURS }, (_, i) => {
              const h = GRID_START_HOUR + i;
              return (
                <div
                  key={h}
                  className="text-[10px] text-muted-foreground tabular-nums text-right pr-2 -translate-y-2"
                  style={{ height: HOUR_PX }}
                >
                  {i === 0 ? "" : `${String(h).padStart(2, "0")}:00`}
                </div>
              );
            })}
          </div>

          {/* Day columns */}
          {days.map(d => (
            <DayColumn
              key={d.toISOString()}
              day={d}
              isToday={sameDay(d, today)}
              events={eventsByDay.get(isoDate(d)) || []}
              highlightedIds={highlightedIds}
              onEventClick={onEventClick}
              onRangeSelected={onRangeSelected}
              onTaskDropped={onTaskDropped}
              wider={!!singleDay}
            />
          ))}
        </div>
      </div>
      <style>{`
        .no-scrollbar { scrollbar-width: none; -ms-overflow-style: none; }
        .no-scrollbar::-webkit-scrollbar { display: none; }
      `}</style>
    </div>
  );
}

function DayColumn({
  day, isToday, events, highlightedIds, onEventClick, onRangeSelected, onTaskDropped, wider,
}: {
  day: Date;
  isToday: boolean;
  events: CalendarEvent[];
  highlightedIds?: Set<number>;
  onEventClick: (e: CalendarEvent) => void;
  onRangeSelected: (d: Date, startMin: number, endMin: number) => void;
  onTaskDropped: (d: Date, mins: number, task: Task) => void;
  wider?: boolean;
}) {
  // Lay out non-all-day events with overlap-aware columns.
  const positioned = useMemo(() => assignColumns(
    events.filter(e => !e.all_day)
          .map(e => ({ event: e, ...minutesOf(e) }))
  ), [events]);

  const now = new Date();
  const showNowLine = isToday;
  const nowMins = now.getHours() * 60 + now.getMinutes();
  const nowTop = (nowMins - GRID_START_HOUR * 60) * (HOUR_PX / 60);

  const colRef = useRef<HTMLDivElement>(null);
  const [drag, setDrag] = useState<{ startY: number; currentY: number } | null>(null);
  const [dropping, setDropping] = useState(false);

  function yToMins(y: number): number {
    const clamped = Math.max(0, Math.min(VISIBLE_HOURS * HOUR_PX, y));
    return GRID_START_HOUR * 60 + clamped * 60 / HOUR_PX;
  }
  function snap(mins: number): number {
    return Math.round(mins / 15) * 15;
  }

  function handleMouseDown(e: React.MouseEvent) {
    // Don't start a drag if click started on an event block (those
    // have their own click handler via stopPropagation, but we double-check).
    if ((e.target as HTMLElement).closest("[data-event-block]")) return;
    if (e.button !== 0) return;
    const rect = colRef.current!.getBoundingClientRect();
    const y = e.clientY - rect.top;
    setDrag({ startY: y, currentY: y });
    e.preventDefault();
  }

  // Track mousemove + mouseup globally while dragging so the user
  // can move outside the column without losing the selection.
  useEffect(() => {
    if (!drag) return;
    function onMove(e: MouseEvent) {
      const rect = colRef.current!.getBoundingClientRect();
      const y = e.clientY - rect.top;
      setDrag(d => d ? { ...d, currentY: y } : null);
    }
    function onUp() {
      setDrag(d => {
        if (!d) return null;
        const top = Math.min(d.startY, d.currentY);
        const bot = Math.max(d.startY, d.currentY);
        const startMin = snap(yToMins(top));
        let endMin = snap(yToMins(bot));
        // Single click (no drag) → 30-min default.
        if (Math.abs(bot - top) < 4) endMin = startMin + 30;
        // Minimum 15-min event.
        if (endMin <= startMin) endMin = startMin + 15;
        onRangeSelected(day, startMin, endMin);
        return null;
      });
    }
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp, { once: true });
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, [drag, day, onRangeSelected]);

  return (
    <div
      ref={colRef}
      onMouseDown={handleMouseDown}
      onDragOver={(e) => {
        if (e.dataTransfer.types.includes("application/yorik-task")) {
          e.preventDefault();
          e.dataTransfer.dropEffect = "copy";
          setDropping(true);
        }
      }}
      onDragLeave={() => setDropping(false)}
      onDrop={(e) => {
        setDropping(false);
        const raw = e.dataTransfer.getData("application/yorik-task");
        if (!raw) return;
        try {
          const task: Task = JSON.parse(raw);
          e.preventDefault();
          const rect = colRef.current!.getBoundingClientRect();
          const y = e.clientY - rect.top;
          onTaskDropped(day, snap(yToMins(y)), task);
        } catch {}
      }}
      className={cn(
        "relative border-r border-border last:border-r-0 select-none flex-1 min-w-0",
        isToday && "bg-primary/[0.025]",
        dropping && "bg-primary/10",
        drag ? "cursor-grabbing" : "cursor-crosshair",
      )}
      style={{ height: VISIBLE_HOURS * HOUR_PX }}
    >
      {/* Hour grid lines */}
      {Array.from({ length: VISIBLE_HOURS }, (_, i) => (
        <div
          key={i}
          className="absolute left-0 right-0 border-t border-border/60 pointer-events-none"
          style={{ top: i * HOUR_PX }}
        />
      ))}

      {/* Half-hour lines (subtler) */}
      {Array.from({ length: VISIBLE_HOURS }, (_, i) => (
        <div
          key={`h-${i}`}
          className="absolute left-0 right-0 border-t border-border/20 pointer-events-none"
          style={{ top: i * HOUR_PX + HOUR_PX / 2 }}
        />
      ))}

      {/* Drag selection overlay */}
      {drag && (() => {
        const top = Math.min(drag.startY, drag.currentY);
        const height = Math.max(8, Math.abs(drag.currentY - drag.startY));
        const startMin = snap(yToMins(Math.min(drag.startY, drag.currentY)));
        const endMin = snap(yToMins(Math.max(drag.startY, drag.currentY)));
        return (
          <div
            className="absolute left-0.5 right-0.5 z-30 pointer-events-none rounded-md bg-primary/20 border border-primary/50 shadow-lg"
            style={{ top, height }}
          >
            <div className="text-[10px] tabular-nums font-semibold text-primary px-2 py-1">
              {minsToTime(startMin)} – {minsToTime(Math.max(startMin + 15, endMin))}
            </div>
          </div>
        );
      })()}

      {/* Now indicator */}
      {showNowLine && nowTop >= 0 && nowTop <= VISIBLE_HOURS * HOUR_PX && (
        <div
          className="absolute left-0 right-0 z-20 pointer-events-none"
          style={{ top: nowTop }}
        >
          <div className="relative">
            <span className="absolute -left-1 -top-1 w-2.5 h-2.5 rounded-full bg-red-500 shadow" />
            <div className="border-t border-red-500" />
          </div>
        </div>
      )}

      {/* Event blocks */}
      {positioned.map(({ event, startMin, endMin, column, columnCount }) => (
        <EventBlock
          key={`${event.id}_${event.occurrence_date || ""}`}
          event={event}
          startMin={startMin}
          endMin={endMin}
          column={column}
          columnCount={columnCount}
          highlighted={!!highlightedIds?.has(event.id)}
          onClick={(e) => { e.stopPropagation(); onEventClick(event); }}
        />
      ))}
    </div>
  );
}

function EventBlock({
  event, startMin, endMin, column, columnCount, highlighted, onClick,
}: {
  event: CalendarEvent;
  startMin: number;
  endMin: number;
  column: number;
  columnCount: number;
  highlighted?: boolean;
  onClick: (e: React.MouseEvent) => void;
}) {
  // Clip the block to the visible window so an event starting at 5am
  // doesn't get drawn at negative top.
  const visStart = Math.max(startMin, GRID_START_HOUR * 60);
  const visEnd   = Math.min(endMin, GRID_END_HOUR * 60);
  if (visEnd <= visStart) return null;

  const top    = (visStart - GRID_START_HOUR * 60) * (HOUR_PX / 60);
  const height = Math.max(22, (visEnd - visStart) * (HOUR_PX / 60));
  const widthPct = 100 / columnCount;
  const leftPct  = column * widthPct;
  const visualOf = useEventVisualFn();
  const vis = visualOf(event, !!highlighted);

  return (
    <button
      data-event-block
      onClick={onClick}
      onMouseDown={(e) => e.stopPropagation()}
      className={cn(
        "absolute z-10 text-left rounded-md p-1.5 overflow-hidden cursor-pointer hover:brightness-110 hover:shadow-lg transition shadow-sm",
        highlighted && "ring-2 ring-violet-400 ring-offset-1 animate-pulse-flash z-20",
      )}
      style={{
        top,
        height,
        left:  `calc(${leftPct}% + 2px)`,
        width: `calc(${widthPct}% - 4px)`,
        background: vis.fill,
        borderLeft: `3px solid ${vis.border}`,
        color: vis.accent,
      }}
    >
      {highlighted && (
        <style>{`
          @keyframes pulse-flash {
            0%, 100% { transform: scale(1); }
            50% { transform: scale(1.04); }
          }
          .animate-pulse-flash { animation: pulse-flash 1s ease-in-out 3; }
        `}</style>
      )}
      <div className="text-[11px] font-semibold leading-tight truncate">
        {event.title || "(no title)"}
      </div>
      {height > 32 && (
        <div className="text-[10px] tabular-nums opacity-80 mt-0.5">
          {minsToTime(startMin)}{event.ends_at && ` – ${minsToTime(endMin)}`}
        </div>
      )}
    </button>
  );
}

function AllDayStrip({
  days, eventsByDay, onEventClick,
}: {
  days: Date[];
  eventsByDay: Map<string, CalendarEvent[]>;
  onEventClick: (e: CalendarEvent) => void;
}) {
  // Only render the strip if at least one day has an all-day event.
  const anyAllDay = days.some(d => (eventsByDay.get(isoDate(d)) || []).some(e => e.all_day));
  if (!anyAllDay) return null;
  const visualOf = useEventVisualFn();
  return (
    <div className="flex border-b border-border bg-muted/20 shrink-0">
      <div className="w-14 shrink-0 text-[9px] uppercase text-muted-foreground text-right pr-2 pt-1.5 font-medium tracking-wider">
        all-day
      </div>
      {days.map(d => {
        const items = (eventsByDay.get(isoDate(d)) || []).filter(e => e.all_day);
        return (
          <div key={d.toISOString()} className="flex-1 py-1.5 px-1 border-r border-border last:border-r-0 space-y-1 min-h-[28px]">
            {items.map(e => (
              <button
                key={`${e.id}_${e.occurrence_date || ""}`}
                onClick={(ev) => { ev.stopPropagation(); onEventClick(e); }}
                className="block w-full text-left text-[11px] px-2 py-0.5 rounded font-medium truncate transition hover:brightness-110"
                style={(() => {
                  const v = visualOf(e, false);
                  return {
                    background: v.fill,
                    borderLeft: `2px solid ${v.border}`,
                    color: v.accent,
                  };
                })()}
              >
                {e.title}
              </button>
            ))}
          </div>
        );
      })}
    </div>
  );
}

// Minutes-from-midnight start/end of an event.
function minutesOf(e: CalendarEvent): { startMin: number; endMin: number } {
  const start = e.starts_at;
  const end = e.ends_at || e.starts_at;
  const startMin = parseInt(start.slice(11, 13)) * 60 + parseInt(start.slice(14, 16));
  const endMin = end ? parseInt(end.slice(11, 13)) * 60 + parseInt(end.slice(14, 16)) : startMin + 30;
  return { startMin, endMin: Math.max(endMin, startMin + 15) };
}

interface Positioned {
  event: CalendarEvent;
  startMin: number;
  endMin: number;
  column: number;
  columnCount: number;
}

/** Greedy overlap-aware column assignment. Events that overlap get
 *  split side-by-side; events that don't overlap take full width.
 *  Standard algorithm used by every calendar app — packs left-most. */
function assignColumns(items: Array<{ event: CalendarEvent; startMin: number; endMin: number }>): Positioned[] {
  if (items.length === 0) return [];
  const sorted = [...items].sort((a, b) =>
    a.startMin - b.startMin || a.endMin - b.endMin
  );
  const out: Positioned[] = [];
  let cluster: typeof sorted = [];
  let clusterEnd = 0;
  function flush() {
    if (!cluster.length) return;
    // Within the cluster: assign each event the leftmost column whose
    // last event has already ended.
    const colEnds: number[] = [];
    const assigned: number[] = [];
    for (const it of cluster) {
      let placed = -1;
      for (let c = 0; c < colEnds.length; c++) {
        if (colEnds[c] <= it.startMin) { placed = c; break; }
      }
      if (placed === -1) { placed = colEnds.length; colEnds.push(0); }
      colEnds[placed] = it.endMin;
      assigned.push(placed);
    }
    const count = colEnds.length;
    cluster.forEach((it, i) => out.push({
      ...it, column: assigned[i], columnCount: count,
    }));
    cluster = [];
    clusterEnd = 0;
  }
  for (const it of sorted) {
    if (it.startMin >= clusterEnd && cluster.length > 0) flush();
    cluster.push(it);
    clusterEnd = Math.max(clusterEnd, it.endMin);
  }
  flush();
  return out;
}

function minsToTime(mins: number): string {
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}

function viewTitle(view: ViewMode, anchor: Date, gridStart: Date): string {
  if (view === "month") {
    return anchor.toLocaleDateString([], { month: "long", year: "numeric" });
  }
  if (view === "week") {
    const end = addDays(gridStart, 6);
    const sameMonth = gridStart.getMonth() === end.getMonth();
    const sameYear = gridStart.getFullYear() === end.getFullYear();
    if (sameMonth) {
      return `${gridStart.toLocaleDateString([], { month: "long", day: "numeric" })} – ${end.getDate()}, ${end.getFullYear()}`;
    }
    if (sameYear) {
      return `${gridStart.toLocaleDateString([], { month: "short", day: "numeric" })} – ${end.toLocaleDateString([], { month: "short", day: "numeric" })}, ${end.getFullYear()}`;
    }
    return `${gridStart.toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" })} – ${end.toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" })}`;
  }
  // day
  return anchor.toLocaleDateString([], { weekday: "long", month: "long", day: "numeric", year: "numeric" });
}

function addDays(d: Date, n: number): Date {
  const r = new Date(d); r.setDate(r.getDate() + n); return r;
}
function startOfDay(d: Date): Date {
  const r = new Date(d); r.setHours(0, 0, 0, 0); return r;
}

// ───────────────────────── date helpers ──────────────────────────────

function startOfMonth(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth(), 1);
}
function startOfWeek(d: Date, mondayFirst: boolean): Date {
  const r = new Date(d);
  const day = r.getDay();
  const offset = mondayFirst ? (day === 0 ? -6 : 1 - day) : -day;
  r.setDate(r.getDate() + offset);
  r.setHours(0, 0, 0, 0);
  return r;
}
function sameDay(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear()
      && a.getMonth() === b.getMonth()
      && a.getDate() === b.getDate();
}
function isoDate(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}
function addMonths(d: Date, n: number): Date {
  const r = new Date(d);
  r.setMonth(r.getMonth() + n);
  return r;
}
function shortDay(iso: string): string {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const now = new Date();
  if (sameDay(d, now)) return "Today";
  const y = new Date(now); y.setDate(y.getDate() - 1);
  if (sameDay(d, y)) return "Yesterday";
  return d.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
}
// Pick visual colours for an event: category palette wins (LLM- or
// user-assigned), per-event hex is the override, accent is the default.
// One function so the three render sites stay consistent.
// Context for "this is the current user's calendar map" so event-
// rendering helpers can tell, glanceably, whether an event lives on a
// calendar the viewer owns or one shared from someone else. When the
// event is on a non-owned calendar, eventVisual tints the fill with
// the calendar's own color (kept distinct from category colors via
// migration 030) while still showing the event's category as the left
// stripe. Net effect: your own day stays category-coloured exactly as
// before; Sara's events overlayed onto your day look unmistakably
// hers.
const CalendarVisualContext = createContext<{
  calendarsById: Map<number, Calendar>;
}>({ calendarsById: new Map() });

/** Helper for the components that render events: returns a (ev,
 *  highlighted) → visual function bound to the current calendars map.
 *  Use this instead of calling eventVisual directly so the shared-
 *  event tint kicks in automatically. */
function useEventVisualFn() {
  const { calendarsById } = useContext(CalendarVisualContext);
  return useCallback(
    (ev: { color?: string | null; category?: string | null; calendar_id?: number | null },
     highlighted: boolean) => eventVisual(ev, highlighted, calendarsById),
    [calendarsById],
  );
}

function eventVisual(
  ev: { color?: string | null; category?: string | null; calendar_id?: number | null },
  highlighted: boolean,
  calendarsById?: Map<number, Calendar>,
): { accent: string; fill: string; border: string } {
  const sw = swatchFor(ev.category);
  // If we can resolve the event's calendar AND it isn't owned by the
  // viewer, the fill comes from the calendar's color so "whose" is
  // glanceable. We keep the category as the left stripe so "what kind"
  // is still readable. Falls through to plain category coloring if the
  // calendar map isn't supplied (back-compat with old call sites that
  // haven't been migrated to useEventVisualFn yet).
  const cal = ev.calendar_id != null ? calendarsById?.get(ev.calendar_id) : null;
  const isSharedView = !!(cal && cal.you_own === false);
  if (isSharedView) {
    const calColor = cal!.color || "#818cf8";
    const stripe = sw ? sw.accent : calColor;
    return {
      accent: stripe,
      fill:   hexToRgba(calColor, highlighted ? 0.36 : 0.20),
      border: stripe,
    };
  }
  if (sw) {
    return {
      accent: sw.accent,
      fill:   highlighted ? hexToRgba(sw.accent, 0.32) : sw.fill,
      border: sw.accent,
    };
  }
  const c = ev.color || "#818cf8";
  return {
    accent: c,
    fill:   hexToRgba(c, highlighted ? 0.32 : 0.18),
    border: c,
  };
}

function hexToRgba(hex: string, alpha: number): string {
  // Accept #rgb / #rrggbb. Falls back to hex string for other formats.
  const m = /^#?([0-9a-f]{3,8})$/i.exec(hex.trim());
  if (!m) return hex;
  let s = m[1];
  if (s.length === 3) s = s.split("").map(c => c + c).join("");
  if (s.length !== 6) return hex;
  const r = parseInt(s.slice(0, 2), 16);
  const g = parseInt(s.slice(2, 4), 16);
  const b = parseInt(s.slice(4, 6), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

// ═══════════════════════════════════════════════════════════════════
//  Calendar overlay — sidebar list, attendee picker, freebusy preview,
//  share modal. All driven by /api/calendars + /api/calendar/freebusy.
// ═══════════════════════════════════════════════════════════════════

function CalendarsSidebar({
  calendars, hidden, onToggle, onShare, onRefresh, onSetHidden,
}: {
  calendars: Calendar[];
  hidden: Set<number>;
  onToggle: (id: number) => void;
  onShare: (c: Calendar) => void;
  onRefresh: () => void;
  onSetHidden: (next: Set<number>) => void;
}) {
  // Look up user names so the "People" section can show "Wife's
  // calendar" with the owner's real name in a chip.
  const usersApi = useApi<AssignableUser[]>("/api/users/assignable", []);
  const userMap = useMemo(
    () => new Map((usersApi.data || []).map(u => [u.id, u.name])),
    [usersApi.data],
  );

  // Three sections — my, others', household.
  const mine      = calendars.filter(c => c.you_own && c.kind !== "shared");
  const others    = calendars.filter(c => !c.you_own && c.kind !== "shared");
  const household = calendars.filter(c => c.kind === "shared");

  if (calendars.length === 0) return null;

  // "Show everyone" flips ON every People + Household calendar in one
  // click — the "household overview" mode. Clicking again hides them.
  const sharedSet = new Set([...others, ...household].map(c => c.id));
  const allSharedHidden = [...sharedSet].every(id => hidden.has(id));
  function toggleShowEveryone() {
    const next = new Set(hidden);
    if (allSharedHidden) {
      // Reveal all
      for (const id of sharedSet) next.delete(id);
    } else {
      // Hide all
      for (const id of sharedSet) next.add(id);
    }
    onSetHidden(next);
  }

  return (
    <div className="border-t border-border mt-2 pt-4 px-5 space-y-3">
      <div className="flex items-center justify-between">
        <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold">
          Calendars
        </div>
        <button
          onClick={() => onCreateCalendarPrompt(onRefresh)}
          className="text-muted-foreground hover:text-foreground"
          title="Create a new calendar"
        >
          <Plus className="w-3 h-3" />
        </button>
      </div>

      {/* My calendars */}
      {mine.length > 0 && (
        <SidebarCalendarSection
          label="My calendars"
          calendars={mine}
          hidden={hidden}
          onToggle={onToggle}
          onShare={onShare}
        />
      )}

      {/* People — every other person's calendar I can see. */}
      {others.length > 0 && (
        <SidebarCalendarSection
          label="People"
          calendars={others}
          hidden={hidden}
          onToggle={onToggle}
          onShare={onShare}
          ownerLookup={(id) => userMap.get(id)}
        />
      )}

      {/* Shared — the non-person bucket. */}
      {household.length > 0 && (
        <SidebarCalendarSection
          label="Shared"
          calendars={household}
          hidden={hidden}
          onToggle={onToggle}
          onShare={onShare}
        />
      )}

      {/* "Show everyone" — one click to flip all People + Shared
          calendars on so the user gets the household overview. Hidden
          when there's nothing to overlay (single-user box). */}
      {sharedSet.size > 0 && (
        <button
          onClick={toggleShowEveryone}
          className={cn(
            "w-full mt-1 h-7 rounded-md text-[11px] font-medium border transition flex items-center justify-center gap-1.5",
            allSharedHidden
              ? "border-border bg-card text-muted-foreground hover:text-foreground"
              : "border-primary/30 bg-primary/10 text-primary",
          )}
          title={allSharedHidden
            ? "Overlay everyone's calendar"
            : "Hide everyone, show only yours"}
        >
          <UsersRound className="w-3 h-3" />
          {allSharedHidden ? "Show everyone" : "Hide everyone"}
        </button>
      )}

      {/* One-shot tidy: move events that ended up on Shared (the
          migration-010 default for legacy events) to your Personal
          calendar. Only useful right after the model change — after
          users move their own events, this becomes a no-op. */}
      <MoveLegacyEventsButton onMoved={onRefresh} />
    </div>
  );
}

function MoveLegacyEventsButton({ onMoved }: { onMoved: () => void }) {
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<{ moved: number } | null>(null);
  async function go() {
    if (!window.confirm(
      "Move every event currently on Shared that you created or were invited to into your Personal calendar?\n\n" +
      "Events nobody owns (truly household items) stay on Shared.")) return;
    setBusy(true);
    try {
      const r = await api.post<{ moved: number }>("/api/calendar/move-mine-from-shared");
      setDone(r);
      onMoved();
    } catch (e: any) {
      alert(`Move failed: ${e.message || e}`);
    } finally { setBusy(false); }
  }
  if (done) {
    return (
      <div className="text-[10px] text-emerald-600 px-1 pt-1">
        Moved {done.moved} event{done.moved === 1 ? "" : "s"} to your Personal calendar.
      </div>
    );
  }
  return (
    <button
      onClick={go}
      disabled={busy}
      className="w-full mt-1 h-7 rounded-md text-[10px] text-muted-foreground hover:text-foreground border border-dashed border-border hover:border-foreground/30 transition flex items-center justify-center gap-1"
      title="One-shot tidy: pull your events out of Shared into Personal"
    >
      {busy ? <Loader2 className="w-3 h-3 animate-spin" /> : "↪"}
      Move my events out of Shared
    </button>
  );
}

function SidebarCalendarSection({
  label, calendars, hidden, onToggle, onShare, ownerLookup,
}: {
  label: string;
  calendars: Calendar[];
  hidden: Set<number>;
  onToggle: (id: number) => void;
  onShare: (c: Calendar) => void;
  ownerLookup?: (uid: number) => string | undefined;
}) {
  return (
    <div>
      <div className="text-[9px] uppercase tracking-[0.12em] text-muted-foreground/70 font-semibold mb-1 px-0.5">
        {label}
      </div>
      <div className="space-y-0.5">
        {calendars.map(c => {
          const isHidden = hidden.has(c.id);
          const ownerName = ownerLookup ? ownerLookup(c.owner_user_id) : undefined;
          return (
            <div
              key={c.id}
              className="group flex items-center gap-2 py-1 px-1 -mx-1 rounded-md hover:bg-muted/40"
            >
              <button
                onClick={() => onToggle(c.id)}
                className="flex items-center gap-2 flex-1 min-w-0 text-left"
                title={isHidden ? "Show in grid" : "Hide from grid"}
              >
                <span
                  className={cn(
                    "w-2.5 h-2.5 rounded-full shrink-0 transition",
                    isHidden && "opacity-30",
                  )}
                  style={{ background: c.color }}
                />
                <span className={cn(
                  "text-xs truncate flex-1",
                  isHidden && "line-through opacity-50",
                )}>
                  {ownerName ? ownerName : c.name}
                  {ownerName && c.kind !== "personal" && (
                    <span className="opacity-50 ml-1">· {c.name}</span>
                  )}
                </span>
                {c.access_level === "free_busy" && (
                  <span className="text-[9px] uppercase tracking-wider text-muted-foreground opacity-60">
                    busy only
                  </span>
                )}
                {c.access_level === "read" && (
                  <span className="text-[9px] uppercase tracking-wider text-muted-foreground opacity-60">
                    read
                  </span>
                )}
              </button>
              {c.you_own && (
                <button
                  onClick={() => onShare(c)}
                  className="p-1 opacity-0 group-hover:opacity-100 transition text-muted-foreground hover:text-foreground"
                  title="Share this calendar"
                >
                  <Share2 className="w-3 h-3" />
                </button>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

async function onCreateCalendarPrompt(onRefresh: () => void): Promise<void> {
  const name = window.prompt("New calendar name");
  if (!name) return;
  try {
    await api.post<Calendar>("/api/calendars", {
      name: name.trim(), kind: "project",
      color: "#a78bfa",
    });
    onRefresh();
  } catch (e: any) {
    alert(`Create failed: ${e.message || e}`);
  }
}

function AttendeeUserPicker({
  selected, onChange,
}: {
  selected: Set<number>;
  onChange: (next: Set<number>) => void;
}) {
  // Pull household user profiles from the existing assignable-users
  // endpoint — the tasks app uses the same list.
  const usersApi = useApi<AssignableUser[]>("/api/users/assignable", []);
  const users = usersApi.data || [];
  if (users.length === 0) {
    return <div className="text-xs text-muted-foreground italic">No other accounts in this box.</div>;
  }
  function toggle(uid: number) {
    const next = new Set(selected);
    if (next.has(uid)) next.delete(uid); else next.add(uid);
    onChange(next);
  }
  return (
    <div className="flex flex-wrap gap-1.5">
      {users.map(u => {
        const on = selected.has(u.id);
        return (
          <button
            key={u.id}
            type="button"
            onClick={() => toggle(u.id)}
            className={cn(
              "text-[11px] px-2.5 py-1 rounded-full border transition flex items-center gap-1.5",
              on
                ? "bg-primary text-primary-foreground border-primary"
                : "bg-card border-border text-muted-foreground hover:text-foreground",
            )}
          >
            {on && <Check className="w-2.5 h-2.5" />}
            {u.name}
          </button>
        );
      })}
    </div>
  );
}

function FreebusyPreview({
  userIds, date, startTime, endTime,
}: {
  userIds: number[];
  date: string;
  startTime: string;
  endTime: string;
}) {
  // Fetch the day's blocks for the selected attendees and check for
  // overlap with the chosen window. Updates on time / date / attendee
  // change — debounced lightly so dragging the time picker isn't a
  // firestorm of requests.
  const [blocks, setBlocks] = useState<FreebusyBlocks>({});
  const [loading, setLoading] = useState(false);
  const usersApi = useApi<AssignableUser[]>("/api/users/assignable", []);
  const userMap = useMemo(
    () => new Map((usersApi.data || []).map(u => [u.id, u.name])),
    [usersApi.data],
  );
  useEffect(() => {
    const handle = window.setTimeout(async () => {
      if (userIds.length === 0) return;
      setLoading(true);
      try {
        const from = `${date}T00:00:00`;
        const to   = `${date}T23:59:59`;
        const params = new URLSearchParams({
          users: userIds.join(","), from, to,
        });
        const r = await api.get<FreebusyBlocks>(`/api/calendar/freebusy?${params}`);
        setBlocks(r);
      } catch { setBlocks({}); }
      finally { setLoading(false); }
    }, 250);
    return () => window.clearTimeout(handle);
  }, [userIds.join(","), date]);

  function timeToMin(t: string) {
    const [h, m] = t.split(":").map(Number);
    return h * 60 + (m || 0);
  }
  function isoToMin(iso: string) {
    if (!iso) return 0;
    const d = new Date(iso);
    return d.getHours() * 60 + d.getMinutes();
  }

  const slotStart = timeToMin(startTime);
  const slotEnd   = timeToMin(endTime);

  return (
    <div className="rounded-md border border-border bg-muted/30 p-2.5">
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-1.5 flex items-center justify-between">
        <span>Free / busy on {new Date(date + "T12:00:00").toLocaleDateString([], { weekday: "long", day: "numeric", month: "short" })}</span>
        {loading && <Loader2 className="w-3 h-3 animate-spin" />}
      </div>
      <div className="space-y-1">
        {userIds.map(uid => {
          const userBlocks = blocks[String(uid)] || [];
          const overlap = userBlocks.some(b => {
            const bs = isoToMin(b.start);
            const be = isoToMin(b.end);
            return bs < slotEnd && be > slotStart;
          });
          return (
            <div key={uid} className="flex items-center gap-2 text-[11px]">
              <span className="w-20 truncate text-muted-foreground">
                {userMap.get(uid) || `User ${uid}`}
              </span>
              {/* Horizontal hour ruler — 24h compressed to ~180px. */}
              <div className="relative h-3 flex-1 rounded bg-background border border-border overflow-hidden">
                {userBlocks.map((b, i) => {
                  const bs = isoToMin(b.start);
                  const be = isoToMin(b.end);
                  const left  = (bs / 1440) * 100;
                  const width = Math.max(((be - bs) / 1440) * 100, 0.5);
                  return (
                    <div
                      key={i}
                      className="absolute top-0 bottom-0 bg-muted-foreground/40"
                      style={{ left: `${left}%`, width: `${width}%` }}
                      title={`${b.start.slice(11, 16)} – ${b.end.slice(11, 16)}`}
                    />
                  );
                })}
                {/* Proposed slot — colored overlay so the user sees the
                    clash (or absence of one) at a glance. */}
                <div
                  className={cn(
                    "absolute top-0 bottom-0 border-l border-r",
                    overlap
                      ? "bg-red-500/40 border-red-500"
                      : "bg-emerald-500/40 border-emerald-500",
                  )}
                  style={{
                    left:  `${(slotStart / 1440) * 100}%`,
                    width: `${Math.max(((slotEnd - slotStart) / 1440) * 100, 0.5)}%`,
                  }}
                />
              </div>
              {overlap
                ? <span className="text-[10px] text-red-500 font-medium">Busy</span>
                : <span className="text-[10px] text-emerald-500 font-medium">Free</span>}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function ShareCalendarModal({
  calendar, onClose, onChanged,
}: {
  calendar: Calendar;
  onClose: () => void;
  onChanged: () => void;
}) {
  const sharesApi = useApi<CalendarShare[]>(`/api/calendars/${calendar.id}/shares`, [calendar.id]);
  const shares = sharesApi.data || [];
  const usersApi = useApi<AssignableUser[]>("/api/users/assignable", []);
  const users = usersApi.data || [];
  // Users not yet shared with — exclude the calendar owner too.
  const sharedUserIds = new Set(shares.map(s => s.user_id));
  const available = users.filter(u => !sharedUserIds.has(u.id) && u.id !== calendar.owner_user_id);
  const [pendingUserId, setPendingUserId] = useState<number | null>(null);
  const [pendingLevel,  setPendingLevel]  = useState<"free_busy" | "read" | "write">("free_busy");
  const [saving, setSaving] = useState(false);

  async function addShare() {
    if (!pendingUserId) return;
    setSaving(true);
    try {
      await api.post(`/api/calendars/${calendar.id}/shares`, {
        user_id: pendingUserId, access_level: pendingLevel,
      });
      setPendingUserId(null); setPendingLevel("free_busy");
      sharesApi.refetch();
      onChanged();
    } catch (e: any) {
      alert(`Share failed: ${e.message || e}`);
    } finally { setSaving(false); }
  }

  async function changeLevel(s: CalendarShare, level: "free_busy" | "read" | "write") {
    try {
      await api.post(`/api/calendars/${calendar.id}/shares`, {
        user_id: s.user_id, access_level: level,
      });
      sharesApi.refetch();
      onChanged();
    } catch (e: any) {
      alert(`Update failed: ${e.message || e}`);
    }
  }

  async function removeShare(s: CalendarShare) {
    if (!confirm(`Stop sharing "${calendar.name}" with ${s.name}?`)) return;
    try {
      await api.delete(`/api/calendars/${calendar.id}/shares/${s.user_id}`);
      sharesApi.refetch();
      onChanged();
    } catch (e: any) {
      alert(`Remove failed: ${e.message || e}`);
    }
  }

  return (
    <div
      className="fixed inset-0 z-[900] flex items-center justify-center bg-black/60 backdrop-blur-sm p-4"
      onClick={onClose}
    >
      <div
        className="bg-card border border-border rounded-2xl shadow-2xl w-full max-w-md max-h-[90vh] overflow-y-auto"
        onClick={e => e.stopPropagation()}
      >
        <header className="px-6 py-5 border-b border-border flex items-center justify-between">
          <div className="flex items-center gap-2">
            <span className="w-3 h-3 rounded-full" style={{ background: calendar.color }} />
            <h2 className="font-semibold text-base">Share "{calendar.name}"</h2>
          </div>
          <button onClick={onClose} className="p-1.5 hover:bg-muted rounded-md text-muted-foreground">
            <X className="w-4 h-4" />
          </button>
        </header>

        <div className="p-6 space-y-4 text-sm">
          {/* Existing shares */}
          {shares.length === 0 && (
            <div className="text-xs text-muted-foreground italic">
              No one else can see this calendar yet.
            </div>
          )}
          {shares.map(s => (
            <div key={s.user_id} className="flex items-center gap-3 py-1.5">
              <div className="w-8 h-8 rounded-full bg-muted flex items-center justify-center text-xs font-semibold shrink-0">
                {(s.name || "?").trim().split(/\s+/).slice(0, 2).map(t => t[0]?.toUpperCase()).join("")}
              </div>
              <div className="flex-1 min-w-0">
                <div className="text-sm truncate">{s.name}</div>
                <div className="text-[10px] text-muted-foreground truncate">{s.email}</div>
              </div>
              <select
                value={s.access_level}
                onChange={e => changeLevel(s, e.target.value as any)}
                className="h-8 px-2 bg-muted border border-border rounded-md text-xs focus:outline-none"
              >
                <option value="free_busy">🕐 Busy only</option>
                <option value="read">👁 See details</option>
                <option value="write">✏️ Edit</option>
              </select>
              <button
                onClick={() => removeShare(s)}
                className="p-1.5 text-muted-foreground hover:text-red-500"
                title="Stop sharing"
              >
                <Trash2 className="w-3.5 h-3.5" />
              </button>
            </div>
          ))}

          {/* Add new share */}
          {available.length > 0 && (
            <div className="border-t border-border pt-4 space-y-2">
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold">
                Add member
              </div>
              <div className="flex gap-2">
                <select
                  value={pendingUserId || ""}
                  onChange={e => setPendingUserId(parseInt(e.target.value, 10) || null)}
                  className="flex-1 h-9 px-2 bg-muted border border-border rounded-md text-sm focus:outline-none"
                >
                  <option value="">Pick a member…</option>
                  {available.map(u => (
                    <option key={u.id} value={u.id}>{u.name}</option>
                  ))}
                </select>
              </div>
              {/* Three radio-style cards, one-line explanations */}
              <div className="grid grid-cols-3 gap-1.5">
                {([
                  { v: "free_busy", emoji: "🕐", label: "Busy only", desc: "See when, not what" },
                  { v: "read",      emoji: "👁", label: "Details",   desc: "Read titles & notes" },
                  { v: "write",     emoji: "✏️", label: "Edit",      desc: "Add & change events" },
                ] as const).map(opt => (
                  <button
                    key={opt.v}
                    type="button"
                    onClick={() => setPendingLevel(opt.v)}
                    className={cn(
                      "text-left p-2 rounded-md border transition",
                      pendingLevel === opt.v
                        ? "bg-primary/10 border-primary/40"
                        : "border-border hover:bg-muted/50",
                    )}
                  >
                    <div className="text-sm">{opt.emoji} <b className="font-medium text-xs">{opt.label}</b></div>
                    <div className="text-[10px] text-muted-foreground mt-0.5">{opt.desc}</div>
                  </button>
                ))}
              </div>
              <button
                onClick={addShare}
                disabled={!pendingUserId || saving}
                className="w-full h-9 rounded-md bg-primary text-primary-foreground text-sm font-medium hover:opacity-90 disabled:opacity-50 flex items-center justify-center gap-1.5"
              >
                {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Share2 className="w-3.5 h-3.5" />}
                Share
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
