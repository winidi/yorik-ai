"""Pluggable backends for agent capabilities.

Each subpackage is a capability with an ABC + registry. Concrete backends
register themselves at import time. Adding a new web-search provider or
memory backend is "drop a file in `providers/x/`" — no changes to the
loop or the registry.
"""
