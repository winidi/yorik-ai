"""Speaker identification via sherpa-onnx (WeSpeaker CAM++), CPU only.

Why a speaker encoder at all: the kiosk and the voice FAB identify who is
talking so the request runs as that household member. Real voice commands
are 1–3 seconds; CAM++ trained on VoxCeleb holds up at that length and
emits 512-dim vectors that compare cleanly with cosine similarity.

Why sherpa-onnx: it replaced SpeechBrain ECAPA in September 2026 when the
backend went torch-free. Same job, a 29 MB ONNX file instead of a 4 GB
torch stack. Embeddings from the old encoder (192 floats) don't match the
new ones; enrolled profiles are ignored with a log line until the person
re-enrolls in Settings → Voice.

Design choices:
- Lazy singleton: the extractor loads on first use (~0.3 s).
- All failure modes (no model, no enrolled embeddings, audio too short,
  runtime error) return None from `identify()`. The caller falls back to
  the role query param. The voice endpoint MUST keep working before
  anyone enrolls.
- Embeddings stored as a JSON list of floats in user_profiles.voice_embedding.
"""

from __future__ import annotations

import json
import logging
import math
import os
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from .database import DEFAULT_DB_PATH, conn_ctx

log = logging.getLogger("homeos.voice_id")

MODEL_DIR = os.getenv("HOMEOS_SPEAKER_MODEL_DIR", "data/speaker_model")
MODEL_FILE = os.getenv("HOMEOS_SPEAKER_MODEL_FILE", "wespeaker_en_voxceleb_CAM++_LM.onnx")
MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
    + MODEL_FILE
)
# CAM++ cosine scores: same speaker typically 0.6–0.8, different speakers
# below 0.3 on clean audio. Tune per install via config.env.
MATCH_THRESHOLD = float(os.getenv("HOMEOS_VOICE_MATCH_THRESHOLD", "0.55"))
MIN_ENROLL_SECONDS = float(os.getenv("HOMEOS_VOICE_ENROLL_MIN_SECONDS", "2"))
DB_PATH = os.getenv("HOMEOS_DB_PATH", DEFAULT_DB_PATH)
SAMPLE_RATE = 16_000

_extractor = None
_lock = threading.Lock()


def model_path() -> Path:
    return Path(MODEL_DIR) / MODEL_FILE


def installed() -> bool:
    return model_path().exists()


def download() -> None:
    """Fetch the speaker model (~29 MB). Blocking."""
    import requests
    p = model_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".part")
    with requests.get(MODEL_URL, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(tmp, "wb") as fh:
            for chunk in r.iter_content(chunk_size=1 << 20):
                fh.write(chunk)
    tmp.replace(p)


def _get_extractor():
    """Lazy-load the extractor. Re-raises so callers can decide how to handle."""
    global _extractor
    if _extractor is not None:
        return _extractor
    with _lock:
        if _extractor is not None:
            return _extractor
        import sherpa_onnx
        if not installed():
            log.info("voice_id: speaker model missing — downloading %s", MODEL_FILE)
            download()
        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(model_path()),
            num_threads=max(1, min(2, os.cpu_count() or 1)),
            provider="cpu",
        )
        _extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
        log.info("voice_id: speaker encoder ready (%s, dim=%d)", MODEL_FILE, _extractor.dim)
        return _extractor


def _load_wav_16k_mono(path: str) -> np.ndarray:
    """Decode any audio file (WebM/Opus, mp3, wav, …) into float32 mono 16 kHz.

    Routed through ffmpeg because browser inputs are WebM/Opus that the
    python-side decoders won't handle. ffmpeg is a required system dependency.
    """
    cmd = [
        "ffmpeg", "-loglevel", "error", "-nostdin",
        "-i", path,
        "-f", "f32le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, check=True)
    audio = np.frombuffer(proc.stdout, dtype=np.float32)
    if audio.size == 0:
        raise RuntimeError("ffmpeg produced empty audio")
    return audio


def _audio_seconds(path: str) -> float:
    try:
        return float(_load_wav_16k_mono(path).size) / SAMPLE_RATE
    except Exception as exc:  # noqa: BLE001
        log.warning("voice_id: could not measure audio length (%s)", exc)
        return 0.0


def embed(wav_path: str) -> List[float]:
    """Compute the speaker embedding for a single audio file."""
    audio = _load_wav_16k_mono(wav_path)
    ext = _get_extractor()
    with _lock:
        stream = ext.create_stream()
        stream.accept_waveform(SAMPLE_RATE, audio)
        stream.input_finished()
        vec = ext.compute(stream)
    return [float(x) for x in vec]


def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb + 1e-9)


def _load_enrolled_profiles() -> List[Dict[str, Any]]:
    """All user_profiles rows that have an embedding."""
    with conn_ctx(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, name, role, language, voice_embedding "
            "FROM user_profiles WHERE voice_embedding IS NOT NULL AND voice_embedding != ''"
        ).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        try:
            emb = json.loads(r["voice_embedding"])
        except (json.JSONDecodeError, TypeError):
            log.warning("profile %s has corrupt embedding — skipping", r["id"])
            continue
        out.append({
            "id": r["id"],
            "name": r["name"],
            "role": r["role"],
            "language": r["language"] or "en",
            "embedding": emb,
        })
    return out


def _is_enabled() -> bool:
    """Check the admin-toggleable voice_id_enabled setting (default on).

    Stored in app_settings as "1"/"0". When off, identify() returns None
    immediately and the caller falls back to the role query param —
    skipping the embedding step entirely, which is the main latency
    cost on /api/ask-voice for installs with even one enrolled profile.
    """
    try:
        with conn_ctx(DB_PATH) as conn:
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key = 'voice_id_enabled'"
            ).fetchone()
        return (row is None) or (row["value"] != "0")
    except Exception:  # noqa: BLE001
        return True  # fail-open: never break voice flow on a settings hiccup


def identify(wav_path: str) -> Optional[Dict[str, Any]]:
    """Return the best-matching enrolled profile, or None on any failure path.

    The "None" outcomes (all silent, all logged):
      - The voice_id_enabled setting is off
      - The encoder raises anything (missing model, bad audio, …)
      - No profile has an enrolled embedding of the current encoder's size
      - Audio is shorter than the encoder can usefully encode (<0.4s)
      - Best cosine similarity is below HOMEOS_VOICE_MATCH_THRESHOLD

    The caller falls back to the role query param.
    """
    if not _is_enabled():
        return None
    try:
        # Cheap check first: if nobody is enrolled there's no possible match,
        # so avoid the ffmpeg + encoder cost of measuring + embedding the audio.
        profiles = _load_enrolled_profiles()
        if not profiles:
            log.info("voice_id: no enrolled profiles — falling back to role param")
            return None
        secs = _audio_seconds(wav_path)
        if secs < 0.4:
            log.warning("voice_id: audio too short (%.2fs) — skipping ID", secs)
            return None
        query_emb = embed(wav_path)
        stale = [p["name"] for p in profiles if len(p["embedding"]) != len(query_emb)]
        if stale:
            log.warning(
                "voice_id: %s enrolled with the previous encoder — re-enroll in Settings → Voice",
                ", ".join(stale),
            )
        scored = [
            {**p, "similarity": _cosine(query_emb, p["embedding"])}
            for p in profiles if len(p["embedding"]) == len(query_emb)
        ]
        if not scored:
            return None
        scored.sort(key=lambda p: p["similarity"], reverse=True)
        best = scored[0]
        # Always log the raw score so we can tune HOMEOS_VOICE_MATCH_THRESHOLD.
        runners_up = ", ".join(f"{p['name']}={p['similarity']:.3f}" for p in scored[1:3])
        log.info(
            "voice_id: best=%s sim=%.3f threshold=%.2f (others: %s)",
            best["name"], best["similarity"], MATCH_THRESHOLD, runners_up or "—",
        )
        if best["similarity"] < MATCH_THRESHOLD:
            log.info("voice_id: best match below threshold — falling back")
            return None
        return {
            "profile_id": best["id"],
            "name": best["name"],
            "role": best["role"],
            "language": best["language"],
            "similarity": round(best["similarity"], 4),
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("voice_id: identification failed (%s) — falling back to role param", exc)
        return None


def enroll(profile_id: Any, wav_path: str) -> Dict[str, Any]:
    """Compute embedding for `wav_path`, store on the profile. Returns the row."""
    secs = _audio_seconds(wav_path)
    if secs < MIN_ENROLL_SECONDS:
        raise ValueError(
            f"Please record at least {MIN_ENROLL_SECONDS:.0f} seconds of speech "
            f"(got {secs:.1f}s)."
        )
    vector = embed(wav_path)
    with conn_ctx(DB_PATH) as conn:
        existing = conn.execute("SELECT id FROM user_profiles WHERE id = ?", (profile_id,)).fetchone()
        if not existing:
            raise LookupError(f"user_profile id={profile_id} does not exist")
        conn.execute(
            "UPDATE user_profiles SET voice_embedding = ? WHERE id = ?",
            (json.dumps(vector), profile_id),
        )
        row = conn.execute("SELECT id, name, role, language FROM user_profiles WHERE id = ?", (profile_id,)).fetchone()
    return {**dict(row), "enrolled_seconds": round(secs, 2), "embedding_dim": len(vector)}


def warm_up() -> None:
    """Trigger the model download/load. Used by start.sh Phase 4."""
    try:
        _get_extractor()
        log.info("voice_id: speaker encoder ready")
    except Exception as exc:  # noqa: BLE001
        log.warning("voice_id: warm-up failed: %s", exc)
