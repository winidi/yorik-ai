/**
 * First-run setup screen — shown when /api/auth/me reports
 * `setup_required: true` (i.e. no user has a password yet). Creates
 * the first admin account in one step.
 */

import { useState } from "react";
import { Loader2, Sparkles, Mail, Lock, User as UserIcon, AlertCircle, Info, Copy, CheckCircle2, Home } from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";

interface Props {
  onSetupComplete: () => void;
}

// Phase F-lite: two flavours of token in the URL.
//   * ?invite=<token>  — INITIAL setup; creates the first admin.
//   * ?reset=<token>   — PASSWORD RESET for an existing admin (issued
//                         by the host operator's Households UI).
// We read whichever is present and remember which kind so the UI
// renders the right copy + form, and POSTs to the right endpoint.
// Server-side both kinds resolve through the same invite_lookup
// endpoint; the URL param is purely a frontend hint.
type TokenKind = "invite" | "reset";
function readToken(): { kind: TokenKind; value: string } | null {
  try {
    const p = new URLSearchParams(window.location.search);
    for (const kind of ["reset", "invite"] as const) {
      const raw = p.get(kind);
      if (!raw) continue;
      const trimmed = raw.trim();
      if (trimmed.length < 8 || trimmed.length > 200) continue;
      return { kind, value: trimmed };
    }
    return null;
  } catch {
    return null;
  }
}

interface SetupResponse {
  ok: boolean;
  user_id: number;
  provisioning?: {
    paperless?: {
      ok: boolean;
      fallback_password?: string;
      fallback_password_note?: string;
    };
    immich?: { ok: boolean };
  };
}

export function SetupScreen({ onSetupComplete }: Props) {
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [tokenInfo] = useState(() => readToken());
  const inviteToken = tokenInfo?.kind === "invite" ? tokenInfo.value : null;
  const resetToken  = tokenInfo?.kind === "reset"  ? tokenInfo.value : null;
  // When the backend had to generate a separate strong password for
  // Paperless (because the user's chosen one didn't meet Paperless's
  // complexity rules), we MUST display it once before completing —
  // otherwise the user thinks their chosen password works for
  // Paperless's web UI, can't log in, and has no recovery path.
  const [paperlessFallback, setPaperlessFallback] = useState<{
    password: string; note: string;
  } | null>(null);
  const [copied, setCopied] = useState(false);

  const mismatch = confirm.length > 0 && password !== confirm;
  const tooShort = password.length > 0 && password.length < 8;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy || mismatch || tooShort) return;
    setBusy(true);
    setErr(null);
    try {
      if (resetToken) {
        // Reset-via-invite path — go straight to onSetupComplete on
        // success since there's no Paperless-fallback-password
        // window to display (no new external accounts created).
        await api.post("/api/auth/reset-password-via-invite", {
          invite_token: resetToken,
          new_password: password,
        });
        onSetupComplete();
        return;
      }
      const r = await api.post<SetupResponse>("/api/auth/setup", {
        name: name.trim(),
        email: email.trim(),
        password,
        invite_token: inviteToken,
      });
      const fb = r.provisioning?.paperless?.fallback_password;
      if (fb) {
        // Pause on the fallback-password screen until the user
        // explicitly confirms they've saved it. We never see this
        // value again — backend stores it encrypted; this is the
        // only one-shot the user gets in cleartext.
        setPaperlessFallback({
          password: fb,
          note: r.provisioning?.paperless?.fallback_password_note ||
                "Save this password — Yorik uses it for Paperless and you'll need it for direct Paperless logins.",
        });
      } else {
        onSetupComplete();
      }
    } catch (e: any) {
      setErr(e.message || "Setup failed");
    } finally {
      setBusy(false);
    }
  }

  async function copyFallback() {
    if (!paperlessFallback) return;
    try {
      await navigator.clipboard.writeText(paperlessFallback.password);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // clipboard API can fail in non-HTTPS contexts; fall back silently.
    }
  }

  // ── Stage 2: paperless-fallback-password reveal (one-shot) ──
  if (paperlessFallback) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-background text-foreground px-6 login-bg">
        <div className="w-full max-w-md">
          <div className="flex flex-col items-center text-center mb-6">
            <div className="w-14 h-14 rounded-2xl bg-amber-500/20 flex items-center justify-center mb-4 shadow-lg">
              <AlertCircle className="w-6 h-6 text-amber-600" />
            </div>
            <div className="text-2xl font-semibold">Save this Paperless password</div>
            <div className="text-sm text-muted-foreground mt-1 max-w-sm">
              Your chosen password didn't meet Paperless's complexity rules,
              so we generated a stronger one for it. You'll need this for
              direct logins to the Paperless web UI. Yorik uses it
              automatically.
            </div>
          </div>

          <div className="bg-card border border-amber-500/40 rounded-2xl shadow-xl p-6 space-y-4">
            <div className="text-[11px] uppercase tracking-wider text-muted-foreground">
              Paperless password (one-time display)
            </div>
            <div className="flex items-center gap-2">
              <code className="flex-1 font-mono text-sm bg-muted/60 px-3 py-2.5 rounded-md break-all select-all">
                {paperlessFallback.password}
              </code>
              <button
                onClick={copyFallback}
                title="Copy to clipboard"
                className="w-10 h-10 inline-flex items-center justify-center rounded-md bg-muted hover:bg-muted/70 text-foreground transition shrink-0"
              >
                {copied
                  ? <CheckCircle2 className="w-4 h-4 text-emerald-600" />
                  : <Copy className="w-4 h-4" />}
              </button>
            </div>
            <div className="text-[11px] text-muted-foreground leading-relaxed">
              {paperlessFallback.note}
            </div>
            <button
              onClick={onSetupComplete}
              className="w-full h-10 rounded-md font-medium text-sm bg-gradient-to-r from-violet-500 to-blue-500 hover:from-violet-600 hover:to-blue-600 text-white shadow-md transition"
            >
              I saved it — continue
            </button>
          </div>

          <div className="text-[11px] text-muted-foreground text-center mt-6">
            Stored encrypted on this machine. Find it later under Settings → Connectors → Paperless.
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-background text-foreground px-6 login-bg">
      <div className="w-full max-w-md">
        <div className="flex flex-col items-center text-center mb-6">
          <div className={cn(
            "w-14 h-14 rounded-2xl flex items-center justify-center mb-4 shadow-lg",
            resetToken
              ? "bg-gradient-to-br from-amber-500/30 to-red-500/30"
              : inviteToken
              ? "bg-gradient-to-br from-orange-500/30 to-amber-500/30"
              : "bg-gradient-to-br from-violet-500/30 to-blue-500/30",
          )}>
            {resetToken
              ? <Lock className="w-6 h-6 text-amber-600" />
              : inviteToken
              ? <Home className="w-6 h-6 text-orange-500" />
              : <Sparkles className="w-6 h-6 text-violet-500" />}
          </div>
          <div className="text-2xl font-semibold">
            {resetToken ? "Reset your password" :
             inviteToken ? "Set up your household" :
             "Set up your Yorik"}
          </div>
          <div className="text-sm text-muted-foreground mt-1 max-w-sm">
            {resetToken
              ? "The host operator issued you a reset link. Pick a new password and you're back in. Your data is untouched."
              : inviteToken
              ? "You've been invited to your own private Yorik. Pick the credentials you'll use to sign in — only you will have access to this household's data."
              : "First run — create the owner account. You'll be the admin, with full access to settings and other users you invite later."}
          </div>
        </div>

        <form
          onSubmit={submit}
          className="bg-card border border-border rounded-2xl shadow-xl p-6 space-y-3"
        >
          {/* Name + email only matter on initial setup; for reset
              they're already known to the backend (encoded in the
              invite's target_email). Hiding them on the reset path
              avoids confusing the user about whether they need to
              re-state who they are. */}
          {!resetToken && (
            <>
              <label className="block">
                <div className="text-[11px] text-muted-foreground mb-1">Your name</div>
                <div className="relative">
                  <UserIcon className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground" />
                  <input
                    autoFocus
                    value={name}
                    onChange={e => setName(e.target.value)}
                    placeholder="Jane Doe"
                    className="w-full h-10 pl-9 pr-3 rounded-md bg-muted/60 text-sm focus:outline-none focus:bg-muted focus:ring-2 focus:ring-ring/30 transition"
                    required
                  />
                </div>
              </label>

              <label className="block">
                <div className="text-[11px] text-muted-foreground mb-1">Email</div>
                <div className="relative">
                  <Mail className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground" />
                  <input
                    type="email"
                    value={email}
                    onChange={e => setEmail(e.target.value)}
                    placeholder="you@example.com"
                    className="w-full h-10 pl-9 pr-3 rounded-md bg-muted/60 text-sm focus:outline-none focus:bg-muted focus:ring-2 focus:ring-ring/30 transition"
                    required
                  />
                </div>
              </label>
            </>
          )}

          <label className="block">
            <div className="text-[11px] text-muted-foreground mb-1">Password</div>
            <div className="relative">
              <Lock className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground" />
              <input
                type="password"
                autoComplete="new-password"
                value={password}
                onChange={e => setPassword(e.target.value)}
                minLength={8}
                className={cn(
                  "w-full h-10 pl-9 pr-3 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-ring/30 transition",
                  tooShort
                    ? "bg-red-500/5 border border-red-500/30"
                    : "bg-muted/60 focus:bg-muted",
                )}
                required
              />
            </div>
            <div className={cn(
              "text-[10px] mt-1",
              tooShort ? "text-red-500" : "text-muted-foreground",
            )}>
              At least 8 characters.
            </div>
            <div className="mt-2 text-[10px] text-amber-700 dark:text-amber-400 bg-amber-500/10 border border-amber-500/20 rounded-md px-2 py-1.5 flex items-start gap-1.5 leading-relaxed">
              <Info className="w-3 h-3 mt-0.5 shrink-0" />
              <span>
                <strong>Alpha note:</strong> 8 characters is the current minimum. Beta will require
                stronger passwords (longer, common-password check). Yorik holds your real personal
                data — pick something solid now so you don't have to reset later.
              </span>
            </div>
          </label>

          <label className="block">
            <div className="text-[11px] text-muted-foreground mb-1">Confirm password</div>
            <div className="relative">
              <Lock className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground" />
              <input
                type="password"
                autoComplete="new-password"
                value={confirm}
                onChange={e => setConfirm(e.target.value)}
                className={cn(
                  "w-full h-10 pl-9 pr-3 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-ring/30 transition",
                  mismatch
                    ? "bg-red-500/5 border border-red-500/30"
                    : "bg-muted/60 focus:bg-muted",
                )}
                required
              />
            </div>
            {mismatch && (
              <div className="text-[10px] text-red-500 mt-1">Passwords don't match.</div>
            )}
          </label>

          {err && (
            <div className="text-xs text-red-500 bg-red-500/10 border border-red-500/20 rounded-md px-3 py-2 flex items-start gap-2">
              <AlertCircle className="w-3.5 h-3.5 mt-0.5 shrink-0" />
              <span>{err}</span>
            </div>
          )}

          <button
            type="submit"
            disabled={
              busy || mismatch || tooShort || !password ||
              (!resetToken && (!name.trim() || !email.trim()))
            }
            className={cn(
              "w-full h-10 rounded-md font-medium text-sm inline-flex items-center justify-center gap-2 transition",
              !busy && !mismatch && !tooShort && password &&
              (resetToken || (name.trim() && email.trim()))
                ? "bg-gradient-to-r from-violet-500 to-blue-500 hover:from-violet-600 hover:to-blue-600 text-white shadow-md"
                : "bg-muted text-muted-foreground cursor-not-allowed",
            )}
          >
            {busy
              ? <><Loader2 className="w-4 h-4 animate-spin" /> {resetToken ? "Resetting…" : "Setting up…"}</>
              : (resetToken ? "Set new password" : "Create owner account")}
          </button>
        </form>

        <div className="text-[11px] text-muted-foreground text-center mt-6">
          This password is hashed with bcrypt and stored only on this machine.
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
