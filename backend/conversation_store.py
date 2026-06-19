"""SQLite-backed ConversationStore so Vanna's multi-turn context survives restarts.

Wires `Agent(conversation_store=SqliteConversationStore())` so when a client
passes `conversation_id` to `agent.send_message(...)`, prior messages are
loaded, the LLM gets the full history, and new messages are appended on disk.

Schema (defined in database.py):
    conversations(id TEXT PK, user_role TEXT, messages TEXT, created_at, updated_at)

`messages` is JSON: a list of Vanna Message objects serialized via Pydantic.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import List, Optional

# Vanna removed in Phase 4 — these come from a thin shim now. This
# legacy store coexists with the new agent_conversations table during
# the transition; the new loop bypasses it entirely.
from .agent.vanna_shim import Conversation, ConversationStore, Message, User

from .database import DEFAULT_DB_PATH, conn_ctx

DB_PATH = os.getenv("HOMEOS_DB_PATH", DEFAULT_DB_PATH)


def _serialize_messages(messages: List[Message]) -> str:
    return json.dumps([m.model_dump(mode="json") for m in messages])


def _deserialize_messages(blob: str) -> List[Message]:
    if not blob:
        return []
    raw = json.loads(blob)
    return [Message.model_validate(m) for m in raw]


def _role_of(user: User) -> str:
    """User.id is f'{role}@homeos.local' in our resolver. Pull the role half."""
    if user.group_memberships:
        return user.group_memberships[0]
    return (user.id or "anonymous").split("@", 1)[0]


class SqliteConversationStore(ConversationStore):
    """Persistent ConversationStore backed by the HomeOS SQLite DB."""

    async def create_conversation(
        self, conversation_id: str, user: User, initial_message: str
    ) -> Conversation:
        msg = Message(role="user", content=initial_message)
        conv = Conversation(id=conversation_id, user=user, messages=[msg])
        with conn_ctx(DB_PATH) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO conversations (id, user_role, messages, created_at, updated_at) "
                "VALUES (?, ?, ?, datetime('now'), datetime('now'))",
                (conversation_id, _role_of(user), _serialize_messages([msg])),
            )
        return conv

    async def get_conversation(self, conversation_id: str, user: User) -> Optional[Conversation]:
        with conn_ctx(DB_PATH) as conn:
            row = conn.execute(
                "SELECT id, user_role, messages, created_at, updated_at FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
        if not row:
            return None
        # MVP scoping: row's user_role must match the caller's role.
        if row["user_role"] != _role_of(user):
            return None
        return Conversation(
            id=row["id"],
            user=user,
            messages=_deserialize_messages(row["messages"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    async def update_conversation(self, conversation: Conversation) -> None:
        with conn_ctx(DB_PATH) as conn:
            conn.execute(
                "UPDATE conversations SET messages = ?, updated_at = datetime('now') WHERE id = ?",
                (_serialize_messages(conversation.messages), conversation.id),
            )
            # If the row didn't exist yet (Vanna can call update before create),
            # fall back to an insert.
            if conn.total_changes == 0 or not conn.execute(
                "SELECT 1 FROM conversations WHERE id = ?", (conversation.id,)
            ).fetchone():
                conn.execute(
                    "INSERT OR REPLACE INTO conversations (id, user_role, messages, created_at, updated_at) "
                    "VALUES (?, ?, ?, datetime('now'), datetime('now'))",
                    (
                        conversation.id,
                        _role_of(conversation.user),
                        _serialize_messages(conversation.messages),
                    ),
                )

    async def delete_conversation(self, conversation_id: str, user: User) -> bool:
        with conn_ctx(DB_PATH) as conn:
            row = conn.execute(
                "SELECT user_role FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            if not row or row["user_role"] != _role_of(user):
                return False
            conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
        return True

    async def list_conversations(
        self, user: User, limit: int = 50, offset: int = 0
    ) -> List[Conversation]:
        with conn_ctx(DB_PATH) as conn:
            rows = conn.execute(
                "SELECT id, user_role, messages, created_at, updated_at FROM conversations "
                "WHERE user_role = ? ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (_role_of(user), limit, offset),
            ).fetchall()
        return [
            Conversation(
                id=r["id"],
                user=user,
                messages=_deserialize_messages(r["messages"]),
                created_at=datetime.fromisoformat(r["created_at"]),
                updated_at=datetime.fromisoformat(r["updated_at"]),
            )
            for r in rows
        ]
