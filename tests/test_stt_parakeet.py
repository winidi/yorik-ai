"""Parakeet STT wiring.

The engine itself is sherpa-onnx; here we pin the pieces around it:
variant selection, the Settings API, the dispatcher's fallback order,
and — when the model files are on disk — one real transcription.
"""

from __future__ import annotations

import os
import sys
import types
import wave
from pathlib import Path

import pytest

from tests.conftest import login_client


@pytest.fixture
def fake_sherpa(monkeypatch, tmp_path):
    """A stand-in sherpa_onnx module plus a fake installed model, so the
    wiring can be tested on a box without the 600 MB download."""
    from backend import stt_parakeet as pk

    calls: list[str] = []

    class _Stream:
        def __init__(self):
            self.result = types.SimpleNamespace(text="hallo welt")

        def accept_waveform(self, sr, audio):
            calls.append(f"accept:{len(audio)}")

    class _Rec:
        def create_stream(self):
            return _Stream()

        def decode_stream(self, s):
            calls.append("decode")

    class _Offline:
        @staticmethod
        def from_transducer(**kw):
            calls.append("load:" + Path(kw["encoder"]).parent.name)
            return _Rec()

    fake = types.ModuleType("sherpa_onnx")
    fake.OfflineRecognizer = _Offline
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake)
    monkeypatch.setenv("HOMEOS_STT_MODEL_DIR", str(tmp_path))
    for variant, meta in pk.VARIANTS.items():
        d = tmp_path / meta["subdir"]
        d.mkdir()
        for f in meta["files"]:
            (d / f).write_bytes(b"x")
    pk._drop_recognizer()
    yield calls
    pk._drop_recognizer()


def _silent_wav(path: Path, seconds: float = 0.5) -> str:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x00" * int(16000 * seconds))
    return str(path)


def test_variant_follows_household_language(monkeypatch):
    from backend import stt_parakeet as pk
    monkeypatch.delenv("HOMEOS_PARAKEET_MODEL", raising=False)
    monkeypatch.setenv("HOMEOS_DEFAULT_LANGUAGE", "de")
    assert pk.current_variant() == "de"
    monkeypatch.setenv("HOMEOS_DEFAULT_LANGUAGE", "en")
    assert pk.current_variant() == "multi"
    monkeypatch.setenv("HOMEOS_PARAKEET_MODEL", "de")
    assert pk.current_variant() == "de"
    with pytest.raises(ValueError):
        pk.set_variant("klingon")


def test_transcribe_through_fake_engine(fake_sherpa, tmp_path, monkeypatch):
    from backend import stt_parakeet as pk
    monkeypatch.setenv("HOMEOS_PARAKEET_MODEL", "de")
    pk._drop_recognizer()
    out = pk.transcribe_detailed(_silent_wav(tmp_path / "a.wav"))
    assert out == {"text": "hallo welt", "language": "de"}
    assert fake_sherpa[0] == "load:parakeet-primeline-onnx"
    # second call reuses the recognizer
    pk.transcribe_detailed(_silent_wav(tmp_path / "b.wav"))
    assert sum(1 for c in fake_sherpa if c.startswith("load:")) == 1
    # multilingual variant reports the household language
    monkeypatch.setenv("HOMEOS_DEFAULT_LANGUAGE", "en")
    pk.set_variant("multi")
    assert pk.transcribe_detailed(_silent_wav(tmp_path / "c.wav"))["language"] == "en"


def test_dispatcher_prefers_parakeet_and_falls_back(fake_sherpa, tmp_path, monkeypatch):
    from backend import voice, stt_parakeet as pk
    monkeypatch.setattr(voice, "STT_BACKEND", "parakeet")
    assert voice.transcribe(_silent_wav(tmp_path / "a.wav")) == "hallo welt"

    # Parakeet broken + Whisper importable → Whisper answers
    monkeypatch.setattr(pk, "transcribe_detailed", lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(voice, "_whisper_available", lambda: True)
    monkeypatch.setattr(voice, "_transcribe_whisper", lambda p: {"text": "from whisper", "language": "en"})
    assert voice.transcribe(_silent_wav(tmp_path / "b.wav")) == "from whisper"

    # Parakeet broken + no Whisper installed → the error surfaces
    monkeypatch.setattr(voice, "_whisper_available", lambda: False)
    with pytest.raises(RuntimeError):
        voice.transcribe(_silent_wav(tmp_path / "c.wav"))


def test_voice_config_exposes_and_switches_parakeet(fresh_app, fake_sherpa, monkeypatch):
    from backend import voice
    client, _ = login_client(fresh_app, role="admin")
    r = client.get("/api/voice/config")
    assert r.status_code == 200
    body = r.json()
    ids = {b["id"] for b in body["backends"]}
    assert "parakeet" in ids
    assert ("whisper" in ids) == bool(body["whisper_available"])
    assert {v["id"] for v in body["parakeet"]["variants"]} == {"de", "multi"}
    assert all(v["installed"] for v in body["parakeet"]["variants"])

    monkeypatch.setenv("HOMEOS_CONFIG_FILE", str(Path(os.environ["HOMEOS_STT_MODEL_DIR"]) / "config.env"))
    r = client.patch("/api/voice/config", json={"stt_backend": "parakeet", "parakeet_model": "multi"})
    assert r.status_code == 200, r.text
    assert voice.STT_BACKEND == "parakeet"
    assert r.json()["parakeet"]["model"] == "multi"
    cfg = Path(os.environ["HOMEOS_CONFIG_FILE"]).read_text()
    assert "HOMEOS_STT_BACKEND=parakeet" in cfg and "HOMEOS_PARAKEET_MODEL=multi" in cfg

    r = client.patch("/api/voice/config", json={"parakeet_model": "nope"})
    assert r.status_code == 400

    r = client.post("/api/voice/test-connection", json={"backend": "parakeet"})
    assert r.json()["ok"] is True

    # already installed → no download thread
    r = client.post("/api/voice/parakeet/download", json={"model": "de"})
    assert r.json() == {"ok": True, "installed": True, "download": r.json()["download"]}


@pytest.mark.skipif(
    not Path(os.getenv("HOMEOS_STT_MODEL_DIR", "data/stt"), "parakeet-primeline-onnx", "tokens.txt").exists(),
    reason="Parakeet model not downloaded on this box",
)
def test_real_model_transcribes_silence_to_empty(monkeypatch, tmp_path):
    pytest.importorskip("sherpa_onnx")
    from backend import stt_parakeet as pk
    monkeypatch.setenv("HOMEOS_PARAKEET_MODEL", "de")
    pk._drop_recognizer()
    out = pk.transcribe_detailed(_silent_wav(tmp_path / "s.wav", 1.0))
    assert out["language"] == "de"
    assert out["text"] == ""
    pk._drop_recognizer()
