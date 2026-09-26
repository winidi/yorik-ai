"""In-app updates: Settings → System shows whether a new version is
ready and starts the update.

GET  /api/system/update   version here, commits waiting on origin, whether
                          the in-app update is installed / running
POST /api/system/update   start yorik-update.service (polkit lets the Yorik
                          user start exactly that unit; see install.sh)

`git fetch` runs at most every 6 hours (or on ?check=1). A working tree
with local changes is reported and never updated from here: that's a
developer's box, and `yorik upgrade` refuses it too.
"""
from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from .auth_sessions import require_admin

log = logging.getLogger("yorik.update")
router = APIRouter(prefix="/api/system", tags=["update"])

_REPO = Path(__file__).resolve().parent.parent
# All-in-one Docker install: releases come as images; the updater
# service (deploy/updater.sh) watches this shared folder.
_UPDATER = Path("/updater")
_RELEASES = "https://api.github.com/repos/winidi/yorik-ai/releases/latest"
_release_cache: dict[str, Any] = {}
_FETCH_EVERY_S = 6 * 3600
_last_fetch = 0.0


def _git(*args: str, timeout: int = 30) -> tuple[int, str]:
    try:
        r = subprocess.run(["git", *args], cwd=_REPO, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


def _systemctl(*args: str) -> int:
    try:
        return subprocess.run(["systemctl", *args], capture_output=True, timeout=15).returncode
    except (OSError, subprocess.TimeoutExpired):
        return 1


def _unit_installed() -> bool:
    try:
        out = subprocess.run(["systemctl", "list-unit-files", "yorik-update.service"],
                             capture_output=True, text=True, timeout=15).stdout
        return "yorik-update.service" in out
    except (OSError, subprocess.TimeoutExpired):
        return False


def _docker_mode() -> bool:
    import os
    return os.getenv("YORIK_RUNTIME") == "docker"


def _latest_release(check: bool) -> dict[str, Any]:
    """The newest published release (tag, name, notes), cached 6 h."""
    import httpx
    if not check and _release_cache and time.time() - _release_cache.get("at", 0) < _FETCH_EVERY_S:
        return _release_cache
    try:
        r = httpx.get(_RELEASES, timeout=10, headers={"Accept": "application/vnd.github+json"})
        if r.status_code == 200:
            d = r.json()
            _release_cache.update(at=time.time(), tag=d.get("tag_name") or "",
                                  name=d.get("name") or "", notes=(d.get("body") or "")[:2000])
    except Exception as exc:  # noqa: BLE001 — offline is fine
        log.info("release check failed: %s", exc)
    return _release_cache


def _docker_status(check: bool) -> dict[str, Any]:
    import os
    current = os.getenv("YORIK_VERSION") or "dev"
    rel = _latest_release(check)
    latest = rel.get("tag") or ""
    newer = bool(latest) and latest.lstrip("v") != current.lstrip("v") and current not in ("dev", "local")
    status = ""
    try:
        status = (_UPDATER / "status").read_text().strip()
    except OSError:
        pass
    notes = [l.strip("-* ").strip() for l in (rel.get("notes") or "").splitlines() if l.strip()][:12]
    return {
        "available": True, "runtime": "docker", "current": current, "latest": latest or None,
        "behind": 1 if newer else 0, "changes": notes if newer else [],
        "local_changes": False, "can_update": _UPDATER.is_dir(),
        "running": status.startswith("pulling") or (_UPDATER / "request").exists(),
        "updater_status": status or None, "checked": bool(rel),
    }


@router.get("/update", dependencies=[Depends(require_admin)])
def update_status(check: int = 0) -> dict[str, Any]:
    global _last_fetch
    if _docker_mode():
        return _docker_status(bool(check))
    if not (_REPO / ".git").exists():
        return {"available": False, "reason": "not a git checkout"}
    fetched = False
    if check or time.time() - _last_fetch > _FETCH_EVERY_S:
        code, _ = _git("fetch", "--quiet", "origin", timeout=60)
        fetched = code == 0
        if fetched:
            _last_fetch = time.time()
    _, head = _git("log", "-1", "--format=%h %cs")
    _, upstream = _git("rev-parse", "--abbrev-ref", "@{u}")
    code, log_out = _git("log", "--format=%s", "HEAD..@{u}")
    waiting = [line for line in log_out.splitlines() if line.strip()] if code == 0 else []
    _, dirty = _git("status", "--porcelain", "--untracked-files=no")
    return {
        "available": True,
        "current": head,
        "upstream": upstream or None,
        "behind": len(waiting),
        "changes": waiting[:12],
        "local_changes": bool(dirty),
        "can_update": _unit_installed(),
        "running": _systemctl("is-active", "--quiet", "yorik-update.service") == 0,
        "checked": fetched or _last_fetch > 0,
    }


@router.post("/update", dependencies=[Depends(require_admin)])
def start_update() -> dict[str, Any]:
    if _docker_mode():
        if not _UPDATER.is_dir():
            raise HTTPException(409, "The updater isn't part of this install.")
        (_UPDATER / "request").write_text(str(time.time()))
        log.info("in-app update requested (docker)")
        return {"ok": True, "message": "Updating. Yorik restarts by itself in a few minutes."}
    st = update_status()
    if not st.get("can_update"):
        raise HTTPException(409, "In-app updates aren't set up on this machine. Run bash install.sh once more, or ./scripts/yorik upgrade.")
    if st.get("local_changes"):
        raise HTTPException(409, "This copy of Yorik has local changes, so it isn't updated from here. Use ./scripts/yorik upgrade.")
    if not st.get("behind"):
        return {"ok": True, "message": "Already up to date."}
    code = _systemctl("start", "--no-block", "yorik-update.service")
    if code != 0:
        raise HTTPException(500, "The update couldn't start. Details are in the system log (journalctl -u yorik-update).")
    log.info("in-app update started (%s commits)", st.get("behind"))
    return {"ok": True, "message": "Updating. Yorik restarts by itself in a minute or two."}
