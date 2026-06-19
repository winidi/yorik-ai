"""Boot-time MCP loader — reads YAML config, spawns clients, wires tools.

Config format (YAML at ``YORIK_MCP_SERVERS_FILE``, defaults to
``data/mcp_servers.yaml``)::

    servers:
      filesystem:
        command: npx
        args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
        env: {}
        connect_timeout: 30      # seconds (optional)
        call_timeout: 60         # seconds (optional)
      github:
        command: npx
        args: ["-y", "@modelcontextprotocol/server-github"]
        env:
          GITHUB_PERSONAL_ACCESS_TOKEN: "ghp_..."

For each server, every tool the server advertises becomes a Yorik tool
named ``mcp_<server>_<toolname>``. The names collision-resolve via the
server prefix so multiple servers can each expose a ``read_file``.

Boot is best-effort: if a server fails to start, log + skip — the rest
of Yorik keeps working.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...context import ToolContext
from ...tools import ToolResult
from .client import McpError, McpStdioClient

logger = logging.getLogger("yorik.agent.mcp.registry")

DEFAULT_CONFIG_PATH = "data/mcp_servers.yaml"


# ---------------------------------------------------------------------------
# Tool adapter — wraps an MCP tool as our Tool protocol
# ---------------------------------------------------------------------------


class McpToolAdapter:
    """Adapts a single MCP tool definition to Yorik's Tool protocol."""

    def __init__(
        self,
        client: McpStdioClient,
        mcp_tool: Dict[str, Any],
    ) -> None:
        self._client = client
        self._raw_name = mcp_tool["name"]
        self.name = f"mcp_{client.name}_{self._raw_name}"
        self.description = (
            f"[{client.name}] " + (mcp_tool.get("description") or self._raw_name)
        )
        # MCP tools advertise an inputSchema (JSON Schema) — pass through.
        self.json_schema = mcp_tool.get("inputSchema") or {
            "type": "object", "properties": {},
        }

    async def execute(self, ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
        try:
            raw = await self._client.call_tool(self._raw_name, args or {})
        except McpError as exc:
            return ToolResult(
                result_for_llm=f"ERROR: MCP server {self._client.name!r} tool "
                               f"{self._raw_name!r} failed: {exc}",
                metadata={"mcp_server": self._client.name, "error": str(exc)},
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                result_for_llm=f"ERROR: MCP tool {self.name!r} raised "
                               f"{type(exc).__name__}: {exc}",
            )

        # Result shape per MCP spec:
        # {"content": [{"type": "text", "text": "..."} | {"type": "image", ...}],
        #  "isError": bool}
        is_error = bool(raw.get("isError"))
        content = raw.get("content") or []
        text_parts: List[str] = []
        for item in content:
            if isinstance(item, dict):
                t = item.get("type")
                if t == "text":
                    text_parts.append(str(item.get("text") or ""))
                elif t in ("image", "audio", "resource"):
                    text_parts.append(f"[{t} content omitted from tool result]")
        text = "\n".join(p for p in text_parts if p) or json.dumps(raw, default=str)[:2000]

        if is_error:
            text = f"ERROR (MCP server reported isError=true): {text}"

        return ToolResult(
            result_for_llm=text,
            metadata={"mcp_server": self._client.name, "mcp_tool": self._raw_name},
        )


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


_active_clients: List[McpStdioClient] = []


def _load_config() -> Dict[str, Any]:
    path = Path(os.getenv("YORIK_MCP_SERVERS_FILE", DEFAULT_CONFIG_PATH))
    if not path.exists():
        return {}
    try:
        import yaml
    except ImportError:
        logger.warning("PyYAML not installed — skipping MCP config at %s", path)
        return {}
    try:
        with path.open("r") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception as exc:  # noqa: BLE001
        logger.exception("could not parse MCP config %s: %s", path, exc)
        return {}
    return data


async def load_and_register_mcp_servers(agent_registry: Any) -> int:
    """Read the config, spawn each server, register every discovered tool.

    Returns the count of registered tools. Best-effort: per-server
    failures are logged + skipped.
    """
    config = _load_config()
    servers = config.get("servers") or {}
    if not servers:
        return 0

    registered = 0
    for server_name, spec in servers.items():
        if not isinstance(spec, dict):
            logger.warning("MCP server %r: spec must be a dict", server_name)
            continue
        command = spec.get("command")
        if not command:
            logger.warning("MCP server %r: missing 'command' — skipping", server_name)
            continue
        try:
            client = McpStdioClient(
                name=server_name,
                command=command,
                args=spec.get("args") or [],
                env=spec.get("env") or {},
                cwd=spec.get("cwd"),
                connect_timeout=float(spec.get("connect_timeout") or 30.0),
                call_timeout=float(spec.get("call_timeout") or 60.0),
            )
            tools = await client.start()
        except Exception as exc:  # noqa: BLE001
            logger.exception("MCP server %r failed to start: %s", server_name, exc)
            continue

        _active_clients.append(client)
        for mcp_tool in tools:
            try:
                agent_registry.register(McpToolAdapter(client, mcp_tool))
                registered += 1
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "could not register MCP tool %r from %r: %s",
                    mcp_tool.get("name"), server_name, exc,
                )

    if registered:
        logger.info("MCP: registered %d tool(s) from %d server(s)",
                    registered, len(_active_clients))
    return registered


async def shutdown_all() -> None:
    """Close every active MCP client. Call from app shutdown if you want
    a clean exit; daemon threads will go away on process exit anyway."""
    for c in _active_clients:
        try:
            await c.close()
        except Exception:  # noqa: BLE001
            pass
    _active_clients.clear()


__all__ = [
    "McpToolAdapter",
    "load_and_register_mcp_servers",
    "shutdown_all",
]
