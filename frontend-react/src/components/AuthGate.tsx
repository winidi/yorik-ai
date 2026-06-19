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

import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { Loader2, Sparkles } from "lucide-react";
import { api, registerSessionExpiredHandler as api_registerSessionExpiredHandler } from "@/lib/api";
import type { AuthMe, YorikUser } from "@/lib/api";
import { LoginScreen } from "./LoginScreen";
import { SetupScreen } from "./SetupScreen";
import { OnboardingWizard } from "./OnboardingWizard";

interface AuthContextValue {
  user: YorikUser;
  isTenant: boolean;
  refresh: () => Promise<void>;
  logout: () => Promise<void>;
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
    <AuthContext.Provider value={{ user: state.user, isTenant: !!state.is_tenant, refresh, logout }}>
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
