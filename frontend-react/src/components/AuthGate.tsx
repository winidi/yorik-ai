/**
 * Auth gate — wraps every routed app. Decides what to show:
 *   1. Setup screen — if no user has a password yet (first-run install)
 *   2. Login screen — if not logged in
 *   3. Onboarding wizard — if logged in but `onboarded_at` is null
 *   4. The actual app — once everything's settled
 *
 * Why this lives outside the router: each app shouldn't have to know
 * about auth. The gate runs once at the top, sets up an AuthContext,
 * and renders the right thing.
 */

import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { Loader2, Sparkles } from "lucide-react";
import { api, registerSessionExpiredHandler as api_registerSessionExpiredHandler } from "@/lib/api";
import type { AuthMe, YorikUser } from "@/lib/api";
import { JoinScreen } from "./JoinScreen";
import { LoginScreen } from "./LoginScreen";
import { SetupScreen } from "./SetupScreen";
import { OnboardingWizard } from "./OnboardingWizard";

interface AuthContextValue {
  user: YorikUser;
  isTenant: boolean;
  refresh: () => Promise<void>;
  logout: () => Promise<void>;
  /** Somebody is PIN-unlocked at the wall right now. */
  wallUnlock: boolean;
  /** End the unlock and give the wall back to itself. */
  endWallUnlock: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be inside <AuthGate>");
  return ctx;
}

export function AuthGate({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<AuthMe | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const me = await api.get<AuthMe>("/api/auth/me");
      setState(me);
      setError(null);
    } catch (e: any) {
      setError(e.message || "Auth check failed");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  // The role on <html> lets CSS size things per person: a child's
  // account gets everything a little larger (index.css).
  const role = state?.user?.role;
  useEffect(() => {
    if (role) document.documentElement.dataset.role = role;
    else delete document.documentElement.dataset.role;
  }, [role]);

  // The wall never shows a login screen. A PIN there unlocks the tablet
  // for a few minutes, and when that runs out the cookie points at a
  // session the server has already dropped — which would land the
  // hallway on LoginScreen. Inside the wrapper we first ask for the
  // wall's own session back (it is parked in a second cookie, so this
  // only restores something this device already had). Once per lapse:
  // the ref keeps a failing return from looping.
  const wallReturnTried = useRef(false);
  const signedOut = !loading && !state?.logged_in;
  const inWrapper = typeof navigator !== "undefined"
    && navigator.userAgent.includes("YorikWall");
  useEffect(() => {
    if (!signedOut || !inWrapper || wallReturnTried.current) return;
    wallReturnTried.current = true;
    (async () => {
      try {
        await api.post("/api/auth/wall-return");
        await refresh();
      } catch {
        // No parked session, or it died too: the wall genuinely needs
        // someone to sign in, so let the login screen through.
      }
    })();
  }, [signedOut, inWrapper, refresh]);
  // Signed in again → arm the next lapse.
  useEffect(() => { if (state?.logged_in) wallReturnTried.current = false; }, [state?.logged_in]);

  // Mid-session 401 plumbing. When ANY api call elsewhere in the app
  // gets a 401 on a non-auth endpoint, treat it as "the cookie just
  // expired" and re-fetch /api/auth/me. If we come back logged_in:
  // false, AuthGate re-renders to the login screen. The api layer
  // de-dupes concurrent 401s by only firing the handler once per
  // request; here we further guard by a ref so a burst of 401s
  // doesn't queue a thundering herd of /api/auth/me refetches.
  useEffect(() => {
    let pending = false;
    let lastFire = 0;
    const handler = (_path: string) => {
      // Throttle: at most one refresh per 2s. Catches the typical
      // burst of "every visible tab fired a fetch on focus" without
      // burying the backend.
      const now = Date.now();
      if (pending || now - lastFire < 2000) return;
      pending = true;
      lastFire = now;
      refresh().finally(() => { pending = false; });
    };
    api_registerSessionExpiredHandler(handler);
    return () => { api_registerSessionExpiredHandler(null); };
  }, [refresh]);

  const logout = useCallback(async () => {
    try { await api.post("/api/auth/logout"); } catch {}
    await refresh();
  }, [refresh]);

  // Hand the wall back to itself: ends the PIN unlock and restores the
  // wall's own session, so the hallway is a calendar again rather than
  // the last person's account.
  const endWallUnlock = useCallback(async () => {
    try { await api.post("/api/auth/wall-return"); } catch { /* fall through to refresh */ }
    await refresh();
  }, [refresh]);

  // Three minutes without anyone touching the tablet ends the unlock.
  // This has to live up here, not in the wall screen: somebody who
  // PIN-switches and walks off leaves the tablet on /chat, and the
  // background polling there would otherwise keep them signed in for
  // as long as the app is open. Touch — not traffic — is what counts.
  const unlockMs = (state?.wall_unlock_seconds || 180) * 1000;
  useEffect(() => {
    if (!state?.wall_unlock) return;
    let timer = 0;
    const arm = () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        void endWallUnlock().then(() => {
          // Somebody unlocked the wall, went to /chat and walked away.
          // Handing the session back is not enough — the tablet has to
          // go back to being the wall, and the native idle-watch skips
          // /chat on purpose so it will not do it for us.
          if (inWrapper && !window.location.pathname.startsWith("/r/ambient")) {
            window.location.assign("/r/ambient");
          }
        });
      }, unlockMs);
    };
    arm();
    const events = ["pointerdown", "keydown"] as const;
    events.forEach(e => window.addEventListener(e, arm, { passive: true }));
    return () => {
      window.clearTimeout(timer);
      events.forEach(e => window.removeEventListener(e, arm));
    };
  }, [state?.wall_unlock, unlockMs, endWallUnlock, inWrapper]);

  // An invite's QR code lands here; it works with or without a session
  // (a parent testing the code on their own phone sees the same flow).
  if (window.location.pathname.startsWith("/r/join")) {
    return <JoinScreen />;
  }

  if (loading && !state) {
    return <FullPageSpinner />;
  }

  if (error && !state) {
    return (
      <FullPageMessage
        title="Can't reach Yorik"
        body={
          <>
            <p className="text-muted-foreground">{error}</p>
            <button
              onClick={refresh}
              className="mt-4 px-4 py-2 rounded-lg bg-primary text-primary-foreground text-sm font-medium"
            >
              Retry
            </button>
          </>
        }
      />
    );
  }

  if (state?.setup_required) {
    return <SetupScreen onSetupComplete={refresh} />;
  }

  if (!state?.logged_in || !state.user) {
    return <LoginScreen onLoggedIn={refresh} />;
  }

  // Signed in on the way to another app (Immich asks Yorik to sign the
  // person in — backend/oidc.py sent them here first): go back there.
  // Only a same-origin /oidc/authorize path is followed.
  const oidcNext = new URLSearchParams(window.location.search).get("oidc_next");
  if (oidcNext && oidcNext.startsWith("/oidc/authorize?")) {
    window.location.replace(oidcNext);
    return <FullPageSpinner />;
  }

  if (!state.user.onboarded_at) {
    return (
      <OnboardingWizard
        user={state.user}
        isTenant={!!state.is_tenant}
        onComplete={refresh}
        onSkip={refresh}
      />
    );
  }

  return (
    <AuthContext.Provider value={{ user: state.user, isTenant: !!state.is_tenant, refresh, logout,
                                  wallUnlock: !!state.wall_unlock, endWallUnlock }}>
      {children}
    </AuthContext.Provider>
  );
}

function FullPageSpinner() {
  return (
    <div className="h-screen flex flex-col items-center justify-center bg-background text-foreground">
      <div className="w-12 h-12 rounded-2xl bg-gradient-to-br from-violet-500/30 to-blue-500/30 flex items-center justify-center mb-3">
        <Sparkles className="w-5 h-5 text-violet-500" />
      </div>
      <Loader2 className="w-5 h-5 animate-spin text-muted-foreground" />
    </div>
  );
}

function FullPageMessage({ title, body }: { title: string; body: React.ReactNode }) {
  return (
    <div className="h-screen flex flex-col items-center justify-center bg-background text-foreground px-6 text-center">
      <div className="max-w-md">
        <div className="font-semibold text-lg mb-2">{title}</div>
        <div className="text-sm">{body}</div>
      </div>
    </div>
  );
}
