"""Parakeet speech-to-text on CPU via sherpa-onnx.

This is the engine Dictate (Dirk's desktop dictation app) uses, moved
into Yorik so the phone and tablet get the same quality without a GPU:
NVIDIA parakeet-tdt-0.6b as an int8 ONNX transducer, ~50 ms for two
seconds of audio on four CPU threads, no torch.

Two model variants, picked per household language:
  de     — parakeet-primeline, a German fine-tune (HuggingFace, pinned revision)
  multi  — the multilingual parakeet-tdt-0.6b-v3 export from the sherpa-onnx
           release page (25 European languages)

Models are downloaded on demand (Settings → Speech-to-text → Download, or
start.sh PHASE 4) into HOMEOS_STT_MODEL_DIR (default data/stt). Nothing is
fetched at import time; `installed()` is a pure filesystem check.

Audio comes in as whatever the browser or WhatsApp sent (webm/opus, ogg,
m4a, wav). ffmpeg, already a hard dependency, turns it into 16 kHz mono
float32 in memory.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tarfile
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np

log = logging.getLogger("homeos.stt.parakeet")

SAMPLE_RATE = 16_000
DEFAULT_THREADS = max(1, min(4, os.cpu_count() or 1))

VARIANTS: Dict[str, Dict[str, Any]] = {
    "de": {
        "id": "de",
        "label": "German (Parakeet Primeline)",
        "language": "de",
        "size_mb": 640,
        "blurb": "German fine-tune of NVIDIA Parakeet 0.6B. Best pick for German households.",
        "source": "hf",
        "repo": "flozen1981/parakeet-primeline-onnx",
        "revision": "d548e25b9bfe559aa274f361892dc4ed5d64743a",
        "subdir": "parakeet-primeline-onnx",
        "files": ["encoder.int8.onnx", "encoder.int8.onnx.data",
                  "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt"],
    },
    "multi": {
        "id": "multi",
        "label": "Multilingual (Parakeet v3)",
        "language": "",
        "size_mb": 600,
        "blurb": "NVIDIA Parakeet 0.6B v3, 25 European languages incl. English and German.",
        "source": "tarball",
        "url": "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
               "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8.tar.bz2",
        "subdir": "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8",
        "files": ["encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt"],
    },
}
VALID_VARIANTS = set(VARIANTS)


def model_root() -> Path:
    return Path(os.getenv("HOMEOS_STT_MODEL_DIR", "data/stt"))


def default_variant() -> str:
    lang = (os.getenv("HOMEOS_DEFAULT_LANGUAGE") or "en").lower()
    return "de" if lang.startswith("de") else "multi"


def current_variant() -> str:
    v = (os.getenv("HOMEOS_PARAKEET_MODEL") or "").strip().lower()
    return v if v in VALID_VARIANTS else default_variant()


def set_variant(variant: str) -> None:
    if variant not in VALID_VARIANTS:
        raise ValueError(f"unknown Parakeet model {variant!r}; valid: {sorted(VALID_VARIANTS)}")
    os.environ["HOMEOS_PARAKEET_MODEL"] = variant
    _drop_recognizer()


def model_dir(variant: str) -> Path:
    return model_root() / VARIANTS[variant]["subdir"]


def installed(variant: Optional[str] = None) -> bool:
    variant = variant or current_variant()
    d = model_dir(variant)
    return all((d / f).exists() for f in VARIANTS[variant]["files"])


def catalogue() -> list[Dict[str, Any]]:
    """Rows for the Settings picker."""
    return [
        {"id": v["id"], "label": v["label"], "size_mb": v["size_mb"],
         "blurb": v["blurb"], "installed": installed(v["id"])}
        for v in VARIANTS.values()
    ]


# ─── download ───────────────────────────────────────────────────────

_download_lock = threading.Lock()
_download_state: Dict[str, Any] = {"state": "idle", "variant": "", "message": "", "started": 0.0}


def download_status() -> Dict[str, Any]:
    return dict(_download_state)


def download(variant: str) -> None:
    """Blocking download of one variant. Raises on failure."""
    meta = VARIANTS[variant]
    dest = model_dir(variant)
    dest.mkdir(parents=True, exist_ok=True)
    if meta["source"] == "hf":
        from huggingface_hub import snapshot_download
        snapshot_download(
            repo_id=meta["repo"], revision=meta["revision"],
            local_dir=str(dest), allow_patterns=meta["files"],
        )
    else:
        _download_tarball(meta["url"], dest.parent, meta["subdir"])
    if not installed(variant):
        raise RuntimeError(f"download finished but files are missing in {dest}")


def _download_tarball(url: str, root: Path, subdir: str) -> None:
    import requests
    with tempfile.NamedTemporaryFile(suffix=".tar.bz2", delete=False, dir=str(root)) as tmp:
        tmp_path = Path(tmp.name)
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            for chunk in r.iter_content(chunk_size=1 << 20):
                tmp.write(chunk)
    try:
        with tarfile.open(tmp_path, "r:bz2") as tar:
            members = [m for m in tar.getmembers()
                       if m.name.startswith(subdir + "/") and not m.name.endswith("/")]
            for m in members:
                # Only plain files, only inside the expected directory.
                if not m.isfile() or ".." in Path(m.name).parts:
                    continue
                tar.extract(m, path=str(root))
    finally:
        tmp_path.unlink(missing_ok=True)


def start_download(variant: str) -> Dict[str, Any]:
    """Kick off a background download; returns the status dict. A second
    call while one runs is a no-op."""
    if variant not in VALID_VARIANTS:
        raise ValueError(f"unknown Parakeet model {variant!r}")
    with _download_lock:
        if _download_state["state"] == "running":
            return download_status()
        _download_state.update(state="running", variant=variant,
                               message=f"downloading ~{VARIANTS[variant]['size_mb']} MB", started=time.time())

    def _run() -> None:
        try:
            download(variant)
            _download_state.update(state="done", message="ready")
            log.info("parakeet model %s downloaded", variant)
        except Exception as exc:  # noqa: BLE001
            _download_state.update(state="error", message=f"{type(exc).__name__}: {exc}")
            log.warning("parakeet model %s download failed: %s", variant, exc)

    threading.Thread(target=_run, daemon=True, name=f"parakeet-download-{variant}").start()
    return download_status()


# ─── recognizer ─────────────────────────────────────────────────────

_recognizer: Any = None
_recognizer_variant: str = ""
_recognizer_lock = threading.Lock()
_decode_lock = threading.Lock()


def _drop_recognizer() -> None:
    global _recognizer, _recognizer_variant
    with _recognizer_lock:
        _recognizer = None
        _recognizer_variant = ""


def get_recognizer() -> Any:
    """Process-wide sherpa-onnx recognizer for the current variant.
    Loads on first use (~1 s) and warms up with a second of silence."""
    global _recognizer, _recognizer_variant
    variant = current_variant()
    with _recognizer_lock:
        if _recognizer is not None and _recognizer_variant == variant:
            return _recognizer
        if not installed(variant):
            raise FileNotFoundError(
                f"Parakeet model '{variant}' is not downloaded (expected in {model_dir(variant)})"
            )
        import sherpa_onnx
        d = model_dir(variant)
        rec = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=str(d / "encoder.int8.onnx"),
            decoder=str(d / "decoder.int8.onnx"),
            joiner=str(d / "joiner.int8.onnx"),
            tokens=str(d / "tokens.txt"),
            model_type="nemo_transducer",
            num_threads=DEFAULT_THREADS,
            decoding_method="greedy_search",
        )
        s = rec.create_stream()
        s.accept_waveform(SAMPLE_RATE, np.zeros(SAMPLE_RATE, dtype=np.float32))
        rec.decode_stream(s)
        _recognizer, _recognizer_variant = rec, variant
        log.info("parakeet '%s' loaded (%d threads)", variant, DEFAULT_THREADS)
        return rec


def warm_up() -> None:
    get_recognizer()


def _decode_audio(path: str) -> np.ndarray:
    """Any container → float32 mono 16 kHz via ffmpeg."""
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", path,
           "-f", "f32le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-"]
    out = subprocess.run(cmd, capture_output=True, check=False)
    if out.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {out.stderr.decode(errors='replace')[:200]}")
    return np.frombuffer(out.stdout, dtype=np.float32)


def transcribe_detailed(path: str) -> Dict[str, str]:
    """Same contract as voice.transcribe_detailed: {"text", "language"}.
    Parakeet does not detect the language; the German variant reports
    'de', the multilingual one the household default."""
    rec = get_recognizer()
    audio = _decode_audio(path)
    if audio.size == 0:
        return {"text": "", "language": _language()}
    with _decode_lock:
        s = rec.create_stream()
        s.accept_waveform(SAMPLE_RATE, audio)
        rec.decode_stream(s)
        text = (s.result.text or "").strip()
    return {"text": text, "language": _language()}


def _language() -> str:
    fixed = VARIANTS[current_variant()]["language"]
    return fixed or (os.getenv("HOMEOS_DEFAULT_LANGUAGE") or "en").lower()
