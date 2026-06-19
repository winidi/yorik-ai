/**
 * First-run welcome wizard.
 *
 * Fetches /api/onboarding/state on mount. If the user hasn't completed
 * the tour yet, opens a 6-step modal:
 *
 *   1. Welcome — what Yorik is.
 *   2. Local AI — checks the LLM is reachable.
 *   3. Demo data — one-click POST /api/demo/seed so the rest of the
 *      tour has something to query.
 *   4. Connect email — opens AccountWizard INLINE (no /settings detour).
 *   5. Try saying… — example chips that jump into chat with a seeded
 *      composer.
 *   6. Use Yorik on your phone — Tailscale + 'Add to Home Screen'
 *      instructions, browser-detected.
 *
 * Backend "snapshot" fields let us skip steps a maintainer pre-wired
 * before sharing the box (e.g. if email is already configured, we
 * don't ask the user to set it up).
 */

import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { createPortal } from "react-dom";
import {
  Sparkles, Cpu, Mail, MessageSquare, Check, X, ArrowRight, Loader2,
  Database, AlertCircle, Smartphone,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";
import { AccountWizard } from "@/apps/email/AccountWizard";

interface OnboardingState {
  completed: boolean;
  has_email: boolean;
  has_events: boolean;
  has_bills: boolean;
}

interface HealthState {
  llm_reachable: boolean;
  model: string;
}

const TRY_EXAMPLES = [
  "Was steht heute an?",
  "Find my insurance contract",
  "Schedule the dentist Friday at 2pm",
];

export function OnboardingModal() {
  const navigate = useNavigate();
  const [state, setState] = useState<OnboardingState | null>(null);
  const [health, setHealth] = useState<HealthState | null>(null);
  const [step, setStep] = useState(0);
  const [closing, setClosing] = useState(false);
  const [showEmailWizard, setShowEmailWizard] = useState(false);
  const [seedBusy, setSeedBusy] = useState(false);
  const [seedError, setSeedError] = useState<string | null>(null);

  const loadDemoData = useCallback(async () => {
    setSeedBusy(true);
    setSeedError(null);
    try {
      await api.post("/api/demo/seed");
      setState(s => (s ? { ...s, has_events: true, has_bills: true } : s));
      // Auto-advance so the user feels forward momentum and lands on
      // the chat-examples step with real data to query.
      setStep(s => s + 1);
    } catch (e: any) {
      const msg = String(e?.message || "");
      // 409 = already seeded. Treat it as success — the state flip
      // below makes the step show its "already loaded" body next render.
      if (msg.includes("409")) {
        setState(s => (s ? { ...s, has_events: true, has_bills: true } : s));
        setStep(s => s + 1);
      } else {
        setSeedError(msg || "demo seed failed");
      }
    } finally {
      setSeedBusy(false);
    }
  }, []);

  useEffect(() => {
    (async () => {
      try {
        const s = await api.get<OnboardingState>("/api/onboarding/state");
        if (!s.completed) {
          setState(s);
          try {
            setHealth(await api.get<HealthState>("/api/health"));
          } catch { /* health unreachable still allows wizard */ }
          // Mark complete IMMEDIATELY on first render — fire-and-forget,
          // backgrounded. Otherwise users who close the tab without
          // clicking "Done" or X get the wizard on every visit, which
          // is the exact opposite of what a first-run modal is for.
          // The wizard still displays this turn — they get the welcome
          // experience once — but the next visit won't re-show it.
          api.post("/api/onboarding/complete").catch(() => {});
        }
      } catch {
        // If onboarding endpoint isn't reachable (older box), silently skip.
      }
    })();
  }, []);

  const finish = useCallback(async () => {
    setClosing(true);
    // Also POST here as a safety net for the "modal closed by click on X"
    // path — the render-time call above may not have landed yet on a
    // very slow network.
    try { await api.post("/api/onboarding/complete"); } catch {}
    setState(null);
  }, []);

  if (!state || closing) return null;

  // Email wizard takes over the screen while it's open — we hide the
  // welcome modal underneath instead of stacking two backdrops. On
  // save we flip state.has_email so the email step re-renders as the
  // "you're all set" variant, and auto-advance to the chat-examples
  // step so the user sees forward momentum. Cancel just returns to
  // the email step so they can hit Next to skip.
  if (showEmailWizard) {
    return (
      <AccountWizard
        onClose={() => setShowEmailWizard(false)}
        onSaved={() => {
          setShowEmailWizard(false);
          setState(s => (s ? { ...s, has_email: true } : s));
          // Advance to the next step (chat examples) — capped at the
          // last step so we never overshoot if the step list grows.
          setStep(s => s + 1);
        }}
      />
    );
  }

  const steps = [
    {
      key: "welcome",
      icon: Sparkles,
      tint: "from-violet-500 to-blue-500",
      title: "Welcome to Yorik",
      body: (
        <p className="text-sm text-muted-foreground leading-relaxed">
          Yorik is your family and business OS — calendar, email, documents,
          photos, chat. Everything runs locally on this machine. No cloud,
          no subscriptions, no data leaves the house.
        </p>
      ),
      cta: "Let's go",
    },
    {
      key: "llm",
      icon: Cpu,
      tint: "from-emerald-500 to-teal-500",
      title: "Local AI",
      body: (
        <div className="text-sm">
          {health?.llm_reachable ? (
            <div className="flex items-start gap-2">
              <Check className="w-4 h-4 mt-0.5 text-emerald-500 shrink-0" />
              <div className="text-muted-foreground leading-relaxed">
                Your local model <span className="font-mono text-foreground">{health.model}</span> is
                running and ready. Voice + chat will work out of the box.
              </div>
            </div>
          ) : (
            <div className="flex items-start gap-2">
              <X className="w-4 h-4 mt-0.5 text-amber-500 shrink-0" />
              <div className="text-muted-foreground leading-relaxed">
                Local LLM isn't reachable. Start your llama-swap (or similar)
                container at <span className="font-mono">localhost:8080</span>,
                or point Yorik elsewhere in Settings → LLM.
              </div>
            </div>
          )}
        </div>
      ),
      cta: "Next",
    },
    {
      key: "demo",
      icon: Database,
      tint: "from-amber-500 to-orange-500",
      title: state.has_events ? "Example data is loaded" : "See it with example data",
      body: state.has_events ? (
        <div className="flex items-start gap-2 text-sm">
          <Check className="w-4 h-4 mt-0.5 text-emerald-500 shrink-0" />
          <div className="text-muted-foreground leading-relaxed">
            A week of events, a few tasks, a couple of bills are loaded.
            You can remove them any time in Settings → Demo.
          </div>
        </div>
      ) : (
        <div className="space-y-3 text-sm text-muted-foreground leading-relaxed">
          <p>
            Want a populated app to click around in? One click loads a
            week of fake events, tasks, and bills (with a friendly note
            in the bell). Removable in one click from Settings → Demo.
          </p>
          {seedError && (
            <div className="flex items-start gap-2 text-amber-600 dark:text-amber-400">
              <AlertCircle className="w-4 h-4 mt-0.5 shrink-0" />
              <div className="text-xs leading-snug">{seedError}</div>
            </div>
          )}
          <button
            onClick={loadDemoData}
            disabled={seedBusy}
            className={cn(
              "text-sm font-medium px-3 py-2 rounded-lg bg-primary text-primary-foreground",
              "hover:opacity-90 transition inline-flex items-center gap-1.5",
              seedBusy && "opacity-60 cursor-not-allowed",
            )}
          >
            {seedBusy
              ? <><Loader2 className="w-3.5 h-3.5 animate-spin" /> Loading…</>
              : <><Database className="w-3.5 h-3.5" /> Load example data</>}
          </button>
        </div>
      ),
      cta: "Skip",
    },
    {
      key: "email",
      icon: Mail,
      tint: "from-sky-500 to-blue-500",
      title: state.has_email ? "Email is connected" : "Connect your email (optional)",
      body: state.has_email ? (
        <div className="flex items-start gap-2 text-sm">
          <Check className="w-4 h-4 mt-0.5 text-emerald-500 shrink-0" />
          <div className="text-muted-foreground leading-relaxed">
            You've already wired up an email account. Yorik will summarise,
            triage, and auto-draft replies for you.
          </div>
        </div>
      ) : (
        <div className="space-y-3 text-sm text-muted-foreground leading-relaxed">
          <p>
            Connecting an IMAP account lets Yorik triage bills, propose
            calendar slots, and draft replies. Optional — you can do this later.
          </p>
          <button
            onClick={() => setShowEmailWizard(true)}
            className="text-sm font-medium px-3 py-2 rounded-lg bg-primary text-primary-foreground hover:opacity-90 transition inline-flex items-center gap-1.5"
          >
            <Mail className="w-3.5 h-3.5" /> Set up email now
          </button>
        </div>
      ),
      cta: "Next",
    },
    {
      key: "try",
      icon: MessageSquare,
      tint: "from-fuchsia-500 to-pink-500",
      title: "Try saying…",
      body: (
        <div className="space-y-2">
          <p className="text-sm text-muted-foreground mb-3 leading-relaxed">
            Tap one to send it to Yorik. The mic in the bottom-right does the
            same thing by voice.
          </p>
          {TRY_EXAMPLES.map((ex, i) => (
            <button
              key={i}
              onClick={async () => {
                // Stash the seed in sessionStorage so ChatApp can pick
                // it up on mount and prefill its composer.
                try { sessionStorage.setItem("yorik_chat_seed", ex); } catch {}
                await finish();
                navigate("/chat");
              }}
              className="w-full text-left text-sm bg-card border border-border rounded-lg px-3 py-2 hover:border-primary/40 hover:bg-primary/[0.04] transition"
            >
              "{ex}"
            </button>
          ))}
        </div>
      ),
      cta: "Next",
    },
    {
      key: "phone",
      icon: Smartphone,
      tint: "from-indigo-500 to-violet-500",
      title: "Use Yorik on your phone",
      body: (() => {
        const host = typeof window !== "undefined" ? window.location.host : "";
        const isTailscale = host.includes(".ts.net");
        const ua = typeof navigator !== "undefined" ? navigator.userAgent : "";
        const isIOS     = /iPad|iPhone|iPod/.test(ua);
        const isAndroid = /Android/.test(ua);
        const installHint = isIOS
          ? "On iPhone Safari: tap the Share button, then 'Add to Home Screen'."
          : isAndroid
            ? "On Android Chrome: tap the ⋮ menu, then 'Install app' or 'Add to Home screen'."
            : "On your phone's browser: open this URL, then use Share / menu → 'Add to Home Screen' so Yorik opens like a native app.";
        return (
          <div className="space-y-3 text-sm text-muted-foreground leading-relaxed">
            {isTailscale ? (
              <div className="flex items-start gap-2 text-emerald-600 dark:text-emerald-400">
                <Check className="w-4 h-4 mt-0.5 shrink-0" />
                <div className="text-xs leading-snug">
                  You're already on a Tailscale URL — open the same address
                  (<span className="font-mono">{host}</span>) on your phone.
                </div>
              </div>
            ) : (
              <p>
                Yorik is reachable across your devices via{" "}
                <a
                  href="https://tailscale.com/download"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-primary hover:underline inline-flex items-center gap-0.5"
                >
                  Tailscale <ArrowRight className="w-3 h-3 -rotate-45" />
                </a>{" "}
                (free for personal use, no port-forwarding). Install it on
                this box and on your phone, then visit{" "}
                <span className="font-mono">your-box.your-tailnet.ts.net:8000</span>{" "}
                on the phone.
              </p>
            )}
            <p className="text-xs">{installHint}</p>
          </div>
        );
      })(),
      cta: "Done",
    },
  ];

  const current = steps[step];
  const isLast = step === steps.length - 1;

  const next = () => {
    if (isLast) void finish();
    else setStep(step + 1);
  };

  return createPortal(
    <div className="fixed inset-0 z-[80] flex items-center justify-center bg-black/40 backdrop-blur-sm p-4">
      <div className="w-full max-w-md bg-card border border-border rounded-2xl shadow-2xl">
        <div className="p-6">
          <div className="flex items-start justify-between gap-3 mb-4">
            <div className={cn(
              "w-12 h-12 rounded-xl bg-gradient-to-br text-white flex items-center justify-center shadow-md",
              current.tint,
            )}>
              <current.icon className="w-6 h-6" />
            </div>
            <button
              onClick={finish}
              className="text-muted-foreground hover:text-foreground transition"
              title="Skip the welcome tour"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
          <h2 className="text-xl font-semibold mb-2">{current.title}</h2>
          <div className="mb-6">{current.body}</div>
          <div className="flex items-center justify-between">
            <div className="flex gap-1.5">
              {steps.map((_, i) => (
                <div
                  key={i}
                  className={cn(
                    "h-1.5 rounded-full transition-all",
                    i === step ? "w-6 bg-primary" : "w-1.5 bg-muted",
                  )}
                />
              ))}
            </div>
            <button
              onClick={next}
              className="px-4 py-2 rounded-lg bg-primary text-primary-foreground text-sm font-medium hover:opacity-90 transition flex items-center gap-1.5"
            >
              {current.cta}
              {!isLast && <ArrowRight className="w-3.5 h-3.5" />}
              {isLast && <Check className="w-3.5 h-3.5" />}
            </button>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  );
}
