"""Import-time registration of all built-in plugins.

Imported once from backend/main.py at startup. Each submodule's
register_*() calls run as side effects, populating the registries
before the first analyse_message call.

Adding a new built-in plugin = adding it to one of the package
imports below. Adding a third-party addon = it imports the registry
and calls register_*() itself; this file doesn't change."""

from __future__ import annotations

import logging

log = logging.getLogger("yorik.suggestions.bootstrap")


def bootstrap() -> None:
    """Idempotent — safe to call multiple times. Registers core
    retrievers, suggestion types, and triggers."""
    # Side-effect imports — every module calls register_*() on load.
    from . import retrievers as _retrievers  # noqa: F401
    # Suggestion types + triggers arrive on Day 3.
    try:
        from . import types as _types  # noqa: F401
    except ImportError:
        pass
    try:
        from . import triggers as _triggers  # noqa: F401
    except ImportError:
        pass

    from . import registry as _reg
    log.info(
        "suggestions bootstrap: %d retrievers, %d types, %d trigger-events",
        len(_reg.get_retrievers()),
        len(_reg.all_types()),
        sum(len(_reg.get_triggers(e)) for e in ["email.new", "wa.new", "calendar.invite"]),
    )
