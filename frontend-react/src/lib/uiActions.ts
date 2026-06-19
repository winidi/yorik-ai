/**
 * Global UI-action bus with a buffer for race-with-mount cases.
 *
 * Problem: VoiceFab navigates to /calendar then dispatches a
 * `yorik-ui-action` event. But React Router navigation is async — the
 * destination component (CalendarApp) doesn't mount + attach its
 * useEffect listener until the next render cycle. The event fires
 * BEFORE the listener exists → silently missed.
 *
 * Solution: keep a tiny in-memory queue. emitUiAction() both fires
 * the event AND appends to the queue. Components drain matching
 * actions on mount, then process new ones via the listener as usual.
 */

interface UiAction {
  type: string;
  [k: string]: any;
}

const QUEUE_TTL_MS = 5000; // queue entries older than this are ignored on drain

interface QueuedAction {
  action: UiAction;
  ts: number;
}

const queue: QueuedAction[] = [];

/** Dispatch a UI action AND queue it for any component that hasn't mounted yet. */
export function emitUiAction(action: UiAction): void {
  queue.push({ action, ts: Date.now() });
  // Sweep stale entries while we're here so the queue doesn't grow.
  const cutoff = Date.now() - QUEUE_TTL_MS;
  while (queue.length > 0 && queue[0].ts < cutoff) queue.shift();
  window.dispatchEvent(new CustomEvent("yorik-ui-action", { detail: action }));
}

/**
 * Drain any queued actions whose `type` is in `types` and that haven't
 * expired. Components call this on mount to catch up on actions that
 * fired during their route transition.
 */
export function drainUiActions(types: string[]): UiAction[] {
  const cutoff = Date.now() - QUEUE_TTL_MS;
  const matchSet = new Set(types);
  const remaining: QueuedAction[] = [];
  const drained: UiAction[] = [];
  for (const entry of queue) {
    if (entry.ts < cutoff) continue;
    if (matchSet.has(entry.action.type)) {
      drained.push(entry.action);
    } else {
      remaining.push(entry);
    }
  }
  queue.length = 0;
  queue.push(...remaining);
  return drained;
}
