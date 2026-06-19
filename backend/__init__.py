"""HomeOS backend package.

We load `config.env` at import time so every module that reads
`os.getenv("HOMEOS_*")` sees the configured values without having to
chase down dotenv loading itself.
"""

import os
from pathlib import Path

try:
    from dotenv import dotenv_values, load_dotenv
except ImportError:  # pragma: no cover — dotenv is in requirements.txt
    load_dotenv = None  # type: ignore[assignment]
    dotenv_values = None  # type: ignore[assignment]

_CONFIG_ENV = Path(__file__).resolve().parent.parent / "config.env"

# Keys the running app can PATCH from the Settings UI. For these,
# config.env is the persistent source of truth — and on an uvicorn
# `--reload` respawn the worker inherits the watchdog parent's stale
# env (from start.sh time), which silently rolls back the user's
# saved LLM URL / model / whisper choice. Without forcing config.env
# to override here, the next code change touches a Python file and
# Yorik suddenly forgets where the LLM lives. See the PATCH
# endpoints in backend/main.py for the writer side.
_RUNTIME_MUTABLE_KEYS = {
    "HOMEOS_LLM_BASE_URL",
    "HOMEOS_MODEL",
    "HOMEOS_WHISPER_MODEL",
}

if load_dotenv is not None and _CONFIG_ENV.exists():
    # Pass 1: non-overriding for everything else (shell env wins —
    # tests can still inject HOMEOS_DB_PATH=/tmp/test.db etc.).
    load_dotenv(_CONFIG_ENV, override=False)
    # Pass 2: force-override the UI-mutable keys so a runtime PATCH
    # to config.env survives the next worker respawn.
    if dotenv_values is not None:
        for _k, _v in dotenv_values(_CONFIG_ENV).items():
            if _k in _RUNTIME_MUTABLE_KEYS and _v is not None:
                os.environ[_k] = _v
