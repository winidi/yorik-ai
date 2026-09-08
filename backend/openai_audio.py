"""OpenAI-shaped audio endpoints — Yorik as the household's speech server.

    POST /v1/audio/transcriptions   multipart: file, [model], [language], [response_format]
    POST /v1/audio/speech           json: input, [voice], [language], [response_format]

Anything that already speaks to OpenAI, Groq or a llama.cpp server for
speech can point at Yorik instead: Dictate on the desktops, Hermes for
voice notes and spoken replies, a script. One engine (Parakeet on the
CPU, Supertonic for the voice) for every device in the house, and the
family laptops carry no model at all.

Auth is a personal API token (``Authorization: Bearer yk_…``) or a
browser session, the same way ``/api/*`` works. The ``model`` field is
accepted and ignored: Yorik runs whatever engine the admin configured.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field

log = logging.getLogger("yorik.openai_audio")

router = APIRouter(prefix="/v1/audio", tags=["openai-audio"])

_MAX_UPLOAD = 25 * 1024 * 1024  # OpenAI's own limit; plenty for voice notes
_MAX_SPEECH_CHARS = 4096


def _current_user():
    from .auth_sessions import current_user
    return current_user


@router.post("/transcriptions")
async def transcriptions(
    file: UploadFile = File(...),
    model: Optional[str] = Form(default=None),
    language: Optional[str] = Form(default=None),
    response_format: str = Form(default="json"),
    user: Dict[str, Any] = Depends(_current_user()),
):
    """Transcribe one audio file with the configured STT engine."""
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    if len(data) > _MAX_UPLOAD:
        raise HTTPException(status_code=413, detail="file larger than 25 MB")
    suffix = Path(file.filename or "audio.webm").suffix or ".webm"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    try:
        from .voice import transcribe_detailed
        detail = await asyncio.to_thread(transcribe_detailed, tmp_path)
    except Exception as exc:  # noqa: BLE001
        log.warning("transcription failed user=%s: %s", user.get("id"), exc)
        raise HTTPException(status_code=500, detail=f"transcription failed: {type(exc).__name__}: {exc}")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    text = (detail.get("text") or "").strip()
    lang = (language or detail.get("language") or "").lower()
    fmt = (response_format or "json").lower()
    if fmt == "text":
        return PlainTextResponse(text)
    if fmt == "verbose_json":
        return JSONResponse({"task": "transcribe", "language": lang, "duration": None,
                             "text": text, "segments": []})
    return JSONResponse({"text": text, "language": lang})


class SpeechRequest(BaseModel):
    input: str = Field(..., max_length=_MAX_SPEECH_CHARS)
    model: Optional[str] = None
    voice: Optional[str] = Field(default=None, description="Supertonic style M1..M5 / F1..F5; "
                                                          "OpenAI voice names map to the language default")
    language: Optional[str] = Field(default=None, description="ISO-639-1; defaults to the household language")
    response_format: str = Field(default="wav", description="wav | pcm (24 kHz s16le mono) | mp3 | opus | flac")


# What each format needs from ffmpeg. `pcm` follows OpenAI: 24 kHz, 16-bit,
# mono, headerless — the shape Hermes' streaming player expects.
_FORMATS = {
    "wav":  ("audio/wav",  None),
    "pcm":  ("audio/pcm",  ["-f", "s16le", "-ac", "1", "-ar", "24000"]),
    "mp3":  ("audio/mpeg", ["-f", "mp3", "-b:a", "96k"]),
    "opus": ("audio/ogg",  ["-f", "ogg", "-c:a", "libopus", "-b:a", "48k"]),
    "flac": ("audio/flac", ["-f", "flac"]),
}
_SUPERTONIC_VOICES = {"M1", "M2", "M3", "M4", "M5", "F1", "F2", "F3", "F4", "F5"}


def _transcode(wav: bytes, args: list[str]) -> bytes:
    import subprocess
    out = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", "pipe:0", *args, "pipe:1"],
                         input=wav, capture_output=True, check=False)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.decode(errors="replace")[:200])
    return out.stdout


@router.post("/speech")
async def speech(
    body: SpeechRequest,
    user: Dict[str, Any] = Depends(_current_user()),
):
    """Synthesize speech with Supertonic."""
    fmt = (body.response_format or "wav").lower()
    if fmt not in _FORMATS:
        raise HTTPException(status_code=400, detail=f"response_format must be one of {sorted(_FORMATS)}")
    text = (body.input or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty input")
    from . import tts as _tts
    language = (body.language or user.get("language") or os.getenv("HOMEOS_DEFAULT_LANGUAGE", "en")).lower()
    voice = (body.voice or "").strip().upper()
    if voice not in _SUPERTONIC_VOICES:
        voice = ""  # "alloy" and friends → the household's voice for that language
    wav = await asyncio.to_thread(_tts.synthesize, text, language, voice or None)
    if not wav:
        raise HTTPException(status_code=503, detail="text-to-speech is not available on this install")
    media_type, ff_args = _FORMATS[fmt]
    if ff_args:
        try:
            wav = await asyncio.to_thread(_transcode, wav, ff_args)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"transcode to {fmt} failed: {exc}")
    return Response(content=wav, media_type=media_type,
                    headers={"Content-Disposition": f'inline; filename="speech.{fmt}"'})
