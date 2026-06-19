/**
 * Ken-Burns crossfade slideshow for the ambient kiosk wall.
 *
 * Cycles through photos served from /api/ambient/slideshow. Each
 * image gets a slow zoom + pan ("Ken Burns") while the next one
 * fades in underneath. When the array exhausts, restart from the
 * top — the data fetch refreshes itself every N minutes so newly-
 * added photos appear without a tablet reload.
 *
 * Empty state: nothing rendered (parent shows "configure album"
 * link separately). Single-image state: no cycling, just the still.
 */
import { useEffect, useMemo, useState } from "react";

export interface SlideshowPhoto {
  id:            string;
  thumbnail_url: string;
  taken_at?:     string | null;
}

interface Props {
  photos:        SlideshowPhoto[];
  /** ms each slide is on screen (excluding crossfade overlap). */
  durationMs?:   number;
  /** ms for the crossfade between slides. */
  fadeMs?:       number;
}

// Defaults tuned for a wall-mounted photo frame, not a TV slideshow.
// 12s is long enough to actually look at a photo without feeling
// rushed; 4s crossfade is slow enough that the eye doesn't notice
// the cut, only the dissolve.
export function Slideshow({ photos, durationMs = 12000, fadeMs = 4000 }: Props) {
  const [idx, setIdx] = useState(0);
  // Shuffle ONCE per photo set so reloads don't redraw the same
  // sequence every time. Deterministic within a single mount,
  // re-shuffled when the photo list changes (e.g. after the
  // /slideshow refetch returns a new album).
  const order = useMemo(() => {
    const ids = photos.map((_, i) => i);
    for (let i = ids.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [ids[i], ids[j]] = [ids[j], ids[i]];
    }
    return ids;
  }, [photos]);

  useEffect(() => {
    if (photos.length <= 1) return;
    const t = setInterval(() => {
      setIdx(i => (i + 1) % order.length);
    }, durationMs);
    return () => clearInterval(t);
  }, [photos.length, order.length, durationMs]);

  if (photos.length === 0) return null;

  const currentI  = order[idx % order.length] ?? 0;
  const previousI = order[(idx - 1 + order.length) % order.length] ?? 0;
  const current   = photos[currentI];
  const previous  = photos[previousI];

  return (
    <div className="fixed inset-0 overflow-hidden bg-black">
      {/* Keyframes co-located with the only component that uses them.
          kb-fade drives the crossfade (opacity 0→1) on the incoming
          frame; kb-zoom drives the Ken-Burns scale+translate, reading
          per-instance start/end values from CSS variables set inline.
          Both ease in/out for a slow-in slow-out feel that disguises
          the discrete frame change. */}
      <style>{`
        @keyframes kb-fade {
          from { opacity: 0; }
          to   { opacity: 1; }
        }
        @keyframes kb-zoom {
          from {
            transform: scale(var(--kb-start-scale, 1.05))
                       translate(var(--kb-start-x, 0), var(--kb-start-y, 0));
          }
          to {
            transform: scale(var(--kb-end-scale, 1.15))
                       translate(var(--kb-end-x, 0), var(--kb-end-y, 0));
          }
        }
      `}</style>
      {/* Outgoing frame (previous photo) sits beneath at full opacity
          until the incoming one fades up over it. No fade-out
          needed — the incoming reaches opacity 1 just as we'd want
          previous to be gone, and the next remount drops it. */}
      {previous && previous.id !== current.id && (
        <SlideImage
          key={`prev-${previous.id}-${idx}`}
          photo={previous}
          durationMs={durationMs}
        />
      )}
      <SlideImage
        key={`cur-${current.id}-${idx}`}
        photo={current}
        durationMs={durationMs}
        fadeIn
        fadeMs={fadeMs}
      />
      {/* Bottom-half gradient — gives the IdleOverlay a readable
          surface without committing to a hard solid band. */}
      <div
        className="pointer-events-none fixed inset-x-0 bottom-0 h-72"
        style={{
          background:
            "linear-gradient(to top, rgba(0,0,0,0.65) 0%, rgba(0,0,0,0.0) 100%)",
        }}
      />
    </div>
  );
}

function SlideImage({
  photo, durationMs, fadeIn = false, fadeMs = 0,
}: {
  photo:      SlideshowPhoto;
  durationMs: number;
  fadeIn?:    boolean;
  fadeMs?:    number;
}) {
  // Random Ken-Burns parameters per mount so each photo gets a
  // unique zoom/pan vector and we don't see the same animation
  // repeat. Captured at mount-time, not state — re-renders during
  // the cycle don't shuffle mid-animation. Pan amplitude is small
  // (±3%) because anything bigger looks shaky on a 4K wall tablet.
  const kb = useMemo(() => ({
    startScale: 1.0 + Math.random() * 0.05,
    endScale:   1.10 + Math.random() * 0.10,
    startX:     (Math.random() - 0.5) * 6,   // %
    startY:     (Math.random() - 0.5) * 6,
    endX:       (Math.random() - 0.5) * 6,
    endY:       (Math.random() - 0.5) * 6,
  }), []);

  // Two layers per slide:
  //   1. Blurred backdrop — same image, object-cover, heavily blurred
  //      and slightly scaled up. Fills the screen edge-to-edge so a
  //      portrait photo on a landscape tablet (or vice versa) doesn't
  //      get black bars; the bars become a soft colour wash that
  //      reads as part of the photo's palette. Apple-TV / Google-Photos
  //      pattern.
  //   2. Sharp foreground — same image, object-contain, Ken-Burns
  //      driven. ALWAYS shows the entire photo regardless of the
  //      photo's aspect ratio vs the tablet's. This is the fix for
  //      portraits cropping to a horizontal strip on a landscape wall.
  //
  // Both layers share the same crossfade (opacity on the wrapper),
  // so the slide as a whole fades in/out together. Only the
  // foreground gets Ken-Burns — the backdrop stays static so the
  // soft wash doesn't pulse distractingly behind the focal photo.
  const wrapperStyle: React.CSSProperties = {
    opacity: fadeIn ? 0 : 1,
    animation: fadeIn
      ? `kb-fade ${fadeMs}ms cubic-bezier(0.4, 0, 0.2, 1) forwards`
      : undefined,
  };
  const kbStyle: React.CSSProperties = {
    animation: `kb-zoom ${durationMs + fadeMs}ms ease-in-out forwards`,
    ["--kb-start-scale" as any]: String(kb.startScale),
    ["--kb-end-scale"   as any]: String(kb.endScale),
    ["--kb-start-x"     as any]: `${kb.startX}%`,
    ["--kb-start-y"     as any]: `${kb.startY}%`,
    ["--kb-end-x"       as any]: `${kb.endX}%`,
    ["--kb-end-y"       as any]: `${kb.endY}%`,
  };
  return (
    <div className="fixed inset-0" style={wrapperStyle}>
      <img
        src={photo.thumbnail_url}
        alt=""
        loading="eager"
        aria-hidden="true"
        className="absolute inset-0 w-full h-full object-cover"
        style={{
          filter: "blur(48px) brightness(0.7) saturate(1.1)",
          transform: "scale(1.12)",
        }}
      />
      <img
        src={photo.thumbnail_url}
        alt=""
        loading="eager"
        className="absolute inset-0 w-full h-full object-contain"
        style={kbStyle}
      />
    </div>
  );
}
