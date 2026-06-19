"""In-memory throttles: login attempts + per-IP API rate limits.

Two separate but related concerns in one module — both use the same
sliding-window-counter primitive over an in-process dict, both reset on
restart (acceptable for a single-process self-hosted box; the cost of a
short reset window is much less than the cost of pulling in Redis).

If/when Yorik grows past a single FastAPI process, replace `_Window`
with a tiny Redis-backed equivalent — the call sites won't change.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Deque, Dict, Optional, Tuple

# ── Configuration knobs (env-overridable so deployments can tune) ────

import os

# Login throttle — per-account.
_LOGIN_ACCT_WINDOW_S       = int(os.getenv("YORIK_LOGIN_ACCT_WINDOW_S", "900"))   # 15 min
_LOGIN_ACCT_MAX_FAILS      = int(os.getenv("YORIK_LOGIN_ACCT_MAX_FAILS", "5"))
_LOGIN_ACCT_LOCKOUT_S      = int(os.getenv("YORIK_LOGIN_ACCT_LOCKOUT_S", "900"))  # 15 min

# Login throttle — per-IP (across all accounts; catches credential-stuffing).
_LOGIN_IP_WINDOW_S         = int(os.getenv("YORIK_LOGIN_IP_WINDOW_S", "900"))
_LOGIN_IP_MAX_FAILS        = int(os.getenv("YORIK_LOGIN_IP_MAX_FAILS", "20"))
_LOGIN_IP_LOCKOUT_S        = int(os.getenv("YORIK_LOGIN_IP_LOCKOUT_S", "1800"))   # 30 min

# General API rate limit — per-IP, sliding window.
# Default bumped 120 → 500 (2026-06-05) after the chat UI tripped 429s
# during normal active use. Multiple components poll independently
# (apps/notifications/dashboard-workers/system-status/email/demo-state)
# without sharing a cache, so passive polling alone runs ~80–100 calls/
# min — leaving almost no budget for actual user activity (chat, photo
# search, calendar nav) inside one window. 500/min = ~8/sec/IP, which
# is generous for a single user and still blocks any abuse pattern by
# wide margins. A NAT'd household of 3 users sees ~24/sec effective,
# still well under the cap.
_API_WINDOW_S              = int(os.getenv("YORIK_API_WINDOW_S", "60"))
_API_MAX_REQUESTS          = int(os.getenv("YORIK_API_MAX_REQUESTS", "500"))

# Per-path overrides for expensive / sensitive routes. Match by PREFIX.
# Order matters — more specific prefixes first.
_API_PATH_LIMITS: list[tuple[str, int]] = [
    ("/api/ask",          int(os.getenv("YORIK_API_ASK_MAX",         "15"))),  # LLM is expensive
    ("/api/ask-voice",    int(os.getenv("YORIK_API_ASK_VOICE_MAX",   "15"))),
    ("/api/auth/login",   int(os.getenv("YORIK_API_LOGIN_MAX",       "10"))),  # tighter; the login-guard adds account-level throttle on top
    ("/api/auth/setup",   int(os.getenv("YORIK_API_SETUP_MAX",       "5"))),
    ("/api/documents/upload", int(os.getenv("YORIK_API_UPLOAD_MAX",   "30"))),
]

_lock = threading.Lock()


class _Window:
    """Sliding-window timestamp deque. Cheap to inspect (O(n) drop-old
    on every check, but n is bounded by the rate cap so it stays tiny)."""

    __slots__ = ("times",)

    def __init__(self) -> None:
        self.times: Deque[float] = deque()

    def hit(self, now: float, window_s: int) -> int:
        """Record a hit and return the count within the window."""
        self.times.append(now)
        cutoff = now - window_s
        while self.times and self.times[0] < cutoff:
            self.times.popleft()
        return len(self.times)

    def count(self, now: float, window_s: int) -> int:
        cutoff = now - window_s
        while self.times and self.times[0] < cutoff:
            self.times.popleft()
        return len(self.times)


# ── Login throttle state ─────────────────────────────────────────────

_login_acct_windows: Dict[str, _Window] = {}
_login_ip_windows:   Dict[str, _Window] = {}
_login_acct_locked:  Dict[str, float]   = {}  # email → unlock_ts
_login_ip_locked:    Dict[str, float]   = {}  # ip    → unlock_ts


def check_login_allowed(email: str, ip: str) -> Tuple[bool, Optional[int], Optional[str]]:
    """Returns (allowed, retry_after_seconds, reason).

    Call BEFORE attempting to verify the password. If allowed=False,
    reject the login attempt with a 429 and use retry_after_seconds in
    the Retry-After header. Tracks no state when allowed."""
    now = time.time()
    em = (email or "").strip().lower()
    with _lock:
        unlock = _login_acct_locked.get(em)
        if unlock and unlock > now:
            return False, max(1, int(unlock - now)), "account temporarily locked"
        unlock = _login_ip_locked.get(ip)
        if unlock and unlock > now:
            return False, max(1, int(unlock - now)), "too many login attempts from this address"
    return True, None, None


def record_login_failure(email: str, ip: str) -> None:
    """Call AFTER a failed login. Flips the lockout if the threshold is
    hit. Successful logins should call `clear_login_failures(email)`."""
    now = time.time()
    em = (email or "").strip().lower()
    with _lock:
        acct_win = _login_acct_windows.setdefault(em, _Window())
        ip_win   = _login_ip_windows.setdefault(ip, _Window())
        acct_fails = acct_win.hit(now, _LOGIN_ACCT_WINDOW_S)
        ip_fails   = ip_win.hit(now, _LOGIN_IP_WINDOW_S)
        if acct_fails >= _LOGIN_ACCT_MAX_FAILS:
            _login_acct_locked[em] = now + _LOGIN_ACCT_LOCKOUT_S
        if ip_fails >= _LOGIN_IP_MAX_FAILS:
            _login_ip_locked[ip] = now + _LOGIN_IP_LOCKOUT_S


def clear_login_failures(email: str) -> None:
    """Reset the account-side counter + lockout after a successful login.
    IP-side counter intentionally stays — one good login doesn't undo
    that the IP just tried 19 wrong passwords."""
    em = (email or "").strip().lower()
    with _lock:
        _login_acct_windows.pop(em, None)
        _login_acct_locked.pop(em, None)


# ── General API rate limit state ─────────────────────────────────────

_api_windows: Dict[Tuple[str, str], _Window] = {}  # (ip, bucket) → window


def _bucket_for(path: str) -> Tuple[str, int]:
    """Returns (bucket_key, max_in_window) for a given path. Bucket key
    isolates per-path counters so a flurry of /api/ask doesn't burn the
    user's general-API budget."""
    for prefix, cap in _API_PATH_LIMITS:
        if path.startswith(prefix):
            return (prefix, cap)
    return ("__general__", _API_MAX_REQUESTS)


def check_api_allowed(ip: str, path: str) -> Tuple[bool, Optional[int]]:
    """Returns (allowed, retry_after_seconds). Records the hit if allowed.
    Skip via the middleware's allowlist for /api/health, OPTIONS preflight,
    static assets, etc. — this is hot-path code."""
    bucket, cap = _bucket_for(path)
    now = time.time()
    key = (ip, bucket)
    with _lock:
        win = _api_windows.setdefault(key, _Window())
        count = win.count(now, _API_WINDOW_S)
        if count >= cap:
            # Compute retry-after from the oldest entry: it'll expire at
            # times[0] + window. Clamped to >=1 so 429s never say "0s".
            retry = max(1, int(win.times[0] + _API_WINDOW_S - now)) if win.times else 1
            return False, retry
        win.hit(now, _API_WINDOW_S)
    return True, None


def snapshot() -> Dict[str, int]:
    """Cheap stats for debugging / a hypothetical admin view. Not
    routed today — call from a REPL when investigating."""
    with _lock:
        return {
            "api_buckets":         len(_api_windows),
            "login_acct_tracked":  len(_login_acct_windows),
            "login_ip_tracked":    len(_login_ip_windows),
            "login_acct_locked":   sum(1 for ts in _login_acct_locked.values() if ts > time.time()),
            "login_ip_locked":     sum(1 for ts in _login_ip_locked.values() if ts > time.time()),
        }
