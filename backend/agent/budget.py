# SPDX-License-Identifier: MIT
# Ported verbatim from NousResearch/hermes-agent agent/iteration_budget.py
# (MIT, https://github.com/NousResearch/hermes-agent). Cosmetic edits:
# Yorik-specific docstring framing; semantics unchanged.
"""Per-turn iteration budget for the agent loop — thread-safe consume/refund.

Yorik usage: one IterationBudget per /api/ask call, capped at
agent.loop.MAX_ITERATIONS (default 8). Tool calls that genuinely shouldn't
count against the budget (e.g. a "just look up something" subskill that
shouldn't shorten the user's real work) get a refund() so they don't
prematurely exhaust the loop.

Hermes uses this same class for parent + subagents with different caps.
Yorik doesn't have subagents (yet — see Phase 8 of the masterplan) so the
"parent cap vs subagent cap" distinction in the original docstring is
informational only for us.
"""

from __future__ import annotations

import threading


class IterationBudget:
    """Thread-safe iteration counter for an agent loop."""

    def __init__(self, max_total: int):
        self.max_total = max_total
        self._used = 0
        self._lock = threading.Lock()

    def consume(self) -> bool:
        """Try to consume one iteration. Returns True if allowed, False if the
        budget is exhausted."""
        with self._lock:
            if self._used >= self.max_total:
                return False
            self._used += 1
            return True

    def refund(self) -> None:
        """Give back one iteration. Useful when a tool call shouldn't count
        toward the budget (e.g. a no-op verification step)."""
        with self._lock:
            if self._used > 0:
                self._used -= 1

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    @property
    def remaining(self) -> int:
        with self._lock:
            return max(0, self.max_total - self._used)


__all__ = ["IterationBudget"]
