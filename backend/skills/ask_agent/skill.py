"""ask_agent — delegate one question to the household's strong agent.

The agent is anything with an OpenAI-shaped chat endpoint; the reference
is Hermes' API server (``hermes gateway`` platform ``api_server``,
default 127.0.0.1:8642). Yorik sends one request and returns the text.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

log = logging.getLogger("yorik.skills.ask_agent")


def personal_agent(user_id: Any) -> Optional[dict[str, Any]]:
    """The asking person's own agent from their profile, or None."""
    if not user_id:
        return None
    try:
        from backend.database import get_conn
        with get_conn() as conn:
            row = conn.execute(
                "SELECT agent_url, agent_key, agent_name FROM user_profiles WHERE id = ?", (str(user_id),)
            ).fetchone()
    except Exception as exc:  # noqa: BLE001
        log.warning("ask_agent: could not read the profile agent (%s)", exc)
        return None
    if not row or not (row["agent_url"] or "").strip():
        return None
    return {"url": row["agent_url"].strip().rstrip("/"), "key": (row["agent_key"] or "").strip(),
            "name": (row["agent_name"] or "").strip() or "your agent"}


def shared_agent_enabled() -> bool:
    """The household agent from config.env is used for people without
    their own only when the admin says so; off by default, because it
    is somebody's machine with somebody's files."""
    return (os.getenv("HOMEOS_AGENT_SHARED") or "").strip().lower() in ("1", "true", "yes", "on")


def _cfg(ctx=None) -> dict[str, Any]:
    own = personal_agent(getattr(ctx, "user_id", None)) if ctx is not None else None
    if own:
        base = own
    elif shared_agent_enabled():
        base = {"url": (os.getenv("HOMEOS_AGENT_URL") or "").strip().rstrip("/"),
                "key": (os.getenv("HOMEOS_AGENT_KEY") or "").strip(),
                "name": (os.getenv("HOMEOS_AGENT_NAME") or "Hermes").strip()}
    else:
        base = {"url": "", "key": "", "name": (os.getenv("HOMEOS_AGENT_NAME") or "Hermes").strip()}
    return {
        **base,
        "model": (os.getenv("HOMEOS_AGENT_MODEL") or "hermes-agent").strip(),
        "timeout": float(os.getenv("HOMEOS_AGENT_TIMEOUT") or 150),
        # Thinking level Hermes runs the question with: none (fast, default),
        # low, medium, high. Yorik questions are chat turns; deep reasoning
        # costs 4-5x the tokens for the same answer.
        "reasoning": (os.getenv("HOMEOS_AGENT_REASONING") or "none").strip().lower(),
    }


def _user(ctx) -> tuple[str, str]:
    """(name, language) of the asking user; blanks when unknown."""
    uid = getattr(ctx, "user_id", None)
    if not uid:
        return "", ""
    try:
        from backend.database import get_conn
        with get_conn() as conn:
            row = conn.execute(
                "SELECT name, language FROM user_profiles WHERE id = ?", (uid,)
            ).fetchone()
        if row:
            return row["name"] or "", (row["language"] or "").lower()
    except Exception:  # noqa: BLE001
        log.debug("ask_agent: user lookup failed", exc_info=True)
    return "", ""


def _session_id(ctx) -> str:
    uid = str(getattr(ctx, "user_id", "") or "anon")
    conv = str(getattr(ctx, "conversation_id", "") or "chat")
    return f"yorik-{uid}-{conv}"[:120]


async def execute(ctx, question: str, context: Optional[str] = None) -> dict[str, Any]:
    cfg = _cfg(ctx)
    question = (question or "").strip()
    if not question:
        return {"error": "question is empty"}
    if not cfg["url"]:
        return {
            "error": "no agent configured for you",
            "_llm_hint": "Tell the user that no agent is set up for them yet (Settings > You > My agent) "
                         "and answer as best you can.",
        }

    name, language = _user(ctx)
    system = (
        f"You are being asked through Yorik, the household server, by "
        f"{name or 'a household member'}. Answer in plain text, no markdown tables, "
        f"in the language of the question{' (' + language + ')' if language else ''}. "
        f"Be concise: this reply is read back in a chat window."
    )
    user_msg = question if not context else f"{question}\n\nContext: {context.strip()}"
    headers = {"Content-Type": "application/json", "X-Hermes-Session-Id": _session_id(ctx)}
    if cfg["key"]:
        headers["Authorization"] = f"Bearer {cfg['key']}"
    body = {
        "model": cfg["model"],
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user_msg}],
        "stream": False,
    }
    if cfg["reasoning"]:
        body["model_options"] = {"reasoning_effort": cfg["reasoning"]}

    import httpx
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(cfg["timeout"], connect=5.0)) as client:
            resp = await client.post(f"{cfg['url']}/chat/completions", json=body, headers=headers)
    except httpx.ConnectError:
        return {"error": f"{cfg['name']} is not reachable at {cfg['url']} (is the workstation on?)"}
    except httpx.TimeoutException:
        return {"error": f"{cfg['name']} did not answer within {int(cfg['timeout'])} s"}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{cfg['name']} request failed: {type(exc).__name__}: {exc}"}

    if resp.status_code == 401 or resp.status_code == 403:
        return {"error": f"{cfg['name']} rejected the API key (HOMEOS_AGENT_KEY)"}
    if resp.status_code >= 400:
        return {"error": f"{cfg['name']} answered HTTP {resp.status_code}: {resp.text[:200]}"}
    try:
        data = resp.json()
        text = (data["choices"][0]["message"]["content"] or "").strip()
    except Exception:  # noqa: BLE001
        return {"error": f"{cfg['name']} sent an unreadable reply"}
    if not text:
        return {"error": f"{cfg['name']} sent an empty reply"}
    elapsed = round(time.monotonic() - started, 1)
    log.info("ask_agent: %s answered in %.1fs (session %s)", cfg["name"], elapsed, headers["X-Hermes-Session-Id"])
    return {"answer": text, "agent": cfg["name"], "elapsed_s": elapsed}
