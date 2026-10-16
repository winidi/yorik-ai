/**
 * "Get Yorik on this phone": the two things a phone needs after joining.
 *  1. Yorik on the home screen (a web app; iPhone and Android differ)
 *  2. Notifications, asked once, only after (1) on iPhone because
 *     Safari allows push only for home-screen apps (iOS 16.4+)
 * Used by the join flow's last step and the Home checklist.
 */
import { useEffect, useState } from "react";
import { BellRing, Check, Loader2, Share, SquarePlus, MoreVertical, Download } from "lucide-react";
import { useTranslation } from "react-i18next";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import i18n from "@/i18n";

type Platform = "ios" | "android" | "desktop";

export function detectPlatform(): Platform {
  const ua = navigator.userAgent || "";
  if (/iPhone|iPad|iPod/.test(ua) || (ua.includes("Macintosh") && "ontouchend" in document)) return "ios";
  if (/Android/.test(ua)) return "android";
  return "desktop";
}

export function isInstalled(): boolean {
  return window.matchMedia?.("(display-mode: standalone)").matches || (navigator as any).standalone === true;
}

function b64ToUint8(b64: string): Uint8Array {
  const pad = "=".repeat((4 - (b64.length % 4)) % 4);
  const raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, c => c.charCodeAt(0));
}

/** Ask for notifications and register this device. Returns a sentence
 *  for the person either way. */
export async function enablePush(): Promise<{ ok: boolean; text: string }> {
  if (!("PushManager" in window) || !("serviceWorker" in navigator)) {
    return { ok: false, text: detectPlatform() === "ios"
      ? i18n.t("settings.phoneSetup.iosNeedsHome")
      : i18n.t("settings.phoneSetup.noPush") };
  }
  try {
    const perm = await Notification.requestPermission();
    if (perm !== "granted") return { ok: false, text: i18n.t("settings.phoneSetup.notAllowed") };
    const reg = await navigator.serviceWorker.ready;
    const { key } = await api.get<{ key: string }>("/api/push/vapid-public-key");
    const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToUint8(key) });
    await api.post("/api/push/subscribe", { subscription: sub.toJSON() });
    return { ok: true, text: i18n.t("settings.phoneSetup.pushDone") };
  } catch (e: any) {
    return { ok: false, text: e?.message || i18n.t("settings.phoneSetup.pushFailed") };
  }
}

export function PhoneSetup({ onDone, doneLabel }: { onDone: () => void; doneLabel?: string }) {
  const { t } = useTranslation();
  const platform = detectPlatform();
  const [installed, setInstalled] = useState(isInstalled());
  const [prompt, setPrompt] = useState<any>(null);
  const [push, setPush] = useState<{ busy: boolean; ok?: boolean; text?: string }>({ busy: false });

  useEffect(() => {
    const onPrompt = (e: Event) => { e.preventDefault(); setPrompt(e); };
    const onInstalled = () => setInstalled(true);
    window.addEventListener("beforeinstallprompt", onPrompt);
    window.addEventListener("appinstalled", onInstalled);
    return () => {
      window.removeEventListener("beforeinstallprompt", onPrompt);
      window.removeEventListener("appinstalled", onInstalled);
    };
  }, []);

  async function installNow() {
    if (!prompt) return;
    prompt.prompt();
    const r = await prompt.userChoice.catch(() => null);
    if (r?.outcome === "accepted") setInstalled(true);
    setPrompt(null);
  }

  async function askPush() {
    setPush({ busy: true });
    const r = await enablePush();
    setPush({ busy: false, ...r });
  }

  return (
    <div className="space-y-5">
      <section className="space-y-3">
        <h3 className="font-semibold flex items-center gap-2">
          <span className={cn("w-6 h-6 rounded-full text-xs flex items-center justify-center",
                              installed ? "bg-emerald-500 text-white" : "bg-primary/15 text-primary")}>
            {installed ? <Check className="w-3.5 h-3.5" /> : "1"}
          </span>
          {t("settings.phoneSetup.homeScreen")}
        </h3>
        {installed ? (
          <p className="text-sm text-muted-foreground">{t("settings.phoneSetup.installed")}</p>
        ) : platform === "ios" ? (
          <ol className="text-sm space-y-2 pl-1">
            <li className="flex items-start gap-2"><Share className="w-4 h-4 mt-0.5 shrink-0 text-primary" /> {t("settings.phoneSetup.iosShareBefore")}<b>{t("settings.phoneSetup.iosShare")}</b>{t("settings.phoneSetup.iosShareAfter")}</li>
            <li className="flex items-start gap-2"><SquarePlus className="w-4 h-4 mt-0.5 shrink-0 text-primary" /> {t("settings.phoneSetup.iosAddBefore")}<b>{t("settings.phoneSetup.iosAddToHome")}</b>{t("settings.phoneSetup.iosAddMiddle")}<b>{t("settings.phoneSetup.iosAdd")}</b>.</li>
            <li className="text-muted-foreground">{t("settings.phoneSetup.iosOpen")}</li>
          </ol>
        ) : platform === "android" ? (
          prompt ? (
            <button onClick={installNow} className="w-full flex items-center justify-center gap-2 px-4 py-3 rounded-xl bg-primary text-primary-foreground font-medium">
              <Download className="w-4 h-4" /> {t("settings.phoneSetup.androidInstall")}
            </button>
          ) : (
            <ol className="text-sm space-y-2 pl-1">
              <li className="flex items-start gap-2"><MoreVertical className="w-4 h-4 mt-0.5 shrink-0 text-primary" /> {t("settings.phoneSetup.androidMenuBefore")}<b>⋮</b>{t("settings.phoneSetup.androidMenuAfter")}</li>
              <li className="flex items-start gap-2"><SquarePlus className="w-4 h-4 mt-0.5 shrink-0 text-primary" /> {t("settings.phoneSetup.androidAddBefore")}<b>{t("settings.phoneSetup.androidAddToHome")}</b>{t("settings.phoneSetup.androidAddOr")}<b>{t("settings.phoneSetup.androidInstallApp")}</b>.</li>
            </ol>
          )
        ) : (
          <p className="text-sm text-muted-foreground">{t("settings.phoneSetup.desktop")}</p>
        )}
      </section>

      <section className="space-y-3">
        <h3 className="font-semibold flex items-center gap-2">
          <span className={cn("w-6 h-6 rounded-full text-xs flex items-center justify-center",
                              push.ok ? "bg-emerald-500 text-white" : "bg-primary/15 text-primary")}>
            {push.ok ? <Check className="w-3.5 h-3.5" /> : "2"}
          </span>
          {t("settings.phoneSetup.remind")}
        </h3>
        {platform === "ios" && !installed ? (
          <p className="text-sm text-muted-foreground">{t("settings.phoneSetup.stepOneFirst")}</p>
        ) : (
          <>
            {!push.ok && (
              <button onClick={askPush} disabled={push.busy}
                      className="w-full flex items-center justify-center gap-2 px-4 py-3 rounded-xl border border-border bg-card font-medium disabled:opacity-60">
                {push.busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <BellRing className="w-4 h-4" />} {t("settings.phoneSetup.turnOnReminders")}
              </button>
            )}
            {push.text && <p className={cn("text-sm", push.ok ? "text-emerald-600 dark:text-emerald-400" : "text-muted-foreground")}>{push.text}</p>}
          </>
        )}
      </section>

      <button onClick={onDone} className="w-full px-4 py-3 rounded-xl bg-primary text-primary-foreground font-medium">
        {doneLabel ?? t("settings.phoneSetup.done")}
      </button>
    </div>
  );
}
