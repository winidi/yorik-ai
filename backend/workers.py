"""Background-worker heartbeat registry.

Long-running daemon threads (email IMAP fetcher, WhatsApp WS
subscriber, backup scheduler, etc.) call `heartbeat(...)` periodically.
The home screen polls `get_all()` and renders a colored chip per
worker so a silent crash becomes visible instead of "huh, email
stopped syncing yesterday and I never noticed."

In-memory only — survival across restarts isn't useful here (a fresh
process starts fresh workers and they heartbeat within seconds). DB
storage would add lock contention to the hot path for no benefit.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Literal, Optional

Status = Literal["ok", "warn", "error", "starting"]


@dataclass
class WorkerState:
    name:           str
    status:         Status = "starting"
    detail:         str = ""
    kind:           str = ""              # 'supervisor' | 'subscriber' | 'scheduler' | …
    last_heartbeat: Optional[float] = None  # epoch seconds
    started_at:     float = field(default_factory=time.time)
    error_count:    int = 0
    # How often this worker is expected to heartbeat under normal
    # operation. Drives the stale-threshold computation in `get_all()`:
    # a worker that legitimately ticks every 6 hours should not flip
    # to yellow after the global 5-minute floor. Workers that tick
    # often (≤60s) just use the default and the floor wins.
    expected_interval_s: int = 60

    def to_dict(self) -> dict:
        d = asdict(self)
        # Replace epoch floats with ISO strings + a stale-seconds field
        # the frontend can format relative ("3s ago").
        now = time.time()
        d["last_heartbeat_age_s"] = (
            int(now - self.last_heartbeat) if self.last_heartbeat else None
        )
        d["uptime_s"] = int(now - self.started_at)
        return d


_lock = threading.Lock()
_workers: dict[str, WorkerState] = {}


def register(name: str, kind: str = "", expected_interval_s: int = 60) -> None:
    """Called once at worker startup. Idempotent — re-registering an
    existing worker just resets its started_at (handy after a reload).
    `expected_interval_s` lets workers with naturally-long cadences
    (scheduler that runs every 6h, IMAP IDLE that only ticks on new
    mail) avoid being mis-flagged as stale by the home-screen poller."""
    with _lock:
        _workers[name] = WorkerState(
            name=name, kind=kind, status="starting",
            expected_interval_s=max(1, int(expected_interval_s)),
        )


def heartbeat(name: str, status: Status = "ok", detail: str = "") -> None:
    """Worker says 'I'm alive and here's what I'm doing.' Call from the
    worker's main loop after each successful iteration, or with
    status='warn'/'error' when something soft-fails."""
    with _lock:
        w = _workers.get(name)
        if w is None:
            w = WorkerState(name=name)
            _workers[name] = w
        w.status = status
        w.detail = detail
        w.last_heartbeat = time.time()
        if status == "error":
            w.error_count += 1


def report_error(name: str, detail: str) -> None:
    """Short-hand for `heartbeat(name, 'error', detail)` that doesn't
    update last_heartbeat — use when the worker crashed and won't
    heartbeat again until restart."""
    with _lock:
        w = _workers.get(name)
        if w is None:
            w = WorkerState(name=name)
            _workers[name] = w
        w.status = "error"
        w.detail = detail
        w.error_count += 1


def get_all() -> list[dict]:
    """Snapshot of all known workers, sorted by name."""
    with _lock:
        out = [w.to_dict() for w in _workers.values()]
    # Mark workers that haven't heartbeat in 5x their expected interval
    # as stale. The floor (_STALE_THRESHOLD_S) protects against workers
    # that declared an unrealistically tight interval; the per-worker
    # 5× factor protects long-cadence schedulers (paperless reconciler
    # ticks every 6 hours — at the old global 5-min floor it was yellow
    # ~98% of every cycle while functioning perfectly).
    for w in out:
        age = w.get("last_heartbeat_age_s")
        if age is None or w["status"] != "ok":
            continue
        threshold = max(_STALE_THRESHOLD_S, 5 * int(w.get("expected_interval_s") or 60))
        if age > threshold:
            w["status"] = "warn"
            w["detail"] = f"no heartbeat for {age}s — may be stuck"
    out.sort(key=lambda d: d["name"])
    return out


_STALE_THRESHOLD_S = 300  # 5 minutes — floor; per-worker may be longer
