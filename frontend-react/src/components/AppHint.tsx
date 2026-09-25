/**
 * One short hint the first time a person opens an app, above the Dock.
 * "Got it" remembers it on the server for that person (hint_seen), so it
 * never repeats, on this device or another. Home has the tour instead.
 */
import { useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import { Lightbulb } from "lucide-react";
import { api } from "@/lib/api";
import { appIdFromPath } from "@/lib/dock-order";

const HINTS: Record<string, string> = {
  calendar:   "Tap a day or + to add an appointment. Everyone has their own colour.",
  tasks:      "Type a to-do at the top. Tick the box when it's done.",
  chat:       "Talk to Yorik like to a person: it adds dates, finds papers and writes letters.",
  docs:       "Drop a letter or bill here, or take a photo of one. Yorik reads and files it.",
  email:      "Your inbox. Yorik sorts it and suggests replies; nothing goes out without you.",
  photos:     "Your family's photos. How to back up this phone's pictures: tap ? below.",
  board:      "Everyone's day at a glance. Tap your own tasks to tick them.",
  contacts:   "Everyone Yorik knows. Yorik uses this to find addresses and numbers.",
  whatsapp:   "Your WhatsApp chats. Yorik can suggest replies; you decide what's sent.",
  compose:    "Letters, invoices and forms from templates. Or ask Yorik to write one.",
  write:      "Write a letter on your letterhead. Yorik helps with the wording.",
  briefing:   "Your day in one page: dates, to-dos and what came in.",
  finance:    "Your bank account, sorted by what the money went to. Read-only.",
  pipelines:  "Yorik keeps an eye on things for you, like waiting for an answer to a mail.",
  recordings: "Record a conversation; Yorik writes down what was said and agreed.",
};

let seenCache: Set<string> | null = null;

export function AppHint() {
  const loc = useLocation();
  const app = appIdFromPath(loc.pathname);
  const [seen, setSeen] = useState<Set<string> | null>(seenCache);

  useEffect(() => {
    if (seenCache) return;
    api.get<{ hints: string[] }>("/api/me/ui-state")
      .then(st => { seenCache = new Set(st.hints); setSeen(seenCache); })
      .catch(() => { seenCache = new Set(Object.keys(HINTS)); setSeen(seenCache); });
  }, []);

  if (!app || !seen || !HINTS[app] || seen.has(app)) return null;

  function dismiss() {
    const next = new Set(seen); next.add(app!);
    seenCache = next; setSeen(next);
    api.patch("/api/me/ui-state", { hint_seen: app }).catch(() => {});
  }

  return (
    <div className="fixed left-1/2 -translate-x-1/2 z-[55] w-[min(28rem,calc(100vw-2rem))] bottom-[calc(var(--dock-clearance)+0.75rem)]">
      <div className="flex items-start gap-3 p-3 rounded-2xl bg-card border border-border shadow-lg">
        <Lightbulb className="w-4 h-4 text-amber-500 mt-0.5 shrink-0" />
        <p className="text-sm flex-1">{HINTS[app]}</p>
        <button onClick={dismiss} className="text-sm font-medium text-primary shrink-0">Got it</button>
      </div>
    </div>
  );
}
