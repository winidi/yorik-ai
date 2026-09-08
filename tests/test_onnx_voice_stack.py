"""The torch-free voice stack: ONNX embedder + sherpa-onnx speaker id.

Real models are exercised when they are on disk (the workstation); the
wiring is pinned with fakes so CI without the downloads still covers it.
"""

from __future__ import annotations

import json
import os
import sys
import types
import wave
from pathlib import Path

import numpy as np
import pytest


def _wav(path: Path, seconds: float = 1.0, freq: float = 220.0) -> str:
    sr = 16000
    t = np.arange(int(sr * seconds)) / sr
    pcm = (np.sin(2 * np.pi * freq * t) * 12000).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())
    return str(path)


# ─── embedder ────────────────────────────────────────────────────────

def test_embedder_mean_pools_over_mask(monkeypatch, tmp_path):
    """Pooling math against a fake session: hidden states averaged only
    where the attention mask is 1, no normalisation."""
    from backend.embedders import local as L

    class _Enc:
        ids = [5, 6, 7]
        attention_mask = [1, 1, 1]

    class _Tok:
        def encode(self, text):
            return _Enc()

    class _Sess:
        def run(self, _, feeds):
            assert set(feeds) == {"input_ids", "attention_mask", "token_type_ids"}
            hidden = np.zeros((1, 3, 4), dtype=np.float32)
            hidden[0, 0] = [1, 1, 1, 1]
            hidden[0, 1] = [3, 3, 3, 3]
            hidden[0, 2] = [5, 5, 5, 5]
            return [hidden]

    monkeypatch.setattr(L, "_session", _Sess())
    monkeypatch.setattr(L, "_tokenizer", _Tok())
    monkeypatch.setattr(L, "_input_names", ["input_ids", "attention_mask", "token_type_ids"])
    assert L.embed("hallo") == [3.0, 3.0, 3.0, 3.0]
    assert L.dimension() == 384  # known-model lookup, no load


@pytest.mark.skipif(
    not Path(os.getenv("HOMEOS_EMBED_MODEL_DIR", "data/embed"),
             "paraphrase-multilingual-MiniLM-L12-v2", "onnx", "model.onnx").exists(),
    reason="embedder ONNX model not downloaded on this box",
)
def test_embedder_real_model_is_multilingual():
    pytest.importorskip("onnxruntime")
    from backend.embedders import local as L
    de = np.array(L.embed("Zahnarzttermin morgen um zehn"))
    en = np.array(L.embed("dentist appointment tomorrow at ten"))
    other = np.array(L.embed("Die Rechnung für den Stromanbieter ist fällig"))
    cos = lambda a, b: float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))
    assert len(de) == 384
    assert cos(de, en) > cos(de, other)


# ─── speaker id ──────────────────────────────────────────────────────

@pytest.fixture
def fake_speaker(monkeypatch, tmp_path):
    from backend import voice_id as V

    class _Stream:
        def __init__(self):
            self.n = 0

        def accept_waveform(self, sr, audio):
            self.n = len(audio)

        def input_finished(self):
            pass

    class _Ext:
        dim = 4

        def __init__(self, cfg):
            pass

        def create_stream(self):
            return _Stream()

        def compute(self, stream):
            # deterministic "voice print" from the clip length
            return [1.0, float(stream.n % 7), 0.5, 0.25]

    fake = types.ModuleType("sherpa_onnx")
    fake.SpeakerEmbeddingExtractorConfig = lambda **kw: kw
    fake.SpeakerEmbeddingExtractor = _Ext
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake)
    monkeypatch.setattr(V, "MODEL_DIR", str(tmp_path))
    (tmp_path / V.MODEL_FILE).write_bytes(b"x")
    monkeypatch.setattr(V, "_extractor", None)
    yield V
    monkeypatch.setattr(V, "_extractor", None)


def test_identify_ignores_embeddings_from_the_old_encoder(fake_speaker, monkeypatch, tmp_path):
    V = fake_speaker
    clip = _wav(tmp_path / "a.wav", 1.0)
    query = V.embed(clip)
    assert len(query) == 4
    profiles = [
        {"id": 1, "name": "old", "role": "admin", "language": "de", "embedding": [0.1] * 192},
        {"id": 2, "name": "new", "role": "member", "language": "de", "embedding": query},
    ]
    monkeypatch.setattr(V, "_load_enrolled_profiles", lambda: profiles)
    monkeypatch.setattr(V, "_is_enabled", lambda: True)
    hit = V.identify(clip)
    assert hit and hit["name"] == "new" and hit["similarity"] > 0.99

    monkeypatch.setattr(V, "_load_enrolled_profiles", lambda: profiles[:1])
    assert V.identify(clip) is None


def test_enroll_rejects_short_audio(fake_speaker, tmp_path):
    with pytest.raises(ValueError):
        fake_speaker.enroll(1, _wav(tmp_path / "short.wav", 0.5))


@pytest.mark.skipif(
    not Path(os.getenv("HOMEOS_SPEAKER_MODEL_DIR", "data/speaker_model"),
             "wespeaker_en_voxceleb_CAM++_LM.onnx").exists(),
    reason="speaker model not downloaded on this box",
)
def test_speaker_real_model_embeds(tmp_path):
    pytest.importorskip("sherpa_onnx")
    from backend import voice_id as V
    vec = V.embed(_wav(tmp_path / "tone.wav", 2.0))
    assert len(vec) == 512
    assert any(abs(x) > 0 for x in vec)
