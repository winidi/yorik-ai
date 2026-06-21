"""Core context retrievers. Each module here defines one retriever
and registers it at import time. The engine iterates the registry —
adding a new retriever is one new file + an import line, no engine
changes.

Importing this package registers all built-in retrievers as a side
effect. Done from backend.suggestions.bootstrap so the engine always
has them on startup."""

from . import contact      # noqa: F401  — registers itself on import
from . import calendar     # noqa: F401
from . import email_history  # noqa: F401
from . import tasks        # noqa: F401
