/**
 * Global "Yorik is listening" overlay.
 *
 * Mounts once at root so it renders on every route, not just /ambient.
 * Listens for the recording-state + amplitude events VoiceFab broadcasts
 * and paints a centered pulsating dot + amplitude bars at the top of
 * the screen. In continuous (ping-pong) mode it also shows a "tap to
 * end" pill — tapping it dispatches yorik:voice:continuous-end, which
 * VoiceFab handles by stopping the mic and exiting continuous mode.
 *
 * pointer-events are off on the wrapper so the chat composer, command
 * palette, etc. stay tappable underneath; the "end" pill enables its
 * own pointer-events to receive the tap.
 */
import { useEffect, useRef, useState } from "react";

export function VoiceListeningOverlay() {
  const [listening, setListening] = useState(false);
  const [continuous, setContinuous] = useState(false);
  const levelsRef = useRef<number[]>(new Array(24).fill(0));
  const barsRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const onStart = () => {
      setListening(true);
      levelsRef.current = new Array(24).fill(0);
    };
    const onStop = () => setListening(false);
    const onContinuousStart = () => setContinuous(true);
    const onContinuousEnd   = () => setContinuous(false);
    const onLevel = (ev: Event) => {
      const detail = (ev as CustomEvent).detail || {};
      const rms = typeof detail.rms === "number" ? detail.rms : 0;
      const arr = levelsRef.current;
      arr.shift();
      arr.push(rms);
      // Paint directly via ref — 12Hz event rate would otherwise
      // trigger 12 React re-renders per second.
      const host = barsRef.current;
      if (!host) return;
      const children = host.children;
      for (let i = 0; i < children.length && i < arr.length; i++) {
        const h = Math.min(44, 6 + arr[i] * 280);
        (children[i] as HTMLElement).style.height = `${h}px`;
      }
    };

    window.addEventListener("yorik:voice:recording-started", onStart);
    window.addEventListener("yorik:voice:recording-stopped", onStop);
    window.addEventListener("yorik:voice:continuous-started", onContinuousStart);
    window.addEventListener("yorik:voice:continuous-ended", onContinuousEnd);
    window.addEventListener("yorik:voice:level", onLevel);
    return () => {
      window.removeEventListener("yorik:voice:recording-started", onStart);
      window.removeEventListener("yorik:voice:recording-stopped", onStop);
      window.removeEventListener("yorik:voice:continuous-started", onContinuousStart);
      window.removeEventListener("yorik:voice:continuous-ended", onContinuousEnd);
      window.removeEventListener("yorik:voice:level", onLevel);
    };
  }, []);

  if (!listening && !continuous) return null;

  return (
    <div
      aria-live="polite"
      className="pointer-events-none fixed inset-x-0 top-10 z-[60] flex flex-col items-center gap-4 animate-in fade-in duration-300"
    >
      {listening && (
        <>
          <div className="flex items-center gap-3 px-5 py-2.5 rounded-full bg-black/55 backdrop-blur-md text-white">
            <span className="relative inline-flex h-3 w-3">
              <span className="absolute inline-flex h-full w-full rounded-full bg-rose-400 opacity-75 animate-ping" />
              <span className="relative inline-flex rounded-full h-3 w-3 bg-rose-500" />
            </span>
            <span className="text-sm font-medium tracking-wide">Listening…</span>
          </div>
          <div
            ref={barsRef}
            className="flex items-center justify-center gap-1.5 h-12 px-6 py-2 rounded-full bg-black/35 backdrop-blur-md"
          >
            {Array.from({ length: 24 }).map((_, i) => (
              <span
                key={i}
                className="w-1.5 rounded-full bg-gradient-to-t from-rose-400 to-rose-200 transition-[height] duration-75 ease-out"
                style={{ height: "6px" }}
              />
            ))}
          </div>
        </>
      )}
      {continuous && (
        <button
          type="button"
          onClick={() => {
            try {
              window.dispatchEvent(new CustomEvent("yorik:voice:continuous-end"));
            } catch {}
          }}
          className="pointer-events-auto px-4 py-1.5 rounded-full bg-black/65 backdrop-blur-md text-white text-xs font-medium tracking-wide hover:bg-black/80 transition border border-white/15"
        >
          Conversation mode · tap to end
        </button>
      )}
    </div>
  );
}
