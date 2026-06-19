/**
 * Login screen — shown when a user lands on Yorik without a valid
 * session cookie (and at least one user with a password exists).
 *
 * Visual: centered card on a soft gradient page, Yorik mark, email +
 * password, big sign-in button, error toast below. Nothing fancy —
 * the goal is "I know I'm in the right place and I can get in".
 */

import { useState } from "react";
import { Loader2, Mail, Lock, AlertCircle } from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";

interface Props {
  onLoggedIn: () => void;
}

export function LoginScreen({ onLoggedIn }: Props) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setErr(null);
    try {
      await api.post("/api/auth/login", { email: email.trim(), password });
      onLoggedIn();
    } catch (e: any) {
      setErr(e.message || "Sign-in failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-background text-foreground px-6 login-bg">
      <div className="w-full max-w-sm">
        <div className="flex flex-col items-center text-center mb-8">
          <img
            src="/r/butler-mark.png"
            alt="Yorik"
            className="w-16 h-16 object-contain mb-4 dark:invert"
          />
          <div className="text-2xl font-semibold">Welcome back</div>
          <div className="text-sm text-muted-foreground mt-1">
            Sign in to your Yorik
          </div>
        </div>

        <form
          onSubmit={submit}
          className="bg-card border border-border rounded-2xl shadow-xl p-6 space-y-3"
        >
          <label className="block">
            <div className="text-[11px] text-muted-foreground mb-1">Email</div>
            <div className="relative">
              <Mail className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground" />
              <input
                autoFocus
                type="email"
                autoComplete="username"
                value={email}
                onChange={e => setEmail(e.target.value)}
                placeholder="you@example.com"
                className="w-full h-10 pl-9 pr-3 rounded-md bg-muted/60 text-sm focus:outline-none focus:bg-muted focus:ring-2 focus:ring-ring/30 transition"
                required
              />
            </div>
          </label>

          <label className="block">
            <div className="text-[11px] text-muted-foreground mb-1">Password</div>
            <div className="relative">
              <Lock className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground" />
              <input
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={e => setPassword(e.target.value)}
                className="w-full h-10 pl-9 pr-3 rounded-md bg-muted/60 text-sm focus:outline-none focus:bg-muted focus:ring-2 focus:ring-ring/30 transition"
                required
                minLength={1}
              />
            </div>
          </label>

          {err && (
            <div className="text-xs text-red-500 bg-red-500/10 border border-red-500/20 rounded-md px-3 py-2 flex items-start gap-2">
              <AlertCircle className="w-3.5 h-3.5 mt-0.5 shrink-0" />
              <span>{err}</span>
            </div>
          )}

          <button
            type="submit"
            disabled={busy || !email.trim() || !password}
            className={cn(
              "w-full h-10 rounded-md font-medium text-sm inline-flex items-center justify-center gap-2 transition",
              !busy && email.trim() && password
                ? "bg-gradient-to-r from-violet-500 to-blue-500 hover:from-violet-600 hover:to-blue-600 text-white shadow-md"
                : "bg-muted text-muted-foreground cursor-not-allowed",
            )}
          >
            {busy ? <><Loader2 className="w-4 h-4 animate-spin" /> Signing in…</> : "Sign in"}
          </button>
        </form>

        <div className="text-[11px] text-muted-foreground text-center mt-6">
          Local-first · your data stays on this machine
        </div>
      </div>
      <style>{`
        .login-bg {
          background-image:
            radial-gradient(circle at 30% 15%, hsl(263 70% 60% / 0.10), transparent 50%),
            radial-gradient(circle at 70% 85%, hsl(200 60% 60% / 0.08), transparent 50%);
        }
      `}</style>
    </div>
  );
}
