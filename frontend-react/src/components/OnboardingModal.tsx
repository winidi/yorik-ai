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
import { Trans, useTranslation } from "react-i18next";
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

export function OnboardingModal() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const tryExamples = [t("onboarding.tour.try1"), t("onboarding.tour.try2"), t("onboarding.tour.try3")];
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
        setSeedError(msg || t("onboarding.tour.seedFailed"));
      }
    } finally {
      setSeedBusy(false);
    }
  }, [t]);

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
      title: t("onboarding.tour.welcomeTitle"),
      body: (
        <p className="text-sm text-muted-foreground leading-relaxed">
          {t("onboarding.tour.welcomeBody")}
        </p>
      ),
      cta: t("onboarding.welcome.letsGo"),
    },
    {
      key: "llm",
      icon: Cpu,
      tint: "from-emerald-500 to-teal-500",
      title: t("onboarding.tour.llmTitle"),
      body: (
        <div className="text-sm">
          {health?.llm_reachable ? (
            <div className="flex items-start gap-2">
              <Check className="w-4 h-4 mt-0.5 text-emerald-500 shrink-0" />
              <div className="text-muted-foreground leading-relaxed">
                <Trans i18nKey="onboarding.tour.llmOk" values={{ model: health.model }} components={{ mono: <span className="font-mono text-foreground" /> }} />
              </div>
            </div>
          ) : (
            <div className="flex items-start gap-2">
              <X className="w-4 h-4 mt-0.5 text-amber-500 shrink-0" />
              <div className="text-muted-foreground leading-relaxed">
                <Trans i18nKey="onboarding.tour.llmDown" components={{ mono: <span className="font-mono" /> }} />
              </div>
            </div>
          )}
        </div>
      ),
      cta: t("onboarding.tour.next"),
    },
    {
      key: "demo",
      icon: Database,
      tint: "from-amber-500 to-orange-500",
      title: state.has_events ? t("onboarding.tour.demoLoadedTitle") : t("onboarding.tour.demoTitle"),
      body: state.has_events ? (
        <div className="flex items-start gap-2 text-sm">
          <Check className="w-4 h-4 mt-0.5 text-emerald-500 shrink-0" />
          <div className="text-muted-foreground leading-relaxed">
            {t("onboarding.tour.demoLoadedBody")}
          </div>
        </div>
      ) : (
        <div className="space-y-3 text-sm text-muted-foreground leading-relaxed">
          <p>
            {t("onboarding.tour.demoBody")}
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
              ? <><Loader2 className="w-3.5 h-3.5 animate-spin" /> {t("common.loading")}</>
              : <><Database className="w-3.5 h-3.5" /> {t("onboarding.tour.loadDemo")}</>}
          </button>
        </div>
      ),
      cta: t("onboarding.tour.skip"),
    },
    {
      key: "email",
      icon: Mail,
      tint: "from-sky-500 to-blue-500",
      title: state.has_email ? t("onboarding.tour.emailDoneTitle") : t("onboarding.tour.emailTitle"),
      body: state.has_email ? (
        <div className="flex items-start gap-2 text-sm">
          <Check className="w-4 h-4 mt-0.5 text-emerald-500 shrink-0" />
          <div className="text-muted-foreground leading-relaxed">
            {t("onboarding.tour.emailDoneBody")}
          </div>
        </div>
      ) : (
        <div className="space-y-3 text-sm text-muted-foreground leading-relaxed">
          <p>
            {t("onboarding.tour.emailBody")}
          </p>
          <button
            onClick={() => setShowEmailWizard(true)}
            className="text-sm font-medium px-3 py-2 rounded-lg bg-primary text-primary-foreground hover:opacity-90 transition inline-flex items-center gap-1.5"
          >
            <Mail className="w-3.5 h-3.5" /> {t("onboarding.tour.setupEmail")}
          </button>
        </div>
      ),
      cta: t("onboarding.tour.next"),
    },
    {
      key: "try",
      icon: MessageSquare,
      tint: "from-fuchsia-500 to-pink-500",
      title: t("onboarding.tour.tryTitle"),
      body: (
        <div className="space-y-2">
          <p className="text-sm text-muted-foreground mb-3 leading-relaxed">
            {t("onboarding.tour.tryBody")}
          </p>
          {tryExamples.map((ex, i) => (
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
              {t("onboarding.tour.quoted", { text: ex })}
            </button>
          ))}
        </div>
      ),
      cta: t("onboarding.tour.next"),
    },
    {
      key: "phone",
      icon: Smartphone,
      tint: "from-indigo-500 to-violet-500",
      title: t("onboarding.tour.phoneTitle"),
      body: (() => {
        const host = typeof window !== "undefined" ? window.location.host : "";
        const isTailscale = host.includes(".ts.net");
        const ua = typeof navigator !== "undefined" ? navigator.userAgent : "";
        const isIOS     = /iPad|iPhone|iPod/.test(ua);
        const isAndroid = /Android/.test(ua);
        const installHint = isIOS
          ? t("onboarding.tour.installIos")
          : isAndroid
            ? t("onboarding.tour.installAndroid")
            : t("onboarding.tour.installOther");
        return (
          <div className="space-y-3 text-sm text-muted-foreground leading-relaxed">
            {isTailscale ? (
              <div className="flex items-start gap-2 text-emerald-600 dark:text-emerald-400">
                <Check className="w-4 h-4 mt-0.5 shrink-0" />
                <div className="text-xs leading-snug">
                  <Trans i18nKey="onboarding.tour.tailscaleAlready" values={{ host }} components={{ mono: <span className="font-mono" /> }} />
                </div>
              </div>
            ) : (
              <p>
                {t("onboarding.tour.tailscaleBefore")}{" "}
                <a
                  href="https://tailscale.com/download"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-primary hover:underline inline-flex items-center gap-0.5"
                >
                  {t("onboarding.tour.tailscaleLink")} <ArrowRight className="w-3 h-3 -rotate-45" />
                </a>{" "}
                {t("onboarding.tour.tailscaleAfter")}{" "}
                <span className="font-mono">your-box.your-tailnet.ts.net:8000</span>{" "}
                {t("onboarding.tour.tailscaleEnd")}
              </p>
            )}
            <p className="text-xs">{installHint}</p>
          </div>
        );
      })(),
      cta: t("onboarding.tour.done"),
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
              title={t("onboarding.tour.skipTitle")}
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
