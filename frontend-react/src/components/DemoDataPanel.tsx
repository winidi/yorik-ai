/**
 * "Try with example data" panel for the Home screen.
 *
 * Admin-only. Renders only when (a) the viewer is an admin, (b) the
 * box is genuinely empty across every dimension we can cheaply
 * check (events, tasks, bills, documents, photos, email accounts,
 * WhatsApp), and (c) demo data isn't already loaded. Click →
 * POST /api/demo/seed → reload so the now-populated state is visible.
 *
 * For non-admins this returns null — there's no point dangling a
 * dismissable seed CTA at someone who can't act on it, and the
 * underlying /api/demo/seed endpoint is admin-gated anyway. Members
 * see an empty home until the admin sets things up.
 *
 * When demo IS loaded, the small "Demo data loaded — remove?" banner
 * is admin-only too (same reasoning).
 */

import { useCallback, useEffect, useState } from "react";
import { Sparkles, X, Loader2 } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useAuth } from "@/components/AuthGate";

interface DemoStatus {
  seeded: boolean;
  seeded_at: string | null;
  counts: Record<string, number>;
  total: number;
}

interface OnboardingState {
  completed:     boolean;
  has_email:     boolean;
  has_events:    boolean;
  has_bills:     boolean;
  has_tasks:     boolean;
  has_documents: boolean;
  has_photos:    boolean;
  has_whatsapp:  boolean;
}

export function DemoDataPanel() {
  const auth = useAuth();
  const isAdmin = auth.user.role === "admin" || auth.user.role === "platform_admin";
  const [status, setStatus] = useState<DemoStatus | null>(null);
  const [hasRealData, setHasRealData] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    if (!isAdmin) return;  // Non-admins skip the probes entirely.
    try {
      const s = await api.get<DemoStatus>("/api/demo/status");
      setStatus(s);
      // Broad "is this box actually in use?" probe. Any positive signal
      // hides the demo CTA — the admin clearly doesn't need it. The
      // /api/onboarding/state endpoint bundles the cheap server-side
      // checks (per the comment block on its handler).
      const o = await api.get<OnboardingState>("/api/onboarding/state");
      const hasReal = o.has_email
                    || o.has_events
                    || o.has_bills
                    || o.has_tasks
                    || o.has_documents
                    || o.has_photos
                    || o.has_whatsapp;
      setHasRealData(hasReal);
    } catch {
      // Silent — the home screen still works if we can't decide.
    }
  }, [isAdmin]);

  useEffect(() => { refresh(); }, [refresh]);

  const seed = useCallback(async () => {
    setBusy(true);
    try {
      await api.post("/api/demo/seed");
      // Reload the page so every app that already fetched empty state
      // gets the new data without us having to plumb refresh signals
      // through each one.
      window.location.reload();
    } catch (e: any) {
      alert(`Couldn't load demo data: ${e?.message || e}`);
      setBusy(false);
    }
  }, []);

  const remove = useCallback(async () => {
    if (!confirm("Remove all demo data? Anything you added on top stays untouched.")) return;
    setBusy(true);
    try {
      await api.delete("/api/demo");
      window.location.reload();
    } catch (e: any) {
      alert(`Couldn't remove demo data: ${e?.message || e}`);
      setBusy(false);
    }
  }, []);

  // Non-admins: never render — this CTA + the seed endpoint are admin-only.
  if (!isAdmin) return null;
  // Loading state — render nothing rather than a flash.
  if (status === null || hasRealData === null) return null;

  // Already loaded: small "remove" banner near the top.
  if (status.seeded) {
    return (
      <div className="mb-4 px-3 py-2 rounded-lg bg-violet-500/[0.06] border border-violet-500/30 text-xs flex items-center gap-2">
        <Sparkles className="w-3.5 h-3.5 text-violet-500 shrink-0" />
        <div className="flex-1">
          <span className="font-medium text-violet-500">Demo data loaded</span>
          <span className="text-muted-foreground"> · {status.total} example items across calendar, tasks, bills.</span>
        </div>
        <button
          onClick={remove}
          disabled={busy}
          className="text-[11px] px-2 py-0.5 rounded border border-violet-500/30 hover:bg-violet-500/10 transition flex items-center gap-1 disabled:opacity-50"
        >
          {busy ? <Loader2 className="w-3 h-3 animate-spin" /> : <X className="w-3 h-3" />}
          Remove
        </button>
      </div>
    );
  }

  // Not loaded + box has real data: don't pester.
  if (hasRealData) return null;

  // Empty box + no demo: offer the install.
  return (
    <div className="mb-4 rounded-2xl bg-gradient-to-br from-violet-500/[0.08] to-blue-500/[0.08] border border-violet-500/30 p-5">
      <div className="flex items-start gap-4">
        <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-violet-500 to-blue-500 flex items-center justify-center shadow-md shrink-0">
          <Sparkles className="w-5 h-5 text-white" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="font-semibold mb-1">Want to try Yorik with example data?</div>
          <div className="text-sm text-muted-foreground leading-relaxed">
            We'll add ~20 realistic calendar events, tasks, and bills so
            you can see how Yorik feels populated. Everything is dated
            relative to today and can be removed in one click from here
            or Settings.
          </div>
          <button
            onClick={seed}
            disabled={busy}
            className={cn(
              "mt-3 inline-flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition shadow-sm",
              "bg-gradient-to-r from-violet-500 to-blue-500 text-white hover:opacity-90",
              "disabled:opacity-60 disabled:cursor-not-allowed",
            )}
          >
            {busy
              ? <><Loader2 className="w-4 h-4 animate-spin" /> Adding…</>
              : <><Sparkles className="w-4 h-4" /> Add example data</>}
          </button>
        </div>
      </div>
    </div>
  );
}
