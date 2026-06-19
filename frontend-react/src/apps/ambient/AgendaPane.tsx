/**
 * Household agenda pane — slides in from the right when the user
 * swipes right on /ambient. Lists today's events from every household
 * member who has consented (Settings → Profile → "Show my appointments
 * on the household wall"). Each row shows time · title · owner's
 * first name so anyone glancing at the wall sees who's doing what.
 *
 * User-agnostic surface — no PIN, no cookie identity beyond the
 * kiosk-scope check the backend already applies. Closes on:
 *   - tap on the dim backdrop
 *   - swipe-left (handled by AmbientApp's gesture dispatcher)
 *   - 30 s of no interaction (auto-dismiss so the wall returns to
 *     photos when the kitchen empties)
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { Calendar, Loader2, MapPin, X } from "lucide-react";
import { api } from "@/lib/api";

interface AgendaEvent {
  id:         number;
  title:      string;
  starts_at:  string;    // naked local-time ISO from the backend
  ends_at:    string | null;
  location:   string | null;
  owner: {
    id:         number;
    name:       string;
    first_name: string;
  };
}

interface Props {
  onClose: () => void;
}

const AUTO_DISMISS_MS = 30_000;

export function AgendaPane({ onClose }: Props) {
  const [events, setEvents] = useState<AgendaEvent[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const closeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Fetch + arm the auto-dismiss timer
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await api.get<{ events: AgendaEvent[] }>("/api/ambient/agenda");
        if (cancelled) return;
        setEvents(r.events || []);
      } catch (e: any) {
        if (cancelled) return;
        setError(e?.message || String(e));
        setEvents([]);
      }
    })();
    closeTimerRef.current = setTimeout(onClose, AUTO_DISMISS_MS);
    return () => {
      cancelled = true;
      if (closeTimerRef.current) clearTimeout(closeTimerRef.current);
    };
  }, [onClose]);

  function resetAutoDismiss() {
    if (closeTimerRef.current) clearTimeout(closeTimerRef.current);
    closeTimerRef.current = setTimeout(onClose, AUTO_DISMISS_MS);
  }

  return (
    // Backdrop — tap to close. Pointer events caught here AND
    // bubbled-through to children for pane interactions.
    <div
      className="fixed inset-0 z-30 flex justify-end"
      onClick={onClose}
      onPointerDown={resetAutoDismiss}
    >
      <div
        className="absolute inset-0 bg-black/55 backdrop-blur-sm"
        aria-hidden="true"
      />
      {/* Pane — slides in from the right. Stop click propagation
          so tapping inside doesn't dismiss. */}
      <div
        className="relative h-full w-full max-w-md bg-card text-foreground shadow-2xl flex flex-col"
        style={{ animation: "agenda-slide-in 280ms cubic-bezier(0.32, 0.72, 0, 1) forwards" }}
        onClick={(e) => e.stopPropagation()}
      >
        <style>{`
          @keyframes agenda-slide-in {
            from { transform: translateX(100%); }
            to   { transform: translateX(0); }
          }
        `}</style>

        <header className="px-6 pt-6 pb-4 flex items-center justify-between border-b border-border">
          <div>
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-1">
              Today
            </div>
            <h2 className="text-2xl font-light">{formatDayLabel()}</h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="h-10 w-10 rounded-full bg-muted/60 hover:bg-muted flex items-center justify-center"
            aria-label="Close"
          >
            <X className="w-5 h-5" />
          </button>
        </header>

        <div className="flex-1 overflow-y-auto px-6 py-4">
          {events === null && (
            <div className="h-full flex items-center justify-center text-muted-foreground">
              <Loader2 className="w-5 h-5 animate-spin" />
            </div>
          )}

          {events !== null && events.length === 0 && (
            <div className="h-full flex flex-col items-center justify-center text-center text-muted-foreground px-6">
              <Calendar className="w-10 h-10 mb-3 opacity-50" />
              <div className="text-base font-medium text-foreground mb-1">
                Nothing on the wall today
              </div>
              <p className="text-xs leading-relaxed max-w-xs">
                Nobody in the household has events today, or nobody's flipped
                "show my appointments on the household wall" in Settings →
                Profile yet.
              </p>
              {error && (
                <p className="text-[11px] text-rose-500/80 mt-3">
                  {error}
                </p>
              )}
            </div>
          )}

          {events !== null && events.length > 0 && (
            <ul className="space-y-2">
              {events.map(ev => <AgendaRow key={ev.id} ev={ev} />)}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}

function AgendaRow({ ev }: { ev: AgendaEvent }) {
  const time = formatTime(ev.starts_at);
  const endTime = ev.ends_at ? formatTime(ev.ends_at) : null;
  const initial = (ev.owner.first_name?.[0] || ev.owner.name?.[0] || "?").toUpperCase();
  const ownerColor = useMemo(() => ownerHue(ev.owner.id), [ev.owner.id]);

  return (
    <li className="flex items-stretch gap-3 py-3 border-b border-border/40 last:border-b-0">
      <div className="text-right w-16 shrink-0 pt-0.5">
        <div className="text-sm font-semibold tabular-nums">{time}</div>
        {endTime && (
          <div className="text-[10px] text-muted-foreground tabular-nums">{endTime}</div>
        )}
      </div>
      <div className="flex-1 min-w-0">
        <div className="text-base font-medium leading-tight">{ev.title}</div>
        {ev.location && (
          <div className="flex items-center gap-1 mt-1 text-xs text-muted-foreground truncate">
            <MapPin className="w-3 h-3 shrink-0" />
            <span className="truncate">{ev.location}</span>
          </div>
        )}
      </div>
      <div className="flex flex-col items-center gap-1 shrink-0 pt-0.5">
        <div
          className="w-8 h-8 rounded-full flex items-center justify-center text-xs font-semibold text-white shadow"
          style={{ background: ownerColor }}
          title={ev.owner.name}
        >
          {initial}
        </div>
        <div className="text-[10px] text-muted-foreground max-w-[60px] truncate">
          {ev.owner.first_name || ev.owner.name}
        </div>
      </div>
    </li>
  );
}

// Hash user_id → a stable hue so each household member gets a
// consistent avatar colour across slides. Same scheme any future
// per-user UI surface can reuse.
function ownerHue(uid: number): string {
  const h = (uid * 137) % 360;
  return `linear-gradient(135deg, hsl(${h} 70% 55%), hsl(${(h + 35) % 360} 70% 45%))`;
}

function formatTime(iso: string): string {
  // Backend hands us naked local-time ISO ("2026-06-07T10:00:00").
  // new Date(local-ISO) parses as LOCAL on most engines; for safety
  // we split rather than trust the parser.
  const m = iso.match(/T(\d{2}):(\d{2})/);
  if (m) return `${m[1]}:${m[2]}`;
  try {
    const d = new Date(iso);
    return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  } catch {
    return iso;
  }
}

function formatDayLabel(): string {
  const d = new Date();
  return d.toLocaleDateString(undefined, {
    weekday: "long",
    month: "long",
    day: "numeric",
  });
}
