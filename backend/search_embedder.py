"""The embedding service, started only when it is needed.

Qwen3-Embedding-4B in its llama.cpp container (scripts/install-search-
embedder.sh) held 3–5 GB of RAM around the clock for a job that runs a
few minutes a day. Dirk, 2026-10-07: "Yorik soll auch auf möglichst
kleinen PCs funktionieren." So the container now sleeps. The index
sweep starts it for its window (every hour, or at night at a set
time — Settings › Search by meaning), embeds what is new, and stops it
again. A search that needs a query vector while it sleeps starts it in
the background and finds by keyword meanwhile; a few seconds later the
meaning branch is back. "Keep the model loaded: always" restores the
old behaviour for boxes with room.

The container is driven with the docker command line (start / stop by
name); where there is no docker, nothing here does anything and the
service is used as it is. State is in memory: when it was last used
and whether a start is under way.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from typing import Optional

log = logging.getLogger("yorik.search_embedder")

CONTAINER = os.getenv("YORIK_SEARCH_EMBED_CONTAINER", "yorik-search-embed")
START_WAIT_S = float(os.getenv("YORIK_SEARCH_EMBED_START_WAIT_S", "60"))
IDLE_STOP_S = int(os.getenv("YORIK_SEARCH_EMBED_IDLE_S", "900"))      # 15 min without a call → stop

_last_used = 0.0
_starting: Optional[threading.Thread] = None
_lock = threading.Lock()
_sweeping = False


def docker() -> Optional[str]:
    return shutil.which("docker")


def touch() -> None:
    global _last_used
    _last_used = time.time()


def idle_seconds() -> float:
    return time.time() - _last_used if _last_used else float("inf")


def reachable() -> bool:
    from . import search_index
    return bool(search_index.service_reachable())


def state() -> str:
    """'running' | 'starting' | 'stopped' | 'unmanaged' (no docker)."""
    if reachable():
        return "running"
    if _starting is not None and _starting.is_alive():
        return "starting"
    return "stopped" if docker() else "unmanaged"


def _run(*args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run([docker(), *args], capture_output=True, text=True, timeout=timeout, check=False)


def _container_exists() -> bool:
    r = _run("container", "inspect", "--format", "{{.State.Status}}", CONTAINER, timeout=15)
    return r.returncode == 0


def start(wait: bool = True) -> bool:
    """Start the container and (optionally) wait until /health answers.
    True when the service can be used."""
    from . import search_index
    if not search_index.EMBED_URL:
        return False
    if reachable():
        touch()
        return True
    if not docker():
        return False
    with _lock:
        if _container_exists():
            r = _run("start", CONTAINER, timeout=30)
        else:
            # first time after an install: the compose profile creates it
            r = _run("compose", "--profile", "search-embed", "up", "-d", "--no-deps", "search-embed", timeout=120)
        if r.returncode != 0:
            log.warning("search embedder: could not start %s: %s", CONTAINER, (r.stderr or r.stdout).strip()[:200])
            return False
    log.info("search embedder: %s started", CONTAINER)
    if not wait:
        return True
    t0 = time.time()
    while time.time() - t0 < START_WAIT_S:
        if reachable():
            touch()
            log.info("search embedder: ready after %.0f s", time.time() - t0)
            return True
        time.sleep(1.5)
    log.warning("search embedder: %s did not answer within %.0f s", CONTAINER, START_WAIT_S)
    return False


def start_in_background() -> None:
    """For a search that finds the service asleep: start it, do not wait."""
    global _starting
    if not docker() or (_starting is not None and _starting.is_alive()):
        return
    _starting = threading.Thread(target=start, kwargs={"wait": True}, name="search-embedder-start", daemon=True)
    _starting.start()


def stop() -> bool:
    if not docker() or _sweeping:
        return False
    r = _run("stop", "-t", "10", CONTAINER, timeout=40)
    if r.returncode == 0:
        log.info("search embedder: %s stopped (idle %.0f s)", CONTAINER, min(idle_seconds(), 10**6))
        return True
    return False


def sweeping(on: bool) -> None:
    global _sweeping
    _sweeping = on
    if on:
        touch()


def maybe_stop_idle(keep: str) -> bool:
    """Stop the service after IDLE_STOP_S without a call, unless the
    household keeps it loaded. Called by the index scheduler's tick."""
    if keep == "always" or not docker() or _sweeping:
        return False
    if idle_seconds() >= IDLE_STOP_S and reachable():
        return stop()
    return False
