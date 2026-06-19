"""Slim MCP (Model Context Protocol) stdio client.

~300 LOC. Spawns a subprocess that speaks JSON-RPC 2.0 over stdin/stdout,
runs the MCP ``initialize`` handshake, discovers the server's tools via
``tools/list``, and exposes each one as a callable. The agent's tool
registry then registers them as ``mcp_<server>_<toolname>``.

Deferred (per masterplan — port from Hermes later if needed):
- HTTP / Streamable HTTP / SSE transports
- OAuth flows
- Sampling (server-initiated LLM calls)
- Notifications beyond log messages
- Resource subscriptions / progress notifications

Hermes's full MCP client is ~3600 LOC; ours is ~300 because we cover the
stdio path that everyone uses for local servers (filesystem, github,
sqlite, etc.) and skip the cloud transports until concrete need.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger("yorik.agent.mcp")


# JSON-RPC 2.0 keys
_JSONRPC_VERSION = "2.0"

# Conservative defaults — most MCP servers are local + responsive in <1s.
DEFAULT_CONNECT_TIMEOUT = 30.0
DEFAULT_CALL_TIMEOUT = 60.0


class McpError(RuntimeError):
    """Raised when an MCP server returns an error or fails to respond."""


class McpStdioClient:
    """One client per MCP server. Owns the subprocess + a single-reader thread.

    Lifecycle:
        client = McpStdioClient(name="files", command="npx", args=["..."], env={...})
        await client.start()                # spawn + initialize + tools/list
        result = await client.call_tool("read_file", {"path": "/etc/hostname"})
        await client.close()

    Not thread-safe — call from a single event loop. Concurrent calls
    from the same loop serialize via the internal request_id counter +
    pending-futures dict.
    """

    def __init__(
        self,
        *,
        name: str,
        command: str,
        args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        call_timeout: float = DEFAULT_CALL_TIMEOUT,
    ) -> None:
        self.name = name
        self.command = command
        self.args = list(args or [])
        self.env = env or {}
        self.cwd = cwd
        self.connect_timeout = connect_timeout
        self.call_timeout = call_timeout

        self._proc: Optional[subprocess.Popen] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._stop_reader = threading.Event()
        self._request_id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._tools_cache: List[Dict[str, Any]] = []
        self._init_capabilities: Dict[str, Any] = {}
        self._main_loop: Optional[asyncio.AbstractEventLoop] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> List[Dict[str, Any]]:
        """Spawn the subprocess, run handshake, return the tools list."""
        if self._proc is not None and self._proc.poll() is None:
            return self._tools_cache  # already started

        # Pass through PATH + the user-supplied env. The subprocess
        # gets only what we hand it (security: don't leak the rest of
        # the parent environment beyond what's needed to find binaries).
        full_env = {
            "PATH": os.getenv("PATH", ""),
            "HOME": os.getenv("HOME", ""),
            **self.env,
        }

        try:
            self._proc = subprocess.Popen(
                [self.command, *self.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=full_env,
                cwd=self.cwd,
                text=True,
                bufsize=1,  # line buffered
            )
        except FileNotFoundError as exc:
            raise McpError(f"command not found: {self.command!r}") from exc

        self._main_loop = asyncio.get_event_loop()
        self._stop_reader.clear()
        self._reader_thread = threading.Thread(
            target=self._read_loop, name=f"mcp-{self.name}-reader", daemon=True,
        )
        self._reader_thread.start()

        try:
            await asyncio.wait_for(self._handshake(), timeout=self.connect_timeout)
            await asyncio.wait_for(self._list_tools(), timeout=self.connect_timeout)
        except asyncio.TimeoutError as exc:
            await self.close()
            raise McpError(f"MCP server {self.name!r} did not respond in time") from exc
        except Exception:
            await self.close()
            raise
        logger.info(
            "MCP server %r ready: %d tool(s) discovered",
            self.name, len(self._tools_cache),
        )
        return self._tools_cache

    async def close(self) -> None:
        """Terminate the subprocess + stop the reader thread."""
        self._stop_reader.set()
        if self._proc is not None:
            try:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            except Exception:  # noqa: BLE001
                pass
            self._proc = None
        if self._reader_thread is not None and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=2.0)
        # Cancel any pending requests
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(McpError(f"server {self.name!r} closed"))
        self._pending.clear()

    # ------------------------------------------------------------------
    # Tool surface
    # ------------------------------------------------------------------

    @property
    def tools(self) -> List[Dict[str, Any]]:
        """The list of tool definitions discovered via ``tools/list``."""
        return list(self._tools_cache)

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Invoke a tool on the server. Returns the raw MCP result content."""
        result = await asyncio.wait_for(
            self._rpc("tools/call", {"name": tool_name, "arguments": arguments}),
            timeout=self.call_timeout,
        )
        # MCP tools/call result shape: {content: [{type, text}, ...], isError?: bool}
        return result

    # ------------------------------------------------------------------
    # JSON-RPC plumbing
    # ------------------------------------------------------------------

    async def _handshake(self) -> None:
        """Send ``initialize`` and wait for the server's capabilities."""
        resp = await self._rpc("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {
                # We don't implement sampling or resource subscriptions yet.
                "roots": {"listChanged": False},
            },
            "clientInfo": {"name": "yorik", "version": "0.1.0"},
        })
        self._init_capabilities = resp.get("capabilities") or {}
        # Per MCP spec, follow with the initialized notification (no response).
        self._send_notification("notifications/initialized", {})

    async def _list_tools(self) -> None:
        resp = await self._rpc("tools/list", {})
        tools = resp.get("tools") or []
        self._tools_cache = [t for t in tools if isinstance(t, dict) and t.get("name")]

    async def _rpc(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Send a request and await the matching response."""
        if self._main_loop is None:
            self._main_loop = asyncio.get_event_loop()
        self._request_id += 1
        rid = self._request_id
        fut: asyncio.Future = self._main_loop.create_future()
        self._pending[rid] = fut

        msg = {
            "jsonrpc": _JSONRPC_VERSION,
            "id":      rid,
            "method":  method,
            "params":  params or {},
        }
        self._write_message(msg)

        try:
            return await fut
        finally:
            self._pending.pop(rid, None)

    def _send_notification(self, method: str, params: Dict[str, Any]) -> None:
        """JSON-RPC notification — no id, no response."""
        self._write_message({
            "jsonrpc": _JSONRPC_VERSION,
            "method":  method,
            "params":  params or {},
        })

    def _write_message(self, msg: Dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise McpError(f"server {self.name!r} is not running")
        try:
            line = json.dumps(msg, ensure_ascii=False) + "\n"
            self._proc.stdin.write(line)
            self._proc.stdin.flush()
        except BrokenPipeError as exc:
            raise McpError(f"server {self.name!r} closed stdin") from exc

    # ------------------------------------------------------------------
    # Reader thread — runs in a daemon thread, posts to the asyncio loop
    # ------------------------------------------------------------------

    def _read_loop(self) -> None:
        """Read newline-delimited JSON messages from stdout, dispatch to
        pending futures (responses) or log (notifications).

        Runs in a daemon thread because subprocess.stdout.readline is
        blocking. We don't try to do non-blocking I/O on the subprocess
        pipe — the threading approach is simpler and good enough for
        small numbers of MCP servers (<10).
        """
        assert self._proc is not None
        stdout = self._proc.stdout
        if stdout is None:
            return
        while not self._stop_reader.is_set():
            try:
                line = stdout.readline()
            except Exception as exc:  # noqa: BLE001
                logger.debug("mcp %s: read error: %s", self.name, exc)
                break
            if not line:  # EOF
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as exc:
                logger.warning("mcp %s: invalid json from server: %s (%s)", self.name, exc, line[:200])
                continue
            self._dispatch(msg)

    def _dispatch(self, msg: Dict[str, Any]) -> None:
        """Route a parsed message: response → pending future; else log."""
        rid = msg.get("id")
        if rid is not None and rid in self._pending:
            fut = self._pending[rid]
            if msg.get("error"):
                err = msg["error"]
                exc = McpError(f"{err.get('message', 'mcp error')} (code={err.get('code')})")
                if self._main_loop and not fut.done():
                    self._main_loop.call_soon_threadsafe(fut.set_exception, exc)
            else:
                result = msg.get("result") or {}
                if self._main_loop and not fut.done():
                    self._main_loop.call_soon_threadsafe(fut.set_result, result)
        elif msg.get("method"):
            # Server-initiated notification (logging/message, etc.). Log + drop.
            logger.debug("mcp %s notification: %s", self.name, msg.get("method"))
        else:
            logger.debug("mcp %s unmatched message: %s", self.name, str(msg)[:200])


__all__ = ["McpStdioClient", "McpError"]
