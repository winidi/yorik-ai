/**
 * The first-run tour: a few small notes pinned to the real buttons on
 * Home, one at a time. No full-screen window. It runs once per person;
 * "done" lives on the server (PATCH /api/me/ui-state), so a reload or a
 * second device never brings it back. Help → "Show the tour again"
 * restarts it (window event "yorik:tour").
 */
import { useEffect, useLayoutEffect, useState } from "react";
import { api } from "@/lib/api";
import { useAuth } from "@/components/AuthGate";
import { isKid } from "@/lib/dock-order";

interface Stop { target: string; title: string; text: string }

const STOPS: Stop[] = [
  { target: "ask", title: "Ask Yorik", text: "Tap here and type or say what you need: \"Remind me tomorrow at 9 to call the doctor.\"" },
  { target: "family", title: "Your family today", text: "Everyone's face and what their day holds. Tap a face to open the family board." },
  { target: "checklist", title: "A few steps left", text: "This list helps you finish setting up. It waits until you're ready." },
  { target: "dock", title: "Your apps", text: "Every app lives down here. The colours stay the same everywhere." },
  { target: "help", title: "Stuck?", text: "Tap the question mark for help on the page you're on, or just ask Yorik \"how does this work?\"" },
];
const KID_STOPS = ["family", "dock", "help"];

export function startTour() { window.dispatchEvent(new Event("yorik:tour")); }

export function FirstRunTour() {
  const role = useAuth().user?.role;
  const [stops, setStops] = useState<Stop[] | null>(null);
  const [i, setI] = useState(0);
  const [box, setBox] = useState<DOMRect | null>(null);

  function begin() {
    const wanted = isKid(role) ? STOPS.filter(s => KID_STOPS.includes(s.target)) : STOPS;
    // Only stops whose button is actually on screen (no family row yet, etc.)
    const present = wanted.filter(s => document.querySelector(`[data-tour="${s.target}"]`));
    if (present.length) { setStops(present); setI(0); }
  }

  useEffect(() => {
    let alive = true;
    api.get<{ tour_done: boolean }>("/api/me/ui-state")
      .then(st => { if (alive && !st.tour_done) setTimeout(() => alive && begin(), 900); })
      .catch(() => {});
    const again = () => begin();
    window.addEventListener("yorik:tour", again);
    return () => { alive = false; window.removeEventListener("yorik:tour", again); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [role]);

  const stop = stops?.[i];

  useLayoutEffect(() => {
    if (!stop) return;
    const el = document.querySelector(`[data-tour="${stop.target}"]`) as HTMLElement | null;
    if (!el) { setBox(null); return; }
    el.scrollIntoView({ block: "center", behavior: "smooth" });
    const place = () => setBox(el.getBoundingClientRect());
    place();
    const t = setTimeout(place, 350);
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => { clearTimeout(t); window.removeEventListener("resize", place); window.removeEventListener("scroll", place, true); };
  }, [stop]);

  function finish() {
    setStops(null);
    api.patch("/api/me/ui-state", { tour_done: true }).catch(() => {});
  }

  if (!stops || !stop || !box) return null;
  const last = i === stops.length - 1;
  const pad = 8;
  const below = box.top < window.innerHeight / 2;
  const cardTop = below ? box.bottom + pad + 12 : undefined;
  const cardBottom = below ? undefined : window.innerHeight - box.top + pad + 12;
  const cardLeft = Math.min(Math.max(16, box.left + box.width / 2 - 160), window.innerWidth - 336);

  return (
    <div className="fixed inset-0 z-[880]" aria-live="polite">
      {/* The dimmed page with a hole around the button (one big shadow). */}
      <div className="absolute rounded-2xl pointer-events-none transition-all duration-200"
           style={{ top: box.top - pad, left: box.left - pad, width: box.width + pad * 2, height: box.height + pad * 2,
                    boxShadow: "0 0 0 9999px rgba(0,0,0,0.55)" }} />
      <div className="absolute w-[320px] max-w-[calc(100vw-32px)] bg-background border border-border rounded-2xl shadow-2xl p-4"
           style={{ top: cardTop, bottom: cardBottom, left: cardLeft }} role="dialog" aria-label={stop.title}>
        <div className="text-xs text-muted-foreground mb-1">{i + 1} of {stops.length}</div>
        <div className="font-semibold mb-1">{stop.title}</div>
        <p className="text-sm text-muted-foreground">{stop.text}</p>
        <div className="flex items-center justify-between mt-4">
          <button onClick={finish} className="text-sm text-muted-foreground hover:text-foreground">Skip the tour</button>
          <button onClick={() => (last ? finish() : setI(i + 1))}
                  className="px-4 py-2 rounded-xl bg-primary text-primary-foreground text-sm font-medium">
            {last ? "Got it" : "Next"}
          </button>
        </div>
      </div>
    </div>
  );
}
