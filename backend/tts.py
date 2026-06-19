"""Supertonic 3 text-to-speech with per-language voice switching.

Supertonic 3 is an on-device ONNX TTS supporting 31 languages with a single
model file (replaces our earlier Piper setup, which needed one voice file
per language). The model is downloaded on first use from HuggingFace to
HOMEOS_VOICES_DIR (~few hundred MB); subsequent runs are offline.

Public surface kept identical to the prior implementation so callers
(backend/main.py, start.sh warm-up) don't need to change:

- `synthesize(text, language) -> Optional[bytes]` — returns 16-bit PCM
  WAV bytes the caller serves directly via /api/tts-audio/{token}.
- `warm_up()` — preloads the model so the first request isn't slow.

Voice selection is by *voice style name* (M1..M5, F1..F5), not by file.
HOMEOS_TTS_VOICE / HOMEOS_TTS_VOICE_DE override the defaults below.
"""

from __future__ import annotations

import io
import logging
import os
import wave
from pathlib import Path
from typing import Optional

import numpy as np

log = logging.getLogger("homeos.tts")

VOICES_DIR = Path(os.getenv("HOMEOS_VOICES_DIR", "data/voices")) / "supertonic-3"
DEFAULT_LANGUAGE = os.getenv("HOMEOS_DEFAULT_LANGUAGE", "en")

# Voice style per language. Supertonic-3 ships 10 voices (M1..M5, F1..F5)
# and is fully multilingual — any voice works for any language; the
# per-language map only controls timbre.
#
# Defaults: HOMEOS_TTS_VOICE is the universal fallback. Per-language
# overrides use HOMEOS_TTS_VOICE_<LANG_UPPER>:
#   HOMEOS_TTS_VOICE_DE=F1   # German narrator → feminine
#   HOMEOS_TTS_VOICE_FR=M3   # French narrator → masculine warm
#   HOMEOS_TTS_VOICE_PL=F2
# Lookup at synthesis time so adding a new language in onboarding "just
# works" without redeploying.
DEFAULT_VOICE = os.getenv("HOMEOS_TTS_VOICE", "M1")

# Seed the cache from explicit env so warm_up()'s preload sees them.
VOICE_STYLE_BY_LANG: dict[str, str] = {"en": DEFAULT_VOICE}
for _envkey, _envval in os.environ.items():
    if _envkey.startswith("HOMEOS_TTS_VOICE_") and len(_envkey) > len("HOMEOS_TTS_VOICE_"):
        _lang = _envkey[len("HOMEOS_TTS_VOICE_"):].lower()
        if _envval:
            VOICE_STYLE_BY_LANG[_lang] = _envval


def _voice_for_lang(lang: str) -> str:
    """Pick voice style for `lang`: explicit env override > seeded cache >
    universal default. Cache the resolution so we don't re-getenv per call.
    """
    if lang in VOICE_STYLE_BY_LANG:
        return VOICE_STYLE_BY_LANG[lang]
    override = os.getenv(f"HOMEOS_TTS_VOICE_{lang.upper()}", "")
    voice = override or DEFAULT_VOICE
    VOICE_STYLE_BY_LANG[lang] = voice
    return voice

# Allow first-run download. Once cached in VOICES_DIR you can flip this off
# via HOMEOS_TTS_AUTO_DOWNLOAD=0 if you don't want any HF traffic on startup.
AUTO_DOWNLOAD = os.getenv("HOMEOS_TTS_AUTO_DOWNLOAD", "1") not in ("0", "false", "False")

_tts = None  # lazily-loaded supertonic.TTS singleton
_style_cache: dict = {}  # voice_style_name → Style


def _get_engine():
    """Lazy-load the Supertonic engine. Returns None if the package or model is missing."""
    global _tts
    if _tts is not None:
        return _tts
    try:
        from supertonic import TTS
    except ImportError as exc:
        log.warning("tts: supertonic not importable (%s) — synthesis disabled", exc)
        return None
    try:
        _tts = TTS(
            model="supertonic-3",
            model_dir=str(VOICES_DIR),
            auto_download=AUTO_DOWNLOAD,
        )
        log.info(
            "tts: supertonic-3 loaded (sr=%d, voices=%s)",
            _tts.sample_rate,
            _tts.voice_style_names,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("tts: failed to load supertonic-3 from %s: %s", VOICES_DIR, exc)
        return None
    return _tts


def _get_style(voice_name: str):
    """Cached voice-style lookup. Returns None on failure."""
    if voice_name in _style_cache:
        return _style_cache[voice_name]
    engine = _get_engine()
    if engine is None:
        return None
    try:
        style = engine.get_voice_style(voice_name)
    except Exception as exc:  # noqa: BLE001
        log.warning("tts: voice style %r not available: %s", voice_name, exc)
        return None
    _style_cache[voice_name] = style
    return style


def _wav_bytes(samples: np.ndarray, sample_rate: int) -> bytes:
    """Float32 [-1, 1] waveform → 16-bit PCM mono WAV bytes."""
    # Supertonic returns shape (1, N) — flatten to (N,) for the wave writer.
    audio = np.asarray(samples).reshape(-1)
    # Clip then scale to int16. Use 32767 to keep positive peaks intact.
    audio = np.clip(audio, -1.0, 1.0)
    pcm = (audio * 32767.0).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())
    return buf.getvalue()


def synthesize(text: str, language: str = "en") -> Optional[bytes]:
    """Return WAV bytes for `text` in the given language, or None on any failure."""
    if not text or not text.strip():
        return None
    engine = _get_engine()
    if engine is None:
        return None
    voice_name = _voice_for_lang(language or DEFAULT_LANGUAGE)
    style = _get_style(voice_name)
    if style is None:
        return None
    try:
        wav, _dur = engine.synthesize(text, voice_style=style, lang=language)
    except Exception as exc:  # noqa: BLE001
        log.warning("tts: synthesize failed (lang=%s, voice=%s): %s", language, voice_name, exc)
        return None
    return _wav_bytes(wav, engine.sample_rate)


def warm_up() -> None:
    """Preload the model + every configured voice style. Called by start.sh Phase 4."""
    engine = _get_engine()
    if engine is None:
        log.warning("tts: warm-up skipped — engine not loadable")
        return
    for lang, voice in VOICE_STYLE_BY_LANG.items():
        if _get_style(voice) is not None:
            log.info("tts: voice for '%s' ready (style=%s)", lang, voice)
        else:
            log.warning("tts: voice for '%s' NOT loaded (style=%s)", lang, voice)
