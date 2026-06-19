# SPDX-License-Identifier: MIT
# Ported verbatim from NousResearch/hermes-agent agent/retry_utils.py
# (MIT, https://github.com/NousResearch/hermes-agent). Cosmetic edits to
# the module docstring; semantics unchanged.
"""Jittered exponential backoff — anti-thundering-herd retry delays.

Yorik calls this from the LLM client wrapper when an OpenAI-compatible
endpoint (llama-swap, OpenRouter, etc.) returns a transient failure
(429, 503, connection reset). Without jitter, two concurrent /api/ask
calls that hit the same rate limit both back off for exactly the same
duration and collide again on retry. The jitter decorrelates them.

Defaults: base 5s, max 120s, jitter ratio 0.5 (jitter adds 0-50% of the
computed delay).
"""

import random
import threading
import time

# Monotonic counter for jitter seed uniqueness within the same process.
# Protected by a lock to avoid race conditions in concurrent retry paths
# (e.g. multiple gateway sessions retrying simultaneously).
_jitter_counter = 0
_jitter_lock = threading.Lock()


def jittered_backoff(
    attempt: int,
    *,
    base_delay: float = 5.0,
    max_delay: float = 120.0,
    jitter_ratio: float = 0.5,
) -> float:
    """Compute a jittered exponential backoff delay.

    Args:
        attempt: 1-based retry attempt number.
        base_delay: Base delay in seconds for attempt 1.
        max_delay: Maximum delay cap in seconds.
        jitter_ratio: Fraction of computed delay to use as random jitter
            range. 0.5 means jitter is uniform in [0, 0.5 * delay].

    Returns:
        Delay in seconds: min(base * 2^(attempt-1), max_delay) + jitter.

    The jitter decorrelates concurrent retries so multiple sessions
    hitting the same provider don't all retry at the same instant.
    """
    global _jitter_counter
    with _jitter_lock:
        _jitter_counter += 1
        tick = _jitter_counter

    exponent = max(0, attempt - 1)
    if exponent >= 63 or base_delay <= 0:
        delay = max_delay
    else:
        delay = min(base_delay * (2 ** exponent), max_delay)

    # Seed from time + counter for decorrelation even with coarse clocks.
    seed = (time.time_ns() ^ (tick * 0x9E3779B9)) & 0xFFFFFFFF
    rng = random.Random(seed)
    jitter = rng.uniform(0, jitter_ratio * delay)

    return delay + jitter


__all__ = ["jittered_backoff"]
