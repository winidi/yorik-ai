import { StrictMode, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Routes, Route, Navigate, useLocation, useNavigate } from "react-router-dom";
import { api } from "@/lib/api";
import "./index.css";
import { EmailApp } from "./apps/email/EmailApp";
import { BriefingApp } from "./apps/briefing/BriefingApp";
import { WhatsAppApp } from "./apps/whatsapp/WhatsAppApp";
import { CalendarApp } from "./apps/calendar/CalendarApp";
import { ChatApp } from "./apps/chat/ChatApp";
import { DocumentsApp } from "./apps/documents/DocumentsApp";
import { ComposeApp } from "./apps/compose/ComposeApp";
import { SettingsApp } from "./apps/settings/SettingsApp";
import { HomeApp } from "./apps/home/HomeApp";
import { PhotosApp } from "./apps/photos/PhotosApp";
import { TasksApp } from "./apps/tasks/TasksApp";
import { ContactsApp } from "./apps/contacts/ContactsApp";
import { AmbientApp } from "./apps/ambient/AmbientApp";
import { CommunityApp } from "./apps/community/CommunityApp";
import { CommandPalette } from "./components/CommandPalette";
import { NotificationBell } from "./components/NotificationBell";
// OnboardingModal kept as a file for now, but not mounted — user
// said the small inline onboarding hints throughout the app are
// enough and the welcome modal was popping on every reload (bug
// we never finished diagnosing; sidestepped by not showing it).
// import { OnboardingModal } from "./components/OnboardingModal";
import { AuthGate } from "./components/AuthGate";
import { VoiceFab } from "./components/VoiceFab";
import { VoiceListeningOverlay } from "./components/VoiceListeningOverlay";
import { NavigationBridge } from "./components/NavigationBridge";
import { SwipeNav } from "./components/SwipeNav";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { DocBucketProvider } from "./apps/documents/DocBucketContext";
import { DocBucketPill } from "./components/DocBucketPill";

// React routes mount under /r/ (Vite base + FastAPI ingress).
// Each <Route> is one ported Yorik app. As more get migrated, add
// them here; legacy vanilla URLs (/, /chat, etc.) keep working.
//
// CommandPalette is mounted at the root so ⌘K / Ctrl+K works on
// every route — its content is portaled to <body> so position is
// independent of the routed component.
// Hide most global chrome on the kiosk ambient route — the wall is
// supposed to look like a digital photo frame, not a debug-y
// browser tab. VoiceFab stays visible because it already has the
// full voice-recording flow (~270 lines we don't want to rewrite
// just to relocate). Everything else (palette, notifications, dock,
// dev-bucket pill) hides via the gate. NavigationBridge stays
// because it's a behavior wrapper not a visual element.
function ChromeGate({ children }: { children: React.ReactNode }) {
  const loc = useLocation();
  if (loc.pathname.startsWith("/ambient")) return null;
  return <>{children}</>;
}

// Auto-redirect kiosk-flagged sessions into ambient on launch.
// Once Yorik is installed as a PWA on the wall tablet, the launcher
// icon opens at start_url=/r/home — but for the kiosk device that's
// the wrong destination; the user wants the slideshow, not the
// dashboard. Probe /api/devices once per app load, and if THIS
// session is marked is_kiosk, bounce to /ambient. Tracked via
// sessionStorage so an admin who manually navigates away (e.g. to
// Settings) isn't immediately yanked back.
function KioskRedirect() {
  const loc = useLocation();
  const navigate = useNavigate();
  useEffect(() => {
    // Only redirect on the initial route (root / home). Admin who
    // navigated to /settings explicitly stays there. The empty deps
    // array below ensures we fire only ONCE per app load, so this
    // doesn't fight manual navigation later in the session.
    if (loc.pathname !== "/" && loc.pathname !== "/home") return;
    (async () => {
      try {
        const devices = await api.get<any[]>("/api/devices");
        // CRITICAL: check is_current — devices[] returns ALL of the
        // user's sessions, so without this an admin on their laptop
        // would also be redirected because the wall kiosk's row
        // satisfies is_kiosk=true. We only want to redirect the row
        // that matches THIS browser's cookie.
        const mine = devices.find((d: any) => d.is_current && d.is_kiosk);
        if (mine) {
          navigate("/ambient", { replace: true });
        }
      } catch {
        // Not logged in / network blip — ignore; user can navigate
        // manually. AuthGate already handles the unauth case.
      }
    })();
    // Once per app load — empty deps. PWA windows that stay mounted
    // across re-opens won't re-fire, but that's fine: any manual
    // navigation gives them control. If they want back in, they
    // tap the "Open kiosk wall" button in Settings → Devices.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return null;
}

// Idle auto-return — drift kiosk-flagged devices back to /ambient
// after N seconds without input. Without this, anyone who taps
// "open settings" or navigates to /chat leaves the wall stranded
// on a debug-looking page forever. Slideshow should always reclaim
// the screen once the household stops poking at it.
//
// Active routes are exempt:
//   - /ambient: already there
//   - /settings: admin work happens here; thinking pauses shouldn't
//     yank the page mid-config
// /chat used to be hard-exempt to protect voice replies from the
// idle bounce, but that broke the "I'm logged in, sitting on /chat
// with no activity" case — the wall stayed forever. Now /chat is
// timed like any other page, and an active voice turn pings
// yorik:voice:active-tick every 5s (dispatched by VoiceFab's
// existing native-bridge heartbeat) so the timer resets while
// Yorik is mid-sentence. Once the turn ends, no more ticks fire
// and the normal 20s idle countdown resumes.
const KIOSK_IDLE_MS = 20_000;
const KIOSK_IDLE_EXEMPT = ["/ambient", "/settings"];

function KioskIdleWatch() {
  const loc = useLocation();
  const navigate = useNavigate();
  const [isKiosk, setIsKiosk] = useState<boolean | null>(null);
  const timerRef = useRef<number | null>(null);

  // Resolve kiosk status once per app load. Same is_current trap as
  // KioskRedirect above — without that check, this would auto-bounce
  // the admin's laptop into /ambient just because they own a kiosk
  // session somewhere on a wall tablet.
  useEffect(() => {
    (async () => {
      try {
        const devices = await api.get<any[]>("/api/devices");
        setIsKiosk(devices.some((d: any) => d.is_current && d.is_kiosk));
      } catch {
        setIsKiosk(false);
      }
    })();
  }, []);

  useEffect(() => {
    if (!isKiosk) return;
    if (KIOSK_IDLE_EXEMPT.some(p => loc.pathname.startsWith(p))) return;

    const reset = () => {
      if (timerRef.current) clearTimeout(timerRef.current);
      timerRef.current = window.setTimeout(() => {
        navigate("/ambient", { replace: true });
      }, KIOSK_IDLE_MS);
    };
    reset();

    // Discrete interactions only — no mousemove, since leaning the
    // head closer to the tablet shouldn't reset the idle timer.
    const events: (keyof WindowEventMap)[] =
      ["pointerdown", "keydown", "touchstart", "scroll", "wheel"];
    events.forEach(e => window.addEventListener(e, reset, { passive: true }));
    // Voice activity is its own kind of "user is using this" — while
    // a voice turn is in flight, VoiceFab fires this every 5s so we
    // don't yank the page out from under a speaking reply on /chat.
    window.addEventListener("yorik:voice:active-tick", reset);
    return () => {
      if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null; }
      events.forEach(e => window.removeEventListener(e, reset));
      window.removeEventListener("yorik:voice:active-tick", reset);
    };
  }, [isKiosk, loc.pathname, navigate]);

  return null;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter basename="/r">
      <AuthGate>
        <DocBucketProvider>
        <ErrorBoundary>
          <ChromeGate>
            <CommandPalette />
            <NotificationBell />
            <DocBucketPill />
          </ChromeGate>
          {/* VoiceFab + NavigationBridge always mounted — VoiceFab is
              the only voice path in/out, NavigationBridge is invisible
              behavior glue. */}
          <VoiceFab />
          <VoiceListeningOverlay />
          <NavigationBridge />
          <KioskRedirect />
          <KioskIdleWatch />
          <SwipeNav />
          <Routes>
            <Route path="/" element={<Navigate to="/home" replace />} />
            <Route path="/home" element={<HomeApp />} />
            <Route path="/email" element={<EmailApp />} />
            <Route path="/whatsapp" element={<WhatsAppApp />} />
            <Route path="/calendar" element={<CalendarApp />} />
            <Route path="/chat" element={<ChatApp />} />
            <Route path="/documents" element={<DocumentsApp />} />
            <Route path="/compose" element={<ComposeApp />} />
            <Route path="/photos" element={<PhotosApp />} />
            <Route path="/tasks" element={<TasksApp />} />
            <Route path="/contacts" element={<ContactsApp />} />
            <Route path="/settings" element={<SettingsApp />} />
            <Route path="/briefing" element={<BriefingApp />} />
            <Route path="/ambient" element={<AmbientApp />} />
            <Route path="/community-app/:appId" element={<CommunityApp />} />
          </Routes>
        </ErrorBoundary>
        </DocBucketProvider>
      </AuthGate>
    </BrowserRouter>
  </StrictMode>,
);
