"""Built-in suggestion triggers — imported by bootstrap.py.

Each submodule register_trigger()s itself at import time. The
upstream emitter (email fetcher, WA bridge, …) calls
get_triggers(event)[i].on_fire(payload) and the trigger module
decides whether to call engine.analyse_message."""

from __future__ import annotations

from . import email_new  # noqa: F401
