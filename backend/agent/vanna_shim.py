"""Compat shim for ex-Vanna types.

When we ripped Vanna out of the runtime (Phase 4 of the masterplan),
several modules still imported its types as a type-only / interface
surface — most notably ``backend/ui_tools.py`` which defines 10 tools
that subclass ``vanna.core.tool.Tool[ArgsModel]``.

Rather than rewrite every tool class on day one of the cutover, this
shim provides drop-in replacements that quack the same shape:

- ``Tool[T]``       — generic base; only attributes accessed are
                     ``.name``, ``.description``, ``get_args_schema()``,
                     ``execute(ctx, args)``.
- ``ToolContext``   — Pydantic model with ``user``, ``conversation_id``,
                     ``request_id``, ``agent_memory``, ``metadata``.
- ``ToolResult``    — Pydantic model with ``success``, ``result_for_llm``,
                     ``ui_component``, ``metadata``, ``error``.
- ``User``          — Pydantic model with ``id``, ``group_memberships``,
                     ``metadata``.
- ``Conversation`` / ``Message`` — for the legacy SqliteConversationStore
                     (kept for backwards compatibility with the old API
                     surface; the new loop uses ``agent_conversations``).

The shim is a real Python module that ui_tools.py imports from instead
of ``vanna.*``. No magic, no sitecustomize tricks — just renamed imports.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Generic, List, Optional, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


# ---------------------------------------------------------------------------
# User + UserResolver
# ---------------------------------------------------------------------------


class User(BaseModel):
    """Mirror of vanna.User. Pydantic for backwards compatibility with the
    legacy code that constructs it positionally / by kwargs."""
    id: str = ""
    group_memberships: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "allow"}


class RequestContext(BaseModel):
    """Mirror of vanna.core.user.RequestContext. Minimal envelope."""
    metadata: Dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "allow"}


class UserResolver:
    """Stub UserResolver — the new loop builds Users directly so this is
    only here for the legacy `backend/auth.py` import surface."""

    def __init__(self, default_role: str = "admin") -> None:
        self.default_role = default_role

    def resolve(self, request: Any = None, **_: Any) -> User:
        return User(id=f"{self.default_role}@homeos.local",
                    group_memberships=[self.default_role])


# ---------------------------------------------------------------------------
# AgentMemory stub
# ---------------------------------------------------------------------------


class AgentMemory:
    """No-op stub. The new loop doesn't use Vanna's agent memory; the
    legacy ToolContext field still wants the type."""

    def __init__(self, *_: Any, **__: Any) -> None:
        pass

    def __getattr__(self, item: str) -> Any:
        # Tools that poke at memory just get None / no-op.
        return lambda *_a, **_kw: None


# ---------------------------------------------------------------------------
# ToolContext / ToolResult / UiComponent stubs
# ---------------------------------------------------------------------------


class UiComponent(BaseModel):
    """Minimal stand-in for vanna.components.UiComponent. The new loop
    doesn't use it — ui_actions flow through a side-channel list and
    the response envelope — so this is only here so ui_tools.py can
    instantiate it without an ImportError."""
    rich_component: Any = None
    simple_component: Any = None
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None).isoformat())

    model_config = {"arbitrary_types_allowed": True, "extra": "allow"}


class ToolContext(BaseModel):
    """Mirror of vanna.core.tool.ToolContext. Legacy tools instantiate
    via either model_validate or our adapter; both work."""
    user: User
    conversation_id: str = ""
    request_id: str = ""
    agent_memory: Any = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    model_config = {"arbitrary_types_allowed": True, "extra": "allow"}


class ToolResult(BaseModel):
    """Mirror of vanna.core.tool.ToolResult.

    Legacy tools return this from execute(); the adapter unwraps into our
    own ToolResult dataclass. ``success`` is preserved even though the new
    loop doesn't gate on it (the BLOCKED-vs-success bug from the Vanna era
    taught us to ignore that flag).
    """
    success: bool = True
    result_for_llm: str = ""
    ui_component: Any = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    model_config = {"arbitrary_types_allowed": True, "extra": "allow"}


# ---------------------------------------------------------------------------
# Tool[T] generic base
# ---------------------------------------------------------------------------


class Tool(Generic[T]):
    """Vanna-shaped Tool base. Subclasses implement ``name``, ``description``,
    ``get_args_schema``, and ``execute``. The new loop never calls these
    directly — it goes through ``backend.agent.tools.Tool`` (our own
    protocol) wired up by the native tool ports.
    """

    @property
    def name(self) -> str:
        raise NotImplementedError

    @property
    def description(self) -> str:
        raise NotImplementedError

    def get_args_schema(self) -> Any:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, context: ToolContext, args: Any) -> ToolResult:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Conversation / Message — legacy SqliteConversationStore only
# ---------------------------------------------------------------------------


class Message(BaseModel):
    """Mirror of vanna.Message. Used by the legacy conversation store."""
    role: str
    content: Any = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    model_config = {"arbitrary_types_allowed": True, "extra": "allow"}


class Conversation(BaseModel):
    """Mirror of vanna.Conversation. Used by the legacy conversation store."""
    id: str
    user: User
    messages: List[Message] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    model_config = {"arbitrary_types_allowed": True, "extra": "allow"}


class ToolCall(BaseModel):
    """Stub vanna.ToolCall — Pydantic shape for an LLM tool-call."""
    id: str = ""
    name: str = ""
    arguments: Dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "allow"}


class AgentConfig(BaseModel):
    """Stub vanna.AgentConfig."""
    model_config = {"extra": "allow"}


class ConversationStore:
    """Stub base for the legacy SqliteConversationStore (which still exists
    for any code path that holds a reference; the new loop bypasses it)."""

    async def create_conversation(self, *a: Any, **kw: Any) -> Conversation:
        raise NotImplementedError

    async def get_conversation(self, *a: Any, **kw: Any) -> Optional[Conversation]:
        raise NotImplementedError

    async def update_conversation(self, *a: Any, **kw: Any) -> None:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Deep-import stubs for vanna_agent.py legacy code paths
# ---------------------------------------------------------------------------
#
# These exist purely to satisfy ``from vanna.foo.bar import X`` lines in
# vanna_agent.py at module load. The actual code paths that instantiate
# them (the old Vanna agent loop) are dead after Phase 4 — the dispatcher
# always routes to the new backend now.


class AuditLogger:
    """Stub. Real audit hooks live in ``backend.agent.audit``."""


class AuditEvent:
    """Stub for the AuditEvent dataclass."""

    def __init__(self, *_: Any, **kw: Any) -> None:
        for k, v in kw.items():
            setattr(self, k, v)


class AuditConfig:
    """Stub for vanna.core.agent.config.AuditConfig."""

    def __init__(self, **_: Any) -> None:
        pass


class LlmRequest(BaseModel):
    """Stub — only the type annotation is used."""

    messages: List[Any] = Field(default_factory=list)
    model_config = {"extra": "allow"}


class SystemPromptBuilder:
    """Stub vanna.core.system_prompt.base.SystemPromptBuilder.

    The new HomeOSSystemPromptBuilder no longer needs to inherit from
    anything — its build_system_prompt is just an async function the
    loop calls directly — but keeping the inheritance until the legacy
    file is fully rewritten lets the existing class definition stay
    unchanged.
    """

    async def build_system_prompt(self, user: Any = None, tools: Any = None) -> str:
        return ""


class DemoAgentMemory(AgentMemory):
    """Stub for vanna.integrations.local.agent_memory.DemoAgentMemory."""

    def __init__(self, max_items: int = 100) -> None:
        super().__init__()
        self.max_items = max_items


class OpenAILlmService:
    """Stub vanna.integrations.openai.OpenAILlmService.

    QwenLlmService inherits this in the legacy file. We never construct
    a Vanna LLM service in the new backend (we use our own LlmClient
    from backend.agent.llm) so the body can be empty.
    """

    def __init__(self, *_: Any, **kw: Any) -> None:
        self.model = kw.get("model", "")
        self.base_url = kw.get("base_url", "")
        self.api_key = kw.get("api_key", "")
        # Provide _client = SimpleNamespace(chat = ...) so the patched_create
        # injection in QwenLlmService.__init__ doesn't crash.
        from types import SimpleNamespace

        def _noop_create(**_kw: Any) -> Any:
            raise RuntimeError(
                "Legacy Vanna LLM service is dead after Phase 4 cutover; "
                "set HOMEOS_AGENT_BACKEND=own (default) and route through "
                "backend.agent.loop instead."
            )
        self._client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=_noop_create)
            )
        )


class SqliteRunner:
    """Functional replacement for vanna.integrations.sqlite.SqliteRunner.

    The legacy ``RoleGatedSqliteRunner`` subclasses this. Its override
    intercepts ``run_sql`` first to do role-gating + ``__BLOCKED__`` marker
    insertion, then (for safe queries) delegates to ``super().run_sql()``
    — which IS this method. So the implementation here has to actually
    execute SQL and return a pandas DataFrame, exactly matching Vanna's
    runtime contract.
    """

    def __init__(self, *, database_path: str = "") -> None:
        self.database_path = database_path

    async def run_sql(self, args: Any, context: Any) -> Any:
        """Execute ``args.sql`` against the sqlite file. Returns a pandas
        DataFrame matching Vanna's contract (one row per result, columns
        from the cursor description; non-SELECT returns an empty df).

        Async signature is preserved (Vanna's runner is async too); the
        sqlite call itself is sync — that's fine, queries are typically
        <10ms.
        """
        import sqlite3
        import pandas as pd

        sql = (args.sql if hasattr(args, "sql") else (args.get("sql") if isinstance(args, dict) else "")) or ""
        if not sql.strip():
            return pd.DataFrame()

        # Read-only sqlite path: connect, execute, fetch.
        conn = sqlite3.connect(self.database_path)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute(sql)
            head = sql.strip().lstrip("(").lstrip().lower()
            is_select = head.startswith("select") or head.startswith("with")
            if is_select:
                rows = cur.fetchall()
                cols = [d[0] for d in (cur.description or [])]
                return pd.DataFrame([dict(r) for r in rows], columns=cols or None)
            # Non-SELECT — commit + return an empty df with rowcount metadata.
            conn.commit()
            df = pd.DataFrame()
            df.attrs["rows_affected"] = cur.rowcount
            return df
        finally:
            conn.close()


# Schema cache for the run_sql description. Without this, the LLM
# hallucinates table + column names every time it considers SQL
# (audit caught it inventing `tax_returns`, `documents.filename`,
# `contacts.name`/`title`/`business_name` — real columns: `display_name`).
# Read once per process; schema only changes on uvicorn restart after
# _ensure_columns migrations.
_RUN_SQL_SCHEMA_CACHE: Optional[str] = None


# Whitelist of tables that are realistic SQL-analytics targets. Other
# tables exist but most are bookkeeping / connector internals / FTS
# shadows that the LLM has no reason to query. Whitelisting keeps the
# schema summary in the run_sql tool description to ~1.5KB (vs ~6KB
# unfiltered) — significant prompt-budget savings on every turn.
_SCHEMA_WHITELIST = (
    # Core household data
    "events", "tasks", "bills", "task_categories",
    # Contacts hub
    "contacts", "contact_addresses", "contact_channels",
    # Documents (lives in a separate documents.db, but the LLM
    # asks about docs via run_sql sometimes — see migration below)
    # Email + WhatsApp
    "email_accounts", "email_messages", "email_drafts",
    "wa_chats", "wa_messages",
    # Users + calendars
    "user_profiles", "calendars", "calendar_shares", "event_attendees",
    # Compose
    "compose_drafts",
)


def _build_schema_summary() -> str:
    """Compact column list for user-facing tables only. See
    _SCHEMA_WHITELIST for what's included and why. This goes into the
    run_sql tool description so the LLM stops hallucinating table +
    column names every time it considers SQL (audit caught
    `tax_returns`, `documents.filename`, `contacts.name`/`title`/
    `business_name` — real column: `display_name`).
    """
    try:
        from backend.database import get_conn
        with get_conn() as conn:
            existing = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            lines = []
            for t in _SCHEMA_WHITELIST:
                if t not in existing:
                    continue
                cols = [r["name"] for r in conn.execute(
                    f"PRAGMA table_info({t})"
                ).fetchall()]
                lines.append(f"  {t}({', '.join(cols)})")
        return "\n".join(lines)
    except Exception:
        return ""


def _get_schema_summary() -> str:
    global _RUN_SQL_SCHEMA_CACHE
    if _RUN_SQL_SCHEMA_CACHE is None:
        _RUN_SQL_SCHEMA_CACHE = _build_schema_summary()
    return _RUN_SQL_SCHEMA_CACHE


class RunSqlTool(Tool):
    """Functional replacement for vanna.tools.RunSqlTool.

    The legacy ``GatedRunSqlTool`` subclasses this. Its override calls
    ``self.sql_runner.run_sql`` first (above), inspects the DataFrame for
    a ``__BLOCKED__`` column, and on non-blocked queries calls
    ``super().execute(context, args)`` — which is this method. So this
    has to take a DataFrame from the runner and turn it into a
    :class:`ToolResult` the LLM can read.
    """

    def __init__(self, *, sql_runner: SqliteRunner = None, **_: Any) -> None:  # type: ignore[assignment]
        self.sql_runner = sql_runner

    @property
    def name(self) -> str:
        return "run_sql"

    @property
    def description(self) -> str:
        # No schema dump here. Putting the schema in the tool description
        # ships it in the system-prompt-side tool catalog every turn,
        # which biases the LLM toward run_sql over the dedicated skills.
        # Schema is surfaced reactively in execute() on 'no such table /
        # column' errors instead — costs one wasted call per session
        # only when SQL is actually attempted, zero prompt cost otherwise.
        return (
            "Execute SQL queries against the configured database. "
            "For any domain table that has a dedicated skill in the index "
            "(events, tasks, bills, contacts, documents), use that skill — "
            "raw SQL is refused with a redirect message. Skills emit "
            "interactive chat cards; run_sql returns a flat dataframe the "
            "user can't act on. Reserve run_sql for ad-hoc analytics "
            "(counts, joins, aggregates) that no skill covers. If a query "
            "fails with 'no such table' or 'no such column', the error "
            "response includes the available schema — retry once with the "
            "corrected names."
        )

    def get_args_schema(self) -> Any:
        # Mirror vanna.capabilities.sql_runner.RunSqlToolArgs.
        class RunSqlToolArgs(BaseModel):
            sql: str

        return RunSqlToolArgs

    async def execute(self, context: ToolContext, args: Any) -> ToolResult:
        """Run a SQL query and format the result for the LLM.

        Mirrors vanna.tools.RunSqlTool.execute (run_sql.py:56-148) closely
        enough that subclasses (GatedRunSqlTool) can call super().execute()
        and get the same end-to-end behaviour. Differences from Vanna's
        original:

        - The infamous hardcoded "Query executed successfully. N row(s)
          affected." string for non-SELECT remains (legitimate behaviour
          for `INSERT INTO saved_queries` etc. — only the blocked-by-gate
          marker needed special handling, and that's the subclass's job).
        - We return our shim's ToolResult (no NotificationComponent
          plumbing; the new agent loop doesn't render Vanna UiComponent
          types anyway).
        """
        sql = args.sql if hasattr(args, "sql") else (args.get("sql") if isinstance(args, dict) else "")
        try:
            df = await self.sql_runner.run_sql(args, context)
        except Exception as exc:  # noqa: BLE001
            msg = f"Error executing query: {exc}"
            # Schema-on-error: 'no such table' / 'no such column' are the
            # two failure modes the LLM hits when it hallucinates names.
            # Attach the schema so the retry can land — but only on these
            # specific errors, not every failure (syntax errors etc. get
            # the bare message).
            err_lower = str(exc).lower()
            if "no such table" in err_lower or "no such column" in err_lower:
                schema = _get_schema_summary()
                if schema:
                    msg += (
                        "\n\nAvailable schema — use ONLY these tables and columns. "
                        "Retry your query once with the corrected names; do NOT "
                        "ask the user to clarify, the right answer is below:\n"
                        + schema
                    )
            return ToolResult(
                success=False,
                result_for_llm=msg,
                error=str(exc),
            )

        head = (sql or "").strip().lstrip("(").lstrip().lower()
        is_select = head.startswith("select") or head.startswith("with")

        if is_select:
            if df is None or df.empty:
                text = "Query executed successfully. No rows returned."
            else:
                # Cap output so a SELECT * on a big table doesn't blow the
                # context window. 50 rows + the header is plenty for the
                # LLM to summarise.
                head_n = df.head(50)
                csv_text = head_n.to_csv(index=False)
                text = csv_text
                if len(df) > 50:
                    text += f"\n[truncated — showed 50 of {len(df)} rows]"
            return ToolResult(success=True, result_for_llm=text)

        # Non-SELECT path. df.attrs["rows_affected"] is set by our SqliteRunner.
        rows_affected = (df.attrs.get("rows_affected", 0)
                         if df is not None and hasattr(df, "attrs") else 0)
        return ToolResult(
            success=True,
            result_for_llm=f"Query executed successfully. {rows_affected} row(s) affected.",
            metadata={"rows_affected": rows_affected},
        )


class ToolRegistry:
    """Stub vanna.ToolRegistry. The legacy file builds one at module load.
    Methods are no-ops; the new backend builds its own registry."""

    def __init__(self, *_: Any, **__: Any) -> None:
        self._tools: List[Any] = []

    def register_local_tool(self, tool: Any, **_: Any) -> None:
        self._tools.append(tool)


class Agent:
    """Stub vanna.Agent. Legacy file constructs at module load. Methods
    raise if called — the dispatcher always routes around them."""

    def __init__(self, *_: Any, **__: Any) -> None:
        pass

    async def send_message(self, *_: Any, **__: Any):
        raise RuntimeError(
            "Legacy Vanna Agent.send_message called after Phase 4 cutover; "
            "set HOMEOS_AGENT_BACKEND=own (default) so /api/ask routes to "
            "backend.agent.loop instead."
        )
        yield  # makes this an async generator (matches Vanna's signature)


# ---------------------------------------------------------------------------
# Components — vanna.components.* stubs (used by GatedRunSqlTool path)
# ---------------------------------------------------------------------------


class ComponentType:
    NOTIFICATION = "notification"
    DATAFRAME = "dataframe"
    RICH_TEXT = "rich_text"
    SIMPLE_TEXT = "simple_text"


class NotificationComponent(BaseModel):
    type: str = ComponentType.NOTIFICATION
    level: str = "info"
    message: str = ""

    model_config = {"extra": "allow"}


class SimpleTextComponent(BaseModel):
    text: str = ""

    model_config = {"extra": "allow"}


class RichTextComponent(BaseModel):
    text: str = ""

    model_config = {"extra": "allow"}


class DataFrameComponent(BaseModel):
    rows: List[Any] = Field(default_factory=list)
    columns: List[str] = Field(default_factory=list)
    title: str = ""
    description: str = ""

    model_config = {"extra": "allow"}

    @classmethod
    def from_records(cls, records: List[Dict[str, Any]], title: str = "",
                     description: str = "") -> "DataFrameComponent":
        cols = list(records[0].keys()) if records else []
        return cls(rows=records, columns=cols, title=title, description=description)


__all__ = [
    "User",
    "RequestContext",
    "UserResolver",
    "AgentMemory",
    "UiComponent",
    "ToolContext",
    "ToolResult",
    "Tool",
    "Message",
    "Conversation",
    "ConversationStore",
    "ToolCall",
    "AgentConfig",
    # Deep stubs
    "AuditLogger",
    "AuditEvent",
    "AuditConfig",
    "LlmRequest",
    "SystemPromptBuilder",
    "DemoAgentMemory",
    "OpenAILlmService",
    "SqliteRunner",
    "RunSqlTool",
    "ToolRegistry",
    "Agent",
    "ComponentType",
    "NotificationComponent",
    "SimpleTextComponent",
    "RichTextComponent",
    "DataFrameComponent",
]
