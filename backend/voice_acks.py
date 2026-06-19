"""Pre-synthesized instant-feedback phrases for voice replies.

Why: TTS of the real LLM answer takes 1-5s. The user already waited 2-3s
for Whisper. Without instant feedback the FAB looks frozen and people
re-tap, doubling the work. So as soon as STT finishes we play a short
"klar, moment" while the LLM cooks.

How: at startup we synthesize a few phrases per language into in-memory
WAV bytes. The /api/ask-voice/stream endpoint picks a random one and
emits an `ack` event with a one-shot URL. The client plays it before
the real TTS chunks arrive.

Disk cache: synthesized WAVs are persisted to ${HOMEOS_VOICES_DIR}/acks/
under filenames that include a hash of the phrase text. Every boot
loads cached files into the in-memory dict instead of re-synthesizing
(turning ~15-30s of cold start.sh time into ~200ms). Changing a
phrase string changes its filename hash, so stale cached files are
ignored and the new phrase is re-synthesized. Old hash files linger
on disk (small, harmless) — clean them by hand if it ever matters.

Adding a language: add an entry to PHRASES_BY_LANG. Warmup runs at
backend boot and is non-blocking — if TTS isn't available, we log and
move on (the feature gracefully degrades to no-ack).
"""
from __future__ import annotations

import hashlib
import logging
import os
import pathlib
import random
import threading
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("homeos.voice_acks")

_VOICES_DIR = pathlib.Path(os.getenv("HOMEOS_VOICES_DIR", "data/voices"))
_ACKS_DIR   = _VOICES_DIR / "acks"


def _ack_path(lang: str, idx: int, phrase: str) -> pathlib.Path:
    """On-disk cache path. The 8-char phrase hash means changing a
    PHRASES_BY_LANG entry invalidates only that one file instead of
    invisibly serving stale audio that mismatches the (lang, idx) key."""
    h = hashlib.sha1(phrase.encode("utf-8")).hexdigest()[:8]
    return _ACKS_DIR / f"{lang}-{idx}-{h}.wav"


# Per-language phrase pools. Keep short (under ~10 chars after TTS rendering)
# so they fit in <1s of audio — the goal is to mask latency, not narrate.
# Rotated randomly so the same phrase doesn't feel robotic across turns.
PHRASES_BY_LANG: Dict[str, List[str]] = {
    "en": [
        "Sure, one moment.",
        "On it.",
        "Got it, working on that.",
        "Right away.",
        "Looking into it.",
        "Just a sec.",
    ],
    "de": [
        "Klar, Moment.",
        "Mache ich.",
        "Einen Augenblick.",
        "Sofort.",
        "Bin dabei.",
        "Kleinen Moment bitte.",
    ],
    "fr": [
        "D'accord, un instant.",
        "Tout de suite.",
        "Je m'en occupe.",
        "Un moment.",
        "Bien sûr.",
    ],
    "es": [
        "Claro, un momento.",
        "En seguida.",
        "Lo estoy haciendo.",
        "Un segundo.",
    ],
    "it": [
        "Certo, un momento.",
        "Subito.",
        "Ci sto pensando.",
        "Un attimo.",
    ],
    "pl": [
        "Jasne, chwila.",
        "Robię to.",
        "Chwileczkę.",
        "Już to robię.",
    ],
}


# (lang, index) → wav bytes. Filled at startup by warmup().
_acks: Dict[Tuple[str, int], bytes] = {}
_acks_lock = threading.Lock()


def warmup() -> None:
    """Load cached ack WAVs from disk; synthesize any that aren't cached
    yet; persist the new ones back to disk so the next boot is free.
    Safe to call multiple times. Called from start.sh's Phase 4 voice
    warmup so the first real voice turn doesn't pay the cost.

    Prints a human-readable summary to stdout (cached vs newly
    synthesized) so start.sh's user sees progress instead of a silent
    15-30 second hang during first boot.
    """
    try:
        _ACKS_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("could not create ack cache dir %s: %s", _ACKS_DIR, exc)

    cached_count = 0
    synthesized_count = 0
    tts_mod = None  # imported lazily on first miss; saves cold-load time when everything is cached

    total = sum(len(p) for p in PHRASES_BY_LANG.values())
    print(f"[acks] warming up {total} phrases across {len(PHRASES_BY_LANG)} languages "
          f"(cached: {_ACKS_DIR})", flush=True)

    for lang, phrases in PHRASES_BY_LANG.items():
        for i, phrase in enumerate(phrases):
            key = (lang, i)
            with _acks_lock:
                if key in _acks:
                    continue
            path = _ack_path(lang, i, phrase)
            # Load from disk if cached.
            if path.exists():
                try:
                    wav = path.read_bytes()
                    if wav:
                        with _acks_lock:
                            _acks[key] = wav
                        cached_count += 1
                        log.info("ack loaded from cache: %s/%d (%d bytes)", lang, i, len(wav))
                        continue
                except OSError as exc:
                    log.warning("ack cache read failed for %s: %s — re-synthesizing", path, exc)
            # Cache miss → synthesize. Import tts lazily to avoid the
            # ~5-15s ONNX model load when every ack is already cached.
            if tts_mod is None:
                from . import tts as _tts_mod
                tts_mod = _tts_mod
                print(f"[acks] cold-loading TTS model for first synthesis (~5-15s)…", flush=True)
            try:
                wav = tts_mod.synthesize(phrase, lang)
            except Exception as exc:  # noqa: BLE001
                log.warning("ack warmup failed (lang=%s, '%s'): %s", lang, phrase, exc)
                continue
            if not wav:
                log.warning("ack warmup got empty wav: %s/%s", lang, phrase)
                continue
            # Persist to disk for next boot. Best-effort: if the FS rejects
            # the write the in-memory copy still works for this process.
            try:
                path.write_bytes(wav)
            except OSError as exc:
                log.warning("could not cache ack to %s: %s", path, exc)
            with _acks_lock:
                _acks[key] = wav
            synthesized_count += 1
            log.info("ack synthesized: %s/%d → %s (%d bytes)", lang, i, phrase, len(wav))

    print(f"[acks] done — {cached_count} loaded from cache, "
          f"{synthesized_count} newly synthesized this boot", flush=True)
    log.info("voice acks warmed up: %d phrases across %d languages "
             "(cached=%d, synthesized=%d)",
             len(_acks), len(PHRASES_BY_LANG), cached_count, synthesized_count)


def random_phrase(lang: str) -> Optional[Tuple[str, int, bytes]]:
    """Pick a random pre-synthesized ack for `lang`. Falls back to English
    if `lang` isn't in our pool. Returns (phrase_text, index, wav_bytes)
    or None if nothing is available (TTS not configured, etc.).
    """
    lang = (lang or "en").lower().strip()
    available = [(l, i) for (l, i) in _acks if l == lang]
    if not available and lang != "en":
        # Fall back to English — better an instant ack in the wrong language
        # than dead air.
        available = [(l, i) for (l, i) in _acks if l == "en"]
    if not available:
        return None
    l, idx = random.choice(available)
    phrase = PHRASES_BY_LANG[l][idx]
    with _acks_lock:
        wav = _acks.get((l, idx))
    return (phrase, idx, wav) if wav else None
