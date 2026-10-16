/**
 * One short hint the first time a person opens an app, above the Dock.
 * "Got it" remembers it on the server for that person (hint_seen), so it
 * never repeats, on this device or another. Home has the tour instead.
 */
import { useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import { Lightbulb } from "lucide-react";
import { useTranslation } from "react-i18next";
import { api } from "@/lib/api";
import { appIdFromPath } from "@/lib/dock-order";

// Apps that have a hint; the text lives in the catalogue as apps.hint.<id>.
const HINT_APPS = [
  "calendar", "tasks", "chat", "docs", "email", "photos", "board", "contacts",
  "whatsapp", "compose", "write", "briefing", "finance", "pipelines", "recordings",
];

let seenCache: Set<string> | null = null;

export function AppHint() {
  const { t } = useTranslation();
  const loc = useLocation();
  const app = appIdFromPath(loc.pathname);
  const [seen, setSeen] = useState<Set<string> | null>(seenCache);

  useEffect(() => {
    if (seenCache) return;
    api.get<{ hints: string[] }>("/api/me/ui-state")
      .then(st => { seenCache = new Set(st.hints); setSeen(seenCache); })
      .catch(() => { seenCache = new Set(HINT_APPS); setSeen(seenCache); });
  }, []);

  if (!app || !seen || !HINT_APPS.includes(app) || seen.has(app)) return null;

  function dismiss() {
    const next = new Set(seen); next.add(app!);
    seenCache = next; setSeen(next);
    api.patch("/api/me/ui-state", { hint_seen: app }).catch(() => {});
  }

  return (
    // In the chat the input sits right above the Dock; the tip goes above
    // it. Elsewhere it stays low and lies above the microphone button
    // (z 61 > 60), so "Got it" is never under it; lifting or narrowing
    // the tip made it cover the last row of a list instead.
    <div data-app-hint className={app === "chat"
      ? "fixed left-1/2 -translate-x-1/2 z-[61] w-[min(28rem,calc(100vw-2rem))] bottom-[calc(var(--dock-clearance)+5.5rem)]"
      : "fixed left-1/2 -translate-x-1/2 z-[61] w-[min(28rem,calc(100vw-2rem))] bottom-[calc(var(--dock-clearance)+0.75rem)]"}>
      <div className="flex items-start gap-3 p-3 rounded-2xl bg-card border border-border shadow-lg">
        <Lightbulb className="w-4 h-4 text-amber-500 mt-0.5 shrink-0" />
        <p className="text-sm flex-1">{t(`apps.hint.${app}`)}</p>
        <button onClick={dismiss} className="text-sm font-medium text-primary shrink-0">{t("apps.hint.gotIt")}</button>
      </div>
    </div>
  );
}
