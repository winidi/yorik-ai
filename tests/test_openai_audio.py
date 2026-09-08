"""OpenAI-shaped /v1/audio endpoints: Yorik as the household speech server."""

from __future__ import annotations

import io
import wave

import pytest
from fastapi.testclient import TestClient

from tests.conftest import login_client


def _wav_bytes(seconds: float = 0.5) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x00" * int(16000 * seconds))
    return buf.getvalue()


@pytest.fixture
def token_and_client(fresh_app):
    logged_in, uid = login_client(fresh_app, role="member")
    token = logged_in.post("/api/tokens", json={"name": "dictate"}).json()["token"]
    return TestClient(fresh_app), token, logged_in


def test_endpoints_require_auth(fresh_app):
    c = TestClient(fresh_app)
    r = c.post("/v1/audio/transcriptions", files={"file": ("a.wav", _wav_bytes(), "audio/wav")})
    assert r.status_code == 401
    r = c.post("/v1/audio/speech", json={"input": "hallo"})
    assert r.status_code == 401


def test_transcriptions_formats(token_and_client, monkeypatch):
    client, token, _ = token_and_client
    from backend import voice
    monkeypatch.setattr(voice, "transcribe_detailed", lambda p: {"text": "Hallo Welt", "language": "de"})
    h = {"Authorization": f"Bearer {token}"}

    r = client.post("/v1/audio/transcriptions", headers=h,
                    files={"file": ("a.webm", _wav_bytes(), "audio/webm")},
                    data={"model": "whisper-1"})
    assert r.status_code == 200, r.text
    assert r.json() == {"text": "Hallo Welt", "language": "de"}

    r = client.post("/v1/audio/transcriptions", headers=h,
                    files={"file": ("a.wav", _wav_bytes(), "audio/wav")},
                    data={"response_format": "text"})
    assert r.text == "Hallo Welt"

    r = client.post("/v1/audio/transcriptions", headers=h,
                    files={"file": ("a.wav", _wav_bytes(), "audio/wav")},
                    data={"response_format": "verbose_json", "language": "en"})
    body = r.json()
    assert body["text"] == "Hallo Welt" and body["language"] == "en"

    r = client.post("/v1/audio/transcriptions", headers=h, files={"file": ("e.wav", b"", "audio/wav")})
    assert r.status_code == 400


def test_speech_wav_and_pcm(token_and_client, monkeypatch):
    client, token, logged_in = token_and_client
    from backend import tts
    calls = []

    def fake_synth(text, language="en", voice=None):
        calls.append((text, language, voice))
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(44100)
            wf.writeframes(b"\x00\x00" * 4410)  # 0.1 s
        return buf.getvalue()

    monkeypatch.setattr(tts, "synthesize", fake_synth)
    h = {"Authorization": f"Bearer {token}"}

    r = client.post("/v1/audio/speech", headers=h, json={"input": "Guten Morgen", "voice": "alloy", "language": "de"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("audio/wav")
    assert calls[-1] == ("Guten Morgen", "de", None)  # unknown voice → language default

    r = client.post("/v1/audio/speech", headers=h,
                    json={"input": "Hello", "voice": "f2", "response_format": "pcm", "language": "en"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("audio/pcm")
    # 0.1 s at 24 kHz, 16-bit mono
    assert abs(len(r.content) - 4800) < 200
    assert calls[-1] == ("Hello", "en", "F2")

    # a browser session works too
    r = logged_in.post("/v1/audio/speech", json={"input": "x", "response_format": "ogg"})
    assert r.status_code == 400
    r = logged_in.post("/v1/audio/speech", json={"input": "   "})
    assert r.status_code == 400
