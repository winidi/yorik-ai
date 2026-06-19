/**
 * Kiosk ambient mode — fullscreen photo slideshow with tap-to-talk.
 *
 * Mounted at /ambient. Refuses to render unless the device has been
 * marked as a kiosk in Settings → Devices (otherwise: redirect home
 * with a polite "this device isn't a kiosk yet" hint). The chrome
 * (Dock, CommandPalette, VoiceFab, NotificationBell, DocBucketPill)
 * checks the route and hides itself when this is active — see main.tsx.
 *
 * Loop:
 *   - Slideshow plays continuously from the device's configured album
 *   - IdleOverlay shows greeting + next event chips + tap-to-talk
 *   - Tap-to-talk → record → POST /api/ask-voice/stream → ActiveResponse
 *   - After silence, fade back to Slideshow
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Loader2, Settings as SettingsIcon } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { useAuth } from "@/components/AuthGate";
import { Slideshow, type SlideshowPhoto } from "./Slideshow";
import { IdleOverlay } from "./IdleOverlay";
import { AvatarPinFallback, type PickableUser } from "./AvatarPinFallback";
import { AgendaPane } from "./AgendaPane";

// Pointer-gesture thresholds. Picked for a wall-mounted tablet —
// a tap is ≤8px movement; a swipe is ≥40px primarily horizontal.
// The dead zone between them (8-40px) feels like a fat-fingered
// tap, so we treat it as a tap to avoid surprising no-ops.
const SWIPE_THRESHOLD_PX     = 40;
const TAP_MAX_MOVEMENT_PX    = 8;
const MAX_TAP_DURATION_MS    = 500;

// Refetch slideshow periodically so newly-added photos appear
// without a tablet reload.
const PHOTOS_REFRESH_MS = 5 * 60 * 1000;

interface MeSession {
  is_kiosk:          boolean;
  device_label?:     string;
}

export function AmbientApp() {
  const auth = useAuth();
  const navigate = useNavigate();

  const [photos, setPhotos]   = useState<SlideshowPhoto[]>([]);
  const [configured, setConfigured] = useState<boolean | null>(null);
  const [error, setError]     = useState<string | null>(null);
  const [kiosk, setKiosk]     = useState<MeSession | null>(null);

  // Tap-to-sign-in picker — opens on any pointerdown over the
  // ambient surface. The household tablet idles on photos; whoever
  // walks up taps anywhere, picks themselves, types their PIN, and
  // lands on /chat as themselves. Replaces the old voice-driven
  // identify_needed flow (which depended on ECAPA speaker-ID — now
  // off by default; see refactor(voice): drop ECAPA speaker-ID
  // gating on kiosk turns).
  const [pickerOpen, setPickerOpen] = useState(false);
  const [pickableUsers, setPickableUsers] = useState<PickableUser[] | null>(null);
  // Distinguish "API said no users have PINs" from "API call failed."
  // The former is a user-fixable config issue ("set a PIN"); the
  // latter is a wlan_trust denial or network glitch. Surfacing the
  // difference saves a debug session on the tablet itself.
  const [pickerError, setPickerError] = useState<string | null>(null);
  // Agenda pane — opened by swipe-right, shows today's events from
  // every consenting household member. User-agnostic surface.
  const [agendaOpen, setAgendaOpen] = useState(false);

  // "Hi Dirk" greeting overlay — fades in when VoiceFab's
  // identification handler dispatches yorik:user:switched after a
  // successful ECAPA + voice-login swap. Auto-clears after ~3s.
  // Lives here (not in VoiceFab) because the VoiceFab popover is
  // hidden on /ambient, and this is the only place the user looks
  // when the wake word fires on the wall.
  const [greeting, setGreeting] = useState<{ name: string; at: number } | null>(null);

  // The listening dot + amplitude visualiser used to live here but
  // moved to <VoiceListeningOverlay /> in main.tsx so /chat (and any
  // other route) gets the same feedback during ping-pong / continuous
  // voice mode.

  // Voice-driven "who's there?" picker — opened when the backend
  // emits identify_needed (kiosk turn + voice-ID below threshold).
  // Carries the transcript so the picker shows what the speaker said
  // and offers a "retry as <name>" after PIN entry.
  const [voicePicker, setVoicePicker] = useState<{
    users:        PickableUser[];
    transcript:   string;
    retry_message:string;
  } | null>(null);

  // Bootstrap — confirm this session is a kiosk, then start the
  // poll loops. Non-kiosk sessions get bounced to /home with a
  // friendly note instead of staring at a blank page.
  useEffect(() => {
    let cancelled = false;
    // YorikWall wrapper detection — running inside the native Android
    // app makes this device inherently a kiosk wall, regardless of
    // whether the user remembered to tick "Make this a kiosk" in
    // Settings. Without this, the wrapper bounces between /ambient
    // and /home every 10s as our native idle-watch fights the PWA's
    // own redirect-to-home for non-kiosk sessions.
    const inWrapper = navigator.userAgent.includes("YorikWall");
    (async () => {
      try {
        const devices = await api.get<any[]>("/api/devices");
        // Must match BOTH: it's the row for this browser's session
        // AND that session is flagged as a kiosk. Just checking
        // is_kiosk would let an admin's laptop in just because they
        // own a kiosk session somewhere on a wall tablet.
        const mine = devices.find(d => d.is_current && d.is_kiosk);
        if (cancelled) return;
        if (!mine && !inWrapper) {
          // Not a kiosk session AND not running inside the wrapper —
          // leave ambient mode and go to the personal home dashboard.
          navigate("/home", { replace: true });
          return;
        }
        // In wrapper but session isn't flagged: keep the kiosk view
        // anyway. We don't have a kiosk_album_id so the slideshow
        // will fall back to its "no album configured" hint; that's
        // fine, the admin can configure it from Settings later.
        const myDevice = devices.find(d => d.is_current);
        setKiosk({
          is_kiosk:         true,
          device_label:     mine?.device_label ?? myDevice?.device_label ?? "Wall",
        });
      } catch (err: any) {
        if (cancelled) return;
        const msg = err instanceof ApiError ? err.message : String(err);
        // In wrapper, even a network blip shouldn't kick us to /home —
        // the slideshow will retry on the next refresh interval.
        if (inWrapper) {
          setKiosk({ is_kiosk: true, device_label: "Wall" });
          return;
        }
        setError(`Couldn't verify kiosk status: ${msg}`);
      }
    })();
    return () => { cancelled = true; };
  }, [navigate]);

  // Slideshow poll loop
  const refreshPhotos = useCallback(async () => {
    try {
      const r = await api.get<{ photos: SlideshowPhoto[]; configured: boolean }>(
        "/api/ambient/slideshow?limit=200",
      );
      setPhotos(r.photos || []);
      setConfigured(r.configured);
    } catch (err: any) {
      // Don't blow away the slideshow on transient errors —
      // keep showing whatever's already loaded.
      console.warn("ambient: slideshow refresh failed", err);
    }
  }, []);

  useEffect(() => {
    if (!kiosk) return;
    refreshPhotos();
    const t = setInterval(refreshPhotos, PHOTOS_REFRESH_MS);
    return () => clearInterval(t);
  }, [kiosk, refreshPhotos]);

  // Screen wake-lock — keep the tablet from sleeping while ambient
  // is mounted. Auto-released when the tab loses visibility or the
  // user navigates away.
  useEffect(() => {
    if (!kiosk) return;
    let lock: any = null;
    const acquire = async () => {
      try {
        const navAny = navigator as any;
        if (navAny.wakeLock?.request) {
          lock = await navAny.wakeLock.request("screen");
        }
      } catch (err) {
        console.warn("ambient: wake-lock failed", err);
      }
    };
    acquire();
    const onVisible = () => {
      if (document.visibilityState === "visible" && !lock) acquire();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      document.removeEventListener("visibilitychange", onVisible);
      lock?.release?.();
    };
  }, [kiosk]);

  // Listen for voice-login → "Hi Dirk" overlay. The custom event
  // carries the matched user dict from the session-swap response;
  // we just need first_name (or fall back to name).
  useEffect(() => {
    function onSwitched(ev: Event) {
      const detail = (ev as CustomEvent).detail || {};
      const first = (detail.first_name || detail.name || "").trim().split(" ")[0];
      if (!first) return;
      setGreeting({ name: first, at: Date.now() });
      // Auto-clear so the next switch (re-greeting same person) re-fires.
      window.setTimeout(() => {
        setGreeting(g => (g && Date.now() - g.at >= 2900) ? null : g);
      }, 3000);
    }
    window.addEventListener("yorik:user:switched", onSwitched);
    return () => window.removeEventListener("yorik:user:switched", onSwitched);
  }, []);

  // Listen for identify_needed from VoiceFab. Pops the avatar+PIN
  // picker with the users + transcript the backend pre-shaped. Without
  // this, a voice turn with a below-threshold ECAPA match silently
  // dead-ends after the "Sofort" ack — the user gets no follow-up.
  useEffect(() => {
    function onIdentifyNeeded(ev: Event) {
      const detail = (ev as CustomEvent).detail || {};
      setVoicePicker({
        users:         Array.isArray(detail.users) ? detail.users : [],
        transcript:    detail.transcript || "",
        retry_message: detail.retry_message || detail.transcript || "",
      });
    }
    window.addEventListener("yorik:identify-needed", onIdentifyNeeded);
    return () => window.removeEventListener("yorik:identify-needed", onIdentifyNeeded);
  }, []);

  // Real fullscreen — kills the Android status bar + Chrome tab strip
  // even when the user opened /ambient as a bare URL. Cannot be called
  // automatically by the browser (needs a user gesture for security);
  // we register a one-shot tap handler that fires the first time the
  // user touches the wall. After that the chrome stays hidden until
  // they leave the route.
  useEffect(() => {
    if (!kiosk) return;
    function tryFullscreen() {
      const el = document.documentElement as any;
      try {
        if (document.fullscreenElement) return;
        (el.requestFullscreen ?? el.webkitRequestFullscreen)?.call(el);
      } catch {}
      window.removeEventListener("pointerdown", tryFullscreen);
    }
    window.addEventListener("pointerdown", tryFullscreen, { once: true });
    return () => {
      window.removeEventListener("pointerdown", tryFullscreen);
      // Don't exit fullscreen on unmount — the user might just be
      // bouncing to /chat for the PIN flow and want the chrome to
      // stay hidden when they come back.
    };
  }, [kiosk]);

  // Tap → fetch pickable users (cached after first call) and pop the
  // AvatarPinFallback. While the fetch is in flight the modal renders
  // immediately so the wall feels responsive.
  const openPicker = useCallback(async () => {
    if (pickerOpen) return;
    setPickerOpen(true);
    if (pickableUsers !== null && !pickerError) return;
    setPickerError(null);
    try {
      const r = await api.get<{ users: PickableUser[] }>("/api/auth/pin-pickable");
      setPickableUsers(r.users || []);
    } catch (err: any) {
      console.warn("ambient: pin-pickable fetch failed", err);
      const msg = err instanceof ApiError ? err.message : String(err);
      setPickerError(msg);
      setPickableUsers([]);
    }
  }, [pickerOpen, pickableUsers, pickerError]);

  // Pointer-gesture dispatcher: distinguishes a tap (small movement,
  // short duration) from a horizontal swipe. Tracks the down-point in
  // a ref so we don't trigger re-renders during the move. On up we
  // classify based on the cumulative delta + duration. Swipe-right
  // opens the agenda; swipe-left from the agenda closes it; everything
  // else falls through as a tap → picker.
  //
  // We DON'T use the native click event because click fires AFTER
  // touchend on mobile WebViews — by the time it arrives we'd already
  // need to know whether this was a swipe. Pointer events let us
  // decide at touchend.
  const downRef = useRef<{ x: number; y: number; t: number } | null>(null);
  const handlePointerDown = useCallback((e: React.PointerEvent) => {
    downRef.current = { x: e.clientX, y: e.clientY, t: Date.now() };
  }, []);
  const handlePointerUp = useCallback((e: React.PointerEvent) => {
    const start = downRef.current;
    downRef.current = null;
    if (!start) return;
    const dx = e.clientX - start.x;
    const dy = e.clientY - start.y;
    const adx = Math.abs(dx), ady = Math.abs(dy);
    const dur = Date.now() - start.t;
    // Swipe right: significant horizontal motion, mostly horizontal,
    // dx positive.
    if (adx > SWIPE_THRESHOLD_PX && adx > ady && dx > 0) {
      if (!agendaOpen && !pickerOpen) setAgendaOpen(true);
      return;
    }
    // Swipe left: closes the agenda if it's open. Otherwise no-op
    // (we don't open anything else on left-swipe for now).
    if (adx > SWIPE_THRESHOLD_PX && adx > ady && dx < 0) {
      if (agendaOpen) setAgendaOpen(false);
      return;
    }
    // Tap: small movement + short duration → open the picker.
    if (adx <= TAP_MAX_MOVEMENT_PX && ady <= TAP_MAX_MOVEMENT_PX && dur <= MAX_TAP_DURATION_MS) {
      if (!agendaOpen && !pickerOpen) openPicker();
    }
    // Otherwise (long press, diagonal swipe, etc.): no action.
  }, [agendaOpen, pickerOpen, openPicker]);

  if (error) {
    return (
      <FullscreenMessage>
        <div className="text-rose-500 mb-2">Ambient failed to start</div>
        <div className="text-sm text-white/70">{error}</div>
      </FullscreenMessage>
    );
  }

  if (!kiosk) {
    return (
      <FullscreenMessage>
        <Loader2 className="w-8 h-8 animate-spin opacity-60" />
      </FullscreenMessage>
    );
  }

  // Album not configured yet — show a polite hint with a settings link.
  // Admins can fix this from Settings → Devices. Non-admin users on
  // the same tablet just see the hint without an action button.
  if (configured === false) {
    return (
      <FullscreenMessage>
        <div className="text-2xl font-light mb-2">No album configured</div>
        <div className="text-white/70 mb-6 max-w-md text-center">
          Pick the Immich album you want to show on this kiosk in
          Settings → Devices → {kiosk.device_label || "this device"} →
          Choose album.
        </div>
        {(auth.user?.role === "admin" || auth.user?.role === "platform_admin") && (
          <button
            type="button"
            onClick={() => navigate("/settings")}
            className="inline-flex items-center gap-2 px-4 py-2 rounded-full bg-white/15 hover:bg-white/25"
          >
            <SettingsIcon className="w-4 h-4" />
            Open Settings
          </button>
        )}
      </FullscreenMessage>
    );
  }

  return (
    <div
      className="fixed inset-0 z-40 bg-black overflow-hidden touch-none"
      onPointerDown={handlePointerDown}
      onPointerUp={handlePointerUp}
    >
      <Slideshow photos={photos} />
      <IdleOverlay greeting={timeGreeting()} />
      {greeting && (
        <div
          aria-live="polite"
          className="pointer-events-none fixed inset-0 z-50 flex items-center justify-center animate-in fade-in duration-500"
        >
          <div className="px-12 py-8 rounded-3xl bg-black/55 backdrop-blur-md text-center">
            <div className="font-serif text-white/95 text-6xl tracking-tight">
              Hi {greeting.name}
            </div>
            <div className="mt-2 text-white/60 text-xs uppercase tracking-[0.2em]">
              Voice recognised
            </div>
          </div>
        </div>
      )}
      {agendaOpen && (
        <AgendaPane onClose={() => setAgendaOpen(false)} />
      )}
      {pickerOpen && (
        <AvatarPinFallback
          users={pickableUsers ?? []}
          transcript={pickerError ? `Couldn't load users: ${pickerError}` : ""}
          retryMessage=""
          onClose={() => setPickerOpen(false)}
          // Chicken-and-egg escape: when pin-pickable 403s
          // (no trusted_kiosk_devices row yet), let an admin
          // sign in fresh + jump to /settings to mark this
          // device as a kiosk. We force-logout first so the
          // wall's stale cookie (often the admin who installed
          // the app, or a household member who PIN-switched
          // earlier) can't be ridden into Settings by anyone
          // who walks up and taps. The kiosk-toggle in Devices
          // is admin-gated, so even a non-admin who signs in
          // here can't actually finish setup. /r/settings is
          // in the wrapper's SKIP_RETURN_PATHS so the idle
          // watcher won't yank them back here mid-setup.
          onSignInWithPassword={pickerError ? async () => {
            try { await api.post("/api/auth/logout"); } catch {}
            await auth.refresh();
            navigate("/settings");
          } : undefined}
          onSwitched={() => {
            // PIN-switched successfully — the ephemeral cookie now
            // points at the picked user. Navigate to /chat so they
            // get the regular Yorik UI in tablet mode. The native
            // wrapper's 10s idle-return brings them back here after
            // a quiet stretch, at which point the wall is the photo
            // wall again and the next tap re-opens the picker.
            setPickerOpen(false);
            auth.refresh().catch(() => {});
            navigate("/chat");
          }}
        />
      )}
      {voicePicker && (
        <AvatarPinFallback
          users={voicePicker.users}
          transcript={voicePicker.transcript}
          retryMessage={voicePicker.retry_message}
          onClose={() => setVoicePicker(null)}
          onSwitched={() => {
            // PIN-switched after voice-ID failed. AvatarPinFallback
            // already dispatches yorik:voice:resume internally, which
            // re-fires the original transcript as the picked user —
            // VoiceFab's startResume handler picks it up and the
            // answer streams back on /ambient. Stay on /ambient (no
            // navigate) so the resume audio lands here.
            setVoicePicker(null);
            auth.refresh().catch(() => {});
          }}
        />
      )}
    </div>
  );
}

function FullscreenMessage({ children }: { children: React.ReactNode }) {
  return (
    <div className="fixed inset-0 z-40 bg-black text-white flex flex-col items-center justify-center">
      {children}
    </div>
  );
}

function timeGreeting(): string {
  // Neutral greeting — no name. The wall is a shared surface
  // (whoever's in the kitchen sees it), so personalizing to the
  // cookie-session user would leak who's currently signed in and
  // feel off to anyone else standing nearby. Once the user taps and
  // signs in, the regular Yorik UI takes over with proper personal
  // greetings throughout.
  const h = new Date().getHours();
  if (h < 5)  return "Good night.";
  if (h < 12) return "Good morning.";
  if (h < 18) return "Good afternoon.";
  return "Good evening.";
}
