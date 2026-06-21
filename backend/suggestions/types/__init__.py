"""Built-in suggestion types — imported by bootstrap.py.

Each submodule register_type()s itself at import time. Addons can
register more types from outside this package; the engine doesn't
care where they came from."""

from __future__ import annotations

from . import draft_reply  # noqa: F401
from . import propose_meeting_slot  # noqa: F401
