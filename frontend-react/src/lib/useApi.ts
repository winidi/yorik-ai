/**
 * Minimal fetch hook. Returns { data, loading, error, refetch }.
 * Refetches on `deps` change (same semantics as useEffect's deps).
 * Not as full-featured as SWR/React Query but zero deps and good
 * enough for our scale; if the cache story gets complex later we
 * can swap in @tanstack/react-query without changing call sites much.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";

interface UseApiState<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
}

export function useApi<T>(
  path: string | null,
  deps: any[] = [],
  pollMs?: number,
) {
  const [state, setState] = useState<UseApiState<T>>({
    data: null, loading: !!path, error: null,
  });
  const aliveRef = useRef(true);
  // Per-call sequence number. Lets us drop in-flight responses whose
  // request started before the most recent one — otherwise a slow OLD
  // fetch can resolve AFTER a fast new fetch and overwrite fresh data
  // with stale data. Bit us in calendar: when the user quickly switched
  // weeks the month-window response landed after the week-window one.
  const requestIdRef = useRef(0);

  const fetcher = useCallback(async () => {
    if (!path) {
      setState({ data: null, loading: false, error: null });
      return;
    }
    const myId = ++requestIdRef.current;
    setState(s => ({ ...s, loading: true, error: null }));
    try {
      const data = await api.get<T>(path);
      if (aliveRef.current && requestIdRef.current === myId) {
        setState({ data, loading: false, error: null });
      }
    } catch (e: any) {
      if (aliveRef.current && requestIdRef.current === myId) {
        setState({ data: null, loading: false, error: e.message || "error" });
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, ...deps]);

  useEffect(() => {
    aliveRef.current = true;
    fetcher();
    return () => { aliveRef.current = false; };
  }, [fetcher]);

  useEffect(() => {
    if (!pollMs || !path) return;
    const t = setInterval(fetcher, pollMs);
    return () => clearInterval(t);
  }, [pollMs, path, fetcher]);

  return { ...state, refetch: fetcher };
}
