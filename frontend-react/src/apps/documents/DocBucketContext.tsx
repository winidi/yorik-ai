/**
 * Document bucket — a per-session, in-memory selection of docs the
 * user wants to "chat about". The bucket survives in-app navigation
 * (sessionStorage) but is gone on tab close. Intentional v1 scope.
 *
 * The pill component (DocBucketPill) reads this to build the chat
 * seed; DocumentsApp writes to it via per-row star buttons.
 *
 * Why a Context instead of a global store: we have one piece of
 * state, two consumers, and zero need for time-travel debugging.
 * Adding zustand/redux/jotai would be over-engineering.
 */

import {
  createContext, useCallback, useContext, useEffect, useMemo, useState,
  type ReactNode,
} from "react";

const STORAGE_KEY = "yorik_doc_bucket";

interface DocBucketCtx {
  ids:    number[];               // ordered = order added
  has:    (id: number) => boolean;
  add:    (id: number) => void;
  remove: (id: number) => void;
  toggle: (id: number) => void;
  clear:  () => void;
}

const Ctx = createContext<DocBucketCtx | null>(null);

export function DocBucketProvider({ children }: { children: ReactNode }) {
  const [ids, setIds] = useState<number[]>(() => {
    try {
      const raw = sessionStorage.getItem(STORAGE_KEY);
      if (!raw) return [];
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed)
        ? parsed.filter((x: unknown) => typeof x === "number" && x > 0)
        : [];
    } catch { return []; }
  });

  // Persist on every change so the pill / a deep-link survives a
  // route change. Wrapped in try/catch — quota errors silently fall
  // back to in-memory-only (acceptable degradation).
  useEffect(() => {
    try {
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify(ids));
    } catch {}
  }, [ids]);

  const has    = useCallback((id: number) => ids.includes(id), [ids]);
  const add    = useCallback((id: number) =>
    setIds(prev => prev.includes(id) ? prev : [...prev, id]), []);
  const remove = useCallback((id: number) =>
    setIds(prev => prev.filter(x => x !== id)), []);
  const toggle = useCallback((id: number) =>
    setIds(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id]), []);
  const clear  = useCallback(() => setIds([]), []);

  const value = useMemo<DocBucketCtx>(
    () => ({ ids, has, add, remove, toggle, clear }),
    [ids, has, add, remove, toggle, clear],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useDocBucket(): DocBucketCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useDocBucket must be used inside DocBucketProvider");
  return ctx;
}
