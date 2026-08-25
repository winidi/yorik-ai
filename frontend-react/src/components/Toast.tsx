/**
 * Toast — the one non-blocking notice the apps use for "that failed" /
 * "done". Replaces window.alert(), which froze the page (and on the
 * kiosk tablet, stole focus from the voice flow).
 *
 *   toast("Saved");                       // neutral
 *   toast("Couldn't save", "error");      // red
 *   toast("Backup finished", "success");  // green
 *
 * Mount <Toaster /> once (main.tsx). No provider, no context: a module-
 * level listener list so any file can call toast() without wiring.
 */

import { useEffect, useState } from "react";
import { cn } from "@/lib/utils";

export type ToastKind = "info" | "error" | "success";
interface ToastItem { id: number; message: string; kind: ToastKind }

type Listener = (items: ToastItem[]) => void;
let _items: ToastItem[] = [];
let _seq = 0;
const _listeners = new Set<Listener>();

function _emit() { for (const l of _listeners) l(_items); }

export function toast(message: string, kind: ToastKind = "info"): void {
  const id = ++_seq;
  _items = [..._items, { id, message: String(message ?? ""), kind }].slice(-4);
  _emit();
  window.setTimeout(() => {
    _items = _items.filter(t => t.id !== id);
    _emit();
  }, kind === "error" ? 7000 : 3500);
}

export function Toaster() {
  const [items, setItems] = useState<ToastItem[]>(_items);
  useEffect(() => {
    _listeners.add(setItems);
    return () => { _listeners.delete(setItems); };
  }, []);
  if (items.length === 0) return null;
  return (
    <div
      aria-live="polite"
      className="fixed bottom-4 left-1/2 -translate-x-1/2 z-[100] flex flex-col gap-2 items-center pointer-events-none px-4 w-full max-w-md"
    >
      {items.map(t => (
        <div
          key={t.id}
          role={t.kind === "error" ? "alert" : "status"}
          className={cn(
            "pointer-events-auto w-full rounded-lg border px-3.5 py-2.5 text-sm shadow-lg backdrop-blur",
            "bg-card/95 text-foreground border-border",
            t.kind === "error" && "border-rose-500/40 text-rose-700 dark:text-rose-300",
            t.kind === "success" && "border-emerald-500/40",
          )}
          onClick={() => { _items = _items.filter(x => x.id !== t.id); _emit(); }}
        >
          {t.message}
        </div>
      ))}
    </div>
  );
}
