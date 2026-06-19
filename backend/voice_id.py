"""Speaker identification via SpeechBrain ECAPA-TDNN.

Why ECAPA: real voice commands are 1–3 seconds. Resemblyzer is built for
≥3-second segments and its similarity scores get noisy below that. ECAPA
(VoxCeleb-trained) is the standard short-utterance speaker encoder and
emits 192-dim d-vectors that compare cleanly with cosine similarity.

Design choices:
- Lazy singleton: the encoder model is ~80 MB and 3-5s to import. We load it
  on first call so uvicorn --reload doesn't pay that cost.
- All failure modes (no model, no enrolled embeddings, audio too short,
  torch error) return None from `identify()`. The caller falls back to the
  role query param. Per the Graceful Fallback rule, the voice endpoint
  MUST keep working before anyone enrolls.
- Embeddings stored as a JSON list of floats in user_profiles.voice_embedding.
  192 floats × ~12 chars ≈ 2.4 KB per profile — fine in SQLite.
"""

from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from .database import DEFAULT_DB_PATH, conn_ctx

log = logging.getLogger("homeos.voice_id")

MODEL_DIR = os.getenv("HOMEOS_SPEAKER_MODEL_DIR", "data/speaker_model")
MATCH_THRESHOLD = float(os.getenv("HOMEOS_VOICE_MATCH_THRESHOLD", "0.65"))
MIN_ENROLL_SECONDS = float(os.getenv("HOMEOS_VOICE_ENROLL_MIN_SECONDS", "2"))
ECAPA_SOURCE = "speechbrain/spkrec-ecapa-voxceleb"
DB_PATH = os.getenv("HOMEOS_DB_PATH", DEFAULT_DB_PATH)

_encoder = None  # singleton instance of EncoderClassifier


def _get_encoder():
    """Lazy-load ECAPA. Re-raises so callers can decide how to handle."""
    global _encoder
    if _encoder is None:
        log.info("loading SpeechBrain ECAPA encoder from %s (first use)", MODEL_DIR)
        # Imports kept local — torchaudio/speechbrain import chain is heavy.
        from speechbrain.inference.speaker import EncoderClassifier
        Path(MODEL_DIR).mkdir(parents=True, exist_ok=True)
        _encoder = EncoderClassifier.from_hparams(
            source=ECAPA_SOURCE,
            savedir=MODEL_DIR,
            run_opts={"device": "cpu"},  # GPU is for the LLM
        )
    return _encoder


def _load_wav_16k_mono(path: str):
    """Decode any audio file (WebM/Opus, mp3, wav, …) into a 16 kHz mono torch tensor.

    Routed through ffmpeg because torchaudio ≥2.12 requires torchcodec and our
    browser inputs are WebM/Opus that the python-side decoders won't handle.
    ffmpeg is already a required system dependency (used by Whisper).
    """
    import subprocess
    import numpy as np
    import torch

    # ffmpeg → raw 16-bit PCM mono @ 16 kHz on stdout
    cmd = [
        "ffmpeg", "-loglevel", "error", "-nostdin",
        "-i", path,
        "-f", "s16le", "-ac", "1", "-ar", "16000", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, check=True)
    pcm = np.frombuffer(proc.stdout, dtype=np.int16)
    if pcm.size == 0:
        raise RuntimeError("ffmpeg produced empty audio")
    waveform = torch.from_numpy(pcm.astype(np.float32) / 32768.0).unsqueeze(0)
    return waveform, 16_000


def _audio_seconds(path: str) -> float:
    try:
        wav, sr = _load_wav_16k_mono(path)
        return float(wav.shape[-1]) / sr
    except Exception as exc:
        log.warning("voice_id: could not measure audio length (%s)", exc)
        return 0.0


def embed(wav_path: str) -> List[float]:
    """Compute the 192-dim ECAPA embedding for a single audio file."""
    waveform, _ = _load_wav_16k_mono(wav_path)
    enc = _get_encoder()
    embedding = enc.encode_batch(waveform).squeeze().tolist()
    if not isinstance(embedding, list):  # zero-length safety
        embedding = [float(embedding)]
    return [float(x) for x in embedding]


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
    skipping the ECAPA embedding step entirely, which is the main latency
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
      - SpeechBrain or torch raises anything (corrupt model, bad audio, …)
      - No profile has an enrolled embedding
      - Audio is shorter than ECAPA can usefully encode (<0.4s)
      - Best cosine similarity is below HOMEOS_VOICE_MATCH_THRESHOLD

    The caller falls back to the role query param.
    """
    if not _is_enabled():
        return None
    try:
        # Cheap check first: if nobody is enrolled there's no possible match,
        # so avoid the torch/ffmpeg cost of measuring + embedding the audio.
        profiles = _load_enrolled_profiles()
        if not profiles:
            log.info("voice_id: no enrolled profiles — falling back to role param")
            return None
        secs = _audio_seconds(wav_path)
        if secs < 0.4:
            log.warning("voice_id: audio too short (%.2fs) — skipping ID", secs)
            return None
        query_emb = embed(wav_path)
        scored = [
            {**p, "similarity": _cosine(query_emb, p["embedding"])}
            for p in profiles
        ]
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


def enroll(profile_id: int, wav_path: str) -> Dict[str, Any]:
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
        _get_encoder()
        log.info("voice_id: SpeechBrain ECAPA ready")
    except Exception as exc:  # noqa: BLE001
        log.warning("voice_id: warm-up failed: %s", exc)
