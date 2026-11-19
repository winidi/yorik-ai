"""yorik-guard: a Hermes agent reaches Yorik's data only through Yorik's tools.

Hermes already refuses to read .env files with its file tools, but an agent
with a terminal can write a script that does (2026-11-19: asked to put an
invoice into Paperless, the agent read Yorik's .env for the Paperless admin
password, looked into Yorik's container, and logged into Gmail with an app
password from another .env). This plugin vetoes such tool calls before they
run (pre_tool_call), whatever tool carries them:

    Yorik's folder          any path inside the Yorik install
    Yorik's containers      docker / podman commands on them (exec, cp, inspect, ...)
    container escapes       the Docker socket, host-root mounts, --privileged, nsenter
    process environments    /proc/<pid>/environ
    the services behind it  Paperless, Immich and Yorik's /api on this machine
    password files          .env, .env.local, ... anywhere (not .env.example)

The Yorik tools themselves (mcp__yorik__*) are never checked. A call is
judged by its text, so this stops the paths an agent takes, not a determined
attacker; it is a guard rail, not a sandbox.

guard.json next to this file says what to protect; setup.py writes it.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Iterable, List, Optional

log = logging.getLogger("yorik-guard")

_CONFIG_PATH = Path(__file__).with_name("guard.json")
_EXEMPT_TOOLS = {"tool_search", "tool_describe", "skill_view", "skills_list", "todo", "clarify"}
_DOCKER = re.compile(r"\b(docker|podman|nerdctl)\b")
_DOCKER_READ_ONLY = re.compile(r"\b(docker|podman|nerdctl)\s+(ps|stats|images|version)\b")
_ESCAPE = re.compile(r"docker\.sock|\bnsenter\b|--privileged\b|--pid[= ]host\b|(-v|--volume)[= ]?\s*/:")
_ENVIRON = re.compile(r"/proc/[^/\s'\"]+/environ\b")
# ".env", "/x/.env", ".env.local"; not ".env.example" (the documented shape),
# not process.env / import.meta.env (code that reads variables, not the file).
_ENV_FILE = re.compile(r"(?<![\w.-])(?<!process)(?<!meta)\.env(?:\.(?!example\b|sample\b|template\b|dist\b)[\w-]+)?(?![\w.-])")
_LOCAL_HOSTS = ["localhost", "127.0.0.1", "0.0.0.0", "[::1]", "::1", "host.docker.internal"]

MESSAGE = ("Blocked by yorik-guard: {reason}. Yorik's data (mail, documents, Paperless, photos, calendar) and "
           "its passwords are reached only through the Yorik tools (mcp__yorik__*), never around them. "
           "Do not try another way. If no Yorik tool does what the user asked, tell the user in one sentence "
           "which step is missing.")


def _config() -> dict:
    try:
        data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


def _path_forms(path: str, home: str) -> List[str]:
    p = path.rstrip("/")
    forms = [p]
    if home and p.startswith(home.rstrip("/") + "/"):
        rel = p[len(home.rstrip("/")) + 1:]
        forms += [f"~/{rel}", f"$home/{rel}", f"${{home}}/{rel}"]
        if "/" in rel:                       # "yorikai/yorik-ai" from ~, or after a cd
            forms.append(rel)
    return [f.lower() for f in forms]


def reason_for(text: str, cfg: dict) -> Optional[str]:
    """Why this tool-call text is refused, or None."""
    low = text.lower()
    home = cfg.get("home") or ""
    for d in cfg.get("yorik_dirs") or []:
        if any(f in low for f in _path_forms(d, home)):
            return "it reaches into Yorik's own folder"
    names = [n.lower() for n in (cfg.get("containers") or []) + (cfg.get("words") or []) if n]
    if _DOCKER.search(low) and any(n in low for n in names) and not (
            _DOCKER_READ_ONLY.search(low) and not re.search(r"\b(exec|cp|inspect|run|logs|attach|export|commit)\b", low)):
        return "it opens one of Yorik's containers"
    if _ESCAPE.search(low):
        return "it reaches the host through the container runtime"
    if _ENVIRON.search(low):
        return "it reads a process environment"
    hosts = [h.lower() for h in _LOCAL_HOSTS + list(cfg.get("hosts") or []) if h]
    host_re = "|".join(re.escape(h) for h in hosts)
    for port in cfg.get("service_ports") or []:
        if re.search(rf"(?<![\w.-])({host_re}):{int(port)}\b", low):
            return "it talks to a service behind Yorik (Paperless, Immich) directly"
    for port in cfg.get("api_ports") or []:
        if re.search(rf"(?<![\w.-])({host_re}):{int(port)}/api\b", low):
            return "it calls Yorik's web API around its tools"
    if cfg.get("block_env_files", True) and _ENV_FILE.search(text):
        return "it touches a .env password file"
    return None


def _is_yorik_tool(name: str) -> bool:
    return name.startswith(("mcp__yorik__", "mcp_yorik_"))


def check(tool_name: str, args: Any, cfg: Optional[dict] = None) -> Optional[str]:
    """The block message for this call, or None to let it run."""
    cfg = _config() if cfg is None else cfg
    name = tool_name or ""
    if _is_yorik_tool(name) or name in _EXEMPT_TOOLS:
        return None
    if name == "tool_call" and isinstance(args, dict) and isinstance(args.get("calls"), list):
        for call in args["calls"]:
            if isinstance(call, dict):
                msg = check(str(call.get("name") or ""), call.get("arguments"), cfg)
                if msg:
                    return msg
        return None
    if isinstance(args, str):                # some callers pass the raw JSON
        try:
            args = json.loads(args)
        except ValueError:
            pass
    reason = reason_for("\n".join(_strings(args)), cfg)
    if not reason:
        return None
    log.warning("yorik-guard blocked %s: %s", name, reason)
    return MESSAGE.format(reason=reason)


def _on_pre_tool_call(tool_name: str = "", args: Any = None, **_: Any) -> Optional[dict]:
    msg = check(tool_name, args)
    return {"action": "block", "message": msg} if msg else None


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
