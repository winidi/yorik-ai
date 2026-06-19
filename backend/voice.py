"""Standalone voice CLI: record 5s, transcribe, POST to /api/ask.

Run:  python -m backend.voice
       (or)  python backend/voice.py

Also exposes `transcribe(path)` which the FastAPI `/api/ask-voice` endpoint uses
to handle browser-uploaded audio.

Transcription backend is selectable at runtime via HOMEOS_STT_BACKEND:
  whisper            — local Whisper, the default, no network needed
  groq               — Groq's OpenAI-compatible /v1/audio/transcriptions
  openai-compatible  — any URL speaking the same OpenAI shape
                       (OpenAI, ElevenLabs, self-hosted llama.cpp / CrispASR…)

Whisper is ALWAYS the fallback: if the configured cloud backend fails for any
reason that smells like a transient network/auth/rate-limit issue, the request
is retried locally so the user still gets a transcript.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger("homeos.voice")

# "tiny" gets us ~300-500ms STT for short voice commands (the typical case in
# Yorik), with adequate accuracy for English + German short phrases. Switch to
# "base" or "small" via HOMEOS_WHISPER_MODEL if dictation quality matters more
# than latency. Settings → Voice exposes this at runtime.
WHISPER_MODEL_NAME = os.getenv("HOMEOS_WHISPER_MODEL", "tiny")
API_URL = os.getenv("HOMEOS_API_URL", "http://localhost:8000/api/ask")
SAMPLE_RATE = 16_000
# Default for the terminal CLI. Override per-call: `python -m backend.voice 15`.
# Browser uses HOMEOS_VOICE_MAX_SECONDS (toggleable) instead.
DURATION_SECONDS = int(os.getenv("HOMEOS_VOICE_CLI_SECONDS", "15"))

# ─── STT backend selection ───────────────────────────────────────────
# Default is local Whisper — no network, no API key, user audio stays
# on-device. Settings → LLM → Speech-to-text exposes the engine
# picker; switches persist to config.env via _replace_or_append_env in
# main.py and are mirrored into these module globals via set_backend().
STT_BACKEND = (os.getenv("HOMEOS_STT_BACKEND") or "whisper").strip().lower()
STT_URL = (os.getenv("HOMEOS_STT_URL") or "").strip()
STT_API_KEY = os.getenv("HOMEOS_STT_API_KEY") or ""
STT_MODEL_NAME = (os.getenv("HOMEOS_STT_MODEL_NAME") or "").strip()

# Catalogue rendered by Settings UI. `default_url` and `default_model`
# get pre-filled when the user picks the engine so they only have to
# paste an API key for the common-case Groq flow.
STT_BACKEND_CATALOGUE = [
    {
        "id": "whisper",
        "label": "Local Whisper",
        "blurb": "Runs on this machine. No API key, no network. Audio never leaves the device.",
        "requires_key": False,
        "default_url": "",
        "default_model": "",
    },
    {
        "id": "groq",
        "label": "Groq Cloud",
        "blurb": "Whisper Large v3 Turbo on Groq's LPU hardware. ~10× faster than local, better German. Audio is sent to Groq.",
        "requires_key": True,
        "default_url": "https://api.groq.com/openai/v1/audio/transcriptions",
        "default_model": "whisper-large-v3-turbo",
    },
    {
        "id": "openai-compatible",
        "label": "OpenAI-compatible endpoint",
        "blurb": "Any provider speaking OpenAI's /v1/audio/transcriptions: OpenAI itself, ElevenLabs, a self-hosted llama.cpp / CrispASR server, etc.",
        "requires_key": True,
        "default_url": "https://api.openai.com/v1/audio/transcriptions",
        "default_model": "whisper-1",
    },
]

VALID_STT_BACKENDS = {b["id"] for b in STT_BACKEND_CATALOGUE}

# Long-form English names that Whisper sometimes returns when asked
# for verbose JSON, mapped back to ISO-639-1. Groq returns these
# instead of two-letter codes. Anything not in this table falls back
# to HOMEOS_DEFAULT_LANGUAGE so the downstream TTS-voice + LLM-reply
# language pickers don't break on an unknown code.
_LANG_LONGFORM_TO_ISO = {
    "english": "en",
    "german": "de",
    "french": "fr",
    "spanish": "es",
    "italian": "it",
    "polish": "pl",
    "portuguese": "pt",
    "dutch": "nl",
    "russian": "ru",
    "czech": "cs",
    "swedish": "sv",
    "danish": "da",
    "japanese": "ja",
    "korean": "ko",
    "chinese": "zh",
    "arabic": "ar",
    "turkish": "tr",
    "ukrainian": "uk",
}


# Whisper model catalogue. (name, label, size_mb, latency_ms_short, blurb)
# Latencies are CPU-only ballpark for a 5-second utterance — GPUs will
# be 5-10× faster. Sizes are the ONNX/PT download from openai/whisper.
WHISPER_CATALOGUE = [
    {"id": "tiny",     "label": "Tiny",      "size_mb": 75,    "ms_short": 500,    "blurb": "Fastest on CPU. Fine for short commands in EN/DE."},
    {"id": "base",     "label": "Base",      "size_mb": 145,   "ms_short": 1000,   "blurb": "Best speed/accuracy balance on CPU. Recommended."},
    {"id": "small",    "label": "Small",     "size_mb": 480,   "ms_short": 2500,   "blurb": "Notably more accurate on noisy audio. Slow on CPU."},
    {"id": "medium",   "label": "Medium",    "size_mb": 1500,  "ms_short": 6000,   "blurb": "Near-flagship quality. CPU pain. Use only with GPU."},
    {"id": "turbo",    "label": "Turbo",     "size_mb": 1600,  "ms_short": 4500,   "blurb": "GPU-optimized — slow on CPU. Use with GPU for medium quality at small-tier speed."},
    {"id": "large-v3", "label": "Large v3",  "size_mb": 3100,  "ms_short": 12000,  "blurb": "Best accuracy. CPU-prohibitive. GPU strongly recommended."},
]

VALID_WHISPER_MODELS = {m["id"] for m in WHISPER_CATALOGUE}


@lru_cache(maxsize=1)
def _model():
    import whisper  # heavy import — lazy
    return whisper.load_model(WHISPER_MODEL_NAME)


def set_model(name: str) -> None:
    """Swap the active Whisper model at runtime. Invalidates the cached
    model so the next transcribe() call re-loads with the new name.

    Note: switching to a not-yet-downloaded model triggers a ~secs to
    ~minutes download on the next voice request. Settings UI warns
    the user about this.
    """
    if name not in VALID_WHISPER_MODELS:
        raise ValueError(f"unknown Whisper model {name!r}; valid: {sorted(VALID_WHISPER_MODELS)}")
    global WHISPER_MODEL_NAME
    WHISPER_MODEL_NAME = name
    _model.cache_clear()
    os.environ["HOMEOS_WHISPER_MODEL"] = name


def set_backend(name: str, *, url: Optional[str] = None, api_key: Optional[str] = None,
                model_name: Optional[str] = None) -> None:
    """Swap the STT backend (and optionally URL/key/model) at runtime.

    Same shape as set_model(): updates module globals + os.environ so
    in-process callers see the change immediately, and is mirrored to
    config.env by the /api/voice/config PATCH handler in main.py.
    Whisper stays the silent fallback regardless of which backend is
    selected — see transcribe_detailed().
    """
    name = (name or "").strip().lower()
    if name not in VALID_STT_BACKENDS:
        raise ValueError(f"unknown STT backend {name!r}; valid: {sorted(VALID_STT_BACKENDS)}")
    global STT_BACKEND, STT_URL, STT_API_KEY, STT_MODEL_NAME
    STT_BACKEND = name
    os.environ["HOMEOS_STT_BACKEND"] = name
    if url is not None:
        STT_URL = url.strip()
        os.environ["HOMEOS_STT_URL"] = STT_URL
    if api_key is not None:
        # Trim defensively — pasted keys often carry a trailing
        # newline from clipboard managers, and Bearer auth headers
        # are strict about whitespace.
        STT_API_KEY = (api_key or "").strip()
        if STT_API_KEY:
            os.environ["HOMEOS_STT_API_KEY"] = STT_API_KEY
        else:
            os.environ.pop("HOMEOS_STT_API_KEY", None)
    if model_name is not None:
        STT_MODEL_NAME = model_name.strip()
        os.environ["HOMEOS_STT_MODEL_NAME"] = STT_MODEL_NAME


def transcribe(path: str) -> str:
    """STT for a file path. Returns the cleaned text."""
    return transcribe_detailed(path)["text"]


def transcribe_detailed(path: str) -> Dict[str, str]:
    """Returns {"text": str, "language": ISO-639-1 code}.
    Used by /api/ask-voice to drive TTS voice + LLM reply language.

    Dispatcher:
      - HOMEOS_STT_BACKEND=whisper → local Whisper (current behavior)
      - HOMEOS_STT_BACKEND=groq / openai-compatible → cloud POST,
        with automatic Whisper fallback on transient errors so a
        network blip never blocks a voice query.
    """
    if STT_BACKEND == "whisper":
        return _transcribe_local(path)
    try:
        return _transcribe_cloud(path)
    except Exception as exc:  # noqa: BLE001
        if _should_fall_back(exc):
            log.warning(
                "STT backend=%s failed (%s) — falling back to local Whisper for this request",
                STT_BACKEND, exc,
            )
            return _transcribe_local(path)
        raise


def _transcribe_local(path: str) -> Dict[str, str]:
    """The historical Whisper path. Always available as the safety net."""
    result = _model().transcribe(path, fp16=False)
    return {
        "text": (result.get("text") or "").strip(),
        "language": (result.get("language") or "en").lower(),
    }


def _transcribe_cloud(path: str) -> Dict[str, str]:
    """POST audio to an OpenAI-shape /v1/audio/transcriptions endpoint.

    Used by both 'groq' and 'openai-compatible' backends — they only
    differ in the configured URL + default model. response_format is
    verbose_json so we can read back the detected language.
    """
    import requests  # heavy-ish, kept lazy for fast cold imports

    url = STT_URL or _default_for(STT_BACKEND, "default_url")
    if not url:
        raise RuntimeError(
            f"STT backend={STT_BACKEND!r} but no URL configured (HOMEOS_STT_URL)"
        )
    if not STT_API_KEY:
        raise RuntimeError(
            f"STT backend={STT_BACKEND!r} but no API key configured (HOMEOS_STT_API_KEY)"
        )

    model = STT_MODEL_NAME or _default_for(STT_BACKEND, "default_model") or "whisper-1"
    headers = {"Authorization": f"Bearer {STT_API_KEY}"}

    with open(path, "rb") as fh:
        files = {"file": (Path(path).name, fh, "application/octet-stream")}
        data = {"model": model, "response_format": "verbose_json"}
        # 30s is generous for the typical 5-15s utterance even on a
        # cold connection — anything slower than that is a real
        # problem worth falling back from.
        resp = requests.post(url, headers=headers, files=files, data=data, timeout=30)

    if resp.status_code >= 400:
        # Surface the provider error in the log but raise a clean
        # exception so the dispatcher's fallback logic can react.
        snippet = (resp.text or "")[:200].replace("\n", " ")
        raise _CloudSTTError(resp.status_code, snippet)

    payload = resp.json()
    text = (payload.get("text") or "").strip()
    raw_lang = payload.get("language") or ""
    lang = _normalize_lang(raw_lang)
    return {"text": text, "language": lang}


class _CloudSTTError(RuntimeError):
    """Raised when the cloud endpoint returns 4xx/5xx. Carries the status
    code so _should_fall_back() can classify retryable vs not."""

    def __init__(self, status_code: int, snippet: str) -> None:
        super().__init__(f"{status_code}: {snippet}")
        self.status_code = status_code


def _should_fall_back(exc: BaseException) -> bool:
    """Decide whether to retry locally on Whisper. We fall back on
    anything that looks transient: network down, timeout, rate limit,
    auth failure (so a user with a wrong key still gets a transcript
    while they fix it), 5xx, and 'config missing' RuntimeErrors. We do
    NOT fall back on programmer errors (TypeError, AttributeError…)."""
    import requests
    if isinstance(exc, _CloudSTTError):
        # 401/403/429/5xx → fall back. 400 is a malformed-request
        # bug that local Whisper can't paper over but we still try
        # because the user's voice query is the priority.
        return True
    if isinstance(exc, (requests.exceptions.Timeout,
                        requests.exceptions.ConnectionError,
                        requests.exceptions.ChunkedEncodingError)):
        return True
    if isinstance(exc, RuntimeError):
        # set_backend was called with missing URL/key — treat as
        # config drift, salvage the request via Whisper, log loudly.
        return True
    return False


def _normalize_lang(value: str) -> str:
    """Map whatever the cloud returns ('en', 'english', 'EN') into the
    ISO-639-1 two-letter code Yorik's TTS + LLM picker expect."""
    if not value:
        return os.getenv("HOMEOS_DEFAULT_LANGUAGE", "en").lower()
    v = value.strip().lower()
    if len(v) == 2:
        return v
    return _LANG_LONGFORM_TO_ISO.get(v, os.getenv("HOMEOS_DEFAULT_LANGUAGE", "en").lower())


def _default_for(backend_id: str, field: str) -> str:
    for b in STT_BACKEND_CATALOGUE:
        if b["id"] == backend_id:
            return b.get(field, "")
    return ""


def record(duration: int = DURATION_SECONDS) -> str:
    import numpy as np
    import sounddevice as sd
    from scipy.io.wavfile import write as wav_write

    print(f"[voice] recording {duration}s @ {SAMPLE_RATE} Hz — speak now...", flush=True)
    audio = sd.rec(int(duration * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=1, dtype="float32")
    sd.wait()
    audio_int16 = np.int16(np.clip(audio, -1.0, 1.0) * 32767)
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    wav_write(tmp.name, SAMPLE_RATE, audio_int16)
    print(f"[voice] saved {tmp.name}", flush=True)
    return tmp.name


def ask_api(message: str, role: str = "admin") -> Dict[str, Any]:
    import requests
    r = requests.post(API_URL, json={"message": message, "role": role}, timeout=180)
    r.raise_for_status()
    return r.json()


def main() -> int:
    """CLI: `python -m backend.voice [seconds] [role]`. Default 15s as admin."""
    duration = DURATION_SECONDS
    role = "admin"
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        duration = int(sys.argv[1])
        if len(sys.argv) > 2:
            role = sys.argv[2]
    elif len(sys.argv) > 1:
        role = sys.argv[1]
    wav_path = record(duration)
    try:
        text = transcribe(wav_path)
        print(f"[voice] transcript: {text!r}")
        if not text:
            print("[voice] empty transcript — aborting")
            return 1
        response = ask_api(text, role=role)
        print("[voice] response:")
        import json
        print(json.dumps(response, indent=2, default=str))
    finally:
        try:
            Path(wav_path).unlink(missing_ok=True)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
