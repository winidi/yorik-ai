"""Per-request context objects threaded through the agent loop.

Three dataclasses, no inheritance:

- ``User``: who's asking. Surfaces ``id`` + ``role`` + ``language`` so
  tools can do per-user filtering and reply in the right language.
- ``RequestContext``: full envelope for one /api/ask call. Carries the
  user, conversation_id, language, identified_name (voice ID), and a
  request_id for log correlation.
- ``ToolContext``: what a tool sees. Wraps RequestContext + the request's
  message and adds a ``conversation_history`` snapshot for tools that
  want to peek at prior turns.

Frozen where it makes sense; pickleable for trajectory dumping in a
future phase.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class User:
    """The calling user. Mirrors what FastAPI's ``current_user`` resolves
    from the session cookie (plus voice-ID overlay if applicable)."""
    id: int
    role: str = "admin"
    language: str = "en"
    name: Optional[str] = None  # display name; voice-ID surfaces this
    # group_memberships is here for OpenAI-tool-call compatibility with
    # any future per-tool ACL — not used by our current 11 tools, but
    # cheap to carry through.
    group_memberships: tuple = ()


@dataclass
class RequestContext:
    """One /api/ask envelope. Mutable so the loop can pin a generated
    request_id, but treat fields as effectively immutable past loop entry."""
    user: User
    conversation_id: Optional[str] = None
    language: str = "en"
    identified_name: Optional[str] = None
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    # The original user message text (sanitised). Tools sometimes want to
    # quote it back; cheaper to read here than walk the message list.
    message: str = ""


@dataclass
class ToolContext:
    """What gets passed to ``Tool.execute(ctx, args)``.

    Wraps RequestContext + the current iteration's accumulated messages
    so tools can introspect the conversation (e.g. ``check_calendar`` may
    want to see prior tool results before deciding what to query).
    """
    request: RequestContext
    iteration: int = 0
    # Snapshot of messages accumulated so far this turn — read-only for
    # tools (mutations should go through the loop, not the context).
    conversation_so_far: List[Dict[str, Any]] = field(default_factory=list)

    # Convenience pass-throughs so tools can write ``ctx.user`` /
    # ``ctx.role`` / ``ctx.user_id`` instead of ``ctx.request.user.role`` —
    # matches the shape Yorik's skills already expect from their old
    # SkillContext (registry.py line 65: ``self.user_id = user_id``).
    @property
    def user(self) -> User:
        return self.request.user

    @property
    def user_id(self) -> int:
        return self.request.user.id

    @property
    def role(self) -> str:
        return self.request.user.role

    @property
    def conversation_id(self) -> Optional[str]:
        return self.request.conversation_id

    @property
    def language(self) -> str:
        return self.request.language


__all__ = ["User", "RequestContext", "ToolContext"]
