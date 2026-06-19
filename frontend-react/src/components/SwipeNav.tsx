/**
 * Global horizontal swipe between dock apps.
 *
 * Commits on pointermove the instant horizontal intent is clear
 * (>30px horizontal, ratio favours horizontal): we setPointerCapture
 * + preventDefault so the Android WebView stops interpreting the
 * gesture as a vertical scroll and aborting it with pointercancel.
 * Without this, ~60% of intended swipes on the tablet died mid-flight.
 *
 * Direction follows the iOS / Android home-screen convention: content
 * tracks the finger. Swipe LEFT (finger drags leftward, content
 * slides left) → reveals the NEXT (rightward) app in DOCK_ORDER.
 * Swipe RIGHT → reveals the PREVIOUS (leftward) app.
 * Apps not in DOCK_ORDER (e.g. Settings) are treated as a virtual
 * position just before "home" — swipe left enters the dock at home,
 * swipe right is a no-op.
 *
 * Touch + pen only; mouse drags are deliberately ignored to keep
 * desktop use from triggering accidental navigation.
 *
 * Excludes:
 *   - /ambient (its own swipe-right opens the agenda)
 *   - the 24px gutters next to each screen edge (Android gesture
 *     navigation steals left/right-edge swipes for back/forward)
 *   - form inputs, contenteditable, and anything ancestor-flagged
 *     `[data-no-swipe]`
 *   - pointerdowns that start inside a horizontally scrollable
 *     container (carousels, code blocks)
 */

import { useEffect, useRef } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { DOCK_ORDER, REACT_ROUTES, appIdFromPath } from "@/lib/dock-order";

// Trigger thresholds for an actual app-switch. Once travel passes
// MIN_DIST_PX *or* MIN_DIST_FRAC × viewport (whichever is larger),
// and the move is ≤MAX_DUR_MS old, we fire. Loosened from earlier
// values that needed too much arm travel in landscape.
const MIN_DIST_FRAC = 0.18;
const MIN_DIST_PX = 100;
const MAX_DUR_MS = 800;
const MAX_VERTICAL_DRIFT_PX = 80;
const MIN_RATIO = 1.5;
const EDGE_DEAD_ZONE_PX = 24;
// Once horizontal travel passes this, we declare the gesture "ours":
// capture pointer + preventDefault on subsequent moves so the
// WebView doesn't fire pointercancel. Aggressively small so we win
// Chromium's scroll-vs-swipe arbitration race inside scroll
// containers, where touch-action: pan-y still permits arbitration.
const COMMIT_HORIZONTAL_PX = 10;

const SKIP_PATH_PREFIXES = ["/ambient"];

function isInsideHorizontalScroller(el: HTMLElement | null): boolean {
  let cur: HTMLElement | null = el;
  while (cur && cur !== document.body) {
    if (cur.scrollWidth > cur.clientWidth) {
      const style = getComputedStyle(cur);
      if (style.overflowX === "auto" || style.overflowX === "scroll") return true;
    }
    cur = cur.parentElement;
  }
  return false;
}

type SwipeState = {
  x: number;
  y: number;
  t: number;
  id: number;
  committed: boolean;
  consumed: boolean;
  captureEl: Element | null;
};

export function SwipeNav() {
  const loc = useLocation();
  const navigate = useNavigate();
  const stateRef = useRef<SwipeState | null>(null);

  // Native bridge for /r/photos. yorik-wall's MainActivity intercepts
  // horizontal swipes on the Photos page (where SwipeNav otherwise
  // can't see them because Immich runs in a cross-origin iframe) and
  // calls back here with the direction string. Mirrors fire() but
  // without distance/ratio checks — native already ran those.
  useEffect(() => {
    (window as Window & { __yorikNativeSwipe?: (direction: string) => void }).__yorikNativeSwipe = (direction: string) => {
      console.log("[swipe] NATIVE", direction);
      const currentAppId = appIdFromPath(loc.pathname);
      const i = currentAppId ? DOCK_ORDER.indexOf(currentAppId) : -1;
      // Content-follows-finger: finger LEFT exposes the next app, finger RIGHT the previous.
      const target = direction === "left" ? DOCK_ORDER[i + 1] : DOCK_ORDER[i - 1];
      if (!target) return;
      const route = REACT_ROUTES[target];
      if (!route) return;
      navigate(route);
    };
    return () => {
      delete (window as Window & { __yorikNativeSwipe?: (direction: string) => void }).__yorikNativeSwipe;
    };
  }, [loc.pathname, navigate]);

  useEffect(() => {
    if (SKIP_PATH_PREFIXES.some(p => loc.pathname.startsWith(p))) return;

    const fire = (dx: number) => {
      const s = stateRef.current;
      if (s) s.consumed = true;
      const currentAppId = appIdFromPath(loc.pathname);
      // Apps outside DOCK_ORDER (Settings) get a virtual index of -1
      // so swipe-left enters the dock at home, swipe-right is a no-op.
      const i = currentAppId ? DOCK_ORDER.indexOf(currentAppId) : -1;
      // Content-follows-finger: dragging left exposes the next app
      // on the right; dragging right exposes the previous app on
      // the left.
      const target = dx < 0 ? DOCK_ORDER[i + 1] : DOCK_ORDER[i - 1];
      if (!target) { console.log("[swipe] fire NO-OP at edge", { currentAppId, dir: dx < 0 ? "left" : "right" }); return; }
      const route = REACT_ROUTES[target];
      if (!route) return;
      console.log("[swipe] NAVIGATE", JSON.stringify({ from: currentAppId, to: target, route }));
      navigate(route);
    };

    const onDown = (e: PointerEvent) => {
      if (e.pointerType !== "touch" && e.pointerType !== "pen") return;
      const tgt = e.target as HTMLElement | null;
      // Hard reject: anything explicitly marked don't-swipe.
      if (tgt?.closest?.("[data-no-swipe]")) {
        console.log("[swipe] down REJECT no-swipe");
        return;
      }
      // Soft reject for inputs / contenteditable: ONLY reject if the
      // editable element is currently focused — i.e. the user is
      // actively typing. Without this check, swipes over the TipTap
      // editor in Compose are blocked on every touch even when the
      // user just wants to navigate. The 10px commit + 100px nav
      // threshold means accidental swipe-while-typing is unlikely.
      const editable = tgt?.closest?.("input, textarea, [contenteditable=true], [contenteditable='']");
      if (editable && document.activeElement === editable) {
        console.log("[swipe] down REJECT editable focused");
        return;
      }
      if (e.clientX < EDGE_DEAD_ZONE_PX || e.clientX > window.innerWidth - EDGE_DEAD_ZONE_PX) {
        console.log("[swipe] down REJECT edge", JSON.stringify({ x: e.clientX, vw: window.innerWidth }));
        return;
      }
      if (isInsideHorizontalScroller(tgt)) {
        console.log("[swipe] down REJECT h-scroller", tgt?.tagName);
        return;
      }
      stateRef.current = {
        x: e.clientX, y: e.clientY, t: Date.now(), id: e.pointerId,
        committed: false, consumed: false, captureEl: null,
      };
      console.log("[swipe] down ACCEPT", JSON.stringify({ x: e.clientX, y: e.clientY, pt: e.pointerType }));
    };

    const onMove = (e: PointerEvent) => {
      const s = stateRef.current;
      if (!s || s.id !== e.pointerId || s.consumed) return;
      const dx = e.clientX - s.x;
      const dy = e.clientY - s.y;
      const adx = Math.abs(dx);
      const ady = Math.abs(dy);

      // Pre-commit: if the gesture is dominantly vertical, release —
      // the user is scrolling. Drop state so onUp can't fire either.
      if (!s.committed && ady > MAX_VERTICAL_DRIFT_PX && ady > adx) {
        console.log("[swipe] move ABANDON (vertical)", JSON.stringify({ adx, ady }));
        stateRef.current = null;
        return;
      }

      // Cross the commit threshold → claim the gesture.
      if (!s.committed && adx > COMMIT_HORIZONTAL_PX && adx > ady * MIN_RATIO) {
        s.committed = true;
        const captureTarget = (e.target as Element) ?? document.documentElement;
        try {
          captureTarget.setPointerCapture?.(e.pointerId);
          s.captureEl = captureTarget;
        } catch { /* setPointerCapture can throw if target detached */ }
        console.log("[swipe] move COMMIT", JSON.stringify({ adx, ady }));
      }

      if (s.committed) {
        // Block the WebView from interpreting this as scroll/back.
        if (e.cancelable) e.preventDefault();
        const minDist = Math.max(MIN_DIST_PX, window.innerWidth * MIN_DIST_FRAC);
        const dur = Date.now() - s.t;
        if (adx >= minDist && dur <= MAX_DUR_MS) fire(dx);
      }
    };

    const onUp = (e: PointerEvent) => {
      const s = stateRef.current;
      if (!s || s.id !== e.pointerId) return;
      stateRef.current = null;
      if (s.captureEl) {
        try { s.captureEl.releasePointerCapture?.(e.pointerId); } catch {}
      }
      if (s.consumed) return;
      // Backstop for slow swipes that crossed threshold only at the
      // very end without ever triggering the move-time fire.
      const dx = e.clientX - s.x;
      const dy = e.clientY - s.y;
      const adx = Math.abs(dx);
      const ady = Math.abs(dy);
      const dur = Date.now() - s.t;
      const minDist = Math.max(MIN_DIST_PX, window.innerWidth * MIN_DIST_FRAC);
      console.log("[swipe] up", JSON.stringify({ dx, dy, dur, minDist, committed: s.committed }));
      if (dur > MAX_DUR_MS) return;
      if (ady > MAX_VERTICAL_DRIFT_PX) return;
      if (adx < minDist) return;
      if (adx < ady * MIN_RATIO) return;
      fire(dx);
    };

    const onCancel = (e: PointerEvent) => {
      const s = stateRef.current;
      if (!s || s.id !== e.pointerId) return;
      // If we already committed, the cancel arrived AFTER we owned
      // the gesture — fine, we already navigated or will via onUp.
      // If we hadn't committed yet, the WebView aborted before we
      // could claim the gesture; drop state quietly.
      console.log("[swipe] cancel", JSON.stringify({ committed: s.committed, consumed: s.consumed }));
      stateRef.current = null;
    };

    window.addEventListener("pointerdown", onDown, { passive: true });
    // Non-passive on move so preventDefault works post-commit.
    window.addEventListener("pointermove", onMove, { passive: false });
    window.addEventListener("pointerup", onUp, { passive: true });
    window.addEventListener("pointercancel", onCancel, { passive: true });
    return () => {
      window.removeEventListener("pointerdown", onDown);
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onCancel);
    };
  }, [loc.pathname, navigate]);

  return null;
}
