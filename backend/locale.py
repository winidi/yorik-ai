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

# country code → (timezone, OCR language pack(s), per-user reply language,
# the money people there count in)
# OCR packs are Tesseract language codes; always include "eng" as a fallback
# so foreign-language documents in the same library still OCR.
# US has many timezones — default to America/New_York; user can override.
COUNTRY_LOCALE: Dict[str, Dict[str, str]] = {
    "DE": {"tz": "Europe/Berlin", "ocr": "deu+eng", "language": "de", "currency": "EUR"},
    "AT": {"tz": "Europe/Vienna", "ocr": "deu+eng", "language": "de", "currency": "EUR"},
    "CH": {"tz": "Europe/Zurich", "ocr": "deu+fra+ita+eng", "language": "de", "currency": "CHF"},
    "US": {"tz": "America/New_York", "ocr": "eng", "language": "en", "currency": "USD"},
    "GB": {"tz": "Europe/London", "ocr": "eng", "language": "en", "currency": "GBP"},
    "PL": {"tz": "Europe/Warsaw", "ocr": "pol+eng", "language": "pl", "currency": "PLN"},
    "FR": {"tz": "Europe/Paris", "ocr": "fra+eng", "language": "fr", "currency": "EUR"},
    "ES": {"tz": "Europe/Madrid", "ocr": "spa+eng", "language": "es", "currency": "EUR"},
    "IT": {"tz": "Europe/Rome", "ocr": "ita+eng", "language": "it", "currency": "EUR"},
}


def _in_docker() -> bool:
    return os.getenv("YORIK_RUNTIME") == "docker"


def remember(locale: Dict[str, str], country: str) -> None:
    """Keep the chosen locale in the database, so it survives a rebuilt
    container (config.env inside the Docker image does not) and the
    household's currency has a source."""
    from .household_settings import set_setting
    set_setting("locale.country", country)
    set_setting("locale.tz", locale["tz"])
    set_setting("locale.ocr", locale["ocr"])
    set_setting("locale.currency", locale["currency"])


def apply_saved() -> None:
    """At start-up: the timezone the household chose wins over the
    installer's guess. Called from main's startup."""
    try:
        from .household_settings import get_setting
        tz = get_setting("locale.tz")
    except Exception:  # noqa: BLE001
        return
    if tz:
        os.environ["YORIK_TZ"] = tz


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

    try:
        remember(locale, cc)
    except Exception as exc:  # noqa: BLE001
        log.warning("locale: could not store the choice: %s", exc)
    # Update the running process's view of TZ too so any code reading
    # os.environ inside the same boot sees the new value.
    os.environ["YORIK_TZ"] = locale["tz"]
    os.environ["PAPERLESS_OCR_LANGUAGE"] = locale["ocr"]

    if _in_docker():
        # The stack's .env (TZ, PAPERLESS_OCR_LANGUAGE) lives on the host;
        # the installer filled it from the computer's own settings. The
        # container cannot edit it or restart Paperless, so only Yorik's
        # own timezone and the currency change here.
        note = ("timezone and currency saved — the document reader's language comes from the "
                "installer (PAPERLESS_OCR_LANGUAGE in deploy/.env)")
    else:
        _write_env_vars({
            "YORIK_TZ": locale["tz"],
            "PAPERLESS_OCR_LANGUAGE": locale["ocr"],
        })
        note = _paperless_restart_if_bundled() or "config.env updated — Paperless not running, no restart needed"

    return {
        "applied":      True,
        "country":      cc,
        "tz":           locale["tz"],
        "ocr_language": locale["ocr"],
        "language":     locale["language"],
        "currency":     locale["currency"],
        "note":         note,
    }
