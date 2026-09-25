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


@router.get("/update", dependencies=[Depends(require_admin)])
def update_status(check: int = 0) -> dict[str, Any]:
    global _last_fetch
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
