"""How fast this installation is, and deadlines that follow it.

The chat's deadlines (the query embedding, the model's search wordings,
the search fan-out, copying a quote) were set on Dirk's workstation, a
Ryzen 9 9950X with the models on a GPU. On a mini PC or a laptop they
cut work off that would have finished a moment later — and cutting off
means answering with less (Dirk 2026-09-28: "Manche Leute haben
langsamere Rechner. Nicht dass Yorik dann unbrauchbar wird").

Each kind of work keeps a moving average of how long it really took on
this machine; a deadline is its base value times that average over the
workstation's, never below 1x and at most MAX_FACTOR. A timeout counts
as a measurement of its full length, so a slow machine catches up after
a few questions instead of timing out for ever.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Dict, Optional

log = logging.getLogger("yorik.speed")

# Measured on the reference workstation, 2026-09-28 (medians).
REFERENCE = {
    "embed": 0.06,   # one search-query embedding (Qwen3-Embedding-4B service)
    "llm": 0.30,     # a short model answer (a few dozen tokens)
    "cpu": 0.11,     # the CPU check below
}
MAX_FACTOR = 8.0
ALPHA = 0.3          # weight of the newest measurement

_avg: Dict[str, float] = {}
_lock = threading.Lock()


def record(kind: str, seconds: Optional[float]) -> None:
    """One real duration of `kind` on this machine."""
    if seconds is None or seconds < 0 or kind not in REFERENCE:
        return
    with _lock:
        old = _avg.get(kind)
        new = seconds if old is None else (1 - ALPHA) * old + ALPHA * seconds
        _avg[kind] = new
    f = factor(kind)
    if f >= 2.0 and (old is None or old / REFERENCE[kind] < 2.0):
        log.info("speed: %s runs %.1fx slower than the reference — deadlines stretched", kind, f)


def _measure_cpu() -> float:
    from difflib import SequenceMatcher
    a = "die akademie ist kostenlos es gibt lediglich weitere inhalte wie vorlagen " * 3
    b = a.replace("kostenlos", "kostenfrei")
    t = time.perf_counter()
    for _ in range(300):
        SequenceMatcher(None, a, b, autojunk=False).ratio()
    return time.perf_counter() - t


def factor(kind: str) -> float:
    """How much slower than the reference this machine is at `kind`,
    between 1 and MAX_FACTOR; 1 until something was measured."""
    if kind == "cpu" and "cpu" not in _avg:
        record("cpu", _measure_cpu())          # once, ~0.1 s here
    avg = _avg.get(kind)
    if avg is None:
        return 1.0
    return max(1.0, min(MAX_FACTOR, avg / REFERENCE[kind]))


def budget(base_s: float, kind: str) -> float:
    """A deadline of `base_s` on the reference machine, for this one."""
    return base_s * factor(kind)


def timed(kind: str):
    """Context manager: records how long the block took."""
    class _T:
        def __enter__(self):
            self.t = time.perf_counter()
            return self

        def __exit__(self, exc_type, exc, tb):
            record(kind, time.perf_counter() - self.t)
            return False
    return _T()


def snapshot() -> Dict[str, Dict[str, float]]:
    """For the settings / health page: measured average and factor."""
    return {k: {"avg_s": round(_avg[k], 3), "factor": round(factor(k), 2)} for k in list(_avg)}
