"""Back-compat alias for `backend.ask`.

This module used to host the Vanna-AI-based agent loop. Since the
in-tree rewrite (May 2026, see backend/agent/) it became a thin facade
dispatching into backend.agent.loop. The file was renamed to
`backend/ask.py` in the dev-public push because the old name was
actively misleading — Vanna isn't loaded at runtime any more.

This shim is kept so external scripts that still `from backend import
vanna_agent` (one-off ops scripts, third-party plugins built against
the pre-rename name) keep working. New code should import `backend.ask`
directly.
"""

from __future__ import annotations

import warnings as _warnings

from . import ask as _ask
from .ask import *  # noqa: F401, F403 — surface every public name

_warnings.warn(
    "backend.vanna_agent is a deprecated alias for backend.ask — "
    "update your imports.",
    DeprecationWarning,
    stacklevel=2,
)

# Make attribute access transparent (e.g. `vanna_agent.LLM_BASE_URL`
# without re-exporting every constant). `from .ask import *` covers
# everything in __all__; this catches private-but-used names too.
def __getattr__(name: str):
    return getattr(_ask, name)
