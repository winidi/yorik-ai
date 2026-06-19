"""Country → locale mapping used by the onboarding wizard.

When a user picks their country in Step 1 of onboarding, we derive
sensible defaults for the server's timezone and Paperless's OCR
language packs, write them to config.env, and (best-effort) restart
the running Paperless container so the OCR change takes effect.

Per-user reply language is a SEPARATE concern (stored in
user_profiles.language) — this module is about server-wide locale.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Dict, Optional

log = logging.getLogger("homeos.locale")

# country code → (timezone, OCR language pack(s), per-user reply language)
# OCR packs are Tesseract language codes; always include "eng" as a fallback
# so foreign-language documents in the same library still OCR.
# US has many timezones — default to America/New_York; user can override.
COUNTRY_LOCALE: Dict[str, Dict[str, str]] = {
    "DE": {"tz": "Europe/Berlin", "ocr": "deu+eng", "language": "de"},
    "AT": {"tz": "Europe/Vienna", "ocr": "deu+eng", "language": "de"},
    "CH": {"tz": "Europe/Zurich", "ocr": "deu+fra+ita+eng", "language": "de"},
    "US": {"tz": "America/New_York", "ocr": "eng", "language": "en"},
    "GB": {"tz": "Europe/London", "ocr": "eng", "language": "en"},
    "PL": {"tz": "Europe/Warsaw", "ocr": "pol+eng", "language": "pl"},
    "FR": {"tz": "Europe/Paris", "ocr": "fra+eng", "language": "fr"},
    "ES": {"tz": "Europe/Madrid", "ocr": "spa+eng", "language": "es"},
    "IT": {"tz": "Europe/Rome", "ocr": "ita+eng", "language": "it"},
}


def _config_path() -> Path:
    return Path(os.getenv("HOMEOS_CONFIG_FILE", "config.env"))


def _replace_or_append_env(text: str, key: str, value: str) -> str:
    """Same logic as start.sh's _set_env helper, in Python."""
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    line = f"{key}={value}"
    if pattern.search(text):
        return pattern.sub(line, text)
    if text and not text.endswith("\n"):
        text += "\n"
    return text + line + "\n"


def _write_env_vars(updates: Dict[str, str]) -> None:
    path = _config_path()
    text = path.read_text() if path.exists() else ""
    for k, v in updates.items():
        text = _replace_or_append_env(text, k, v)
    path.write_text(text)


def _paperless_restart_if_bundled() -> Optional[str]:
    """Restart the bundled Paperless container if it's running, so the
    new PAPERLESS_OCR_LANGUAGE takes effect. Returns a short status
    string for the API response — None if no restart was needed.
    """
    try:
        ps = subprocess.run(
            ["docker", "ps", "--filter", "name=homeos-paperless-web",
             "--filter", "status=running", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if "homeos-paperless-web" not in (ps.stdout or ""):
        return None  # BYO paperless or not running — nothing to restart
    try:
        subprocess.run(
            ["docker", "compose", "restart", "paperless-web"],
            capture_output=True, text=True, timeout=30, check=True,
        )
        return "paperless restarted to apply new OCR language"
    except subprocess.CalledProcessError as exc:
        log.warning("paperless restart failed: %s", exc.stderr)
        return f"paperless restart failed: {exc.stderr.decode() if isinstance(exc.stderr, bytes) else exc.stderr}"
    except subprocess.TimeoutExpired:
        return "paperless restart timed out (>30s) — restart manually with: docker compose restart paperless-web"


def apply_country(country_code: str) -> Dict[str, str]:
    """Look up the country's locale defaults, write to config.env, and
    restart Paperless if needed. Returns the derived locale dict plus
    a 'note' summarizing what changed.

    Unknown countries are a no-op (returns empty dict).
    """
    cc = (country_code or "").upper()
    locale = COUNTRY_LOCALE.get(cc)
    if not locale:
        return {"applied": False, "note": f"no locale mapping for country '{cc}' — keeping current values"}

    _write_env_vars({
        "YORIK_TZ": locale["tz"],
        "PAPERLESS_OCR_LANGUAGE": locale["ocr"],
    })
    # Update the running process's view of TZ too so any code reading
    # os.environ inside the same boot sees the new value.
    os.environ["YORIK_TZ"] = locale["tz"]
    os.environ["PAPERLESS_OCR_LANGUAGE"] = locale["ocr"]

    restart_note = _paperless_restart_if_bundled()

    return {
        "applied":      True,
        "country":      cc,
        "tz":           locale["tz"],
        "ocr_language": locale["ocr"],
        "language":     locale["language"],
        "note":         restart_note or "config.env updated — Paperless not running, no restart needed",
    }
