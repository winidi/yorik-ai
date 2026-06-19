"""Slim MCP (Model Context Protocol) client — lands in Phase 6.

Will be a ~300 LOC stdio-only client. HTTP/SSE/OAuth deferred until
concrete need (Hermes's mcp_tool.py is ~3600 LOC; most of that is
transport variants and auth flows we don't need on day one).
"""
