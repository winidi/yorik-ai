"""Unit tests for backend.security_throttle — login lockout + API rate
limit. These run against the module's in-process state directly; no
FastAPI app needed. The autouse fixture clears the module-level dicts
between tests so cases stay independent.

Pins the invariants the audit asked for:
  - Login: 5 fails / account / 15min → lockout
  - Login: 20 fails / IP / 15min → IP block (catches credential-stuffing)
  - Success clears the account counter (but not the IP counter)
  - API: per-IP, per-bucket sliding window with path-specific caps
  - Retry-After is a sane positive integer
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_throttle_state():
    """Module-level dicts mean state leaks between tests by default.
    Reload the module before each test so every case starts clean.
    Cheaper than monkeypatching every dict by hand."""
    import importlib
    import sys
    sys.modules.pop("backend.security_throttle", None)
    import backend.security_throttle as st  # noqa: F401
    importlib.reload(st)
    yield


# ─── login throttle ────────────────────────────────────────────────────

def test_login_first_attempts_are_allowed():
    """Sanity: pre-failure check_login_allowed returns True."""
    import backend.security_throttle as st
    ok, retry, reason = st.check_login_allowed("user@example.com", "10.0.0.1")
    assert ok is True
    assert retry is None
    assert reason is None


def test_login_account_lockout_after_five_failures(monkeypatch):
    """5 wrong passwords for one account → 6th attempt locked out."""
    import backend.security_throttle as st
    monkeypatch.setattr(st, "_LOGIN_ACCT_MAX_FAILS", 5)
    # Reset windows in case the env override changed mid-test
    st._login_acct_windows.clear()
    st._login_acct_locked.clear()

    email, ip = "victim@example.com", "10.0.0.1"
    for _ in range(5):
        ok, _, _ = st.check_login_allowed(email, ip)
        assert ok is True
        st.record_login_failure(email, ip)

    ok, retry, reason = st.check_login_allowed(email, ip)
    assert ok is False
    assert retry is not None and retry > 0
    assert "locked" in (reason or "").lower()


def test_login_ip_lockout_across_accounts():
    """Credential-stuffing: 20 fails across many accounts from one IP
    → IP gets blocked even though each account still has 4-or-fewer
    fails."""
    import backend.security_throttle as st
    ip = "10.0.0.99"
    # 20 different accounts, 1 failure each. Under the per-account cap
    # (5), so no account locks, but the per-IP total trips.
    for i in range(20):
        ok, _, _ = st.check_login_allowed(f"user{i}@example.com", ip)
        assert ok is True
        st.record_login_failure(f"user{i}@example.com", ip)

    # 21st attempt from this IP, against a fresh account, should fail.
    ok, retry, reason = st.check_login_allowed("fresh@example.com", ip)
    assert ok is False
    assert retry is not None and retry > 0
    assert "address" in (reason or "").lower() or "ip" in (reason or "").lower()


def test_login_success_clears_account_counter_not_ip():
    """A user who fat-fingered 3 times then logged in should NOT be one
    fail from lockout. But the IP counter sticks — the IP that just
    tried 19 wrong passwords against other accounts is still suspicious."""
    import backend.security_throttle as st
    email, ip = "fatfinger@example.com", "10.0.0.1"

    # 3 fails for this account
    for _ in range(3):
        st.record_login_failure(email, ip)
    assert len(st._login_acct_windows[email].times) == 3
    assert len(st._login_ip_windows[ip].times) == 3

    # success
    st.clear_login_failures(email)

    # account counter wiped
    assert email not in st._login_acct_windows
    # IP counter survives
    assert len(st._login_ip_windows[ip].times) == 3


def test_login_lockout_isolated_per_account():
    """One account locked must not lock out a different account from
    the same IP (otherwise one bad neighbour grief-blocks the whole
    household)."""
    import backend.security_throttle as st
    ip = "10.0.0.1"
    # Lock out account A by failing 5 times
    for _ in range(5):
        st.record_login_failure("a@example.com", ip)

    ok_a, _, _ = st.check_login_allowed("a@example.com", ip)
    ok_b, _, _ = st.check_login_allowed("b@example.com", ip)
    assert ok_a is False
    assert ok_b is True


# ─── API rate limit ────────────────────────────────────────────────────

def test_api_general_bucket_allows_then_blocks(monkeypatch):
    """Default 120/min bucket: 120 are allowed, 121st gets 429 with a
    sane Retry-After."""
    import backend.security_throttle as st
    monkeypatch.setattr(st, "_API_MAX_REQUESTS", 5)  # shrink for speed
    st._api_windows.clear()

    ip = "10.0.0.1"
    for i in range(5):
        ok, retry = st.check_api_allowed(ip, "/api/events")
        assert ok is True, f"attempt {i+1} should be allowed"
        assert retry is None

    ok, retry = st.check_api_allowed(ip, "/api/events")
    assert ok is False
    assert isinstance(retry, int) and retry >= 1


def test_api_per_path_buckets_are_independent():
    """/api/ask is a separate bucket from general /api/* — burning the
    LLM budget doesn't lock you out of /api/events and vice versa.
    Uses default caps (15 for /api/ask, 120 for general)."""
    import backend.security_throttle as st
    ip = "10.0.0.1"

    # Burn through the /api/ask bucket
    for _ in range(15):
        ok, _ = st.check_api_allowed(ip, "/api/ask")
        assert ok is True
    # 16th /api/ask → blocked
    ok, _ = st.check_api_allowed(ip, "/api/ask")
    assert ok is False

    # General bucket is still fine — independent counter
    ok, _ = st.check_api_allowed(ip, "/api/events")
    assert ok is True


def test_api_per_ip_buckets_are_independent():
    """One IP getting rate-limited doesn't affect another IP. Important
    for shared NATs (apartment building, school) — one noisy device
    shouldn't break the rest."""
    import backend.security_throttle as st

    # Burn one IP's budget on /api/ask (cap is 15)
    for _ in range(15):
        st.check_api_allowed("10.0.0.1", "/api/ask")
    ok_a, _ = st.check_api_allowed("10.0.0.1", "/api/ask")
    assert ok_a is False

    # Different IP, same path, fresh budget
    ok_b, _ = st.check_api_allowed("10.0.0.2", "/api/ask")
    assert ok_b is True


def test_api_retry_after_is_positive_int():
    """Bug class to pin: Retry-After must never be 0 or negative,
    even when the oldest entry expired one microsecond ago."""
    import backend.security_throttle as st
    ip = "10.0.0.1"
    for _ in range(15):
        st.check_api_allowed(ip, "/api/ask")
    for _ in range(5):
        ok, retry = st.check_api_allowed(ip, "/api/ask")
        assert ok is False
        assert isinstance(retry, int)
        assert retry >= 1
