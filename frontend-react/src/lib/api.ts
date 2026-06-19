/**
 * Thin fetch wrapper for talking to the Yorik FastAPI backend.
 * Always sends credentials so the yorik_session cookie travels with
 * every request (so React-side requests respect the same auth as the
 * legacy vanilla frontend).
 */

const API_BASE = ""; // same-origin in prod; Vite dev proxies /api/* to :8000

class ApiError extends Error {
  constructor(public status: number, message: string, public body?: unknown) {
    super(message);
    this.name = "ApiError";
  }
}

// YorikWall Android wrapper exposes the install's stable UUID via
// window.YorikNative.getDeviceId(). We forward it as a header on
// every API call so the backend can recognise the device across
// PWA cookie wipes (uninstall → reinstall, browser data clear,
// fresh login) and auto-apply trusted-kiosk policy. No-op when
// running in a regular browser (window.YorikNative is undefined).
export function wallDeviceHeader(): Record<string, string> {
  try {
    const n = (window as any).YorikNative;
    if (n && typeof n.getDeviceId === "function") {
      const id = String(n.getDeviceId() || "").trim();
      if (id) return { "x-yorik-wall-device": id };
    }
  } catch {}
  return {};
}

// Paths where a 401 response is EXPECTED state, not a session-expiry
// signal we should react to. /api/auth/me is the canonical "am I
// logged in?" probe — its 401 means "not yet"; reloading on it would
// crash the login screen into an infinite refresh loop.
const _EXPECTED_401_PATHS = new Set<string>([
  "/api/auth/me",
  "/api/auth/state",
  "/api/auth/login",
]);

// Cross-component 401 handler. AuthGate subscribes; when a 401
// arrives on a path that ISN'T an expected one (e.g. mid-session
// the cookie expired), the gate re-fetches /api/auth/me, sees
// `logged_in: false`, and flips back to the login screen. Without
// this, every tab keeps firing 401 toasts forever and the UI
// freezes — exactly the day-one "I opened it after a week and now
// nothing works" failure mode.
type SessionExpiredHandler = (path: string) => void;
let _sessionExpiredHandler: SessionExpiredHandler | null = null;
export function registerSessionExpiredHandler(fn: SessionExpiredHandler | null) {
  _sessionExpiredHandler = fn;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(API_BASE + path, {
    credentials: "include",
    ...init,
    headers: {
      ...(init.body && !(init.body instanceof FormData) ? { "content-type": "application/json" } : {}),
      ...wallDeviceHeader(),
      ...(init.headers || {}),
    },
  });
  const contentType = res.headers.get("content-type") || "";
  const body = contentType.includes("application/json")
    ? await res.json().catch(() => null)
    : await res.text().catch(() => null);
  if (!res.ok) {
    if (res.status === 401 && !_EXPECTED_401_PATHS.has(path) && _sessionExpiredHandler) {
      // Fire once per request; AuthGate de-dupes via its own
      // pending re-check. Don't block the throw — callers still
      // see the ApiError so per-tab error UI works.
      try { _sessionExpiredHandler(path); } catch { /* noop */ }
    }
    const msg = (typeof body === "object" && body && "detail" in body)
      ? String((body as { detail: unknown }).detail)
      : `HTTP ${res.status}`;
    throw new ApiError(res.status, msg, body);
  }
  return body as T;
}

export const api = {
  get:    <T>(path: string)              => request<T>(path),
  post:   <T>(path: string, data?: any)  => request<T>(path, { method: "POST", body: data && JSON.stringify(data) }),
  patch:  <T>(path: string, data?: any)  => request<T>(path, { method: "PATCH", body: data && JSON.stringify(data) }),
  delete: <T>(path: string)              => request<T>(path, { method: "DELETE" }),
};

export { ApiError };

// Auth shape mirroring backend/main.py's /api/auth/me.
export interface YorikUser {
  id: number;
  name: string;
  first_name?: string;
  last_name?: string;
  email: string;
  role: string;
  language?: string;
  country?: string | null;
  address_street?: string | null;
  address_postcode?: string | null;
  address_city?: string | null;
  phone?: string | null;
  business_name?: string | null;
  tax_id?: string | null;
  iban?: string | null;
  onboarded_at?: string | null;
  /** Scanned handwritten signature as a data URL. Empty string when unset. */
  signature_data_url?: string;
  /** ISO timestamp when the kiosk PIN was last set/changed. Null when
   *  no PIN has been configured yet — the Profile tab uses this to
   *  decide whether to show "Update PIN" vs "Set PIN". */
  pin_set_at?: string | null;
}
export interface AuthMe {
  logged_in: boolean;
  user?: YorikUser;
  setup_required?: boolean;
  is_tenant?: boolean;
}
