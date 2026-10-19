"""Liveness watchdog: a plain thread asks this server over HTTP whether
it still answers; after several failures in a row the process exits so
systemd (Restart=on-failure) or Docker (restart: unless-stopped) starts
a fresh one.

Why: 2026-10-19 (F27) Postgres restarted for 15 s and the server kept
its port open but answered nothing for 8.5 h. Nobody at home restarts a
service by hand, so the box has to do it itself.

The probe goes through the same path a person's request takes (a plain
`def` route, so the thread pool must be alive); it does not touch the
database, because a long Postgres outage must not turn into a restart
loop that trips systemd's start limit."""

from __future__ import annotations

import logging
import os
import threading
import time
import urllib.request
from typing import Callable, Optional

log = logging.getLogger("yorik.watchdog")

INTERVAL_S = 30.0
PROBE_TIMEOUT_S = 15.0
FAILURES_TO_RESTART = 4          # ~2-3 minutes without an answer
EXIT_CODE = 70                   # EX_SOFTWARE; non-zero so Restart=on-failure fires
LIVE_PATH = "/api/health/live"


def bind_port() -> int:
    """Same resolution as the bind probe in main.py: tenants set
    HOMEOS_PORT, Docker and the plain install fall through to 8000."""
    try:
        return int(os.environ.get("HOMEOS_PORT") or os.environ.get("YORIK_PORT", "8000"))
    except ValueError:
        return 8000


def http_probe(url: str, timeout: float = PROBE_TIMEOUT_S) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 — loopback only
            return r.status == 200
    except Exception as e:  # noqa: BLE001
        log.warning("liveness probe failed: %s", e)
        return False


class Watchdog:
    def __init__(
        self,
        probe: Callable[[], bool],
        on_dead: Callable[[], None],
        *,
        interval: float = INTERVAL_S,
        failures_to_restart: int = FAILURES_TO_RESTART,
    ) -> None:
        self._probe = probe
        self._on_dead = on_dead
        self.interval = interval
        self.failures_to_restart = failures_to_restart
        self.failures = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def tick(self) -> bool:
        """One probe. Returns True when the process is considered dead
        (and on_dead has been called)."""
        if self._probe():
            if self.failures:
                log.info("server answers again after %d failed probes", self.failures)
            self.failures = 0
            return False
        self.failures += 1
        log.warning("server not answering (%d/%d)", self.failures, self.failures_to_restart)
        if self.failures >= self.failures_to_restart:
            self._on_dead()
            return True
        return False

    def run(self) -> None:
        # First probe only after one interval: uvicorn may still be binding.
        while not self._stop.wait(self.interval):
            if self.tick():
                return

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, name="yorik-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


def _exit_for_restart() -> None:
    log.critical(
        "server has not answered for %d probes — exiting with %d so the "
        "service manager restarts it", FAILURES_TO_RESTART, EXIT_CODE,
    )
    for h in logging.getLogger().handlers:
        try:
            h.flush()
        except Exception:  # noqa: BLE001
            pass
    # No Python cleanup: a stuck loop would never finish it, and systemd
    # kills the rest of the cgroup (Docker: the container) with us.
    os._exit(EXIT_CODE)


_instance: Optional[Watchdog] = None


def enabled() -> bool:
    if os.environ.get("YORIK_WATCHDOG", "1").strip().lower() in ("0", "off", "false", "no"):
        return False
    # Test clients run the startup hooks without a listening server.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    return True


def start() -> Optional[Watchdog]:
    global _instance
    if _instance is not None or not enabled():
        return _instance
    url = f"http://127.0.0.1:{bind_port()}{LIVE_PATH}"
    _instance = Watchdog(
        lambda: http_probe(url), _exit_for_restart,
        interval=INTERVAL_S, failures_to_restart=FAILURES_TO_RESTART,
    )
    _instance.start()
    log.info("liveness watchdog on %s every %.0f s, restart after %d failures",
             url, INTERVAL_S, FAILURES_TO_RESTART)
    return _instance


def stop() -> None:
    global _instance
    if _instance is not None:
        _instance.stop()
        _instance = None
