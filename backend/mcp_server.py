"""Yorik as an MCP server — the skills registry for outside agents.

Any agent that speaks MCP over Streamable HTTP (Hermes, Claude Code, a
script using the official SDK) connects to ``POST /mcp`` with a personal
API token and sees one tool per skill its user may call. The tool
schemas come straight from the skill.md manifests, so a skill added to
the registry is on the wire without further work.

What stays inside Yorik:
  - Authentication is a personal API token bound to ONE user (see
    api_tokens.py). Cookies are not accepted here.
  - Authorization is the skill's own: the same SkillContext the chat
    path builds, with the token owner's role and user id. An outside
    agent can do exactly what its user can do in chat, nothing more.
  - Confirm-before-apply survives the hop. A destructive skill stages
    its action and the tool result carries ``pending_confirmation``;
    the agent asks its human and then calls ``pending_confirm`` or
    ``pending_cancel``.

Transport notes: this is the JSON response flavour of Streamable HTTP
(one JSON-RPC message per POST, no server-initiated stream). Clients
that ask for SSE get plain JSON, which the spec allows. GET answers 405
so a probing client learns quickly that there is no event stream.
"""

from __future__ import annotations

import json
import logging
import secrets
from typing import Any, Optional

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

log = logging.getLogger("yorik.mcp")

router = APIRouter(tags=["mcp"])

SERVER_NAME = "yorik"
SERVER_VERSION = "0.1.0"
_KNOWN_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
_DEFAULT_PROTOCOL_VERSION = "2025-06-18"
_TEXT_CAP = 60_000

INSTRUCTIONS = (
    "You are talking to a Yorik instance, a self-hosted household server "
    "(calendar, tasks, contacts, documents, photos, letters, email). Every "
    "tool here is one Yorik skill, run as the user who owns the API token. "
    "Call skill_view(name) before the first use of a skill to read its "
    "rules and argument details. Skills that delete something do not "
    "delete on their own: the result carries pending_confirmation with a "
    "pending_id and a preview. By default the owner confirms deletions in "
    "the Yorik app; tell your user the card is waiting in their notification "
    "bell, and use pending_cancel(pending_id) if they changed their mind. "
    "Only when the account allows agents to confirm deletions does "
    "pending_confirm(pending_id) run them, and then only after the human "
    "agreed. Creates and updates are applied at once and may also return a "
    "pending_id; pending_cancel undoes them."
)

_JSON_TYPES = {"string", "integer", "number", "boolean", "array", "object"}


# ─── auth ───────────────────────────────────────────────────────────

def _authenticate(request: Request) -> Optional[dict[str, Any]]:
    """Bearer API token only. Session cookies are deliberately ignored:
    a browser tab must not be able to drive the MCP surface."""
    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("bearer "):
        return None
    from .api_tokens import resolve_token
    return resolve_token(header[7:].strip())


def _unauthorized() -> JSONResponse:
    return JSONResponse(
        {"error": "unauthorized",
         "detail": "send a personal API token as 'Authorization: Bearer yk_…' "
                   "(create one under Settings → API tokens)"},
        status_code=401,
        headers={"WWW-Authenticate": 'Bearer realm="yorik-mcp"'},
    )


def _effective_role(user: dict[str, Any]) -> str:
    role = (user.get("role") or "member").lower()
    # Registry.invoke already treats platform_admin as admin for skills;
    # mirror that for the tool listing so the founder account sees them.
    return "admin" if role == "platform_admin" else role


# ─── tool catalogue ─────────────────────────────────────────────────

def _input_schema(inputs: Any) -> dict[str, Any]:
    """skill.md ``inputs`` → JSON Schema. Unknown types are left untyped
    rather than guessed, so the agent still sees the description."""
    props: dict[str, Any] = {}
    required: list[str] = []
    if isinstance(inputs, dict):
        for key, meta in inputs.items():
            if not isinstance(meta, dict):
                props[key] = {}
                continue
            prop: dict[str, Any] = {}
            t = meta.get("type")
            if isinstance(t, str) and t in _JSON_TYPES:
                prop["type"] = t
            if meta.get("description"):
                prop["description"] = str(meta["description"]).strip()
            if "default" in meta and meta["default"] is not None:
                prop["default"] = meta["default"]
            if isinstance(meta.get("enum"), list):
                prop["enum"] = meta["enum"]
            if isinstance(meta.get("items"), dict):
                prop["items"] = meta["items"]
            props[key] = prop
            if meta.get("required"):
                required.append(key)
    schema: dict[str, Any] = {
        "type": "object",
        "properties": props,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


def _skill_description(skill: Any) -> str:
    parts = [str(skill.description or "").strip()]
    tags = set(skill.tags or [])
    if "destructive" in tags:
        parts.append("Stages the deletion and returns pending_confirmation; "
                     "nothing is removed until pending_confirm(pending_id).")
    elif "mutation" in tags:
        parts.append("Applies the change at once; pending_cancel(pending_id) undoes it.")
    if skill.when_not_to_use:
        parts.append("Not for: " + " ".join(str(skill.when_not_to_use).split())[:300])
    return " ".join(p for p in parts if p)


_BUILTIN_TOOLS: list[dict[str, Any]] = [
    {
        "name": "skill_view",
        "description": "Full manifest of one skill: argument rules, when to use it, "
                       "examples. Read it before the first call to that skill.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "skill name"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "pending_confirm",
        "description": "Acknowledge an applied change, or run a staged deletion when the "
                       "account allows agents to confirm deletions. Only after the human "
                       "agreed to the preview.",
        "inputSchema": {
            "type": "object",
            "properties": {"pending_id": {"type": "string"}},
            "required": ["pending_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "pending_cancel",
        "description": "Discard a staged deletion, or undo an applied create/update.",
        "inputSchema": {
            "type": "object",
            "properties": {"pending_id": {"type": "string"}},
            "required": ["pending_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "whoami",
        "description": "The Yorik user this token acts as: name, role, language.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]
_BUILTIN_NAMES = {t["name"] for t in _BUILTIN_TOOLS}


def list_tools(user: dict[str, Any]) -> list[dict[str, Any]]:
    from .skills import get_registry
    from .skills.registry import _get_disabled_skills

    role = _effective_role(user)
    disabled = _get_disabled_skills()
    tools: list[dict[str, Any]] = []
    for s in sorted(get_registry().all(), key=lambda x: (x.effective_category, x.name)):
        if s.name in disabled or s.name in _BUILTIN_NAMES or "no-mcp" in (s.tags or []):
            continue
        if s.permissions and role not in s.permissions and "*" not in s.permissions:
            continue
        tools.append({
            "name": s.name,
            "description": _skill_description(s),
            "inputSchema": _input_schema(s.inputs),
        })
    return _BUILTIN_TOOLS + tools


# ─── tool execution ─────────────────────────────────────────────────

class ToolError(Exception):
    """Reported to the agent as an isError result, not a protocol error."""


async def call_tool(user: dict[str, Any], name: str, arguments: dict[str, Any]) -> Any:
    from .skills import get_registry, SkillContext, SkillError
    from .ui_tools import get_ui_actions, reset_ui_actions

    if name == "whoami":
        return {"id": user["id"], "name": user.get("name"), "role": user.get("role"),
                "language": user.get("language")}
    if name == "skill_view":
        skill_name = str(arguments.get("name") or "")
        reg = get_registry()
        view = reg.view(skill_name)
        if not view or not _may_call(user, reg.get(skill_name)):
            raise ToolError(f"unknown skill: {skill_name!r}")
        return view
    if name in ("pending_confirm", "pending_cancel"):
        return await _resolve_pending(user, name, str(arguments.get("pending_id") or ""))

    reg = get_registry()
    skill = reg.get(name)
    if not skill or not _may_call(user, skill):
        raise KeyError(name)
    ctx = SkillContext(reg, role=_effective_role(user), user_id=user["id"],
                       source=f"token:{user.get('token_name') or '?'}")
    reset_ui_actions()
    try:
        result = await reg.invoke(name, ctx=ctx, **(arguments or {}))
    except SkillError as exc:
        raise ToolError(str(exc)) from exc
    payload: dict[str, Any] = {"result": result}
    for action in get_ui_actions():
        if action.get("type") == "pending_confirmation":
            preview = action.get("preview") or {}
            block = {
                "pending_id": action.get("pending_id"),
                "skill": action.get("skill"),
                "preview": preview,
            }
            if preview.get("mode") == "confirm_before" and not user.get("agent_may_confirm_deletes"):
                _notify_owner_of_deletion(user, name, action)
                block["next"] = ("Nothing is deleted yet. The owner confirms this in the Yorik app "
                                 "(notification bell); pending_confirm is not allowed for this "
                                 "account. pending_cancel(pending_id) withdraws it.")
            else:
                block["next"] = ("pending_confirm(pending_id) after the human agreed, "
                                 "pending_cancel(pending_id) otherwise")
            payload["pending_confirmation"] = block
    return payload


def _notify_owner_of_deletion(user: dict[str, Any], skill: str, action: dict[str, Any]) -> None:
    """A staged deletion from an agent lands in the owner's bell with
    Delete / Keep buttons. Best effort: a failure here must not fail the
    skill call, the pending row still exists."""
    try:
        from . import notifications as _notif
        preview = action.get("preview") or {}
        subject = _preview_subject(preview)
        agent = user.get("token_name") or "An agent"
        _notif.create(
            user_id=user["id"],
            kind="agent_pending",
            title=f"{agent} wants to delete {subject}",
            body="Nothing has been removed. Tap Delete to run it, or Keep to leave it.",
            payload={
                "pending_id": action.get("pending_id"),
                "skill": skill,
                "preview": preview,
                "agent": agent,
            },
        )
    except Exception:  # noqa: BLE001
        log.exception("could not notify owner about staged deletion")


def _preview_subject(preview: dict[str, Any]) -> str:
    for key in ("event", "task", "contact", "draft", "bill"):
        obj = preview.get(key)
        if isinstance(obj, dict):
            title = obj.get("title") or obj.get("name") or obj.get("subject")
            if title:
                return f"{key} \"{title}\""
    return preview.get("title") or preview.get("action") or "an item"


def _may_call(user: dict[str, Any], skill: Any) -> bool:
    if skill is None:
        return False
    from .skills.registry import _get_disabled_skills
    if skill.name in _get_disabled_skills() or "no-mcp" in (skill.tags or []):
        return False
    role = _effective_role(user)
    return not skill.permissions or role in skill.permissions or "*" in skill.permissions


async def _resolve_pending(user: dict[str, Any], tool: str, pending_id: str) -> Any:
    """Reuse the HTTP confirm/cancel handlers so telemetry and ownership
    checks stay in one place."""
    from fastapi import HTTPException
    from . import main as _main  # late import: main imports this module

    if not pending_id:
        raise ToolError("pending_id is required")
    if tool == "pending_confirm" and not user.get("agent_may_confirm_deletes"):
        from . import pending_actions as _pa
        row = _pa.get(pending_id)
        if row and _pa.is_deferred(row):
            raise ToolError(
                "This deletion waits for the owner in the Yorik app (notification bell). "
                "Agents may not confirm deletions for this account; ask the person to tap "
                "Delete there, or use pending_cancel to withdraw it."
            )
    handler = _main.pending_confirm if tool == "pending_confirm" else _main.pending_cancel
    try:
        return await handler(pending_id, user=user)
    except HTTPException as exc:
        raise ToolError(str(exc.detail)) from exc


# ─── JSON-RPC over HTTP ─────────────────────────────────────────────

def _rpc_result(msg_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _rpc_error(msg_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": err}


def _as_text(obj: Any) -> str:
    text = json.dumps(obj, ensure_ascii=False, default=str)
    if len(text) > _TEXT_CAP:
        text = text[:_TEXT_CAP] + f"… [truncated, {len(text)} chars total]"
    return text


async def _handle(user: dict[str, Any], msg: Any) -> Optional[dict[str, Any]]:
    """One JSON-RPC message → one response dict, or None for notifications."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        return _rpc_error(msg.get("id") if isinstance(msg, dict) else None,
                          -32600, "invalid request")
    method = msg["method"]
    params = msg.get("params") or {}
    msg_id = msg.get("id")
    is_notification = "id" not in msg

    if method.startswith("notifications/"):
        return None

    if method == "initialize":
        requested = str(params.get("protocolVersion") or "")
        version = requested if requested in _KNOWN_PROTOCOL_VERSIONS else _DEFAULT_PROTOCOL_VERSION
        return _rpc_result(msg_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": INSTRUCTIONS,
        })
    if method == "ping":
        return _rpc_result(msg_id, {})
    if method == "tools/list":
        return _rpc_result(msg_id, {"tools": list_tools(user)})
    if method == "tools/call":
        name = str(params.get("name") or "")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _rpc_error(msg_id, -32602, "arguments must be an object")
        try:
            result = await call_tool(user, name, arguments)
        except KeyError:
            return _rpc_error(msg_id, -32602, f"unknown tool: {name}")
        except ToolError as exc:
            log.info("mcp tool error user=%s tool=%s: %s", user.get("id"), name, str(exc)[:200])
            return _rpc_result(msg_id, {
                "content": [{"type": "text", "text": str(exc)}],
                "isError": True,
            })
        except Exception as exc:  # noqa: BLE001 — never leak a traceback to the wire
            log.exception("mcp tool crashed user=%s tool=%s", user.get("id"), name)
            return _rpc_result(msg_id, {
                "content": [{"type": "text", "text": f"{name} failed: {type(exc).__name__}: {exc}"}],
                "isError": True,
            })
        log.info("mcp tool ok user=%s tool=%s", user.get("id"), name)
        out: dict[str, Any] = {"content": [{"type": "text", "text": _as_text(result)}],
                               "isError": False}
        if isinstance(result, dict):
            out["structuredContent"] = json.loads(json.dumps(result, default=str))
        return _rpc_result(msg_id, out)
    if is_notification:
        return None
    return _rpc_error(msg_id, -32601, f"method not found: {method}")


@router.post("/mcp", include_in_schema=False)
async def mcp_post(request: Request) -> Response:
    user = _authenticate(request)
    if not user:
        return _unauthorized()
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse(_rpc_error(None, -32700, "parse error"), status_code=400)

    messages = body if isinstance(body, list) else [body]
    if not messages:
        return JSONResponse(_rpc_error(None, -32600, "invalid request"), status_code=400)
    responses = [r for r in [await _handle(user, m) for m in messages] if r is not None]

    headers = {"Mcp-Session-Id": request.headers.get("mcp-session-id") or secrets.token_hex(16)}
    if not responses:
        return Response(status_code=202, headers=headers)
    payload: Any = responses if isinstance(body, list) else responses[0]
    return JSONResponse(payload, headers=headers)


@router.get("/mcp", include_in_schema=False)
@router.head("/mcp", include_in_schema=False)
async def mcp_get() -> Response:
    # No server-initiated stream; 405 is the spec's answer for that.
    return JSONResponse({"detail": "POST JSON-RPC messages to this endpoint"},
                        status_code=405, headers={"Allow": "POST, DELETE"})


@router.delete("/mcp", include_in_schema=False)
async def mcp_delete(request: Request) -> Response:
    if not _authenticate(request):
        return _unauthorized()
    return Response(status_code=204)
