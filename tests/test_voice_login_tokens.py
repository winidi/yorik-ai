"""The voice-login token: per-install secret, bound to the browser
session that asked (audit docs/audits/2026-09-22-berechtigungen.md, 4.1)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest

from backend import voice_login_tokens as T


@pytest.fixture
def store(monkeypatch):
    """An in-memory credential store and a fresh secret cache."""
    from backend import credential_store as CS
    box: dict = {}
    monkeypatch.setattr(CS, "get", lambda name: box.get(name))
    monkeypatch.setattr(CS, "put", lambda name, creds: box.__setitem__(name, dict(creds)))
    monkeypatch.delenv("HOMEOS_VOICE_LOGIN_SECRET", raising=False)
    monkeypatch.setattr(T, "_cached_secret", None)
    T._consumed.clear()
    return box


def test_token_is_bound_to_the_session_that_asked(store):
    tok = T.mint(profile_id=7, device_uuid="wall-1", source_sid="sid-A")
    assert T.verify(tok, expected_profile_id=7, expected_device_uuid="wall-1", expected_source_sid="sid-B") is None
    assert T.verify(tok, expected_profile_id=7, expected_device_uuid="wall-1", expected_source_sid="") is None
    assert T.verify(tok, expected_profile_id=8, expected_device_uuid="wall-1", expected_source_sid="sid-A") is None
    assert T.verify(tok, expected_profile_id=7, expected_device_uuid="wall-2", expected_source_sid="sid-A") is None
    assert T.verify(tok, expected_profile_id=7, expected_device_uuid="wall-1", expected_source_sid="sid-A")["p"] == 7
    assert T.verify(tok, expected_profile_id=7, expected_device_uuid="wall-1", expected_source_sid="sid-A") is None   # single use


def test_the_secret_is_per_install_and_kept(store, monkeypatch):
    first = T._secret()
    assert len(first) == 32 and first != hashlib.sha256(b"yorik-voice-login-default-v1").digest()
    assert base64.urlsafe_b64decode(store["voice_login"]["secret"]) == first
    monkeypatch.setattr(T, "_cached_secret", None)                 # a restart reads it back
    assert T._secret() == first
    # a token signed with the old constant is worthless now
    body = json.dumps({"p": 7, "d": "wall-1", "s": "sid-A", "t": 4102444800, "n": "x"}, separators=(",", ":"), sort_keys=True).encode()
    sig = hmac.new(hashlib.sha256(b"yorik-voice-login-default-v1").digest(), body, hashlib.sha256).digest()
    forged = f"{T._b64encode(body)}.{T._b64encode(sig)}"
    assert T.verify(forged, expected_profile_id=7, expected_device_uuid="wall-1", expected_source_sid="sid-A") is None
