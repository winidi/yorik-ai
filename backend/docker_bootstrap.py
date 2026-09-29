"""First start of the all-in-one Docker install (deploy/compose.yaml):
connect Yorik to the photo library, the document archive and the AI
model that run next to it. What start.sh does with `docker exec` and
curl on a classic install, done from inside Yorik's container over the
compose network, so the host needs nothing but Docker.

Run by docker/entrypoint.sh in the background (`python -m
backend.docker_bootstrap`). Every step is idempotent and skips itself
once done; a service that isn't up yet is retried for a while, then
left for the next container start.

    Paperless  token via POST /api/token/ with the admin from .env
    Immich     admin sign-up, login, API key "Yorik integration"
    Ollama     pull the configured model if it isn't there yet

The three run side by side: the model download (several GB) starts at
once instead of after the archive's first start, and Yorik claims the
Immich admin as soon as Immich answers, before anyone else who opens
the photo page could. The download's progress is kept in app_settings
(`ai_model_download`) for the Home screen.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from typing import Callable, Optional

import httpx

log = logging.getLogger("yorik.docker_bootstrap")

PAPERLESS = os.getenv("PAPERLESS_INTERNAL_URL", "http://paperless-web:8000").rstrip("/")
IMMICH = os.getenv("IMMICH_INTERNAL_URL", "http://immich-server:2283").rstrip("/")


def _say(msg: str) -> None:
    print(f"[bootstrap] {msg}", flush=True)


def _wait(probe: Callable[[], bool], what: str, minutes: int) -> bool:
    deadline = time.time() + minutes * 60
    while time.time() < deadline:
        try:
            if probe():
                return True
        except Exception:  # noqa: BLE001 — not up yet
            pass
        time.sleep(5)
    _say(f"{what} not reachable after {minutes} min — will try again at the next start")
    return False


def _set_setting(key: str, value: str) -> None:
    from .database import DEFAULT_DB_PATH, conn_ctx
    with conn_ctx(DEFAULT_DB_PATH) as c:
        c.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = datetime('now')",
            (key, value),
        )
        c.commit()


def paperless() -> None:
    from . import credential_store
    if (credential_store.get("paperless") or {}).get("api_key"):
        _say("documents: already connected")
        return
    user = os.getenv("PAPERLESS_ADMIN_USER", "admin")
    pw = os.getenv("PAPERLESS_ADMIN_PASSWORD", "")
    if not pw:
        _say("documents: no PAPERLESS_ADMIN_PASSWORD in the environment — skipping")
        return
    # Up = the API answers at all (anonymously it redirects to login).
    def up() -> bool:
        return httpx.get(f"{PAPERLESS}/api/", timeout=5, follow_redirects=False).status_code < 500

    _say("documents: waiting for the archive (first start takes a few minutes)")
    if not _wait(up, "document archive", 12):
        return
    r = httpx.post(f"{PAPERLESS}/api/token/", data={"username": user, "password": pw}, timeout=20)
    token = (r.json() or {}).get("token") if r.status_code == 200 else None
    if not token:
        _say(f"documents: token request failed (HTTP {r.status_code})")
        return
    credential_store.put("paperless", {"api_key": token, "base_url": PAPERLESS})
    _set_setting("paperless_api_token", token)
    _set_setting("paperless_base_url", PAPERLESS)
    _say("documents: connected")


def immich() -> None:
    from . import credential_store
    if (credential_store.get("immich") or {}).get("api_key"):
        _say("photos: already connected")
        return
    _say("photos: waiting for the photo library")
    if not _wait(lambda: httpx.get(f"{IMMICH}/api/server/ping", timeout=5).status_code == 200,
                 "photo library", 10):
        return
    admin = credential_store.get("immich_admin") or {}
    email = admin.get("email") or "admin@yorik.local"
    pw = admin.get("password") or secrets.token_hex(12)
    r = httpx.post(f"{IMMICH}/api/auth/admin-sign-up", timeout=20,
                   json={"email": email, "password": pw, "name": "Yorik Admin"})
    if r.status_code in (200, 201):
        credential_store.put("immich_admin", {"email": email, "password": pw})
    elif not admin:
        _say(f"photos: an admin exists already and Yorik doesn't know its password (HTTP {r.status_code})")
        return
    login = httpx.post(f"{IMMICH}/api/auth/login", timeout=20, json={"email": email, "password": pw})
    access = (login.json() or {}).get("accessToken") if login.status_code in (200, 201) else None
    if not access:
        _say(f"photos: admin login failed (HTTP {login.status_code})")
        return
    key = httpx.post(f"{IMMICH}/api/api-keys", timeout=20,
                     headers={"Authorization": f"Bearer {access}"},
                     json={"name": "Yorik integration", "permissions": ["all"]})
    secret = (key.json() or {}).get("secret") if key.status_code in (200, 201) else None
    if not secret:
        _say(f"photos: API key failed (HTTP {key.status_code})")
        return
    credential_store.put("immich", {"api_key": secret, "base_url": IMMICH})
    _set_setting("immich_base_url", IMMICH)
    _say("photos: connected")


def ollama() -> None:
    base = (os.getenv("HOMEOS_LLM_BASE_URL") or "").rstrip("/")
    model = os.getenv("HOMEOS_MODEL") or ""
    if "ollama" not in base or not model:
        return
    root = base[:-3] if base.endswith("/v1") else base
    if not _wait(lambda: httpx.get(f"{root}/api/tags", timeout=5).status_code == 200, "AI model server", 5):
        return
    have = [m.get("name") for m in httpx.get(f"{root}/api/tags", timeout=10).json().get("models", [])]
    if model in have or f"{model}:latest" in have:
        _say(f"AI model: {model} ready")
        return
    _say(f"AI model: downloading {model} (several GB, one-time)")
    _progress(model, "downloading", 0)
    last_log = last_note = 0.0
    try:
        with httpx.stream("POST", f"{root}/api/pull", json={"model": model}, timeout=None) as r:
            for line in r.iter_lines():
                if not line:
                    continue
                now = time.time()
                if now - last_note > 5:
                    pct = _percent(line)
                    if pct is not None:
                        _progress(model, "downloading", pct)
                        last_note = now
                if now - last_log > 60:
                    _say(f"AI model: {line[:120]}")
                    last_log = now
    except Exception:
        _progress(model, "failed", None)
        raise
    _progress(model, "ready", 100)
    _say(f"AI model: {model} ready")


def _percent(line: str) -> Optional[int]:
    """Ollama's pull stream: {"status": "pulling …", "completed": n, "total": m}."""
    try:
        d = json.loads(line)
        if d.get("total"):
            return int(100 * (d.get("completed") or 0) / d["total"])
    except (ValueError, TypeError):
        pass
    return None


def _progress(model: str, status: str, percent: Optional[int]) -> None:
    try:
        _set_setting("ai_model_download", json.dumps(
            {"model": model, "status": status, "percent": percent, "at": int(time.time())}))
    except Exception:  # noqa: BLE001 — progress is a courtesy, never a failure
        pass


def _run(step: Callable[[], None]) -> None:
    try:
        step()
    except Exception as exc:  # noqa: BLE001 — one step never blocks the others
        _say(f"{step.__name__}: {type(exc).__name__}: {exc}")


def main() -> None:
    threads = [threading.Thread(target=_run, args=(step,), name=f"bootstrap-{step.__name__}")
               for step in (immich, ollama, paperless)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
