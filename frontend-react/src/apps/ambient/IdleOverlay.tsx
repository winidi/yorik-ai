/**
 * Subtle bottom-screen overlay on the kiosk wall — greeting +
 * "tap to sign in" hint. Renders OVER the Slideshow without
 * obscuring the photos (semi-transparent + bottom-anchored so the
 * picture stays the focus).
 */
import { Hand } from "lucide-react";

interface Props {
  greeting?: string;    // e.g. "Good evening" — derived from local time
}

export function IdleOverlay({ greeting }: Props) {
  return (
    <div className="fixed inset-x-0 bottom-0 z-10 px-10 py-8 pointer-events-none">
      <div className="flex items-end justify-between gap-8">
        {/* Left: greeting only */}
        <div className="pointer-events-auto space-y-3 max-w-2xl">
          {greeting && (
            <div className="text-white/90 text-3xl font-light drop-shadow-md">
              {greeting}
            </div>
          )}
        </div>

        {/* Right: subtle "tap anywhere to sign in" hint. The whole
            ambient surface is the tap target — picking a user + PIN
            opens the regular Yorik UI in tablet mode. */}
        <div className="pointer-events-none flex items-center gap-2 text-white/70 text-sm pb-1">
          <Hand className="w-5 h-5" />
          <span>Tap anywhere to sign in</span>
        </div>
      </div>
    </div>
  );
}

